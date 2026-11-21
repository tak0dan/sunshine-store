"""Sunshine Store catalogue: public facade (search, queries, refresh coordinator, CLI).

The local catalogue (catalogue.py) is the primary source for the UI; package repositories are upstream providers that the
refresh engine reads in the background (sources.py). Two guarantees follow:

  1. Instant availability. Opening the store only reads the last published catalogue; nothing here waits for a network.
  2. Completeness over time. Refreshes discover new packages, variants, forks, clients and integrations (identity.py) and
     publish them as a new catalogue generation, atomically, while the old one keeps serving searches.

Refresh policy (run by the optional background service, or on demand):
  - never built / maintenance pending -> build now
  - 36 hours since refresh            -> refresh      (wall-clock)
  - 5 hours of active use             -> refresh      (counted 30 minutes at a time while the session is not idle)
Each source also has its own minimum interval and cheap change detection (ETag / Last-Modified / repository fingerprint), so
a refresh in which nothing changed costs almost nothing and rebuilds nothing.

CLI:  python3 index.py --tick | --refresh [--force] | --status | --sources | --discover QUERY
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import catalogue as cat
import identity
import sources as srcmod
from catalogue import CACHE, DB_PATH, STATE_PATH
from identity import SUFFIXES, normalize  # noqa: F401  (re-exported for backend.py)

MAX_AGE = 36 * 3600          # refresh at least every 36 hours
MAX_ACTIVE = 5 * 3600        # ...or after 5 hours of active use
TICK = 30 * 60               # one service tick = 30 active minutes
NEW_DAYS = identity.RECENT_DAYS   # an artifact first seen within this many days is shown as NEW
GITHUB_TTL = 30 * 86400
GITHUB_BUDGET = 25           # GitHub API requests per refresh (anonymous limit is 60/hour)


# ------------------------------------------------------------------ state (refresh policy only; data lives in the catalogue)
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
    return cat.exists()


def _db() -> sqlite3.Connection:
    """A new read-only connection for the calling thread."""
    return cat.connect(readonly=True)


def refresh_running() -> bool:
    return cat.refresh_running()


def generation() -> int:
    """Active catalogue generation (cheap: the UI polls it to notice that a refresh published something)."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        db = _db()
        try:
            return cat.active_generation(db)
        finally:
            db.close()
    except sqlite3.Error:
        return 0


def _identity_stale(db) -> bool:
    want = f"{identity.RULES_VERSION}:{identity.curated_version()}"
    gen = cat.active_generation(db)
    if not gen:
        return False
    if cat.meta_get(db, "identity_ver") != want:
        return True
    return db.execute("SELECT 1 FROM artifact_ident WHERE gen=? LIMIT 1", (gen,)).fetchone() is None


def maintenance_pending() -> bool:
    """Upgrade work (full-text index, identity graph) that a refresh still has to do. Search works meanwhile."""
    if not exists():
        return False
    try:
        db = _db()
        try:
            return (cat.meta_get(db, "fts") not in ("ready", "unsupported")) or _identity_stale(db)
        finally:
            db.close()
    except sqlite3.Error:
        return False


def due(st: dict | None = None) -> bool:
    st = st if st is not None else load_state()
    if not exists() or not st.get("last_refresh"):
        return True
    if maintenance_pending():
        return True
    return (time.time() - st["last_refresh"] >= MAX_AGE) or (st.get("active_seconds", 0) >= MAX_ACTIVE)


def source_status() -> list:
    """Per-source freshness: available | stale | unavailable, with counts and ages (what the UI shows in Settings)."""
    if not exists():
        return []
    db = _db()
    try:
        active, sync = cat.sources_info(db), cat.sync_states(db)
        out = []
        for name in [s.name for s in srcmod.default_sources()]:
            a, s = active.get(name), sync.get(name)
            last = ((s["last_success"] if s else None) or (a["updated"] if a else None))
            out.append({"source": name, "state": cat.classify(name, a, s), "count": a["count"] if a else 0, "last_success": last,
                        "age_hours": round((time.time() - last) / 3600, 1) if last else None,
                        "error": (s["error"] if s and s["status"] == "error" else "")})
        return out
    finally:
        db.close()


