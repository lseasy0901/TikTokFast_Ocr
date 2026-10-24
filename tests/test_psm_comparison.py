#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试不同PSM模式对字母数字识别完整度的影响
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


def filter_alnum(text: str) -> str:
    """只保留字母和数字"""
    return ''.join(c for c in text if c.isalnum())


def test_psm_modes():
    """测试不同PSM模式"""
    print("=== 测试不同PSM模式对字母数字识别的影响 ===")

    try:
        # 测试用例
        test_cases = [
            "I don't want spicy food.",
            "I dont want spicy food",
            "Hello world",
            "Hello, world!",
            "123 test 456"
        ]

        for test_text in test_cases:
            print(f"\n--- 测试文本: '{test_text}' ---")

            # 创建测试图像
            test_image = create_test_image(test_text, 300, 50)

            # 测试不同PSM模式
            psm_modes = [6, 7, 11]
            results = {}

            for psm in psm_modes:
                # 创建专门引擎测试PSM
                engine = OCREngine(psm_modes=[psm])
                raw_result = engine.recognize(test_image)
                filtered_result = filter_alnum(raw_result)

                # 计算完整度
                original_alnum = [c for c in test_text if c.isalnum()]
                result_alnum = [c for c in filtered_result if c.isalnum()]

                completeness = len(set(result_alnum)) / len(set(original_alnum)) if original_alnum else 0

                results[psm] = {
                    'raw': raw_result,
                    'filtered': filtered_result,
                    'completeness': completeness,
                    'original_alnum': original_alnum,
                    'result_alnum': result_alnum
                }

                print(f"\nPSM {psm}:")
                print(f"  原始: '{raw_result}'")
                print(f"  过滤后: '{filtered_result}'")
                print(f"  完整度: {completeness:.2f}")
                print(f"  原始字母: {original_alnum}")
                print(f"  结果字母: {result_alnum}")

                # 检查是否有字母丢失
                if set(original_alnum) != set(result_alnum):
                    missing = set(original_alnum) - set(result_alnum)
                    extra = set(result_alnum) - set(original_alnum)
                    print(f"  警告: 丢失字母: {missing}")
                    if extra:
                        print(f"  额外字母: {extra}")

            # 找出最佳PSM
            best_psm = max(results.keys(), key=lambda p: results[p]['completeness'])
            print(f"\n最佳PSM: {best_psm} (完整度: {results[best_psm]['completeness']:.2f})")

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_preprocessing_impact():
    """测试预处理的影响"""
    print("\n=== 测试预处理对识别的影响 ===")

    try:
        # 测试文本
        test_text = "I don't want spicy food."
        test_image = create_test_image(test_text, 300, 50)

        # 创建引擎
        engine = OCREngine(psm_modes=[11, 7, 6])

        # 不使用预处理
        print("\n--- 不使用预处理 ---")
        result_no_preprocess = engine.recognize(test_image, preprocess=False)
        filtered_no_preprocess = filter_alnum(result_no_preprocess)
        print(f"原始: '{result_no_preprocess}'")
        print(f"过滤后: '{filtered_no_preprocess}'")

        # 使用预处理
        print("\n--- 使用预处理 ---")
        result_with_preprocess = engine.recognize(test_image, preprocess=True)
        filtered_with_preprocess = filter_alnum(result_with_preprocess)
        print(f"原始: '{result_with_preprocess}'")
        print(f"过滤后: '{filtered_with_preprocess}'")

        # 比较
        original_alnum = [c for c in test_text if c.isalnum()]
        no_preprocess_alnum = [c for c in filtered_no_preprocess if c.isalnum()]
        with_preprocess_alnum = [c for c in filtered_with_preprocess if c.isalnum()]

        print(f"\n比较:")
        print(f"原始字母: {original_alnum}")
        print(f"无预处理: {no_preprocess_alnum}")
        print(f"有预处理: {with_preprocess_alnum}")

        no_preprocess_completeness = len(set(no_preprocess_alnum)) / len(set(original_alnum)) if original_alnum else 0
        with_preprocess_completeness = len(set(with_preprocess_alnum)) / len(set(original_alnum)) if original_alnum else 0

        print(f"\n完整度:")
        print(f"无预处理: {no_preprocess_completeness:.2f}")
        print(f"有预处理: {with_preprocess_completeness:.2f}")

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    test_psm_modes()
    test_preprocessing_impact()