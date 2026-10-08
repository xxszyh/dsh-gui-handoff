#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui-control —— 最小 MCP server（stdio / JSON-RPC 2.0），用于 Windows GUI 控制。

设计意图（不是玩具）：
    框架 gui-handoff-路径B 的「驱动」环需要一个能真正操作 GUI 的 MCP server。
    现成项目（Windows-MCP 等）会引入大量变量，无法干净地回答一个关键问题：

        「由 DSH 主进程启动的 MCP 子进程，能不能碰到 GUI？」

    所以这里只手写最小实现，依赖为零：
      - 协议：手写 JSON-RPC over stdio，不用 mcp SDK
      - GUI ：ctypes 直调 user32/gdi32，不起任何子进程
    这样一旦它通了，「沙箱能不能拦住 MCP 子进程」这个问题就有了确定答案，
    与第三方项目的复杂度无关。

工具：
    list_windows      列出可见顶层窗口（标题 / hwnd / 进程 / 位置 / 是否最小化）
    screen_info       屏幕尺寸与光标位置
    screenshot_window 截取指定窗口为 PNG（默认前台窗口）
    click             在屏幕坐标处点击

注意：
    stdout 只允许出现 JSON-RPC。任何调试输出必须走 stderr。
"""

import sys
import os
import json
import math
import time
import zlib
import struct
import ctypes
import ctypes.wintypes as wt

# ---------------------------------------------------------------------------
# 强制 UTF-8：Windows 上 stdout 被重定向时默认用本地代码页（cp936），
# 带中文的 JSON 会被编码坏掉。这是本机反复踩到的同一类坑。
# ---------------------------------------------------------------------------
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if sys.platform != "win32":
    raise SystemExit("gui-control requires Windows; use an equivalent driver on this OS.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.SetProcessDPIAware()  # 避免 DPI 缩放导致坐标错位

SERVER_NAME = "gui-control"
SERVER_VERSION = "0.1.1"
PROTOCOL_VERSION = "2024-11-05"

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


# ---------------------------------------------------------------------------
# Win32 辅助
# ---------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def _window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def list_windows():
    result = []
    proto = ENUM_WINDOWS_PROC

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        result.append({
            "hwnd": int(hwnd),
            "title": title,
            "pid": pid.value,
            "rect": [r.left, r.top, r.right, r.bottom],
            "size": [r.right - r.left, r.bottom - r.top],
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(proto(_cb), 0)
    return result


def _write_png(path, width, height, bgra_buffer):
    """把 GetDIBits 拿到的 BGRA 缓冲写成 PNG（零依赖，手写容器）。

    ⚠ 这里曾经有一个严重的 bug，务必不要再写成那样：
        PNG 的 color type 2 要求一行是【逐像素交错】的 R,G,B,R,G,B…
        我最初写成了三个平面（先是所有 R，再所有 G，再所有 B）。
        解码器按交错读三个平面，第 x 个像素就落到源图第 3x 个位置，
        于是画面变成【水平方向 3 个压缩副本】——周期恰好是宽度的 1/3。
        这个假象极具迷惑性：看起来像"抓图把三个窗口并排了"，
        实际是我自己的编码器写错了。且它曾被误判为视觉模型在胡说。
    """
    if width <= 0 or height <= 0 or len(bgra_buffer) != width * height * 4:
        raise ValueError("invalid BGRA dimensions or buffer length")
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        base = y * stride
        row = bgra_buffer[base:base + stride]
        raw.append(0)                    # PNG filter type 0
        px = bytearray(width * 3)
        px[0::3] = row[2::4]             # R
        px[1::3] = row[1::4]             # G
        px[2::3] = row[0::4]             # B
        raw.extend(px)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def screenshot_window(hwnd, path):
    if not hwnd:
        hwnd = user32.GetForegroundWindow()
    hwnd = wt.HWND(hwnd)
    if not user32.IsWindow(hwnd):
        raise ValueError("窗口句柄无效")
    if user32.IsIconic(hwnd):
        raise ValueError("窗口处于最小化状态，请先恢复窗口再截。")

    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise ValueError("窗口尺寸无效: %dx%d" % (w, h))

    hdc = user32.GetWindowDC(hwnd)
    mdc = bmp = old = None
    try:
        if not hdc:
            raise ctypes.WinError(ctypes.get_last_error())
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not mdc or not bmp:
            raise ctypes.WinError(ctypes.get_last_error())
        old = gdi32.SelectObject(mdc, bmp)
        if not old or old == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        # 先 PrintWindow 试（能抓被遮挡的窗口），失败再退回 BitBlt
        ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        if not ok:
            if not gdi32.BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY):
                raise ctypes.WinError(ctypes.get_last_error())

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h          # 负数 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        bufsize = w * h * 4
        buf = ctypes.create_string_buffer(bufsize)
        # GetDIBits requires the bitmap to be deselected from its DC.
        gdi32.SelectObject(mdc, old)
        old = None
        got = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if got != h:
            raise OSError("GetDIBits 未返回完整像素行")
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        _write_png(path, w, h, buf.raw)
    finally:
        if old and mdc:
            gdi32.SelectObject(mdc, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mdc:
            gdi32.DeleteDC(mdc)
        if hdc:
            user32.ReleaseDC(hwnd, hdc)

    return path


def _require_foreground(hwnd):
    if hwnd is not None:
        if not user32.IsWindow(hwnd) or user32.GetForegroundWindow() != hwnd:
            raise OSError("目标窗口已失去焦点；未发送输入。请重新确认目标窗口。")


def click(x, y, hwnd=None):
    _require_foreground(hwnd)
    if hwnd is not None:
        hit = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hit or user32.GetAncestor(hit, 2) != hwnd:  # GA_ROOT
            raise OSError("点击坐标不属于目标窗口；未发送输入。")
    if not user32.SetCursorPos(int(x), int(y)):
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(0.05)
    _require_foreground(hwnd)
    if hwnd is not None:
        hit = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hit or user32.GetAncestor(hit, 2) != hwnd:
            raise OSError("点击位置已被其他窗口覆盖；未发送输入。")
    events = []
    for flag in (0x0002, 0x0004):  # LEFTDOWN / LEFTUP
        event = INPUT()
        event.type = 0  # INPUT_MOUSE
        event.u.mi = MOUSEINPUT(0, 0, 0, flag, 0, 0)
        events.append(event)
    _send_input(events)
    return "已点击 (%d, %d)" % (int(x), int(y))


# ---------------------------------------------------------------------------
# 键盘输入：SendInput + KEYEVENTF_UNICODE
#
# 为什么不用 SendKeys / keybd_event：
#   它们走"虚拟键"通道，会经过输入法（IME）。在中文系统上实测，
#   注入 "typed by automation at" 被 IME 实时转成拼音候选，
#   实际落盘成 "typed不要automation爱他"（by→不要, at→爱他）。
#
#   KEYEVENTF_UNICODE 以 VK_PACKET 直送 Unicode 码元，绕过 IME 转换，
#   且不依赖当前键盘布局——这是自动化输入唯一可靠的方式。
# ---------------------------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# 这些虚拟键属于"扩展键"，发扫描码时必须带 EXTENDEDKEY，否则被当成小键盘
EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24,          # PageUp PageDown End Home
    0x25, 0x26, 0x27, 0x28,          # Left Up Right Down
    0x2D, 0x2E,                      # Insert Delete
    0x5B, 0x5C,                      # Win
    0x6F,                            # Divide
    0xA3, 0xA5,                      # Right Ctrl / Right Alt
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

# ctypes defaults to a 32-bit return type. HWND/HDC/HBITMAP are pointer-sized,
# so leaving these unspecified corrupts handles on 64-bit Windows.
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
for dll, function, arguments, result in [
    (user32, "EnumWindows", [ENUM_WINDOWS_PROC, wt.LPARAM], wt.BOOL),
    (user32, "IsWindowVisible", [wt.HWND], wt.BOOL),
    (user32, "IsWindow", [wt.HWND], wt.BOOL),
    (user32, "IsIconic", [wt.HWND], wt.BOOL),
    (user32, "GetWindowTextLengthW", [wt.HWND], ctypes.c_int),
    (user32, "GetWindowTextW", [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
    (user32, "GetWindowThreadProcessId", [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
    (user32, "GetWindowRect", [wt.HWND, ctypes.POINTER(RECT)], wt.BOOL),
    (user32, "GetForegroundWindow", [], wt.HWND),
    (user32, "GetWindowDC", [wt.HWND], wt.HDC),
    (user32, "ReleaseDC", [wt.HWND, wt.HDC], ctypes.c_int),
    (user32, "PrintWindow", [wt.HWND, wt.HDC, wt.UINT], wt.BOOL),
    (user32, "ShowWindow", [wt.HWND, ctypes.c_int], wt.BOOL),
    (user32, "SetForegroundWindow", [wt.HWND], wt.BOOL),
    (user32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], wt.BOOL),
    (user32, "WindowFromPoint", [wt.POINT], wt.HWND),
    (user32, "GetAncestor", [wt.HWND, wt.UINT], wt.HWND),
    (user32, "SendInput", [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wt.UINT),
    (user32, "MapVirtualKeyW", [wt.UINT, wt.UINT], wt.UINT),
    (gdi32, "CreateCompatibleDC", [wt.HDC], wt.HDC),
    (gdi32, "CreateCompatibleBitmap", [wt.HDC, ctypes.c_int, ctypes.c_int], wt.HBITMAP),
    (gdi32, "SelectObject", [wt.HDC, wt.HANDLE], wt.HANDLE),
    (gdi32, "DeleteObject", [wt.HANDLE], wt.BOOL),
    (gdi32, "DeleteDC", [wt.HDC], wt.BOOL),
    (gdi32, "BitBlt", [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD], wt.BOOL),
    (gdi32, "GetDIBits", [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, wt.LPVOID,
                         ctypes.POINTER(BITMAPINFOHEADER), wt.UINT], ctypes.c_int),
]:
    bound = getattr(dll, function)
    bound.argtypes = arguments
    bound.restype = result


VK = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)


def _kbd(scan, flags):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(0, scan, flags, 0, 0)
    return inp


def _virtual_key(vk, flags=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return inp


def _send_input(inputs):
    """SendInput 封装。

    注意：不要叫 _send —— 本文件另有一个同名的 JSON-RPC 发送函数 _send，
    Python 后定义者覆盖先定义者，会导致键盘注入静默走错分支。
    （同类事故：PowerShell 变量名大小写不敏感，$C 与 $c 互相覆盖。）
    """
    n = len(inputs)
    if n == 0:
        return 0
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(ctypes.get_last_error(),
                      "SendInput sent %d/%d events; input may be blocked by UIPI" % (sent, n))
    return sent


def type_text(text, hwnd=None):
    """以 Unicode 码元注入文本，绕过 IME。\\n 与 \\t 按虚拟键处理。"""
    inputs = []
    for ch in text:
        if ch == "\n":
            inputs += [_virtual_key(VK["enter"]), _virtual_key(VK["enter"], KEYEVENTF_KEYUP)]
            continue
        if ch == "\t":
            inputs += [_virtual_key(VK["tab"]), _virtual_key(VK["tab"], KEYEVENTF_KEYUP)]
            continue
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):          # BMP 外字符拆成代理对
            unit = int.from_bytes(raw[i:i + 2], "little")
            inputs += [_kbd(unit, KEYEVENTF_UNICODE),
                       _kbd(unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    _require_foreground(hwnd)
    sent = _send_input(inputs)
    return "已注入 %d 个字符 / %d 个输入事件" % (len(text), sent)


def press_key(combo, mode="scan", hwnd=None):
    """按组合键：'enter' / 'ctrl+s' / 'alt+f4' / 'f5' / 'ctrl+shift+p'。

    mode:
      "scan"（默认）—— 发扫描码 + KEYEVENTF_SCANCODE。
          Chromium / Electron（VS Code、Chrome、Edge）的键盘处理对扫描码敏感，
          只发虚拟键码时可能完全无响应（实测 VS Code 就是这种情况）。
      "vk"  —— 只发虚拟键码。少数老程序需要这种。
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if mode not in ("scan", "vk"):
        raise ValueError("按键模式必须是 scan 或 vk")
    if not parts:
        raise ValueError("空按键")
    vks = []
    for p in parts:
        if p not in VK:
            raise ValueError("未知按键: %s" % p)
        vks.append(VK[p])

    def one(vk, up):
        if mode == "vk":
            return _virtual_key(vk, KEYEVENTF_KEYUP if up else 0)
        sc = user32.MapVirtualKeyW(vk, 0)          # MAPVK_VK_TO_VSC
        flags = KEYEVENTF_SCANCODE
        if vk in EXTENDED_VK:
            flags |= KEYEVENTF_EXTENDEDKEY
        if up:
            flags |= KEYEVENTF_KEYUP
        return _kbd(sc, flags)

    inputs = [one(vk, False) for vk in vks] + [one(vk, True) for vk in reversed(vks)]
    _require_foreground(hwnd)
    _send_input(inputs)
    return "已按键: %s (%s 模式)" % (combo, mode)


