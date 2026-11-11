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

from PyQt6.QtCore import QRect, QSize, Qt, QThread, QTimer, pyqtSignal, QProcess
from PyQt6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QImageReader, QKeySequence, QLinearGradient, QPainter,
                         QPixmap, QRadialGradient, QPen, QShortcut, QTextCharFormat, QTextCursor)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
                             QFileDialog)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backend as bk  # noqa: E402
import index  # noqa: E402
import windows  # noqa: E402

# ---- theme engine --------------------------------------------------------------------------------------------
import themes  # noqa: E402

SERIF = '"Cormorant Garamond","Noto Serif","DejaVu Serif",Georgia,serif'
SANS = '"Inter","Noto Sans","DejaVu Sans",sans-serif'
SRC_NAME = {"aur": "AUR", "pacman": "Pacman", "flatpak": "Flatpak", "appimage": "AppImage", "snap": "Snap", "wine": "Wine / Proton"}
SRC_KIND = {"aur": "AUR package", "pacman": "Arch package", "flatpak": "Flatpak app", "appimage": "AppImage", "snap": "Snap", "wine": "Windows app"}
ICON_CACHE = os.path.join(index.CACHE, "icons")
JXL_OK = b"jxl" in [bytes(f).lower() for f in QImageReader.supportedImageFormats()]


def _rgb(h: str):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def mix(a: str, b: str, t: float) -> str:
    """Blend colour a toward b by t (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def rgba(c: str, alpha: float) -> str:
    r, g, b = _rgb(c)
    return f"rgba({r},{g},{b},{alpha})"


def lum(c: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def qcolor(c: str, alpha: int = 255) -> QColor:
    r, g, b = _rgb(c)
    return QColor(r, g, b, alpha)


# current palette (module globals, rewritten by apply_theme)
THEME_ID = ""
IS_DARK = True
VOID = VOID2 = BURGUNDY = CHERRY = CHERRY_B = BTN_FG = GOLD = GOLD_B = GOLD_DIM = PARCH = PARCH_DIM = MUTED = ""
LINE = OVERLAY = OVERLAY_H = SURFACE_RGBA = HOVER_RGBA = GREEN = DANGER = ""
FRAME: dict = {}


def apply_theme(t: dict) -> None:
    """Derive every colour the UI uses from one palette (bg, surface, primary, secondary=text, accent), keeping text readable."""
    global THEME_ID, IS_DARK, VOID, VOID2, BURGUNDY, CHERRY, CHERRY_B, BTN_FG, GOLD, GOLD_B, GOLD_DIM
    global PARCH, PARCH_DIM, MUTED, LINE, OVERLAY, OVERLAY_H, SURFACE_RGBA, HOVER_RGBA, GREEN, DANGER, FRAME
    bg = t["bg"]
    dark = lum(bg) < 0.30
    text = t["secondary"]
    if contrast(text, bg) < 4.5:
        text = "#f2ece0" if dark else "#241f1b"
    surface = t["surface"]
    brand = next((c for c in (t["primary"], t["accent"], text) if contrast(c, bg) >= 3.0), text)
    accent = t["accent"] if contrast(t["accent"], bg) >= 1.8 else brand
    THEME_ID, IS_DARK = t.get("id", "custom"), dark
    VOID, VOID2 = bg, surface
    GOLD = brand
    GOLD_B = mix(brand, text, 0.30)
    if contrast(GOLD_B, bg) < 3.5:
        GOLD_B = text
    GOLD_DIM = mix(brand, bg, 0.5)
    PARCH, PARCH_DIM, MUTED = text, mix(text, bg, 0.30), mix(text, bg, 0.52)
    BURGUNDY = mix(bg, accent, 0.20)
    CHERRY, CHERRY_B = mix(surface, accent, 0.55), mix(surface, accent, 0.75)
    BTN_FG = text if contrast(text, CHERRY) >= 4.5 else ("#ffffff" if lum(CHERRY) < 0.45 else "#1a1612")
    LINE = rgba(brand, 0.30)
    OVERLAY, OVERLAY_H = rgba(text, 0.06), rgba(text, 0.12)
    SURFACE_RGBA = rgba(surface, 0.78 if dark else 0.90)
    HOVER_RGBA = rgba(mix(surface, accent, 0.22), 0.90)
    GREEN = "#7fb36b" if dark else "#3f8a34"
    DANGER = "#e88a98" if dark else "#b3261e"
    FRAME = {"aur": "#6b9de0" if dark else "#2f6fcf", "pacman": mix(text, bg, 0.22), "flatpak": mix(text, bg, 0.55),
             "appimage": "#a78bd9" if dark else "#7a52c4", "snap": "#d9a05b" if dark else "#b56a12",
             "wine": "#d9858f" if dark else "#b3404f"}


def theme_by_id(tid: str, custom: dict | None = None) -> dict:
    if tid == "custom" and custom:
        return custom
    return next((t for t in themes.THEMES if t["id"] == tid), themes.THEMES[0])


def palette_from_image(path: str) -> dict | None:
    """Build a calm dark palette from a wallpaper: its darkest tones for the background, its warmest/brightest for the brand."""
    from PyQt6.QtGui import QImage
    img = QImage(path)
    if img.isNull():
        return None
    img = img.scaled(96, 96, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    px = [img.pixelColor(x, y) for y in range(96) for x in range(96)]
    px.sort(key=lambda c: c.lightnessF())
    def avg(cs):
        n = max(1, len(cs))
        return _hex((sum(c.red() for c in cs) / n, sum(c.green() for c in cs) / n, sum(c.blue() for c in cs) / n))
    dark_end = avg(px[: len(px) // 3])
    bg = mix(dark_end, "#000000", 0.25 if lum(dark_end) > 0.02 else 0)
    bg = mix(bg, "#10131c", 0.35)
    scored = sorted(px, key=lambda c: c.saturationF() * c.lightnessF() ** 0.6, reverse=True)
    brand = avg(scored[: max(8, len(scored) // 40)])
    for _ in range(4):                                # keep it readable on the dark background
        if contrast(brand, bg) >= 4.0:
            break
        brand = mix(brand, "#ffffff", 0.18)
    return {"id": "custom", "label": "From my wallpaper", "bg": bg, "surface": mix(bg, "#ffffff", 0.06), "surface_alt": mix(bg, "#ffffff", 0.10),
            "panel": mix(bg, "#ffffff", 0.04), "primary": brand, "secondary": mix("#efe9dc", brand, 0.08),
            "accent": mix(brand, bg, 0.30), "edge": rgba(brand, 0.2), "source": os.path.basename(path)}


def build_qss() -> str:
    return f"""
