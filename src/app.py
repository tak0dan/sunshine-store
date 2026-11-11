"""Sunshine Store: an app store for Arch (pacman, AUR, Flatpak, AppImage, Snap) in a Microsoft Store /
GNOME Software layout, dressed in the burgundy-and-gold look of the liqueur-calculator page."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import os
import pty
import re
import signal
import struct
import subprocess
import sys
import termios
import urllib.request

from PyQt6.QtCore import QSize, Qt, QThread, QTimer, pyqtSignal, QProcess
from PyQt6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QImageReader, QKeySequence, QLinearGradient, QPainter,
                         QPixmap, QRadialGradient, QPen, QShortcut, QTextCharFormat, QTextCursor)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backend as bk  # noqa: E402
import index  # noqa: E402

# ---- palette (from the liqueur-calculator page) -----------------------------------------------------------
VOID, VOID2 = "#0a0705", "#120b0a"
BURGUNDY, CHERRY, CHERRY_B = "#3d0f16", "#7a1128", "#a01a35"
GOLD, GOLD_B, GOLD_DIM = "#c9a15a", "#e8c579", "#8a7040"
PARCH, PARCH_DIM, MUTED = "#ece1cd", "#b8a996", "#8a7568"
LINE = "rgba(201,161,90,0.22)"
GREEN = "#7fae6a"
SERIF = '"Cormorant Garamond","Noto Serif","DejaVu Serif",Georgia,serif'
SANS = '"Inter","Noto Sans","DejaVu Sans",sans-serif'

# Frame colours by source (AUR blue, Pacman white, Flatpak gray; installed overrides with green).
FRAME = {"aur": "#4c8dff", "pacman": "#ece1cd", "flatpak": "#8a869c", "appimage": "#b48cf2", "snap": "#e8913a"}
SRC_NAME = {"aur": "AUR", "pacman": "Pacman", "flatpak": "Flatpak", "appimage": "AppImage", "snap": "Snap"}
SRC_KIND = {"aur": "AUR package", "pacman": "Arch package", "flatpak": "Flatpak app", "appimage": "AppImage", "snap": "Snap"}

ICON_CACHE = os.path.join(index.CACHE, "icons")
JXL_OK = b"jxl" in [bytes(f).lower() for f in QImageReader.supportedImageFormats()]

QSS = f"""
* {{ font-family: {SANS}; }}
QWidget {{ background: transparent; color: {PARCH}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QLabel#step {{ color: {GOLD_DIM}; font-size: 10px; letter-spacing: 2px; }}
QLabel#h1 {{ font-family: {SERIF}; font-style: italic; font-weight: 700; font-size: 34px; color: {PARCH}; }}
QLabel#h2 {{ font-family: {SERIF}; font-weight: 600; font-size: 24px; color: {GOLD_B}; }}
QLabel#h3 {{ font-family: {SERIF}; font-weight: 600; font-size: 17px; color: {PARCH}; }}
QLabel#dim {{ color: {PARCH_DIM}; }}
QLabel#muted {{ color: {MUTED}; }}
QLineEdit#search {{ background: rgba(0,0,0,0.30); border: 1px solid {LINE}; border-radius: 3px; padding: 11px 14px; font-size: 14px; }}
QLineEdit#search:focus {{ border: 1px solid {GOLD}; }}
QLineEdit#cmd {{ background: rgba(0,0,0,0.45); border: 1px solid {LINE}; border-radius: 3px; padding: 10px 12px;
                 font-family: "JetBrains Mono","DejaVu Sans Mono",monospace; color: {GOLD_B}; selection-background-color: {CHERRY}; }}
QPushButton {{ background: rgba(0,0,0,0.25); border: 1px solid {LINE}; border-radius: 3px; padding: 9px 16px; color: {PARCH_DIM}; }}
QPushButton:hover {{ border-color: {GOLD_DIM}; background: rgba(0,0,0,0.4); }}
QPushButton#pill {{ border-radius: 3px; padding: 8px 16px; }}
QPushButton#pill:checked {{ border-color: {GOLD}; color: {GOLD_B}; background: rgba(201,161,90,0.10); }}
QPushButton#nav {{ border: none; background: transparent; text-align: left; padding: 12px 16px; font-size: 14px; color: {PARCH_DIM}; }}
QPushButton#nav:hover {{ color: {PARCH}; background: rgba(201,161,90,0.06); }}
QPushButton#nav:checked {{ color: {GOLD_B}; background: rgba(201,161,90,0.10); border-left: 2px solid {GOLD}; }}
QPushButton#get {{ background: {CHERRY}; border: 1px solid {GOLD}; color: {PARCH}; font-weight: 600; padding: 11px 30px;
                   border-top-right-radius: 0; border-bottom-right-radius: 0; font-size: 14px; }}
QPushButton#get:hover {{ background: {CHERRY_B}; }}
QToolButton#getarrow {{ background: {CHERRY}; border: 1px solid {GOLD}; border-left: 1px solid {GOLD_DIM}; color: {PARCH};
                        padding: 11px 9px; border-top-left-radius: 0; border-bottom-left-radius: 0; border-top-right-radius: 3px; border-bottom-right-radius: 3px; }}
