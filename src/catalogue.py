"""Persistent, versioned local catalogue (SQLite, WAL).

The UI reads *only* this database; package repositories are upstream data providers that a refresh engine copies into it.

Generations
-----------
Package rows are written per source into a *staging snapshot* (`pkgs.sgen`) that readers cannot see. What readers see is
`pkgs_active`: the rows whose (source, sgen) is listed in `active_sources`. Publishing a new catalogue generation is one
small transaction that repoints `active_sources` and `catalogue_state` at the finished snapshots, so:

  * an interrupted refresh can never damage or half-replace the active catalogue,
  * a source that fails or is unchanged simply keeps its previous snapshot (a failure is never read as "empty"),
  * the previous generation is retained for rollback and garbage-collected after that.

Identity tables (`artifact_ident`, `identities`, `identity_alias`, `relations`) are tagged with the catalogue generation
they were computed for, and are published by the same transaction.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "sunshine-store")
DB_PATH = os.path.join(CACHE, "index.db")
LOCK_PATH = os.path.join(CACHE, "refresh.lock")
STATE_PATH = os.path.join(CACHE, "state.json")
SCHEMA_VERSION = 2
KEEP_GENERATION_ROWS = 12

COLS = ("source", "name", "name_l", "key", "title", "version", "desc", "repo", "app_id", "votes", "pop", "url", "maintainer",
        "ood", "icon", "cats", "is_app")

# A source is "stale" once its last successful sync is older than this (seconds); the data stays searchable.
STALE_AFTER = {"pacman": 14 * 86400, "aur": 3 * 86400, "flatpak": 5 * 86400, "appimage": 14 * 86400}
DEFAULT_STALE = 5 * 86400

DDL = """
CREATE TABLE IF NOT EXISTS pkgs (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, name_l TEXT NOT NULL, key TEXT NOT NULL,
  title TEXT DEFAULT '', version TEXT DEFAULT '', desc TEXT DEFAULT '', repo TEXT DEFAULT '', app_id TEXT DEFAULT '',
  votes INTEGER DEFAULT 0, pop REAL DEFAULT 0, url TEXT DEFAULT '', maintainer TEXT DEFAULT '', ood INTEGER DEFAULT 0,
  icon TEXT DEFAULT '', cats TEXT DEFAULT '', is_app INTEGER DEFAULT 0,
  sgen INTEGER NOT NULL DEFAULT 1, first_seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS i_name ON pkgs(name_l);
CREATE INDEX IF NOT EXISTS i_key ON pkgs(key);
CREATE INDEX IF NOT EXISTS i_src ON pkgs(source, is_app);
CREATE INDEX IF NOT EXISTS i_sgen ON pkgs(source, sgen);

CREATE TABLE IF NOT EXISTS active_sources (
  source TEXT PRIMARY KEY, sgen INTEGER NOT NULL, published_gen INTEGER NOT NULL, updated INTEGER NOT NULL, count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalogue_generations (
  id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, completed_at INTEGER, status TEXT NOT NULL,
  package_count INTEGER DEFAULT 0, software_count INTEGER DEFAULT 0, sources TEXT DEFAULT '{}', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalogue_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), generation INTEGER, previous_generation INTEGER
);
CREATE TABLE IF NOT EXISTS source_sync_state (
  source TEXT PRIMARY KEY, last_attempt INTEGER, last_success INTEGER, validator TEXT, status TEXT DEFAULT '',
  error TEXT DEFAULT '', records INTEGER DEFAULT 0, parser INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS artifact_ident (gen INTEGER NOT NULL, pkg_id INTEGER NOT NULL, ident TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS i_ai_pkg ON artifact_ident(gen, pkg_id);
CREATE INDEX IF NOT EXISTS i_ai_ident ON artifact_ident(gen, ident);
CREATE TABLE IF NOT EXISTS identities (gen INTEGER NOT NULL, ident TEXT NOT NULL, name TEXT, count INTEGER, sources TEXT,
  PRIMARY KEY (gen, ident));
CREATE TABLE IF NOT EXISTS identity_alias (gen INTEGER NOT NULL, alias TEXT NOT NULL, ident TEXT NOT NULL, PRIMARY KEY (gen, alias));
CREATE TABLE IF NOT EXISTS relations (
  gen INTEGER NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, confidence REAL, evidence TEXT, origin TEXT,
  weight REAL DEFAULT 0, PRIMARY KEY (gen, src, dst, kind)
);
CREATE INDEX IF NOT EXISTS i_rel_in ON relations(gen, dst, kind, confidence DESC, weight DESC);
CREATE INDEX IF NOT EXISTS i_rel_out ON relations(gen, src, kind, confidence DESC, weight DESC);
CREATE TABLE IF NOT EXISTS enrichment (key TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL, data TEXT NOT NULL);

CREATE VIEW IF NOT EXISTS pkgs_active AS
  SELECT p.* FROM pkgs p JOIN active_sources a ON a.source = p.source AND a.sgen = p.sgen;
"""

FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS pkgs_fts USING fts5("
           "name_l, title, app_id, desc, content='pkgs', content_rowid='id', tokenize='trigram')")


class Busy(Exception):
    """Another process is already refreshing the catalogue."""


# ------------------------------------------------------------------------------------------------ connections
def connect(readonly: bool = False) -> sqlite3.Connection:
    """New connection (one per thread). Readers never block on the refresh writer (WAL)."""
    os.makedirs(CACHE, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def exists() -> bool:
    """True when a catalogue with at least one published source is available (instant: no network, no rebuild)."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        db = connect(readonly=True)
        try:
            return db.execute("SELECT 1 FROM active_sources LIMIT 1").fetchone() is not None
        finally:
            db.close()
    except sqlite3.Error:
        return False


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create or migrate the schema. Cheap no-op once current; a legacy (pre-generation) catalogue is upgraded in place, so
    the previously indexed data stays instantly available."""
    if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            db.execute("ROLLBACK")
            return
        legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pkgs'").fetchone() is not None
        if legacy:
            have = {r[1] for r in db.execute("PRAGMA table_info(pkgs)")}
            if "sgen" not in have:
                db.execute("ALTER TABLE pkgs ADD COLUMN sgen INTEGER NOT NULL DEFAULT 1")
            if "first_seen" not in have:
                db.execute("ALTER TABLE pkgs ADD COLUMN first_seen INTEGER NOT NULL DEFAULT 0")
        _script(db, DDL)
        try:
            _script(db, FTS_DDL)
            if not legacy:                      # brand-new catalogue: an empty index is already consistent with the empty table
                db.execute("INSERT OR REPLACE INTO meta VALUES ('fts','ready')")
        except sqlite3.OperationalError:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('fts','unsupported')")
        if db.execute("SELECT COUNT(*) FROM catalogue_state").fetchone()[0] == 0:
            now = int(time.time())
            counts = {r[0]: r[1] for r in db.execute("SELECT source, COUNT(*) FROM pkgs GROUP BY source")}
            for src, n in counts.items():
                db.execute("INSERT OR REPLACE INTO active_sources VALUES (?,?,?,?,?)", (src, 1, 1, now, n))
            gen = 1 if counts else None
            if gen:
                db.execute("INSERT OR REPLACE INTO catalogue_generations(id,created_at,completed_at,status,package_count,sources,note) "
                           "VALUES (1,?,?,?,?,?,?)", (now, now, "active", sum(counts.values()), json.dumps({s: 1 for s in counts}),
                                                      "migrated from the previous catalogue format"))
            db.execute("INSERT INTO catalogue_state VALUES (1,?,NULL)", (gen,))
            _migrate_validators(db, counts, now)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    for blob in ("aur.blob", "flathub.blob", "appimage.blob"):      # old download cache: no longer needed
        try:
            os.remove(os.path.join(CACHE, blob))
        except OSError:
            pass


def _script(db, sql: str) -> None:
    """executescript() would COMMIT our open transaction, so run statements one by one."""
    for stmt in sql.split(";\n"):
        stmt = stmt.strip().rstrip(";")
        if stmt:
            db.execute(stmt)


def _migrate_validators(db, counts, now) -> None:
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for src in counts:
        tag = {"flatpak": "flathub"}.get(src, src)
        v = json.dumps({"etag": st.get(tag + "_etag"), "lm": st.get(tag + "_lm")}) if st.get(tag + "_etag") or st.get(tag + "_lm") else None
        db.execute("INSERT OR REPLACE INTO source_sync_state(source,last_attempt,last_success,validator,status,records,parser) "
                   "VALUES (?,?,?,?,?,?,0)", (src, st.get("last_refresh") or now, st.get("last_refresh") or now, v, "ok", counts[src]))


# ------------------------------------------------------------------------------------------------ small helpers
def meta_get(db, key, default=None):
    r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(db, key, value) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


def fts_ready(db) -> bool:
    return meta_get(db, "fts") == "ready"


def active_generation(db) -> int:
    r = db.execute("SELECT generation FROM catalogue_state WHERE id=1").fetchone()
    return (r[0] or 0) if r else 0


def active_map(db) -> dict:
    return {r[0]: r[1] for r in db.execute("SELECT source, sgen FROM active_sources")}


def sources_info(db) -> dict:
    return {r["source"]: r for r in db.execute("SELECT * FROM active_sources")}


def sync_states(db) -> dict:
    return {r["source"]: r for r in db.execute("SELECT * FROM source_sync_state")}


def classify(source: str, active, sync, now: float | None = None) -> str:
    """available | stale | unavailable (see README: stale data stays searchable and says how old it is)."""
    now = now or time.time()
    if active is None:
        return "unavailable"
    last = (sync["last_success"] if sync else None) or active["updated"]
    failed = bool(sync and sync["status"] == "error")
    if failed or now - last > STALE_AFTER.get(source, DEFAULT_STALE):
        return "stale"
    return "available"


# ------------------------------------------------------------------------------------------------ generations
def _next_counter(db, key: str, floor: int) -> int:
    n = max(int(meta_get(db, key, 0) or 0), floor) + 1
    meta_set(db, key, n)
    return n


def begin_generation(db, note: str = "") -> int:
    db.execute("BEGIN IMMEDIATE")
    try:
        floor = db.execute("SELECT COALESCE(MAX(id),0) FROM catalogue_generations").fetchone()[0]
        gid = _next_counter(db, "gen_counter", floor)
        db.execute("INSERT INTO catalogue_generations(id,created_at,status,note) VALUES (?,?,?,?)", (gid, int(time.time()), "building", note))
        db.execute("COMMIT")
        return gid
    except Exception:
        db.execute("ROLLBACK")
        raise


def next_sgen(db) -> int:
    db.execute("BEGIN IMMEDIATE")
    try:
        floor = db.execute("SELECT COALESCE(MAX(sgen),0) FROM pkgs").fetchone()[0]
        n = _next_counter(db, "sgen_counter", floor)
        db.execute("COMMIT")
        return n
    except Exception:
        db.execute("ROLLBACK")
        raise


def stage_rows(db, source: str, sgen: int, rows, first_seen_of: dict, initial: bool, fts: bool) -> int:
    """Write one source's full snapshot, invisible to readers, in a single transaction (all or nothing)."""
    now = int(time.time())
    ph = ",".join("?" * (len(COLS) + 2))
    sql = f"INSERT INTO pkgs({','.join(COLS)},sgen,first_seen) VALUES ({ph})"
    n = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        batch = []
        for r in rows:
            seen = first_seen_of.get((r[2], r[8]))             # (name_l, app_id)
            batch.append(tuple(r) + (sgen, seen if seen is not None else (0 if initial else now)))
            n += 1
            if len(batch) >= 5000:
                db.executemany(sql, batch)
                batch = []
        if batch:
            db.executemany(sql, batch)
        if fts:
            db.execute("INSERT INTO pkgs_fts(rowid,name_l,title,app_id,desc) SELECT id,name_l,title,app_id,desc FROM pkgs "
                       "WHERE source=? AND sgen=?", (source, sgen))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return n


def first_seen_map(db, source: str) -> dict:
    sg = active_map(db).get(source)
    if sg is None:
        return {}
    return {(r[0], r[1]): r[2] for r in db.execute("SELECT name_l, app_id, first_seen FROM pkgs WHERE source=? AND sgen=?", (source, sg))}


def rows_for(db, sgens: dict, columns: str = "*"):
    """Rows of a candidate catalogue: {source: sgen}. Used by identity resolution before anything is published."""
    out = []
    for src, sg in sgens.items():
        out += db.execute(f"SELECT {columns} FROM pkgs WHERE source=? AND sgen=?", (src, sg)).fetchall()
    return out


def store_resolution(db, gen: int, res) -> None:
    """Persist an identity resolution for catalogue generation `gen` (invisible until that generation is published)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        for t in ("artifact_ident", "identities", "identity_alias", "relations"):
            db.execute(f"DELETE FROM {t} WHERE gen=?", (gen,))
        db.executemany("INSERT INTO artifact_ident VALUES (?,?,?)", ((gen, pid, ident) for pid, ident in res.ident_of.items()))
        in_rel = {r[0] for r in res.relations} | {r[1] for r in res.relations}
        keep_alias = set(res.aliases.values())
        db.executemany("INSERT INTO identities VALUES (?,?,?,?,?)",
                       ((gen, i, rec["name"], rec["count"], ",".join(rec["sources"]))
                        for i, rec in res.identities.items() if rec["count"] > 1 or i in in_rel or i in keep_alias))
        db.executemany("INSERT OR REPLACE INTO identity_alias VALUES (?,?,?)", ((gen, a, i) for a, i in res.aliases.items()))
        db.executemany("INSERT OR REPLACE INTO relations VALUES (?,?,?,?,?,?,?,?)", ((gen,) + tuple(r[:6]) + (r[6],) for r in res.relations))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def publish(db, gen: int, new_sources: dict, pending: dict, counts: dict, require_identity: bool = True,
            software_count: int = 0) -> None:
    """Atomically make `gen` the active catalogue.

    new_sources: {source: sgen} snapshots to switch to (all others keep their current snapshot)
    pending:     {source: (validator, records, parser)} sync state to record together with the switch
    Refuses to publish an incomplete generation."""
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        for src, sg in new_sources.items():
            n = db.execute("SELECT COUNT(*) FROM pkgs WHERE source=? AND sgen=?", (src, sg)).fetchone()[0]
            if n == 0 or n != counts.get(src, n):
                raise RuntimeError(f"refusing to publish {src}: snapshot {sg} is incomplete ({n} rows)")
        if require_identity and db.execute("SELECT 1 FROM identities WHERE gen=? LIMIT 1", (gen,)).fetchone() is None \
                and db.execute("SELECT 1 FROM artifact_ident WHERE gen=? LIMIT 1", (gen,)).fetchone() is None:
            raise RuntimeError("refusing to publish: identity graph for this generation is missing")
        for src, sg in new_sources.items():
            db.execute("INSERT OR REPLACE INTO active_sources VALUES (?,?,?,?,?)", (src, sg, gen, now, counts.get(src, 0)))
        for src, (validator, records, parser) in pending.items():
            db.execute("INSERT INTO source_sync_state(source,last_attempt,last_success,validator,status,error,records,parser) "
                       "VALUES (?,?,?,?, 'ok','',?,?) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, "
                       "last_success=excluded.last_success, validator=excluded.validator, status='ok', error='', "
                       "records=excluded.records, parser=excluded.parser", (src, now, now, validator, records, parser))
        amap = active_map(db)
        pk = db.execute("SELECT COALESCE(SUM(count),0) FROM active_sources").fetchone()[0]     # no table scan: counts were recorded at staging
        db.execute("UPDATE catalogue_generations SET status='active', completed_at=?, package_count=?, software_count=?, sources=? WHERE id=?",
                   (now, pk, software_count, json.dumps(amap), gen))
        old = active_generation(db)
        db.execute("UPDATE catalogue_generations SET status='retired' WHERE id=? AND id<>?", (old, gen))
        db.execute("INSERT INTO catalogue_state(id,generation,previous_generation) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "generation=excluded.generation, previous_generation=excluded.previous_generation", (gen, old or None))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def abandon(db, gen: int, why: str) -> None:
    db.execute("UPDATE catalogue_generations SET status='failed', completed_at=?, note=? WHERE id=? AND status='building'",
               (int(time.time()), why[:300], gen))


def record_attempt(db, source: str, ok: bool, error: str = "") -> None:
    """Note a sync attempt that did not publish new data (unchanged: ok; failed: error). Previous data is untouched."""
    now = int(time.time())
    if ok:
        db.execute("INSERT INTO source_sync_state(source,last_attempt,last_success,status) VALUES (?,?,?, 'ok') "
                   "ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, last_success=excluded.last_success, status='ok', error=''",
                   (source, now, now))
    else:
        db.execute("INSERT INTO source_sync_state(source,last_attempt,status,error) VALUES (?,?, 'error', ?) "
                   "ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, status='error', error=excluded.error",
                   (source, now, error[:300]))


def collect_garbage(db) -> int:
    """Drop snapshots and identity data that are neither active nor the previous (rollback) generation. Also removes the
    leftovers of an interrupted refresh. Returns the number of package rows removed."""
    st = db.execute("SELECT generation, previous_generation FROM catalogue_state WHERE id=1").fetchone()
    keep_gens = {g for g in (st["generation"], st["previous_generation"]) if g}
    keep = {(s, g) for s, g in active_map(db).items()}
    if st["previous_generation"]:
        r = db.execute("SELECT sources FROM catalogue_generations WHERE id=?", (st["previous_generation"],)).fetchone()
        if r:
            try:
                keep |= {(s, g) for s, g in json.loads(r[0] or "{}").items()}
            except ValueError:
                pass
    removed = 0
    ready = fts_ready(db)
    for src, sg in db.execute("SELECT DISTINCT source, sgen FROM pkgs").fetchall():
        if (src, sg) in keep:
            continue
        db.execute("BEGIN IMMEDIATE")
        try:
            if ready:        # external-content FTS: remove the index entries together with the rows
                db.execute("INSERT INTO pkgs_fts(pkgs_fts,rowid,name_l,title,app_id,desc) SELECT 'delete',id,name_l,title,app_id,desc "
                           "FROM pkgs WHERE source=? AND sgen=?", (src, sg))
            removed += db.execute("DELETE FROM pkgs WHERE source=? AND sgen=?", (src, sg)).rowcount
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    ph = ",".join("?" * len(keep_gens)) or "NULL"
    db.execute("BEGIN IMMEDIATE")
    try:
        for t in ("artifact_ident", "identities", "identity_alias", "relations"):
            db.execute(f"DELETE FROM {t} WHERE gen NOT IN ({ph})", tuple(keep_gens))
        db.execute("DELETE FROM catalogue_generations WHERE id NOT IN (SELECT id FROM catalogue_generations ORDER BY id DESC LIMIT ?) "
                   "AND id NOT IN (?, ?)", (KEEP_GENERATION_ROWS, st["generation"] or 0, st["previous_generation"] or 0))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return removed


def rebuild_fts(db) -> None:
    """(Re)build the full-text index from the package table. Search falls back to LIKE until this has finished."""
    if meta_get(db, "fts") == "unsupported":
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT INTO pkgs_fts(pkgs_fts) VALUES ('rebuild')")
        meta_set(db, "fts", "ready")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------------------------------------ single refresher
class RefreshLock:
    """One refresh coordinator per user: the application and the systemd service share this lock."""

    def __init__(self):
        self.fd = None

    def acquire(self) -> bool:
        os.makedirs(CACHE, exist_ok=True)
        self.fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        if not self.acquire():
            raise Busy()
        return self

    def __exit__(self, *a):
        self.release()


def refresh_running() -> bool:
    lock = RefreshLock()
    if lock.acquire():
        lock.release()
        return False
    return True