* {{ font-family: {SANS}; }}
QWidget {{ background: transparent; color: {PARCH}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QLabel#step {{ color: {GOLD_DIM}; font-size: 10px; letter-spacing: 2px; }}
QLabel#h1 {{ font-family: {SERIF}; font-style: italic; font-weight: 700; font-size: 34px; color: {PARCH}; }}
QLabel#h2 {{ font-family: {SERIF}; font-weight: 600; font-size: 24px; color: {GOLD_B}; }}
QLabel#h3 {{ font-family: {SERIF}; font-weight: 600; font-size: 17px; color: {PARCH}; }}
QLabel#dim {{ color: {PARCH_DIM}; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#logo {{ font-family: {SERIF}; font-style: italic; font-weight: 700; font-size: 24px; color: {GOLD_B}; padding: 4px 18px 0; }}
QLabel#logosub {{ font-size: 9px; letter-spacing: 4px; color: {GOLD_DIM}; padding: 0 20px 18px; }}
QLabel#legend {{ font-size: 10px; color: {MUTED}; padding: 10px 18px; }}
QFrame#side {{ background: {OVERLAY}; border-right: 1px solid {LINE}; }}
QLineEdit#search {{ background: {SURFACE_RGBA}; border: 1px solid {LINE}; border-radius: 3px; padding: 11px 14px; font-size: 14px; color: {PARCH}; }}
QLineEdit#search:focus {{ border: 1px solid {GOLD}; }}
QLineEdit#cmd {{ background: #14161c; border: 1px solid {LINE}; border-radius: 3px; padding: 10px 12px;
                 font-family: "JetBrains Mono","DejaVu Sans Mono",monospace; color: #e8c98e; selection-background-color: {CHERRY}; }}
QPushButton {{ background: {OVERLAY}; border: 1px solid {LINE}; border-radius: 3px; padding: 9px 16px; color: {PARCH_DIM}; }}
QPushButton:hover {{ border-color: {GOLD_DIM}; background: {OVERLAY_H}; }}
QPushButton#pill {{ border-radius: 3px; padding: 8px 16px; }}
QPushButton#pill:checked {{ border-color: {GOLD}; color: {GOLD_B}; background: {rgba(GOLD, 0.12)}; }}
QPushButton#nav {{ border: none; background: transparent; text-align: left; padding: 12px 16px; font-size: 14px; color: {PARCH_DIM}; }}
QPushButton#nav:hover {{ color: {PARCH}; background: {OVERLAY}; }}
QPushButton#nav:checked {{ color: {GOLD_B}; background: {rgba(GOLD, 0.12)}; border-left: 2px solid {GOLD}; }}
QPushButton#get {{ background: {CHERRY}; border: 1px solid {GOLD}; color: {BTN_FG}; font-weight: 600; padding: 11px 30px;
                   border-top-right-radius: 0; border-bottom-right-radius: 0; font-size: 14px; }}
QPushButton#get:hover {{ background: {CHERRY_B}; }}
QToolButton#getarrow {{ background: {CHERRY}; border: 1px solid {GOLD}; border-left: 1px solid {GOLD_DIM}; color: {BTN_FG};
                        padding: 11px 9px; border-top-left-radius: 0; border-bottom-left-radius: 0; border-top-right-radius: 3px; border-bottom-right-radius: 3px; }}