def status() -> dict:
    st = load_state()
    n, gen, new = 0, 0, 0
    if exists():
        try:
            db = _db()
            try:
                n = db.execute("SELECT COUNT(*) FROM pkgs_active").fetchone()[0]
                gen = cat.active_generation(db)
                new = new_recent(db)
            finally:
                db.close()
        except sqlite3.Error:
            pass
    return {"built": exists(), "packages": n, "generation": gen, "new": new, "last_refresh": st.get("last_refresh"),
            "active_hours": round(st.get("active_seconds", 0) / 3600, 2), "due": due(st), "running": cat.refresh_running(),
            "sources": source_status()}


def new_since(ts: float, db=None) -> int:
    """Artifacts first discovered at or after `ts` (the UI uses it for "N new" after a generation change)."""
    own = db is None
    db = db or _db()
    try:
        return db.execute("SELECT COUNT(*) FROM pkgs_active WHERE first_seen >= ?", (int(ts),)).fetchone()[0]
    finally:
        if own:
            db.close()


def new_recent(db=None) -> int:
    return new_since(time.time() - NEW_DAYS * 86400, db)


# ------------------------------------------------------------------ refresh engine
def refresh(progress=lambda msg: None, force: bool = False, source_list=None, enrich: bool = True) -> dict:
    """Refresh the catalogue in the background. Safe to interrupt at any point: the active generation is never touched until a
    complete, validated one is published. If another process (the app or the systemd service) is already refreshing, this
    returns immediately with busy=True instead of starting a second indexer."""
    lock = cat.RefreshLock()
    if not lock.acquire():
        progress("A refresh is already running…")
        out = status()
        out["busy"] = True
        return out
    try:
        return _refresh_locked(progress, force, source_list or srcmod.default_sources(), enrich)
    finally:
        lock.release()


def _refresh_locked(progress, force, source_list, enrich) -> dict:
    db = cat.connect()
    gen = None
    try:
        db.execute("UPDATE catalogue_generations SET status='failed', note='interrupted' WHERE status='building'")
        cat.collect_garbage(db)                          # leftovers of an interrupted refresh
        if cat.meta_get(db, "fts") != "unsupported" and cat.meta_get(db, "fts") != "ready":
            if db.execute("SELECT 1 FROM pkgs LIMIT 1").fetchone():
                progress("Building the search index…")
                cat.rebuild_fts(db)
        fts = cat.fts_ready(db)
        syncs, amap = cat.sync_states(db), cat.active_map(db)
        first_run = not amap
        staged, counts, pending, failures = {}, {}, {}, {}
        now = time.time()

        for src in source_list:
            s = syncs.get(src.name)
            parser_ok = bool(s and s["parser"] == srcmod.PARSER_VERSION)
            if (not force and parser_ok and src.min_interval and src.name in amap and s["status"] == "ok"
                    and s["last_attempt"] and now - s["last_attempt"] < src.min_interval):
                continue                                  # checked recently enough: costs nothing
            progress(src.label)
            try:
                rows, new_v = src.fetch(s["validator"] if (s and parser_ok) else None, force or not parser_ok)
            except Exception as e:                        # source down: keep its last good snapshot
                cat.record_attempt(db, src.name, False, str(e))
                failures[src.name] = str(e)
                progress(f"{src.name} is unavailable ({e}); keeping the data indexed earlier")
                continue
            if rows is srcmod.UNCHANGED:
                cat.record_attempt(db, src.name, True)
                continue
            n, prev = len(rows), (cat.sources_info(db).get(src.name) or {"count": 0})["count"]
            if n < src.min_rows or (prev and n < prev * 0.5):   # a failed fetch must never look like an empty source
                msg = f"suspicious snapshot ({n} records, had {prev}); keeping the previous one"
                cat.record_attempt(db, src.name, False, msg)
                failures[src.name] = msg
                progress(f"{src.name}: {msg}")
                continue
            sg = cat.next_sgen(db)
            cat.stage_rows(db, src.name, sg, rows, cat.first_seen_map(db, src.name), initial=src.name not in amap, fts=fts)
            staged[src.name], counts[src.name] = sg, n
            pending[src.name] = (new_v, n, srcmod.PARSER_VERSION)
            if first_run:                                 # first launch: results appear source by source
                g = cat.begin_generation(db, f"initial: {src.name}")
                cat.publish(db, g, {src.name: sg}, {src.name: pending[src.name]}, {src.name: n}, require_identity=False)
                amap[src.name] = sg
                staged, counts, pending = {}, {}, {}
                progress(f"{src.name}: {n:,} packages ready to search")

        need_identity = bool(staged) or first_run or _identity_stale(db)
        if staged or (need_identity and cat.active_generation(db)):
            progress("Resolving software identities…")
            gen = cat.begin_generation(db)               # allocated last, so generation numbers only ever grow
            candidate = {**cat.active_map(db), **staged}
            _publish_with_identity(db, gen, candidate, staged, pending, counts)
            gen = None
        cat.collect_garbage(db)

        if enrich and not srcmod.offline() and cat.active_generation(db):
            if _enrich_github(db, progress):
                progress("Updating relationships…")
                gen = cat.begin_generation(db, "identity update")
                _publish_with_identity(db, gen, cat.active_map(db), {}, {}, {})
                gen = None
                cat.collect_garbage(db)

        st = load_state()
        if not failures:
            st["last_refresh"], st["active_seconds"] = time.time(), 0
        elif not st.get("last_refresh") and cat.active_generation(db):
            st["last_refresh"] = time.time() - MAX_AGE + 2 * 3600     # partial data: retry in a couple of hours
        save_state(st)
        progress("Catalogue ready" if not failures else "Catalogue ready (some sources could not be reached)")
    except Exception as e:
        if gen:
            cat.abandon(db, gen, str(e))
        raise
    finally:
        db.close()
    return status()


