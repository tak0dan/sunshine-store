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
import identity
import index
import windows

SOURCES = ("aur", "pacman", "flatpak", "appimage", "snap", "wine")
ORDERS = {
    "aur-first": ("aur", "pacman", "flatpak", "appimage", "snap", "wine"),
    "arch-first": ("pacman", "aur", "flatpak", "appimage", "snap", "wine"),
    "pacman-flatpak": ("pacman", "flatpak", "appimage", "aur", "snap", "wine"),
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
class RelGroup:
    """Software related to the one searched for: "Clients & front-ends for Discord", "Fork of Firefox" ..."""
    title: str
    sub: str
    kind: str
    direction: str                 # "in": others point at the anchor; "out": the anchor points at them
    total: int
    entries: list = field(default_factory=list)      # [(App, confidence, evidence, origin)]


class Results(list):
    """Search results (Apps, best first) plus what the catalogue knows around them."""
    related: list
    generation: int
    stale: list

    def __init__(self, apps=()):
        super().__init__(apps)
        self.related, self.generation, self.stale = [], 0, []


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
    first_seen: int = 0            # when the catalogue first saw it (0 = indexed before tracking started)

    @property
    def is_new(self) -> bool:
        return bool(self.first_seen) and time.time() - self.first_seen < index.NEW_DAYS * 86400

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
    def is_new(self) -> bool:
        return any(p.is_new for p in self.items.values())

    @property
    def label(self) -> str:
        """The software's own name ("Discord"), not the name of whichever package happens to be preferred."""
        for p in self.items.values():
            if p.title:
                return p.title
        return self.pick().label

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


SOURCE_LABEL = {"aur": "AUR", "pacman": "Pacman", "flatpak": "Flathub", "appimage": "AppImage", "snap": "Snap"}


def _age(hours) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{int(hours * 60)} minutes ago"
    return f"{hours:.1f} hours ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def catalog_summary() -> str:
    s = index.status()
    if not s["built"]:
        return "Catalog not built yet."
    ago = "never"
    if s["last_refresh"]:
        ago = _age((time.time() - s["last_refresh"]) / 3600)
    run = "  ·  refreshing in the background…" if s.get("running") else ""
    return f"{s['packages']:,} packages · catalogue generation {s['generation']} · updated {ago} · {s['active_hours']} h of active use since{run}"


def source_lines() -> list:
    """[(state, text)] per source for Settings: available / stale / unavailable with the age of its data."""
    out = []
    for x in index.source_status():
        name = SOURCE_LABEL.get(x["source"], x["source"])
        if x["state"] == "unavailable":
            out.append((x["state"], f"{name}: not indexed yet"))
        else:
            tail = f" — {x['error']}" if x["error"] else ""
            out.append((x["state"], f"{name}: {x['count']:,} records, checked {_age(x['age_hours'])}" + (f" (stale{tail}; still searchable)" if x["state"] == "stale" else "")))
    return out


def stale_notes() -> list:
    return [f"{SOURCE_LABEL.get(x['source'], x['source'])} results are from {_age(x['age_hours'])}" for x in index.source_status() if x["state"] == "stale"]


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


def windows_pkgs(fresh: bool = False) -> list:
    """Installed Windows apps/games (Wine, Proton, Steam launchers) as Pkg objects, cached briefly."""
    t, v = _cache.get("win", (0, []))
    if fresh or time.time() - t > 15:
        v = []
        for e in windows.entries():
            kind = {"wine": "Wine", "proton": "Proton", "steam": "Steam"}[e["kind"]]
            v.append(Pkg(name=e["name"], source="wine", title=e["name"], desc=e["comment"] or f"Windows app, runs with {kind}", installed=True,
                         app_id=e["path"], repo=kind, icon=e["icon"], version="", cats="Wine"))
        _cache["win"] = (time.time(), v)
    return v


def refresh_installed():
    installed_pacman(True)
    installed_flatpak(True)
    windows_pkgs(True)


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
               maintainer=row["maintainer"], out_of_date=bool(row["ood"]), icon=row["icon"], cats=row["cats"],
               first_seen=row["first_seen"] if "first_seen" in row.keys() else 0)


