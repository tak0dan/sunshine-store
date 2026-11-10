"""Sunshine Store backend: pacman, AUR (RPC) and Flatpak behind one small API."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# Hierarchy requested for Sunshine OS: AUR -> Pacman -> Flatpak.
DEFAULT_ORDER = ("aur", "pacman", "flatpak")
VARIANT_SUFFIXES = ("-bin", "-git", "-appimage", "-nightly", "-beta", "-dev", "-stable", "-electron", "-wayland")
AUR_RPC = "https://aur.archlinux.org/rpc/v5"


@dataclass
class Pkg:
    name: str
    source: str                    # "aur" | "pacman" | "flatpak"
    version: str = ""
    desc: str = ""
    installed: bool = False
    votes: int = 0
    url: str = ""
    repo: str = ""                 # pacman repo or flatpak remote
    app_id: str = ""               # flatpak application id
    maintainer: str = ""
    out_of_date: bool = False
    icon: str = ""                # theme icon name or absolute path (installed apps)

    @property
    def ident(self) -> str:
        return self.app_id or self.name

    @property
    def is_variant(self) -> bool:
        return self.name.endswith(VARIANT_SUFFIXES)


@dataclass
class App:
    """One logical application, possibly offered by several sources."""
    key: str
    items: dict = field(default_factory=dict)   # source -> Pkg (best candidate)
    variants: list = field(default_factory=list)  # other AUR packages: -bin, -git, ...

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
        return [s for s in DEFAULT_ORDER if s in self.items]


def _run(cmd, timeout=45) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/local/bin"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize(name: str) -> str:
    n = name.lower()
    if "." in n and n.count(".") >= 2:        # flatpak id: org.mozilla.firefox -> firefox
        n = n.rsplit(".", 1)[-1]
    for suf in VARIANT_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


# ---------------------------------------------------------------- installed state
def installed_pacman() -> set:
    return set(_run(["pacman", "-Qq"]).split())


def installed_flatpak() -> set:
    if not shutil.which("flatpak"):
        return set()
    return set(_run(["flatpak", "list", "--app", "--columns=application"]).split())


# ---------------------------------------------------------------- search
def search_pacman(query: str, have: set) -> list:
    out = _run(["pacman", "-Ss", "--", query])
    pkgs, cur = [], None
    for line in out.splitlines():
        if line and not line.startswith(" "):
            m = re.match(r"^(\S+?)/(\S+)\s+(\S+)", line)
            if m:
                cur = Pkg(name=m.group(2), source="pacman", repo=m.group(1), version=m.group(3),
                          installed=m.group(2) in have)
                pkgs.append(cur)
        elif cur is not None:
            cur.desc = line.strip()
    return pkgs


def _aur_get(path: str):
    req = urllib.request.Request(AUR_RPC + path, headers={"User-Agent": "sunshine-store"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r).get("results", [])


def _aur_pkg(r: dict, have: set) -> Pkg:
    return Pkg(name=r["Name"], source="aur", version=r.get("Version", ""), desc=r.get("Description") or "",
               installed=r["Name"] in have, votes=r.get("NumVotes", 0), url=r.get("URL") or "",
               maintainer=r.get("Maintainer") or "", out_of_date=bool(r.get("OutOfDate")))


def search_aur(query: str, have: set) -> list:
    try:
        res = _aur_get("/search/" + urllib.parse.quote(query) + "?by=name-desc")
    except Exception:
        return []
    res.sort(key=lambda r: (-r.get("NumVotes", 0), r["Name"]))
    return [_aur_pkg(r, have) for r in res[:60]]


def search_flatpak(query: str, have: set) -> list:
    if not shutil.which("flatpak"):
        return []
    out = _run(["flatpak", "search", "--columns=application,name,description,version", query], 40)
    pkgs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "." in parts[0]:
            pkgs.append(Pkg(name=parts[1].strip(), source="flatpak", app_id=parts[0].strip(), repo="flathub",
                            desc=parts[2].strip() if len(parts) > 2 else "",
                            version=parts[3].strip() if len(parts) > 3 else "",
                            installed=parts[0].strip() in have))
    return pkgs


NOISE = re.compile(r"(i18n|l10n|langpack|-lang-|-locale|-debug$|-docs?$|-doc-)")


def search_all(query: str) -> list:
    """Search every source and merge into App objects, best matches first."""
    hp, hf = installed_pacman(), installed_flatpak()
    groups: dict = {}
    for source_pkgs in (search_pacman(query, hp), search_aur(query, hp), search_flatpak(query, hf)):
        for p in source_pkgs:
            if NOISE.search(p.name) and not NOISE.search(query):
                continue
            key = normalize(p.ident if p.source != "flatpak" else p.name)
            app = groups.setdefault(key, App(key))
            if p.source == "aur" and p.is_variant:
                app.variants.append(p)
                continue
            cur = app.items.get(p.source)
            if cur is None or (p.source == "aur" and p.votes > cur.votes):
                app.items[p.source] = p
    # an App with only AUR variants: promote the most-voted variant so it still shows
    for app in groups.values():
        if not app.items and app.variants:
            best = max(app.variants, key=lambda v: v.votes)
            app.items["aur"] = best
            app.variants.remove(best)
    q = query.lower()
    def rank(a: App):
        exact = a.key == q
        return (not exact, not a.key.startswith(q), -max((p.votes for p in a.items.values()), default=0), a.key)
    return sorted(groups.values(), key=rank)


def related_aur(base: str) -> list:
    """AUR variants of an app: base, base-bin, base-git, base-beta-bin ... (for the 'related' tabs)."""
    hp = installed_pacman()
    try:
        res = _aur_get("/search/" + urllib.parse.quote(base) + "?by=name")
    except Exception:
        return []
    suf = "|".join(re.escape(x.lstrip("-")) for x in VARIANT_SUFFIXES + ("-esr", "-stable", "-lts", "-qt", "-gtk"))
    pat = re.compile(rf"^{re.escape(base)}(-({suf}))*$")
    out = [_aur_pkg(r, hp) for r in res if pat.match(r["Name"])]
    out.sort(key=lambda p: (p.name != base, p.name.endswith(("-git",)), p.name.endswith("-bin"), -p.votes, p.name))
    return out[:40]


# ---------------------------------------------------------------- actions
def command_for(p: Pkg, remove: bool = False) -> str:
    if p.source == "pacman":
        return f"sudo pacman -{'Rns' if remove else 'S'} {p.name}"
    if p.source == "aur":
        return f"yay -{'Rns' if remove else 'S'} {p.name}"
    return f"flatpak {'uninstall' if remove else 'install flathub'} {p.app_id}"


def argv_for(p: Pkg, remove: bool = False) -> list:
    """Non-interactive argv the GUI runs (polkit prompts for privileges)."""
    if p.source == "pacman":
        return ["pkexec", "pacman", "-Rns" if remove else "-S", "--noconfirm", *([] if remove else ["--needed"]), p.name]
    if p.source == "aur":
        base = ["yay", "--noconfirm", "--sudo", "pkexec"]
        return base + (["-Rns", p.name] if remove else ["-S", "--needed", p.name])
    if remove:
        return ["flatpak", "uninstall", "-y", "--noninteractive", p.app_id]
    return ["flatpak", "install", "-y", "--noninteractive", "flathub", p.app_id]


def _desktop_entries() -> dict:
    """{desktop-file path: (Icon, NoDisplay)} for installed applications."""
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
    """{name: (version, description)} for every installed package, in one pacman call."""
    infos = {}
    for block in _run(["pacman", "-Qi"], 60).split("\n\n"):
        f = dict(re.findall(r"^(Name|Version|Description)\s*:\s*(.*)$", block, re.M))
        if "Name" in f:
            infos[f["Name"]] = (f.get("Version", ""), f.get("Description", ""))
    return infos


def list_installed(apps_only: bool = True) -> list:
    """Installed pacman/AUR packages and Flatpak apps. apps_only hides libraries and system tools
    (keeps explicitly installed packages that ship a launcher)."""
    infos = _pacman_infos()
    foreign = set(_run(["pacman", "-Qmq"]).split())
    explicit = set(_run(["pacman", "-Qeq"]).split())
    icons = {}                                    # package -> icon of its launcher
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
        pkgs.append(Pkg(name=n, source="aur" if n in foreign else "pacman", installed=True,
                        version=ver, desc=desc, icon=icons.get(n, "")))
    for fid in sorted(installed_flatpak()):
        pkgs.append(Pkg(name=fid.rsplit(".", 1)[-1], source="flatpak", app_id=fid, repo="flathub",
                        installed=True, icon=fid))
    return pkgs


# Starter shelves for the Home page: (title, [names]). Names resolve by normal search.
FEATURED = [
    ("Browse the web", ["firefox", "brave-bin", "chromium"]),
    ("Chat & calls", ["telegram-desktop", "discord", "signal-desktop", "thunderbird"]),
    ("Watch & listen", ["vlc", "spotify", "mpv", "obs-studio"]),
    ("Work & study", ["libreoffice-fresh", "obsidian", "okular", "zotero"]),
    ("Create", ["gimp", "krita", "inkscape", "blender", "kdenlive"]),
    ("Games", ["steam", "lutris", "heroic-games-launcher-bin", "prismlauncher"]),
]


def resolve(names: list) -> list:
    """Turn curated package names into Apps (pacman repos first, then AUR info). Used by Home shelves."""
    hp = installed_pacman()
    apps = {}
    out = _run(["pacman", "-Si", *names], 30)
    for block in out.split("\n\n"):
        f = dict(re.findall(r"^(Repository|Name|Version|Description)\s*:\s*(.*)$", block, re.M))
        if "Name" in f:
            a = App(f["Name"])
            a.items["pacman"] = Pkg(name=f["Name"], source="pacman", repo=f.get("Repository", ""), version=f.get("Version", ""),
                                    desc=f.get("Description", ""), installed=f["Name"] in hp)
            apps[f["Name"]] = a
    missing = [n for n in names if n not in apps]
    if missing:
        try:
            res = _aur_get("/info?" + "&".join("arg[]=" + urllib.parse.quote(n) for n in missing))
        except Exception:
            res = []
        for r in res:
            a = App(r["Name"])
            a.items["aur"] = _aur_pkg(r, hp)
            apps[r["Name"]] = a
    return [apps[n] for n in names if n in apps]
