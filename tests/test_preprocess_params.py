#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试不同预处理参数的影响
"""

import sys
import os
import numpy as np
import cv2

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.abspath('.'))
sys.path.insert(0, project_root)

from ocr.engine import OCREngine
from ocr.preprocess import preprocess_for_ocr


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


def test_preprocess_params():
    """测试不同预处理参数"""
    print("=== 测试不同预处理参数的影响 ===")
    print("测试文本: 'I don't want spicy food.'\n")

    # 创建测试图像
    test_image = create_test_image("I don't want spicy food.", 300, 50)

    # 测试不同的预处理组合
    configs = [
        {"name": "默认配置", "grayscale": True, "scale_factor": 2.0, "threshold": 128, "denoise": True},
        {"name": "不灰度化", "grayscale": False, "scale_factor": 2.0, "threshold": 128, "denoise": True},
        {"name": "不放大", "grayscale": True, "scale_factor": 1.0, "threshold": 128, "denoise": True},
        {"name": "低阈值", "grayscale": True, "scale_factor": 2.0, "threshold": 100, "denoise": True},
        {"name": "高阈值", "grayscale": True, "scale_factor": 2.0, "threshold": 150, "denoise": True},
        {"name": "不降噪", "grayscale": True, "scale_factor": 2.0, "threshold": 128, "denoise": False},
        {"name": "自适应阈值", "grayscale": True, "scale_factor": 2.0, "threshold": 0, "denoise": True},
    ]

    try:
        for config in configs:
            print(f"--- {config['name']} ---")

            if config['threshold'] == 0:
                # 使用自适应阈值
                from ocr.preprocess import preprocess_for_ocr_adaptive
                processed = preprocess_for_ocr_adaptive(
                    test_image,
                    scale_factor=config['scale_factor']
                )
            else:
                # 使用普通预处理
                processed = preprocess_for_ocr(
                    test_image,
                    grayscale=config['grayscale'],
                    scale_factor=config['scale_factor'],
                    threshold=config['threshold'],
                    denoise=config['denoise']
                )

            # 保存处理后的图像
            filename = f"debug_{config['name'].replace(' ', '_').replace('(', '').replace(')', '')}.png"
            cv2.imwrite(filename, processed)
            print(f"已保存: {filename}")

            # OCR识别
            engine = OCREngine(psm_modes=[7, 6, 11])
            raw_result = engine.recognize(test_image, preprocess=False)  # 手动应用预处理
            final_result = filter_alnum_only(raw_result)

            print(f"原始OCR: '{raw_result}'")
            print(f"最终结果: '{final_result}'")

            # 检查是否有I被识别为1
            if '1' in raw_result and 'I' not in raw_result:
                print("警告: I被识别为1")

            print()

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_psm_without_preprocess():
    """测试不使用预处理时的PSM表现"""
    print("\n=== 不使用预处理的PSM测试 ===")
    print("测试文本: 'I don't want spicy food.'\n")

    # 创建测试图像
    test_image = create_test_image("I don't want spicy food.", 300, 50)

    try:
        for psm in [6, 7, 11]:
            print(f"--- PSM {psm} (无预处理) ---")

            engine = OCREngine(psm_modes=[psm])
            raw_result = engine.recognize(test_image, preprocess=False)
            final_result = filter_alnum_only(raw_result)

            print(f"原始OCR: '{raw_result}'")
            print(f"最终结果: '{final_result}'")

            # 检查完整度
            original_alnum = [c for c in "I don't want spicy food." if c.isalnum()]
            result_alnum = [c for c in final_result if c.isalnum()]
            completeness = len(set(result_alnum)) / len(set(original_alnum))

            print(f"完整度: {completeness:.2f}")

            print()

    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    test_preprocess_params()
    test_psm_without_preprocess()