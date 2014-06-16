"""

Implementation of the database layer for PyPI Package serving and
testresult storage.

"""

from __future__ import unicode_literals
try:
    import xmlrpc.client as xmlrpc
except ImportError:
    import xmlrpclib as xmlrpc

import py
html = py.xml.html

from devpi_common.vendor._pip import HTMLPage

from devpi_common.url import URL
from devpi_common.metadata import is_archive_of_project, BasenameMeta
from devpi_common.validation import normalize_name, ensure_unicode
from devpi_common.request import new_requests_session

from . import __version__ as server_version
from .model import ProjectInfo
from .keyfs import load_from_file, dump_to_file
from .log import threadlog, thread_current_log, thread_push_log



class IndexParser:

    def __init__(self, projectname):
        self.projectname = normalize_name(projectname)
        self.basename2link = {}
        self.crawllinks = set()
        self.egglinks = []

    def _mergelink_ifbetter(self, newurl):
        entry = self.basename2link.get(newurl.basename)
        if entry is None or (not entry.md5 and newurl.md5):
            self.basename2link[newurl.basename] = newurl
            threadlog.debug("adding link %s", newurl)
        else:
            threadlog.debug("ignoring candidate link %s", newurl)

    @property
    def releaselinks(self):
        """ return sorted releaselinks list """
        l = sorted(map(BasenameMeta, self.basename2link.values()),
                   reverse=True)
        return self.egglinks + [x.obj for x in l]

    def parse_index(self, disturl, html, scrape=True):
        p = HTMLPage(html, disturl.url)
        seen = set()
        for link in p.links:
            newurl = URL(link.url)
            if not newurl.is_valid_http_url():
                continue
            eggfragment = newurl.eggfragment
            if scrape and eggfragment:
                if not normalize_name(eggfragment).startswith(
                    self.projectname):
                    threadlog.debug("skip egg link %s (projectname: %s)",
                              newurl, self.projectname)
                    continue
                if newurl.basename:
                    # XXX seems we have to maintain a particular
                    # order to keep pip/easy_install happy with some
                    # packages (e.g. nose)
                    if newurl not in self.egglinks:
                        self.egglinks.insert(0, newurl)
                else:
                    threadlog.warn("cannot handle egg directory link (svn?) "
                              "skipping: %s (projectname: %s)",
                              newurl, self.projectname)
                continue
            if is_archive_of_project(newurl, self.projectname):
                if not newurl.is_valid_http_url():
                    threadlog.warn("unparseable/unsupported url: %r", newurl)
                else:
                    seen.add(newurl.url)
                    self._mergelink_ifbetter(newurl)
                    continue
        if scrape:
            for link in p.rel_links():
                if link.url not in seen:
                    disturl = URL(link.url)
                    if disturl.is_valid_http_url():
                        self.crawllinks.add(disturl)

def parse_index(disturl, html, scrape=True):
    if not isinstance(disturl, URL):
        disturl = URL(disturl)
    projectname = disturl.basename or disturl.parentbasename
    parser = IndexParser(projectname)
    parser.parse_index(disturl, html, scrape=scrape)
    return parser

class XMLProxy(object):
    def __init__(self, url):
        self._url = url
        self._session = new_requests_session(agent=("server", server_version))
        self._session.headers["content-type"] = "text/xml"
        self._session.headers["Accept"] = "text/xml"

    def list_packages_with_serial(self):
        return self._execute("list_packages_with_serial")

    def changelog_since_serial(self, serial):
        return self._execute("changelog_since_serial", serial)

    def _execute(self, method, *args):
        payload = xmlrpc.dumps(args, method)
        threadlog.debug("-> %s%s" %(method, args))
        try:
            reply = self._session.post(self._url, data=payload, stream=False)
        except Exception as exc:
            threadlog.warn("%s: error %s with remote %s",
                           method, exc, self._url)
            return None
        if reply.status_code != 200:
            threadlog.warn("%s: status_code %s with remote %s", method,
                     reply.status_code, self._url)
            return None
        res = xmlrpc.loads(reply.content)[0][0]
        threadlog.debug("<- %s%s: %s" %(method, args, res))
        return res


