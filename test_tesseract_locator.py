#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TesseractLocator测试文件

测试TesseractLocator类的功能：
1. 当前电脑安装环境测试
2. 模拟runtime/tesseract结构测试
3. 路径查找逻辑测试
4. 异常处理测试
"""

import os
import sys
import tempfile
import shutil
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ocr.engine import TesseractLocator, TesseractNotFoundError


class TestTesseractLocator(TestCase):
    """TesseractLocator测试类"""

    def setUp(self):
        """测试前准备"""
        self.test_dir = tempfile.mkdtemp()
        self.test_runtime_dir = os.path.join(self.test_dir, "runtime", "tesseract")
        os.makedirs(self.test_runtime_dir, exist_ok=True)

    def tearDown(self):
        """测试后清理"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_find_tesseract_current_system(self):
        """测试当前系统环境下的Tesseract查找"""
        try:
            tesseract_path = TesseractLocator.find_tesseract()
            self.assertIsNotNone(tesseract_path)
            self.assertTrue(os.path.exists(tesseract_path))
            print(f"当前系统Tesseract路径: {tesseract_path}")
        except TesseractNotFoundError:
            self.skipTest("当前系统未安装Tesseract")

    def test_find_tessdata_current_system(self):
        """测试当前系统环境下的Tessdata查找"""
        try:
            # 首先找到tesseract路径
            tesseract_path = TesseractLocator.find_tesseract()
            tessdata_path = TesseractLocator.find_tessdata(tesseract_path)
            self.assertIsNotNone(tessdata_path)
            self.assertTrue(os.path.exists(tessdata_path))
            print(f"当前系统Tessdata路径: {tessdata_path}")
        except TesseractNotFoundError:
            self.skipTest("当前系统未安装Tesseract或Tessdata")

    def test_find_tesseract_in_runtime(self):
        """测试在runtime目录中查找Tesseract"""
        # 创建模拟的runtime/tesseract/tesseract.exe
        tesseract_exe = os.path.join(self.test_runtime_dir, "tesseract.exe")
        os.makedirs(os.path.dirname(tesseract_exe), exist_ok=True)
        with open(tesseract_exe, 'w') as f:
            f.write("# Mock tesseract executable")

        # 修改sys.path模拟源码环境
        original_path = sys.path[0]
        sys.path[0] = self.test_dir

        try:
            # 使用TesseractLocator查找（测试模式）
            tesseract_path = TesseractLocator.find_tesseract(test_mode=True)
            self.assertEqual(tesseract_path, tesseract_exe)
            print(f"在runtime目录中找到Tesseract: {tesseract_path}")
        finally:
            sys.path[0] = original_path

    def test_find_tessdata_in_runtime(self):
        """测试在runtime目录中查找Tessdata"""
        # 创建模拟的tesseract.exe和tessdata目录
        tesseract_exe = os.path.join(self.test_runtime_dir, "tesseract.exe")
        with open(tesseract_exe, 'w') as f:
            f.write("# Mock tesseract executable")

        tessdata_dir = os.path.join(self.test_runtime_dir, "tessdata")
        os.makedirs(tessdata_dir, exist_ok=True)
        eng_traineddata = os.path.join(tessdata_dir, "eng.traineddata")
        with open(eng_traineddata, 'w') as f:
            f.write("# Mock eng.traineddata")

        # 修改sys.path模拟源码环境
        original_path = sys.path[0]
        sys.path[0] = self.test_dir

        try:
            # 使用TesseractLocator查找
            tessdata_path = TesseractLocator.find_tessdata(tesseract_exe)
            self.assertEqual(tessdata_path, tessdata_dir)
            print(f"在runtime目录中找到Tessdata: {tessdata_path}")
        finally:
            sys.path[0] = original_path

    def test_get_tesseract_version(self):
        """测试获取Tesseract版本"""
        try:
            tesseract_path = TesseractLocator.find_tesseract()
            version = TesseractLocator.get_tesseract_version(tesseract_path)
            self.assertIsNotNone(version)
            # 不假设具体版本，只检查格式
            self.assertTrue(version and version != "unknown")
            print(f"Tesseract版本: {version}")
        except TesseractNotFoundError:
            self.skipTest("当前系统未安装Tesseract")

    def test_find_tesseract_in_pyinstaller(self):
        """测试在PyInstaller环境下的Tesseract查找"""
        # 模拟PyInstaller环境
        # 使用setattr代替patch，因为sys.frozen可能不存在
        original_frozen = getattr(sys, 'frozen', None)
        original_meipass = getattr(sys, '_MEIPASS', None)

        try:
            setattr(sys, 'frozen', True)
            setattr(sys, '_MEIPASS', self.test_dir)

            # 创建模拟的tesseract.exe
            tesseract_exe = os.path.join(self.test_dir, "runtime", "tesseract", "tesseract.exe")
            os.makedirs(os.path.dirname(tesseract_exe), exist_ok=True)
            with open(tesseract_exe, 'w') as f:
                f.write("# Mock tesseract executable")

            # 修改sys.path模拟源码环境
            original_path = sys.path[0]
            sys.path[0] = self.test_dir

            try:
                # 测试查找
                tesseract_path = TesseractLocator.find_tesseract(test_mode=True)
                self.assertEqual(tesseract_path, tesseract_exe)
                print(f"在PyInstaller环境中找到Tesseract: {tesseract_path}")
            finally:
                sys.path[0] = original_path
        finally:
            # 恢复原始值
            if original_frozen is not None:
                setattr(sys, 'frozen', original_frozen)
            else:
                delattr(sys, 'frozen')

            if original_meipass is not None:
                setattr(sys, '_MEIPASS', original_meipass)
            else:
                delattr(sys, '_MEIPASS')

    def test_tesseract_not_found(self):
        """测试Tesseract未找到的情况"""
        # 在测试模式下，确保没有找到任何Tesseract
        with patch('subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaises(TesseractNotFoundError):
                TesseractLocator.find_tesseract(test_mode=True)

    def test_tessdata_not_found(self):
        """测试Tessdata未找到的情况"""
        # 创建一个不存在的tesseract路径
        fake_tesseract_path = os.path.join(self.test_dir, "fake_tesseract.exe")
        with open(fake_tesseract_path, 'w') as f:
            f.write("# Fake tesseract")

        with self.assertRaises(TesseractNotFoundError):
            TesseractLocator.find_tessdata(fake_tesseract_path)


if __name__ == '__main__':
    print("开始运行TesseractLocator测试...")
    main()