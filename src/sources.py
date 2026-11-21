"""Package source adapters: upstream data providers for the catalogue.

Every adapter answers one question: "what does this source offer right now?" It returns
    (rows, validator)   fresh data, plus the validator to remember (ETag / repository fingerprint ...)
    (UNCHANGED, None)   the source says nothing changed: the catalogue keeps its snapshot, nothing is rebuilt
or raises SourceUnavailable. An unavailable source is *never* read as an empty one: the refresh engine keeps that source's
last good snapshot and records the failure.
"""
from __future__ import annotations

import glob
import gzip
import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from identity import normalize

UNCHANGED = "unchanged"
PARSER_VERSION = 2          # bump when row parsing changes: forces one full re-fetch of every source

AUR_DUMP = "https://aur.archlinux.org/packages-meta-v1.json.gz"
FLATHUB_APPSTREAM = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"
APPIMAGE_FEED = "https://appimage.github.io/feed.json"
APPIMAGE_DB = "https://appimage.github.io/database/"
FLATHUB_ICON = "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{}.png"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


class SourceUnavailable(Exception):
    pass


def offline() -> bool:
    return os.environ.get("SUNSHINE_STORE_OFFLINE") == "1"


def row(source, name, version="", desc="", repo="", app_id="", votes=0, pop=0.0, url="", maintainer="", ood=0, icon="", cats="",
        is_app=0, title="", key=None):
    """One catalogue record, in catalogue.COLS order."""
    return (source, name, name.lower(), key if key is not None else normalize(name), title, version, desc, repo, app_id,
            votes, pop, url, maintainer, ood, icon, cats, is_app)


