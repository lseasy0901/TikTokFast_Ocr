# -*- coding: utf-8 -*-
"""OCR 模块（Phase 2 + OCR V2.1 Format-Aware + V2.3 时序裁决）"""

from .engine import OCREngine, OCREngineError
from .preprocess import preprocess_for_ocr, preprocess_for_ocr_adaptive
from .roi_manager import ROI, ROIManager
from .worker import OCRWorker
from .clipboard import ClipboardManager

# OCR V2.1：格式感知的房间号识别
from .code_format import CharClass, CodeFormat
from .profiles import PROFILES, GameProfile, get_profile, profile_ids
from .room_code import (
    SUPPORTED_SCALES,
    CandidateResolver,
    RoomCodeCandidate,
    RoomCodeRecognizer,
    SingleFrameResolver,
)

# OCR V2.3：时序裁决（连续一致性）
from .temporal import DEFAULT_MIN_AGREEMENT, DEFAULT_WINDOW_SIZE, TemporalResolver

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

    # 房间号格式 / 游戏档 (OCR V2.1)
    "CharClass",
    "CodeFormat",
    "GameProfile",
    "PROFILES",
    "get_profile",
    "profile_ids",

    # 房间号识别管线 (OCR V2.1)
    "RoomCodeRecognizer",
    "RoomCodeCandidate",
    "CandidateResolver",
    "SingleFrameResolver",
    "SUPPORTED_SCALES",

    # 时序裁决 (OCR V2.3)
    "TemporalResolver",
    "DEFAULT_MIN_AGREEMENT",
    "DEFAULT_WINDOW_SIZE",
]
