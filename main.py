"""GIF Clock v2 — часы на анимированных GIF-цифрах (PyQt6).

Файлы:  assets/digits/0.gif … 9.gif   — цифры
        assets/separator.gif          — разделитель (одна «точка», рисуется
                                        как ':' в времени и '.' в дате)
Запуск: python main.py
"""
import bisect
import json
import os
import sys
import time
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QIcon, QImage, QImageReader,
                         QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QComboBox, QDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget,
)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    _u32 = ctypes.windll.user32
    _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetWindow.restype = wintypes.HWND
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetClassNameW.restype = ctypes.c_int
    _u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u32.SetWindowPos.restype = wintypes.BOOL

    def _class_name(h):
        buf = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(h, buf, 64)
        return buf.value

    def _push_desktop_below(hwnd):
        """Если окна рабочего стола (Progman/WorkerW) оказались выше часов —
        например после «Свернуть все окна» — кладём их обратно под часы.
        Остальные окна приложений не трогаем: они остаются выше часов."""
        above, cur, n = [], _u32.GetWindow(hwnd, 3), 0     # 3 = GW_HWNDPREV
        while cur and n < 1000:
            if _class_name(cur) in ("Progman", "WorkerW"):
                above.append(cur)
            cur, n = _u32.GetWindow(cur, 3), n + 1
        for w in above:                 # NOMOVE | NOSIZE | NOACTIVATE
            _u32.SetWindowPos(w, hwnd, 0, 0, 0, 0, 0x0013)
        return len(above)


def _log(msg):
    """Диагностика встраивания: файл gifclock.log во временной папке Windows."""
    try:
        import tempfile
        with open(os.path.join(tempfile.gettempdir(), "gifclock.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    except OSError:
        pass


def resource_root():
    """Папка с assets: рядом с exe (можно подменять GIF) или внутри сборки."""
    if getattr(sys, "frozen", False):
        near_exe = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(near_exe, "assets")):
            return near_exe
        return getattr(sys, "_MEIPASS", near_exe)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = resource_root()
DIGIT_DIR = os.path.join(BASE_DIR, "assets", "digits")
SEP_PATH = os.path.join(BASE_DIR, "assets", "separator.gif")

DEFAULTS = {
    "gif_height": 100,    # высота одной цифры-GIF, px (ширина — по пропорциям)
    "date_scale": 50,     # размер даты относительно времени, %
    "spacing": 0,         # расстояние между цифрами, px
    "sep_scale": 16,      # диаметр точки-разделителя, % от высоты цифры
    "smooth": True,       # сглаживание при масштабировании
    "opacity": 100,       # непрозрачность всего окна, %
    "bg_alpha": 0,        # подложка за часами, % (0 = полностью прозрачно)
    "show_seconds": True,
    "show_date": False,
    "date_below": False,
    "mode": "desktop",    # desktop — под всеми окнами | embed (эксперимент.) | normal | top
    "unlocked": False,    # True — можно двигать мышкой / открывать меню по ПКМ
    "margin": 20,         # отступ от края экрана при выравнивании, px
    "anchor": None,
    "pos": None,
}

ALIGN = {  # ключ: (доля по X, доля по Y)
    "tl": (0, 0), "tc": (.5, 0), "tr": (1, 0),
    "ml": (0, .5), "c": (.5, .5), "mr": (1, .5),
    "bl": (0, 1), "bc": (.5, 1), "br": (1, 1),
}

_ALPHA_TABLE = bytes(1 if i > 8 else 0 for i in range(256))


# ------------------------------------------------------------ GIF-анимации
def _alpha_bbox(img):
    """Рамка непрозрачных пикселей (x0, y0, x1, y1) или None."""
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    alpha = bytes(ptr)[3::4].translate(_ALPHA_TABLE)
    rows = [y for y in range(h) if any(alpha[y * w:(y + 1) * w])]
    if not rows:
        return None
    cols = [x for x in range(w) if any(alpha[x::w])]
    return cols[0], rows[0], cols[-1] + 1, rows[-1] + 1


class Anim:
    """GIF, прочитанный целиком: кадры + задержки. Масштабируется под клетку
    строго пропорционально, одним коэффициентом для всех кадров."""

    def __init__(self, path, crop=False):
        self.frames, self.delays, self.ends = [], [], []
        self._cache = {}
        reader = QImageReader(path)
        while True:
            img = reader.read()
            if img.isNull():
                break
            d = reader.nextImageDelay()
            self.delays.append(d if d > 10 else 100)
            self.frames.append(img.convertToFormat(QImage.Format.Format_ARGB32))
        if crop and self.frames:       # обрезаем пустые поля (для разделителя)
            boxes = [b for b in map(_alpha_bbox, self.frames) if b]
            if boxes:
                x0 = min(b[0] for b in boxes)
                y0 = min(b[1] for b in boxes)
                x1 = max(b[2] for b in boxes)
                y1 = max(b[3] for b in boxes)
                self.frames = [f.copy(x0, y0, x1 - x0, y1 - y0) for f in self.frames]
        total = 0
        for d in self.delays:
            total += d
            self.ends.append(total)
        self.total = max(total, 1)
        self.ok = bool(self.frames)
        self.size = self.frames[0].size() if self.ok else QSize(45, 100)

    def index_at(self, ms):
        i = bisect.bisect_right(self.ends, ms % self.total)
        return min(i, len(self.frames) - 1)

    def pixmaps(self, w, h, smooth, dpr):
        key = (w, h, smooth, dpr)
        res = self._cache.get(key)
        if res is None:
            if len(self._cache) > 6:
                self._cache.clear()
            fw, fh = self.size.width(), self.size.height()
            s = min(w * dpr / fw, h * dpr / fh)
            tw, th = max(1, round(fw * s)), max(1, round(fh * s))
            mode = (Qt.TransformationMode.SmoothTransformation if smooth
                    else Qt.TransformationMode.FastTransformation)
            res = []
            for f in self.frames:
                pm = QPixmap.fromImage(
                    f.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, mode))
                pm.setDevicePixelRatio(dpr)
                res.append(pm)
            self._cache[key] = res
        return res