def _publish_with_identity(db, gen, candidate, staged, pending, counts) -> None:
    rows = cat.rows_for(db, candidate, "id,source,name,name_l,title,app_id,desc,url,votes,is_app,first_seen")
    res = identity.resolve(rows, _load_enrichment(db))
    cat.store_resolution(db, gen, res)
    cat.publish(db, gen, staged, pending, counts, software_count=len(res.identities))
    cat.meta_set(db, "identity_ver", f"{identity.RULES_VERSION}:{identity.curated_version()}")


# ------------------------------------------------------------------ enrichment pipeline (separate from the core catalogue)
def _load_enrichment(db) -> dict:
    out = {}
    for r in db.execute("SELECT key, data FROM enrichment"):
        try:
            out[r["key"]] = json.loads(r["data"])
        except ValueError:
            pass
    return out


def _enrich_github(db, progress) -> bool:
    """Ask GitHub whether the upstream repositories of fork-like packages are forks (and of what). Budgeted and cached; the
    catalogue never depends on it. Returns True when new facts were stored."""
    cand = db.execute("SELECT url, MAX(votes) v FROM pkgs_active WHERE url LIKE '%github.com/%' AND "
                      "(lower(desc) LIKE '%fork%' OR name_l LIKE '%fork%') GROUP BY url ORDER BY v DESC LIMIT 400").fetchall()
    have = {r["key"]: r["fetched_at"] for r in db.execute("SELECT key, fetched_at FROM enrichment")}
    todo, seen = [], set()
    for r in cand:
        rp = identity.repo_of(r["url"])
        if rp and rp not in seen and time.time() - have.get(rp, 0) > GITHUB_TTL:
            seen.add(rp)
            todo.append(rp)
    stored = 0
    for rp in todo[:GITHUB_BUDGET]:
        try:
            owner, name = rp.split("/")[1:3]
            req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{name}",
                                         headers={"User-Agent": "sunshine-store-indexer", "Accept": "application/vnd.github+json"})
            if os.environ.get("GITHUB_TOKEN"):
                req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
            data = {"fork": bool(d.get("fork")), "parent": ("github.com/" + d["parent"]["full_name"].lower()) if d.get("parent") else "",
                    "topics": d.get("topics") or [], "stars": d.get("stargazers_count", 0), "archived": bool(d.get("archived"))}
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):                      # rate limited: stop politely, try again next refresh
                break
            data = {"missing": True}
        except Exception:
            break                                         # offline or GitHub down: no effect on the catalogue
        db.execute("INSERT OR REPLACE INTO enrichment VALUES (?,?,?)", (rp, int(time.time()), json.dumps(data)))
        stored += 1
    return stored > 0


# ------------------------------------------------------------------ service tick
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


