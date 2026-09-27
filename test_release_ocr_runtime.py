#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布运行环境回归测试 —— 随包 OCR 运行时的定位与提示文案

本文件只验证运行时定位与提示文案，不触碰 OCR 识别规则。

Tesseract 已退役
----------------
Tesseract 不再是生产 OCR 运行时：生产后端是 PP-OCRv6_medium_rec，且
runtime/tesseract/ 已从仓库删除。因此**不再**断言「随包 tesseract.exe /
eng.traineddata 存在」——那是旧运行时的前提，已经不存在了。

ocr.engine 的 OCREngine / TesseractLocator 仍然保留（诊断与
DLV_OCR_BACKEND=legacy 对照路径），它们的定位顺序、冻结环境的错误文案检查
一律保留；但不再要求仓库里存在随包 Tesseract 资产。

PP-OCRv6 打包契约
-----------------
生产模型必须落在 PPOCRModelLocator 探测的位置：

    <base>/runtime/ppocr/PP-OCRv6_medium_rec_infer

其中 <base> 在冻结环境是 sys._MEIPASS。本文件验证这份契约：定位顺序、
「0 字节 inference.pdiparams 不算已提供」的判据、冻结环境不回退开发路径、
以及 douyin_viewer.spec 声明了同一处落盘位置。

模型本体由 PP-OCRv6 打包阶段提供。仓库里还没有它时，仅**存在性**检查会显式
skip 并写明原因（打包阶段放入后即自动生效），其余契约检查照常执行。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ocr import engine as engine_mod
from ocr import ppocr_engine as ppocr_mod
from ocr.engine import OCREngine, OCREngineError, TesseractLocator, TesseractNotFoundError
from ocr.ppocr_engine import (
    MODEL_NAME,
    PPOCREngine,
    PPOCREngineError,
    PPOCRModelLocator,
    PPOCRModelNotFoundError,
)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(PROJECT_ROOT, "douyin_viewer.spec")

# PP-OCRv6 打包契约。必须与 douyin_viewer.spec 和 ocr/ppocr_engine.py 同时一致。
PPOCR_SUBDIR = "ppocr"
PPOCR_MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"
PPOCR_MODEL_PARAMS = "inference.pdiparams"
BUNDLED_PPOCR_MODEL = os.path.join(
    PROJECT_ROOT, "runtime", PPOCR_SUBDIR, PPOCR_MODEL_DIR_NAME
)
BUNDLED_PPOCR_PARAMS = os.path.join(BUNDLED_PPOCR_MODEL, PPOCR_MODEL_PARAMS)

# 退役的 Tesseract 资产：不得再出现在仓库里，也不得再被打包。
RETIRED_TESSERACT_ASSETS = (
    os.path.join(PROJECT_ROOT, "runtime", "tesseract", "tesseract.exe"),
    os.path.join(PROJECT_ROOT, "runtime", "tesseract", "tessdata", "eng.traineddata"),
)

PENDING_MODEL_REASON = (
    "PP-OCRv6 模型尚未放入 %s —— 由 PP-OCRv6 打包阶段提供，"
    "放入后本检查自动生效" % BUNDLED_PPOCR_MODEL
)


def provision_model_tree(base, params_bytes=b"stub weights"):
    """在 base 下放一份最小可用的随包模型目录，返回模型目录路径。"""
    model_dir = os.path.join(base, "runtime", PPOCR_SUBDIR, PPOCR_MODEL_DIR_NAME)
    os.makedirs(model_dir, exist_ok=True)
    with open(os.path.join(model_dir, PPOCR_MODEL_PARAMS), "wb") as f:
        f.write(params_bytes)
    return model_dir


def read_spec():
    with open(SPEC_PATH, encoding="utf-8") as f:
        return f.read()


class TestPytesseractDependency(unittest.TestCase):
    """pytesseract 仍是保留后端的运行依赖（诊断 / DLV_OCR_BACKEND=legacy）。"""

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

    def test_legacy_engine_resolves_an_existing_executable(self):
        """保留路径的定位结果必须是一个真实存在的可执行文件。

        Tesseract 随包资产已删除，因此不再断言"解析到仓库内的
        runtime/tesseract/tesseract.exe"（旧前提）。仍然成立的不变量是：
        TesseractLocator 按自己的顺序解析，且绝不返回一个不存在的路径。
        """
        resolved = TesseractLocator.find_tesseract()
        self.assertTrue(
            os.path.isfile(resolved),
            "TesseractLocator 返回了不存在的路径: %r" % resolved,
        )
        engine = OCREngine()
        self.assertEqual(
            os.path.normcase(engine_mod.pytesseract.pytesseract.tesseract_cmd),
            os.path.normcase(resolved),
        )


