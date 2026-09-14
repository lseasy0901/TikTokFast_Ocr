#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCRWorker测试文件

验证真实直播HUD识别流程：
1. 检查OCRWorker当前流程
2. 验证文本变化时是否更新clipboard
3. 验证空文本不会更新clipboard
4. 验证复制成功反馈显示
"""

import sys
import os
import time
import numpy as np
from unittest.mock import Mock, MagicMock, patch
from unittest import TestCase
from PySide6.QtWidgets import QApplication

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath('.')))
sys.path.insert(0, project_root)

# 初始化QApplication（必须在导入任何Qt模块之前）
app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

from ocr.worker import OCRWorker
from ocr.engine import OCREngine
from ocr.profiles import DELTA_FORCE
from ocr.roi_manager import ROIManager, ROI
from ocr.clipboard import ClipboardManager


class TestOCRWorker(TestCase):
    """OCRWorker测试类"""

    def setUp(self):
        """测试前准备"""
        # 创建模拟的frame buffer getter
        self.frame_buffer = Mock()
        self.frame_buffer.get.return_value = np.zeros((100, 200, 3), dtype=np.uint8)

        # 创建ROI管理器
        self.roi_manager = ROIManager()
        roi = ROI(0, 0, 100, 50, 100, 50)  # x, y, width, height, source_width, source_height
        self.roi_manager.set_roi(roi)

        # 创建OCR引擎
        self.engine = OCREngine()

        # 创建OCRWorker
        self.worker = OCRWorker(
            frame_buffer_getter=lambda: self.frame_buffer,
            roi_manager=self.roi_manager,
            engine=self.engine,
            interval_ms=100,
            profile=DELTA_FORCE.profile_id,
        )

        # 记录信号
        self.text_updates = []
        self.text_copied_signals = []

        def on_text_updated(text):
            self.text_updates.append(text)

        def on_text_copied(text):
            self.text_copied_signals.append(text)

        self.worker.text_updated.connect(on_text_updated)
        self.worker.text_copied.connect(on_text_copied)

    def tearDown(self):
        """测试后清理"""
        if self.worker.is_recognizing():
            self.worker.stop_recognition()
        self.worker.wait()

    def test_text_change_updates_clipboard(self):
        """测试文本变化时更新clipboard"""
        # 模拟OCR引擎返回不同的文本
        with patch.object(self.engine, 'recognize', side_effect=['Hello', 'World', 'Hello']):
            # 启动worker
            self.worker.start_recognition()
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")

            # 等待几次识别
            time.sleep(0.5)  # 增加等待时间

            # 停止worker
            self.worker.stop_recognition()
            self.worker.wait()

            # 验证信号
            self.assertEqual(len(self.text_updates), 2)  # 'Hello' 和 'World'
            self.assertEqual(self.text_updates[0], 'Hello')
            self.assertEqual(self.text_updates[1], 'World')

            # 验证clipboard更新（通过text_copied信号）
            self.assertEqual(len(self.text_copied_signals), 2)
            self.assertEqual(self.text_copied_signals[0], 'Hello')
            self.assertEqual(self.text_copied_signals[1], 'World')

    def test_empty_text_does_not_update_clipboard(self):
        """测试空文本不会更新clipboard"""
        # 模拟OCR引擎返回空文本
        with patch.object(self.engine, 'recognize', side_effect=['Hello', '', 'Hello']):
            # 启动worker
            self.worker.start_recognition()
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")

            # 等待几次识别
            time.sleep(0.5)  # 增加等待时间

            # 停止worker
            self.worker.stop_recognition()
            self.worker.wait()

            # 验证信号
            self.assertEqual(len(self.text_updates), 2)  # 'Hello' 和 'Hello'
            self.assertEqual(self.text_updates[0], 'Hello')
            self.assertEqual(self.text_updates[1], 'Hello')

            # 验证clipboard更新（空文本不应该触发）
            self.assertEqual(len(self.text_copied_signals), 1)  # 只有第一个'Hello'
            self.assertEqual(self.text_copied_signals[0], 'Hello')

    def test_copy_success_feedback(self):
        """测试复制成功反馈显示"""
        # 模拟OCR引擎返回文本
        with patch.object(self.engine, 'recognize', return_value='Test Text'):
            # 模拟clipboard复制成功
            with patch.object(self.worker._clipboard_manager, 'copy', return_value=True):
                # 启动worker
                self.worker.start_recognition()
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
                print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")

                # 等待识别
                time.sleep(0.1)

                # 停止worker
                self.worker.stop_recognition()
                self.worker.wait()

                # 验证信号
                self.assertEqual(len(self.text_copied_signals), 1)
                self.assertEqual(self.text_copied_signals[0], 'Test Text')

    def test_no_text_change_no_clipboard_update(self):
        """测试文本未变化时不更新clipboard"""
        # 模拟OCR引擎返回相同的文本
        with patch.object(self.engine, 'recognize', return_value='Same Text'):
            # 启动worker
            self.worker.start_recognition()
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")
            print(f"OCRWorker started, is_recognizing: {self.worker.is_recognizing()}")

            # 等待几次识别
            time.sleep(0.5)  # 增加等待时间

            # 停止worker
            self.worker.stop_recognition()
            self.worker.wait()

            # 验证信号
            self.assertEqual(len(self.text_updates), 1)  # 只有一次更新
            self.assertEqual(self.text_updates[0], 'Same Text')

            # 验证clipboard更新（只应该有一次）
            self.assertEqual(len(self.text_copied_signals), 1)
            self.assertEqual(self.text_copied_signals[0], 'Same Text')


if __name__ == '__main__':
    print("开始运行OCRWorker测试...")
    # 简单的测试运行，不使用unittest框架
    test = TestOCRWorker()
    test.setUp()

    print("\n1. 测试文本变化时更新clipboard:")
    test.test_text_change_updates_clipboard()
    print("   ✓ 通过")

    print("\n2. 测试空文本不会更新clipboard:")
    test.test_empty_text_does_not_update_clipboard()
    print("   ✓ 通过")

    print("\n3. 测试复制成功反馈显示:")
    test.test_copy_success_feedback()
    print("   ✓ 通过")

    print("\n4. 测试文本未变化时不更新clipboard:")
    test.test_no_text_change_no_clipboard_update()
    print("   ✓ 通过")

    test.tearDown()
    print("\n所有测试通过！")