QToolButton#getarrow::menu-indicator {{ image: none; }}
QToolButton#getarrow:hover {{ background: {CHERRY_B}; }}
QPushButton#danger {{ border-color: {CHERRY_B}; color: #e88a98; }}
QPushButton#choice {{ text-align: left; padding: 16px 14px; font-size: 14px; border-radius: 2px; }}
QPushButton#choice:checked {{ border: 1px solid {GOLD}; color: {GOLD_B};
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(122,17,40,0.45), stop:1 rgba(0,0,0,0.35)); }}
QPushButton#primary {{ background: {CHERRY}; border: 1px solid {GOLD}; color: {PARCH}; font-weight: 600; padding: 12px 34px; }}
QPushButton#primary:hover {{ background: {CHERRY_B}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: rgba(201,161,90,0.25); border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QPlainTextEdit {{ background: rgba(0,0,0,0.45); border: 1px solid {LINE}; border-radius: 3px; font-family: "JetBrains Mono","DejaVu Sans Mono",monospace; }}
QMenu {{ background: {VOID2}; border: 1px solid {LINE}; padding: 6px; }}
QMenu::item {{ padding: 8px 22px; }}
QMenu::item:selected {{ background: {BURGUNDY}; color: {GOLD_B}; }}
QComboBox {{ background: rgba(0,0,0,0.3); border: 1px solid {LINE}; padding: 9px 12px; border-radius: 3px; }}
QComboBox QAbstractItemView {{ background: {VOID2}; border: 1px solid {LINE}; selection-background-color: {BURGUNDY}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 19px; height: 19px; border: 1px solid {GOLD_DIM}; background: rgba(0,0,0,0.3); }}
QCheckBox::indicator:checked {{ background: {CHERRY}; border-color: {GOLD}; }}
"""


# ---------------------------------------------------------------------------------------------- helpers
class Task(QThread):
    done = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, fn, *args, with_progress=False):
        super().__init__()
        self.fn, self.args, self.with_progress = fn, args, with_progress

    def run(self):
        try:
            res = self.fn(*self.args, self.progress.emit) if self.with_progress else self.fn(*self.args)
            self.done.emit(res)
        except Exception as e:                      # keep the UI alive on backend errors
            self.done.emit(e)


_tasks: set = set()


def spawn(fn, *args, on_done=None, on_progress=None, with_progress=False) -> Task:
    t = Task(fn, *args, with_progress=with_progress)
    _tasks.add(t)
    def fin(res):
        _tasks.discard(t)
        if on_done:
            on_done(res)
    t.done.connect(fin)
    if on_progress:
        t.progress.connect(on_progress)
    t.start()
    return t


def letter_pixmap(text: str, color: str, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor(CHERRY))
    g.setColorAt(1, QColor(BURGUNDY))
    p.setBrush(g)
    p.setPen(QPen(QColor(GOLD_DIM), 1))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 6, 6)
    p.setPen(QColor(GOLD_B))
    f = QFont("Noto Serif", int(size * 0.40))
    f.setItalic(True)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, (text[:1] or "?").upper())
    p.end()
    return pm


def _jxl_to_png(path: str) -> str:
    """Repo AppStream icons are JPEG XL; Qt may lack the plugin, so convert once with djxl and cache."""
    import shutil
    import subprocess
    os.makedirs(ICON_CACHE, exist_ok=True)
    out = os.path.join(ICON_CACHE, hashlib.sha1(path.encode()).hexdigest() + ".png")
    if not os.path.exists(out):
        exe = shutil.which("djxl")
        if not exe or subprocess.run([exe, path, out], capture_output=True, timeout=15).returncode != 0:
            raise RuntimeError("no jxl decoder")
    return out


def _fetch_icon(url: str) -> str:
    os.makedirs(ICON_CACHE, exist_ok=True)
    path = os.path.join(ICON_CACHE, hashlib.sha1(url.encode()).hexdigest() + ".png")
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": "sunshine-store"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return path


class IconLabel(QLabel):
    """Shows a package icon: file path, theme name or (downloaded and cached) URL; letter avatar as fallback."""

    def __init__(self, size: int):
        super().__init__()
        self.px = size
        self.setFixedSize(size, size)
        self.setScaledContents(False)

    def set_pkg(self, name: str, icon: str):
        self.setPixmap(letter_pixmap(name, GOLD, self.px))
        if not icon:
            return
        if icon.startswith("http"):
            spawn(_fetch_icon, icon, on_done=self._loaded)
            return
        pm = QPixmap()
        if os.path.isabs(icon):
            if icon.endswith(".jxl") and not JXL_OK:
                spawn(_jxl_to_png, icon, on_done=self._loaded)
                return
            pm = QPixmap(icon)
        else:
            ic = QIcon.fromTheme(icon)
            pm = ic.pixmap(self.px, self.px) if not ic.isNull() else QPixmap()
        self._apply(pm)

    def _loaded(self, path):
        if isinstance(path, Exception):
            return
        try:
            self._apply(QPixmap(path))
        except RuntimeError:                         # label already deleted
            pass

    def _apply(self, pm: QPixmap):
        if not pm.isNull():
            self.setPixmap(pm.scaled(self.px, self.px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class Panel(QFrame):
    """Glass panel with the gold hairline across the top."""

    def __init__(self, strong=False):
        super().__init__()
        self.strong = strong
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(28, 15, 15, 150 if not self.strong else 200))
        p.setPen(QPen(QColor(201, 161, 90, 56), 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0, QColor(201, 161, 90, 0))
        g.setColorAt(0.5, QColor(201, 161, 90, 140))
        g.setColorAt(1, QColor(201, 161, 90, 0))
        p.fillRect(0, 0, self.width(), 1, g)


def lbl(text="", name="", wrap=False, align=None) -> QLabel:
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    l.setWordWrap(wrap)
    if align:
        l.setAlignment(align)
    return l


def frame_color(app: bk.App, pkg: bk.Pkg) -> str:
    return GREEN if (pkg.installed or app.installed) else FRAME[pkg.source]


def elide(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------- cards
class Card(QFrame):
    clicked = pyqtSignal(object)

    def __init__(self, app: bk.App, order, parent=None):
        super().__init__(parent)
        self.app = app
        pkg = app.pick(order)
        col = frame_color(app, pkg)
        self.setObjectName("card")
        self.setStyleSheet(f"QFrame#card {{ border: 1px solid {col}; border-radius: 3px; background: rgba(28,15,15,150); }}"
                           f"QFrame#card:hover {{ background: rgba(61,15,22,150); }} QLabel {{ background: transparent; border: none; }}")
        self.setFixedHeight(124)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        ic = IconLabel(60)
        ic.set_pkg(pkg.label, app.icon or pkg.icon)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        c = QVBoxLayout()
        c.setSpacing(2)
        t = lbl(elide(pkg.label, 26), "h3")
        d = lbl(elide(pkg.desc or "No description", 78), "dim", wrap=True)
        d.setStyleSheet("font-size: 12px;")
        d.setMaximumHeight(36)
        row = QHBoxLayout()
        row.setSpacing(8)
        for s in app.sources[:4]:
            tag = lbl(("● " if s == pkg.source else "○ ") + SRC_NAME[s])
            tag.setStyleSheet(f"font-size: 10px; color: {FRAME[s] if s != 'pacman' else PARCH_DIM};")
            row.addWidget(tag)
        row.addStretch(1)
        if app.installed:
            ok = lbl("✔ Installed")
            ok.setStyleSheet(f"font-size: 10px; color: {GREEN};")
            row.addWidget(ok)
        c.addWidget(t)
        c.addWidget(d, 1)
        c.addLayout(row)
        lay.addLayout(c, 1)

    def mousePressEvent(self, e):
        self.clicked.emit(self.app)


class CardFlow(QWidget):
    """Equal columns that grow to fill the row at any window width."""
    GAP, MIN_W = 14, 330

    def __init__(self):
        super().__init__()
        self.cards = []

    def add(self, card):
        card.setParent(self)
        self.cards.append(card)
        card.show()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.relayout()

    def relayout(self):
        w = self.width()
        if w <= 0 or not self.cards:
            return
        cols = max(1, (w + self.GAP) // (self.MIN_W + self.GAP))
        cw = (w - (cols - 1) * self.GAP) // cols
        ch = self.cards[0].height()
        for i, c in enumerate(self.cards):
            r, col = divmod(i, cols)
            c.setGeometry(col * (cw + self.GAP), r * (ch + self.GAP), cw, ch)
        rows = (len(self.cards) + cols - 1) // cols
        self.setMinimumHeight(rows * (ch + self.GAP) - self.GAP)


class Row(QFrame):
    """Search result row (GNOME Software style)."""
    clicked = pyqtSignal(object)

    def __init__(self, app: bk.App, order):
        super().__init__()
        self.app = app
        pkg = app.pick(order)
        col = frame_color(app, pkg)
        self.setObjectName("row")
        self.setStyleSheet(f"QFrame#row {{ border-left: 3px solid {col}; border-bottom: 1px solid {LINE}; background: transparent; }}"
                           f"QFrame#row:hover {{ background: rgba(61,15,22,110); }} QLabel {{ background: transparent; border: none; }}")
        self.setFixedHeight(86)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(16)
        ic = IconLabel(56)
        ic.set_pkg(pkg.label, app.icon or pkg.icon)
        lay.addWidget(ic)
        c = QVBoxLayout()
        c.setSpacing(1)
        c.addWidget(lbl(pkg.label, "h3"))
        d = lbl(elide(pkg.desc or "No description", 150), "dim", wrap=True)
        d.setStyleSheet("font-size: 12px;")
        d.setMaximumHeight(34)
        c.addWidget(d)
        tags = "   ".join(f"<span style='color:{FRAME[s] if s != 'pacman' else PARCH_DIM}'>{'●' if s == pkg.source else '○'}</span> {SRC_NAME[s]}" for s in app.sources)
        t = lbl(tags, wrap=True)
        t.setStyleSheet(f"font-size: 10px; color: {MUTED};")
        c.addWidget(t)
        lay.addLayout(c, 1)
        b = QPushButton("Installed ✔" if app.installed else "Get")
        b.setObjectName("pill")
        b.setFixedWidth(110)
        b.setStyleSheet(f"color: {GREEN}; border-color: {GREEN};" if app.installed else f"color: {GOLD_B}; border-color: {GOLD_DIM};")
        b.clicked.connect(lambda: self.clicked.emit(self.app))
        lay.addWidget(b)

    def mousePressEvent(self, e):
        self.clicked.emit(self.app)


class Page(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.v = QVBoxLayout(self.inner)
        self.v.setContentsMargins(32, 8, 32, 32)
        self.v.setSpacing(10)
        self.setWidget(self.inner)

    def clear(self):
        while self.v.count():
            it = self.v.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._drop(it.layout())

    def _drop(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._drop(it.layout())

    def heading(self, text, sub="", step=""):
        if step:
            self.v.addWidget(lbl(step.upper(), "step"))
        self.v.addWidget(lbl(text, "h2"))
        if sub:
            self.v.addWidget(lbl(sub, "dim", wrap=True))

    def cards(self, apps, order, on_click):
        flow = CardFlow()
        for a in apps:
            c = Card(a, order, flow)
            c.clicked.connect(on_click)
            flow.add(c)
        self.v.addWidget(flow)

    def message(self, text):
        m = lbl(text, "muted", wrap=True, align=Qt.AlignmentFlag.AlignCenter)
        self.v.addSpacing(30)
        self.v.addWidget(m)

    def end(self):
        self.v.addStretch(1)


# ---------------------------------------------------------------------------------------------- pages
# ---------------------------------------------------------------------------------------------- terminal
ANSI_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
ANSI_COLORS = {30: "#555555", 31: "#e06c75", 32: "#7fae6a", 33: "#e5c07b", 34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#d8cdb8",
               90: "#7a6f63", 91: "#ff8a94", 92: "#9fd18a", 93: "#f0d594", 94: "#8ec5ff", 95: "#dba0ee", 96: "#7fd3df", 97: "#f5ecda"}


class PtyReader(QThread):
    data = pyqtSignal(bytes)
    ended = pyqtSignal(int)

    def __init__(self, argv):
        super().__init__()
        self.argv = argv
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", LANG=os.environ.get("LANG", "C.UTF-8"))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True, env=env)
        os.close(slave)

    def run(self):
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.data.emit(chunk)
        code = self.proc.wait()
        try:
            os.close(self.master)
        except OSError:
            pass
        self.ended.emit(code)

    def send(self, text: str):
        try:
            os.write(self.master, text.encode())
        except OSError:
            pass

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
            QTimer.singleShot(2500, lambda: self.proc.poll() is None and os.killpg(self.proc.pid, signal.SIGTERM))
        except (OSError, ProcessLookupError):
            pass


class TerminalPanel(Panel):
    """Bottom terminal linked to the store's actions: installs and removals run (and print everything) here."""
    finished = pyqtSignal(int)

    def __init__(self):
        super().__init__(strong=True)
        self.setFixedHeight(330)
        self.reader = None
        self._cb = None
        self._fmt = QTextCharFormat()
        self._reset_fmt()
        self._line_start = 0
        self._cr = False
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 8, 14, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(lbl("TERMINAL", "step"))
        self.title = lbl("", "muted")
        head.addWidget(self.title, 1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("pill")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        clr = QPushButton("Clear")
        clr.setObjectName("pill")
        clr.clicked.connect(lambda: self.view.clear())
        head.addWidget(self.stop_btn)
        head.addWidget(clr)
        v.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        v.addWidget(self.view, 1)
        self.input = QLineEdit()
        self.input.setObjectName("cmd")
        self.input.setPlaceholderText("Type here to answer a prompt, or run any command…")
        self.input.returnPressed.connect(self.submit)
        v.addWidget(self.input)
        self.say("Sunshine Store terminal. Installs and removals show up here.\n", GOLD_DIM)

    # ---- output
    def _reset_fmt(self):
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(QColor("#d8cdb8"))

    def say(self, text: str, color: str = ""):
        old = QTextCharFormat(self._fmt)
        if color:
            self._fmt.setForeground(QColor(color))
        self.feed(text)
        self._fmt = old

    def feed(self, text: str):
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        pos = 0
        for m in ANSI_RE.finditer(text):
            self._emit_plain(cur, text[pos:m.start()])
            pos = m.end()
            if m.group(2) == "m":
                self._sgr(m.group(1))
            elif m.group(2) in ("K", "J") or (m.group(2) == "G"):
                self._clear_line(cur)
        self._emit_plain(cur, text[pos:])
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    def _clear_line(self, cur):
        cur.setPosition(self._line_start)
        cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()

    def _emit_plain(self, cur, s: str):
        buf = ""
        def flush():
            nonlocal buf
            if buf:
                cur.insertText(buf, self._fmt)
                buf = ""
        for ch in s:
            if ch == "\r":
                flush()
                self._cr = True                   # cursor returns to column 0; text that follows overwrites the line
            elif ch == "\n":
                flush()
                self._cr = False
                cur.insertText("\n", self._fmt)
                self._line_start = cur.position()
            elif ch == "\x08":
                flush()
                cur.deletePreviousChar()
            elif ch >= " " or ch == "\t":
                if self._cr:
                    flush()
                    self._clear_line(cur)
                    self._cr = False
                buf += ch
        flush()

    def _sgr(self, params: str):
        codes = [int(x) for x in params.split(";") if x.isdigit()] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self._reset_fmt()
            elif c == 1:
                self._fmt.setFontWeight(QFont.Weight.Bold)
            elif c == 22:
                self._fmt.setFontWeight(QFont.Weight.Normal)
            elif c in ANSI_COLORS:
                self._fmt.setForeground(QColor(ANSI_COLORS[c]))
            elif c == 39:
                self._fmt.setForeground(QColor("#d8cdb8"))
            elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                if c == 38:
                    n = codes[i + 2]
                    self._fmt.setForeground(QColor(self._xterm(n)))
                i += 2
            i += 1

    @staticmethod
    def _xterm(n: int) -> str:
        if n < 16:
            return ANSI_COLORS.get(30 + n if n < 8 else 82 + n, "#d8cdb8")
        if n >= 232:
            g = 8 + (n - 232) * 10
            return f"#{g:02x}{g:02x}{g:02x}"
        n -= 16
        lv = lambda x: 0 if x == 0 else 55 + 40 * x  # noqa: E731
        return f"#{lv(n // 36):02x}{lv(n // 6 % 6):02x}{lv(n % 6):02x}"

    # ---- running things
    @property
    def busy(self) -> bool:
        return self.reader is not None

    def run(self, argv: list, title: str = "", on_finished=None) -> bool:
        if self.busy:
            self.say("\nAnother task is still running. Wait for it or press Stop.\n", "#e5c07b")
            return False
        self._cb = on_finished
        self.title.setText(title)
        self.say("\n$ " + " ".join(argv) + "\n", GOLD_B)
        try:
            self.reader = PtyReader(argv)
        except OSError as e:
            self.say(f"Could not start: {e}\n", "#e06c75")
            self.reader = None
            return False
        self.reader.data.connect(lambda b: self.feed(b.decode(errors="replace")))
        self.reader.ended.connect(self._ended)
        self.reader.start()
        self.stop_btn.setEnabled(True)
        return True

    def _ended(self, code: int):
        r, self.reader = self.reader, None
        self.stop_btn.setEnabled(False)
        self.say(("\n✔ Finished" if code == 0 else f"\n✘ Stopped with exit code {code}") + "\n", GREEN if code == 0 else "#e06c75")
        if r:
            r.deleteLater()
        cb, self._cb = self._cb, None
        self.title.setText("")
        if cb:
            cb(code)
        self.finished.emit(code)

    def submit(self):
        text = self.input.text()
        self.input.clear()
        if self.busy:
            self.reader.send(text + "\n")             # the pty echoes it back itself
        elif text.strip():
            shell = os.environ.get("SHELL", "/bin/bash")
            self.run([shell, "-c", text], "your command")

    def stop(self):
        if self.reader:
            self.say("\n^C\n", "#e5c07b")
            self.reader.stop()


class Onboarding(QWidget):
    done = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.addStretch(1)
        panel = Panel(strong=True)
        panel.setMaximumWidth(860)
        v = QVBoxLayout(panel)
        v.setContentsMargins(34, 30, 34, 30)
        v.setSpacing(12)
        v.addWidget(lbl("WELCOME", "step"))
        v.addWidget(lbl("What are you into?", "h1"))
        v.addWidget(lbl("Pick anything that sounds like you. The store will show apps for it first. You can change this any time in Settings.", "dim", wrap=True))
        v.addSpacing(8)
        self.choices = {}
        grid = QWidget()
        gl = QHBoxLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        cols = [QVBoxLayout(), QVBoxLayout(), QVBoxLayout(), QVBoxLayout()]
        for c in cols:
            gl.addLayout(c)
        for i, (iid, emoji, label, _, _) in enumerate(bk.INTERESTS):
            b = QPushButton(f"{emoji}\n{label}".replace("&", "&&"))
            b.setObjectName("choice")
            b.setCheckable(True)
            b.setMinimumHeight(84)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.choices[iid] = b
            cols[i % 4].addWidget(b)
        v.addWidget(grid)
        self.status = lbl("", "muted")
        v.addWidget(self.status)
        row = QHBoxLayout()
        skip = QPushButton("Skip for now")
        skip.clicked.connect(lambda: self.done.emit([]))
        go = QPushButton("Continue")
        go.setObjectName("primary")
        go.clicked.connect(lambda: self.done.emit([k for k, b in self.choices.items() if b.isChecked()]))
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(go)
        v.addLayout(row)
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(panel, 10)
        hb.addStretch(1)
        outer.addLayout(hb)
        outer.addStretch(2)

    def preset(self, ids):
        for k, b in self.choices.items():
            b.setChecked(k in ids)


class Hero(Panel):
    opened = pyqtSignal(object)

    def __init__(self, app: bk.App, order):
        super().__init__(strong=True)
        self.app = app
        pkg = app.pick(order)
        self.setFixedHeight(190)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(34, 24, 34, 24)
        c = QVBoxLayout()
        c.addWidget(lbl("FEATURED TODAY", "step"))
        t = lbl(pkg.label, "h1")
        c.addWidget(t)
        c.addWidget(lbl(elide(pkg.desc, 140), "dim", wrap=True))
        c.addStretch(1)
        b = QPushButton("View")
        b.setObjectName("primary")
        b.setFixedWidth(150)
        b.clicked.connect(lambda: self.opened.emit(app))
        c.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        h.addLayout(c, 1)
        ic = IconLabel(120)
        ic.set_pkg(pkg.label, app.icon or pkg.icon)
        h.addWidget(ic)

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QColor(122, 17, 40, 190))
        g.setColorAt(1, QColor(18, 11, 10, 230))
        p.fillRect(self.rect(), g)
        p.setPen(QPen(QColor(201, 161, 90, 90), 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)

    def mousePressEvent(self, e):
        self.opened.emit(self.app)


class Detail(QWidget):
    back = pyqtSignal()

    def __init__(self, store):
        super().__init__()
        self.store, self.app, self.pkg = store, None, None
        self._removal = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 14, 32, 14)
        outer.setSpacing(12)
        b = QPushButton("←  Back")
        b.setFixedWidth(100)
        b.clicked.connect(self.back)
        outer.addWidget(b)

        head = Panel(strong=True)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(26, 22, 26, 22)
        hl.setSpacing(22)
        self.icon = IconLabel(112)
        hl.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        c = QVBoxLayout()
        c.setSpacing(4)
        self.kind = lbl("", "step")
        self.title = lbl("", "h1")
        self.meta = lbl("", "dim")
        c.addWidget(self.kind)
        c.addWidget(self.title)
        c.addWidget(self.meta)
        self.pills = QHBoxLayout()
        self.pills.setSpacing(8)
        c.addSpacing(6)
        c.addWidget(lbl("INSTALL FROM", "step"))
        c.addLayout(self.pills)
        c.addSpacing(8)
        act = QHBoxLayout()
        act.setSpacing(0)
        self.btn = QPushButton("Get")
        self.btn.setObjectName("get")
        self.btn.clicked.connect(self.act)
        self.arrow = QToolButton()
        self.arrow.setObjectName("getarrow")
        self.arrow.setText("▾")
        self.arrow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)
        self.arrow.setMenu(self.menu)
        act.addWidget(self.btn)
        act.addWidget(self.arrow)
        act.addSpacing(12)
        self.rm = QPushButton("Uninstall")
        self.rm.setObjectName("danger")
        self.rm.clicked.connect(lambda: self.store.run(self.pkg, remove=True))
        act.addWidget(self.rm)
        act.addStretch(1)
        c.addLayout(act)
        hl.addLayout(c, 1)
        outer.addWidget(head)

        cmdp = Panel()
        cl = QVBoxLayout(cmdp)
        cl.setContentsMargins(20, 14, 20, 14)
        self.cmd_label = lbl("INSTALL COMMAND", "step")
        cl.addWidget(self.cmd_label)
        r = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setObjectName("cmd")
        self.cmd.setReadOnly(True)
        copy = QPushButton("Copy")
        copy.setObjectName("pill")
        copy.clicked.connect(self.copy)
        r.addWidget(self.cmd, 1)
        r.addWidget(copy)
        cl.addLayout(r)
        outer.addWidget(cmdp)

        tabs = QHBoxLayout()
        self.tab_btns = []
        for i, name in enumerate(("Overview", "Related versions", "Details")):
            t = QPushButton(name)
            t.setObjectName("pill")
            t.setCheckable(True)
            t.clicked.connect(lambda _, i=i: self.tab(i))
            tabs.addWidget(t)
            self.tab_btns.append(t)
        tabs.addStretch(1)
        outer.addLayout(tabs)
        self.body = Page()
        outer.addWidget(self.body, 1)
        self._idx = 0

    # ---- content
    def show_app(self, app: bk.App, pkg: bk.Pkg | None = None):
        self.app = app
        self.pkg = pkg or app.pick(self.store.order)
        self._removal = False
        p = self.pkg
        self.icon.set_pkg(p.label, p.icon or app.icon)
        self.kind.setText(SRC_KIND[p.source].upper() + ("  ·  INSTALLED" if p.installed else ""))
        self.title.setText(p.label)
        self.fill_meta()
        while self.pills.count():
            w = self.pills.takeAt(0).widget()
            if w:
                w.deleteLater()
        for s in app.sources:
            pb = QPushButton(SRC_NAME[s] + ("  ✔" if app.items[s].installed else ""))
            pb.setObjectName("pill")
            pb.setCheckable(True)
            pb.setChecked(s == p.source)
            col = GREEN if app.items[s].installed else FRAME[s]
            pb.setStyleSheet(f"QPushButton#pill {{ border-color: {col}; }} QPushButton#pill:checked {{ background: rgba(255,255,255,0.07); color: {col}; border-width: 2px; }}")
            pb.clicked.connect(lambda _, s=s: self.show_app(app, app.items[s]))
            self.pills.addWidget(pb)
        self.pills.addStretch(1)
        self.btn.setText("Reinstall" if p.installed else "Get")
        self.rm.setVisible(p.installed)
        self.menu.clear()
        others = [s for s in app.sources if s != p.source]
        if others:
            sub = self.menu.addMenu("Install from another source")
            for s in others:
                sub.addAction(f"{SRC_NAME[s]}  ·  {app.items[s].name} {app.items[s].version}", lambda s=s: self.show_app(app, app.items[s]))
        self.menu.addAction("Show the removal command", self.show_removal)
        self.update_cmd()
        self.tab(0)
        if p.source == "aur":                        # catalog is a few hours old at most: freshen this one package
            spawn(bk.live_aur, p.name, on_done=lambda r, p=p: self.live(p, r))

    def fill_meta(self):
        p = self.pkg
        bits = [p.version, p.maintainer and f"by {p.maintainer}"]
        if p.votes:
            bits.append(f"★ {p.votes} votes")
        if p.out_of_date:
            bits.append("⚠ flagged out of date")
        self.meta.setText("  ·  ".join(b for b in bits if b))

    def live(self, p, r):
        if isinstance(r, Exception) or not r or self.pkg is not p:
            return
        for k, v in r.items():
            setattr(p, k, v)
        self.fill_meta()

    def update_cmd(self):
        self.cmd_label.setText("REMOVAL COMMAND" if self._removal else "INSTALL COMMAND")
        self.cmd.setText(bk.command_for(self.pkg, self._removal))

    def show_removal(self):
        self._removal = not self._removal
        self.update_cmd()

    def copy(self):
        QGuiApplication.clipboard().setText(self.cmd.text())
        self.store.toast("Command copied. Paste it into a terminal.")

    def act(self):
        self.store.run(self.pkg)

    def tab(self, i):
        self._idx = i
        for j, t in enumerate(self.tab_btns):
            t.setChecked(j == i)
        self.body.clear()
        p, a = self.pkg, self.app
        if i == 0:
            self.body.v.addWidget(lbl(p.desc or "No description available.", "dim", wrap=True))
            if p.cats:
                self.body.v.addWidget(lbl("CATEGORIES", "step"))
                self.body.v.addWidget(lbl(p.cats.replace(",", "  ·  "), "dim"))
            self.body.v.addWidget(lbl("WHERE ELSE YOU CAN GET IT", "step"))
            self.body.v.addWidget(lbl("  ·  ".join(SRC_NAME[s] + (" ✔" if a.items[s].installed else "") for s in a.sources), "dim"))
        elif i == 1:
            self.body.v.addWidget(lbl("Release, -bin and -git builds of this app in the AUR.", "dim"))
            self.body.message("Looking…")
            spawn(bk.related, a.key, on_done=self.related_ready)
        else:
            rows = [("Package", p.name), ("Source", SRC_NAME[p.source] + (f" ({p.repo})" if p.repo else "")), ("Version", p.version),
                    ("Maintainer", p.maintainer), ("Website", p.url), ("Votes", str(p.votes) if p.votes else ""),
                    ("Popularity", f"{p.popularity:.2f}" if p.popularity else ""), ("Identifier", p.app_id)]
            for k, v in rows:
                if v:
                    r = QHBoxLayout()
                    kl = lbl(k.upper(), "step")
                    kl.setFixedWidth(120)
                    r.addWidget(kl)
                    vl = lbl(v, "dim", wrap=True)
                    vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    r.addWidget(vl, 1)
                    self.body.v.addLayout(r)
        self.body.end()

    def related_ready(self, res):
        if self._idx != 1:
            return
        self.body.clear()
        self.body.v.addWidget(lbl("Release, -bin and -git builds of this app in the AUR.", "dim"))
        if isinstance(res, Exception) or not res:
            self.body.message("No related versions found.")
            return
        apps = []
        for p in res:
            a = bk.App(p.name)
            a.items["aur"] = p
            apps.append(a)
        self.body.cards(apps, self.store.order, lambda a: self.show_app(a, a.items["aur"]))
        self.body.end()


