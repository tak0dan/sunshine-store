"""Catalogue benchmark against the engineering targets:  python3 tests/bench.py [100000 250000 500000]

  load existing catalogue < 100 ms   |  search < 50 ms   |  publish generation < 100 ms
"""
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)
os.environ["SUNSHINE_STORE_OFFLINE"] = "1"

import catalogue as cat   # noqa: E402
import identity           # noqa: E402
import index              # noqa: E402
import sources as S       # noqa: E402

_R = random.Random(11)
SYL = ["".join(_R.choice("bcdfghjklmnprstvwxz") + _R.choice("aeiou") + (_R.choice("lnrst") if _R.random() < .4 else "") for _ in range(1)) for _ in range(260)]
# a realistic vocabulary: ~20k distinct pseudo-words, so a typical query matches tens or hundreds of rows, like a real catalogue
WORDS = sorted({"".join(_R.choice(SYL) for _ in range(_R.randint(2, 3))) for _ in range(26000)})[:20000]
BROAD = ["bin", "git", "client", "for"]          # deliberately unselective: matches a large share of the catalogue
NOUNS = "editor player client browser server library tool game manager viewer converter plugin theme launcher wrapper".split()


def make_rows(source, n, rng, start=0):
    rows = []
    for i in range(start, start + n):
        a, b = rng.choice(WORDS), rng.choice(WORDS)
        name = f"{a}-{b}" + rng.choice(["", "", "-bin", "-git", "-nightly"]) + (str(i) if rng.random() < 0.3 else "")
        desc = " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 9))) + f" {rng.choice(NOUNS)} for {rng.choice(WORDS)}"
        rows.append(S.row(source, name, "1.0", desc, "repo", app_id=f"org.{a}.{b}" if source == "flatpak" else "", votes=rng.randint(0, 500),
                          is_app=1 if source != "aur" and rng.random() < 0.4 else 0, title=a.capitalize() if source != "aur" else ""))
    return rows


def run(total):
    d = tempfile.mkdtemp(prefix="sstore-bench-")
    cat.CACHE, cat.DB_PATH, cat.LOCK_PATH, cat.STATE_PATH = d, os.path.join(d, "index.db"), os.path.join(d, "refresh.lock"), os.path.join(d, "state.json")
    index.CACHE, index.DB_PATH, index.STATE_PATH = d, cat.DB_PATH, cat.STATE_PATH
    rng = random.Random(7)
    split = {"aur": 0.70, "pacman": 0.15, "flatpak": 0.08, "appimage": 0.07}
    data = {s: make_rows(s, int(total * f), rng) for s, f in split.items()}

    class Fixed(S.Source):
        def __init__(self, name):
            self.name, self.label, self.min_rows, self.min_interval = name, name, 1, 0

        def fetch(self, v, force=False):
            return list(data[self.name]), "v1"

    t = time.perf_counter()
    index.refresh(lambda m: None, source_list=[Fixed(s) for s in split], enrich=False)
    build = time.perf_counter() - t
    db = cat.connect()
    n = db.execute("SELECT COUNT(*) FROM pkgs_active").fetchone()[0]

    # --- publish: stage a fresh AUR snapshot + its identity graph, then time ONLY the atomic switch
    sg = cat.next_sgen(db)
    cat.stage_rows(db, "aur", sg, data["aur"], {}, False, True)
    gen = cat.begin_generation(db)
    cand = {**cat.active_map(db), "aur": sg}
    t = time.perf_counter()
    res = identity.resolve(cat.rows_for(db, cand, "id,source,name,name_l,title,app_id,desc,url,votes,is_app,first_seen"))
    resolve_s = time.perf_counter() - t
    cat.store_resolution(db, gen, res)
    t = time.perf_counter()
    cat.publish(db, gen, {"aur": sg}, {"aur": ("v1", len(data["aur"]), S.PARSER_VERSION)}, {"aur": len(data["aur"])}, software_count=len(res.identities))
    publish_ms = (time.perf_counter() - t) * 1000
    db.close()

    # --- load: a cold process opening the existing catalogue and answering its first search
    code = ("import sys,time;sys.path.insert(0,%r);import index;t=time.perf_counter();"
            "ok=index.exists();g=index.generation();"
            "print(round((time.perf_counter()-t)*1000))" % SRC)
    # (the cold-process probe needs the cache dir layout CACHE/sunshine-store; use a symlinked layout)
    home = d + "-home"
    os.makedirs(home, exist_ok=True)
    link = os.path.join(home, "sunshine-store")
    if not os.path.exists(link):
        os.symlink(d, link)
    load_ms = []
    for _ in range(5):
        out = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, XDG_CACHE_HOME=home), capture_output=True, text=True)
        load_ms.append(int(out.stdout.strip() or -1))

    # --- search latency over varied queries
    qs = [rng.choice(WORDS) for _ in range(30)] + [rng.choice(WORDS) + " " + rng.choice(NOUNS) for _ in range(10)] + [rng.choice(WORDS)[:2] for _ in range(5)]
    times, broad = [], []
    for q in qs + BROAD:
        index.search(q)
        t = time.perf_counter()
        index.search(q)
        (broad if q in BROAD else times).append((time.perf_counter() - t) * 1000)
    rel = []
    ids = list(dict.fromkeys(r["ident"] for r in index.search(WORDS[0], 40)))[:2]     # the UI expands at most two anchors
    for _ in range(20):
        t = time.perf_counter()
        index.relations_for(ids)
        rel.append((time.perf_counter() - t) * 1000)
    size = os.path.getsize(cat.DB_PATH) / 1e6
    shutil.rmtree(d, True)
    shutil.rmtree(home, True)
    ok = lambda v, lim: "ok " if v < lim else "OVER"   # noqa: E731
    print(f"\n{n:>8,} artifacts   db {size:,.0f} MB   full build {build:.1f}s   identity resolve {resolve_s:.1f}s")
    print(f"  load catalogue (open + read active generation)  median {statistics.median(load_ms):>6.0f} ms  {ok(statistics.median(load_ms), 100)} (target 100)")
    print(f"  search                         median {statistics.median(times):>6.1f} ms  p95 {sorted(times)[int(len(times) * .95) - 1]:>6.1f} ms  {ok(sorted(times)[int(len(times) * .95) - 1], 50)} (target 50)")
    print(f"  search, deliberately broad queries {BROAD}  median {statistics.median(broad):>6.1f} ms  max {max(broad):>6.1f} ms  (stress; not a target)")
    print(f"  relationship expansion         median {statistics.median(rel):>6.1f} ms  {ok(statistics.median(rel), 50)} (target 50)")
    print(f"  publish generation (atomic switch)       {publish_ms:>6.1f} ms  {ok(publish_ms, 100)} (target 100)")


if __name__ == "__main__":
    for n in [int(a) for a in sys.argv[1:]] or [100_000, 250_000, 500_000]:
        run(n)
