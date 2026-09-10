# -*- coding: utf-8 -*-
"""
剪切板管理器 (Phase 2.4)

管理 OCR 识别结果的自动复制到系统剪切板。

功能：
    - 自动过滤无效文本
    - 避免重复复制相同文本
    - 线程安全的剪切板操作
"""

import logging
from typing import Optional

from PySide6.QtWidgets import QApplication

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.clipboard")

__all__ = ["ClipboardManager"]


class ClipboardManager:
    """
    剪切板管理器

    负责 OCR 识别结果的自动复制，包含智能过滤逻辑。
    """

    def __init__(self):
        """初始化剪切板管理器"""
        self._last_copied: Optional[str] = None
        self._copy_count: int = 0
        self._filter_count: int = 0
        self._last_error: Optional[str] = None

        logger.info("ClipboardManager 初始化完成")

    def copy(self, text: str) -> bool:
        """
        复制文本到剪切板（带智能过滤）

        过滤条件：
            1. 空文本
            2. 纯空格
            3. 与上一次复制相同的文本

        参数：
            text: 要复制的文本

        返回：
            True 如果成功复制，False 如果被过滤或失败
        """
        # 添加调试日志
        logger.info("[CLIPBOARD] copy called:")
        logger.info("text=%s", text)

        # 1. 空文本过滤
        if not text:
            self._filter_count += 1
            logger.debug("剪切板: 过滤空文本")
            logger.info("result=False (空文本)")
            return False

        # 2. 纯空格过滤
        stripped = text.strip()
        if not stripped:
            self._filter_count += 1
            logger.debug("剪切板: 过滤纯空格文本")
            logger.info("result=False (纯空格)")
            return False

        # 3. 重复文本过滤
        if stripped == self._last_copied:
            self._filter_count += 1
            logger.debug("剪切板: 过滤重复文本: '%s'", stripped)
            logger.info("result=False (重复文本)")
            return False

        # 执行复制
        try:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                logger.error("剪切板: QApplication.clipboard() 返回 None")
                logger.info("result=False (clipboard None)")
                return False

            clipboard.setText(stripped)
            self._last_copied = stripped
            self._copy_count += 1

            logger.info("剪切板: 已复制 (第%d次): '%s'", self._copy_count, stripped)
            logger.info("result=True")
            return True

        except Exception as e:
            self._last_error = str(e)
            logger.error("剪切板: 复制失败: %s", e, exc_info=True)
            logger.info("result=False (异常)")
            return False

    def get_last_copied(self) -> Optional[str]:
        """获取最后一次复制的文本"""
        return self._last_copied

    def reset(self) -> None:
        """重置管理器状态"""
        self._last_copied = None
        self._copy_count = 0
        self._filter_count = 0
        self._last_error = None
        logger.info("ClipboardManager 状态已重置")

    @property
    def copy_count(self) -> int:
        """累计复制次数"""
        return self._copy_count

    @property
    def filter_count(self) -> int:
        """累计过滤次数"""
        return self._filter_count

    @property
    def last_error(self) -> Optional[str]:
        """最后一次错误信息"""
        return self._last_error