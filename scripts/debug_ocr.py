#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
调试OCR详细输出
"""

import sys
import os
import numpy as np
import cv2

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


def debug_ocr_detailed():
    """详细调试OCR输出"""
    print("=== 详细调试OCR输出 ===")

    try:
        # 创建OCR引擎
        engine = OCREngine()

        # 测试文本
        test_text = "I don't want spicy food."
        print(f"原始文本: '{test_text}'")

        # 创建测试图像
        test_image = create_test_image(test_text, 300, 50)

        # 使用详细模式获取更多信息
        result = engine.recognize(test_image, return_details=True)

        print(f"OCR结果: '{result['text']}'")
        print(f"置信度: {result['confidence']}")
        print(f"使用PSM: {result['psm_used']}")

        # 查看详细识别信息
        details = result['details']
        print("\n详细识别信息:")
        for i in range(len(details['text'])):
            text = details['text'][i]
            conf = details['conf'][i]
            x = details['left'][i]
            y = details['top'][i]
            w = details['width'][i]
            h = details['height'][i]

            if text.strip():  # 只显示非空文本
                print(f"  字符 '{text}' - 位置:({x},{y}) 大小:{w}x{h} 置信度:{conf}")

        # 检查缺失的字符
        original_chars = list(test_text)
        result_chars = list(result['text'])

        print(f"\n原始字符: {original_chars}")
        print(f"结果字符: {result_chars}")

        missing = [c for c in original_chars if c not in result_chars]
        extra = [c for c in result_chars if c not in original_chars]

        if missing:
            print(f"缺失字符: {missing}")
        if extra:
            print(f"多余字符: {extra}")

    except Exception as e:
        print(f"调试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    debug_ocr_detailed()