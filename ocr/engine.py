# -*- coding: utf-8 -*-
"""
OCR 引擎模块 (Phase 2.2 Step 1)

基于 Tesseract OCR 的游戏 HUD 识别引擎。

支持：
- 英文 + 数字识别
- 多 PSM 模式（默认 6 统一文本块 / 7 单行 / 11 稀疏文本，按顺序尝试）
- 字符白名单限制
- 灵活的配置选项

读法入口有两个：
- recognize()：返回第一个非空的 PSM 读法（原行为）
- iter_readings()：按 PSM 顺序逐个产出读法，供上层做多读法仲裁
"""

import cv2
import numpy as np
import logging
import os
from typing import Optional, List

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False
    logger = logging.getLogger("DouyinLowLatencyViewer.ocr.engine")
    logger.warning("pytesseract 未安装，OCR 功能不可用。请运行: pip install pytesseract")

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.engine")

__all__ = ["OCREngine", "OCREngineError"]


class TesseractNotFoundError(Exception):
    """Tesseract OCR 未找到异常"""
    pass


class TesseractLocator:
    """
    Tesseract路径定位器

    负责查找tesseract.exe和tessdata路径，支持多种环境：
    1. PyInstaller环境
    2. 源码开发环境
    3. 系统安装
    4. PATH环境变量
    """

    @staticmethod
    def find_tesseract(test_mode: bool = False) -> str:
        """
        查找tesseract.exe路径

        参数：
            test_mode: 测试模式，优先查找测试目录

        返回：
            tesseract.exe的完整路径

        异常：
            TesseractNotFoundError: 如果未找到tesseract
        """
        import sys
        import os

        # 测试模式：优先查找测试目录，并禁用系统路径
        if test_mode:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            tesseract_path = os.path.join(current_dir, "runtime", "tesseract", "tesseract.exe")
            if os.path.exists(tesseract_path):
                return tesseract_path
            # 测试模式下不查找系统路径
            raise TesseractNotFoundError("测试模式下未找到Tesseract")

        # 第一优先级：PyInstaller环境
        if getattr(sys, "frozen", False):
            # sys._MEIPASS存在（PyInstaller onefile模式）
            if hasattr(sys, '_MEIPASS'):
                meipass_path = sys._MEIPASS
                tesseract_path = os.path.join(meipass_path, "runtime", "tesseract", "tesseract.exe")
                if os.path.exists(tesseract_path):
                    return tesseract_path

                # exe目录/runtime/tesseract/tesseract.exe
                exe_dir = os.path.dirname(sys.executable)
                tesseract_path = os.path.join(exe_dir, "runtime", "tesseract", "tesseract.exe")
                if os.path.exists(tesseract_path):
                    return tesseract_path

        # 第二优先级：源码环境
        current_dir = os.path.dirname(os.path.abspath(__file__))
        tesseract_path = os.path.join(current_dir, "runtime", "tesseract", "tesseract.exe")
        if os.path.exists(tesseract_path):
            return tesseract_path

        # 第三优先级：系统安装路径
        system_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
        ]

        for path in system_paths:
            if os.path.exists(path):
                return path

        # 第四优先级：PATH环境变量
        try:
            import subprocess
            result = subprocess.run(['where', 'tesseract'], capture_output=True, text=True, check=True)
            lines = result.stdout.strip().split('\n')
            if lines:
                return lines[0].strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        # 如果全部失败
        raise TesseractNotFoundError(
            "Tesseract OCR 未找到。请确保已安装Tesseract-OCR并添加到系统PATH，或提供runtime/tesseract目录。"
        )

    @staticmethod
    def find_tessdata(tesseract_path: str) -> str:
        """
        查找tessdata路径

        参数：
            tesseract_path: tesseract.exe的路径

        返回：
            tessdata目录的路径

        异常：
            TesseractNotFoundError: 如果未找到tessdata
        """
        import os

        # 尝试从runtime/tesseract/tessdata查找
        current_dir = os.path.dirname(os.path.abspath(__file__))
        tessdata_path = os.path.join(current_dir, "runtime", "tesseract", "tessdata")
        if os.path.exists(tessdata_path):
            return tessdata_path

        # 尝试从tesseract.exe所在目录的父目录查找
        tesseract_dir = os.path.dirname(tesseract_path)
        tessdata_path = os.path.join(tesseract_dir, "tessdata")
        if os.path.exists(tessdata_path):
            return tessdata_path

        # 尝试系统默认路径
        system_paths = [
            r"C:\Program Files\Tesseract-OCR\tessdata",
            r"C:\Program Files (x86)\Tesseract-OCR\tessdata"
        ]

        for path in system_paths:
            if os.path.exists(path):
                return path

        # 如果全部失败
        raise TesseractNotFoundError("Tessdata未找到。请确保已安装Tesseract-OCR语言数据。")

    @staticmethod
    def get_tesseract_version(tesseract_path: str) -> str:
        """
        获取Tesseract版本

        诊断用途：该方法会真实启动一次 `tesseract --version` 子进程，
        因此不得放在 GUI 线程或 OCREngine 的构造路径上调用。

        参数：
            tesseract_path: tesseract.exe的路径

        返回：
            版本字符串
        """
        import subprocess
        try:
            result = subprocess.run(
                [tesseract_path, '--version'],
                capture_output=True,
                text=True,
                check=True
            )
            # 提取版本号，例如 "tesseract 5.3.0"
            version_line = result.stdout.split('\n')[0]
            return version_line.split()[1]
        except Exception:
            return "unknown"


