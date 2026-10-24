#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
最终测试：模拟实际使用场景
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


def test_final_scenario():
    """测试最终场景"""
    print("=== 最终测试场景 ===")
    print("目标：只保留 A-Z、a-z、0-9，不能丢失有效字母\n")

    try:
        # 创建OCR引擎
        engine = OCREngine(psm_modes=[6, 7, 11])

        # 测试用例
        test_cases = [
            {
                "input": "I don't want spicy food.",
                "expected": "Idontwantspicyfood",
                "description": "包含撇号和空格"
            },
            {
                "input": "I dont want spicy food",
                "expected": "Idontwantspicyfood",
                "description": "无标点，有空格"
            },
            {
                "input": "Hello world",
                "expected": "Helloworld",
                "description": "简单英文"
            },
            {
                "input": "Hello, world!",
                "expected": "Helloworld",
                "description": "包含标点"
            },
            {
                "input": "123 test 456",
                "expected": "123test456",
                "description": "纯数字测试"
            },
            {
                "input": "A1B2C3",
                "expected": "A1B2C3",
                "description": "字母数字混合"
            }
        ]

        all_passed = True

        for i, test_case in enumerate(test_cases, 1):
            print(f"\n--- 测试用例 {i}: {test_case['description']} ---")
            print(f"输入: '{test_case['input']}'")
            print(f"期望: '{test_case['expected']}'")

            # 创建测试图像
            test_image = create_test_image(test_case['input'], 300, 50)

            # 识别文本
            raw_result = engine.recognize(test_image)
            final_result = filter_alnum(raw_result)

            print(f"OCR原始: '{raw_result}'")
            print(f"最终结果: '{final_result}'")

            # 检查结果
            original_alnum = [c for c in test_case['input'] if c.isalnum()]
            result_alnum = [c for c in final_result if c.isalnum()]

            print(f"原始字母数字: {original_alnum}")
            print(f"结果字母数字: {result_alnum}")

            # 检查是否通过
            if set(result_alnum) == set(test_case['expected']):
                print("通过")
            else:
                print("失败")
                all_passed = False

            # 详细检查是否有字母丢失
            missing = set(original_alnum) - set(result_alnum)
            extra = set(result_alnum) - set(original_alnum)

            if missing:
                print(f"警告: 丢失字母: {missing}")
            if extra:
                print(f"警告: 额外字母: {extra}")

        print(f"\n=== 总结 ===")
        if all_passed:
            print("所有测试用例都通过了！")
        else:
            print("部分测试用例失败，需要进一步优化。")

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    test_final_scenario()