# -*- coding: utf-8 -*-
"""自动加入的配置与校准点持久化。

独立文件 join_config.json(与 ui_prefs.json 同目录):自动化配置含校准点,
属于"坏了就该重新校准"的数据,不和通用偏好混放;损坏即整体回默认,
绝不能因配置问题影响直播/OCR/授权主流程。

窗口匹配(2026-10-24 实测无畏契约国服):
    类名 ``VALORANTUnrealWindow`` 是主匹配(独特稳定);标题是
    ``VALORANT  ``(尾随空格)仅辅助。**不要用"无畏契约"当标题关键词**——
    那会命中「无畏契约登录器」(最小化隐藏窗)导致点错窗口。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger("DouyinLowLatencyViewer.automation.join_config")

__all__ = ["JoinConfig", "default_config_path", "Point"]

_FILENAME = "join_config.json"

#: 合法热键名白名单(与 automation.hotkey.VK_NAMES 保持同步;在那里定义
#: 会引入 Qt 依赖,配置层只需要名字集合)。load 时校验,非法名回默认,
#: 不让坏值存到运行时才炸。
HOTKEY_CHOICES = frozenset(f"F{i}" for i in range(1, 13))
#: 校准点:相对游戏窗口**客户区**的比例坐标 (fx, fy),0..1
Point = Tuple[float, float]

#: 校准参考点(2026-10-24 实测量取,仅用于校准引导的高亮提示;
#: 不作为可执行点——执行必须以用户校准的三点为准)
REFERENCE_POINTS = {
    "paste": (0.62, 0.435),   # 输入框右侧的「粘贴」小按钮(2026-10-25 截图)
    "join": (0.505, 0.58),    # 「加入房间」按钮
}


def default_config_path() -> str:
    import pathlib

    from utils.stream_history import default_data_dir

    return str(pathlib.Path(default_data_dir()) / _FILENAME)


@dataclass
class JoinConfig:
    """自动加入的全部可配置项(内存副本,save() 落盘)。"""

    #: 总开关。关掉时热键不注册(不是"注册了不响应")
    enabled: bool = False
    #: 全局热键的键名(automation.hotkey.VK_NAMES 的键)
    hotkey: str = "F8"
    #: 窗口类名(主匹配,精确)
    window_class: str = "VALORANTUnrealWindow"
    #: 窗口标题关键词(辅助匹配,包含;勿用"无畏契约"——会命中登录器)
    window_title: str = "VALORANT"
    #: 点击粘贴后是否自动点「加入房间」
    auto_confirm: bool = True
    #: 两处校准点(2026-10-25 用户截图澄清:粘贴是输入框旁**常驻小按钮**,
    #: 点击即贴入剪贴板内容,无需先点输入框弹浮层);None = 未校准该点
    paste_point: Optional[Point] = None    # 输入框旁的「粘贴」小按钮
    join_point: Optional[Point] = None     # 「加入房间」按钮
    #: 旧三步流程的字段(点输入框)。已废弃仅作向后兼容读取,序列不再使用
    input_point: Optional[Point] = None
    #: 校准时间(RFC3339),展示用
    calibrated_at: str = ""
    #: 校准时记录的客户区宽高比;执行时偏差>5% 视为布局漂移(警告)
    calibrated_aspect: float = 0.0
    #: 序列延时(ms):激活后等切换动画、点击之间等 UI 响应
    delay_activate_ms: int = 400
    delay_click_ms: int = 450

    # ------------------------------------------------------------------
    @property
    def is_calibrated(self) -> bool:
        """两点齐全才算校准完成(粘贴按钮 + 加入按钮)。"""
        return self.paste_point is not None and self.join_point is not None

    def points_summary(self) -> str:
        if not self.is_calibrated:
            missing = [n for n, p in (
                ("粘贴按钮", self.paste_point),
                ("加入按钮", self.join_point)) if p is None]
            return f"未校准(缺:{'/'.join(missing)})"
        when = f",{self.calibrated_at[:10]}" if self.calibrated_at else ""
        return f"已校准两点{when}"

    # ------------------------------------------------------------------
    def save(self, path: Optional[str] = None) -> None:
        path = path or default_config_path()
        data = {
            "version": 1,
            "enabled": self.enabled,
            "hotkey": self.hotkey,
            "window_class": self.window_class,
            "window_title": self.window_title,
            "auto_confirm": self.auto_confirm,
            "input_point": list(self.input_point) if self.input_point else None,
            "paste_point": list(self.paste_point) if self.paste_point else None,
            "join_point": list(self.join_point) if self.join_point else None,
            "calibrated_at": self.calibrated_at,
            "calibrated_aspect": self.calibrated_aspect,
            "delay_activate_ms": self.delay_activate_ms,
            "delay_click_ms": self.delay_click_ms,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 - 写失败只影响记住配置
            logger.warning("join_config.json 写入失败: %s", e)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "JoinConfig":
        """读配置;文件缺失/损坏/字段非法一律回默认值(不抛异常)。"""
        path = path or default_config_path()
        cfg = cls()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return cfg
            if isinstance(data.get("enabled"), bool):
                cfg.enabled = data["enabled"]
            if (isinstance(data.get("hotkey"), str)
                    and data["hotkey"] in HOTKEY_CHOICES):
                cfg.hotkey = data["hotkey"]
            for key, attr in (("window_class", "window_class"),
                              ("window_title", "window_title")):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    setattr(cfg, attr, value.strip())
            if isinstance(data.get("auto_confirm"), bool):
                cfg.auto_confirm = data["auto_confirm"]
            for key in ("input_point", "paste_point", "join_point"):
                value = data.get(key)
                if (isinstance(value, (list, tuple)) and len(value) == 2
                        and all(isinstance(v, (int, float)) for v in value)
                        and 0.0 <= value[0] <= 1.0 and 0.0 <= value[1] <= 1.0):
                    setattr(cfg, key, (float(value[0]), float(value[1])))
            if isinstance(data.get("calibrated_at"), str):
                cfg.calibrated_at = data["calibrated_at"]
            value = data.get("calibrated_aspect")
            if isinstance(value, (int, float)) and value > 0:
                cfg.calibrated_aspect = float(value)
            for key in ("delay_activate_ms", "delay_click_ms"):
                value = data.get(key)
                if isinstance(value, int) and 50 <= value <= 5000:
                    setattr(cfg, key, value)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 - 坏配置回默认
            logger.warning("join_config.json 读取失败,按默认值处理: %s", e)
        return cfg
