# -*- coding: utf-8 -*-
"""
冻结环境 OCR 自检入口（仅供构建期验证，不随正式包发布）

目的：
    证明"安装包里的 OCR 能跑"，而不只是"源码环境里能跑"。

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

    try:
        import pytesseract
        from ocr.engine import OCREngine, TESSERACT_AVAILABLE, TesseractLocator

        result["tesseract_available"] = TESSERACT_AVAILABLE
        result["pytesseract_module"] = getattr(pytesseract, "__file__", None)
        result["pytesseract_version"] = getattr(pytesseract, "__version__", None)

        result["resolved_tesseract"] = TesseractLocator.find_tesseract()
        result["resolved_tessdata"] = TesseractLocator.find_tessdata(
            result["resolved_tesseract"]
        )
        result["tesseract_exists"] = os.path.isfile(result["resolved_tesseract"])
        result["tessdata_exists"] = os.path.isdir(result["resolved_tessdata"])

        # 真跑一次识别：定位正确不等于能跑（缺 DLL、缺 traineddata 都要在这里露出来）
        import cv2
        import numpy as np

        image = np.full((80, 320), 255, dtype=np.uint8)
        cv2.putText(image, "AB1234", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)

        engine = OCREngine(psm_modes=[7])
        text = engine.recognize(image).replace(" ", "")
        result["ocr_text"] = text
        result["ocr_ok"] = "1234" in text
        result["ok"] = bool(result["ocr_ok"])
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
