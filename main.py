"""GIF Clock v3 — часы на анимированных GIF-цифрах (PyQt6).

Файлы:  assets/digits/0.gif … 9.gif   — цифры
        assets/separator.gif          — разделитель (одна точка; в времени
                                        рисуется как ':', в дате как '.')
Запуск: python main.py        (--settings — открыть настройки, --reset — сброс)

Архитектура:
  * Слой окна: часы всегда ниже ВСЕХ обычных окон и прямо над рабочим столом.
  * Таймер времени (clock_timer) работает всегда и не зависит от видимости.
  * Таймер GIF-кадров (anim_timer) ставится на паузу, когда часы закрыты
    другим окном / полноэкранным приложением, и мгновенно возобновляется.
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
    QApplication, QBoxLayout, QCheckBox, QDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QSlider,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)


# ============================================================ служебное
def _log(msg):
    """Журнал: файл gifclock.log во временной папке Windows (%TEMP%)."""
    try:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "gifclock.log")
        mode = "w" if os.path.exists(path) and os.path.getsize(path) > 200_000 else "a"
        with open(path, mode, encoding="utf-8") as f:
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
    "opacity": 100,       # непрозрачность окна, %
    "bg_alpha": 0,        # подложка за часами, % (0 = полностью прозрачно)
    "show_seconds": True,
    "show_date": False,
    "date_below": False,  # False — дата справа от времени, True — под временем
    "pause_hidden": True, # ставить GIF на паузу, когда часы не видны
    "unlocked": False,    # True — можно двигать мышкой / ПКМ-меню
    "margin": 20,         # отступ от края экрана при выравнивании, px
    "anchor": None,
    "pos": None,
}

ALIGN = {  # ключ: (доля по X, доля по Y)
    "tl": (0, 0), "tc": (.5, 0), "tr": (1, 0),
    "ml": (0, .5), "c": (.5, .5), "mr": (1, .5),
    "bl": (0, 1), "bc": (.5, 1), "br": (1, 1),
}


# ============================================================ Windows API
if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    _u32 = ctypes.windll.user32
    _dwm = ctypes.windll.dwmapi
    _k32 = ctypes.windll.kernel32
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                       wintypes.LONG, wintypes.LONG, wintypes.DWORD,
                                       wintypes.DWORD)

    _u32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]
    _u32.EnumWindows.restype = wintypes.BOOL
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetClassNameW.restype = ctypes.c_int
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]
    _u32.IsWindowVisible.restype = wintypes.BOOL
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.IsIconic.restype = wintypes.BOOL
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.ShowWindow.restype = wintypes.BOOL
    _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.GetWindowRect.restype = wintypes.BOOL
    _u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u32.SetWindowPos.restype = wintypes.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
                                     _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD]
    _u32.SetWinEventHook.restype = wintypes.HANDLE
    _u32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    _u32.UnhookWinEvent.restype = wintypes.BOOL
    _get_long = getattr(_u32, "GetWindowLongPtrW", None) or _u32.GetWindowLongW
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_long.restype = ctypes.c_ssize_t

    _DESK = ("Progman", "WorkerW")              # окна рабочего стола
    _SHELL = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
              "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland")
    _NOMOVE_NOSIZE_NOACTIVATE = 0x0001 | 0x0002 | 0x0010

    def _class_name(h):
        buf = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(h, buf, 64)
        return buf.value

    def _is_own_window(h):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value == os.getpid()

    def _is_cloaked(h):
        v = wintypes.DWORD()
        try:
            _dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(v), 4)   # DWMWA_CLOAKED
        except OSError:
            return False
        return v.value != 0

    def _ensure_shown(hwnd):
        """Если Windows спрятала/свернула окно — показать без активации."""
        if not _u32.IsWindowVisible(hwnd) or _u32.IsIconic(hwnd):
            _u32.ShowWindow(hwnd, 4)            # SW_SHOWNOACTIVATE
            return True
        return False

    def _layer_check(hwnd):
        """Строгий слой: [обычные окна] > ЧАСЫ > рабочий стол.
        Возвращает (исправлен_ли_порядок, закрыты_ли_часы_другим_окном)."""
        order = []

        def cb(h, _):
            order.append(h)
            return True
        _u32.EnumWindows(_ENUMPROC(cb), 0)      # окна сверху вниз
        if hwnd not in order:
            return False, False
        us = order.index(hwnd)
        desk = [h for h in order if _class_name(h) in _DESK]
        dset, fixed = set(desk), False
        if desk:
            nxt = order[us + 1] if us + 1 < len(order) else None
            if order.index(desk[0]) < us or nxt not in dset:
                # часы — на самое дно, затем рабочий стол кладём прямо под них
                _u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, _NOMOVE_NOSIZE_NOACTIVATE)  # HWND_BOTTOM
                after = hwnd
                for d in desk:
                    _u32.SetWindowPos(d, after, 0, 0, 0, 0, _NOMOVE_NOSIZE_NOACTIVATE)
                    after = d
                fixed = True
        above = ([h for h in order if h != hwnd and h not in dset] if fixed
                 else order[:us])
        # закрыты ли часы целиком каким-нибудь видимым окном выше
        cr = wintypes.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(cr))
        wr = wintypes.RECT()
        for h in above:
            if not _u32.IsWindowVisible(h) or _u32.IsIconic(h):
                continue
            if _get_long(h, -20) & (0x20 | 0x80):   # TRANSPARENT | TOOLWINDOW — оверлеи
                continue
            _u32.GetWindowRect(h, ctypes.byref(wr))
            if not (wr.left <= cr.left and wr.top <= cr.top and
                    wr.right >= cr.right and wr.bottom >= cr.bottom):
                continue
            if _class_name(h) in _SHELL or _is_cloaked(h) or _is_own_window(h):
                continue
            return fixed, True
        return fixed, False

    def _opt_out_power_throttling():
        """Windows 11 может «душить» таймеры фоновых программ — отключаем."""
        try:
            class _STATE(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                            ("StateMask", wintypes.ULONG)]
            st = _STATE(1, 0x1 | 0x4, 0)     # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION
            _k32.GetCurrentProcess.restype = wintypes.HANDLE
            _k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
            _k32.SetProcessInformation(_k32.GetCurrentProcess(), 4,
                                       ctypes.byref(st), ctypes.sizeof(st))
        except Exception:
            pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "GifClock")
        return True
    except OSError:
        return False


def set_autostart(on):
    if sys.platform != "win32":
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "GifClock", 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, "GifClock")
                except OSError:
                    pass
    except OSError as e:
        _log(f"autostart error: {e!r}")


# ============================================================ GIF-анимации
_ALPHA_TABLE = bytes(1 if i > 8 else 0 for i in range(256))
ANIM = {"ms": 0.0}      # «время анимации»: замирает, пока GIF на паузе


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


# ================================================================ виджеты
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
            self.idx = self.anim.index_at(ANIM["ms"]) if self.anim else 0
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


# ============================================================ окно часов
class ClockWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.store = QSettings("GifClock", "GifClock")
        self.cfg = dict(DEFAULTS)
        try:
            saved = json.loads(self.store.value("cfg2", "{}"))
            self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (ValueError, TypeError, AttributeError):
            pass

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
        self._anim_paused = False
        self._last_tick = None
        self._hooks, self._hook_cb = [], None

        # 1) ТАЙМЕР ВРЕМЕНИ — работает всегда, не зависит от видимости окна
        self.clock_timer = QTimer(self)
        self.clock_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.clock_timer.timeout.connect(self.update_time)
        # 2) ТАЙМЕР GIF-КАДРОВ — ставится на паузу, когда часы не видны
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.tick_frames)
        # 3) Слежение за слоем окна и видимостью
        self.guard = QTimer(self)
        self.guard.timeout.connect(self._guard)
        self.guard_soon = QTimer(self)      # быстрый запуск по событиям Windows
        self.guard_soon.setSingleShot(True)
        self.guard_soon.timeout.connect(self._guard)
        self._refresh_timer = QTimer(self)  # пересоздание окна после смены экрана/сна
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_window)

        self._make_tray()
        self.apply()
        if self.cfg["pos"] and not self.cfg["anchor"]:
            self.move(*self.cfg["pos"])
        if not self._on_screen():             # сохранённая позиция вне экрана
            self.align("c")

        self.clock_timer.start(100)
        self.anim_timer.start(33)
        self.guard.start(250)

        app = QApplication.instance()
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        for sc in app.screens():
            sc.geometryChanged.connect(self._screens_changed)
        self._install_win_hooks()
        app.aboutToQuit.connect(self._remove_win_hooks)

    # --- события Windows: мгновенная реакция на Win+D, сворачивание, смену окна
    def _install_win_hooks(self):
        if sys.platform != "win32":
            return
        try:
            def cb(hook, event, hwnd, id_obj, id_child, thread, ms):
                try:
                    self.guard_soon.start(20)
                except Exception:
                    pass
            self._hook_cb = _WINEVENTPROC(cb)
            # FOREGROUND, MINIMIZESTART..MINIMIZEEND, DESKTOPSWITCH; свои процессы пропускаем
            for lo, hi in ((0x0003, 0x0003), (0x0016, 0x0017), (0x0020, 0x0020)):
                h = _u32.SetWinEventHook(lo, hi, None, self._hook_cb, 0, 0, 0x0002)
                if h:
                    self._hooks.append(h)
        except Exception as e:
            _log(f"hooks error: {e!r}")

    def _remove_win_hooks(self):
        if sys.platform != "win32":
            return
        for h in self._hooks:
            _u32.UnhookWinEvent(h)
        self._hooks = []

    # --- слой, видимость, пауза анимации
    def _guard(self):
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            restored = _ensure_shown(hwnd)
            fixed, covered = _layer_check(hwnd)
            if restored or fixed:
                _log(f"layer restored (shown={restored}, reordered={fixed})")
                self._kick()
            self._set_anim_paused(bool(covered and self.cfg["pause_hidden"]))
        except Exception as e:
            _log(f"guard error: {e!r}")

    def _set_anim_paused(self, paused):
        """Пауза/возобновление ТОЛЬКО GIF-кадров. Время не затрагивается."""
        if paused == self._anim_paused:
            return
        self._anim_paused = paused
        if paused:
            self.anim_timer.stop()
        else:
            self.tick_frames()            # мгновенно показать актуальный кадр
            self.anim_timer.start(33)
            self._kick()

    def _kick(self):
        """Принудительная перерисовка (вместо «ресайза окна вручную»)."""
        self.update()
        for g in (self.time_group, self.date_group):
            for it in g.items:
                it.update()
        self.repaint()

    def _on_screen(self):
        r = self.frameGeometry()
        return any(r.intersects(sc.availableGeometry()) for sc in QApplication.screens())

    def _screens_changed(self, *_):
        self._refresh_timer.start(800)

    def _refresh_window(self):
        """Пересоздать окно: лечит застывшую картинку после сна ПК, смены
        разрешения или отключения монитора."""
        try:
            pos = self.pos()
            self._flags_applied = self._flags()
            self.setWindowFlags(self._flags_applied)
            self.show()
            self.move(pos)
            if self.cfg["anchor"]:
                self.align(self.cfg["anchor"], save=False)
            elif not self._on_screen():
                self.align("c")
            self._guard()
            self._kick()
            _log("window refreshed")
        except Exception as e:
            _log(f"refresh error: {e!r}")

    # --- трей (единственный способ управлять окном, пока оно «заблокировано»)
    def _make_tray(self):
        self.tray = None
        self._tray_timer = QTimer(self)
        self._tray_timer.timeout.connect(self._retry_tray)
        if not self._create_tray():
            self._tray_timer.start(2000)   # при автозапуске трей может появиться позже

    def _retry_tray(self):
        if self._create_tray():
            self._tray_timer.stop()
            self.apply()
            self.refresh_ui()

    def _create_tray(self):
        if self.tray is not None:
            return True
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
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
        return True

    # --- время и кадры
    def update_time(self):
        now = datetime.now()
        mono = time.monotonic()
        if self._last_tick is not None and mono - self._last_tick > 3:
            _log(f"clock gap {mono - self._last_tick:.1f}s (sleep/throttle) -> refresh")
            self._refresh_timer.start(300)
        self._last_tick = mono
        self.time_group.set_digits(now.strftime("%H%M%S" if self.cfg["show_seconds"] else "%H%M"))
        if self.cfg["show_date"]:
            self.date_group.set_digits(now.strftime("%d%m%Y"))

    def tick_frames(self):
        ANIM["ms"] = time.monotonic() * 1000
        self.time_group.tick(ANIM["ms"])
        if self.cfg["show_date"]:
            self.date_group.tick(ANIM["ms"])

    # --- окно: флаги и применение настроек
    def _flags(self):
        W = Qt.WindowType
        f = W.FramelessWindowHint | W.Tool | W.WindowDoesNotAcceptFocus
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
            self._guard()                 # сразу опустить окно на слой рабочего стола
        self.setWindowOpacity(c["opacity"] / 100)
        if c["anchor"]:
            self.align(c["anchor"], save=False)
        self.update_time()
        if not self._anim_paused:
            self.tick_frames()
        self._kick()

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
            self.guard_soon.start(20)    # на случай, если клик что-то подвинул

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


# ========================================================== окно настроек
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
        form.addRow("Непрозрачность, %", self._slider("opacity", 10, 100))
        form.addRow("Подложка за часами, %", self._slider("bg_alpha", 0, 100))
        self._check(form, "show_seconds", "Показывать секунды")
        self._check(form, "show_date", "Показывать дату")
        self._check(form, "date_below", "Дата под временем (иначе справа)")
        self._check(form, "unlocked", "Разрешить перемещение мышкой (разблокировать)")
        self._check(form, "pause_hidden",
                    "Останавливать GIF, когда часы закрыты окном / полный экран")
        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.autostart.toggled.connect(set_autostart)
        if sys.platform == "win32":
            form.addRow(self.autostart)
        root.addWidget(box)

        hint = QLabel("Часы всегда лежат под обычными окнами, на уровне рабочего стола.\n"
                      "Когда перемещение запрещено, клики проходят сквозь часы.\n"
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
        self.autostart.blockSignals(True)
        self.autostart.setChecked(autostart_enabled())
        self.autostart.blockSignals(False)


def _excepthook(t, v, tb):
    import traceback
    _log("ERROR " + "".join(traceback.format_exception(t, v, tb)))   # не закрываем программу


def main():
    sys.excepthook = _excepthook
    if sys.platform == "win32":
        _opt_out_power_throttling()
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
    if first_run or "--settings" in sys.argv:   # дальше запускаются только часы
        clock.open_settings()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
