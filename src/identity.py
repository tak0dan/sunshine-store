"""Software identities and typed relationships.

A *package artifact* (pacman/discord, aur/discord-bin, flatpak/com.discordapp.Discord ...) is one way to get a piece of
software. A *software identity* is the software itself ("discord"). Artifacts are attached to an identity; identities are
connected to each other by typed relationships and are never merged into one another:

    client_for       a client / front-end / launcher for another program        (legcord  -> discord)
    integration_for  plugin, extension, theme, overlay, bridge, bot, RPC ...    (discord-rpc -> discord)
    fork_of          a fork or derivative                                       (vscodium -> visual-studio-code)

Every relationship carries its evidence, an origin (curated / description / name / github) and a confidence; only
relationships at or above MIN_CONFIDENCE are kept. Resolution is a pure function of the catalogue rows (plus optional
cached GitHub enrichment and curated data), so it is deterministic and unit-testable without a database.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

# Variant suffixes: the same software built differently. `discord-canary` is a distribution of Discord.
SUFFIXES = ("-bin", "-git", "-appimage", "-nightly", "-beta", "-dev", "-stable", "-electron", "-wayland",
            "-canary", "-ptb", "-preview", "-insiders", "-devel", "-unstable", "-latest", "-lts", "-snapshot",
            "-community", "-flatpak")

MIN_CONFIDENCE = 0.6
RECENT_DAYS = 5        # artifacts first seen this recently are "new": boosted in relationship ordering and badged in the UI
RULES_VERSION = 4      # bump with any change to the rules below: the identity graph is rebuilt once on the next refresh
KINDS = ("client_for", "integration_for", "fork_of")
# Group titles when the anchor is the *target* (incoming) or the *source* (outgoing) of the relationship.
LABEL_IN = {"client_for": "Clients & front-ends", "integration_for": "Plugins & integrations", "fork_of": "Forks & derivatives"}
LABEL_OUT = {"client_for": "Is a client for", "integration_for": "Extends", "fork_of": "Is a fork of"}

GENERIC = frozenset("""linux gnome kde desktop terminal web mail email music video audio file files text image images chat network
system unix cli gui tui qt gtk x11 wayland irc rss api bot game games editor browser player server client library tool tools
app apps application manager viewer reader plugin plugins theme themes extension extensions addon addons package script open
source free simple small fast lightweight modern cross platform windows mac macos android ios python rust go java node electron
flutter git github gitlab the and for with that this your all any ncurses curses tdlib sdk framework toolkit""".split())
STOP_LEAD = frozenset("the a an your my our all any this that these those using unofficial official".split())
BOUNDARY = frozenset("to for with that which using via by from in on of written made built based designed implemented".split())
ALLOW_NEXT = frozenset("with for written made built in that which using based designed to implemented featuring".split())

CLIENT_NOUNS = {"client", "clients", "frontend", "front-end", "gui", "tui", "launcher"}
INTEG_NOUNS = {"plugin", "plugins", "extension", "extensions", "addon", "addons", "add-on", "add-ons", "theme", "themes",
               "overlay", "bridge", "integration", "mod", "mods", "rpc", "bot", "wrapper"}
NAME_SUFFIX = {"rpc": ("integration_for", 0.6), "overlay": ("integration_for", 0.6), "plugin": ("integration_for", 0.6),
               "plugins": ("integration_for", 0.6), "extension": ("integration_for", 0.6), "extensions": ("integration_for", 0.6),
               "addon": ("integration_for", 0.6), "addons": ("integration_for", 0.6), "theme": ("integration_for", 0.6),
               "themes": ("integration_for", 0.6), "bridge": ("integration_for", 0.6), "bot": ("integration_for", 0.6),
               "integration": ("integration_for", 0.6), "presence": ("integration_for", 0.6),
               "client": ("client_for", 0.6), "tui": ("client_for", 0.6), "gui": ("client_for", 0.6), "frontend": ("client_for", 0.6),
               "cli": ("client_for", 0.45), "qt": ("client_for", 0.45), "gtk": ("client_for", 0.45)}

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*")
_HINT = re.compile(r"client|front-?end|\bgui\b|\btui\b|launcher|plugin|extension|add-?on|theme|overlay|bridge|integration|"
                   r"\bmods?\b|\bbot\b|\brpc\b|wrapper|\bfork", re.I)
_FOR = re.compile(r"\b(?P<n>client|frontend|front-end|gui|tui|launcher|wrapper|bridge|overlay|plugins?|extensions?|add-?ons?|"
                  r"themes?|mods?|integration|bot|rpc)\s+(?:for|of|to)\s+(?P<rest>(?:[^.;:()]|\.(?=[A-Za-z0-9])){1,70})", re.I)
_FORK_OF = re.compile(r"\b(?:fork of|forked from|fork for)\s+(?P<rest>(?:[^.;:()]|\.(?=[A-Za-z0-9])){1,70})", re.I)
_REPO = re.compile(r"https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)/([^/\s#?]+)/([^/\s#?]+)", re.I)


_NONALNUM = re.compile(r"[^a-z0-9]+")
_NAME = re.compile(r"^(?P<x>.+?)[-_](?P<s>[a-z]+)$")
_NOUN = re.compile(r"\b(client|frontend|front-end|gui|tui|fork)\b", re.I)
NOUN_KIND = {"client": "client_for", "frontend": "client_for", "front-end": "client_for", "gui": "client_for", "tui": "client_for",
             "fork": "fork_of"}


def slug(s: str) -> str:
    return _NONALNUM.sub("-", s.lower()).strip("-")


def normalize(name: str) -> str:
    n = name.lower()
    if n.count(".") >= 2:                      # flatpak id org.mozilla.firefox -> firefox
        n = n.rsplit(".", 1)[-1]
    while n.endswith(SUFFIXES):
        for s in SUFFIXES:
            if n.endswith(s):
                n = n[: -len(s)]
                break
    return n


def repo_of(url: str) -> str:
    if not url or ("github" not in url and "gitlab" not in url and "codeberg" not in url):
        return ""
    m = _REPO.match(url)
    if not m:
        return ""
    r = m.group(3)
    r = r[:-4] if r.endswith(".git") else r
    return f"{m.group(1)}/{m.group(2)}/{r}".lower()


# ------------------------------------------------------------------------------------------------ curated data
def _data_files() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "sunshine-store", "identities.json")
    shipped = [p for p in (os.path.join(here, "identities.json"), os.path.join(here, "..", "data", "identities.json")) if os.path.exists(p)]
    return shipped[:1] + ([cfg] if os.path.exists(cfg) else [])


def load_curated() -> dict:
    """Shipped data plus the user's own ~/.config/sunshine-store/identities.json (manual corrections: aliases, relations, deny)."""
    out = {"aliases": {}, "relations": [], "deny": []}
    for path in _data_files():
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        for ident, al in (d.get("aliases") or {}).items():
            out["aliases"].setdefault(ident, [])
            out["aliases"][ident] += [a for a in al if a not in out["aliases"][ident]]
        out["relations"] += d.get("relations") or []
        out["deny"] += d.get("deny") or []
    return out