# ------------------------------------------------------------------ queries (always against the published generation)
_SEL = "SELECT p.*, COALESCE(m.ident, p.key) AS ident FROM pkgs_active p LEFT JOIN artifact_ident m ON m.gen=? AND m.pkg_id=p.id"
_RANK = """CASE WHEN p.name_l = :q OR p.key = :q OR COALESCE(m.ident,'') = :q OR (:aid <> '' AND m.ident = :aid) THEN 0
                WHEN p.name_l LIKE :p THEN 1
                WHEN p.title <> '' AND lower(p.title) LIKE :l THEN 2
                WHEN p.name_l LIKE :l THEN 3 ELSE 4 END"""


def is_new(row) -> bool:
    try:
        fs = row["first_seen"]
    except (KeyError, IndexError):
        return False
    return bool(fs) and time.time() - fs < NEW_DAYS * 86400


def _fts_expr(q: str):
    """Trigram full-text expression: every word must appear as a substring (words of 3+ characters)."""
    words = re.findall(r"[^\s\"']+", q.lower())
    if not words or any(len(w) < 3 for w in words):
        return None
    return " AND ".join('"' + w.replace('"', '""') + '"' for w in words)


_ORDER = "ORDER BY rank, p.is_app DESC, p.votes DESC, length(p.name_l), p.name_l, p.source, p.name LIMIT :n"
_JOIN_M = "LEFT JOIN artifact_ident m ON m.gen = :g AND m.pkg_id = p.id"


def _hydrate(db, g: int, ids: list) -> list:
    """Full rows for the ranked ids, in rank order. Ranking sorts only narrow (id, rank) tuples: sorting whole rows (with their
    descriptions) for tens of thousands of matches is what makes a naive search slow."""
    if not ids:
        return []
    by_id = {r["id"]: r for r in db.execute(
        f"SELECT p.*, COALESCE(m.ident, p.key) AS ident FROM pkgs p LEFT JOIN artifact_ident m ON m.gen=? AND m.pkg_id=p.id "
        f"WHERE p.id IN ({','.join('?' * len(ids))})", [g] + ids)}
    return [by_id[i] for i in ids if i in by_id]


def search(query: str, limit: int = 400) -> list:
    """Rows matching the query across all sources, best first. Uses the full-text index (milliseconds); very short queries use
    the name prefix; the plain scan only remains for the moment after an upgrade, until the index has been built."""
    if not exists():
        return []
    q = query.strip().lower()
    if not q:
        return []
    db = _db()
    try:
        g = cat.active_generation(db)
        al = db.execute("SELECT ident FROM identity_alias WHERE gen=? AND alias=?", (g, identity.slug(q))).fetchone()
        args = {"q": q, "p": q + "%", "l": f"%{q}%", "aid": al[0] if al else "", "n": limit, "g": g}
        expr = _fts_expr(q) if cat.fts_ready(db) else None
        ids = None
        if expr:
            try:
                ids = [r[0] for r in db.execute(
                    f"SELECT p.id, {_RANK} AS rank FROM pkgs_fts JOIN pkgs_active p ON p.id = pkgs_fts.rowid {_JOIN_M} "
                    f"WHERE pkgs_fts MATCH :m {_ORDER}", dict(args, m=expr))]
            except sqlite3.Error:
                ids = None
        if ids is None and len(q) <= 2:
            # one or two characters: prefix of the (indexed) name, never a scan of every description
            ids = [r[0] for r in db.execute(
                f"SELECT p.id, {_RANK} AS rank FROM pkgs_active p {_JOIN_M} WHERE p.name_l >= :q AND p.name_l < :hi {_ORDER}",
                dict(args, hi=q + "\U0010ffff"))]
        if ids is None:
            ids = [r[0] for r in db.execute(
                f"SELECT p.id, {_RANK} AS rank FROM pkgs_active p {_JOIN_M} "
                "WHERE p.name_l LIKE :l OR p.key LIKE :l OR lower(p.app_id) LIKE :l OR lower(p.title) LIKE :l OR lower(p.desc) LIKE :l "
                f"{_ORDER}", args)]
        if al:                                            # an alias ("vscode") brings in every artifact of that software
            have = set(ids)
            extra = [r[0] for r in db.execute("SELECT m.pkg_id FROM artifact_ident m JOIN pkgs_active p ON p.id = m.pkg_id "
                                              "WHERE m.gen=? AND m.ident=? LIMIT 60", (g, al[0])) if r[0] not in have]
            ids = extra + ids
        return _hydrate(db, g, ids)
    finally:
        db.close()