def rows_to_apps(rows, source: str | None = None, limit: int = 150) -> list:
    hp, hf = installed_pacman(), installed_flatpak()
    groups: dict = {}
    order: list = []
    for r in rows:
        if source and r["source"] != source:
            continue
        p = _pkg(r, hp, hf)
        key = r["ident"] if "ident" in r.keys() else r["key"]          # one card per software identity, not per package name
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


HEAD_SIZE = 5     # results shown above the relationship groups; exactly these are not repeated inside them
REL_IN = {"client_for": "Clients & front-ends for", "integration_for": "Plugins & integrations for", "fork_of": "Forks & derivatives of"}
REL_OUT = {"client_for": "A client for", "integration_for": "Extends", "fork_of": "A fork of"}


def related_groups(anchors: list, exclude=()) -> list:
    """Typed relationships around software identities as RelGroups. anchors: [(ident, label)]. Identities in `exclude`
    (already in the main results) are left out so the same software never appears twice."""
    label = dict(anchors)
    groups = index.relations_for([i for i, _ in anchors], per_group=10)
    if not groups:
        return []
    others = list(dict.fromkeys(it["other"] for g in groups for it in g["items"] if it["other"] not in exclude))
    apps = {a.key: a for a in rows_to_apps(index.artifacts_of(others, per=6), limit=1000)}
    out = []
    for g in groups:
        entries = [(apps[it["other"]], it["confidence"], it["evidence"], it["origin"]) for it in g["items"] if it["other"] in apps]
        if not entries:
            continue
        who = label.get(g["anchor"], g["anchor"])
        if g["dir"] == "in":
            out.append(RelGroup(f"{REL_IN[g['kind']]} {who}", f"{g['total']} found through {who}", g["kind"], "in", g["total"], entries))
        else:
            out.append(RelGroup(f"{who}: {REL_OUT[g['kind']].lower()}", f"{who} is related to this software", g["kind"], "out", g["total"], entries))
    return out


def search_all(query: str, source: str | None = None) -> Results:
    """Search the local catalogue only (no network, so it is instant even offline). Snap Store results are merged in later by
    snap_extra(), and the result carries relationship groups for the software that matched."""
    if not index.exists():
        raise NoCatalog()
    res = Results(rows_to_apps(index.search(query), source))
    ql = query.lower()
    local = [p for p in windows_pkgs() if ql in p.name.lower()]
    if source in (None, "wine") and local:
        by_key = {a.key: a for a in res}
        front = []
        for p in local:
            k = index.normalize(p.name)
            a = by_key.get(k)
            if a is None:
                a = App(k)
                front.append(a)
            a.items["wine"] = p
        res = Results(front + list(res))
    res.generation = index.generation()
    res.stale = stale_notes()
    # anchors: the software the query names (exact identity / alias), else the best match
    want = identity.slug(query)
    anchors = [(a.key, a.label) for a in res if a.key == want and a.items.keys() - {"wine"}] or \
              [(a.key, a.label) for a in res[:1] if a.items.keys() - {"wine"}]
    try:                                               # the head of the list is what the user sees at once: don't repeat it below
        res.related = related_groups(anchors[:2], exclude={a.key for a in res[:HEAD_SIZE]})
    except Exception:                                  # relationships are an extra; never lose the search results over them
        res.related = []
    return res


