"""Ratty helpers: key names, Windows window/screen helpers, image comparison."""
import base64
import ctypes
import io
import os
import sys

from PIL import Image, ImageChops, ImageStat
from pynput.keyboard import Key, KeyCode

WIN = sys.platform == "win32"
MODIFIERS = ("Key.ctrl", "Key.alt", "Key.shift", "Key.cmd")


def set_dpi_aware():
    if not WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------- keys ----------
def key_to_str(k):
    """Exact key id used inside recorded macros."""
    if isinstance(k, Key):
        return "Key." + k.name
    if isinstance(k, KeyCode):
        if k.char is not None:
            return "c:" + k.char
        if k.vk is not None:
            return "vk:" + str(k.vk)
    return None


def str_to_key(s):
    if s.startswith("Key."):
        return Key[s[4:]]
    if s.startswith("c:"):
        return KeyCode.from_char(s[2:])
    if s.startswith("vk:"):
        return KeyCode.from_vk(int(s[3:]))
    raise ValueError(s)


def hk_token(k):
    """Normalized key id for hotkeys (left/right modifiers merged, layout-independent)."""
    if isinstance(k, Key):
        name = k.name
        for m in ("ctrl", "alt", "shift", "cmd"):
            if name == m or name.startswith(m + "_"):
                return "Key." + m
        return "Key." + name
    if isinstance(k, KeyCode):
        if k.vk is not None:
            return f"vk:{k.vk}"
        if k.char:
            return "c:" + k.char.lower()
    return None


def token_label(t):
    if t.startswith("Key."):
        n = t[4:]
        special = {"esc": "Esc", "cmd": "Win", "page_up": "PgUp", "page_down": "PgDn",
                   "caps_lock": "CapsLock", "num_lock": "NumLock", "scroll_lock": "ScrollLock",
                   "print_screen": "PrtSc"}
        if n in special:
            return special[n]
        if n[0] == "f" and n[1:].isdigit():
            return n.upper()
        return n.replace("_", " ").title()
    if t.startswith("vk:"):
        v = int(t[3:])
        if 48 <= v <= 57 or 65 <= v <= 90:
            return chr(v)
        if 96 <= v <= 105:
            return f"Num{v - 96}"
        return f"VK{v}"
    if t.startswith("c:"):
        return t[2:].upper()
    if t.startswith("mouse:"):
        return {"left": "Mouse Left", "right": "Mouse Right", "middle": "Mouse Middle",
                "x1": "Mouse 4", "x2": "Mouse 5"}.get(t[6:], "Mouse " + t[6:])
    return t


def combo_label(combo):
    if not combo:
        return "(none)"

    def order(t):
        if t in MODIFIERS:
            return (0, MODIFIERS.index(t))
        if t.startswith("mouse:"):
            return (2, t)
        return (1, t)
    return "+".join(token_label(t) for t in sorted(combo, key=order))


def keystr_label(s):
    if s.startswith("c:"):
        return repr(s[2:])
    if s.startswith("Key."):
        return s[4:].replace("_", " ").title()
    return s


# ---------- Windows API ----------
if WIN:
    from ctypes import wintypes as wt
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _g = ctypes.WinDLL("gdi32")
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _u.GetDC.restype = wt.HDC
    _u.GetDC.argtypes = [wt.HWND]
    _u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _u.GetSystemMetrics.argtypes = [ctypes.c_int]
    _u.WindowFromPoint.restype = wt.HWND
    _u.WindowFromPoint.argtypes = [wt.POINT]
    _u.GetAncestor.restype = wt.HWND
    _u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    _u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _u.GetWindowTextLengthW.argtypes = [wt.HWND]
    _u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _u.IsWindowVisible.argtypes = [wt.HWND]
    _u.IsIconic.argtypes = [wt.HWND]
    _ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _u.EnumWindows.argtypes = [_ENUMPROC, wt.LPARAM]
    _g.GetPixel.restype = wt.DWORD
    _g.GetPixel.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _g.CreateCompatibleDC.restype = wt.HDC
    _g.CreateCompatibleDC.argtypes = [wt.HDC]
    _g.CreateCompatibleBitmap.restype = wt.HBITMAP
    _g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _g.SelectObject.restype = wt.HGDIOBJ
    _g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    _g.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
    _g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                             ctypes.c_void_p, wt.UINT]
    _g.DeleteObject.argtypes = [wt.HGDIOBJ]
    _g.DeleteDC.argtypes = [wt.HDC]
    _k.OpenProcess.restype = wt.HANDLE
    _k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _k.CloseHandle.argtypes = [wt.HANDLE]

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class _BMI(ctypes.Structure):
        _fields_ = [("h", _BMIH), ("colors", wt.DWORD * 3)]


