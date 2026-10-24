# -*- coding: utf-8 -*-
"""Win32 基元:窗口定位 / 客户区几何 / 前台激活 / 鼠标点击。

全部走物理像素(ctypes 直调 user32),不经过 Qt 的 DPI 逻辑坐标——
校准与执行共用同一坐标系,150% 缩放屏上不会出现 1.5 倍偏差。

窗口识别(2026-10-24 实测无畏契约国服):
    窗口类名 ``VALORANTUnrealWindow`` —— 独特且稳定,**主匹配**。
    标题是 ``'VALORANT  '``(两个尾随空格!),且存在干扰窗口
    「无畏契约登录器」(最小化隐藏在 -32000)—— 标题关键词只作辅助,
    绝不能单独使用("无畏契约"会命中登录器)。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes as wt
from typing import List, Optional, Tuple

logger = logging.getLogger("DouyinLowLatencyViewer.automation.win32")

__all__ = [
    "WindowInfo", "find_game_window", "client_geometry",
    "activate_window", "is_foreground", "click_point", "cursor_pos",
    "process_elevation", "self_elevation", "window_pid",
]

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

_WM_NCLBUTTONDOWN = 0xA1
_HTCAPTION = 2


class WindowInfo:
    """一个候选游戏窗口的静态描述(枚举时的快照)。"""

    __slots__ = ("hwnd", "title", "class_name")

    def __init__(self, hwnd: int, title: str, class_name: str):
        self.hwnd = hwnd
        self.title = title
        self.class_name = class_name

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"WindowInfo(hwnd={self.hwnd}, title={self.title!r}, "
                f"class={self.class_name!r})")


def _enum_visible_windows() -> List[WindowInfo]:
    """枚举所有可见顶层窗口(含最小化的——最小化窗口仍可见性为 True)。"""
    results: List[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return 1
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value
        if not title:
            return 1
        cls_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls_buf, 128)
        results.append(WindowInfo(hwnd, title, cls_buf.value))
        return 1

    user32.EnumWindows(_cb, 0)
    return results


def filter_windows(windows: List[WindowInfo], class_name: str = "",
                   title_keyword: str = "") -> List[WindowInfo]:
    """按 类名精确匹配(优先) / 标题包含匹配(辅助) 过滤窗口列表。

    纯函数(单测直接喂构造列表):
      - class_name 非空:类名相等(大小写不敏感)才命中;
      - title_keyword 非空:strip 后大小写不敏感包含(容忍 'VALORANT  '
        的尾随空格);
      - 两者都给:命中任一即算(类名是主证据,标题兜底覆盖改了类名的版本)。
      - 最小化窗口(如登录器藏在 -32000)若类名/标题不匹配自然排除;
        类名恰好匹配但已最小化的**保留**(执行前会先恢复)。
    """
    cls = (class_name or "").strip().lower()
    kw = (title_keyword or "").strip().lower()
    if not cls and not kw:
        return []
    hits = []
    for w in windows:
        if cls and w.class_name.lower() == cls:
            hits.append(w)
            continue
        if kw and kw in w.title.strip().lower():
            hits.append(w)
    return hits


def find_game_window(class_name: str, title_keyword: str = ""
                     ) -> Optional[WindowInfo]:
    """找到游戏主窗口;找不到返回 None(调用方负责明确报错)。"""
    return (filter_windows(_enum_visible_windows(), class_name, title_keyword)
            or [None])[0]


# ----------------------------------------------------------------------
# 几何
# ----------------------------------------------------------------------
def client_geometry(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """客户区 (screen_x, screen_y, width, height);窗口无效返回 None。"""
    if not user32.IsWindow(hwnd):
        return None
    cr = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    origin = wt.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return origin.x, origin.y, cr.right, cr.bottom


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_foreground(hwnd: int) -> bool:
    return user32.GetForegroundWindow() == hwnd


# ----------------------------------------------------------------------
# 前台激活(组合拳:恢复 → AttachThreadInput → Alt 解锁 → 验证)
# ----------------------------------------------------------------------
def activate_window(hwnd: int, timeout_ms: int = 600) -> bool:
    """把窗口带到前台;**成功与否以 GetForegroundWindow 验证为准**。

    Windows 前台锁定:后台进程直接 SetForegroundWindow 只会闪任务栏。
    组合:最小化先恢复 → 把本线程 attach 到当前前台线程(骗过锁)
    → 模拟一次 Alt 抬键(满足"用户正在输入"的接管条件)→ 设置前台。
    返回 False 时**调用方绝不能继续执行点击序列**(点击会落进当时
    的前台应用——那是事故,不是失败)。
    """
    if not user32.IsWindow(hwnd):
        return False
    if is_foreground(hwnd):
        return True
    if is_iconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE

    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    our_tid = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != our_tid:
        attached = bool(user32.AttachThreadInput(our_tid, fg_tid, True))
    try:
        # Alt 抬键:让系统认为"用户刚操作过键盘",允许接管前台
        user32.keybd_event(0x12, 0, 2, 0)   # VK_MENU, KEYEVENTF_KEYUP
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_tid, fg_tid, False)

    # 验证闭环:切换动画期间轮询,前台真的是它才算成功
    import time
    deadline = time.perf_counter() + timeout_ms / 1000.0
    while time.perf_counter() < deadline:
        if is_foreground(hwnd):
            return True
        time.sleep(0.03)
    return is_foreground(hwnd)


# ----------------------------------------------------------------------
# 鼠标
# ----------------------------------------------------------------------
_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def cursor_pos() -> Tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def click_point(x: int, y: int, hold_ms: int = 40) -> bool:
    """物理像素坐标点击一次:SetCursorPos + SendInput down/up。

    SetCursorPos 直接吃虚拟屏物理坐标(多显示器安全);按键事件用
    SendInput(前台真实事件,不做后台注入)。返回 False = 调用失败,
    调用方应中止序列。
    """
    if not user32.SetCursorPos(int(x), int(y)):
        return False
    import time

    def _send(flags: int) -> bool:
        inp = _INPUT(type=_INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        return user32.SendInput(1, ctypes.byref(inp),
                                ctypes.sizeof(_INPUT)) == 1

    if not _send(_MOUSEEVENTF_LEFTDOWN):
        return False
    time.sleep(hold_ms / 1000.0)
    return _send(_MOUSEEVENTF_LEFTUP)


def window_from_point(x: int, y: int) -> int:
    """物理坐标下的顶层窗口句柄(校准落点验证用)。"""
    pt = wt.POINT(x, y)
    return user32.WindowFromPoint(pt)


# ----------------------------------------------------------------------
# 权限检测(UIPI 前置)
# ----------------------------------------------------------------------
def process_elevation(pid: int):
    """进程是否以管理员(高完整性)运行;查询失败返回 None(不猜)。"""
    advapi32 = ctypes.windll.advapi32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenElevation = 20
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elev = ctypes.c_ulong()
            length = ctypes.c_ulong()
            if not advapi32.GetTokenInformation(
                    token, TokenElevation, ctypes.byref(elev), 4,
                    ctypes.byref(length)):
                return None
            return bool(elev.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(h)


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def self_elevation() -> bool:
    """本进程是否管理员(对自己查询不会失败;极端失败按普通权限)。"""
    return bool(process_elevation(kernel32.GetCurrentProcessId()))
