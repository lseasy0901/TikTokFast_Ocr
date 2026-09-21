#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布运行环境回归测试 —— 随包 OCR 运行时

覆盖事故的两条根因（两者都只在安装包上暴露）：
  1. pytesseract 不在 requirements.txt 里，构建环境没有它，冻结包里就没有它。
     spec 的 hiddenimports 写着 "pytesseract" 也救不回来：PyInstaller 对找不到的
     hidden import 只警告不报错。结果 `import pytesseract` 在客户机上失败，
     TESSERACT_AVAILABLE=False，OCR 一次都没跑过。
  2. find_tesseract() 用 ocr/ 目录去拼 runtime/，源码树和冻结包里都找不到随包的
     tesseract.exe，于是回退到 C:\\Program Files 与 PATH —— 开发机上碰巧能跑，
     客户机上必然失败。

本文件只验证运行时定位与提示文案，不触碰 OCR 识别规则。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ocr import engine as engine_mod
from ocr.engine import OCREngine, OCREngineError, TesseractLocator, TesseractNotFoundError

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BUNDLED_TESSERACT = os.path.join(PROJECT_ROOT, "runtime", "tesseract", "tesseract.exe")
BUNDLED_TESSDATA = os.path.join(PROJECT_ROOT, "runtime", "tesseract", "tessdata")


class TestPytesseractDependency(unittest.TestCase):
    """pytesseract 必须是一个真正的、可 import 的运行依赖。"""

    def test_pytesseract_importable(self):
        self.assertTrue(
            engine_mod.TESSERACT_AVAILABLE,
            "pytesseract 必须在构建/运行环境中可 import（见 requirements.txt）",
        )
        self.assertIsNotNone(engine_mod.pytesseract)

    def test_pytesseract_declared_in_requirements(self):
        """依赖声明必须写进 requirements.txt —— 构建环境靠它安装。"""
        req = os.path.join(PROJECT_ROOT, "requirements.txt")
        with open(req, encoding="utf-8") as f:
            # 去掉注释行后再判断，避免注释里提到包名就算通过
            lines = [
                line.split("#", 1)[0].strip().lower()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        self.assertIn("pytesseract", lines)
        # pytesseract 在模块顶层 import PIL.Image，冻结包缺了 PIL 一样起不来
        self.assertIn("pillow", lines)

    def test_engine_constructs_in_source_env(self):
        """源码环境：引擎必须能构造，且用随包 Tesseract，而不是系统安装。"""
        engine = OCREngine()
        resolved = engine_mod.pytesseract.pytesseract.tesseract_cmd
        self.assertEqual(
            os.path.normcase(resolved), os.path.normcase(BUNDLED_TESSERACT)
        )


class TestBundledPathResolution(unittest.TestCase):
    """随包 tesseract.exe / tessdata 的定位规则。"""

    def test_source_env_resolves_bundled_not_system(self):
        """源码环境解析到仓库内的 runtime/，不落到系统安装目录或 PATH。"""
        resolved = TesseractLocator.find_tesseract()
        self.assertEqual(
            os.path.normcase(resolved), os.path.normcase(BUNDLED_TESSERACT)
        )

    def test_tessdata_beside_executable(self):
        """tessdata 的主规则是"与 tesseract.exe 同级"。"""
        resolved = TesseractLocator.find_tessdata(BUNDLED_TESSERACT)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(BUNDLED_TESSDATA))

    def test_frozen_env_resolves_via_meipass(self):
        """冻结 onedir：_MEIPASS 指向 _internal，随包 runtime/ 在其下。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", PROJECT_ROOT, create=True):
            self.assertEqual(
                os.path.normcase(TesseractLocator.find_tesseract()),
                os.path.normcase(BUNDLED_TESSERACT),
            )

    def test_frozen_env_resolves_via_exe_dir(self):
        """无 _MEIPASS 时（Velopack 安装目录形态）回退到 exe 同级目录。"""
        with mock.patch.object(sys, "frozen", True, create=True):
            if hasattr(sys, "_MEIPASS"):
                self.skipTest("当前解释器自带 _MEIPASS，无法模拟缺失场景")
            fake_exe = os.path.join(PROJECT_ROOT, "DouyinLowLatencyViewer.exe")
            with mock.patch.object(sys, "executable", fake_exe):
                self.assertEqual(
                    os.path.normcase(TesseractLocator.find_tesseract()),
                    os.path.normcase(BUNDLED_TESSERACT),
                )

    def test_frozen_never_falls_back_to_path(self):
        """冻结环境绝不查 PATH —— 客户机上没有 Tesseract，回退只会掩盖缺包。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", os.path.join(PROJECT_ROOT, "nope"), create=True), \
             mock.patch.object(sys, "executable", os.path.join(PROJECT_ROOT, "nope", "app.exe")), \
             mock.patch.object(engine_mod.shutil, "which") as which:
            with self.assertRaises(TesseractNotFoundError):
                TesseractLocator.find_tesseract()
        which.assert_not_called()


