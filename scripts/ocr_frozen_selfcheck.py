# -*- coding: utf-8 -*-
"""
冻结环境 OCR 自检入口（仅供构建期验证，不随正式包发布）

目的：
    证明"安装包里的 OCR 能跑"，而不只是"源码环境里能跑"。

    生产后端是 PP-OCRv6_medium_rec + ONNX Runtime（DirectML，CPU 回退），
    所以自检必须验证随包**产物**能被定位、ONNX Runtime 能被导入、provider
    能被选中、session 能被建立、并且真的能产出正确文本。
    原先验证的是随包 Tesseract 运行时（已删），后来是随包 Paddle 模型
    （runtime/ppocr/，已随 ONNX 迁移删除）。

为什么断言**具体文本**而不是"非空"：
    ONNX 后端的字符表来自随包的 charset.txt。它的项数或顺序错了**不会抛异常**，
    只会把每个字符索引静默解错 —— 输出仍然是"一段非空文本"。只断言非空的话，
    这类缺陷会一路通过自检、通过打包，直到用户看见乱码。
    合成图上的 "AB1234" 由本机 ONNX 后端实测逐字识别正确（见下方 expected），
    因此把期望值写死，正好挡住字符表错位这一类静默失败。

用法：
    本文件由 scripts/ocr_frozen_selfcheck.spec 驱动 —— 那个 spec 通过替换
    douyin_viewer.spec 的 Analysis 入口脚本来构建，datas / binaries /
    hiddenimports 与正式包**完全一致**。因此这里得到的结果就是正式包的结果。

    结果写到 DLV_OCR_SELFCHECK_OUT 指向的文件（EXE 是 console=False，
    不能靠 stdout 回传）。退出码非 0 表示自检失败。
"""

import json
import os
import sys
import traceback

#: 合成自检图里的文本，以及后端应当逐字读出的结果。
SELF_CHECK_TEXT = "AB1234"


def main() -> int:
    result = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": getattr(sys, "_MEIPASS", None),
        "executable": sys.executable,
        "python": sys.version,
    }

    # 旧 Tesseract 路径仍在源码里（诊断 / DLV_OCR_BACKEND=legacy 对照），
    # 但随包资产已经删除，因此这里只**记录**它的可用性，不参与成败判定。
    try:
        from ocr.engine import TESSERACT_AVAILABLE
        result["legacy_tesseract_available"] = TESSERACT_AVAILABLE
    except Exception as exc:  # noqa: BLE001
        result["legacy_tesseract_available"] = "import failed: %s" % exc

    try:
        from ocr.onnx_engine import MODEL_NAME, ONNXEngine, ONNXModelLocator

        result["onnx_model_name"] = MODEL_NAME
        result["expected_model_candidates"] = (
            ONNXModelLocator._bundled_candidates("PP-OCRv6_medium_rec_fp32.onnx"))
        result["expected_charset_candidates"] = (
            ONNXModelLocator._bundled_candidates("charset.txt"))

        # 1) 产物定位：模型与字符表都必须解析到**随包目录**（冻结构建里
        #    dev 兜底是不可达的，所以解析成功即说明随包投放正确）。
        model_path = ONNXModelLocator.find_model_path()
        charset_path = ONNXModelLocator.find_charset_path()
        result["resolved_model_path"] = model_path
        result["resolved_charset_path"] = charset_path
        result["model_usable"] = ONNXModelLocator.is_usable_model_file(model_path)
        result["charset_usable"] = ONNXModelLocator.is_usable_model_file(charset_path)

        import cv2
        import numpy as np

        # 2) 必须能被导入并选中 provider。ONNX Runtime 若没被 hook 收进包，
        #    这一步就以 ImportError 露出（而不是等到用户点识别）。
        import onnxruntime as ort
        result["onnxruntime_version"] = ort.__version__
        result["available_providers"] = list(ort.get_available_providers())

        # 3) 合成一张真实形态的 BGR 图。必须是 3 通道 —— 那是生产路径的输入
        #    契约（ROI 是原始帧的 BGR 切片），ONNXEngine 会校验并在 2-D 输入上
        #    抛错，用灰度图会让自检误报失败。
        image = np.full((80, 320, 3), 245, dtype=np.uint8)
        cv2.putText(image, SELF_CHECK_TEXT, (20, 55), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (10, 10, 10), 3)

        from ocr.roi_manager import ROI
        roi = ROI(0, 0, image.shape[1], image.shape[0],
                  image.shape[1], image.shape[0])

        def _run_once(tag):
            """构造 -> 建 session -> 识别 -> shutdown，返回 (text, engine)。"""
            engine = ONNXEngine()
            try:
                status = engine.backend_status
                result[tag + "_provider_requested"] = status["provider_requested"]
                result[tag + "_init_ms"] = engine.init_ms
                # session 必须显式建立：ONNX 的输入 shape 要等 ROI 才能定。
                engine.ensure_session_for_roi(roi, image)
                result[tag + "_provider_actual"] = engine.backend_status["provider_actual"]
                result[tag + "_pinned_width"] = engine.pinned_width
                result[tag + "_shapes_seen"] = engine.backend_status["shapes_seen"]
                text = engine.recognize(image).replace(" ", "")
                result[tag + "_text"] = text
                return text, engine
            except Exception:
                engine.shutdown()
                raise

        # 4) 真跑一次并 shutdown。
        text, engine = _run_once("ocr")
        try:
            result["ocr_ok"] = (text == SELF_CHECK_TEXT)
        finally:
            engine.shutdown()
        result["closed_after_shutdown"] = engine.backend_status["closed"]

        # 5) 释放后重新构造并再跑一次：证明可反复启停（worker 每次开始识别
        #    都是新引擎 + 新 session）。
        text2, engine2 = _run_once("restart")
        try:
            result["restart_ok"] = (text2 == SELF_CHECK_TEXT)
        finally:
            engine2.shutdown()

        # 6) 不变量：一个 session 里只能出现一个 shape。DirectML 上「后引入的
        #    shape」会永久慢 3–6 倍，所以这条必须在真实产物里成立。
        result["single_shape"] = (
            len(result.get("ocr_shapes_seen") or []) == 1
            and len(result.get("restart_shapes_seen") or []) == 1
        )

        # 7) provider 必须是两个受支持值之一（DirectML 主路径 / CPU 回退）。
        #    记录实际值，不把 CPU 回退判成失败 —— 但也不允许是别的。
        providers = {result.get("ocr_provider_actual"),
                     result.get("restart_provider_actual")}
        result["provider_supported"] = providers.issubset(
            {"DmlExecutionProvider", "CPUExecutionProvider"})

        result["ok"] = bool(
            result["model_usable"]
            and result["charset_usable"]
            and result["ocr_ok"]
            and result["closed_after_shutdown"]
            and result["restart_ok"]
            and result["single_shape"]
            and result["provider_supported"]
        )
    except Exception as exc:  # noqa: BLE001 - 自检要把任何失败形态都记下来
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()

    out_path = os.environ.get("DLV_OCR_SELFCHECK_OUT")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