DIGITS = {}      # цифра -> Anim
SEP = None       # Anim разделителя или None
ASPECT = 0.45    # ширина/высота цифры


def load_animations():
    global SEP, ASPECT
    missing = []
    for d in range(10):
        a = Anim(os.path.join(DIGIT_DIR, f"{d}.gif"))
        if a.ok:
            DIGITS[d] = a
        else:
            missing.append(f"assets/digits/{d}.gif")
    if DIGITS:
        s = next(iter(DIGITS.values())).size
        ASPECT = s.width() / s.height()
    a = Anim(SEP_PATH, crop=True)
    SEP = a if a.ok else None
    return missing


# ------------------------------------------------------------------ виджеты
class DigitLabel(QWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.digit, self.anim, self.idx, self.smooth = None, None, 0, True

    def configure(self, w, h, smooth):
        self.smooth = smooth
        self.setFixedSize(w, h)
        self.update()

    def set_digit(self, d):
        if d != self.digit:
            self.digit = d
            self.anim = DIGITS.get(d)
            self.idx = 0
            self.update()

    def tick(self, ms):
        if self.anim:
            i = self.anim.index_at(ms)
            if i != self.idx:
                self.idx = i
                self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if self.anim:
            dpr = self.devicePixelRatioF()
            pm = self.anim.pixmaps(self.width(), self.height(), self.smooth, dpr)[self.idx]
            x = (self.width() - pm.width() / dpr) / 2
            y = (self.height() - pm.height() / dpr) / 2
            p.drawPixmap(QPointF(x, y), pm)
        elif self.digit is not None:      # запасной вариант — обычный текст
            f = p.font()
            f.setPixelSize(max(6, int(self.height() * 0.6)))
            p.setFont(f)
            p.setPen(QColor("white"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.digit))


class SepWidget(QWidget):
    """Разделитель из assets/separator.gif: ':' = две точки, '.' = одна."""

    def __init__(self, kind):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.kind, self.idx, self.smooth = kind, 0, True
        self.d = 8

    def configure(self, h, pct, smooth):
        self.smooth = smooth
        self.d = max(4, round(h * pct / 100))
        pad = max(2, round(self.d * 0.7))
        self.setFixedSize(self.d + 2 * pad, h)
        self.update()

    def tick(self, ms):
        if SEP:
            i = SEP.index_at(ms)
            if i != self.idx:
                self.idx = i
                self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, d = self.height(), self.d
        ys = (h * 0.33, h * 0.70) if self.kind == ":" else (h - d / 2 - h * 0.04,)
        cx = self.width() / 2
        if SEP:
            dpr = self.devicePixelRatioF()
            pm = SEP.pixmaps(d, d, self.smooth, dpr)[self.idx]
            for cy in ys:
                p.drawPixmap(QPointF(cx - pm.width() / dpr / 2, cy - pm.height() / dpr / 2), pm)
        else:                              # нет файла — белые кружки
            p.setBrush(QColor("white"))
            p.setPen(Qt.PenStyle.NoPen)
            for cy in ys:
                p.drawEllipse(QRectF(cx - d / 2, cy - d / 2, d, d))


class DigitGroup(QWidget):
    """Шаблон: 'dd:dd:dd' — d цифра, остальное — разделитель."""

    def __init__(self, pattern):
        super().__init__()
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.items, self.digits = [], []
        self.pattern = None
        self.set_pattern(pattern)

    def set_pattern(self, pattern):
        if pattern == self.pattern:
            return
        self.pattern = pattern
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.items, self.digits = [], []
        for ch in pattern:
            if ch == "d":
                w = DigitLabel()
                self.digits.append(w)
            else:
                w = SepWidget(ch)
            self.items.append(w)
            self.lay.addWidget(w)

    def configure(self, h, spacing, sep_pct, smooth):
        w = max(4, round(h * ASPECT))
        self.lay.setSpacing(max(0, spacing))
        for it in self.items:
            if isinstance(it, DigitLabel):
                it.configure(w, h, smooth)
            else:
                it.configure(h, sep_pct, smooth)

    def set_digits(self, text):
        for lbl, ch in zip(self.digits, text):
            lbl.set_digit(int(ch))

    def tick(self, ms):
        for it in self.items:
            it.tick(ms)


# ------------------------------------------------------------- окно часов
class ClockWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.store = QSettings("GifClock", "GifClock")
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(self.store.value("cfg2", "{}")))
        except (ValueError, TypeError):
            pass
        if self.cfg.get("mode") not in ("desktop", "normal", "top"):
            self.cfg["mode"] = "desktop"      # старые/неизвестные режимы

        self.setWindowTitle("GIF Clock")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.time_group = DigitGroup("dd:dd:dd")
        self.date_group = DigitGroup("dd.dd.dddd")
        self.lay = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.lay.setContentsMargins(8, 6, 8, 6)
        self.lay.setSpacing(12)
        self.lay.setSizeConstraint(QBoxLayout.SizeConstraint.SetFixedSize)
        self.lay.addWidget(self.time_group, 0, Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(self.date_group, 0, Qt.AlignmentFlag.AlignCenter)

        self._drag = None
        self._flags_applied = None
        self.settings_dlg = None
        self._make_tray()

        self.apply()
        if self.cfg["pos"] and not self.cfg["anchor"]:
            self.move(*self.cfg["pos"])
        if not self._on_screen():             # сохранённая позиция вне экрана
            self.align("c")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)
        self.guard = QTimer(self)          # следит, чтобы рабочий стол был под часами
        self.guard.timeout.connect(self._zorder_guard)
        self.guard.start(250)

    def _on_screen(self):
        r = self.frameGeometry()
        return any(r.intersects(sc.availableGeometry()) for sc in QApplication.screens())

    def _zorder_guard(self):
        if sys.platform != "win32" or self.cfg["mode"] != "desktop":
            return
        try:
            n = _push_desktop_below(int(self.winId()))
            if n:
                _log(f"desktop windows were above the clock: {n}, pushed below")
        except Exception as e:
            _log(f"zorder guard error: {e!r}")

    # --- трей (единственный способ управлять окном, пока оно «заблокировано»)
    def _make_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if DIGITS:
            pm = next(iter(DIGITS.values())).pixmaps(32, 32, True, 1.0)[0]
        else:
            pm = QPixmap(32, 32)
            pm.fill(QColor("steelblue"))
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        self.tray.setToolTip("GIF Clock")
        menu = QMenu()
        menu.addAction("Настройки…", self.open_settings)
        self.act_unlock = QAction("Разрешить перемещение мышкой", menu, checkable=True)
        self.act_unlock.toggled.connect(lambda v: self.set_option("unlocked", v))
        menu.addAction(self.act_unlock)
        menu.addSeparator()
        menu.addAction("Выход", QApplication.instance().quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and self.open_settings())
        QApplication.instance().aboutToQuit.connect(self.tray.hide)
        self.tray.show()

    # --- обновление
    def tick(self):
        now = datetime.now()
        ms = time.monotonic() * 1000
        self.time_group.set_digits(now.strftime("%H%M%S" if self.cfg["show_seconds"] else "%H%M"))
        self.time_group.tick(ms)
        if self.cfg["show_date"]:
            self.date_group.set_digits(now.strftime("%d%m%Y"))
            self.date_group.tick(ms)

    def _flags(self):
        W = Qt.WindowType
        f = W.FramelessWindowHint | W.Tool | W.WindowDoesNotAcceptFocus
        mode = self.cfg["mode"]
        if mode == "top":
            f |= W.WindowStaysOnTopHint
        if not self.cfg["unlocked"] and self.tray is not None:
            f |= W.WindowTransparentForInput      # клики проходят насквозь
        return f

    def apply(self):
        c = self.cfg
        h = max(10, int(c["gif_height"]))
        self.time_group.set_pattern("dd:dd:dd" if c["show_seconds"] else "dd:dd")
        self.time_group.configure(h, c["spacing"], c["sep_scale"], c["smooth"])
        hd = max(8, round(h * c["date_scale"] / 100))
        self.date_group.configure(hd, round(c["spacing"] * c["date_scale"] / 100),
                                  c["sep_scale"], c["smooth"])
        self.date_group.setVisible(c["show_date"])
        self.lay.setDirection(QBoxLayout.Direction.TopToBottom if c["date_below"]
                              else QBoxLayout.Direction.LeftToRight)
        self.lay.activate()
        self.adjustSize()

        flags = self._flags()
        if flags != self._flags_applied:
            was_visible = self._flags_applied is not None
            pos = self.pos()
            self._flags_applied = flags
            self.setWindowFlags(flags)
            self.show()
            if was_visible:
                self.move(pos)
        self.setWindowOpacity(c["opacity"] / 100)
        if c["anchor"]:
            self.align(c["anchor"], save=False)
        self.tick()
        self.update()

    def set_option(self, key, value):
        self.cfg[key] = value
        self.apply()
        self.save()
        self.refresh_ui()

    def reset(self):
        self.cfg = dict(DEFAULTS)
        self.apply()
        self.save()
        self.refresh_ui()

    def save(self):
        self.store.setValue("cfg2", json.dumps(self.cfg))

    def refresh_ui(self):
        if self.tray is not None:
            self.act_unlock.blockSignals(True)
            self.act_unlock.setChecked(bool(self.cfg["unlocked"]))
            self.act_unlock.blockSignals(False)
        if self.settings_dlg is not None:
            self.settings_dlg.sync()

    # --- позиционирование (9 точек)
    def align(self, where, save=True):
        screen = self.screen() or QApplication.primaryScreen()
        g = screen.availableGeometry()
        m = self.cfg["margin"]
        fx, fy = ALIGN[where]
        x = g.left() + m + (g.width() - self.width() - 2 * m) * fx
        y = g.top() + m + (g.height() - self.height() - 2 * m) * fy
        self.move(round(x), round(y))
        if save:
            self.cfg["anchor"], self.cfg["pos"] = where, None
            self.save()

    # --- отрисовка подложки и перетаскивание
    def paintEvent(self, _):
        c = self.cfg
        alpha = round(c["bg_alpha"] * 2.55)
        if c["unlocked"]:
            alpha = max(alpha, 25)   # чтобы окно ловило мышь (Windows)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if alpha:
            p.setBrush(QColor(0, 0, 0, alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        if c["unlocked"]:            # рамка-подсказка в режиме перемещения
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 140), 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)

    def mousePressEvent(self, e):
        if self.cfg["unlocked"] and e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.cfg["anchor"], self.cfg["pos"] = None, [self.x(), self.y()]
            self.save()

    def mouseDoubleClickEvent(self, e):
        if self.cfg["unlocked"]:
            self.open_settings()

    def contextMenuEvent(self, e):
        if not self.cfg["unlocked"]:
            return
        menu = QMenu(self)
        menu.addAction("Настройки…", self.open_settings)
        menu.addSeparator()
        menu.addAction("Выход", QApplication.instance().quit)
        menu.exec(e.globalPos())

    def open_settings(self):
        if self.settings_dlg is None:
            self.settings_dlg = SettingsDialog(self)
        self.settings_dlg.sync()
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()


