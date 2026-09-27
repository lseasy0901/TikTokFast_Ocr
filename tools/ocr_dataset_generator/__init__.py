# -*- coding: utf-8 -*-
"""
LiveLens OCR 数据集生成器（Phase: Dataset Generator）。

边界（本阶段刻意不做）：
    不调用 PP-OCRv6 / Paddle / Tesseract，不做直播采集、不做 ROI 选择、
    不生成合成图片、不训练、不自动标注。
    只建立**可靠、可复现、格式严格、配额严格**的数据集生成基础设施。

外部入口：
    CLI      python -m tools.ocr_dataset_generator --help
    Python   from tools.ocr_dataset_generator import generate_balanced_codes
"""

from .code_generator import (
    GenerationPlan,
    FormatPlan,
    QuotaError,
    generate_balanced_codes,
    generate_code,
    generate_unique_codes,
    validate_code,
)
from .schemas import FORMATS, FORMAT_IDS, SAMPLE_FIELDS, TRUTH_FIELDS

__all__ = [
    "FORMATS",
    "FORMAT_IDS",
    "SAMPLE_FIELDS",
    "TRUTH_FIELDS",
    "GenerationPlan",
    "FormatPlan",
    "QuotaError",
    "generate_code",
    "generate_unique_codes",
    "generate_balanced_codes",
    "validate_code",
]

__version__ = "0.1.0"
