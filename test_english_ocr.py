#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试英文字幕OCR准确率问题
"""

import sys
import os
import numpy as np
import cv2
from unittest.mock import Mock, patch

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.abspath('.'))
sys.path.insert(0, project_root)

from ocr.engine import OCREngine


def create_test_image(text: str, width=300, height=50) -> np.ndarray:
    """创建测试图像"""
    # 创建白色背景
    image = np.ones((height, width, 3), dtype=np.uint8) * 255

    # 设置字体
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.8
    thickness = 2

    # 获取文本大小
    (text_width, text_height), baseline = cv2.getTextSize(text, font, font_scale, thickness)

    # 计算居中位置
    x = (width - text_width) // 2
    y = (height + text_height) // 2

    # 绘制文本
    cv2.putText(image, text, (x, y), font, font_scale, 0, thickness)

    return image


def test_current_behavior():
    """测试当前OCR行为"""
    print("=== 测试当前OCR行为 ===")

    try:
        # 创建OCR引擎
        engine = OCREngine()
        print("OCR引擎初始化成功")

        # 测试用例
        test_cases = [
            "I don't want spicy food.",
            "I dont want spicy food",
            "Hello world",
            "Hello, world!",
            "123 test 456"
        ]

        for text in test_cases:
            print(f"\n测试文本: '{text}'")

            # 创建测试图像
            test_image = create_test_image(text, 300, 50)

            # 识别文本
            result = engine.recognize(test_image)
            print(f"识别结果: '{result}'")

            # 检查是否丢失了有效字母
            original_letters = [c for c in text if c.isalnum()]
            result_letters = [c for c in result if c.isalnum()]

            if set(original_letters) != set(result_letters):
                print(f"警告: 字母丢失! 原始: {original_letters}, 结果: {result_letters}")
            else:
                print(f"成功: 所有字母都保留了")

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_psm_modes():
    """测试不同PSM模式"""
    print("\n=== 测试不同PSM模式 ===")

    try:
        # 创建OCR引擎，指定PSM模式
        engine = OCREngine(psm_modes=[6, 7, 11])

        # 测试文本
        test_text = "I don't want spicy food."
        test_image = create_test_image(test_text, 300, 50)

        # 测试不同PSM模式
        psm_results = {}

        for psm in [6, 7, 11]:
            print(f"\n测试 PSM {psm}:")

            # 临时修改PSM模式
            engine.psm_modes = [psm]
            result = engine.recognize(test_image)
            print(f"识别结果: '{result}'")

            psm_results[psm] = result

            # 检查质量
            original_letters = [c for c in test_text if c.isalnum()]
            result_letters = [c for c in result if c.isalnum()]

            if set(original_letters) == set(result_letters):
                print(f"成功: 字母完整保留")
            else:
                print(f"失败: 字母丢失: {original_letters} -> {result_letters}")

        # 比较结果
        print(f"\n=== PSM模式比较 ===")
        for psm, result in psm_results.items():
            print(f"PSM {psm}: '{result}'")

    except Exception as e:
        print(f"PSM测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_different_whitelists():
    """测试不同字符白名单"""
    print("\n=== 测试不同字符白名单 ===")

    try:
        # 创建测试图像
        test_text = "I don't want spicy food."
        test_image = create_test_image(test_text, 300, 50)

        # 测试不同白名单
        whitelists = [
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:-",  # 当前
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",  # 不含标点
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!'\"?-",  # 含更多标点
        ]

        for i, whitelist in enumerate(whitelists):
            print(f"\n测试白名单 {i+1}: {whitelist}")

            # 创建引擎
            engine = OCREngine(whitelist=whitelist)
            result = engine.recognize(test_image)
            print(f"识别结果: '{result}'")

    except Exception as e:
        print(f"白名单测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    test_current_behavior()
    test_psm_modes()
    test_different_whitelists()