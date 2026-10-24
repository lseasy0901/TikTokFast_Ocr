# -*- coding: utf-8 -*-
"""自动加入房间(automation)—— 把「复制到剪贴板的房间码」自动填进游戏。

流程契约(2026-10-24 与用户确认,目标:无畏契约国服自定义房):
    前提:游戏以窗口模式运行,并停留在「加入房间」页面(用户负责)。
    触发:全局热键(默认 F8,可配)。
    动作:定位游戏窗口 → 激活到前台 → 点击[房间码输入框]
          → 点击[浮层的粘贴项](把剪贴板内容贴入) → 点击[加入房间]。
    三处点击位置由用户**校准一次**得到,存相对游戏窗口客户区的比例坐标,
    换分辨率/调窗口大小不失效(游戏 UI 等比缩放)。

模块划分:
    join_config     配置与校准点持久化(join_config.json)
    win32           窗口枚举/客户区/前台激活/SendInput 点击(纯 ctypes,物理像素)
    hotkey          RegisterHotKey + Qt 原生事件过滤器
    join_controller 执行序列(QTimer 链,不阻塞 GUI,逐步验证前台)
    calibration     三点校准(鼠标轮询捕获,落点验证,所见即所得)

风险披露(已与用户确认知情):目标游戏带内核级反作弊,SendInput 模拟
输入在检测能力范围内,存在封号风险;本模块只做前台可见的真实鼠标事件,
不做后台注入。
"""

from automation.join_config import JoinConfig, default_config_path
from automation.win32 import (
    WindowInfo,
    find_game_window,
    filter_windows,
    client_geometry,
    activate_window,
    click_point,
)

from automation.hotkey import GlobalHotkey, VK_NAMES
from automation.join_controller import JoinController, JoinResult, frac_to_pixel
from automation.calibration import CalibrationOverlay, STEP_PROMPTS

__all__ = [
    "JoinConfig", "default_config_path", "REFERENCE_POINTS",
    "WindowInfo", "find_game_window", "filter_windows",
    "client_geometry", "activate_window", "click_point",
    "GlobalHotkey", "VK_NAMES",
    "JoinController", "JoinResult", "frac_to_pixel",
    "CalibrationOverlay", "STEP_PROMPTS",
]