def focus_window(hwnd):
    h = wt.HWND(hwnd)
    if not user32.IsWindow(h):
        raise ValueError("句柄无效: %s" % hwnd)
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)          # SW_RESTORE
        time.sleep(0.35)
    ok = user32.SetForegroundWindow(h)
    if not ok or user32.GetForegroundWindow() != hwnd:
        raise OSError("未能确认目标窗口处于前台（前台锁定）；请手动聚焦后重试。")
    return "焦点 -> 已设置"


# ---------------------------------------------------------------------------
# read_region —— 把「裁剪 + 放大 + 坐标映射」固化下来
#
# 为什么必须有这个工具（而不是每次手工注意）：
#   整张 2183x1365 的图发给视觉模型，它内部会缩到约 1440 宽再处理，
#   终端里 8px 高的字被缩成 5px —— 必然读错。
#   实测同一张图读含用户名的路径：
#       原图整张      -> 读成 12749 / 14760\...\练习代码   （全错）
#       裁出终端区域  -> 10749                            （差一位）
#       裁剪 + 放大2x -> 10743                            （正确）
#   所以「先裁再放大」不是优化，是读准细节的前提条件。
# ---------------------------------------------------------------------------
def read_region(image_path, x=None, y=None, w=None, h=None, scale=2.0, out_path=None):
    """裁剪 image_path 的指定区域并放大，写出供视觉模型读取的小图。

    x/y/w/h 支持两种写法：
      整数  -> 像素坐标
      0~1 的小数 -> 按图像宽高的比例（不知道像素位置时很有用）
    省略全部则取整图。

    返回 JSON 字符串，含输出路径、源区域、放大倍数——
    用它可以把模型在小图上报的坐标换算回原图坐标。
    """
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("需要 Pillow（pip install pillow）才能裁剪放大") from error

    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)

    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale 必须是正的有限数")
    for value in (x, y, w, h):
        if value is not None and not math.isfinite(value):
            raise ValueError("裁剪坐标必须是有限数")
    if (w is not None and w <= 0) or (h is not None and h <= 0):
        raise ValueError("裁剪宽高必须为正数")

    im = Image.open(image_path)
    W, H = im.size

    def px(v, total, default):
        if v is None:
            return default
        return int(round(v * total)) if type(v) is float and 0.0 <= v <= 1.0 else int(round(v))

    x0 = px(x, W, 0)
    y0 = px(y, H, 0)
    x1 = x0 + px(w, W, W - x0)
    y1 = y0 + px(h, H, H - y0)

    x0 = max(0, min(x0, W - 1))
    y0 = max(0, min(y0, H - 1))
    x1 = max(x0 + 1, min(x1, W))
    y1 = max(y0 + 1, min(y1, H))

    try:
        crop = im.crop((x0, y0, x1, y1))
    finally:
        im.close()
    if scale and scale != 1:
        resized = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
        crop.close()
        crop = resized

    if not out_path:
        base, ext = os.path.splitext(image_path)
        out_path = "%s_crop%s" % (base, ext or ".png")
    d = os.path.dirname(out_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    try:
        crop.save(out_path)
    finally:
        crop.close()

    return json.dumps({
        "out_path": out_path,
        "src_size": [W, H],
        "region": [x0, y0, x1, y1],
        "region_size": [x1 - x0, y1 - y0],
        "out_size": [crop.width, crop.height],
        "upscale": scale,
        "note": ("模型在 out_path 上给出的坐标 (mx,my) 换算回原图是："
                 "orig_x = %d + mx/%s, orig_y = %d + my/%s" % (x0, scale, y0, scale)),
    }, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "list_windows",
        "description": "列出当前所有可见的顶层窗口（标题、窗口句柄、进程 ID、位置尺寸、是否最小化）。用于在操作 GUI 前先看清桌面上有什么。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "screen_info",
        "description": "返回屏幕尺寸和当前光标位置。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "screenshot_window",
        "description": "把指定窗口截图为 PNG 文件。hwnd 省略时截当前前台窗口。返回文件路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer", "description": "窗口句柄；省略则用前台窗口"},
                "path": {"type": "string", "description": "输出 PNG 的绝对路径"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "click",
        "description": "在屏幕绝对坐标处左键单击。注意：会真实移动并点击鼠标，可能抢夺用户焦点。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "hwnd": {"type": "integer", "description": "可选目标顶层句柄；焦点或点击目标不匹配时拒绝操作"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "focus_window",
        "description": "把指定窗口切到前台并恢复（若已最小化）。操作 GUI 前应先聚焦目标窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {"hwnd": {"type": "integer", "description": "窗口句柄"}},
            "required": ["hwnd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": "向当前焦点窗口注入文本。使用 SendInput + KEYEVENTF_UNICODE，绕过中文输入法（IME），不会把英文转成中文。支持中文与任意 Unicode。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本；\\n 会按回车处理"},
                "hwnd": {"type": "integer", "description": "可选目标顶层句柄；焦点不匹配时拒绝输入"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_region",
        "description": ("把截图的指定区域裁出来并放大 2 倍，输出一张小图供视觉模型读取。"
                        "读细节（小字、路径、按钮文字）前必须先过这一步——整张大图会被视觉模型"
                        "内部缩放到约 1440 宽，小字必然读错。x/y/w/h 可用像素值或 0~1 的比例。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "源截图绝对路径"},
                "x": {"type": "number", "description": "左边界（像素或 0~1 比例）"},
                "y": {"type": "number", "description": "上边界（像素或 0~1 比例）"},
                "w": {"type": "number", "description": "宽度（像素或 0~1 比例）；省略则到右边界"},
                "h": {"type": "number", "description": "高度（像素或 0~1 比例）；省略则到下边界"},
                "scale": {"type": "number", "description": "放大倍数，默认 2（实测最优）"},
                "out_path": {"type": "string", "description": "输出路径；省略则与源图同目录加 _crop 后缀"},
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press_key",
        "description": "发送组合键，如 'enter'、'ctrl+s'、'alt+f4'。默认扫描码模式，可选虚拟键模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "按键或组合键，用 + 连接"},
                "mode": {"type": "string", "enum": ["scan", "vk"], "default": "scan"},
                "hwnd": {"type": "integer", "description": "可选目标顶层句柄；焦点不匹配时拒绝输入"},
            },
            "required": ["keys"],
            "additionalProperties": False,
        },
    },
]


