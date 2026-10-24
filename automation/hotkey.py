# -*- coding: utf-8 -*-
"""全局热键:RegisterHotKey + Qt 原生事件过滤器。

无论 LiveLens 是否在前台(游戏前台、浏览器前台都一样),热键都会触发
——RegisterHotKey 是系统级注册。热键冲突(别的软件占了同一个键)时
注册失败并如实上报,启动/改键后都要验证注册结果。

只支持单修饰键-free 的 F1..F12:自动加入是"一键"动作,组合键在游戏
前台时容易被游戏自己吃掉,单功能键最可靠;候选表刻意收窄。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

__all__ = ["GlobalHotkey", "VK_NAMES"]

_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000

#: 键名 → 虚拟键码(候选刻意只给 F1..F12,理由见模块注释)
VK_NAMES = {f"F{i}": 0x70 + i - 1 for i in range(1, 13)}

user32 = ctypes.windll.user32


class _MSG(ctypes.Structure):
    """Win32 MSG(64 位布局)。只取我们关心的字段。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", wt.POINT),
    ]


class _HotkeyFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 翻译成 Qt 信号的薄过滤器(其余消息原样放行)。"""

    def __init__(self, owner: "GlobalHotkey"):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message == _WM_HOTKEY and msg.wParam == self._owner._id:
                self._owner.activated.emit()
                return True, 0  # PySide6 期望 (bool, result)
        except Exception:  # noqa: BLE001 - 过滤器绝不能把消息泵搞崩
            pass
        return False, 0


class GlobalHotkey(QObject):
    """一枚全局热键的注册/信号/注销。"""

    #: 热键按下(在 Qt 主线程发,槽可直接更新 UI)
    activated = Signal()
    #: 注册失败原因(冲突/非法键名),None=当前注册成功
    last_error: str | None = None

    _next_id = 1  # 进程内唯一的热键 id 计数

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._id = GlobalHotkey._next_id
        GlobalHotkey._next_id += 1
        self._registered_vk: int | None = None
        self._filter = _HotkeyFilter(self)

    # ------------------------------------------------------------------
    def register(self, key_name: str) -> bool:
        """注册热键(先注销旧的)。键名必须来自 :data:`VK_NAMES`。"""
        self.unregister()
        vk = VK_NAMES.get((key_name or "").strip().upper())
        if vk is None:
            self.last_error = f"不支持的键名:{key_name}(可选 F1–F12)"
            return False
        if not user32.RegisterHotKey(None, self._id, _MOD_NOREPEAT, vk):
            self.last_error = (
                f"{key_name} 注册失败(可能被其它软件占用)")
            return False
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)
        self._registered_vk = vk
        self.last_error = None
        return True

    def unregister(self) -> None:
        """注销(幂等);把过滤器也卸掉,热键关闭时零残留。"""
        if self._registered_vk is not None:
            user32.UnregisterHotKey(None, self._id)
            self._registered_vk = None
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None:
            try:
                app.removeNativeEventFilter(self._filter)
            except Exception:  # noqa: BLE001
                pass

    @property
    def is_registered(self) -> bool:
        return self._registered_vk is not None