class TestUserFacingErrors(unittest.TestCase):
    """安装包里不能出现开发环境的说法（pip / PATH / 手动安装）。"""

    DEVELOPER_WORDS = ("pip install", "PATH", "Tesseract-OCR")

    def test_frozen_missing_tesseract_message(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", os.path.join(PROJECT_ROOT, "nope"), create=True), \
             mock.patch.object(sys, "executable", os.path.join(PROJECT_ROOT, "nope", "app.exe")):
            with self.assertRaises(TesseractNotFoundError) as ctx:
                TesseractLocator.find_tesseract()
            message = str(ctx.exception)
        self.assertIn("重新安装 LiveLens", message)
        for word in self.DEVELOPER_WORDS:
            self.assertNotIn(word, message)

    def test_frozen_engine_error_message(self):
        """缺 pytesseract 时，冻结环境给的是重装指引，不是 pip 命令。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(engine_mod, "TESSERACT_AVAILABLE", False):
            with self.assertRaises(OCREngineError) as ctx:
                OCREngine()
            message = str(ctx.exception)
        self.assertIn("重新安装 LiveLens", message)
        for word in self.DEVELOPER_WORDS:
            self.assertNotIn(word, message)

    def test_frozen_missing_tessdata_message(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(engine_mod, "_bundled_dirs", return_value=[os.path.join(PROJECT_ROOT, "nope")]):
            with self.assertRaises(TesseractNotFoundError) as ctx:
                TesseractLocator.find_tessdata(
                    os.path.join(PROJECT_ROOT, "nope", "tesseract.exe")
                )
            message = str(ctx.exception)
        self.assertIn("重新安装 LiveLens", message)

    def test_dev_environment_still_gets_developer_hint(self):
        """源码环境保留 pip 指引 —— 那里它才是可执行的建议。"""
        with mock.patch.object(engine_mod, "TESSERACT_AVAILABLE", False):
            with self.assertRaises(OCREngineError) as ctx:
                OCREngine()
            message = str(ctx.exception)
        self.assertIn("pip install pytesseract", message)


class TestSpecPackaging(unittest.TestCase):
    """spec 必须继续打包 Tesseract 运行时，并守住 pytesseract。"""

    def setUp(self):
        spec_path = os.path.join(PROJECT_ROOT, "douyin_viewer.spec")
        with open(spec_path, encoding="utf-8") as f:
            self.spec = f.read()

    def test_spec_guards_pytesseract(self):
        """构建期缺 pytesseract 必须直接失败，不能再悄悄出一个坏包。"""
        self.assertIn("importlib.util.find_spec", self.spec)
        self.assertIn("pytesseract", self.spec)

    def test_spec_bundles_tesseract_runtime(self):
        self.assertIn('"pytesseract"', self.spec)          # hiddenimports
        self.assertIn('os.path.join("runtime", "tesseract")', self.spec)  # 落盘位置
        self.assertIn("_TESSDATA_SRC", self.spec)

    def test_bundled_assets_present_on_disk(self):
        """spec 的落盘路径与定位器必须指向同一处。"""
        self.assertTrue(os.path.isfile(BUNDLED_TESSERACT))
        self.assertTrue(os.path.isdir(BUNDLED_TESSDATA))
        self.assertTrue(
            os.path.isfile(os.path.join(BUNDLED_TESSDATA, "eng.traineddata"))
        )


class TestRealOCRThroughBundledRuntime(unittest.TestCase):
    """真跑一次识别，证明随包运行时确实可用（不是只解析出路径）。"""

    def test_recognize_rendered_text(self):
        import cv2
        import numpy as np

        image = np.full((80, 320), 255, dtype=np.uint8)
        cv2.putText(image, "AB1234", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)

        engine = OCREngine(psm_modes=[7])
        text = engine.recognize(image).replace(" ", "")
        self.assertIn("1234", text, f"OCR 未识别出渲染文本，实际得到: {text!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