class Settings(QWidget):
    def __init__(self, store):
        super().__init__()
        self.store = store
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 8, 32, 24)
        outer.setSpacing(14)
        outer.addWidget(lbl("PREFERENCES", "step"))
        outer.addWidget(lbl("Settings", "h2"))

        p1 = Panel()
        v = QVBoxLayout(p1)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("Fast search catalog", "h3"))
        v.addWidget(lbl("The store keeps a local copy of every app list so searches are instant. A small background service keeps it fresh: "
                        "every 5 hours of active use, or every 36 hours in total, whichever comes first.", "dim", wrap=True))
        self.svc = QCheckBox("Keep the catalog fresh in the background")
        self.svc.setChecked(bk.service_enabled())
        self.svc.toggled.connect(self.toggle_service)
        v.addWidget(self.svc)
        self.summary = lbl(bk.catalog_summary(), "muted")
        v.addWidget(self.summary)
        r = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh now")
        self.refresh_btn.clicked.connect(store.refresh_catalog)
        self.refresh_msg = lbl("", "muted")
        r.addWidget(self.refresh_btn)
        r.addWidget(self.refresh_msg, 1)
        v.addLayout(r)
        outer.addWidget(p1)

        p2 = Panel()
        v = QVBoxLayout(p2)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("Where apps come from", "h3"))
        v.addWidget(lbl("When the same app is available from several places, this is the order the store picks from.", "dim", wrap=True))
        self.order = QComboBox()
        for k, label in bk.ORDER_LABELS.items():
            self.order.addItem(label, k)
        self.order.setCurrentIndex(max(0, list(bk.ORDER_LABELS).index(store.cfg.get("order", "aur-first"))))
        self.order.currentIndexChanged.connect(self.set_order)
        v.addWidget(self.order)
        outer.addWidget(p2)

        p2b = Panel()
        v = QVBoxLayout(p2b)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("Terminal", "h3"))
        v.addWidget(lbl("Installs and removals run in the terminal at the bottom of the window, so you can see exactly what happens.", "dim", wrap=True))
        self.auto_term = QCheckBox("Open the terminal automatically when something installs")
        self.auto_term.setChecked(store.cfg.get("auto_terminal", True))
        self.auto_term.toggled.connect(lambda on: (store.cfg.__setitem__("auto_terminal", on), bk.save_config(store.cfg)))
        v.addWidget(self.auto_term)
        outer.addWidget(p2b)

        p3 = Panel()
        v = QVBoxLayout(p3)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("Your interests", "h3"))
        v.addWidget(lbl("Choose again what the Home page highlights.", "dim"))
        b = QPushButton("Change interests…")
        b.setFixedWidth(200)
        b.clicked.connect(store.show_onboarding)
        v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(p3)
        outer.addStretch(1)

    def toggle_service(self, on):
        if not bk.set_service(on):
            self.svc.blockSignals(True)
            self.svc.setChecked(not on)
            self.svc.blockSignals(False)
            self.store.toast("Could not change the background service here (is the store installed system-wide?).")
        else:
            self.store.toast("Background catalog updates " + ("enabled." if on else "disabled."))

    def set_order(self, i):
        key = self.order.itemData(i)
        self.store.cfg["order"] = key
        bk.save_config(self.store.cfg)
        self.store.order = bk.ORDERS[key]
        self.store.toast("Source order saved.")

    def refresh_summary(self):
        self.summary.setText(bk.catalog_summary())