def virtual_screen():
    """(x, y, w, h) of the whole desktop across all monitors."""
    if WIN:
        return (_u.GetSystemMetrics(76), _u.GetSystemMetrics(77),
                _u.GetSystemMetrics(78), _u.GetSystemMetrics(79))
    from PIL import ImageGrab
    im = ImageGrab.grab()
    return (0, 0, im.width, im.height)


def pixel_at(x, y):
    if not WIN:
        return None
    hdc = _u.GetDC(None)
    try:
        c = _g.GetPixel(hdc, x, y)
    finally:
        _u.ReleaseDC(None, hdc)
    if c == 0xFFFFFFFF:
        return None
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


def grab(rect):
    """Screenshot of rect (l, t, r, b) in screen pixels as an RGB PIL image."""
    l, t, r, b = [int(v) for v in rect]
    w, h = max(1, r - l), max(1, b - t)
    if not WIN:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(l, t, l + w, t + h)).convert("RGB")
    hdc = _u.GetDC(None)
    mdc = _g.CreateCompatibleDC(hdc)
    bmp = _g.CreateCompatibleBitmap(hdc, w, h)
    old = _g.SelectObject(mdc, bmp)
    try:
        _g.BitBlt(mdc, 0, 0, w, h, hdc, l, t, 0x00CC0020)
        bmi = _BMI()
        bmi.h.biSize = ctypes.sizeof(_BMIH)
        bmi.h.biWidth = w
        bmi.h.biHeight = -h
        bmi.h.biPlanes = 1
        bmi.h.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        _g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        _g.SelectObject(mdc, old)
        _g.DeleteObject(bmp)
        _g.DeleteDC(mdc)
        _u.ReleaseDC(None, hdc)
    return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)


def window_rect(hwnd):
    rc = wt.RECT()
    if _u.GetWindowRect(hwnd, ctypes.byref(rc)):
        return (rc.left, rc.top, rc.right, rc.bottom)
    return None


def _title(hwnd):
    n = _u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe(hwnd):
    pid = wt.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if _k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _k.CloseHandle(h)


def window_info_at(x, y):
    """Top-level window under a point: {exe, title, wx, wy} (its top-left corner)."""
    if not WIN:
        return None
    try:
        hwnd = _u.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hwnd:
            return None
        root = _u.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
        r = window_rect(root)
        if not r:
            return None
        return {"exe": _exe(root), "title": _title(root), "wx": r[0], "wy": r[1]}
    except Exception:
        return None


def find_window_origin(exe, title):
    """Current top-left of the matching window, or None."""
    if not WIN or not exe:
        return None
    found = []

    def cb(h, _):
        if _u.IsWindowVisible(h) and not _u.IsIconic(h) and _exe(h) == exe:
            found.append((h, _title(h)))
        return True
    _u.EnumWindows(_ENUMPROC(cb), 0)
    exact = [h for h, t in found if t == title]
    pick = exact[0] if exact else (found[0][0] if len(found) == 1 else None)
    r = window_rect(pick) if pick else None
    return (r[0], r[1]) if r else None


def set_startup(enabled, command):
    if not WIN:
        return
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            winreg.SetValueEx(key, "Ratty", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "Ratty")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ---------- image comparison ----------
def _maxdiff(a, b):
    d = ImageChops.difference(a, b)
    r, g, bl = d.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), bl)


def has_color(img, color, tol):
    """True if any pixel is within tol of color."""
    solid = Image.new("RGB", img.size, tuple(color))
    return _maxdiff(img, solid).getextrema()[0] <= tol


def has_changed(base, img, tol):
    if base.size != img.size:
        return True
    return _maxdiff(base, img).getextrema()[1] > tol


def looks_like(base, img, tol):
    if base.size != img.size:
        return False
    return ImageStat.Stat(_maxdiff(base, img)).mean[0] <= tol


def img_to_b64(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def b64_to_img(s):
    return Image.open(io.BytesIO(base64.b64decode(s))).convert("RGB")


def hex_color(c):
    return "#%02x%02x%02x" % tuple(c) if c else ""


def parse_hex(s):
    s = s.strip().lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
