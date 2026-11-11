"""Sunshine Store backend: instant local catalog search, plus pacman / AUR / Flatpak actions."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import appimage
import index

SOURCES = ("aur", "pacman", "flatpak", "appimage", "snap")
ORDERS = {
    "aur-first": ("aur", "pacman", "flatpak", "appimage", "snap"),
    "arch-first": ("pacman", "aur", "flatpak", "appimage", "snap"),
    "pacman-flatpak": ("pacman", "flatpak", "appimage", "aur", "snap"),
}
ORDER_LABELS = {
    "aur-first": "AUR → Pacman → Flatpak → AppImage → Snap",
    "arch-first": "Pacman → AUR → Flatpak → AppImage → Snap  (official Arch packages first)",
    "pacman-flatpak": "Pacman → Flatpak → AppImage → AUR → Snap  (safest first)",
}
DEFAULT_ORDER = ORDERS["aur-first"]
VARIANT_SUFFIXES = index.SUFFIXES
AUR_RPC = "https://aur.archlinux.org/rpc/v5"

CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "sunshine-store", "config.json")

# Interests asked on first launch: id, emoji, label, curated apps, AppStream categories.
INTERESTS = [
    ("web", "🌐", "Browsing & chat", ["firefox", "brave-bin", "chromium", "thunderbird", "telegram-desktop", "discord", "signal-desktop"], ["Network"]),
    ("media", "🎬", "Movies & music", ["vlc", "mpv", "spotify", "obs-studio", "audacious", "strawberry"], ["AudioVideo", "Audio", "Video"]),
    ("games", "🎮", "Games", ["steam", "lutris", "heroic-games-launcher-bin", "prismlauncher", "retroarch", "bottles"], ["Game"]),
    ("create", "🎨", "Drawing, photo & video", ["gimp", "krita", "inkscape", "blender", "kdenlive", "darktable", "godot"], ["Graphics"]),
    ("study", "📚", "Study & office", ["libreoffice-fresh", "obsidian", "okular", "zotero", "anki", "calibre", "onlyoffice-bin"], ["Office", "Education"]),
    ("code", "💻", "Programming", ["visual-studio-code-bin", "neovim", "jetbrains-toolbox", "docker", "git", "postman-bin"], ["Development"]),
    ("science", "🔬", "Science & maths", ["octave", "geogebra", "stellarium", "qalculate-gtk", "kalzium"], ["Science", "Math"]),
    ("tools", "🧰", "Handy tools", ["timeshift", "bleachbit", "gparted", "keepassxc", "bitwarden", "ark"], ["Utility", "System"]),
]


class NoCatalog(Exception):
    """The local catalog has not been built yet."""


@dataclass
class Pkg:
    name: str
    source: str                    # "aur" | "pacman" | "flatpak"
    title: str = ""
    version: str = ""
    desc: str = ""
    installed: bool = False
    votes: int = 0
    popularity: float = 0.0
    url: str = ""
    repo: str = ""
    app_id: str = ""
    maintainer: str = ""
    out_of_date: bool = False
    icon: str = ""                 # absolute path, http(s) URL, or theme icon name
    cats: str = ""

    @property
    def ident(self) -> str:
        return self.app_id or self.name

    @property
    def label(self) -> str:
        return self.title or self.name

    @property
    def is_variant(self) -> bool:
        return self.name.endswith(VARIANT_SUFFIXES)


@dataclass
class App:
    """One logical application, possibly offered by several sources."""
    key: str
    items: dict = field(default_factory=dict)       # source -> Pkg (best candidate)
    variants: list = field(default_factory=list)    # other AUR packages: -bin, -git, ...

    def pick(self, order=DEFAULT_ORDER) -> Pkg:
        for s in order:
            if s in self.items:
                return self.items[s]
        return next(iter(self.items.values()))

    @property
    def installed(self) -> bool:
        return any(p.installed for p in self.items.values())

    @property
    def sources(self):
        return [s for s in SOURCES if s in self.items]

    @property
    def icon(self) -> str:
        for p in self.items.values():
            if p.icon:
                return p.icon
        return ""


# ---------------------------------------------------------------- config
def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- background service
UNIT = "sunshine-store-indexer.timer"


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def service_enabled() -> bool:
    r = _systemctl("is-enabled", UNIT)
    return bool(r and r.stdout.strip() == "enabled")


def set_service(on: bool) -> bool:
    r = _systemctl("enable" if on else "disable", "--now", UNIT)
    return bool(r and r.returncode == 0)


def catalog_summary() -> str:
    s = index.status()
    if not s["built"]:
        return "Catalog not built yet."
    ago = "never"
    if s["last_refresh"]:
        h = (time.time() - s["last_refresh"]) / 3600
        ago = f"{int(h * 60)} minutes ago" if h < 1 else f"{h:.1f} hours ago"
    return f"{s['packages']:,} packages · updated {ago} · {s['active_hours']} h of active use since"


# ---------------------------------------------------------------- installed state
_cache: dict = {}


def _run(cmd, timeout=45) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/local/bin"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def installed_pacman(fresh=False) -> set:
    t, v = _cache.get("pac", (0, set()))
    if fresh or time.time() - t > 30:
        v = set(_run(["pacman", "-Qq"]).split())
        _cache["pac"] = (time.time(), v)
    return v


def installed_flatpak(fresh=False) -> set:
    if not shutil.which("flatpak"):
        return set()
    t, v = _cache.get("fp", (0, set()))
    if fresh or time.time() - t > 60:
        v = set(_run(["flatpak", "list", "--app", "--columns=application"]).split())
        _cache["fp"] = (time.time(), v)
    return v


def installed_appimage() -> set:
    return appimage.installed_repos()


def installed_snap() -> set:
    if not shutil.which("snap"):
        return set()
    t, v = _cache.get("snap", (0, set()))
    if time.time() - t > 60:
        v = {l.split()[0] for l in _run(["snap", "list"]).splitlines()[1:] if l.strip()}
        _cache["snap"] = (time.time(), v)
    return v


def refresh_installed():
    installed_pacman(True)
    installed_flatpak(True)


# ---------------------------------------------------------------- rows -> apps
def _pkg(row, hp, hf) -> Pkg:
    src = row["source"]
    if src == "flatpak":
        inst = row["app_id"] in hf
    elif src == "appimage":
        inst = row["app_id"] in installed_appimage()
    else:
        inst = row["name"] in hp
    nm = row["app_id"] and row["name"] or row["name"]
    return Pkg(name=row["name"], source=src, title=row["title"], version=row["version"], desc=row["desc"], installed=inst,
               votes=row["votes"], popularity=row["pop"], url=row["url"], repo=row["repo"], app_id=row["app_id"],
               maintainer=row["maintainer"], out_of_date=bool(row["ood"]), icon=row["icon"], cats=row["cats"])


def rows_to_apps(rows, source: str | None = None, limit: int = 150) -> list:
    hp, hf = installed_pacman(), installed_flatpak()
    groups: dict = {}
    order: list = []
    for r in rows:
        if source and r["source"] != source:
            continue
        p = _pkg(r, hp, hf)
        key = r["key"]
        app = groups.get(key)
        if app is None:
            app = groups[key] = App(key)
            order.append(key)
        if p.source == "aur" and p.is_variant:
            if len(app.variants) < 20:
                app.variants.append(p)
            continue
        cur = app.items.get(p.source)
        if cur is None or (p.source == "aur" and p.votes > cur.votes):
            app.items[p.source] = p
    out = []
    for k in order:
        a = groups[k]
        if not a.items and a.variants:               # only variants exist: promote the most voted
            best = max(a.variants, key=lambda v: v.votes)
            a.items["aur"] = best
            a.variants.remove(best)
        if a.items:
            out.append(a)
    return out[:limit]


def snap_search(query: str) -> list:
    """Live Snap Store search; only when snapd is installed (there is no bulk catalog to cache)."""
    if not shutil.which("snap"):
        return []
    try:
        url = "https://api.snapcraft.io/v2/snaps/find?fields=title,summary,version,publisher,media&q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"Snap-Device-Series": "16", "User-Agent": "sunshine-store"})
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.load(r).get("results", [])
    except Exception:
        return []
    have = installed_snap()
    out = []
    for x in res[:20]:
        s = x.get("snap", {})
        icon = next((m["url"] for m in s.get("media", []) if m.get("type") == "icon"), "")
        out.append(Pkg(name=x.get("name", ""), source="snap", title=s.get("title", ""), version=x.get("version", "") or "",
                       desc=s.get("summary", ""), installed=x.get("name", "") in have, icon=icon,
                       maintainer=(s.get("publisher") or {}).get("display-name", "")))
    return out


def search_all(query: str, source: str | None = None) -> list:
    if not index.exists():
        raise NoCatalog()
    apps = rows_to_apps(index.search(query), source)
    if source in (None, "snap"):
        by_key = {a.key: a for a in apps}
        for p in snap_search(query):
            k = index.normalize(p.name)
            a = by_key.get(k)
            if a is None:
                a = by_key[k] = App(k)
                apps.append(a)
            a.items.setdefault("snap", p)
    return apps


def resolve(names: list) -> list:
    """Curated names -> Apps in the given order (repo packages preferred over AUR for the same name)."""
    if not index.exists():
        raise NoCatalog()
    rows = index.by_names(names)
    hp, hf = installed_pacman(), installed_flatpak()
    by_name: dict = {}
    for r in rows:
        cur = by_name.get(r["name_l"])
        if cur is None or (cur["source"] == "aur" and r["source"] != "aur"):
            by_name[r["name_l"]] = r
    apps = []
    for n in names:
        r = by_name.get(n.lower())
        if r is None:
            continue
        a = App(r["key"])
        a.items[r["source"]] = _pkg(r, hp, hf)
        # also offer the same app from other sources when the catalog has them
        for r2 in index.search(r["key"], 60):
            if r2["key"] == r["key"] and r2["source"] not in a.items and not (r2["source"] == "aur" and r2["name"].endswith(VARIANT_SUFFIXES)):
                a.items[r2["source"]] = _pkg(r2, hp, hf)
        apps.append(a)
    return apps


def interest_shelf(iid: str, per: int = 10) -> tuple:
    """(title, [App...]) for one interest: curated apps first, then repo apps from matching categories."""
    for i, emoji, label, names, cats in INTERESTS:
        if i == iid:
            apps = resolve(names)
            seen = {a.key for a in apps}
            hp, hf = installed_pacman(), installed_flatpak()
            for r in index.by_category(cats, "pacman", per * 2):
                if r["key"] not in seen and len(apps) < per:
                    a = App(r["key"])
                    a.items["pacman"] = _pkg(r, hp, hf)
                    apps.append(a)
                    seen.add(r["key"])
            return f"{emoji}  {label}", apps[:per]
    return iid, []


def trending_aur(n: int = 12) -> list:
    if not index.exists():
        return []
    import sqlite3
    db = index._db()
    try:
        rows = db.execute("SELECT * FROM pkgs WHERE source='aur' AND ood=0 ORDER BY pop DESC LIMIT ?", (n * 3,)).fetchall()
    finally:
        db.close()
    return rows_to_apps(rows, "aur", n)


def related(base: str) -> list:
    """AUR variants of an app, straight from the catalog: base, base-bin, base-git, base-beta-bin ..."""
    suf = "|".join(re.escape(x.lstrip("-")) for x in VARIANT_SUFFIXES + ("-esr", "-stable", "-lts", "-qt", "-gtk"))
    pat = re.compile(rf"^{re.escape(base)}(-({suf}))*$")
    hp, hf = installed_pacman(), installed_flatpak()
    out = [_pkg(r, hp, hf) for r in index.search(base, 800) if r["source"] == "aur" and pat.match(r["name"])]
    out.sort(key=lambda p: (p.name != base, p.name.endswith("-git"), p.name.endswith("-bin"), -p.votes, p.name))
    return out[:40]


def live_aur(name: str) -> dict:
    """Fresh votes/version/out-of-date for one AUR package (called when its page opens)."""
    try:
        req = urllib.request.Request(f"{AUR_RPC}/info?arg[]=" + urllib.parse.quote(name), headers={"User-Agent": "sunshine-store"})
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.load(r).get("results", [])
        if res:
            x = res[0]
            return {"version": x.get("Version", ""), "votes": x.get("NumVotes", 0), "out_of_date": bool(x.get("OutOfDate")),
                    "maintainer": x.get("Maintainer") or "", "popularity": x.get("Popularity", 0.0)}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- actions
_APPIMAGE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appimage.py")


def command_for(p: Pkg, remove: bool = False) -> str:
    if p.source == "appimage":
        return f"sunshine-store-appimage {'remove' if remove else 'install'} {p.app_id}"
    if p.source == "snap":
        return f"sudo snap {'remove' if remove else 'install'} {p.name}"
    if p.source == "pacman":
        return f"sudo pacman -{'Rns' if remove else 'S'} {p.name}"
    if p.source == "aur":
        return f"yay -{'Rns' if remove else 'S'} {p.name}"
    return f"flatpak {'uninstall' if remove else 'install flathub'} {p.app_id}"


def argv_for(p: Pkg, remove: bool = False) -> list:
    """Non-interactive argv the GUI runs (polkit prompts for privileges)."""
    if p.source == "appimage":
        import sys
        if remove:
            return [sys.executable, _APPIMAGE_PY, "remove", p.app_id]
        return [sys.executable, _APPIMAGE_PY, "install", p.app_id, p.label, p.icon]
    if p.source == "snap":
        return ["pkexec", "snap", "remove" if remove else "install", p.name]
    if p.source == "pacman":
        return ["pkexec", "pacman", "-Rns" if remove else "-S", "--noconfirm", *([] if remove else ["--needed"]), p.name]
    if p.source == "aur":
        base = ["yay", "--noconfirm", "--sudo", "pkexec"]
        return base + (["-Rns", p.name] if remove else ["-S", "--needed", p.name])
    if remove:
        return ["flatpak", "uninstall", "-y", "--noninteractive", p.app_id]
    return ["flatpak", "install", "-y", "--noninteractive", "flathub", p.app_id]


# ---------------------------------------------------------------- installed list
def _desktop_entries() -> dict:
    import glob
    out = {}
    for path in glob.glob("/usr/share/applications/*.desktop"):
        icon, hidden, kind = "", False, ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("["):
                        if line.strip() != "[Desktop Entry]" and kind:
                            break
                    elif line.startswith("Icon="):
                        icon = line[5:].strip()
                    elif line.startswith(("NoDisplay=true", "Hidden=true")):
                        hidden = True
                    elif line.startswith("Type="):
                        kind = line[5:].strip()
        except OSError:
            continue
        if kind == "Application" and not hidden:
            out[path] = icon
    return out


def _pacman_infos() -> dict:
    infos = {}
    for block in _run(["pacman", "-Qi"], 60).split("\n\n"):
        f = dict(re.findall(r"^(Name|Version|Description)\s*:\s*(.*)$", block, re.M))
        if "Name" in f:
            infos[f["Name"]] = (f.get("Version", ""), f.get("Description", ""))
    return infos


def list_installed(apps_only: bool = True) -> list:
    infos = _pacman_infos()
    foreign = set(_run(["pacman", "-Qmq"]).split())
    explicit = set(_run(["pacman", "-Qeq"]).split())
    icons = {}
    if apps_only:
        entries = _desktop_entries()
        out = _run(["pacman", "-Qo", *entries.keys()], 60) if entries else ""
        for m in re.finditer(r"^(\S+) is owned by (\S+) ", out, re.M):
            icons.setdefault(m.group(2), entries.get(m.group(1), ""))
        names = [n for n in sorted(explicit) if n in icons]
    else:
        names = sorted(explicit)
    pkgs = []
    for n in names:
        ver, desc = infos.get(n, ("", ""))
        pkgs.append(Pkg(name=n, source="aur" if n in foreign else "pacman", installed=True, version=ver, desc=desc, icon=icons.get(n, "")))
    for fid in sorted(installed_flatpak(True)):
        pkgs.append(Pkg(name=fid.rsplit(".", 1)[-1], source="flatpak", app_id=fid, repo="flathub", installed=True, icon=fid))
    for repo, e in appimage.load().items():
        pkgs.append(Pkg(name=e.get("name", repo), title=e.get("name", repo), source="appimage", app_id=repo, repo="appimage.github.io",
                        version=e.get("version", ""), installed=True, icon=e.get("icon", "")))
    return pkgs
