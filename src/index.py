"""Sunshine Store catalog index.

A local SQLite catalog of pacman packages, AppStream app data (names, summaries, categories, icons),
the whole AUR and Flathub, so searching is instant instead of waiting on three slow tools.

Refresh policy (run by the optional background service, or on demand):
  - never built            -> build now
  - 36 hours since refresh -> refresh      (wall-clock)
  - 5 hours of active use  -> refresh      (counted 30 minutes at a time while the session is not idle)
The AUR and Flathub downloads are conditional (ETag / Last-Modified), so an unchanged catalog costs nothing.

CLI:  python3 index.py --tick | --refresh | --status
"""
from __future__ import annotations

import glob
import gzip
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "sunshine-store")
DB_PATH = os.path.join(CACHE, "index.db")
STATE_PATH = os.path.join(CACHE, "state.json")
AUR_DUMP = "https://aur.archlinux.org/packages-meta-v1.json.gz"
FLATHUB_APPSTREAM = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"
APPIMAGE_FEED = "https://appimage.github.io/feed.json"
APPIMAGE_DB = "https://appimage.github.io/database/"
FLATHUB_ICON = "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{}.png"
MAX_AGE = 36 * 3600          # refresh at least every 36 hours
MAX_ACTIVE = 5 * 3600        # ...or after 5 hours of active use
TICK = 30 * 60               # one service tick = 30 active minutes

SCHEMA = """
CREATE TABLE pkgs (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, name_l TEXT NOT NULL, key TEXT NOT NULL,
  title TEXT DEFAULT '', version TEXT DEFAULT '', desc TEXT DEFAULT '', repo TEXT DEFAULT '', app_id TEXT DEFAULT '',
  votes INTEGER DEFAULT 0, pop REAL DEFAULT 0, url TEXT DEFAULT '', maintainer TEXT DEFAULT '', ood INTEGER DEFAULT 0,
  icon TEXT DEFAULT '', cats TEXT DEFAULT '', is_app INTEGER DEFAULT 0
);
CREATE INDEX i_name ON pkgs(name_l);
CREATE INDEX i_key ON pkgs(key);
CREATE INDEX i_src ON pkgs(source, is_app);
"""

SUFFIXES = ("-bin", "-git", "-appimage", "-nightly", "-beta", "-dev", "-stable", "-electron", "-wayland")


def normalize(name: str) -> str:
    n = name.lower()
    if n.count(".") >= 2:                      # flatpak id org.mozilla.firefox -> firefox
        n = n.rsplit(".", 1)[-1]
    changed = True
    while changed:
        changed = False
        for s in SUFFIXES:
            if n.endswith(s):
                n, changed = n[: -len(s)], True
    return n