def _http(url: str, validator: str | None, force: bool) -> tuple:
    """Conditional GET. Returns (bytes | None when unchanged, new validator JSON)."""
    if offline():
        raise SourceUnavailable("offline")
    req = urllib.request.Request(url, headers={"User-Agent": "sunshine-store-indexer"})
    old = {}
    if validator and not force:
        try:
            old = json.loads(validator)
        except ValueError:
            old = {}
    if old.get("etag"):
        req.add_header("If-None-Match", old["etag"])
    if old.get("lm"):
        req.add_header("If-Modified-Since", old["lm"])
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return data, json.dumps({"etag": r.headers.get("ETag"), "lm": r.headers.get("Last-Modified")})
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, validator
        raise SourceUnavailable(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise SourceUnavailable(str(getattr(e, "reason", e)))


class Source:
    name = ""
    label = ""
    min_rows = 100                  # a snapshot smaller than this is treated as a failed fetch, not as an empty source
    min_interval = 0                # seconds between checks (the cheap ones check on every refresh)

    def fetch(self, validator: str | None, force: bool = False):
        raise NotImplementedError


# ------------------------------------------------------------------------------------------------ pacman
class Pacman(Source):
    name, label, min_rows = "pacman", "Reading repositories…", 100

    def _fingerprint(self) -> str:
        files = sorted(glob.glob("/var/lib/pacman/sync/*.db") + glob.glob("/usr/share/swcatalog/xml/*.xml.gz")
                       + glob.glob("/var/lib/swcatalog/xml/*.xml.gz"))
        parts = []
        for f in files:
            try:
                st = os.stat(f)
                parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                pass
        return "|".join(parts)

    def fetch(self, validator, force=False):
        fp = self._fingerprint()
        if not fp:
            raise SourceUnavailable("no pacman databases found")
        if validator == fp and not force:
            return UNCHANGED, None
        try:
            out = subprocess.run(["pacman", "-Si"], capture_output=True, text=True, timeout=120,
                                 env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise SourceUnavailable(str(e))
        apps = _appstream_apps()
        rows = []
        for block in out.split("\n\n"):
            f = dict(re.findall(r"^(Repository|Name|Version|Description|URL)\s*:\s*(.*)$", block, re.M))
            if "Name" not in f:
                continue
            a = apps.get(f["Name"]) or {}
            rows.append(row("pacman", f["Name"], f.get("Version", ""), a.get("summary") or f.get("Description", ""), f.get("Repository", ""),
                            url=f.get("URL", ""), icon=a.get("icon", ""), cats=a.get("cats", ""), is_app=1 if a else 0,
                            title=a.get("title", "")))
        return rows, fp


def _appstream_apps() -> dict:
    """{pkgname: dict(title, summary, icon, cats)} from the distro AppStream catalogs."""
    apps = {}
    for path in glob.glob("/usr/share/swcatalog/xml/*.xml.gz") + glob.glob("/var/lib/swcatalog/xml/*.xml.gz"):
        try:
            with gzip.open(path, "rb") as f:
                root = ET.parse(f).getroot()
        except (OSError, ET.ParseError):
            continue
        origin = root.get("origin", "")
        for c in root.iter("component"):
            if c.get("type") not in ("desktop-application", "console-application", "web-application"):
                continue
            pkg = (c.findtext("pkgname") or "").strip()
            if not pkg:
                continue
            title = summary = ""
            for n in c.findall("name"):
                if not n.get(XML_LANG):
                    title = (n.text or "").strip()
            for n in c.findall("summary"):
                if not n.get(XML_LANG):
                    summary = (n.text or "").strip()
            icon = ""
            for i in c.findall("icon"):
                if i.get("type") == "cached" and i.text:
                    for size in ("128x128", "64x64"):
                        p = f"/usr/share/swcatalog/icons/{origin}/{size}/{i.text.strip()}"
                        if os.path.exists(p):
                            icon = p
                            break
                elif i.get("type") == "stock" and i.text and not icon:
                    icon = i.text.strip()
            cats = ",".join(x.text.strip() for x in c.findall("categories/category") if x.text)
            apps.setdefault(pkg, {"title": title, "summary": summary, "icon": icon, "cats": cats})
    return apps


# ------------------------------------------------------------------------------------------------ AUR
class Aur(Source):
    name, label, min_rows, min_interval = "aur", "Updating the AUR…", 1000, 3600

    def fetch(self, validator, force=False):
        data, new = _http(AUR_DUMP, validator, force)
        if data is None:
            return UNCHANGED, None
        try:
            items = json.loads(gzip.decompress(data))
        except (OSError, ValueError) as e:
            raise SourceUnavailable(f"bad AUR data: {e}")
        rows = [row("aur", r["Name"], r.get("Version", ""), r.get("Description") or "", votes=r.get("NumVotes", 0),
                    pop=r.get("Popularity", 0.0), url=r.get("URL") or "", maintainer=r.get("Maintainer") or "",
                    ood=1 if r.get("OutOfDate") else 0) for r in items if r.get("Name")]
        return rows, new


# ------------------------------------------------------------------------------------------------ Flathub
class Flathub(Source):
    name, label, min_rows, min_interval = "flatpak", "Updating Flathub…", 200, 6 * 3600

    def fetch(self, validator, force=False):
        data, new = _http(FLATHUB_APPSTREAM, validator, force)
        if data is None:
            return UNCHANGED, None
        try:
            root = ET.parse(io.BytesIO(gzip.decompress(data))).getroot()
        except (OSError, ET.ParseError) as e:
            raise SourceUnavailable(f"bad Flathub data: {e}")
        rows = []
        for c in root.iter("component"):
            if c.get("type") not in ("desktop-application", "desktop", None):
                continue
            cid = (c.findtext("id") or "").strip()
            if not cid or "." not in cid:
                continue
            cid = re.sub(r"\.desktop$", "", cid)
            title = summary = ""
            for n in c.findall("name"):
                if not n.get(XML_LANG):
                    title = (n.text or "").strip()
            for n in c.findall("summary"):
                if not n.get(XML_LANG):
                    summary = (n.text or "").strip()
            cats = ",".join(x.text.strip() for x in c.findall("categories/category") if x.text)
            rows.append(row("flatpak", title or cid.rsplit(".", 1)[-1], desc=summary, repo="flathub", app_id=cid,
                            icon=FLATHUB_ICON.format(cid), cats=cats, is_app=1, title=title, key=normalize(cid)))
        return rows, new


# ------------------------------------------------------------------------------------------------ AppImage
class AppImage(Source):
    name, label, min_rows, min_interval = "appimage", "Updating AppImages…", 100, 24 * 3600

    def fetch(self, validator, force=False):
        data, new = _http(APPIMAGE_FEED, validator, force)
        if data is None:
            return UNCHANGED, None
        try:
            items = json.loads(data).get("items", [])
        except ValueError as e:
            raise SourceUnavailable(f"bad AppImage feed: {e}")
        rows = []
        for it in items:
            try:
                gh = next((l["url"] for l in (it.get("links") or []) if l.get("type") == "GitHub"), "")
                nm = (it.get("name") or "").replace("_", " ").strip()
                if not gh or not nm:
                    continue
                icons = it.get("icons") or []
                author = ((it.get("authors") or [{}])[0] or {}).get("name") or ""
                cats = ",".join(str(c) for c in (it.get("categories") or []) if c)
                rows.append(row("appimage", nm, desc=it.get("description") or "", repo="appimage.github.io", app_id=gh,
                                url="https://github.com/" + gh, maintainer=str(author), icon=(APPIMAGE_DB + icons[0]) if icons else "",
                                cats=cats, is_app=1, title=nm))
            except (TypeError, AttributeError, KeyError, IndexError):
                continue
        return rows, new


def default_sources() -> list:
    return [Pacman(), Aur(), Flathub(), AppImage()]
