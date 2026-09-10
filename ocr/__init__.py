# -*- coding: utf-8 -*-
"""OCR 模块（Phase 2）"""

from .engine import OCREngine, OCREngineError
from .preprocess import preprocess_for_ocr, preprocess_for_ocr_adaptive
from .roi_manager import ROI, ROIManager
from .worker import OCRWorker
from .clipboard import ClipboardManager

__all__ = [
    # OCR 引擎
    "OCREngine",
    "OCREngineError",

    # 预处理
    "preprocess_for_ocr",
    "preprocess_for_ocr_adaptive",

    # ROI 管理
    "ROI",
    "ROIManager",

    # OCR 工作线程
    "OCRWorker",

    # 剪切板管理
    "ClipboardManager",
]