class TestTesseractAssetsRetired(unittest.TestCase):
    """退役的 Tesseract 资产不得回到仓库，也不得回到打包配置。"""

    def test_retired_assets_absent_from_disk(self):
        for path in RETIRED_TESSERACT_ASSETS:
            self.assertFalse(
                os.path.exists(path),
                "退役的 Tesseract 资产又被放回了仓库: %s" % path,
            )

    def test_spec_does_not_bundle_tesseract(self):
        spec = read_spec()
        for token in ("_TESSERACT", "_TESSDATA"):
            self.assertNotIn(
                token, spec,
                "spec 仍在打包 Tesseract 运行时（发现 %s）" % token,
            )
        self.assertNotIn(
            'os.path.join("runtime", "tesseract")', spec,
            "spec 仍把 Tesseract 作为落盘位置",
        )


class TestPPOCRModelResolution(unittest.TestCase):
    """PP-OCRv6 随包模型目录的定位契约。"""

    def test_model_dir_name_matches_production_model(self):
        """目录名必须由生产模型名派生，二者不能各自漂移。"""
        self.assertEqual(MODEL_NAME, "PP-OCRv6_medium_rec")
        self.assertEqual(PPOCR_MODEL_DIR_NAME, MODEL_NAME + "_infer")

    def test_bundled_candidate_layout(self):
        """<base>/runtime/ppocr/<模型目录名> 就是唯一随包落盘形状。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ppocr_mod, "_bundled_dirs", return_value=[tmp]):
                candidates = PPOCRModelLocator._bundled_candidates()
            self.assertEqual(
                [os.path.join(tmp, "runtime", PPOCR_SUBDIR, PPOCR_MODEL_DIR_NAME)],
                candidates,
            )

    def test_resolves_bundled_model_from_tree(self):
        """源码环境解析到随包 runtime/ppocr/，不落到开发目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            expected = provision_model_tree(tmp)
            with mock.patch.object(ppocr_mod, "_bundled_dirs", return_value=[tmp]), \
                 mock.patch.object(ppocr_mod, "_DEV_MODEL_DIR", os.path.join(tmp, "nope")):
                resolved = PPOCRModelLocator.find_model_dir()
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(expected))

    def test_zero_byte_params_is_not_provisioned(self):
        """0 字节 inference.pdiparams 不能算「已提供」。

        这条判据同时被 douyin_viewer.spec 的构建闸门镜像，两侧必须一致。
        """
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = provision_model_tree(tmp, params_bytes=b"")
            self.assertFalse(PPOCRModelLocator.is_usable_model_dir(model_dir))
            model_dir = provision_model_tree(tmp, params_bytes=b"w")
            self.assertTrue(PPOCRModelLocator.is_usable_model_dir(model_dir))

    def test_env_override_wins(self):
        """DLV_PPOCR_MODEL_DIR 是部署覆盖，优先于随包目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            override = provision_model_tree(os.path.join(tmp, "override"))
            with mock.patch.dict(os.environ, {"DLV_PPOCR_MODEL_DIR": override}):
                self.assertEqual(
                    os.path.normcase(PPOCRModelLocator.find_model_dir()),
                    os.path.normcase(override),
                )

    def test_env_override_must_be_usable(self):
        """覆盖值不可用时显式失败，不静默回落到别的目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"DLV_PPOCR_MODEL_DIR": os.path.join(tmp, "missing")}
            ):
                with self.assertRaises(PPOCRModelNotFoundError):
                    PPOCRModelLocator.find_model_dir()

    def test_frozen_env_never_falls_back_to_dev_dir(self):
        """冻结环境不查开发目录 —— 客户机上没有 D:\\new_model。

        回退只会把「随包文件缺失」掩盖成含糊的找不到模型。
        """
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = os.path.join(tmp, "DouyinLowLatencyViewer.exe")
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(sys, "executable", fake_exe):
                with self.assertRaises(PPOCRModelNotFoundError) as ctx:
                    PPOCRModelLocator.find_model_dir()
            self.assertNotIn(ppocr_mod._DEV_MODEL_DIR, str(ctx.exception))

    def test_frozen_env_resolves_via_meipass(self):
        """冻结 onedir：_MEIPASS 指向 _internal，随包 runtime/ppocr/ 在其下。"""
        with tempfile.TemporaryDirectory() as tmp:
            expected = provision_model_tree(tmp)
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertEqual(
                    os.path.normcase(PPOCRModelLocator.find_model_dir()),
                    os.path.normcase(expected),
                )

    def test_bundled_model_present_on_disk(self):
        """打包契约要求模型真的躺在那条路径上（打包阶段提供）。

        未提供时显式 skip 而不是失败：仓库当前处于「已声明前提、模型未放入」
        的中间态，硬断言会把打包阶段的待办变成一条常驻红灯。
        """
        if not os.path.isfile(BUNDLED_PPOCR_PARAMS):
            self.skipTest(PENDING_MODEL_REASON)
        self.assertTrue(PPOCRModelLocator.is_usable_model_dir(BUNDLED_PPOCR_MODEL))