def curated_version() -> str:
    """Changes when curated data changes, so the identity graph is rebuilt even if no package source changed."""
    import hashlib
    h = hashlib.sha1()
    for p in _data_files():
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


# ------------------------------------------------------------------------------------------------ resolution
@dataclass
class Resolution:
    ident_of: dict = field(default_factory=dict)      # pkg id -> identity slug
    identities: dict = field(default_factory=dict)    # identity -> {"name": str, "count": int, "sources": [..]}
    aliases: dict = field(default_factory=dict)       # alias slug -> identity
    relations: list = field(default_factory=list)     # (src, dst, kind, confidence, evidence, origin, weight)


def _get(row, k, default=""):
    try:
        v = row[k]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _targets(tokens: list, notable: dict, aliases: dict, limit: int = 1, anchored: bool = False) -> list:
    """Notable identities named in a run of words (bigrams first); generic words never count. `anchored`: the target has to
    start the run ("fork of Discord ...", not "fork of discord.py, a wrapper for Discord")."""
    found = []
    i = 0
    while i < len(tokens) and len(found) < limit:
        if anchored and i > 0:
            break
        hit = None
        for span in (2, 1):
            if i + span > len(tokens):
                continue
            cand = slug(" ".join(tokens[i:i + span]))
            if span == 1 and (cand in GENERIC or len(cand) < 3):
                continue
            cand = aliases.get(cand, cand)
            if cand in notable:
                hit = (cand, span)
                break
        if hit:
            if hit[0] not in found:
                found.append(hit[0])
            i += hit[1]
        else:
            i += 1
    return found


