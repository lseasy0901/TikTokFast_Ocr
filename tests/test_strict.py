#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
严格测试：只测试PSM和预处理，不进行任何字符替换
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


def filter_alnum_only(text: str) -> str:
    """只保留字母和数字，不做任何其他修改"""
    return ''.join(c for c in text if c.isalnum())


def test_strict():
    """严格测试"""
    print("=== 严格测试：PSM和预处理影响 ===")
    print("规则：")
    print("- 只保留 [A-Za-z0-9]")
    print("- 不替换任何字符")
    print("- 不进行语义推断")
    print("- 比较不同PSM/预处理对原始识别的影响\n")

    try:
        # 测试文本
        test_text = "I don't want spicy food."
        print(f"测试文本: '{test_text}'")
        print(f"期望结果: '{filter_alnum_only(test_text)}'\n")

        # 创建测试图像
        test_image = create_test_image(test_text, 300, 50)

        # 测试不同PSM模式
        psm_modes = [6, 7, 11]

        print("--- 不同PSM模式对比 ---")
        results_psm = {}

        for psm in psm_modes:
            engine = OCREngine(psm_modes=[psm])

            # 不使用预处理
            raw_no_pre = engine.recognize(test_image, preprocess=False)
            final_no_pre = filter_alnum_only(raw_no_pre)

            # 使用预处理
            raw_with_pre = engine.recognize(test_image, preprocess=True)
            final_with_pre = filter_alnum_only(raw_with_pre)

            results_psm[psm] = {
                'no_pre': {'raw': raw_no_pre, 'final': final_no_pre},
                'with_pre': {'raw': raw_with_pre, 'final': final_with_pre}
            }

            print(f"\nPSM {psm}:")
            print(f"  无预处理:")
            print(f"    原始OCR: '{raw_no_pre}'")
            print(f"    最终结果: '{final_no_pre}'")

            print(f"  有预处理:")
            print(f"    原始OCR: '{raw_with_pre}'")
            print(f"    最终结果: '{final_with_pre}'")

        # 比较完整度
        original_alnum = [c for c in test_text if c.isalnum()]
        original_set = set(original_alnum)

        print(f"\n--- 完整度分析 ---")
        best_psm = None
        best_score = 0

        for psm in psm_modes:
            # 使用预处理的结果
            final = results_psm[psm]['with_pre']['final']
            result_set = set([c for c in final if c.isalnum()])

            completeness = len(result_set & original_set) / len(original_set)

            print(f"PSM {psm}: {completeness:.2f} ({len(result_set & original_set)}/{len(original_set)})")

            if completeness > best_score:
                best_score = completeness
                best_psm = psm

        print(f"\n最佳PSM: {best_psm} (完整度: {best_score:.2f})")

        # 检查是否有字母总是丢失
        print(f"\n--- 字母丢失情况 ---")
        common_lost = set()
        for psm in psm_modes:
            final = results_psm[psm]['with_pre']['final']
            result_set = set([c for c in final if c.isalnum()])
            lost = original_set - result_set
            if lost:
                print(f"PSM {psm} 丢失: {lost}")
                common_lost = common_lost.intersection(lost)

        if common_lost:
            print(f"\n所有PSM都丢失的字母: {common_lost}")
        else:
            print("\n没有字母在所有PSM中都丢失")

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    test_strict()