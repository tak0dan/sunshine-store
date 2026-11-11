"""Windows apps and games for Sunshine Store: install with Wine / Proton, find what is installed, run and remove it.

Menu entries come from three places, all plain .desktop files in ~/.local/share/applications:
  - Wine's own menu builder   (applications/wine/...)
  - the `steam-shortcuts` tool (launchers for Steam and Proton games, marked X-SteamShortcuts-Prefix)
  - launchers made by this store (marked X-SunshineStore-Windows)

  python3 windows.py list | add <exe> [name] [prefix] | remove <desktop-file> [--prefix] | install <exe> [wine|proton] [prefix]
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
APPS = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "applications")
PREFIXES = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "sunshine-store", "prefixes")
ICONS = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "sunshine-store", "icons")
MARK = "X-SunshineStore-Windows"
EXTS = (".exe", ".msi", ".bat", ".lnk")


# ---------------------------------------------------------------- what is available
def has_wine() -> bool:
    return bool(shutil.which("wine"))


def has_proton() -> bool:
    return bool(shutil.which("umu-run"))


def has_steam_shortcuts() -> bool:
    return bool(shutil.which("steam-shortcuts"))


def runners() -> list:
    out = []
    if has_wine():
        out.append("wine")
    if has_proton():
        out.append("proton")
    return out


# ---------------------------------------------------------------- reading entries
def _parse_desktop(path: str) -> dict:
    d, inside = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    inside = line.strip() == "[Desktop Entry]"
                elif inside and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    d.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return d


def _prefix_of(exec_line: str, d: dict) -> str:
    m = re.search(r"WINEPREFIX=(?:\"([^\"]+)\"|(\S+))", exec_line)
    if m:
        return m.group(1) or m.group(2)
    return d.get("X-SteamShortcuts-Prefix", "") or ""


def entries() -> list:
    """Installed Windows apps/games found in the user's menu."""
    out, seen = [], set()
    for path in glob.glob(os.path.join(APPS, "**", "*.desktop"), recursive=True):
        d = _parse_desktop(path)
        if not d or d.get("NoDisplay", "").lower() == "true" or d.get("Hidden", "").lower() == "true" or d.get("Type", "Application") != "Application":
            continue
        ex = d.get("Exec", "")
        rel = os.path.relpath(path, APPS)
        wine_made = rel.startswith("wine" + os.sep) or "WINEPREFIX" in ex or re.search(r"\bwine\b", ex) is not None
        steam = "X-SteamShortcuts-Prefix" in d or "steam://rungameid" in ex
        mine = MARK in d
        if not (wine_made or steam or mine):
            continue
        if rel.startswith(os.path.join("wine", "Programs", "Accessories")) or d.get("Name", "").lower() in ("uninstall", "wine", "notepad"):
            continue
        kind = "steam" if steam and "X-SteamShortcuts-Prefix" not in d else ("proton" if "X-SteamShortcuts-Prefix" in d or "proton" in ex.lower() or "umu-run" in ex else "wine")
        key = (d.get("Name", ""), ex)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": d.get("Name") or os.path.basename(path)[:-8], "exec": ex, "icon": d.get("Icon", ""), "path": path,
                    "kind": kind, "prefix": _prefix_of(ex, d), "comment": d.get("Comment", ""), "mine": mine})
    out.sort(key=lambda e: e["name"].lower())
    return out


# ---------------------------------------------------------------- installing
def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def default_prefix(exe: str) -> str:
    return os.path.join(PREFIXES, slug(os.path.splitext(os.path.basename(exe))[0]))


def install_argv(exe: str, runner: str = "wine", prefix: str = "") -> list:
    """Command that runs a Windows installer in its own prefix (shown to the user and executed in the terminal)."""
    prefix = prefix or default_prefix(exe)
    low = exe.lower()
    if runner == "proton" and has_proton():
        return ["env", f"WINEPREFIX={prefix}", "GAMEID=umu-default", "PROTONPATH=GE-Proton", "umu-run", exe]
    if low.endswith(".msi"):
        return ["env", f"WINEPREFIX={prefix}", "wine", "msiexec", "/i", exe]
    return ["env", f"WINEPREFIX={prefix}", "wine", exe]