def _head_window(text: str, noun_pos: int) -> list | None:
    """Words directly before a head noun ("terminal-native Discord and Slack client"), or None when the noun is not the
    head of the description ("tool to customize Spotify client": the client is the object, not the thing described)."""
    words = [(m.group(0), m.start()) for m in _TOKEN.finditer(text[:noun_pos])]
    window = []
    for w, _ in reversed(words):
        if w.lower() in BOUNDARY:
            return None
        window.append(w)
        if len(window) >= 6:
            break
    return list(reversed(window))


def resolve(rows, enrichment: dict | None = None, curated: dict | None = None) -> Resolution:
    """rows: objects indexable by id, source, name, name_l, title, app_id, desc, url, votes, is_app."""
    rows = list(rows)
    curated = curated if curated is not None else load_curated()
    enrichment = enrichment or {}
    res = Resolution()
    now = time.time()

    # ---- curated aliases: any of these names/ids is the same software
    alias_to_ident = {}
    for ident, al in curated["aliases"].items():
        for a in al:
            alias_to_ident[slug(a)] = ident

    # ---- pass 1: package-style artifacts (name-based identity)
    known = set()
    for r in rows:
        if r["source"] == "flatpak":
            continue
        base = normalize(r["name"])
        ident = alias_to_ident.get(slug(base)) or alias_to_ident.get(slug(r["name"])) or base
        res.ident_of[r["id"]] = ident
        if r["source"] in ("pacman", "aur"):
            known.add(ident)

    # ---- pass 2: flatpak artifacts: prefer an identity that already exists (app-id tail, then title)
    generic_tail = {"client", "desktop", "app", "studio", "gui", "launcher", "stable", "official", "linux"}
    for r in rows:
        if r["source"] != "flatpak":
            continue
        tail = slug(_get(r, "app_id").rsplit(".", 1)[-1]) or slug(r["name"])
        title = slug(_get(r, "title"))
        cands = [tail, title, re.sub(r"-(community|desktop|app)$", "", tail), re.sub(r"-(community|desktop|app)$", "", title)]
        cands = [alias_to_ident.get(c, c) for c in cands if c]
        pick = next((c for c in cands if c in known), None)
        if pick is None:
            pick = tail if tail and tail not in generic_tail else (title or tail)
        res.ident_of[r["id"]] = pick

    # ---- identity records + notability (a notable identity may be the *target* of a relationship)
    notable: dict = {}
    weight: dict = {}          # identity -> ordering weight (official packages and votes rank higher)
    repo_ident: dict = {}
    best_title: dict = {}
    for r in rows:
        ident = res.ident_of[r["id"]]
        rec = res.identities.setdefault(ident, {"name": "", "count": 0, "sources": []})
        rec["count"] += 1
        if r["source"] not in rec["sources"]:
            rec["sources"].append(r["source"])
        title = _get(r, "title")
        if title and (ident not in best_title or r["source"] in ("flatpak", "pacman")):
            best_title[ident] = title
        src = r["source"]
        strength = 2 if (src in ("pacman", "flatpak", "appimage") and _get(r, "is_app", 0)) else \
            1 if src in ("pacman", "flatpak", "appimage") else (1 if (_get(r, "votes", 0) or 0) >= 30 else 0)
        if strength > notable.get(ident, -1):
            notable[ident] = strength
        w = strength * 100 + min(int(_get(r, "votes", 0) or 0), 99)
        fs = _get(r, "first_seen", 0) or 0
        if fs and now - fs < RECENT_DAYS * 86400:
            w += 500                                       # a fresh discovery is shown before long-established look-alikes
        if w > weight.get(ident, -1):
            weight[ident] = w
        rp = repo_of(_get(r, "url"))
        if rp and strength >= repo_ident.get(rp, ("", -1))[1]:
            repo_ident[rp] = (ident, strength)
    notable = {k: v for k, v in notable.items() if v >= 1 and len(k) >= 3 and k not in GENERIC and not k.isdigit()}
    for ident, rec in res.identities.items():
        rec["name"] = best_title.get(ident) or ident
    # alias table: curated aliases + readable titles ("Visual Studio Code" -> visual-studio-code)
    res.aliases = {slug(a): ident for ident, al in curated["aliases"].items() for a in al}
    for ident, title in best_title.items():
        t = slug(title)
        if t and t != ident and t not in res.aliases and res.identities.get(ident, {}).get("count", 0) >= 1:
            res.aliases[t] = ident

    # ---- relationships
    ev: dict = {}          # (src, dst, kind) -> {"conf": float, "evidence": [str], "origin": set}

    def add(src, dst, kind, conf, text, origin):
        if not src or not dst or src == dst or kind not in KINDS:
            return
        e = ev.setdefault((src, dst, kind), {"conf": 0.0, "evidence": [], "origin": set()})
        e["conf"] = max(e["conf"], conf) + (0.1 if origin not in e["origin"] and e["origin"] else 0.0)
        e["conf"] = min(e["conf"], 0.95) if origin != "curated" else 1.0
        if text not in e["evidence"] and len(e["evidence"]) < 3:
            e["evidence"].append(text)
        e["origin"].add(origin)

    for r in rows:
        src = res.ident_of[r["id"]]
        desc = _get(r, "desc")
        name = r["name_l"] if _get(r, "name_l") else r["name"].lower()
        # (1) description evidence
        if desc and _HINT.search(desc):
            m = _FOR.search(desc)
            if m:
                noun = m.group("n").lower().replace("add-on", "addon")
                kind = "client_for" if noun in CLIENT_NOUNS else "integration_for"
                toks = [t for t in _TOKEN.findall(m.group("rest")) if t.lower() not in STOP_LEAD]
                for t in _targets(toks[:3], notable, res.aliases, 1, anchored=True):
                    add(src, t, kind, 0.75, f"“{desc[:90].strip()}”", "description")
            m = _FORK_OF.search(desc)
            if m:
                toks = [t for t in _TOKEN.findall(m.group("rest")) if t.lower() not in STOP_LEAD]
                for t in _targets(toks[:3], notable, res.aliases, 1, anchored=True):
                    add(src, t, "fork_of", 0.75, f"“{desc[:90].strip()}”", "description")
            lead = desc[:120]
            seen_noun = set()
            for mm in _NOUN.finditer(lead):
                kind = NOUN_KIND[mm.group(1).lower()]
                if kind in seen_noun:
                    continue
                seen_noun.add(kind)
                nxt = _TOKEN.search(lead, mm.end())
                if nxt and lead[mm.end():nxt.start()].strip() == "" and nxt.group(0).lower() not in ALLOW_NEXT:
                    continue          # "Discord Client modification": the noun is not the head
                win = _head_window(lead, mm.start())
                if not win:
                    continue
                for t in _targets(win, notable, res.aliases, 2):
                    add(src, t, kind, 0.75 if kind == "client_for" else 0.7, f"“{desc[:90].strip()}”", "description")
        # (2) name evidence: <target>-rpc, <target>-overlay, <target>-tui ...
        m = _NAME.match(name)
        if m and m.group("s") in NAME_SUFFIX:
            kind, conf = NAME_SUFFIX[m.group("s")]
            x = alias_to_ident.get(slug(normalize(m.group("x"))), slug(normalize(m.group("x"))))
            if x in notable and x != src:
                add(src, x, kind, conf, f"name “{r['name']}”", "name")

    # (3) GitHub enrichment: the repository is a fork of another packaged project
    for rp, (ident, _) in repo_ident.items():
        info = enrichment.get(rp)
        if info and info.get("fork") and info.get("parent"):
            parent = repo_ident.get(info["parent"].lower())
            if parent and parent[0] != ident:
                add(ident, parent[0], "fork_of", 0.9, f"GitHub: {rp.split('/', 1)[1]} is a fork of {info['parent']}", "github")

    # (4) curated relations (shipped + the user's own corrections) and deny list
    for rel in curated["relations"]:
        s, d, k = rel.get("src"), rel.get("dst"), rel.get("kind")
        if s in res.identities and d in res.identities:
            add(s, d, k, 1.0, rel.get("evidence", "curated"), "curated")
    deny = {(x.get("src"), x.get("dst")) for x in curated["deny"]}

    # keep confident, non-contradictory relationships (A client_for B beats an equally weak B client_for A)
    for (s, d, k), e in ev.items():
        if e["conf"] < MIN_CONFIDENCE or (s, d) in deny:
            continue
        rev = ev.get((d, s, k))
        if rev and "curated" not in e["origin"] and rev["conf"] >= e["conf"] and (d, s) not in deny:
            if rev["conf"] > e["conf"] or d < s:
                continue
        res.relations.append((s, d, k, round(e["conf"], 2), " · ".join(e["evidence"]), "+".join(sorted(e["origin"])), weight.get(s, 0)))
    return res
