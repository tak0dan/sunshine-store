"""Headless UI test: the store opens on the persisted catalogue, shows related software, and follows a background refresh live.
Skipped when PyQt6 is not installed.  Run: python3 -m unittest tests.test_ui   (uses QT_QPA_PLATFORM=offscreen)"""
import os
import sys
import tempfile
import threading
import unittest

# This suite is intentionally headless.  Respecting a desktop session's
# Wayland/XCB setting makes Qt load a display plugin during unittest discovery
# and can abort the entire test process on machines without a usable display.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_catalogue as T   # noqa: E402  (also puts src/ on sys.path and sets offline mode)

try:
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except ImportError:      # pragma: no cover
    HAVE_QT = False


def wait_for(cond, timeout=8.0):
    from PyQt6.QtTest import QTest
    for _ in range(int(timeout * 20)):
        if cond():
            return True
        QTest.qWait(50)
    return cond()


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class UiLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = T.fresh_env(self)
        cfg = tempfile.mkdtemp(prefix="sstore-cfg-")
        os.environ["XDG_CONFIG_HOME"] = cfg
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.srcs = T.sources()
        # enough unrelated "discord*" packages that the visible head of the results cannot swallow every relation
        self.filler = [T.R("aur", f"discord-stats-{i}", "1", "Chat statistics", votes=1000 + i) for i in range(8)]
        self.srcs[1].set(T.AUR + self.filler)
        T.index.refresh(lambda m: None, source_list=self.srcs, enrich=False)
        import app as A
        self.A = A
        self.store = A.Store()
        self.store.show()
        self.addCleanup(self.store.close)

    def labels(self, page=None):
        page = page or self.store.results
        return [w.text() for w in page.inner.findChildren(QLabel) if w.text()]

    def test_opens_on_cached_catalogue_without_any_refresh(self):
        self.assertTrue(T.index.exists())
        self.assertFalse(self.store._building, "no refresh was needed to show the catalogue")

    def test_search_shows_related_groups_and_survives_a_live_generation(self):
        st = self.store
        st.search.setText("discord")
        st.do_search()
        self.assertTrue(wait_for(lambda: st.results.inner.findChildren(self.A.Row)))
        heads = [t for t in self.labels() if t.startswith(("Clients", "Plugins"))]
        self.assertIn("Clients & front-ends for Discord", heads)
        gen = st._gen
        # a background refresh (another "process") publishes a new generation containing a brand-new client
        srcs = self.srcs
        srcs[1].set(T.AUR + self.filler + [T.R("aur", "newcord", "0.1", "Terminal Discord client written in Zig", votes=2)])
        th = threading.Thread(target=lambda: T.index.refresh(lambda m: None, source_list=srcs, enrich=False))
        th.start()
        self.assertTrue(wait_for(lambda: st._gen != gen and not th.is_alive(), 15))
        self.assertTrue(wait_for(lambda: any("newcord" in t for t in self.labels()), 8))
        self.assertEqual(st.search.text(), "discord", "the query is left alone")
        self.assertTrue(any("NEW" in t for t in self.labels()), "new discovery is badged")
        self.assertIn("Catalogue updated", st.status.text())

    def test_stale_source_is_called_out_in_results(self):
        db = T.cat.connect()
        db.execute("UPDATE source_sync_state SET last_success=last_success-864000, status='error', error='offline' WHERE source='flatpak'")
        db.close()
        st = self.store
        st.search.setText("firefox")
        st.do_search()
        self.assertTrue(wait_for(lambda: st.results.inner.findChildren(self.A.Row)))
        self.assertTrue(any("Flathub results are from" in t for t in self.labels()))


if __name__ == "__main__":
    unittest.main()
