"""Sunshine Store: a Microsoft-Store-style shell for pacman, the AUR and Flatpak."""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QObject, QProcess, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
                             QTabBar, QToolButton, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backend as bk  # noqa: E402

# Frame colours by source (requested hierarchy AUR -> Pacman -> Flatpak = Blue -> White -> Gray).
FRAME = {"aur": "#4c8dff", "pacman": "#f3eefc", "flatpak": "#8a869c"}
SRC_NAME = {"aur": "AUR", "pacman": "Pacman", "flatpak": "Flatpak"}
INSTALLED_GREEN = "#34d399"
ACCENT = "#ffc43d"

STYLE = f"""
* {{ font-family: 'Noto Sans'; }}
QWidget {{ background: #1e1b2e; color: #f3eefc; font-size: 13px; }}
QLineEdit {{ background: #2d2942; border: 1px solid #443f63; border-radius: 18px; padding: 9px 16px; font-size: 14px; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton, QToolButton {{ background: #2d2942; border: 1px solid #443f63; border-radius: 8px; padding: 8px 14px; }}
QPushButton:hover, QToolButton:hover {{ border-color: {ACCENT}; }}
QPushButton#nav {{ border: none; background: transparent; text-align: left; padding: 11px 14px; font-size: 14px; }}
QPushButton#nav:checked {{ background: #2d2942; color: {ACCENT}; }}
QPushButton#install, QToolButton#installarrow {{ background: {ACCENT}; color: #1e1b2e; border: none; font-weight: bold; }}
QPushButton#install {{ border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 10px 26px; font-size: 14px; }}
QToolButton#installarrow::menu-indicator {{ image: none; }}
QToolButton#installarrow {{ border-top-left-radius: 0; border-bottom-left-radius: 0; padding: 10px 8px; }}
QPushButton#remove {{ background: transparent; border-color: #c0566a; color: #ff9aaa; }}
QLabel#h1 {{ font-size: 24px; font-weight: bold; }}
QLabel#h2 {{ font-size: 17px; font-weight: bold; color: {ACCENT}; }}
QLabel#muted {{ color: #a69fc2; }}
QFrame#side {{ background: #17142a; }}
QScrollArea {{ border: none; }}
QTabBar::tab {{ background: #2d2942; padding: 7px 14px; border-radius: 8px; margin-right: 6px; }}
QTabBar::tab:selected {{ background: {ACCENT}; color: #1e1b2e; }}
QPlainTextEdit {{ background: #121022; border: 1px solid #443f63; border-radius: 8px; font-family: monospace; }}
QMenu {{ background: #2d2942; border: 1px solid #443f63; padding: 6px; }}
QMenu::item {{ padding: 8px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: #443f63; }}
"""


class Task(QThread):
    done = pyqtSignal(object)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:  # keep the UI alive on backend errors
            self.done.emit(e)


def letter_icon(text: str, color: str, size: int = 56) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color).darker(190))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size, size, 14, 14)
    p.setPen(QColor(color))
    f = QFont("Noto Sans", int(size * 0.38), QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, (text[:1] or "?").upper())
    p.end()
    return pm


def app_icon(pkg: bk.Pkg, size: int = 56) -> QPixmap:
    ic = QIcon.fromTheme(pkg.app_id or pkg.name)
    if not ic.isNull():
        return ic.pixmap(size, size)
    return letter_icon(pkg.name, FRAME[pkg.source] if pkg.source != "pacman" else ACCENT, size)


def frame_color(app: bk.App, pkg: bk.Pkg) -> str:
    return INSTALLED_GREEN if (pkg.installed or app.installed) else FRAME[pkg.source]


