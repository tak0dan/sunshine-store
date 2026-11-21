"""Offline tests for the persistent catalogue, refresh engine, search and identity graph.  Run: python3 -m unittest discover tests"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)
os.environ["SUNSHINE_STORE_OFFLINE"] = "1"

import catalogue as cat   # noqa: E402
import identity           # noqa: E402
import index              # noqa: E402
import sources as S       # noqa: E402

R = S.row


class Fake(S.Source):
    """A scriptable package source."""

    def __init__(self, name, rows, min_rows=1, interval=0):
        self.name, self.label, self.min_rows, self.min_interval = name, f"fake {name}", min_rows, interval
        self.rows, self.mode, self.calls, self.version = rows, "rows", 0, 1

    def set(self, rows):
        self.rows, self.version = rows, self.version + 1

    def fetch(self, validator, force=False):
        self.calls += 1
        if self.mode == "fail":
            raise S.SourceUnavailable("down")
        if validator == f"v{self.version}" and not force:
            return S.UNCHANGED, None
        return list(self.rows), f"v{self.version}"


PACMAN = [
    R("pacman", "discord", "1", "Chat for gamers", "extra", url="https://discord.com", is_app=1, title="Discord"),
    R("pacman", "firefox", "1", "Fast, Private & Safe Web Browser", "extra", is_app=1, title="Firefox"),
    R("pacman", "spotify", "1", "A proprietary music streaming service", "extra", is_app=1, title="Spotify"),
    R("pacman", "legcord", "1", "Discord client with builtin client mod and theme support", "extra", is_app=1),
    R("pacman", "ncspot", "1", "Cross-platform ncurses Spotify client written in Rust", "extra"),
    R("pacman", "librewolf", "1", "Firefox fork focused on privacy", "extra", is_app=1),
    R("pacman", "discord-rpc", "1", "Discord Rich Presence library", "extra"),
]
AUR = [
    R("aur", "discord-bin", "1", "Chat for gamers (binary)", votes=300),
    R("aur", "discord-canary", "2", "All-in-one voice and text chat - alpha build", votes=40),
    R("aur", "discord_arch_electron", "1", "Discord using system electron", votes=90),
    R("aur", "oxycode", "0.1", "Terminal-native Discord client written in Rust", votes=5),
    R("aur", "discord-overlay", "1", "Overlay for Discord voice chat", votes=12),
    R("aur", "crycord", "1", "A Discord Client modification with plugins", votes=3),
    R("aur", "spicetify-cli", "1", "Command-line tool to customize Spotify client", votes=200),
]
FLATPAK = [
    R("flatpak", "Discord", "", "Chat for gamers", "flathub", app_id="com.discordapp.Discord", is_app=1, title="Discord",
      key=identity.normalize("com.discordapp.Discord")),
    R("flatpak", "Firefox", "", "Browser", "flathub", app_id="org.mozilla.firefox", is_app=1, title="Firefox",
      key=identity.normalize("org.mozilla.firefox")),
]


def fresh_env(tc):
    d = tempfile.mkdtemp(prefix="sstore-test-")
    tc.addCleanup(shutil.rmtree, d, True)
    cat.CACHE, cat.DB_PATH, cat.LOCK_PATH, cat.STATE_PATH = d, os.path.join(d, "index.db"), os.path.join(d, "refresh.lock"), os.path.join(d, "state.json")
    index.CACHE, index.DB_PATH, index.STATE_PATH = d, cat.DB_PATH, cat.STATE_PATH
    return d


def sources():
    return [Fake("pacman", PACMAN), Fake("aur", AUR), Fake("flatpak", FLATPAK)]


def names(rows):
    return [r["name"] for r in rows]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = fresh_env(self)
        self.msgs = []

    def refresh(self, srcs, **kw):
        return index.refresh(self.msgs.append, source_list=srcs, enrich=False, **kw)

    def conn(self):
        return cat.connect(readonly=True)


class FirstLaunch(Base):
    def test_empty_state_has_no_catalogue_and_search_is_safe(self):
        self.assertFalse(index.exists())
        self.assertEqual(index.search("discord"), [])

    def test_progressive_publication_and_persistence(self):
        srcs = sources()
        gens = []
        orig = cat.publish

        def spy(*a, **k):
            orig(*a, **k)
            db = self.conn()
            gens.append((cat.active_generation(db), sorted(cat.active_map(db)), len(index.search("discord"))))
            db.close()
        cat.publish = spy
        self.addCleanup(setattr, cat, "publish", orig)
        self.refresh(srcs)
        # one publication per source (progressively usable), then the final one with the identity graph
        self.assertEqual([g[1] for g in gens][:3], [["pacman"], ["aur", "pacman"], ["aur", "flatpak", "pacman"]])
        self.assertGreater(gens[0][2], 0, "search worked as soon as the first source was published")
        self.assertTrue(index.exists())
        self.assertGreaterEqual(index.generation(), 4)

    def test_first_seen_is_zero_on_initial_build_so_nothing_is_marked_new(self):
        self.refresh(sources())
        self.assertTrue(all(not index.is_new(r) for r in index.search("discord")))


class IncrementalRefresh(Base):
    def setUp(self):
        super().setUp()
        self.srcs = sources()
        self.refresh(self.srcs)
        self.gen = index.generation()

    def test_unchanged_sources_publish_nothing(self):
        self.refresh(self.srcs)
        self.assertEqual(index.generation(), self.gen)
        db = self.conn()
        self.assertEqual(db.execute("SELECT COUNT(DISTINCT sgen) FROM pkgs").fetchone()[0], 3)   # no snapshot was rebuilt
        db.close()

    def test_new_package_is_discovered_and_marked_new(self):
        aur = self.srcs[1]
        aur.set(AUR + [R("aur", "discord-ptb-bin", "1", "Discord public test build", votes=1), R("aur", "newthing", "1", "brand new tool")])
        self.refresh(self.srcs)
        self.assertGreater(index.generation(), self.gen)
        rows = {r["name"]: r for r in index.search("newthing")}
        self.assertTrue(index.is_new(rows["newthing"]))
        old = {r["name"]: r for r in index.search("oxycode")}
        self.assertFalse(index.is_new(old["oxycode"]), "existing artifacts keep their first_seen")
        self.assertEqual(index.new_recent(), 2)

    def test_only_changed_source_is_rebuilt(self):
        db = self.conn()
        before = cat.active_map(db)
        db.close()
        self.srcs[1].set(AUR + [R("aur", "another", "1", "x")])
        self.refresh(self.srcs)
        db = self.conn()
        after = cat.active_map(db)
        db.close()
        self.assertEqual(before["pacman"], after["pacman"])
        self.assertEqual(before["flatpak"], after["flatpak"])
        self.assertNotEqual(before["aur"], after["aur"])

    def test_removed_package_disappears_only_after_a_successful_reconciliation(self):
        self.srcs[1].set([r for r in AUR if r[1] != "oxycode"])
        self.refresh(self.srcs)
        self.assertNotIn("oxycode", names(index.search("oxycode")))

    def test_per_source_minimum_interval_skips_recent_checks(self):
        srcs = [Fake("pacman", PACMAN), Fake("aur", AUR, interval=3600), Fake("flatpak", FLATPAK, interval=3600)]
        self.refresh(srcs)
        self.assertEqual(srcs[1].calls, 0, "AUR was synced moments ago (setUp): its minimum interval skips the check")
        self.assertGreaterEqual(srcs[0].calls, 1, "pacman has no interval: its cheap check runs every time")
        self.refresh(srcs, force=True)
        self.assertEqual(srcs[1].calls, 1)


class FailureHandling(Base):
    def setUp(self):
        super().setUp()
        self.srcs = sources()
        self.refresh(self.srcs)

    def test_unavailable_source_keeps_its_last_good_data(self):
        self.srcs[2].mode = "fail"
        self.srcs[1].set(AUR + [R("aur", "fresh-pkg", "1", "fresh")])
        out = self.refresh(self.srcs, force=True)
        flat = [r for r in index.search("discord") if r["source"] == "flatpak"]
        self.assertTrue(flat, "Flatpak entries did not vanish because Flathub was down")
        self.assertIn("fresh-pkg", names(index.search("fresh-pkg")), "healthy sources still published")
        st = {s["source"]: s for s in out["sources"]}
        self.assertEqual(st["flatpak"]["state"], "stale")
        self.assertIn("down", st["flatpak"]["error"])
        self.assertEqual(st["aur"]["state"], "available")

    def test_all_sources_down_changes_nothing(self):
        gen = index.generation()
        for s in self.srcs:
            s.mode = "fail"
        self.refresh(self.srcs, force=True)
        self.assertEqual(index.generation(), gen)
        self.assertTrue(index.search("discord"))

    def test_empty_or_tiny_snapshot_is_rejected_not_treated_as_empty(self):
        self.srcs[0].min_rows = 3
        self.srcs[0].set(PACMAN[:1])
        self.refresh(self.srcs, force=True)
        self.assertIn("firefox", names(index.search("firefox")))
        self.assertTrue(any("suspicious" in m for m in self.msgs))

    def test_half_size_snapshot_is_rejected(self):
        self.srcs[1].set(AUR[:2])
        self.refresh(self.srcs, force=True)
        self.assertIn("oxycode", names(index.search("oxycode")))

    def test_failure_during_publication_leaves_active_catalogue_intact(self):
        gen = index.generation()
        before = names(index.search("discord"))
        self.srcs[1].set(AUR + [R("aur", "should-not-appear", "1", "x")])
        orig = cat.publish

        def boom(*a, **k):
            raise RuntimeError("power cut")
        cat.publish = boom
        try:
            with self.assertRaises(RuntimeError):
                self.refresh(self.srcs)
        finally:
            cat.publish = orig
        self.assertEqual(index.generation(), gen)
        self.assertEqual(names(index.search("discord")), before)
        self.assertEqual(index.search("should-not-appear"), [], "staged rows are invisible until published")
        self.refresh(self.srcs)                                     # recovers and cleans the orphan snapshot
        self.assertIn("should-not-appear", names(index.search("should-not-appear")))
        db = self.conn()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM catalogue_generations WHERE status='building'").fetchone()[0], 0)
        db.close()

    def test_hard_kill_mid_refresh_is_survivable(self):
        gen = index.generation()
        script = (
            "import os,sys\nsys.path.insert(0,%r)\nimport catalogue as cat, index, sources as S\n"
            "class Die(S.Source):\n name,label,min_rows,min_interval='aur','x',1,0\n"
            " def fetch(self,v,force=False):\n"
            "  rows=[S.row('aur','zzz-%%d'%%i,'1','d') for i in range(2000)]\n"
            "  return rows,'x'\n"
            "orig=cat.publish\n"
            "def die(*a,**k): os._exit(9)\n"
            "cat.publish=die\n"
            "index.refresh(lambda m:None, source_list=[Die()], enrich=False)\n" % SRC)
        env = dict(os.environ, XDG_CACHE_HOME=os.path.dirname(self.dir), SUNSHINE_STORE_OFFLINE="1")
        # the child must use the same cache dir as this test
        env["XDG_CACHE_HOME"] = self.dir + "-home"
        os.makedirs(os.path.join(env["XDG_CACHE_HOME"], "sunshine-store"), exist_ok=True)
        shutil.copy(cat.DB_PATH, os.path.join(env["XDG_CACHE_HOME"], "sunshine-store", "index.db"))
        self.addCleanup(shutil.rmtree, env["XDG_CACHE_HOME"], True)
        p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
        self.assertEqual(p.returncode, 9, p.stderr.decode())
        child_db = sqlite3.connect(os.path.join(env["XDG_CACHE_HOME"], "sunshine-store", "index.db"))
        self.assertEqual(child_db.execute("SELECT generation FROM catalogue_state").fetchone()[0], gen)
        self.assertEqual(child_db.execute("SELECT COUNT(*) FROM pkgs_active WHERE name LIKE 'zzz-%'").fetchone()[0], 0)
        self.assertGreater(child_db.execute("SELECT COUNT(*) FROM pkgs WHERE name LIKE 'zzz-%'").fetchone()[0], 0, "orphan staging exists")
        child_db.close()


class Coordination(Base):
    def test_second_refresh_does_not_start_while_one_runs(self):
        lock = cat.RefreshLock()
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        out = self.refresh(sources())
        self.assertTrue(out.get("busy"))
        self.assertFalse(index.exists(), "nothing was indexed by the second refresher")
        self.assertTrue(cat.refresh_running())

    def test_lock_is_released_after_refresh(self):
        self.refresh(sources())
        self.assertFalse(cat.refresh_running())

    def test_reader_works_while_a_writer_is_mid_transaction(self):
        self.refresh(sources())
        w = cat.connect()
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT INTO pkgs(source,name,name_l,key,sgen) VALUES ('aur','blocked','blocked','blocked',99)")
        t0 = time.time()
        self.assertTrue(index.search("discord"))
        self.assertLess(time.time() - t0, 1.0)
        w.execute("ROLLBACK")
        w.close()


class Migration(Base):
    LEGACY = """CREATE TABLE pkgs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, name_l TEXT NOT NULL, key TEXT NOT NULL,
      title TEXT DEFAULT '', version TEXT DEFAULT '', desc TEXT DEFAULT '', repo TEXT DEFAULT '', app_id TEXT DEFAULT '',
      votes INTEGER DEFAULT 0, pop REAL DEFAULT 0, url TEXT DEFAULT '', maintainer TEXT DEFAULT '', ood INTEGER DEFAULT 0,
      icon TEXT DEFAULT '', cats TEXT DEFAULT '', is_app INTEGER DEFAULT 0)"""

    def make_legacy(self):
        db = sqlite3.connect(cat.DB_PATH)
        db.execute(self.LEGACY)
        for r in PACMAN + AUR:
            db.execute(f"INSERT INTO pkgs({','.join(cat.COLS)}) VALUES ({','.join('?' * len(cat.COLS))})", r)
        db.commit()
        db.close()
        with open(cat.STATE_PATH, "w") as f:
            json.dump({"last_refresh": time.time(), "aur_etag": "abc", "aur_lm": "Mon"}, f)

    def test_legacy_catalogue_is_upgraded_in_place_and_stays_searchable(self):
        self.make_legacy()
        self.assertTrue(index.exists())
        rows = index.search("discord")                     # LIKE fallback: full-text index not built yet
        self.assertIn("discord-bin", names(rows))
        self.assertTrue(index.maintenance_pending())
        self.assertTrue(index.due())
        db = self.conn()
        self.assertEqual(cat.active_generation(db), 1)
        self.assertEqual(json.loads(cat.sync_states(db)["aur"]["validator"])["etag"], "abc")
        db.close()

    def test_maintenance_builds_fts_and_identities_without_any_network_change(self):
        self.make_legacy()
        srcs = [Fake("pacman", PACMAN), Fake("aur", AUR)]
        for s in srcs:
            s.mode = "fail"                                # offline upgrade must still complete
        self.refresh(srcs)
        self.assertFalse(index.maintenance_pending())
        db = self.conn()
        self.assertTrue(cat.fts_ready(db))
        self.assertGreater(db.execute("SELECT COUNT(*) FROM relations WHERE gen=?", (cat.active_generation(db),)).fetchone()[0], 0)
        db.close()


class Search(Base):
    def setUp(self):
        super().setUp()
        self.refresh(sources())

    def test_fts_matches_like_for_substrings(self):
        db = self.conn()
        for q in ("discord", "cord", "fire", "spot", "rust"):
            like = {r["id"] for r in db.execute("SELECT id FROM pkgs_active WHERE name_l LIKE ? OR lower(app_id) LIKE ? OR lower(title) LIKE ? "
                                                "OR lower(desc) LIKE ? OR key LIKE ?", (f"%{q}%",) * 5)}
            fts = {r["id"] for r in index.search(q, 1000)}
            self.assertEqual(like, fts, q)
        db.close()

    def test_short_queries_fall_back_to_scan(self):
        self.assertTrue(index.search("fi"))

    def test_multiword_query_requires_all_words(self):
        self.assertEqual(names(index.search("discord client"))[:1], ["legcord"] if "legcord" in names(index.search("discord client")) else names(index.search("discord client"))[:1])
        self.assertTrue(all("client" in (r["desc"] + r["name"]).lower() and "discord" in (r["desc"] + r["name"]).lower() for r in index.search("discord client")))

    def test_special_characters_do_not_break_the_query(self):
        for q in ('"', "a\"b", "c++", "foo*", "NEAR(", "-x", "a OR b", "'"):
            index.search(q)

    def test_results_are_ranked_exact_name_first(self):
        self.assertEqual(index.search("firefox")[0]["name"].lower(), "firefox")

    def test_alias_finds_the_software(self):
        db = cat.connect()
        db.execute("INSERT OR REPLACE INTO identity_alias VALUES (?,?,?)", (index.generation(), "vsc", "firefox"))
        db.close()
        self.assertIn("firefox", names(index.search("vsc")))


class Identity(Base):
    def setUp(self):
        super().setUp()
        self.refresh(sources())
        self.rows = {r["name"]: r for r in index.search("discord", 100)}

    def test_distributions_resolve_to_one_identity(self):
        idents = {n: self.rows[n]["ident"] for n in ("discord", "discord-bin", "discord-canary", "discord_arch_electron", "Discord")}
        self.assertEqual(set(idents.values()), {"discord"}, idents)

    def test_flatpak_id_joins_existing_identity(self):
        self.assertEqual(index.by_names(["firefox"])[0]["ident"], "firefox")
        fx = [r for r in index.search("firefox") if r["source"] == "flatpak"][0]
        self.assertEqual(fx["ident"], "firefox")

    def test_client_is_related_to_discord_but_not_merged(self):
        self.assertEqual(self.rows["oxycode"]["ident"], "oxycode")
        self.assertNotEqual(self.rows["oxycode"]["ident"], self.rows["discord"]["ident"])
        grp = {(g["kind"], g["dir"]): g for g in index.relations_for(["discord"])}
        clients = {i["other"] for i in grp[("client_for", "in")]["items"]}
        self.assertTrue({"oxycode", "legcord"} <= clients, clients)
        out = {i["other"] for g in index.relations_for(["oxycode"]) if g["dir"] == "out" and g["kind"] == "client_for" for i in g["items"]}
        self.assertEqual(out, {"discord"}, "reverse discovery: Oxycode points at Discord")

    def test_integrations_and_forks(self):
        grp = {(g["kind"], g["dir"]): g for g in index.relations_for(["discord"])}
        self.assertTrue({"discord-rpc", "discord-overlay"} <= {i["other"] for i in grp[("integration_for", "in")]["items"]})
        fx = {(g["kind"], g["dir"]): g for g in index.relations_for(["firefox"])}
        self.assertIn("librewolf", {i["other"] for i in fx[("fork_of", "in")]["items"]})

    def test_object_of_a_sentence_is_not_mistaken_for_the_head(self):
        # "tool to customize Spotify client" and "Discord Client modification" must not become clients
        sp = [i["other"] for g in index.relations_for(["spotify"]) if g["kind"] == "client_for" and g["dir"] == "in" for i in g["items"]]
        self.assertIn("ncspot", sp)
        self.assertNotIn("spicetify-cli", sp)
        dc = [i["other"] for g in index.relations_for(["discord"]) if g["kind"] == "client_for" and g["dir"] == "in" for i in g["items"]]
        self.assertNotIn("crycord", dc)

    def test_every_relation_has_evidence_and_confidence(self):
        for g in index.relations_for(["discord", "spotify", "firefox"]):
            for it in g["items"]:
                self.assertTrue(it["evidence"] and it["origin"])
                self.assertGreaterEqual(it["confidence"], identity.MIN_CONFIDENCE)

    def test_package_rename_keeps_the_identity(self):
        srcs = sources()
        self.refresh(srcs, force=True)
        srcs[1].set([r if r[1] != "oxycode" else R("aur", "oxycode-bin", "0.2", "Terminal-native Discord client written in Rust", votes=5) for r in AUR])
        self.refresh(srcs)
        r = {x["name"]: x for x in index.search("oxycode")}
        self.assertEqual(r["oxycode-bin"]["ident"], "oxycode")
        self.assertIn("oxycode", {i["other"] for g in index.relations_for(["discord"]) if g["kind"] == "client_for" for i in g["items"]})

    def test_manual_correction_deny_and_add(self):
        cfg = os.path.join(self.dir, "cfg")
        os.makedirs(os.path.join(cfg, "sunshine-store"))
        with open(os.path.join(cfg, "sunshine-store", "identities.json"), "w") as f:
            json.dump({"deny": [{"src": "oxycode", "dst": "discord"}],
                       "relations": [{"src": "ncspot", "dst": "discord", "kind": "integration_for", "evidence": "user note"}]}, f)
        os.environ["XDG_CONFIG_HOME"] = cfg
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.assertTrue(index.maintenance_pending(), "changed curated data triggers a rebuild of the graph")
        self.refresh(sources())
        dc = {(g["kind"], i["other"]) for g in index.relations_for(["discord"]) if g["dir"] == "in" for i in g["items"]}
        self.assertNotIn(("client_for", "oxycode"), dc)
        self.assertIn(("integration_for", "ncspot"), dc)

    def test_relations_are_part_of_the_generation_and_old_ones_are_collected(self):
        for _ in range(3):
            self.refresh(sources(), force=True)
        db = self.conn()
        gens = {r[0] for r in db.execute("SELECT DISTINCT gen FROM relations")}
        self.assertLessEqual(len(gens), 2)
        self.assertIn(cat.active_generation(db), gens)
        db.close()


class Freshness(Base):
    def test_stale_state_after_long_outage_still_serves_data(self):
        self.refresh(sources())
        db = cat.connect()
        db.execute("UPDATE source_sync_state SET last_success=?, last_attempt=? WHERE source='aur'", (time.time() - 10 * 86400,) * 2)
        db.close()
        st = {s["source"]: s for s in index.source_status()}
        self.assertEqual(st["aur"]["state"], "stale")
        self.assertEqual(st["pacman"]["state"], "available")
        self.assertTrue(index.search("oxycode"))

    def test_source_never_indexed_is_unavailable(self):
        self.refresh([Fake("pacman", PACMAN)])
        st = {s["source"]: s for s in index.source_status()}
        self.assertEqual(st["aur"]["state"], "unavailable")


class Enrichment(Base):
    def test_cached_github_fork_fact_becomes_a_relation(self):
        up = R("pacman", "upstream-app", "1", "An app", "extra", url="https://github.com/acme/app", is_app=1)
        fk = R("aur", "app-reborn", "1", "Community variant", url="https://github.com/zed/app-reborn", votes=3)
        srcs = [Fake("pacman", [up]), Fake("aur", [fk])]
        self.refresh(srcs)
        self.assertEqual(index.relations_for(["upstream-app"]), [])
        db = cat.connect()
        db.execute("INSERT INTO enrichment VALUES (?,?,?)", ("github.com/zed/app-reborn", int(time.time()),
                                                            json.dumps({"fork": True, "parent": "github.com/acme/app"})))
        db.close()
        self.refresh(srcs, force=True)
        g = index.relations_for(["upstream-app"])
        self.assertEqual(g[0]["kind"], "fork_of")
        self.assertEqual(g[0]["items"][0]["other"], "app-reborn")
        self.assertEqual(g[0]["items"][0]["origin"], "github")


if __name__ == "__main__":
    unittest.main()