# ------------------------------------------------------------ окно настроек
class SettingsDialog(QDialog):
    def __init__(self, clock):
        super().__init__(None)      # без родителя: не зависит от флагов часов
        self.clock = clock
        self.setWindowTitle("Настройки часов")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.sliders, self.checks = {}, {}
        root = QVBoxLayout(self)

        box = QGroupBox("Размер")
        form = QFormLayout(box)
        form.addRow("Размер GIF-цифр (высота), px", self._slider("gif_height", 20, 600))
        form.addRow("Размер даты, % от времени", self._slider("date_scale", 20, 100))
        form.addRow("Размер точки-разделителя, %", self._slider("sep_scale", 5, 60))
        form.addRow("Расстояние между цифрами, px", self._slider("spacing", 0, 60))
        self._check(form, "smooth", "Сглаживание при масштабировании")
        root.addWidget(box)

        box = QGroupBox("Положение на экране")
        grid = QGridLayout(box)
        names = {"tl": "↖ Верх-лево", "tc": "↑ Верх-центр", "tr": "↗ Верх-право",
                 "ml": "← Центр-лево", "c": "⦿ Центр", "mr": "→ Центр-право",
                 "bl": "↙ Низ-лево", "bc": "↓ Низ-центр", "br": "↘ Низ-право"}
        for i, key in enumerate(ALIGN):
            b = QPushButton(names[key])
            b.clicked.connect(lambda _=False, k=key: clock.align(k))
            grid.addWidget(b, i // 3, i % 3)
        form2 = QFormLayout()
        form2.addRow("Отступ от края, px", self._slider("margin", 0, 300))
        grid.addLayout(form2, 3, 0, 1, 3)
        root.addWidget(box)

        box = QGroupBox("Вид и поведение")
        form = QFormLayout(box)
        self.mode = QComboBox()
        self.mode.addItem("Как виджет: над обоями, под окнами", "desktop")
        self.mode.addItem("Обычное окно", "normal")
        self.mode.addItem("Поверх всех окон", "top")
        self.mode.currentIndexChanged.connect(
            lambda _: clock.set_option("mode", self.mode.currentData()))
        form.addRow("Уровень окна", self.mode)
        form.addRow("Непрозрачность, %", self._slider("opacity", 10, 100))
        form.addRow("Подложка за часами, %", self._slider("bg_alpha", 0, 100))
        self._check(form, "show_seconds", "Показывать секунды")
        self._check(form, "show_date", "Показывать дату")
        self._check(form, "date_below", "Дата под временем (иначе справа)")
        self._check(form, "unlocked", "Разрешить перемещение мышкой (разблокировать)")
        root.addWidget(box)

        hint = QLabel("Когда перемещение запрещено, клики проходят сквозь часы.\n"
                      "Настройки и «Выход» — в значке в трее.")
        hint.setStyleSheet("color: gray;")
        root.addWidget(hint)

        row = QHBoxLayout()
        b = QPushButton("Сбросить")
        b.clicked.connect(clock.reset)
        row.addWidget(b)
        b = QPushButton("Выход из программы")
        b.clicked.connect(QApplication.instance().quit)
        row.addWidget(b)
        root.addLayout(row)

    def _slider(self, key, lo, hi):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setMinimumWidth(190)
        lbl = QLabel()
        lbl.setMinimumWidth(32)

        def changed(v):
            lbl.setText(str(v))
            self.clock.set_option(key, v)
        s.valueChanged.connect(changed)
        self.sliders[key] = (s, lbl)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(s)
        h.addWidget(lbl)
        return w

    def _check(self, form, key, text):
        cb = QCheckBox(text)
        cb.toggled.connect(lambda v: self.clock.set_option(key, v))
        self.checks[key] = cb
        form.addRow(cb)

    def sync(self):
        cfg = self.clock.cfg
        for key, (s, lbl) in self.sliders.items():
            s.blockSignals(True)
            s.setValue(int(cfg[key]))
            s.blockSignals(False)
            lbl.setText(str(int(cfg[key])))
        for key, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(bool(cfg[key]))
            cb.blockSignals(False)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(cfg["mode"])))
        self.mode.blockSignals(False)


def _excepthook(t, v, tb):
    import traceback
    _log("ERROR " + "".join(traceback.format_exception(t, v, tb)))   # не закрываем программу


def main():
    sys.excepthook = _excepthook
    if "--reset" in sys.argv:
        QSettings("GifClock", "GifClock").clear()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    missing = load_animations()
    if missing:
        QMessageBox.warning(None, "GIF Clock",
                            "Не найдены файлы (цифры будут показаны текстом):\n" +
                            "\n".join(missing))
    clock = ClockWindow()
    first_run = clock.cfg["pos"] is None and clock.cfg["anchor"] is None
    if first_run:
        clock.cfg["unlocked"] = True       # при первом запуске можно двигать
        clock.apply()
        clock.align("c")
    clock.refresh_ui()
    clock.open_settings()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