def _icon_from_exe(exe: str, out_png: str) -> str:
    """Pull the program icon out of an .exe when icoutils is installed (optional); returns a path or ''."""
    if not (shutil.which("wrestool") and shutil.which("icotool")):
        return ""
    try:
        ico = out_png[:-4] + ".ico"
        with open(ico, "wb") as f:
            subprocess.run(["wrestool", "-x", "-t", "14", exe], stdout=f, stderr=subprocess.DEVNULL, timeout=20, check=True)
        subprocess.run(["icotool", "-x", "-o", os.path.dirname(out_png), ico], capture_output=True, timeout=20)
        pngs = sorted(glob.glob(out_png[:-4] + "*.png"), key=os.path.getsize, reverse=True)
        if pngs:
            if pngs[0] != out_png:
                os.replace(pngs[0], out_png)
            return out_png
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def add_to_menu(exe: str, name: str = "", prefix: str = "", runner: str = "wine") -> str:
    """Create a menu launcher for an already-installed Windows program (an .exe inside a prefix, or anywhere on disk)."""
    name = name or os.path.splitext(os.path.basename(exe))[0].replace("_", " ").title()
    prefix = prefix or default_prefix(exe)
    os.makedirs(APPS, exist_ok=True)
    os.makedirs(ICONS, exist_ok=True)
    icon = _icon_from_exe(exe, os.path.join(ICONS, slug(name) + ".png")) or "wine"
    if runner == "proton" and has_proton():
        ex = f"env WINEPREFIX={shlex.quote(prefix)} GAMEID=umu-default PROTONPATH=GE-Proton umu-run {shlex.quote(exe)}"
    else:
        ex = f"env WINEPREFIX={shlex.quote(prefix)} wine start /unix {shlex.quote(exe)}"
    path = os.path.join(APPS, f"sunshine-win-{slug(name)}.desktop")
    with open(path, "w") as f:
        f.write(f"[Desktop Entry]\nType=Application\nName={name}\nExec={ex}\nIcon={icon}\nCategories=Game;\n"
                f"Comment=Windows program added by Sunshine Store\nTerminal=false\nStartupWMClass={os.path.basename(exe).lower()}\n{MARK}={prefix}\n")
    os.chmod(path, 0o755)
    return path


def run_steam_shortcuts() -> list:
    """argv that creates launchers for Steam/Proton installs (the user's steam-shortcuts tool)."""
    return ["steam-shortcuts"]


def watch_enabled() -> bool:
    return os.path.exists(os.path.join(HOME, ".config/systemd/user")) and bool(glob.glob(os.path.join(HOME, ".config/systemd/user/*steam-shortcuts*")))


# ---------------------------------------------------------------- running / removing
def remove(path: str, delete_prefix: bool = False) -> int:
    d = _parse_desktop(path)
    prefix = d.get(MARK, "") or _prefix_of(d.get("Exec", ""), d)
    try:
        os.remove(path)
        print(f"Removed menu entry {os.path.basename(path)}")
    except OSError as e:
        print(f"Could not remove {path}: {e}", file=sys.stderr)
        return 1
    if delete_prefix and prefix and os.path.abspath(prefix).startswith(os.path.abspath(PREFIXES) + os.sep):
        shutil.rmtree(prefix, ignore_errors=True)
        print(f"Deleted the Wine prefix {prefix}")
    return 0


def uninstaller_argv(prefix: str) -> list:
    return ["env", f"WINEPREFIX={prefix}", "wine", "uninstaller"] if prefix else ["wine", "uninstaller"]


def run_command(entry: dict) -> str:
    """What to type in a terminal to start this app (shown under 'Run')."""
    ex = re.sub(r"\s%[a-zA-Z]", "", entry.get("exec", ""))
    return ex.strip()


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["list"]:
        print(json.dumps(entries(), indent=1))
    elif a[:1] == ["add"] and len(a) >= 2:
        print(add_to_menu(a[1], a[2] if len(a) > 2 else "", a[3] if len(a) > 3 else ""))
    elif a[:1] == ["remove"] and len(a) >= 2:
        sys.exit(remove(a[1], "--prefix" in a))
    elif a[:1] == ["install"] and len(a) >= 2:
        os.execvp("env", install_argv(a[1], a[2] if len(a) > 2 else "wine", a[3] if len(a) > 3 else ""))
    else:
        print(__doc__)
        sys.exit(1)
