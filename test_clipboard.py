#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试剪切板复制功能
"""

import sys
import time
from PySide6.QtWidgets import QApplication
from ocr.clipboard import ClipboardManager

def test_clipboard_copy():
    """测试剪切板复制功能"""
    # 初始化QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # 创建ClipboardManager
    clipboard = ClipboardManager()

    print("开始测试剪切板复制功能...")

    # 测试用例
    test_cases = [
        ("TEST123", True),  # 正常文本
        ("", False),       # 空文本
        ("   ", False),    # 纯空格
        ("TEST123", False), # 重复文本
    ]

    for text, expected in test_cases:
        print(f"\n测试: text='{text}'")
        result = clipboard.copy(text)
        print(f"结果: {result} (期望: {expected})")

        # 验证结果
        if result == expected:
            print("OK 通过")
        else:
            print("FAIL 失败")

    # 测试实际复制到系统剪切板
    print("\n测试实际复制到系统剪切板...")
    test_text = "实际复制测试"
    result = clipboard.copy(test_text)

    if result:
        print(f"Copy success: {test_text}")
        print("请按 Ctrl+V 粘贴，应该显示: " + test_text)
    else:
        print("✗ 复制失败")

    print("\n测试完成")

if __name__ == '__main__':
    test_clipboard_copy()