def snap_extra(query: str, have_keys: list) -> list:
    """Live Snap Store results for the query, as [(key, Pkg)]; called after the local results are already on screen."""
    out = []
    for p in snap_search(query):
        out.append((index.normalize(p.name), p))
    return out


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
        a = App(r["ident"])
        a.items[r["source"]] = _pkg(r, hp, hf)
        # also offer the same app from other sources when the catalog has them
        for r2 in index.search(r["key"], 60):
            if r2["ident"] == r["ident"] and r2["source"] not in a.items and not (r2["source"] == "aur" and r2["name"].endswith(VARIANT_SUFFIXES)):
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
                if r["ident"] not in seen and len(apps) < per:
                    a = App(r["ident"])
                    a.items["pacman"] = _pkg(r, hp, hf)
                    apps.append(a)
                    seen.add(r["ident"])
            return f"{emoji}  {label}", apps[:per]
    return iid, []


def trending_aur(n: int = 12) -> list:
    if not index.exists():
        return []
    return rows_to_apps(index.trending("aur", n * 3), "aur", n)


def related_software(ident: str, label: str) -> list:
    """Relationship groups for one product page (clients, plugins, forks of it; what it is a client or fork of)."""
    if not index.exists():
        return []
    return related_groups([(ident, label)])


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
def run_command_for(p: Pkg) -> str:
    """What to type in a terminal to start the app once installed."""
    if p.source == "flatpak":
        return f"flatpak run {p.app_id}"
    if p.source == "snap":
        return f"snap run {p.name}"
    if p.source == "appimage":
        e = appimage.load().get(p.app_id)
        return f'"{e["path"]}"' if e else f"~/Applications/{p.label.replace(' ', '')}*.AppImage"
    if p.source == "wine":
        for e in windows.entries():
            if e["path"] == p.app_id:
                return windows.run_command(e)
        return "wine start /unix <program.exe>"
    if p.installed:
        for line in _run(["pacman", "-Ql", p.name]).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("/usr/bin/") and not parts[1].endswith("/"):
                return os.path.basename(parts[1])
    return p.name


_APPIMAGE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appimage.py")


def command_for(p: Pkg, remove: bool = False) -> str:
    if p.source == "wine":
        return f'sunshine-store-windows remove "{p.app_id}"' if remove else run_command_for(p)
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
    if p.source == "wine":
        import sys
        return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "windows.py"), "remove", p.app_id]
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


# ---------------------------------------------------------------- updates
UPDATE_SCRIPT = r"""
echo "== System packages (pacman) =="
pkexec pacman -Syu --noconfirm || echo "The system update did not finish."
if command -v yay >/dev/null 2>&1; then echo; echo "== AUR packages (yay) =="; yay -Sua --noconfirm --sudo pkexec; fi
if command -v flatpak >/dev/null 2>&1; then echo; echo "== Flatpak apps =="; flatpak update -y --noninteractive; fi
echo
echo "All updates finished."
"""


def full_update_argv() -> list:
    return ["bash", "-c", UPDATE_SCRIPT]


def list_updates() -> dict:
    """What can be updated right now: {'pacman': [...], 'aur': [...], 'flatpak': [...]} (lines of 'name old -> new')."""
    out = {"pacman": [], "aur": [], "flatpak": []}
    if shutil.which("checkupdates"):
        out["pacman"] = [l for l in _run(["checkupdates"], 90).splitlines() if l.strip()]
    else:
        out["pacman"] = [l for l in _run(["pacman", "-Qu"], 30).splitlines() if l.strip()]
    if shutil.which("yay"):
        out["aur"] = [l for l in _run(["yay", "-Qua"], 90).splitlines() if l.strip()]
    if shutil.which("flatpak"):
        out["flatpak"] = [l.replace("\t", "  ") for l in _run(["flatpak", "remote-ls", "--updates", "--columns=application,version"], 90).splitlines() if l.strip()]
    return out


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
    for p in windows_pkgs(True):
        pkgs.append(p)
    for repo, e in appimage.load().items():
        pkgs.append(Pkg(name=e.get("name", repo), title=e.get("name", repo), source="appimage", app_id=repo, repo="appimage.github.io",
                        version=e.get("version", ""), installed=True, icon=e.get("icon", "")))
    return pkgs