def perform_crawling(pypistage, result, numthreads=10):
    pending = set(result.crawllinks)
    while pending:
        try:
            crawlurl = pending.pop()
        except KeyError:
            break
        threadlog.info("visiting crawlurl %s", crawlurl)
        response = pypistage.httpget(crawlurl.url, allow_redirects=True)
        threadlog.info("crawlurl %s %s", crawlurl, response)
        assert hasattr(response, "status_code")
        if not isinstance(response, int) and response.status_code == 200:
            ct = response.headers.get("content-type", "").lower()
            if ct.startswith("text/html"):
                result.parse_index(
                    URL(response.url), response.text, scrape=False)
                continue
        threadlog.warn("crawlurl %s status %s", crawlurl, response)


def invalidate_on_version_change(basedir):
    verfile = basedir.join(".mirrorversion")
    if not verfile.check():
        ver = "0"
    else:
        ver = verfile.read()
    if ver != PyPIStage.VERSION:
        if basedir.check():
            threadlog.info("version format change: removing root/pypi state")
            basedir.remove()
    verfile.dirpath().ensure(dir=1)
    verfile.write(PyPIStage.VERSION)

class PyPIStage:
    VERSION = "4"
    name = "root/pypi"
    ixconfig = dict(bases=(), volatile=False, type="mirror")

    def __init__(self, xom):
        self.keyfs = xom.keyfs
        self.httpget = xom.httpget
        self.filestore = xom.filestore
        self.pypimirror = xom.pypimirror
        self.xom = xom
        if xom.is_replica():
            url = xom.config.master_url
            self.PYPIURL_SIMPLE = url.joinpath("root/pypi/+simple/").url
        else:
            self.PYPIURL_SIMPLE = PYPIURL_SIMPLE

    def getprojectnames(self):
        """ return list of all projects served through the mirror. """
        return sorted(self.pypimirror.name2serials)

    getprojectnames_perstage = getprojectnames

    def _dump_project_cache(self, projectname, entries, serial):
        normname = normalize_name(projectname)
        dumplist = [(entry.relpath, entry.md5, entry.key.name)
                            for entry in entries]
        data = {"serial": serial,
                "latest_serial": serial,
                "entrylist": dumplist,
                "projectname": projectname}
        threadlog.debug("saving data for %s: %s", projectname, data)
        self.keyfs.PYPILINKS(name=normname).set(data)

    def _load_project_cache(self, projectname):
        normname = normalize_name(projectname)
        data = self.keyfs.PYPILINKS(name=normname).get()
        #log.debug("load data for %s: %s", projectname, data)
        return data

    def _load_cache_entries(self, projectname):
        cache = self._load_project_cache(projectname)
        if cache and cache["serial"] >= cache["latest_serial"]:
            get_proxy = self.filestore.get_proxy_file_entry
            return [get_proxy(relpath, md5, keyname=keyname)
                        for relpath, md5, keyname in cache["entrylist"]]

    def getreleaselinks(self, projectname):
        """ return all releaselinks from the index and referenced scrape
        pages, returning cached entries if we have a recent enough
        request stored locally.

        If the pypi server cannot be reached return -1
        If pypi does not return a fresh enough page although we know it
        must exist, return -2.
        """
        entries = self._load_cache_entries(projectname)
        if entries is not None:
            return entries
        info = self.get_project_info(projectname)
        if not info:
            return 404
        # get the simple page for the project
        url = self.PYPIURL_SIMPLE + info.name + "/"
        threadlog.debug("visiting index %s", url)
        response = self.httpget(url, allow_redirects=True)
        if response.status_code != 200:
            return response.status_code

        if self.xom.is_replica():
            devpi_serial = int(response.headers["X-DEVPI-SERIAL"])
            self.keyfs.notifier.wait_tx_serial(devpi_serial)
            # XXX raise TransactionRestart to get a consistent clean view
            self.keyfs.commit_transaction_in_thread()
            self.keyfs.begin_transaction_in_thread()
            entries = self._load_cache_entries(projectname)
            if entries is not None:
                return entries
            threadlog.error("did not get cached entries for %s", projectname)
            return 502

        # determine and check real project name
        real_projectname = response.url.strip("/").split("/")[-1]
        assert real_projectname == info.name

        # check that we got a fresh enough page
        serial = int(response.headers["X-PYPI-LAST-SERIAL"])
        newest_serial = self.pypimirror.name2serials.get(info.name, -1)
        if serial < newest_serial:
            threadlog.warn("%s: pypi returned serial %s, expected %s",
                     real_projectname, serial, newest_serial)
            return -2  # the page we got is not fresh enough
        threadlog.debug("%s: got response with serial %s" %
                  (real_projectname, serial))

        # parse simple index's link and perform crawling
        assert response.text is not None, response.text
        result = parse_index(response.url, response.text)
        perform_crawling(self, result)
        releaselinks = list(result.releaselinks)

        self.keyfs.restart_as_write_transaction()

        # compute release link entries and cache according to serial
        entries = [self.filestore.maplink(link) for link in releaselinks]
        self._dump_project_cache(real_projectname, entries, serial)
        return entries

    getreleaselinks_perstage = getreleaselinks

    def get_project_info(self, name):
        norm_name = normalize_name(name)
        name = self.pypimirror.normname2name.get(norm_name, norm_name)
        if name in self.pypimirror.name2serials:
            return ProjectInfo(self, name)

    get_project_info_perstage = get_project_info

    def op_with_bases(self, opname, **kw):
        return [(self, getattr(self, opname)(**kw))]

    def get_projectconfig(self, name):
        releaselinks = self.getreleaselinks(name)
        if isinstance(releaselinks, int):
            return releaselinks
        data = {}
        for link in releaselinks:
            basename = link.basename
            if link.eggfragment:
                version = "egg=" + link.eggfragment
            else:
                version = BasenameMeta(basename).version
            verdata = data.setdefault(version, {})
            verdata["name"] = name
            verdata["version"] = version
            files = verdata.setdefault("+files", {})
            files[basename] = link.relpath
        return data

    get_projectconfig_perstage = get_projectconfig

    def get_description(self, name, version):
        link = "https://pypi.python.org/pypi/%s/%s/" % (name, version)
        return html.div("please refer to description on remote server ",
            html.a(link, href=link)).unicode(indent=2)


