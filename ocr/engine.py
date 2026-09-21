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
import shutil
import sys
from typing import Optional, List

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.engine")

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None
    TESSERACT_AVAILABLE = False

__all__ = [
    "OCREngine",
    "OCREngineError",
    "TesseractLocator",
    "TesseractNotFoundError",
    "is_frozen",
]

# ---------------------------------------------------------------------------
# 随包 OCR 运行时定位 — 发布打包修复
#
# 背景：
#   安装包必须自带 OCR 运行时。最终用户的机器上没有 Python、没有 pytesseract、
#   没有 Tesseract-OCR，也不会去设置 PATH；因此 tesseract.exe / 其 DLL /
#   tessdata 全部随包分发，并由本模块__唯一__负责定位。
#
# 事故成因（两条独立的缺陷，都只在安装包上暴露）：
#   1. pytesseract 未列入 requirements.txt，构建环境里根本没有它。spec 的
#      hiddenimports 写着 "pytesseract"，但 PyInstaller 对找不到的 hidden import
#      只警告不报错，于是包里没有 pytesseract。冻结后 `import pytesseract` 失败，
#      TESSERACT_AVAILABLE=False，OCR 一次都没跑过。
#   2. find_tesseract() 用 `dirname(__file__)`（即 ocr/ 目录）去拼 runtime/，
#      源码树下永远找不到仓库根的 runtime/tesseract/，于是静默落到
#      `C:\Program Files\Tesseract-OCR` 和 PATH —— 开发机上"碰巧"能跑，
#      客户机上必然失败。
#
# 冻结环境刻意不做 PATH 回退：客户机上并没有 Tesseract，静默回退只会把明确的
# 「随包文件缺失」变成含糊的「找不到命令」，掩盖打包缺陷。
# ---------------------------------------------------------------------------

#: runtime/ 下的子目录名，与 douyin_viewer.spec 的 _TESSERACT_DEST 必须一致。
_TESSERACT_SUBDIR = "tesseract"

#: 部署覆盖用的环境变量名，与 DLV_FFMPEG_PATH / SUSI_HELPER_PATH 同一模式。
_ENV_TESSERACT = "DLV_TESSERACT_PATH"
_ENV_TESSDATA = "DLV_TESSDATA_PATH"

#: 面向安装用户的提示。不含 pip / PATH / 手动安装 Tesseract ——
#: 那些说法只对源码开发环境成立，出现在安装包上就是无效指引。
FROZEN_OCR_MISSING_MESSAGE = "内置 OCR 组件缺失或损坏，请重新安装 LiveLens。"


def is_frozen() -> bool:
    """当前是否为 PyInstaller 冻结环境（含 Velopack 安装后的 onedir）。"""
    return bool(getattr(sys, "frozen", False))


def ocr_missing_message(dev_detail: str) -> str:
    """把一句开发环境诊断转换成当前环境下真正可执行的提示。

    ``dev_detail`` 只说给能改代码的人听（pip install / PATH / 手动装 Tesseract）。
    冻结环境里这些都不成立，只返回重装安装包的指引。
    """
    if is_frozen():
        return FROZEN_OCR_MISSING_MESSAGE
    return dev_detail


if not TESSERACT_AVAILABLE:
    # 安装包里出现这句就代表打包缺陷（pytesseract 没被带上），
    # 因此文案按环境分流：最终用户看到的是重装指引，不是 pip 命令。
    logger.warning(
        "pytesseract 不可用，OCR 功能不可用: %s",
        ocr_missing_message("请运行: pip install pytesseract"),
    )


def _exe_name(name: str) -> str:
    """Windows 下补 .exe 后缀，其余平台沿用裸名。"""
    return name + ".exe" if os.name == "nt" else name


