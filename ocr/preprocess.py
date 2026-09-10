# -*- coding: utf-8 -*-
"""
OCR 预处理模块 (Phase 2.2 Step 1)

针对游戏 HUD 优化的图像预处理：
- 灰度化
- 放大2倍
- 二值化
- 降噪
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.preprocess")

__all__ = ["preprocess_for_ocr"]


def preprocess_for_ocr(image: np.ndarray,
                       grayscale: bool = True,
                       scale_factor: float = 2.0,
                       threshold: int = 128,
                       denoise: bool = True) -> np.ndarray:
    """
    针对 OCR 的游戏 HUD 优化预处理流程

    参数：
        image: 输入图像 (BGR 格式的 numpy.ndarray)
        grayscale: 是否灰度化处理 (默认 True)
        scale_factor: 放大倍数 (默认 2.0)
        threshold: 二值化阈值 (0-255, 默认 128)
        denoise: 是否降噪处理 (默认 True)

    返回：
        预处理后的图像 (numpy.ndarray)
    """
    if image is None or image.size == 0:
        logger.error("输入图像为空")
        return image

    processed = image.copy()

    # 1. 灰度化
    if grayscale and len(processed.shape) == 3:
        processed = cv2.cvtColor(processed, cv2.COLOR_BGR2GRAY)
        logger.debug("图像已灰度化")

    # 2. 放大2倍 (提高 OCR 识别精度)
    if scale_factor > 1.0:
        new_width = int(processed.shape[1] * scale_factor)
        new_height = int(processed.shape[0] * scale_factor)
        processed = cv2.resize(
            processed,
            (new_width, new_height),
            interpolation=cv2.INTER_CUBIC
        )
        logger.debug("图像已放大 %.1fx -> %dx%d", scale_factor, new_width, new_height)

    # 3. 二值化
    # 使用固定阈值而不是OTSU，避免"I"被识别为"1"
    if len(processed.shape) == 2:  # 灰度图
        processed = cv2.threshold(
            processed,
            threshold,
            255,
            cv2.THRESH_BINARY
        )[1]
        logger.debug("图像已二值化 (阈值=%d)", threshold)

    # 4. 降噪
    if denoise:
        # 使用轻微的形态学降噪，避免中值滤波改变字母形状
        kernel = np.ones((2, 2), np.uint8)
        processed = cv2.morphologyEx(processed, cv2.MORPH_OPEN, kernel)

        logger.debug("图像已降噪 (形态学开运算)")

    return processed


def preprocess_for_ocr_adaptive(image: np.ndarray,
                                 scale_factor: float = 2.0,
                                 block_size: int = 15,
                                 c: float = 8.0) -> np.ndarray:
    """
    自适应阈值预处理（适用于光照不均匀的场景）

    参数：
        image: 输入图像
        scale_factor: 放大倍数
        block_size: 自适应阈值块大小 (必须为奇数)
        c: 常数，从均值或加权均值中减去的值

    返回：
        预处理后的图像
    """
    if image is None or image.size == 0:
        logger.error("输入图像为空")
        return image

    processed = image.copy()

    # 1. 灰度化
    if len(processed.shape) == 3:
        processed = cv2.cvtColor(processed, cv2.COLOR_BGR2GRAY)

    # 2. 放大
    if scale_factor > 1.0:
        new_width = int(processed.shape[1] * scale_factor)
        new_height = int(processed.shape[0] * scale_factor)
        processed = cv2.resize(
            processed,
            (new_width, new_height),
            interpolation=cv2.INTER_CUBIC
        )

    # 3. 自适应阈值二值化
    processed = cv2.adaptiveThreshold(
        processed,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size,
        c
    )

    # 4. 降噪
    processed = cv2.medianBlur(processed, 3)

    return processed