# ---------------------------------------------------------------------------------------------- main window
class Store(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sunshine Store")
        self.resize(1240, 800)
        self.cfg = bk.load_config()
        self.order = bk.ORDERS.get(self.cfg.get("order", "aur-first"), bk.DEFAULT_ORDER)
        self.procs, self._results, self._filter, self._query = [], [], None, ""
        self._building = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QFrame()
        side.setFixedWidth(210)
        side.setStyleSheet("QFrame { background: rgba(0,0,0,0.35); border-right: 1px solid rgba(201,161,90,0.18); }")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 18, 0, 14)
        logo = lbl("☀  Sunshine")
        logo.setStyleSheet(f"font-family: {SERIF}; font-style: italic; font-weight: 700; font-size: 24px; color: {GOLD_B}; padding: 4px 18px 0; border: none;")
        sub = lbl("S T O R E")
        sub.setStyleSheet(f"font-size: 9px; letter-spacing: 4px; color: {GOLD_DIM}; padding: 0 20px 18px; border: none;")
        sl.addWidget(logo)
        sl.addWidget(sub)
        self.nav = {}
        for key, label in (("home", "⌂   Home"), ("installed", "✔   Installed"), ("updates", "⬆   Updates"), ("settings", "⚙   Settings")):
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self.go(k))
            sl.addWidget(b)
            self.nav[key] = b
        sl.addStretch(1)
        leg = lbl("<span style='color:#4c8dff'>■</span> AUR &nbsp; <span style='color:#ece1cd'>■</span> Pacman<br>"
                  "<span style='color:#8a869c'>■</span> Flatpak &nbsp; <span style='color:#b48cf2'>■</span> AppImage<br>"
                  "<span style='color:#e8913a'>■</span> Snap &nbsp; <span style='color:#7fae6a'>■</span> Installed")
        leg.setStyleSheet(f"font-size: 10px; color: {MUTED}; padding: 10px 18px; border: none;")
        sl.addWidget(leg)
        root.addWidget(side)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(32, 18, 32, 6)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search apps, games and tools in every source…")
        self.search.textChanged.connect(self.on_text)
        self.search.returnPressed.connect(self.do_search)
        top.addWidget(self.search)
        main.addLayout(top)
        self.pillbar = QHBoxLayout()
        self.pillbar.setContentsMargins(32, 4, 32, 4)
        self.pill_w = QWidget()
        self.pill_w.setLayout(self.pillbar)
        self.pill_w.hide()
        main.addWidget(self.pill_w)
        self.status = lbl("", "muted")
        self.status.setContentsMargins(34, 0, 0, 0)
        main.addWidget(self.status)

        self.stack = QStackedWidget()
        self.onboarding = Onboarding()
        self.onboarding.done.connect(self.finish_onboarding)
        self.home = Page()
        self.results = Page()
        self.installed_page = Page()
        self.updates_page = Page()
        self.settings = Settings(self)
        self.detail = Detail(self)
        self.detail.back.connect(self.go_back)
        for w in (self.onboarding, self.home, self.results, self.installed_page, self.updates_page, self.settings, self.detail):
            self.stack.addWidget(w)
        main.addWidget(self.stack, 1)
        self.term = TerminalPanel()
        self.term.hide()
        main.addWidget(self.term)
        bar = QHBoxLayout()
        bar.setContentsMargins(32, 4, 32, 8)
        self.term_btn = QPushButton("⌨  Terminal")
        self.term_btn.setObjectName("pill")
        self.term_btn.setCheckable(True)
        self.term_btn.setToolTip("Show or hide the terminal (Ctrl+`)")
        self.term_btn.toggled.connect(self.set_terminal)
        bar.addStretch(1)
        bar.addWidget(self.term_btn)
        main.addLayout(bar)
        QShortcut(QKeySequence("Ctrl+`"), self, activated=lambda: self.term_btn.toggle())
        root.addLayout(main, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self.do_search)
        self._prev = self.home
        self.show_all = False

        if not index.exists():
            self.refresh_catalog(first=True)
        elif index.due() and not bk.service_enabled():
            QTimer.singleShot(4000, lambda: self.refresh_catalog(quiet=True))
        if not self.cfg.get("onboarded"):
            self.show_onboarding()
        else:
            self.go("home")

    # ---- painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(VOID))
        w, h = self.width(), self.height()
        g = QRadialGradient(w * 0.15, -h * 0.05, max(w, h) * 0.7)
        g.setColorAt(0, QColor(122, 17, 40, 70))
        g.setColorAt(1, QColor(122, 17, 40, 0))
        p.fillRect(self.rect(), g)
        g2 = QRadialGradient(w, h * 0.2, max(w, h) * 0.5)
        g2.setColorAt(0, QColor(201, 161, 90, 22))
        g2.setColorAt(1, QColor(201, 161, 90, 0))
        p.fillRect(self.rect(), g2)

    # ---- helpers
    def set_terminal(self, on):
        self.term.setVisible(on)
        if on:
            self.term.input.setFocus()

    def toast(self, text):
        self.status.setText(text)

    def open_app(self, app):
        self._prev = self.stack.currentWidget()
        self.detail.show_app(app)
        self.stack.setCurrentWidget(self.detail)

    def go_back(self):
        self.stack.setCurrentWidget(self._prev if self._prev is not self.detail else self.home)

    def check_nav(self, key):
        for k, b in self.nav.items():
            b.setChecked(k == key)

    # ---- catalog
    def refresh_catalog(self, first=False, quiet=False):
        if self._building:
            return
        self._building = True
        if first:
            self.toast("Preparing the app catalog for the first time (about 10 seconds)…")
        self.settings.refresh_btn.setEnabled(False)
        def prog(m):
            self.settings.refresh_msg.setText(m)
            self.term.say("catalog: " + m + "\n", GOLD_DIM)
            if first:
                self.toast(m)
        def done(res):
            self._building = False
            self.settings.refresh_btn.setEnabled(True)
            self.settings.refresh_summary()
            ok = not isinstance(res, Exception)
            self.settings.refresh_msg.setText("Catalog is up to date." if ok else f"Could not refresh: {res}")
            if first:
                self.toast("Catalog ready." if ok else f"Could not build the catalog: {res}")
                if self.stack.currentWidget() is self.home or self.nav["home"].isChecked():
                    self.go("home")
            bk.refresh_installed()
        spawn(index.refresh, on_done=done, on_progress=prog, with_progress=True)

    # ---- onboarding
    def show_onboarding(self):
        self.onboarding.preset(self.cfg.get("interests", []))
        self.check_nav("")
        self.pill_w.hide()
        self.stack.setCurrentWidget(self.onboarding)

    def finish_onboarding(self, ids):
        self.cfg["interests"], self.cfg["onboarded"] = ids, True
        bk.save_config(self.cfg)
        self.go("home")

    # ---- navigation
    def go(self, key):
        self.check_nav(key)
        self.pill_w.hide()
        self.toast("")
        if key == "home":
            self.stack.setCurrentWidget(self.home)
            self.build_home()
        elif key == "installed":
            self.stack.setCurrentWidget(self.installed_page)
            self.build_installed()
        elif key == "updates":
            self.stack.setCurrentWidget(self.updates_page)
            self.build_updates()
        else:
            self.settings.refresh_summary()
            self.stack.setCurrentWidget(self.settings)

    def build_home(self):
        pg = self.home
        pg.clear()
        if not index.exists():
            pg.message("Preparing the app catalog…\nThis only happens once and takes a few seconds.")
            return
        pg.message("Loading your shelves…")
        ids = self.cfg.get("interests") or [i[0] for i in bk.INTERESTS[:3]]
        def work():
            shelves = [bk.interest_shelf(i, 9) for i in ids[:5]]
            return shelves, bk.trending_aur(9)
        spawn(work, on_done=self.home_ready)

    def home_ready(self, res):
        pg = self.home
        pg.clear()
        if isinstance(res, Exception):
            pg.message(f"Could not load the catalog: {res}")
            return
        shelves, trending = res
        feats = [a for t, apps in shelves for a in apps[:1]]
        if feats:
            hero = Hero(feats[datetime.date.today().toordinal() % len(feats)], self.order)
            hero.opened.connect(self.open_app)
            pg.v.addWidget(hero)
        for title, apps in shelves:
            if apps:
                pg.v.addSpacing(10)
                pg.v.addWidget(lbl(title, "h2"))
                pg.cards(apps, self.order, self.open_app)
        if trending:
            pg.v.addSpacing(10)
            pg.v.addWidget(lbl("🔥  Trending in the AUR", "h2"))
            pg.cards(trending, self.order, self.open_app)
        pg.end()

    def build_installed(self):
        pg = self.installed_page
        pg.clear()
        pg.heading("Installed", "Everything you added yourself, from every source", "LIBRARY")
        pg.message("Loading…")
        spawn(bk.list_installed, not self.show_all, on_done=self.installed_ready)

    def installed_ready(self, pkgs):
        pg = self.installed_page
        pg.clear()
        if isinstance(pkgs, Exception):
            pkgs = []
        pg.heading("Installed", f"{len(pkgs)} " + ("packages, including system tools" if self.show_all else "apps"), "LIBRARY")
        tog = QPushButton("Show apps only" if self.show_all else "Show all packages (including system)")
        tog.setObjectName("pill")
        tog.setFixedWidth(300)
        tog.clicked.connect(lambda: (setattr(self, "show_all", not self.show_all), self.build_installed()))
        pg.v.addWidget(tog, 0, Qt.AlignmentFlag.AlignLeft)
        apps = []
        for p in pkgs:
            a = bk.App(p.name)
            a.items[p.source] = p
            apps.append(a)
        pg.cards(apps, self.order, self.open_app)
        pg.end()

    def build_updates(self):
        pg = self.updates_page
        pg.clear()
        pg.heading("Updates", "Sunshine OS updates pacman, the AUR, Flatpak and the rest together, with a restore point first.", "KEEP IT FRESH")
        b = QPushButton("Update everything now")
        b.setObjectName("primary")
        b.setFixedWidth(260)
        b.clicked.connect(lambda: QProcess.startDetached("sunshine-update", []))
        pg.v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        pg.end()

    # ---- search
    def on_text(self, text):
        if len(text.strip()) >= 2:
            self._debounce.start(220)
        elif not text.strip():
            self.go("home")

    def do_search(self):
        q = self.search.text().strip()
        if not q or q == self._query and self.stack.currentWidget() is self.results:
            return
        self._query = q
        self.check_nav("")
        self.stack.setCurrentWidget(self.results)
        t0 = datetime.datetime.now()
        def fin(res):
            if q != self._query:
                return
            ms = (datetime.datetime.now() - t0).total_seconds() * 1000
            self.show_results(q, res, ms)
        spawn(bk.search_all, q, on_done=fin)

    def show_results(self, q, res, ms=0):
        if isinstance(res, bk.NoCatalog):
            self.results.clear()
            self.results.message("The catalog is still being prepared. Try again in a few seconds.")
            return
        if isinstance(res, Exception):
            self.results.clear()
            self.results.message(f"Search failed: {res}")
            return
        self._results = res
        self._filter = None
        self.render_results(q, ms)

    def render_results(self, q, ms=0):
        # source filter pills with counts
        while self.pillbar.count():
            w = self.pillbar.takeAt(0).widget()
            if w:
                w.deleteLater()
        counts = {s: sum(1 for a in self._results if s in a.items) for s in bk.SOURCES}
        opts = [(None, f"All  {len(self._results)}")] + [(s, f"{SRC_NAME[s]}  {n}") for s, n in counts.items() if n]
        for s, text in opts:
            b = QPushButton(text)
            b.setObjectName("pill")
            b.setCheckable(True)
            b.setChecked(s == self._filter)
            if s:
                b.setStyleSheet(f"QPushButton#pill:checked {{ border-color: {FRAME[s]}; color: {FRAME[s] if s != 'pacman' else PARCH}; }}")
            b.clicked.connect(lambda _, s=s: self.set_filter(s, q))
            self.pillbar.addWidget(b)
        self.pillbar.addStretch(1)
        self.pill_w.show()
        apps = [a for a in self._results if self._filter is None or self._filter in a.items][:80]
        pg = self.results
        pg.clear()
        pg.heading(f"Results for “{q}”", f"{len(apps)} apps" + (f"  ·  {ms:.0f} ms" if ms else ""), "SEARCH")
        if not apps:
            pg.message("Nothing found. Check the spelling, or try a more general word.")
        for a in apps:
            r = Row(a, self.order)
            r.clicked.connect(self.open_app)
            pg.v.addWidget(r)
        pg.end()

    def set_filter(self, s, q):
        self._filter = s
        self.render_results(q)

    # ---- install / remove (everything runs in the linked terminal)
    def run(self, pkg, remove=False):
        argv = bk.argv_for(pkg, remove)
        if self.cfg.get("auto_terminal", True):
            self.term_btn.setChecked(True)
        started = self.term.run(argv, ("Removing " if remove else "Installing ") + pkg.label,
                                on_finished=lambda code: self.finished(pkg, remove, code))
        if started:
            self.toast(("Removing " if remove else "Installing ") + pkg.label + "…")

    def finished(self, pkg, remove, code):
        if code == 0:
            pkg.installed = not remove
            bk.refresh_installed()
            self.toast(("Removed " if remove else "Installed ") + pkg.label)
            if self.detail.pkg is pkg:
                self.detail.show_app(self.detail.app, pkg)
        else:
            self.toast("That did not work. The terminal below shows what happened.")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setFont(QFont("Inter", 10))
    w = Store()
    if "--size" in sys.argv:
        sw, sh = sys.argv[sys.argv.index("--size") + 1].split("x")
        w.resize(int(sw), int(sh))
    w.show()
    if "--shot" in sys.argv:                          # test helper
        arg = lambda k, d="": sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
        out, page, q = arg("--shot"), arg("--page", "home"), arg("--query")
        def setup():
            w.cfg.setdefault("interests", ["games", "create", "media"]) if page != "onboarding" else None
            if page == "onboarding":
                w.show_onboarding()
            elif page == "settings":
                w.go("settings")
            elif page == "installed":
                w.go("installed")
            elif page == "terminal":
                w.term_btn.setChecked(True)
                w.term.view.clear()
                demo = ("printf 'Resolving dependencies...\\n'; for i in 10 40 70 100; do printf '\\r\\033[1m downloading\\033[0m [%3d%%]' $i; sleep 0.3; done; "
                        "printf '\\n\\033[32m::\\033[0m installing demo-package (1.0-1)\\n\\033[33mwarning:\\033[0m this is a coloured line\\n'; read -p 'Proceed? [Y/n] ' a; echo answered:$a")
                w.term.run(["bash", "-c", demo], "demo")
                QTimer.singleShot(3000, lambda: w.term.reader and (w.term.say("y\n", GOLD_B), w.term.reader.send("y\n")))
            elif page in ("results", "detail"):
                w.search.setText(q)
                w.do_search()
        def detail():
            if page == "detail":
                rows = w.results.findChildren(Row)
                if rows:
                    w.open_app(rows[0].app)
        QTimer.singleShot(300, setup)
        QTimer.singleShot(int(arg("--wait", "9000")) - 3000, detail)
        QTimer.singleShot(int(arg("--wait", "9000")), lambda: (w.grab().save(out), app.quit()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