def dispatch(name, args):
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None:
        raise ValueError("未知工具: %s" % name)
    schema = tool["inputSchema"]
    properties = schema["properties"]
    if any(key not in properties for key in args):
        raise ValueError("未知工具参数")
    if any(key not in args for key in schema.get("required", [])):
        raise ValueError("缺少必需工具参数")
    for key, value in args.items():
        expected = properties[key]["type"]
        valid = ((expected == "string" and isinstance(value, str))
                 or (expected == "integer" and type(value) is int)
                 or (expected == "number" and type(value) in (int, float)))
        if not valid or ("enum" in properties[key] and value not in properties[key]["enum"]):
            raise ValueError("工具参数类型或取值无效: %s" % key)
        if expected == "number" and not math.isfinite(value):
            raise ValueError("工具数值参数必须是有限数")
    if name == "list_windows":
        return json.dumps(list_windows(), ensure_ascii=False, indent=1)
    if name == "screen_info":
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return json.dumps({
            "screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)],
            "cursor": [pt.x, pt.y],
        }, ensure_ascii=False)
    if name == "screenshot_window":
        return screenshot_window(args.get("hwnd"), args["path"])
    if name == "click":
        return click(args["x"], args["y"], args.get("hwnd"))
    if name == "focus_window":
        return focus_window(args["hwnd"])
    if name == "type_text":
        return type_text(args["text"], args.get("hwnd"))
    if name == "press_key":
        return press_key(args["keys"], args.get("mode", "scan"), args.get("hwnd"))
    if name == "read_region":
        return read_region(
            args["image_path"],
            args.get("x"), args.get("y"), args.get("w"), args.get("h"),
            args.get("scale", 2.0), args.get("out_path"),
        )
    raise ValueError("未知工具: %s" % name)


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------
def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg):
    sys.stderr.write("[gui-control] %s\n" % msg)
    sys.stderr.flush()