QToolButton#getarrow::menu-indicator {{ image: none; }}
QToolButton#getarrow:hover {{ background: {CHERRY_B}; }}
QPushButton#danger {{ border-color: {DANGER}; color: {DANGER}; }}
QPushButton#choice {{ text-align: left; padding: 16px 14px; font-size: 14px; border-radius: 2px; }}
QPushButton#choice:checked {{ border: 1px solid {GOLD}; color: {GOLD_B}; background: {rgba(mix(VOID2, GOLD, 0.2), 0.9)}; }}
QPushButton#primary {{ background: {CHERRY}; border: 1px solid {GOLD}; color: {BTN_FG}; font-weight: 600; padding: 12px 34px; }}
QPushButton#primary:hover {{ background: {CHERRY_B}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {rgba(GOLD, 0.30)}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QPlainTextEdit {{ background: #14161c; color: #d8d8d2; border: 1px solid {LINE}; border-radius: 3px; font-family: "JetBrains Mono","DejaVu Sans Mono",monospace; }}
QMenu {{ background: {VOID2}; color: {PARCH}; border: 1px solid {LINE}; padding: 6px; }}
QMenu::item {{ padding: 8px 22px; }}
QMenu::item:selected {{ background: {BURGUNDY}; color: {GOLD_B}; }}
QComboBox {{ background: {OVERLAY}; border: 1px solid {LINE}; padding: 9px 12px; border-radius: 3px; color: {PARCH}; }}
QComboBox QAbstractItemView {{ background: {VOID2}; color: {PARCH}; border: 1px solid {LINE}; selection-background-color: {BURGUNDY}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 19px; height: 19px; border: 1px solid {GOLD_DIM}; background: {OVERLAY}; }}
QCheckBox::indicator:checked {{ background: {CHERRY}; border-color: {GOLD}; }}
QPushButton#update {{ background: {CHERRY}; border: 1px solid {GOLD}; color: {BTN_FG}; font-weight: 600; padding: 10px 18px; }}
QPushButton#update:hover {{ background: {CHERRY_B}; }}
QFrame#popup {{ background: {VOID2}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame#codebox {{ background: #14161c; border: none; border-radius: 6px; }}
QFrame#codebox QLabel {{ color: #e8c98e; font-family: "JetBrains Mono","DejaVu Sans Mono",monospace; font-size: 12px; background: transparent; }}
QToolButton#copy {{ background: transparent; border: none; color: #9aa0ab; font-size: 17px; padding: 2px 6px; }}
QToolButton#copy:hover {{ color: #ffffff; }}
QToolButton#getarrow {{ min-width: 18px; }}
QSplitter::handle:vertical {{ background: {rgba(GOLD, 0.16)}; height: 7px; }}
QSplitter::handle:vertical:hover {{ background: {rgba(GOLD, 0.55)}; }}
"""


apply_theme(theme_by_id(themes.DEFAULT_THEME))


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
    g.setColorAt(0, qcolor(CHERRY))
    g.setColorAt(1, qcolor(BURGUNDY))
    p.setBrush(g)
    p.setPen(QPen(qcolor(GOLD_DIM), 1))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 6, 6)
    p.setPen(qcolor(GOLD_B))
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
        p.fillRect(self.rect(), qcolor(VOID2, (215 if self.strong else 185) if IS_DARK else 235))
        p.setPen(QPen(qcolor(GOLD, 70), 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0, qcolor(GOLD, 0))
        g.setColorAt(0.5, qcolor(GOLD, 150))
        g.setColorAt(1, qcolor(GOLD, 0))
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
        self.setStyleSheet(f"QFrame#card {{ border: 1px solid {col}; border-radius: 3px; background: {SURFACE_RGBA}; }}"
                           f"QFrame#card:hover {{ background: {HOVER_RGBA}; }} QLabel {{ background: transparent; border: none; }}")
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
        self.updateGeometry()


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
                           f"QFrame#row:hover {{ background: {HOVER_RGBA}; }} QLabel {{ background: transparent; border: none; }}")
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
        self.setMinimumHeight(150)
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
        self.say("Sunshine Store terminal. Installs and removals show up here.\n", "#8a8f99")

    # ---- output
    def _reset_fmt(self):
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(QColor("#d8d8d2"))

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
                self._fmt.setForeground(QColor("#d8d8d2"))
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
        self.say("\n$ " + " ".join(argv) + "\n", "#e8c98e")
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
        self.say(("\n✔ Finished" if code == 0 else f"\n✘ Stopped with exit code {code}") + "\n", "#7fb36b" if code == 0 else "#e06c75")
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


class Swatch(QPushButton):
    """A clickable mini-preview of one theme."""

    def __init__(self, t: dict):
        super().__init__()
        self.t = t
        self.setFixedSize(156, 86)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{t['label']}  ·  {t.get('source', '')}")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, qcolor(t["bg"]))
        card = QRect(8, 8, r.width() - 14, 38)
        p.fillRect(card, qcolor(t["surface"]))
        txt = t["secondary"] if contrast(t["secondary"], t["bg"]) >= 4.5 else ("#f2ece0" if lum(t["bg"]) < 0.3 else "#241f1b")
        brand = next((c for c in (t["primary"], t["accent"], txt) if contrast(c, t["bg"]) >= 3.0), txt)
        for i, col in enumerate((brand, t["accent"], txt)):
            p.setBrush(qcolor(col))
            p.setPen(QPen(qcolor(txt, 60), 1))
            p.drawEllipse(card.x() + 8 + i * 22, card.y() + 9, 18, 18)
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(qcolor(txt))
        p.drawText(QRect(8, 52, r.width() - 14, 26), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   p.fontMetrics().elidedText(t["label"], Qt.TextElideMode.ElideRight, r.width() - 14))
        selected = t.get("id") == THEME_ID
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcolor(GOLD) if selected else qcolor(brand, 70), 2 if selected else 1))
        p.drawRect(r)
        if selected:
            p.setPen(qcolor(brand))
            p.drawText(QRect(r.width() - 26, 50, 22, 24), Qt.AlignmentFlag.AlignCenter, "✔")


class SwatchFlow(CardFlow):
    GAP, MIN_W = 12, 156


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
        g.setColorAt(0, qcolor(mix(VOID2, GOLD, 0.30), 230))
        g.setColorAt(1, qcolor(VOID, 235))
        p.fillRect(self.rect(), g)
        p.setPen(QPen(qcolor(GOLD, 100), 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)

    def mousePressEvent(self, e):
        self.opened.emit(self.app)


class CopyBox(QFrame):
    """A code block with a copy icon, like the Flathub install box."""

    def __init__(self, text: str, store):
        super().__init__()
        self.setObjectName("codebox")
        self.text, self.store = text, store
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 8, 10)
        l = QLabel(text)
        l.setWordWrap(True)
        l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(l, 1)
        b = QToolButton()
        b.setObjectName("copy")
        b.setText("⧉")
        b.setToolTip("Copy")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(self.copy)
        h.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)

    def copy(self):
        QGuiApplication.clipboard().setText(self.text)
        self.store.toast("Copied. Paste it into a terminal.")


class CommandPopup(QFrame):
    """Dropdown under the Install button: Manual install, Run, and the other places to get the app."""

    def __init__(self, store, parent):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("popup")
        self.setFixedWidth(440)
        self.store = store

    def build(self, app: bk.App, pkg: bk.Pkg, switch):
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 20)
        v.setSpacing(8)
        if pkg.source == "wine":
            v.addWidget(lbl("Run", "h3"))
            v.addWidget(CopyBox(bk.run_command_for(pkg), self.store))
            v.addWidget(lbl("Remove", "h3"))
            v.addWidget(lbl("Takes the menu entry away. Use the Wine uninstaller from the Windows page to remove the program itself.", "dim", wrap=True))
            v.addWidget(CopyBox(bk.command_for(pkg, True), self.store))
        else:
            v.addWidget(lbl("Manual Install", "h3"))
            v.addWidget(lbl("Prefer the terminal? Run this instead of pressing Install.", "dim", wrap=True))
            v.addWidget(CopyBox(bk.command_for(pkg), self.store))
            v.addWidget(lbl("Run", "h3"))
            v.addWidget(CopyBox(bk.run_command_for(pkg), self.store))
            if pkg.installed:
                v.addWidget(lbl("Remove", "h3"))
                v.addWidget(CopyBox(bk.command_for(pkg, True), self.store))
        others = [s for s in app.sources if s != pkg.source]
        if others:
            v.addSpacing(6)
            v.addWidget(lbl("Get it from somewhere else", "h3"))
            for s in others:
                b = QPushButton(f"{SRC_NAME[s]}   ·   {app.items[s].name} {app.items[s].version}")
                b.setObjectName("pill")
                b.clicked.connect(lambda _, s=s: (self.close(), switch(app, app.items[s])))
                v.addWidget(b)

    def show_under(self, button: QWidget):
        self.adjustSize()
        g = button.mapToGlobal(button.rect().bottomRight())
        self.move(g.x() - self.width() + 1, g.y() + 8)
        self.show()


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
        self.arrow.setToolTip("Manual install, run command, other sources")
        self.arrow.clicked.connect(self.open_popup)
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
            pb.setStyleSheet(f"QPushButton#pill {{ border-color: {col}; }} QPushButton#pill:checked {{ background: {OVERLAY_H}; color: {col}; border-width: 2px; }}")
            pb.clicked.connect(lambda _, s=s: self.show_app(app, app.items[s]))
            self.pills.addWidget(pb)
        self.pills.addStretch(1)
        self.btn.setText("Open" if p.source == "wine" else ("Reinstall" if p.installed else "Get"))
        self.rm.setText("Remove from menu" if p.source == "wine" else "Uninstall")
        self.rm.setVisible(p.installed)
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

    def open_popup(self):
        if not self.pkg:
            return
        pop = CommandPopup(self.store, self)
        pop.build(self.app, self.pkg, self.show_app)
        pop.show_under(self.arrow)

    def act(self):
        if self.pkg.source == "wine":
            QProcess.startDetached("bash", ["-c", windows.run_command({"exec": next((e["exec"] for e in windows.entries() if e["path"] == self.pkg.app_id), "")})])
            self.store.toast("Starting " + self.pkg.label + "…")
        else:
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


class DropZone(QFrame):
    dropped = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setFixedHeight(150)
        self.setObjectName("drop")
        self._style(False)
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = lbl("⬇", "h1", align=Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon)
        v.addWidget(lbl("Drop a Windows installer or game here (.exe, .msi)", "h3", align=Qt.AlignmentFlag.AlignCenter))
        v.addWidget(lbl("or", "muted", align=Qt.AlignmentFlag.AlignCenter))
        b = QPushButton("Browse…")
        b.setObjectName("pill")
        b.setFixedWidth(140)
        b.clicked.connect(self.browse)
        v.addWidget(b, 0, Qt.AlignmentFlag.AlignCenter)

    def _style(self, hot: bool):
        col = GOLD if hot else GOLD_DIM
        self.setStyleSheet(f"QFrame#drop {{ border: 2px dashed {col}; border-radius: 8px; background: {rgba(GOLD, 0.10 if hot else 0.04)}; }}"
                           f"QFrame#drop QLabel {{ border: none; background: transparent; }}")

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a Windows program", os.path.expanduser("~/Downloads"),
                                              "Windows programs (*.exe *.msi *.bat *.lnk);;All files (*)")
        if path:
            self.dropped.emit(path)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(windows.EXTS) for u in e.mimeData().urls()):
            e.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, e):
        self._style(False)

    def dropEvent(self, e):
        self._style(False)
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(windows.EXTS):
                self.dropped.emit(p)
                break


class WindowsPage(Page):
    """Install Windows apps and games with Wine or Proton; the ones already installed show up in the catalog too."""

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.pending = None

    def build(self):
        st = self.store
        self.clear()
        self.heading("Windows apps & games", "Run Windows programs on Sunshine OS. Drop an installer below: it is run with Wine or Proton in the "
                     "terminal at the bottom, and the menu entry is created for you afterwards.", "WINDOWS")
        have = windows.runners()
        if not have:
            p = Panel()
            v = QVBoxLayout(p)
            v.setContentsMargins(22, 18, 22, 18)
            v.addWidget(lbl("Wine is not installed yet", "h3"))
            v.addWidget(lbl("Wine runs Windows programs. Install it from the store, then come back here.", "dim", wrap=True))
            b = QPushButton("Find Wine in the store")
            b.setObjectName("primary")
            b.clicked.connect(lambda: (st.search.setText("wine"), st.do_search()))
            v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
            self.v.addWidget(p)
        dz = DropZone()
        dz.dropped.connect(self.ask)
        self.v.addWidget(dz)
        self.slot = QVBoxLayout()
        self.v.addLayout(self.slot)

        row = QHBoxLayout()
        scan = QPushButton("Find Steam / Proton games")
        scan.setObjectName("pill")
        scan.setEnabled(windows.has_steam_shortcuts())
        scan.setToolTip("Runs steam-shortcuts: makes menu entries for installed Steam and Proton games" if windows.has_steam_shortcuts()
                        else "steam-shortcuts is not installed")
        scan.clicked.connect(lambda: st.run_steam_shortcuts())
        row.addWidget(scan)
        watch = QCheckBox("Create menu entries automatically after each install")
        watch.setEnabled(windows.has_steam_shortcuts())
        watch.setChecked(windows.watch_enabled())
        watch.toggled.connect(lambda on: st.term.run(["steam-shortcuts", "--install-watch" if on else "--remove-watch"], "Steam shortcuts watcher"))
        row.addWidget(watch)
        row.addStretch(1)
        self.v.addLayout(row)

        self.v.addSpacing(8)
        self.v.addWidget(lbl("Installed Windows apps", "h2"))
        apps = []
        for p in bk.windows_pkgs(True):
            a = bk.App(p.name)
            a.items["wine"] = p
            apps.append(a)
        if apps:
            self.cards(apps, st.order, st.open_app)
        else:
            self.v.addWidget(lbl("Nothing here yet. Apps installed with Wine or Proton, and Steam games, appear here and in search.", "muted", wrap=True))
        self.end()

    def ask(self, exe: str):
        while self.slot.count():
            it = self.slot.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        p = Panel(strong=True)
        v = QVBoxLayout(p)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("READY TO INSTALL", "step"))
        v.addWidget(lbl(os.path.basename(exe), "h3"))
        v.addWidget(lbl(exe, "muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(lbl("Run with", "dim"))
        combo = QComboBox()
        for r in windows.runners() or ["wine"]:
            combo.addItem({"wine": "Wine", "proton": "Proton (umu-launcher)"}[r], r)
        row.addWidget(combo)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(lbl("Each program gets its own Wine prefix, so one app can never break another.", "muted", wrap=True))
        v.addWidget(CopyBox(" ".join(windows.install_argv(exe, combo.currentData() or "wine")), self.store))
        btns = QHBoxLayout()
        go = QPushButton("Install")
        go.setObjectName("primary")
        go.clicked.connect(lambda: (self.store.install_windows(exe, combo.currentData() or "wine"), p.deleteLater()))
        add = QPushButton("Already installed: just add it to the menu")
        add.setObjectName("pill")
        add.clicked.connect(lambda: (self.store.install_windows(exe, combo.currentData() or "wine", add_only=True), p.deleteLater()))
        no = QPushButton("Cancel")
        no.clicked.connect(p.deleteLater)
        btns.addWidget(go)
        btns.addWidget(add)
        btns.addStretch(1)
        btns.addWidget(no)
        v.addLayout(btns)
        self.slot.addWidget(p)


class Settings(QScrollArea):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self.setWidget(inner)
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(32, 8, 32, 24)
        outer.setSpacing(14)
        outer.addWidget(lbl("PREFERENCES", "step"))
        outer.addWidget(lbl("Settings", "h2"))

        pa = Panel()
        v = QVBoxLayout(pa)
        v.setContentsMargins(22, 18, 22, 18)
        v.addWidget(lbl("Appearance", "h3"))
        v.addWidget(lbl("Pick a look; it applies right away. The Sunshine themes come from your wallpapers, the others are palettes from the Ingama Textil theme set.", "dim", wrap=True))
        flow = SwatchFlow()
        custom = store.cfg.get("custom_theme")
        for t in ([custom] if custom else []) + themes.THEMES:
            sw = Swatch(t)
            sw.clicked.connect(lambda _, t=t: store.set_theme(t["id"], t if t.get("id") == "custom" else None))
            flow.add(sw)
        v.addWidget(flow)
        wp = QPushButton("🖼  Build a theme from a wallpaper…")
        wp.setFixedWidth(300)
        wp.clicked.connect(store.theme_from_wallpaper)
        v.addWidget(wp, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(pa)

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
        self.procs, self._results, self._query = [], [], ""
        self._src_on = {s for s in self.cfg.get("sources", list(bk.SOURCES)) if s in bk.SOURCES} or set(bk.SOURCES)
        self._building = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QFrame()
        side.setFixedWidth(210)
        side.setObjectName("side")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 18, 0, 14)
        logo = lbl("☀  Sunshine", "logo")
        sub = lbl("S T O R E", "logosub")
        sl.addWidget(logo)
        sl.addWidget(sub)
        self.nav = {}
        for key, label in (("home", "⌂   Home"), ("installed", "✔   Installed"), ("windows", "▣   Windows"), ("updates", "⬆   Updates"), ("settings", "⚙   Settings")):
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self.go(k))
            sl.addWidget(b)
            self.nav[key] = b
        sl.addStretch(1)
        self.legend = lbl("", "legend")
        self.fill_legend()
        sl.addWidget(self.legend)
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
        top.addWidget(self.search, 1)
        self.update_btn = QPushButton("⟳  Update everything")
        self.update_btn.setObjectName("update")
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setToolTip("Update system packages, AUR and Flatpak apps in one go (runs in the terminal below)")
        self.update_btn.clicked.connect(self.full_update)
        top.addWidget(self.update_btn)
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
        self.windows_page = WindowsPage(self)
        self.settings = Settings(self)
        self.detail = Detail(self)
        self.detail.back.connect(self.go_back)
        for w in (self.onboarding, self.home, self.results, self.installed_page, self.windows_page, self.updates_page, self.settings, self.detail):
            self.stack.addWidget(w)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(7)
        self.split.addWidget(self.stack)
        self.term = TerminalPanel()
        self.split.addWidget(self.term)
        self.term.hide()
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.splitterMoved.connect(self._term_moved)
        main.addWidget(self.split, 1)
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
        QTimer.singleShot(7000, self.refresh_update_count)
        if not self.cfg.get("onboarded"):
            self.show_onboarding()
        else:
            self.go("home")

    # ---- painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(VOID))
        w, h = self.width(), self.height()
        g = QRadialGradient(w * 0.15, -h * 0.05, max(w, h) * 0.7)
        glow = BURGUNDY if IS_DARK else mix(VOID, GOLD, 0.15)
        g.setColorAt(0, qcolor(glow, 90 if IS_DARK else 70))
        g.setColorAt(1, qcolor(glow, 0))
        p.fillRect(self.rect(), g)
        g2 = QRadialGradient(w, h * 0.2, max(w, h) * 0.5)
        g2.setColorAt(0, qcolor(GOLD, 26))
        g2.setColorAt(1, qcolor(GOLD, 0))
        p.fillRect(self.rect(), g2)

    # ---- helpers
    def set_terminal(self, on):
        if on:
            self.term.show()
            total = max(self.split.height(), 600)
            h = max(150, min(int(self.cfg.get("term_height", 300)), total - 220))
            self.split.setSizes([total - h - 7, h])
            self.term.input.setFocus()
        else:
            self._term_moved()
            self.term.hide()
            bk.save_config(self.cfg)

    def _term_moved(self, *_):
        if self.term.isVisible() and len(self.split.sizes()) > 1 and self.split.sizes()[1] > 0:
            self.cfg["term_height"] = self.split.sizes()[1]

    def closeEvent(self, e):
        self._term_moved()
        bk.save_config(self.cfg)
        super().closeEvent(e)

    def fill_legend(self):
        c = FRAME
        self.legend.setText(f"<span style='color:{c['aur']}'>■</span> AUR &nbsp; <span style='color:{c['pacman']}'>■</span> Pacman<br>"
                            f"<span style='color:{c['flatpak']}'>■</span> Flatpak &nbsp; <span style='color:{c['appimage']}'>■</span> AppImage<br>"
                            f"<span style='color:{c['snap']}'>■</span> Snap &nbsp; <span style='color:{c['wine']}'>■</span> Wine<br>"
                            f"<span style='color:{GREEN}'>■</span> Installed")

    def set_theme(self, tid, custom=None):
        t = theme_by_id(tid, custom or self.cfg.get("custom_theme"))
        apply_theme(t)
        QApplication.instance().setStyleSheet(build_qss())
        self.cfg["theme"] = tid
        if custom:
            self.cfg["custom_theme"] = custom
        bk.save_config(self.cfg)
        self.fill_legend()
        cur = self.stack.currentWidget()
        if cur is self.home:
            self.build_home()
        elif cur is self.installed_page:
            self.build_installed()
        elif cur is self.results and self._results:
            self.render_results(self._query)
        elif cur is self.detail and self.detail.app:
            self.detail.show_app(self.detail.app, self.detail.pkg)
        idx, was = self.stack.indexOf(self.settings), cur is self.settings
        self.stack.removeWidget(self.settings)
        self.settings.deleteLater()
        self.settings = Settings(self)
        self.stack.insertWidget(idx, self.settings)
        if was:
            self.stack.setCurrentWidget(self.settings)
        self.update()
        self.toast("Theme: " + t.get("label", tid))

    def theme_from_wallpaper(self):
        start = os.path.expanduser("~/Pictures/wallpapers")
        path, _ = QFileDialog.getOpenFileName(self, "Pick a wallpaper", start if os.path.isdir(start) else os.path.expanduser("~"),
                                              "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            pal = palette_from_image(path)
            if pal:
                self.set_theme("custom", pal)
            else:
                self.toast("Could not read that image.")

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
            self.term.say("catalog: " + m + "\n", "#8a8f99")
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
        elif key == "windows":
            self.stack.setCurrentWidget(self.windows_page)
            self.windows_page.build()
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
        pg.heading("Updates", "One button updates the system, the AUR and Flatpak together. The terminal below shows everything that happens.", "KEEP IT FRESH")
        b = QPushButton("⟳  Update everything now")
        b.setObjectName("primary")
        b.setFixedWidth(280)
        b.clicked.connect(self.full_update)
        pg.v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        pg.v.addSpacing(8)
        pg.v.addWidget(lbl("Checking what can be updated…", "muted"))
        pg.end()
        def done(res):
            if self.stack.currentWidget() is not pg:
                return
            pg.clear()
            pg.heading("Updates", "One button updates the system, the AUR and Flatpak together. The terminal below shows everything that happens.", "KEEP IT FRESH")
            b2 = QPushButton("⟳  Update everything now")
            b2.setObjectName("primary")
            b2.setFixedWidth(280)
            b2.clicked.connect(self.full_update)
            pg.v.addWidget(b2, 0, Qt.AlignmentFlag.AlignLeft)
            if isinstance(res, Exception):
                pg.v.addWidget(lbl(f"Could not check for updates: {res}", "muted", wrap=True))
            else:
                total = sum(len(v) for v in res.values())
                pg.v.addSpacing(6)
                pg.v.addWidget(lbl("Everything is up to date." if not total else f"{total} updates available", "h2"))
                for title, key in (("System packages", "pacman"), ("AUR packages", "aur"), ("Flatpak apps", "flatpak")):
                    items = res.get(key, [])
                    if items:
                        pg.v.addWidget(lbl(f"{title}  ·  {len(items)}", "h3"))
                        box = lbl("\n".join(items[:60]) + ("\n…" if len(items) > 60 else ""), "dim")
                        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                        box.setStyleSheet("font-family: monospace; font-size: 12px;")
                        pg.v.addWidget(box)
                self._updates = res
                self.nav["updates"].setText("⬆   Updates" + (f"  ({total})" if total else ""))
            pg.end()
        spawn(bk.list_updates, on_done=done)

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
        self.render_results(q, ms)

    def render_results(self, q, ms=0):
        """Source chips are multi-select toggles (remembered); results show only apps offered by a selected source."""
        while self.pillbar.count():
            w = self.pillbar.takeAt(0).widget()
            if w:
                w.deleteLater()
        counts = {s: sum(1 for a in self._results if s in a.items) for s in bk.SOURCES}
        lab = lbl("SOURCES", "step")
        self.pillbar.addWidget(lab)
        for s in bk.SOURCES:
            b = QPushButton(f"{SRC_NAME[s]}  {counts[s]}")
            b.setObjectName("pill")
            b.setCheckable(True)
            b.setChecked(s in self._src_on)
            col = FRAME[s]
            b.setStyleSheet(f"QPushButton#pill:checked {{ border-color: {col}; color: {col}; background: {rgba(col, 0.12)}; }}")
            b.setToolTip("Click to include or leave out " + SRC_NAME[s])
            b.clicked.connect(lambda _, s=s: self.toggle_source(s, q))
            self.pillbar.addWidget(b)
        allb = QPushButton("All")
        allb.setObjectName("pill")
        allb.clicked.connect(lambda: self.set_sources(set(bk.SOURCES), q))
        self.pillbar.addWidget(allb)
        self.pillbar.addStretch(1)
        self.pill_w.show()
        on = self._src_on
        eff_order = tuple(s for s in self.order if s in on)
        apps = [a for a in self._results if any(s in on for s in a.items)][:80]
        pg = self.results
        pg.clear()
        shown = len(on) < len(bk.SOURCES)
        pg.heading(f"Results for “{q}”", f"{len(apps)} apps" + (f"  ·  {len(on)} of {len(bk.SOURCES)} sources" if shown else "") + (f"  ·  {ms:.0f} ms" if ms else ""), "SEARCH")
        if not apps:
            pg.message("Nothing found in the selected sources. Try selecting more of them above, or a more general word.")
        for a in apps:
            r = Row(a, eff_order)
            r.clicked.connect(self.open_app)
            pg.v.addWidget(r)
        pg.end()

    def toggle_source(self, s, q):
        on = set(self._src_on)
        on.symmetric_difference_update({s})
        self.set_sources(on or set(bk.SOURCES), q)

    def set_sources(self, on, q):
        self._src_on = set(on)
        self.cfg["sources"] = [s for s in bk.SOURCES if s in self._src_on]
        bk.save_config(self.cfg)
        self.render_results(q)

    # ---- updates
    def full_update(self):
        if self.term.busy:
            self.toast("Another task is running in the terminal. Wait for it or press Stop there.")
            self.term_btn.setChecked(True)
            return
        self.term_btn.setChecked(True)
        started = self.term.run(bk.full_update_argv(), "Updating everything",
                                on_finished=lambda code: (bk.refresh_installed(), self.refresh_update_count(), self.go("updates") if self.nav["updates"].isChecked() else None))
        if started:
            self.toast("Updating system, AUR and Flatpak… you will be asked for your password once or twice.")

    def refresh_update_count(self):
        def done(res):
            if isinstance(res, Exception):
                return
            n = sum(len(v) for v in res.values())
            self._updates = res
            self.nav["updates"].setText(f"⬆   Updates" + (f"  ({n})" if n else ""))
            self.update_btn.setText("⟳  Update everything" + (f"  ({n})" if n else ""))
        spawn(bk.list_updates, on_done=done)

    # ---- Windows apps (Wine / Proton)
    def install_windows(self, exe, runner="wine", add_only=False):
        if add_only:
            path = windows.add_to_menu(exe, runner=runner)
            bk.refresh_installed()
            self.toast("Added to the menu: " + os.path.basename(path))
            self.go("windows")
            return
        if self.cfg.get("auto_terminal", True):
            self.term_btn.setChecked(True)
        argv = windows.install_argv(exe, runner)
        def done(code):
            bk.refresh_installed()
            if windows.has_steam_shortcuts():
                self.term.run(windows.run_steam_shortcuts(), "Creating menu entries",
                              on_finished=lambda c: (bk.refresh_installed(), self.go("windows")))
            else:
                self.go("windows")
        self.term.run(argv, "Installing " + os.path.basename(exe), on_finished=done)
        self.toast("Installing " + os.path.basename(exe) + " with " + runner.title() + "…")

    def run_steam_shortcuts(self):
        if self.cfg.get("auto_terminal", True):
            self.term_btn.setChecked(True)
        self.term.run(windows.run_steam_shortcuts(), "Creating menu entries", on_finished=lambda c: (bk.refresh_installed(), self.go("windows")))

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
            if pkg.source == "wine" and remove:
                self.go("windows")
                return
            self.toast(("Removed " if remove else "Installed ") + pkg.label)
            if self.detail.pkg is pkg:
                self.detail.show_app(self.detail.app, pkg)
        else:
            self.toast("That did not work. The terminal below shows what happened.")


def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("sunshine-store")
    cfg0 = bk.load_config()
    tid = sys.argv[sys.argv.index("--theme") + 1] if "--theme" in sys.argv else cfg0.get("theme", themes.DEFAULT_THEME)
    apply_theme(theme_by_id(tid, cfg0.get("custom_theme")))
    app.setStyleSheet(build_qss())
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
            elif page == "updates":
                w.go("updates")
            elif page == "windows":
                w.go("windows")
            elif page == "windows-ask":
                w.go("windows")
                QTimer.singleShot(600, lambda: w.windows_page.ask("/home/user/Downloads/GameSetup.exe"))
            elif page == "terminal":
                w.term_btn.setChecked(True)
                w.term.view.clear()
                demo = ("printf 'Resolving dependencies...\\n'; for i in 10 40 70 100; do printf '\\r\\033[1m downloading\\033[0m [%3d%%]' $i; sleep 0.3; done; "
                        "printf '\\n\\033[32m::\\033[0m installing demo-package (1.0-1)\\n\\033[33mwarning:\\033[0m this is a coloured line\\n'; read -p 'Proceed? [Y/n] ' a; echo answered:$a")
                w.term.run(["bash", "-c", demo], "demo")
                QTimer.singleShot(3000, lambda: w.term.reader and (w.term.reader.send("y\n")))
            elif page in ("results", "detail", "popup"):
                w.search.setText(q)
                w.do_search()
        def detail():
            if page in ("detail", "popup"):
                rows = w.results.findChildren(Row)
                if rows:
                    w.open_app(rows[0].app)
                if page == "popup":
                    QTimer.singleShot(1200, w.detail.open_popup)
        def finish():
            pm = w.grab()
            pops = w.detail.findChildren(CommandPopup) if page == "popup" else []
            if pops:
                p = QPainter(pm)
                p.drawPixmap(w.mapFromGlobal(pops[-1].pos()), pops[-1].grab())
                p.end()
            pm.save(out)
            app.quit()
        QTimer.singleShot(300, setup)
        QTimer.singleShot(int(arg("--wait", "9000")) - 3000, detail)
        QTimer.singleShot(int(arg("--wait", "9000")), finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