class Card(QFrame):
    clicked = pyqtSignal(object)

    def __init__(self, app: bk.App, order):
        super().__init__()
        self.app = app
        pkg = app.pick(order)
        col = frame_color(app, pkg)
        self.setObjectName("card")
        self.setStyleSheet(f"QFrame#card {{ border: 2px solid {col}; border-radius: 14px; background: #262238; }}"
                           f"QFrame#card:hover {{ background: #2f2a47; }} QLabel {{ background: transparent; }}")
        self.setFixedSize(250, 150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        ic = QLabel()
        ic.setPixmap(app_icon(pkg))
        ic.setFixedSize(56, 56)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col2 = QVBoxLayout()
        name = QLabel(pkg.name)
        name.setStyleSheet("font-weight: bold; font-size: 14px;")
        desc = QLabel(pkg.desc or "No description")
        desc.setWordWrap(True)
        desc.setObjectName("muted")
        desc.setMaximumHeight(52)
        chips = QLabel("  ".join(("● " if s == pkg.source else "○ ") + SRC_NAME[s] for s in app.sources))
        chips.setStyleSheet(f"color: {col}; font-size: 11px;")
        col2.addWidget(name)
        col2.addWidget(desc, 1)
        col2.addWidget(chips)
        lay.addLayout(col2, 1)

    def mousePressEvent(self, e):
        self.clicked.emit(self.app)


class Grid(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(28, 12, 28, 24)
        self.setWidget(self.inner)

    def clear(self):
        while self.vbox.count():
            it = self.vbox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._drop(it.layout())

    def _drop(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def heading(self, text, sub=""):
        h = QLabel(text)
        h.setObjectName("h2")
        self.vbox.addWidget(h)
        if sub:
            s = QLabel(sub)
            s.setObjectName("muted")
            self.vbox.addWidget(s)

    def cards(self, apps, order, on_click):
        g = QGridLayout()
        g.setSpacing(14)
        width = max(1, (self.viewport().width() - 56) // 264)
        for i, a in enumerate(apps):
            c = Card(a, order)
            c.clicked.connect(on_click)
            g.addWidget(c, i // width, i % width, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.vbox.addLayout(g)

    def message(self, text):
        m = QLabel(text)
        m.setObjectName("muted")
        m.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vbox.addWidget(m)
        self.vbox.addStretch(1)


class Detail(QWidget):
    back = pyqtSignal()
    open_pkg = pyqtSignal(object)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.app = None
        self.pkg = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 18)
        b = QPushButton("←  Back")
        b.clicked.connect(self.back)
        b.setFixedWidth(100)
        outer.addWidget(b)
        top = QHBoxLayout()
        self.icon = QLabel()
        self.icon.setFixedSize(96, 96)
        top.addWidget(self.icon)
        info = QVBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("h1")
        self.meta = QLabel()
        self.meta.setObjectName("muted")
        self.srcline = QLabel()
        info.addWidget(self.title)
        info.addWidget(self.meta)
        info.addWidget(self.srcline)
        top.addLayout(info, 1)
        outer.addLayout(top)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        outer.addWidget(self.desc)

        row = QHBoxLayout()
        row.setSpacing(0)
        self.btn = QPushButton("Install")
        self.btn.setObjectName("install")
        self.btn.clicked.connect(self.act)
        self.arrow = QToolButton()
        self.arrow.setObjectName("installarrow")
        self.arrow.setText("▾")
        self.arrow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)
        self.arrow.setMenu(self.menu)
        row.addWidget(self.btn)
        row.addWidget(self.arrow)
        row.addSpacing(12)
        self.rm = QPushButton("Uninstall")
        self.rm.setObjectName("remove")
        self.rm.clicked.connect(lambda: self.store.run(self.pkg, remove=True))
        row.addWidget(self.rm)
        row.addStretch(1)
        outer.addLayout(row)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumHeight(140)
        self.out.hide()
        outer.addWidget(self.out)

        self.rel_title = QLabel("Related packages")
        self.rel_title.setObjectName("h2")
        outer.addWidget(self.rel_title)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.currentChanged.connect(self.tab_changed)
        outer.addWidget(self.tabs)
        self.rel = Grid()
        outer.addWidget(self.rel, 1)
        self._variants = []

    def show_app(self, app: bk.App, pkg: bk.Pkg | None = None):
        self.app = app
        self.pkg = pkg or app.pick(self.store.order)
        p = self.pkg
        col = frame_color(app, p)
        self.icon.setPixmap(app_icon(p, 96))
        self.title.setText(p.name)
        bits = [p.version, f"from {SRC_NAME[p.source]}" + (f" ({p.repo})" if p.repo else "")]
        if p.votes:
            bits.append(f"★ {p.votes} votes")
        if p.out_of_date:
            bits.append("⚠ flagged out of date")
        self.meta.setText("  ·  ".join(b for b in bits if b))
        self.srcline.setText(f"<span style='color:{col}'>●</span> "
                             f"{'Installed' if p.installed else SRC_NAME[p.source] + ' package'}")
        self.desc.setText(p.desc or "")
        self.btn.setText("Reinstall" if p.installed else "Install")
        self.rm.setVisible(p.installed)
        self.menu.clear()
        self.menu.addAction("Copy install command", self.copy_cmd)
        others = [s for s in app.sources if s != p.source]
        if others:
            sub = self.menu.addMenu("Install from another source")
            for s in others:
                sub.addAction(f"{SRC_NAME[s]}  ·  {app.items[s].name} {app.items[s].version}",
                              lambda s=s: self.show_app(app, app.items[s]))
        self.menu.addAction("Show command for removing", lambda: self.copy_cmd(remove=True))
        self.out.hide()
        self.out.clear()
        self.load_related()

    def copy_cmd(self, remove=False):
        QGuiApplication.clipboard().setText(bk.command_for(self.pkg, remove))
        self.store.toast(f"Copied: {bk.command_for(self.pkg, remove)}")

    def act(self):
        self.out.show()
        self.store.run(self.pkg, log=self.out)

    def load_related(self):
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self.tabs.blockSignals(False)
        self._variants = []
        self.rel.clear()
        self.rel.message("Looking for related packages…")
        base = self.app.key
        self._t = Task(bk.related_aur, base)
        self._t.done.connect(self.related_ready)
        self._t.start()

    def related_ready(self, res):
        if isinstance(res, Exception):
            res = []
        groups = {}
        for p in list(self.app.variants) + [r for r in res]:
            groups.setdefault(p.name, p)
        pkgs = list(groups.values())
        self._variants = [("All", pkgs)]
        for label, test in (("Release", lambda n: not n.endswith(bk.VARIANT_SUFFIXES)),
                            ("-bin", lambda n: n.endswith("-bin")),
                            ("-git", lambda n: n.endswith("-git")),
                            ("Other", lambda n: n.endswith(bk.VARIANT_SUFFIXES) and not n.endswith(("-bin", "-git")))):
            sel = [p for p in pkgs if test(p.name)]
            if sel:
                self._variants.append((label, sel))
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for label, sel in self._variants:
            self.tabs.addTab(f"{label} ({len(sel)})")
        self.tabs.blockSignals(False)
        self.tab_changed(0)

    def tab_changed(self, i):
        self.rel.clear()
        if not self._variants or i < 0:
            self.rel.message("No related packages found.")
            return
        apps = []
        for p in self._variants[i][1]:
            a = bk.App(p.name)
            a.items["aur"] = p
            apps.append(a)
        if not apps:
            self.rel.message("No related packages found.")
            return
        self.rel.cards(apps, self.store.order, lambda a: self.show_app(a, a.items["aur"]))


class Store(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sunshine Store")
        self.resize(1180, 760)
        self.order = bk.DEFAULT_ORDER
        self.procs = []
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(200)
        sl = QVBoxLayout(side)
        logo = QLabel("☀  Sunshine Store")
        logo.setStyleSheet(f"font-size: 17px; font-weight: bold; color: {ACCENT}; padding: 14px 8px;")
        sl.addWidget(logo)
        self.nav = {}
        for key, label in (("home", "🏠  Home"), ("installed", "✔  Installed"), ("updates", "⬆  Updates")):
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self.go(k))
            sl.addWidget(b)
            self.nav[key] = b
        sl.addStretch(1)
        legend = QLabel("<b>Frames</b><br><span style='color:#4c8dff'>■</span> AUR<br>"
                        "<span style='color:#f3eefc'>■</span> Pacman<br>"
                        "<span style='color:#8a869c'>■</span> Flatpak<br>"
                        f"<span style='color:{INSTALLED_GREEN}'>■</span> Installed")
        legend.setObjectName("muted")
        legend.setStyleSheet("padding: 10px;")
        sl.addWidget(legend)
        root.addWidget(side)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search apps, games, tools…")
        self.search.returnPressed.connect(self.do_search)
        sw = QHBoxLayout()
        sw.setContentsMargins(28, 18, 28, 6)
        sw.addWidget(self.search)
        main.addLayout(sw)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(30, 0, 0, 0)
        main.addWidget(self.status)
        self.stack = QStackedWidget()
        self.list = Grid()
        self.detail = Detail(self)
        self.detail.back.connect(lambda: self.stack.setCurrentWidget(self.list))
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.detail)
        main.addWidget(self.stack, 1)
        root.addLayout(main, 1)
        self.go("home")

    # ---- helpers
    def toast(self, text):
        self.status.setText(text)

    def open_app(self, app):
        self.detail.show_app(app)
        self.stack.setCurrentWidget(self.detail)

    def go(self, key):
        for k, b in self.nav.items():
            b.setChecked(k == key)
        self.stack.setCurrentWidget(self.list)
        self.list.clear()
        if key == "home":
            self.show_home()
        elif key == "installed":
            self.list.heading("Installed", "Apps you added yourself, from every source")
            self.list.message("Loading…")
            self._t = Task(bk.list_installed)
            self._t.done.connect(self.installed_ready)
            self._t.start()
        else:
            self.list.heading("Updates")
            self.list.message("Use “Update System” from the menu: it updates pacman, AUR and Flatpak together, safely.")

    def installed_ready(self, pkgs):
        self.list.clear()
        self.list.heading("Installed", f"{len(pkgs)} apps and tools")
        apps = []
        for p in pkgs:
            a = bk.App(p.name)
            a.items[p.source] = p
            apps.append(a)
        self.list.cards(apps, self.order, self.open_app)
        self.list.vbox.addStretch(1)

    def show_home(self):
        self.list.heading("Welcome", "Search above, or pick something from the shelves below.")
        self.list.message("Loading suggestions…")
        names = [n for _, ns in bk.FEATURED for n in ns]
        self._t = Task(bk.resolve, names)
        self._t.done.connect(self.home_ready)
        self._t.start()

    def home_ready(self, apps):
        if isinstance(apps, Exception):
            apps = []
        if self.nav["home"].isChecked() is False or self.stack.currentWidget() is not self.list:
            return
        self.list.clear()
        self.list.heading("Welcome", "Search above, or pick something from the shelves below.")
        by = {a.key: a for a in apps}
        for title, ns in bk.FEATURED:
            shelf = [by[n] for n in ns if n in by]
            if shelf:
                self.list.heading(title)
                self.list.cards(shelf, self.order, self.open_app)
        self.list.vbox.addStretch(1)

    def do_search(self):
        q = self.search.text().strip()
        if not q:
            return
        self.stack.setCurrentWidget(self.list)
        self.list.clear()
        self.list.message(f"Searching for “{q}” in AUR, Pacman and Flatpak…")
        self._t = Task(bk.search_all, q)
        self._t.done.connect(lambda res: self.results(q, res))
        self._t.start()

    def results(self, q, res):
        self.list.clear()
        if isinstance(res, Exception) or not res:
            self.list.heading(f"Results for “{q}”")
            self.list.message("Nothing found. Check the spelling or your internet connection.")
            return
        self.list.heading(f"Results for “{q}”", f"{len(res)} apps")
        self.list.cards(res[:90], self.order, self.open_app)
        self.list.vbox.addStretch(1)

    # ---- install / remove
    def run(self, pkg, remove=False, log=None):
        argv = bk.argv_for(pkg, remove)
        log = log or self.detail.out
        log.show()
        log.appendPlainText("$ " + " ".join(argv))
        pr = QProcess(self)
        pr.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        pr.readyReadStandardOutput.connect(lambda: log.appendPlainText(bytes(pr.readAllStandardOutput()).decode(errors="replace").rstrip()))
        pr.finished.connect(lambda code, _: self.finished(pkg, remove, code, log))
        pr.start(argv[0], argv[1:])
        self.procs.append(pr)
        self.toast(("Removing " if remove else "Installing ") + pkg.name + "…")

    def finished(self, pkg, remove, code, log):
        if code == 0:
            pkg.installed = not remove
            log.appendPlainText("✔ Done")
            self.toast(("Removed " if remove else "Installed ") + pkg.name)
            if self.detail.pkg is pkg:
                self.detail.show_app(self.detail.app, pkg)
                self.detail.out.show()
        else:
            log.appendPlainText(f"✘ Failed (exit code {code}). You can copy the command from the arrow menu and run it in a terminal to see more.")
            self.toast("That did not work.")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = Store()
    w.show()
    if "--shot" in sys.argv:            # test helper: render a screenshot and quit
        from PyQt6.QtCore import QTimer
        out = sys.argv[sys.argv.index("--shot") + 1]
        q = sys.argv[sys.argv.index("--query") + 1] if "--query" in sys.argv else ""
        if q:
            w.search.setText(q)
            w.do_search()
        def grab():
            if "--open" in sys.argv:
                cards = w.list.findChildren(Card)
                if cards:
                    w.open_app(cards[0].app)
                    QTimer.singleShot(6000, lambda: (w.grab().save(out), app.quit()))
                    return
            w.grab().save(out)
            app.quit()
        QTimer.singleShot(int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 4000, grab)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