def main():
    _log("started, pid=%d" % os.getpid())
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        # 容忍 UTF-8 BOM：某些调用方（例如 PowerShell 管道，$OutputEncoding 带 BOM 时）
        # 会在首行插入 BOM，不剥掉的话第一条 initialize 会被判为非法 JSON 而静默丢弃。
        line = line.lstrip("\ufeff")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            _log("bad json: %s" % e)
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue

        if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
                or not isinstance(msg.get("method"), str)):
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "invalid request"}})
            continue
        if "id" not in msg:
            # Notifications do not trigger GUI actions and never get a response.
            if msg["method"] == "notifications/initialized":
                _log("initialized")
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if isinstance(mid, bool) or not isinstance(mid, (str, int, type(None))):
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "invalid request id"}})
            continue
        params = msg.get("params", {})
        if not isinstance(params, dict):
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32602, "message": "params must be an object"}})
            continue

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }})
        elif method == "notifications/initialized":
            _log("initialized")
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            _log("call %s" % name)
            try:
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                text = dispatch(name, args)
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": text}], "isError": False}})
            except Exception as e:
                _send({"jsonrpc": "2.0", "id": mid,
                       "result": {"content": [{"type": "text", "text": "ERROR: %s" % e}], "isError": True}})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "method not found: %s" % method}})
        # 其余是通知，不需要回


if __name__ == "__main__":
    main()