class PyPIMirror:
    def __init__(self, xom):
        self.xom = xom
        self.keyfs = keyfs = xom.keyfs
        self.path_name2serials = str(
            keyfs.basedir.join(PyPIStage.name, ".name2serials"))

    def init_pypi_mirror(self, proxy):
        """ initialize pypi mirror if no mirror state exists. """
        self.name2serials = self.load_name2serials(proxy)
        # create a mapping of normalized name to real name
        self.normname2name = d = dict()
        for name in self.name2serials:
            norm = normalize_name(name)
            if norm != name:
                d[norm] = name

    def load_name2serials(self, proxy):
        name2serials = load_from_file(self.path_name2serials, {})
        if name2serials:
            threadlog.info("reusing already cached name/serial list")
        else:
            threadlog.info("retrieving initial name/serial list")
            name2serials = proxy.list_packages_with_serial()
            if name2serials is None:
                from devpi_server.main import fatal
                fatal("mirror initialization failed: "
                      "pypi.python.org not reachable")
            dump_to_file(name2serials, self.path_name2serials)
        return name2serials

    def set_project_serial(self, name, serial):
        """ set the current serial and fill normalization table. """
        self.name2serials[name] = serial
        n = normalize_name(name)
        if n != name:
            self.normname2name[n] = name
        return n

    def thread_run(self, proxy):
        log = thread_push_log("[MIR]")
        log.info("changelog/update tasks starting")
        while 1:
            # get changes since the maximum serial we are aware of
            current_serial = max(itervalues(self.name2serials))
            changelog = proxy.changelog_since_serial(current_serial)
            if changelog:
                with self.keyfs.transaction(write=True):
                    self.process_changelog(changelog)
            self.thread.sleep(self.xom.config.args.refresh)

    def process_changelog(self, changelog):
        changed = set()
        log = thread_current_log()
        for x in changelog:
            name, version, action, date, serial = x
            # XXX remove names if action == "remove" and version is None
            name = ensure_unicode(name)
            normname = self.set_project_serial(name, serial)
            changed.add(normname)
            key = self.keyfs.PYPILINKS(name=normname)
            cache = key.get()
            if cache:
                if cache["latest_serial"] >= serial:  # should this happen?
                    return  # the cached serial is new enough
                cache["latest_serial"] = serial
                key.set(cache)
                log.debug("set latest_serial of %s to %s",
                          normname, serial)
            #else:
            #    log.debug("no cache found for %s" % name)
        # XXX include name2serials writing into the ongoing transaction
        # as an external rename (not managed through keyfs)
        if self.name2serials:
            dump_to_file(self.name2serials, self.path_name2serials)

        log.debug("processed changelog of size %d: %s" %(
                  len(changelog), ",".join(changed)))





PYPIURL_SIMPLE = "https://pypi.python.org/simple/"
PYPIURL = "https://pypi.python.org/"

def itervalues(d):
    return getattr(d, "itervalues", d.values)()
def iteritems(d):
    return getattr(d, "iteritems", d.items)()