def _project_root() -> str:
    """源码树根目录（本文件位于 <root>/ocr/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_usable(path: str) -> bool:
    """存在且非空。0 字节的占位文件不能算"找到"。"""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _frozen_base_dirs():
    """冻结构建中可能承载 runtime/ 的目录，按优先级排列。

    ``sys._MEIPASS`` 在 PyInstaller onedir 下指向 ``<exe_dir>/_internal``，
    也就是 COLLECT 实际落盘的位置；``<exe_dir>`` 本身是 Velopack 更新后
    ``current/`` 的形态。两处都查，避免把布局固定死在某一个上。

    返回空列表表示当前不是冻结环境，调用方应改走源码树路径。
    """
    if not is_frozen():
        return []

    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if exe_dir not in dirs:
        dirs.append(exe_dir)

    return dirs


def _bundled_dirs():
    """随包 runtime/ 的候选父目录：冻结环境用冻结目录，源码环境用仓库根。"""
    return _frozen_base_dirs() or [_project_root()]


class TesseractNotFoundError(Exception):
    """Tesseract OCR 未找到异常"""
    pass


class TesseractLocator:
    """
    Tesseract 运行时路径解析器（随包 OCR 的唯一定位边界）

    负责把「随包 runtime/tesseract/」解析成 tesseract.exe 与 tessdata 的绝对路径。
    布局与 douyin_viewer.spec 的 _TESSERACT_DEST 一致：

        <base>/runtime/tesseract/tesseract.exe
        <base>/runtime/tesseract/*.dll
        <base>/runtime/tesseract/tessdata/eng.traineddata

    其中 <base> 在冻结环境是 _MEIPASS / exe 目录，在源码环境是仓库根。

    查找顺序（与 utils.runtime_paths 的 FFmpeg 解析保持同一套语义）：
        1. 环境变量覆盖（部署用，便于同一份构建适配不同部署）
        2. 随包目录：冻结环境 <MEIPASS>/runtime/... 或 <exe_dir>/runtime/...
           源码环境 <项目根>/runtime/...
        3. 仅非冻结的开发环境，回退到 PATH

    冻结环境不做 PATH 回退，也不查 C:\\Program Files —— 客户机上没有这些，
    查了只会把「随包文件缺失」掩盖成「找不到命令」。
    """

    @staticmethod
    def _bundled_candidates() -> List[str]:
        """随包 tesseract.exe 的候选路径，按优先级排列。"""
        return [
            os.path.join(base, "runtime", _TESSERACT_SUBDIR, _exe_name("tesseract"))
            for base in _bundled_dirs()
        ]

    @staticmethod
    def find_tesseract(test_mode: bool = False) -> str:
        """
        查找tesseract.exe路径

        参数：
            test_mode: 测试模式。只查随包目录，连开发环境的 PATH 回退也禁用，
                       便于测试断言"不依赖系统 PATH"。

        返回：
            tesseract.exe的完整路径

        异常：
            TesseractNotFoundError: 如果未找到tesseract
        """
        override = os.environ.get(_ENV_TESSERACT, "").strip()
        if override:
            if _is_usable(override):
                return override
            raise TesseractNotFoundError(
                ocr_missing_message(
                    f"DLV_TESSERACT_PATH 指向的文件不存在或为空: {override}"
                )
            )

        for candidate in TesseractLocator._bundled_candidates():
            if _is_usable(candidate):
                return candidate

        if is_frozen():
            # 走到这里说明随包文件缺失：这是发布包的缺陷，不是用户环境问题。
            raise TesseractNotFoundError(FROZEN_OCR_MISSING_MESSAGE)

        if test_mode:
            raise TesseractNotFoundError(
                "测试模式下未找到随包 Tesseract（不回退系统 PATH）"
            )

        # 仅源码开发环境：允许 PATH 上的 Tesseract 安装。
        for name in ("tesseract", "tesseract.exe"):
            found = shutil.which(name)
            if found:
                return found

        raise TesseractNotFoundError(
            "Tesseract OCR 未找到。请确保已安装 Tesseract-OCR 并添加到系统 PATH，"
            "或提供 runtime/tesseract 目录。"
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
        candidates = []

        override = os.environ.get(_ENV_TESSDATA, "").strip()
        if override:
            candidates.append(override)

        # tesseract.exe 同级目录 —— 随包布局即如此，源码 / 冻结 / 系统安装都适用，
        # 因此这是唯一的主规则，不需要在任何地方重复写死路径。
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(tesseract_path)), "tessdata")
        )

        # 随包目录：源码树下 tesseract.exe 可能来自 PATH，但语言数据仍应取随包的那份。
        candidates.extend(
            os.path.join(base, "runtime", _TESSERACT_SUBDIR, "tessdata")
            for base in _bundled_dirs()
        )

        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate

        raise TesseractNotFoundError(
            ocr_missing_message(
                "Tessdata 未找到。请确保已安装 Tesseract-OCR 语言数据，"
                "或提供 runtime/tesseract/tessdata 目录。"
            )
        )

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
                ocr_missing_message(
                    "pytesseract 未安装。请运行: pip install pytesseract\n"
                    "同时确保 Tesseract-OCR 已安装并添加到系统 PATH。"
                )
            )

        # 查找 Tesseract 路径
        try:
            if tesseract_cmd:
                # 使用自定义路径
                tesseract_path = tesseract_cmd
                if not _is_usable(tesseract_path):
                    raise TesseractNotFoundError(
                        ocr_missing_message(f"自定义 Tesseract 路径不存在: {tesseract_path}")
                    )
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

            # 查找 tessdata 路径。
            # 这里的作用已经不止"记个路径"：find_tessdata() 缺失时会抛
            # TesseractNotFoundError，把"exe 在但语言数据没打进去"这种半残包
            # 在引擎构造时就变成本模块的缺失提示，而不是等到第一帧识别才报
            # 一句含糊的 tesseract 错误。
            tessdata_path = TesseractLocator.find_tessdata(tesseract_path)
            logger.info("[TESSDATA] path=%s", tessdata_path)

            # pytesseract 0.3.13 并不读这个属性（源码里没有 tessdata_dir），
            # 因此它不承担定位职责 —— tesseract 自己按 exe 同级目录找 tessdata，
            # 而随包布局正是 <...>/runtime/tesseract/tessdata。保留赋值只为兼容
            # 既有测试对 pytesseract 模块属性的建模，不要误以为它生效。
            pytesseract.pytesseract.tessdata_dir = tessdata_path

        except TesseractNotFoundError as e:
            # 冻结环境里 e 已经是面向最终用户的成品文案，不再加开发向的前缀。
            if is_frozen():
                raise OCREngineError(str(e))
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