class OCREngineError(Exception):
    """OCR 引擎异常"""
    pass


class OCREngine:
    """
    Tesseract OCR 引擎封装

    专为游戏 HUD 场景优化：
    - 固定字体识别
    - 英文 + 数字 + 标点
    - PSM 7 (单行) 优先，PSM 6 (多行) 回退
    """

    # 游戏场景字符白名单 - 优化英文识别
    DEFAULT_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    def __init__(self,
                 tesseract_cmd: Optional[str] = None,
                 whitelist: str = DEFAULT_WHITELIST,
                 language: str = "eng",
                 psm_modes: List[int] = None):
        """
        初始化 OCR 引擎

        参数：
            tesseract_cmd: Tesseract 可执行文件路径（如需自定义）
            whitelist: 允许识别的字符白名单
            language: OCR 语言 (默认 'eng' - 英语)
            psm_modes: PSM 模式列表，按优先级尝试 (默认 [7, 6])
        """
        if not TESSERACT_AVAILABLE:
            raise OCREngineError(
                "pytesseract 未安装。请运行: pip install pytesseract\n"
                "同时确保 Tesseract-OCR 已安装并添加到系统 PATH。"
            )

        # 查找 Tesseract 路径
        try:
            if tesseract_cmd:
                # 使用自定义路径
                tesseract_path = tesseract_cmd
                if not os.path.exists(tesseract_path):
                    raise TesseractNotFoundError(f"自定义 Tesseract 路径不存在: {tesseract_path}")
            else:
                # 使用 TesseractLocator 查找
                tesseract_path = TesseractLocator.find_tesseract()

            # 设置 Tesseract 路径
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            logger.info("[TESSERACT] executable=%s", tesseract_path)

            # 注意：刻意不做 `tesseract --version` 探测。
            # 它会真实启动一次子进程，打包环境下首次启动要冷加载上百 MB 的 DLL
            # （还可能被杀软扫描），在交互路径上是不可接受的阻塞。
            # 版本信息仅为诊断用途，需要时用 TesseractLocator.get_tesseract_version()
            # 单独、显式地获取，不放在 OCREngine 的构造路径上。

            # 查找 tessdata 路径
            tessdata_path = TesseractLocator.find_tessdata(tesseract_path)
            logger.info("[TESSDATA] path=%s", tessdata_path)

            # 设置 tessdata 目录
            pytesseract.pytesseract.tessdata_dir = tessdata_path

        except TesseractNotFoundError as e:
            raise OCREngineError(f"Tesseract OCR 未找到: {e}")

        # 配置参数
        self.whitelist = whitelist
        self.language = language
        self.psm_modes = psm_modes if psm_modes else [6, 7, 11]  # 多行优先，然后单行，最后稀疏文本

        logger.info(
            "[TESSERACT] language=%s, whitelist_chars=%d, psm_modes=%s",
            language, len(whitelist), self.psm_modes
        )

    def _build_config(self, psm: int) -> str:
        """
        构建 Tesseract 配置字符串

        参数：
            psm: 页面分割模式 (PSM)

        返回：
            Tesseract 配置字符串
        """
        # 基础配置
        config_parts = [
            f"--psm {psm}",
            f"-c tessedit_char_whitelist={self.whitelist}",
        ]

        # 性能优化配置
        config_parts.extend([
            "--oem 1",  # LSTM OCR 引擎
        ])

        return " ".join(config_parts)

    def _run_psm(self, image: np.ndarray, psm: int) -> str:
        """按单个 PSM 跑一次 Tesseract，返回去掉首尾空白的文本（可能是空串）。"""
        config = self._build_config(psm)

        # 执行 OCR
        text = pytesseract.image_to_string(
            image,
            lang=self.language,
            config=config
        )

        # 清理结果
        return text.strip()

    def iter_readings(self,
                      image: np.ndarray,
                      preprocess: bool = True):
        """
        逐个产出每个 PSM 的非空读法：(psm, 文本)。

        与 recognize() 的区别：
            recognize() 遇到第一个非空结果就返回，调用方只能看到一个读法。
            当排在前面的 PSM 读错、后面的 PSM 读对时（实测同一张卡片
            PSM 6 读 'Gom180'、PSM 11 读 'GGM180'），错误读法就成了唯一真相。
            多读法仲裁必须能看到后续读法，因此需要这个入口。

        做成生成器是为了让调用方【按需】取读法：
        某次 OCR 调用约 200ms，只在前面的读法可疑时才值得跑后面的 PSM。

        参数：
            image: 输入图像 (BGR 格式的 numpy.ndarray)
            preprocess: 与 recognize() 一致，保留该参数；实现同样不做预处理

        产出：
            (psm, 文本)，按 self.psm_modes 的顺序，只产出非空结果

        异常：
            OCREngineError: 图像为空；或所有 PSM 都失败（与 recognize() 一致）
        """
        if image is None or image.size == 0:
            raise OCREngineError("输入图像为空")

        produced = 0
        last_error = None
        for psm in self.psm_modes:
            try:
                text = self._run_psm(image, psm)
            except Exception as e:
                last_error = e
                logger.debug("PSM=%d 识别失败: %s", psm, str(e))
                continue
            if text:
                logger.debug("OCR 读法 (PSM=%d): '%s'", psm, text)
                produced += 1
                yield (psm, text)

        if not produced and last_error is not None:
            raise OCREngineError(f"所有 PSM 模式识别失败: {last_error}")

    def recognize(self,
                  image: np.ndarray,
                  preprocess: bool = True,
                  return_details: bool = False) -> str | dict:
        """
        识别图像中的文本（返回【第一个非空的 PSM 读法】）

        需要看到后续读法时用 iter_readings()。

        参数：
            image: 输入图像 (BGR 格式的 numpy.ndarray)
            preprocess: 是否进行预处理 (默认 True)
            return_details: 是否返回详细结果 (默认 False)

        返回：
            if return_details=False: 识别的文本字符串
            if return_details=True: {
                "text": 识别文本,
                "confidence": 置信度,
                "psm_used": 使用的 PSM 模式,
                "details": 详细结果列表
            }

        异常：
            OCREngineError: OCR 识别失败
        """
        if image is None or image.size == 0:
            raise OCREngineError("输入图像为空")

        try:
            # 不进行预处理，直接使用原始图像
            # 原因：预处理会导致"I"被识别为"1"
            logger.debug("使用原始图像，不进行预处理")

            # 尝试不同的 PSM 模式
            last_error = None
            for psm in self.psm_modes:
                try:
                    text = self._run_psm(image, psm)

                    if text:  # 成功识别到文本
                        logger.debug("OCR 成功 (PSM=%d): '%s'", psm, text)

                        if return_details:
                            # 获取详细结果（诊断路径，用同一个 PSM 的配置）
                            details = pytesseract.image_to_data(
                                image,
                                lang=self.language,
                                config=self._build_config(psm),
                                output_type=pytesseract.Output.DICT
                            )

                            # 计算平均置信度
                            confidences = [int(c) for c in details['conf'] if c != '-1']
                            avg_confidence = sum(confidences) / len(confidences) if confidences else 0

                            return {
                                "text": text,
                                "confidence": avg_confidence,
                                "psm_used": psm,
                                "details": details
                            }

                        return text

                except Exception as e:
                    last_error = e
                    logger.debug("PSM=%d 识别失败: %s", psm, str(e))
                    continue

            # 所有 PSM 模式都失败
            if last_error:
                raise OCREngineError(f"所有 PSM 模式识别失败: {last_error}")

            return "" if not return_details else {"text": "", "confidence": 0, "psm_used": None, "details": None}

        except Exception as e:
            logger.error("OCR 识别异常: %s", e, exc_info=True)
            raise OCREngineError(f"OCR 识别失败: {e}")

    def recognize_with_roi(self,
                           image: np.ndarray,
                           roi: tuple,
                           **kwargs) -> str:
        """
        识别图像中指定 ROI 区域的文本

        参数：
            image: 输入图像
            roi: ROI 区域 (x, y, width, height)
            **kwargs: 传递给 recognize() 的其他参数

        返回：
            识别的文本
        """
        x, y, width, height = roi
        roi_image = image[y:y+height, x:x+width]
        return self.recognize(roi_image, **kwargs)

    def test_connection(self) -> bool:
        """
        测试 Tesseract 连接是否正常

        返回：
            True 如果连接正常，False 否则
        """
        try:
            # 创建一个简单的测试图像
            test_image = np.ones((50, 200), dtype=np.uint8) * 255
            cv2.putText(test_image, "Test", (50, 35),
                       cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)

            # 尝试识别
            config = self._build_config(7)
            result = pytesseract.image_to_string(
                test_image,
                lang=self.language,
                config=config
            )

            logger.info("Tesseract 连接测试成功，识别结果: '%s'", result.strip())
            return True

        except Exception as e:
            logger.error("Tesseract 连接测试失败: %s", e)
            return False

    def _post_process_ocr_text(self, text: str) -> str:
        """
        后处理OCR文本，修复常见错误

        参数：
            text: OCR识别的原始文本

        返回：
            处理后的文本
        """
        if not text:
            return text

        # 修复常见OCR混淆 - 仅在明确的情况下转换
        # 1. '1' 开头的可能是 'I' (特别是英文句子开头)
        if text.startswith('1') and len(text) > 1:
            # 检查剩余部分是否包含字母，如果是，很可能第一个是'I'
            has_letters = any(c.isalpha() for c in text[1:])
            if has_letters:
                text = 'I' + text[1:]

        # 2. 仅在上下文明确的情况下替换
        # 只替换单独的'1'，不替换数字中的'1'
        parts = []
        for i, char in enumerate(text):
            if char == '1':
                # 检查前后字符，如果是数字的一部分则保留，否则替换为I
                if (i > 0 and text[i-1].isdigit()) or (i < len(text)-1 and text[i+1].isdigit()):
                    parts.append(char)
                else:
                    # 如果是单独的'1'，且文本看起来像英文，替换为'I'
                    if any(c.isalpha() for c in text):
                        parts.append('I')
                    else:
                        parts.append(char)
            else:
                parts.append(char)
        text = ''.join(parts)

        # 3. 其他常见但谨慎的替换
        # 只替换某些特定模式
        text = text.replace('|', 'I')  # 竖线几乎总是I

        # 4. 合并重复的字母（但保留一些重复的模式）
        import re
        # 只合并3个以上的重复字母
        text = re.compile(r'(.)\1{2,}').sub(r'\1\1', text)

        return text
