# -*- coding: utf-8 -*-
"""
冻结环境 OCR 自检入口（仅供构建期验证，不随正式包发布）

目的：
    证明"安装包里的 OCR 能跑"，而不只是"源码环境里能跑"。

    生产后端是 PP-OCRv6_medium_rec，所以自检必须验证随包**模型**能被定位、
    能被加载、能产出文本。原先验证的是随包 Tesseract 运行时；那一套随
    runtime/tesseract/ 的删除一起失效了。

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
        from ocr.ppocr_engine import (
            MODEL_NAME,
            PPOCREngine,
            PPOCRModelLocator,
        )

        result["ppocr_model_name"] = MODEL_NAME
        result["expected_model_dir"] = PPOCRModelLocator._bundled_candidates()

        model_dir = PPOCRModelLocator.find_model_dir()
        result["resolved_model_dir"] = model_dir
        result["model_usable"] = PPOCRModelLocator.is_usable_model_dir(model_dir)

        # 真跑一次识别：定位正确不等于能跑（缺 DLL / 缺 CUDA / 权重损坏都要
        # 在这里露出来）。识别内容不做具体断言 —— 准确率由离线基准与真机验收
        # 负责，自检只回答"能不能跑出文本"。
        import cv2
        import numpy as np

        # 必须是 3 通道 BGR —— 那是生产路径的输入契约（ROI 是原始帧的 BGR 切片）。
        # 旧的自检用 2 通道灰度也能跑 Tesseract，但 PP-OCRv6 的 predict 会以
        # IndexError: tuple index out of range 拒绝 2-D 输入，自检会误报失败。
        image = np.full((80, 320, 3), 245, dtype=np.uint8)
        cv2.putText(image, "AB1234", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                    (10, 10, 10), 3)

        engine = PPOCREngine()
        try:
            result["init_ms"] = engine.init_ms
            result["backend_status"] = engine.backend_status
            text = engine.recognize(image).replace(" ", "")
            result["ocr_text"] = text
            result["ocr_ok"] = bool(text)
        finally:
            engine.shutdown()
        result["closed_after_shutdown"] = engine.backend_status["closed"]

        # 释放后重新构造：证明可反复启停（worker 每次开始识别都是新引擎）。
        engine2 = PPOCREngine()
        try:
            text2 = engine2.recognize(image).replace(" ", "")
            result["restart_text"] = text2
            result["restart_ok"] = bool(text2)
        finally:
            engine2.shutdown()

        result["ok"] = bool(
            result["model_usable"]
            and result["ocr_ok"]
            and result["closed_after_shutdown"]
            and result["restart_ok"]
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