def by_names(names: list) -> list:
    if not exists() or not names:
        return []
    db = _db()
    try:
        marks = ",".join("?" * len(names))
        low = [n.lower() for n in names]
        return db.execute(f"{_SEL} WHERE p.name_l IN ({marks}) OR p.key IN ({marks})", [cat.active_generation(db)] + low * 2).fetchall()
    finally:
        db.close()


def by_category(cats: list, source: str | None = None, limit: int = 12) -> list:
    if not exists() or not cats:
        return []
    db = _db()
    try:
        cond = " OR ".join("(',' || p.cats || ',') LIKE ?" for _ in cats)
        args = [cat.active_generation(db)] + [f"%,{c},%" for c in cats]
        sql = f"{_SEL} WHERE p.is_app=1 AND ({cond})" + (" AND p.source=?" if source else "") + " ORDER BY length(p.name_l) LIMIT ?"
        return db.execute(sql, args + ([source] if source else []) + [limit]).fetchall()
    finally:
        db.close()


def trending(source: str, n: int) -> list:
    if not exists():
        return []
    db = _db()
    try:
        return db.execute(f"{_SEL} WHERE p.source=? AND p.ood=0 ORDER BY p.pop DESC LIMIT ?", (cat.active_generation(db), source, n)).fetchall()
    finally:
        db.close()


def artifacts_of(idents: list, per: int = 12) -> list:
    """Every artifact of the given software identities (all sources), most relevant first."""
    if not exists() or not idents:
        return []
    db = _db()
    try:
        g = cat.active_generation(db)
        out = []
        for i in idents:
            out += db.execute("SELECT p.*, m.ident AS ident FROM artifact_ident m JOIN pkgs_active p ON p.id = m.pkg_id "
                              "WHERE m.gen=? AND m.ident=? ORDER BY p.is_app DESC, p.votes DESC LIMIT ?", (g, i, per)).fetchall()
        return out
    finally:
        db.close()


def relations_for(idents: list, per_group: int = 10) -> list:
    """Typed relationships around the given identities, both directions, grouped as
    [{"anchor", "kind", "dir": "in"|"out", "total", "items": [{"other","confidence","evidence","origin"}]}]."""
    if not exists() or not idents:
        return []
    db = _db()
    try:
        g = cat.active_generation(db)
        out = []
        for a in idents:
            for kind in identity.KINDS:
                for direction, col, oth in (("in", "dst", "src"), ("out", "src", "dst")):
                    where = f"gen=? AND {col}=? AND kind=?"
                    total = db.execute(f"SELECT COUNT(*) FROM relations WHERE {where}", (g, a, kind)).fetchone()[0]
                    if not total:
                        continue
                    lim = per_group if direction == "in" else 3
                    rows = db.execute(f"SELECT {oth} AS other, confidence, evidence, origin FROM relations WHERE {where} "
                                      "ORDER BY confidence DESC, weight DESC LIMIT ?", (g, a, kind, lim)).fetchall()
                    out.append({"anchor": a, "kind": kind, "dir": direction, "total": total,
                                "items": [dict(r) for r in rows]})
        return out
    finally:
        db.close()


# ------------------------------------------------------------------ command line
if __name__ == "__main__":
    args = sys.argv[1:]
    arg = args[0] if args else "--status"
    say = lambda m: print(m, flush=True)  # noqa: E731
    if arg == "--refresh":
        print(json.dumps(refresh(say, force="--force" in args), default=str))
    elif arg == "--tick":
        print(json.dumps(tick(say), default=str))
    elif arg == "--sources":
        for s in source_status():
            print(f"{s['source']:<9} {s['state']:<12} {s['count']:>8,} records  age {s['age_hours']} h  {s['error']}")
    elif arg == "--discover" and len(args) > 1:
        rows = search(" ".join(args[1:]), 40)
        idents = []
        for r in rows[:6]:
            if r["ident"] not in idents:
                idents.append(r["ident"])
        print("identities:", ", ".join(idents))
        for grp in relations_for(idents[:3], 8):
            label = (identity.LABEL_IN if grp["dir"] == "in" else identity.LABEL_OUT)[grp["kind"]]
            print(f"\n{label}  [{grp['anchor']}]  ({grp['total']})")
            for it in grp["items"]:
                print(f"   {it['other']:<32} {it['confidence']:.2f} {it['origin']}")
    else:
        print(json.dumps(status(), default=str))