class TestPPOCRNoSilentFallback(unittest.TestCase):
    """模型缺失必须是显式失败，不能吞成空文本或换后端。"""

    def test_missing_model_raises_not_empty_text(self):
        """引擎在定位阶段就失败：空文本在上游是「这帧没读到」，不是「引擎坏了」。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ppocr_mod, "_bundled_dirs", return_value=[tmp]), \
                 mock.patch.object(ppocr_mod, "_DEV_MODEL_DIR", os.path.join(tmp, "nope")), \
                 mock.patch.dict(os.environ, {"DLV_PPOCR_MODEL_DIR": ""}):
                with self.assertRaises(PPOCRModelNotFoundError):
                    PPOCREngine()

    def test_missing_model_error_is_an_ocr_engine_error(self):
        """继承 OCREngineError 是刻意的：worker 的既有失败路径无需改动即可接管。"""
        self.assertTrue(issubclass(PPOCRModelNotFoundError, PPOCREngineError))
        self.assertTrue(issubclass(PPOCREngineError, OCREngineError))

    def test_real_inference_round_trip_requires_provisioned_model(self):
        """真跑一次识别需要模型 + GPU，且只在模型已随包时才可能成立。"""
        if not os.path.isfile(BUNDLED_PPOCR_PARAMS):
            self.skipTest(PENDING_MODEL_REASON)
        self.skipTest(
            "需要 GPU + Paddle/PaddleX 运行时；本项在 PP-OCRv6 打包阶段的"
            "真机验收里执行（见 test_ppocrv6_backend.py）"
        )


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

    def test_frozen_missing_ppocr_model_message(self):
        """生产后端缺模型时，冻结环境同样只给重装指引。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = os.path.join(tmp, "DouyinLowLatencyViewer.exe")
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(sys, "executable", fake_exe):
                with self.assertRaises(PPOCRModelNotFoundError) as ctx:
                    PPOCRModelLocator.find_model_dir()
            message = str(ctx.exception)
        self.assertIn("重新安装 LiveLens", message)
        for word in self.DEVELOPER_WORDS:
            self.assertNotIn(word, message)
        self.assertNotIn("DLV_PPOCR_MODEL_DIR", message)

    def test_dev_environment_still_gets_developer_hint(self):
        """源码环境保留 pip 指引 —— 那里它才是可执行的建议。"""
        with mock.patch.object(engine_mod, "TESSERACT_AVAILABLE", False):
            with self.assertRaises(OCREngineError) as ctx:
                OCREngine()
            message = str(ctx.exception)
        self.assertIn("pip install pytesseract", message)


class TestSpecPackaging(unittest.TestCase):
    """spec 必须声明 PP-OCRv6 打包前提，并守住 pytesseract。"""

    def setUp(self):
        self.spec = read_spec()

    def test_spec_guards_pytesseract(self):
        """构建期缺 pytesseract 必须直接失败，不能再悄悄出一个坏包。"""
        self.assertIn("importlib.util.find_spec", self.spec)
        self.assertIn("pytesseract", self.spec)

    def test_spec_requires_ppocrv6_model(self):
        """构建闸门必须点名 PP-OCRv6 模型，否则 OCR 后端会被静默漏掉。"""
        self.assertIn(PPOCR_MODEL_DIR_NAME, self.spec)
        self.assertIn(PPOCR_MODEL_PARAMS, self.spec)
        self.assertIn("PP-OCRv6 model", self.spec)

    def test_spec_model_dest_matches_locator(self):
        """spec 的落盘位置与定位器的探测位置必须指向同一处。"""
        self.assertIn('_PPOCR_SUBDIR = "ppocr"', self.spec)
        self.assertIn('os.path.join("runtime", _PPOCR_SUBDIR)', self.spec)
        self.assertEqual(PPOCR_SUBDIR, "ppocr")

    def test_spec_declares_paddle_runtime_prerequisite(self):
        """Paddle/PaddleX 运行时的提供方式必须写在 spec 里（本阶段不实现）。"""
        self.assertIn("Paddle", self.spec)
        self.assertIn("packaging phase", self.spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
