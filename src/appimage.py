"""AppImage install/remove for Sunshine Store. Everything stays in the user's home directory.

  python3 appimage.py install <owner/repo> [Name] [icon-url]
  python3 appimage.py remove  <owner/repo>
  python3 appimage.py list
Installs the newest x86_64 .AppImage release asset into ~/Applications and adds a menu entry.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import urllib.request

HOME = os.path.expanduser("~")
APPS = os.path.join(HOME, "Applications")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")))
MANIFEST = os.path.join(DATA, "sunshine-store", "appimages.json")
MENU = os.path.join(DATA, "applications")
ICONS = os.path.join(DATA, "sunshine-store", "icons")


def _get(url: str, binary=False):
    headers = {"User-Agent": "sunshine-store", "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return r.read() if binary else json.load(r)


def load() -> dict:
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(m: dict) -> None:
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(m, f, indent=1)


def installed_repos() -> set:
    return {r for r, v in load().items() if os.path.exists(v.get("path", ""))}


def pick_asset(assets: list):
    cands = []
    for a in assets:
        n = a["name"].lower()
        if not n.endswith(".appimage") or any(x in n for x in ("arm", "aarch64", "i386", "i686", "armv7", "zsync")):
            continue
        score = (2 if re.search(r"x86[_-]?64|amd64", n) else 1)
        cands.append((score, a))
    return max(cands, key=lambda c: c[0])[1] if cands else None


def install(repo: str, name: str = "", icon_url: str = "") -> int:
    name = name or repo.split("/")[-1]
    print(f"Looking up the latest release of {repo}…", flush=True)
    rel = _get(f"https://api.github.com/repos/{repo}/releases/latest")
    asset = pick_asset(rel.get("assets", []))
    if not asset:
        print("No x86_64 AppImage found in the latest release.", file=sys.stderr)
        return 2
    os.makedirs(APPS, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    path = os.path.join(APPS, asset["name"])
    print(f"Downloading {asset['name']} ({asset['size'] // 1048576} MB)…", flush=True)
    data = _get(asset["browser_download_url"], binary=True)
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    icon = "application-x-executable"
    if icon_url:
        try:
            os.makedirs(ICONS, exist_ok=True)
            ipath = os.path.join(ICONS, slug + ".png")
            with open(ipath, "wb") as f:
                f.write(_get(icon_url, binary=True))
            icon = ipath
        except Exception:
            pass
    os.makedirs(MENU, exist_ok=True)
    desktop = os.path.join(MENU, f"appimage-{slug}.desktop")
    with open(desktop, "w") as f:
        f.write(f"[Desktop Entry]\nType=Application\nName={name}\nExec=\"{path}\" --no-sandbox %U\nIcon={icon}\n"
                f"Categories=Utility;\nComment=Installed by Sunshine Store (AppImage)\nTerminal=false\n")
    m = load()
    m[repo] = {"name": name, "path": path, "desktop": desktop, "icon": icon, "version": rel.get("tag_name", "")}
    _save(m)
    print(f"Installed {name} {rel.get('tag_name', '')} to {path}", flush=True)
    return 0


def remove(repo: str) -> int:
    m = load()
    e = m.pop(repo, None)
    if not e:
        print("Not installed through Sunshine Store.", file=sys.stderr)
        return 1
    for p in (e.get("path"), e.get("desktop")):
        try:
            if p:
                os.remove(p)
        except OSError:
            pass
    _save(m)
    print(f"Removed {e.get('name', repo)}.")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["install"] and len(a) >= 2:
        sys.exit(install(a[1], a[2] if len(a) > 2 else "", a[3] if len(a) > 3 else ""))
    if a[:1] == ["remove"] and len(a) >= 2:
        sys.exit(remove(a[1]))
    if a[:1] == ["list"]:
        print(json.dumps(load(), indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