# ------------------------------------------------------------------ state
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(CACHE, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


def exists() -> bool:
    return os.path.exists(DB_PATH)


def due(st: dict | None = None) -> bool:
    st = st if st is not None else load_state()
    if not exists() or not st.get("last_refresh"):
        return True
    return (time.time() - st["last_refresh"] >= MAX_AGE) or (st.get("active_seconds", 0) >= MAX_ACTIVE)


def status() -> dict:
    st = load_state()
    n = 0
    if exists():
        try:
            n = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM pkgs").fetchone()[0]
        except sqlite3.Error:
            pass
    return {"built": exists(), "packages": n, "last_refresh": st.get("last_refresh"),
            "active_hours": round(st.get("active_seconds", 0) / 3600, 2), "due": due(st)}


# ------------------------------------------------------------------ fetch helpers
def _download(url: str, st: dict, tag: str) -> bytes | None:
    """Conditional GET; returns None when the server says nothing changed."""
    req = urllib.request.Request(url, headers={"User-Agent": "sunshine-store-indexer"})
    et, lm = st.get(tag + "_etag"), st.get(tag + "_lm")
    if et:
        req.add_header("If-None-Match", et)
    if lm:
        req.add_header("If-Modified-Since", lm)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            st[tag + "_etag"], st[tag + "_lm"] = r.headers.get("ETag"), r.headers.get("Last-Modified")
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise


def _cached_blob(tag: str, data: bytes | None) -> bytes | None:
    """Keep the last download on disk so a 304 can still rebuild the index."""
    path = os.path.join(CACHE, tag + ".blob")
    if data is not None:
        with open(path, "wb") as f:
            f.write(data)
        return data
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


# ------------------------------------------------------------------ builders
def _pacman_rows():
    out = subprocess.run(["pacman", "-Si"], capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
    for block in out.split("\n\n"):
        f = dict(re.findall(r"^(Repository|Name|Version|Description|URL)\s*:\s*(.*)$", block, re.M))
        if "Name" in f:
            yield ("pacman", f["Name"], f.get("Version", ""), f.get("Description", ""), f.get("Repository", ""), f.get("URL", ""))


def _appstream_apps():
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
                if not n.get("{http://www.w3.org/XML/1998/namespace}lang"):
                    title = (n.text or "").strip()
            for n in c.findall("summary"):
                if not n.get("{http://www.w3.org/XML/1998/namespace}lang"):
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
                    icon = i.text.strip()          # theme icon name
            cats = ",".join(x.text.strip() for x in c.findall("categories/category") if x.text)
            apps.setdefault(pkg, {"title": title, "summary": summary, "icon": icon, "cats": cats})
    return apps


def _flathub_rows(blob: bytes):
    root = ET.parse(io.BytesIO(gzip.decompress(blob))).getroot()
    for c in root.iter("component"):
        if c.get("type") not in ("desktop-application", "desktop", None):
            continue
        cid = (c.findtext("id") or "").strip()
        if not cid or "." not in cid:
            continue
        cid = re.sub(r"\.desktop$", "", cid)
        title = summary = ""
        for n in c.findall("name"):
            if not n.get("{http://www.w3.org/XML/1998/namespace}lang"):
                title = (n.text or "").strip()
        for n in c.findall("summary"):
            if not n.get("{http://www.w3.org/XML/1998/namespace}lang"):
                summary = (n.text or "").strip()
        cats = ",".join(x.text.strip() for x in c.findall("categories/category") if x.text)
        yield cid, title, summary, cats


def refresh(progress=lambda msg: None) -> dict:
    """Rebuild the catalog. Safe to interrupt: the old index stays until the new one is complete."""
    os.makedirs(CACHE, exist_ok=True)
    st = load_state()
    tmp = DB_PATH + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    ins = ("INSERT INTO pkgs(source,name,name_l,key,title,version,desc,repo,app_id,votes,pop,url,maintainer,ood,icon,cats,is_app)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    progress("Reading installed repositories…")
    apps = _appstream_apps()
    n = 0
    for _, name, ver, desc, repo, url in _pacman_rows():
        a = apps.get(name)
        db.execute(ins, ("pacman", name, name.lower(), normalize(name), (a or {}).get("title", ""), ver,
                         (a or {}).get("summary") or desc, repo, "", 0, 0, url, "", 0, (a or {}).get("icon", ""),
                         (a or {}).get("cats", ""), 1 if a else 0))
        n += 1

    progress("Updating the AUR catalog…")
    try:
        blob = _cached_blob("aur", _download(AUR_DUMP, st, "aur"))
        if blob:
            for r in json.loads(gzip.decompress(blob)):
                nm = r.get("Name", "")
                if not nm:
                    continue
                db.execute(ins, ("aur", nm, nm.lower(), normalize(nm), "", r.get("Version", ""), r.get("Description") or "", "", "",
                                 r.get("NumVotes", 0), r.get("Popularity", 0.0), r.get("URL") or "", r.get("Maintainer") or "",
                                 1 if r.get("OutOfDate") else 0, "", "", 0))
    except Exception as e:                      # offline: keep going with what we have
        progress(f"AUR catalog unavailable ({e})")

    progress("Updating Flathub…")
    try:
        blob = _cached_blob("flathub", _download(FLATHUB_APPSTREAM, st, "flathub"))
        if blob:
            for cid, title, summary, cats in _flathub_rows(blob):
                db.execute(ins, ("flatpak", title or cid.rsplit(".", 1)[-1], (title or cid).lower(), normalize(cid), title, "", summary,
                                 "flathub", cid, 0, 0, "", "", 0, FLATHUB_ICON.format(cid), cats, 1))
    except Exception as e:
        progress(f"Flathub catalog unavailable ({e})")

    progress("Updating AppImages…")
    try:
        blob = _cached_blob("appimage", _download(APPIMAGE_FEED, st, "appimage"))
        if blob:
            for it in json.loads(blob).get("items", []):
                try:
                    gh = next((l["url"] for l in (it.get("links") or []) if l.get("type") == "GitHub"), "")
                    nm = (it.get("name") or "").replace("_", " ").strip()
                    if not gh or not nm:
                        continue
                    icons = it.get("icons") or []
                    author = ((it.get("authors") or [{}])[0] or {}).get("name") or ""
                    cats = ",".join(str(c) for c in (it.get("categories") or []) if c)
                    db.execute(ins, ("appimage", nm, nm.lower(), normalize(nm), nm, "", it.get("description") or "", "appimage.github.io", gh,
                                     0, 0, "https://github.com/" + gh, str(author), 0,
                                     (APPIMAGE_DB + icons[0]) if icons else "", cats, 1))
                except (TypeError, AttributeError, KeyError, IndexError):
                    continue
    except Exception as e:
        progress(f"AppImage catalog unavailable ({e})")

    db.commit()
    db.close()
    os.replace(tmp, DB_PATH)
    st["last_refresh"], st["active_seconds"] = time.time(), 0
    save_state(st)
    progress("Catalog ready")
    return status()


def tick(progress=lambda msg: None) -> dict:
    """Called by the background timer every 30 minutes."""
    st = load_state()
    if not _session_idle():
        st["active_seconds"] = st.get("active_seconds", 0) + TICK
        save_state(st)
    if due(st):
        return refresh(progress)
    return status()


def _session_idle() -> bool:
    """True when every graphical session of this user reports IdleHint=yes (the timer runs from systemd, so look sessions up)."""
    try:
        sessions = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Sessions", "--value"],
                                  capture_output=True, text=True, timeout=5).stdout.split()
        if not sessions:
            return True                         # nobody logged in: nothing to count
        for sid in sessions:
            hint = subprocess.run(["loginctl", "show-session", sid, "-p", "IdleHint", "--value"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            if hint != "yes":
                return False
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------ queries
def _db():
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def search(query: str, limit: int = 400) -> list:
    """Rows matching the query across all sources, best first."""
    if not exists():
        return []
    q = query.strip().lower()
    if not q:
        return []
    like = f"%{q}%"
    db = _db()
    try:
        rows = db.execute(
            """SELECT *, CASE WHEN name_l = :q OR key = :q THEN 0
                              WHEN name_l LIKE :p THEN 1
                              WHEN title <> '' AND lower(title) LIKE :l THEN 2
                              WHEN name_l LIKE :l THEN 3 ELSE 4 END AS rank
               FROM pkgs
               WHERE name_l LIKE :l OR key LIKE :l OR lower(app_id) LIKE :l OR lower(title) LIKE :l OR lower(desc) LIKE :l
               ORDER BY rank, is_app DESC, votes DESC, length(name_l) LIMIT :n""",
            {"q": q, "p": q + "%", "l": like, "n": limit}).fetchall()
    finally:
        db.close()
    return rows


def by_names(names: list) -> list:
    if not exists() or not names:
        return []
    db = _db()
    try:
        marks = ",".join("?" * len(names))
        return db.execute(f"SELECT * FROM pkgs WHERE name_l IN ({marks}) OR key IN ({marks})",
                          [n.lower() for n in names] * 2).fetchall()
    finally:
        db.close()


def by_category(cats: list, source: str | None = None, limit: int = 12) -> list:
    if not exists() or not cats:
        return []
    db = _db()
    try:
        cond = " OR ".join("(',' || cats || ',') LIKE ?" for _ in cats)
        args = [f"%,{c},%" for c in cats]
        sql = f"SELECT * FROM pkgs WHERE is_app=1 AND ({cond})" + (" AND source=?" if source else "") + " ORDER BY length(name_l) LIMIT ?"
        return db.execute(sql, args + ([source] if source else []) + [limit]).fetchall()
    finally:
        db.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--status"
    say = lambda m: print(m, flush=True)  # noqa: E731
    if arg == "--refresh":
        print(json.dumps(refresh(say)))
    elif arg == "--tick":
        print(json.dumps(tick(say)))
    else:
        print(json.dumps(status()))
