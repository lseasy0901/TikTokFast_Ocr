# -*- coding: utf-8 -*-
"""可选品牌字体加载（resources/fonts/ 下放 .ttf/.otf 即自动注册）。

机制优先于捆绑：字体文件**不进 git**（体积考虑），打包时由发布脚本带上；
缺文件、加载失败一律静默回退系统字体栈 —— 字体是纯增强，永远不能因为它
多一次启动失败。

用法（见 :func:`brand_family`）：
    gui.fonts.load_brand_fonts()          # QApplication 创建后、出窗口前
    theme.apply_theme() 内部会自动把 brand_family() 前置进 FONT_FAMILY
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("DouyinLowLatencyViewer.gui.fonts")

__all__ = ["load_brand_fonts", "brand_family", "FONT_DIR"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = _PROJECT_ROOT / "resources" / "fonts"

_loaded_families: list[str] = []
_scanned = False


def load_brand_fonts() -> list[str]:
    """扫描并注册 resources/fonts/ 下的字体；返回成功注册的 family 名。

    幂等：重复调用直接返回缓存。
    """
    global _scanned
    if _scanned:
        return list(_loaded_families)
    _scanned = True

    if not FONT_DIR.is_dir():
        return []
    try:
        from PySide6.QtGui import QFontDatabase
    except Exception:  # noqa: BLE001 - 无 GUI 环境静默跳过
        return []

    for path in sorted(FONT_DIR.glob("*")):
        if path.suffix.lower() not in (".ttf", ".otf"):
            continue
        try:
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id < 0:
                logger.warning("字体注册失败（Qt 拒绝）: %s", path.name)
                continue
            families = QFontDatabase.applicationFontFamilies(font_id)
            for family in families:
                if family not in _loaded_families:
                    _loaded_families.append(family)
            if families:
                logger.info("[UI] 品牌字体已注册: %s (%s)",
                            path.name, ", ".join(families))
        except Exception as e:  # noqa: BLE001 - 单个字体坏了不影响其它
            logger.warning("字体加载异常 %s: %s", path.name, e)

    return list(_loaded_families)


def brand_family() -> str | None:
    """首选品牌字体名（第一个注册成功的）；没有则 None。"""
    if not _scanned:
        load_brand_fonts()
    return _loaded_families[0] if _loaded_families else None
