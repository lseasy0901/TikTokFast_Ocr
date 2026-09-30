# -*- coding: utf-8 -*-
"""ONNX (DirectML) OCR 后端验收测试。

覆盖：
    [A] 三方法鸭子契约、异常层级、输入校验、线程亲和、shutdown 幂等
    [B] 静态约束：worker 的引擎构造点、惰性导入、不读 Paddle 侧文件
    [C] session 生命周期：一 ROI 一 shape、ROI 变化重建、钉宽不变量
    [D] 引擎选择：DLV_OCR_ENGINE 的取值语义（默认 ppocr / onnx / 未知硬报错）
    [E] 资源定位：模型与 charset 的解析顺序与硬失败

运行（需要能 import onnxruntime 的解释器）：
    # 本机：ONNX Runtime 隔离在实验目录
    set PYTHONPATH=D:\\TIikTok_Ocr\\_onnx_export_lab\\deps_dml
    python test_onnx_engine.py

退出码：0 = 全部通过；77 = 缺运行时/模型，跳过（**不会伪装成通过**）；1 = 有失败。
"""

import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_FAILURES = []

#: 真实 ROI 数据集（与 test_ppocrv6_backend.py 同一约定：机器上的绝对路径）。
#: 缺失时只跳过「识别出已知文本」这一条，结构性断言照常跑。
ROI_DIR = r"C:\Users\ZhuanZ（无密码）\Documents\LiveLens-OCR-Dataset-20260927-043111\images"


def check(ok, what, detail=""):
    mark = "PASS" if ok else "FAIL"
    line = "  [%s] %s" % (mark, what)
    if detail:
        line += "  (%s)" % detail
    print(line)
    if not ok:
        _FAILURES.append(what)
    return ok


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def skip(reason):
    print("\n" + "=" * 68)
    print("SKIPPED: %s" % reason)
    print("=" * 68)
    return 77


def _code_only(path):
    """源码去掉 docstring 与 # 注释后的文本 —— 只用于「可执行路径」静态断言。

    直接搜原文会把注释里的说明文字也算进来（例如文档里写「不读 inference.yml」），
    那种误报会让人以为检查在保护什么。
    """
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.add(d)
    for d in sorted(docs, key=len, reverse=True):
        src = src.replace('"""' + d + '"""', "").replace("'''" + d + "'''", "")
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


def _onnx_available():
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _roi_image():
    """一张真实 ROI（uint8 BGR）；数据集缺失时返回 None。"""
    if not os.path.isdir(ROI_DIR):
        return None
    import glob
    files = sorted(glob.glob(os.path.join(ROI_DIR, "*.png")))
    if not files:
        return None
    from ocr.onnx_preprocess import read_image_bgr
    return read_image_bgr(files[0])


def _synth(h=230, w=871):
    """数据集缺失时的替代品：结构断言不关心内容。"""
    import numpy as np
    return np.zeros((h, w, 3), dtype=np.uint8)


def _roi_for(image):
    from ocr.roi_manager import ROI
    h, w = image.shape[:2]
    return ROI(0, 0, w, h, w, h)


# ======================================================================
# A：契约
# ======================================================================
def test_a_contract(engine, image):
    section("A. 三方法契约 / 异常层级 / 输入校验 / 线程亲和")
    from ocr.engine import OCREngineError
    from ocr.onnx_engine import (
        ONNXEngineError, ONNXModelNotFoundError, READING_LABEL,
    )

    check(issubclass(ONNXEngineError, OCREngineError),
          "ONNXEngineError 是 OCREngineError 的子类"
          "（worker 既有错误/拆除路径无需改动即可接管）")
    check(issubclass(ONNXModelNotFoundError, ONNXEngineError),
          "ONNXModelNotFoundError 是 ONNXEngineError 的子类")
    check(READING_LABEL == "onnx",
          "READING_LABEL 独立于 ppocr（避免诊断列把后端记错）",
          "label=%r" % READING_LABEL)
    for name in ("iter_readings", "recognize", "shutdown"):
        check(callable(getattr(engine, name, None)),
              "实现契约方法 %s()" % name)

    check(engine.init_ms is not None and engine.init_ms > 0,
          "构造期完成 ROI 无关的加载", "init=%.0fms" % (engine.init_ms,))
    check(engine._session is None,
          "构造期**不**建 session（shape 要等 ROI）")

    # 未建 session 就推理：必须是 ONNXEngineError，不是 AttributeError
    # （AttributeError 会绕过 worker 的 except OCREngineError，每 300ms 刷一次错误）
    try:
        engine.recognize(image)
        check(False, "未建 session 时 recognize() 必须显式失败")
    except ONNXEngineError as e:
        check("会话尚未创建" in str(e),
              "未建 session 时抛 ONNXEngineError（不是 AttributeError）", str(e)[:40])
    except AttributeError as e:
        check(False, "抛的是 AttributeError —— 会绕过 worker 的错误路径", str(e))

    # 非法输入必须在**推理前**被拒
    import numpy as np
    engine.ensure_session_for_roi(_roi_for(image), image)   # 先建好会话
    cases = [
        ("None", None),
        ("空数组", np.zeros((0, 0, 3), dtype=np.uint8)),
        ("2-D 灰度", np.zeros((48, 320), dtype=np.uint8)),
        ("1 通道", np.zeros((48, 320, 1), dtype=np.uint8)),
        ("4 通道", np.zeros((48, 320, 4), dtype=np.uint8)),
        ("float32", np.zeros((48, 320, 3), dtype=np.float32)),
    ]
    for label, bad in cases:
        try:
            engine.recognize(bad)
            check(False, "非法输入 %s 必须被拒绝" % label)
        except ONNXEngineError as e:
            check(True, "非法输入 %s 被拒" % label, str(e)[:34])

    # 调用方图像不得被修改（sample capture 依赖这一点）
    before = image.copy()
    try:
        engine.recognize(image)
    except Exception:  # noqa: BLE001
        pass
    check(np.array_equal(before, image),
          "推理不修改调用方的 ROI 图像（sample capture 的前提）")

    # 线程亲和
    err = []

    def _other():
        try:
            engine.recognize(image)
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_other)
    t.start()
    t.join(30)
    check(len(err) == 1 and isinstance(err[0], ONNXEngineError),
          "跨线程调用被拒（属主线程约束）",
          type(err[0]).__name__ if err else "no error")

    # shutdown 幂等 + 释放后不可用
    engine.shutdown()
    engine.shutdown()
    check(True, "shutdown() 幂等（连续两次无异常）")
    try:
        engine.recognize(image)
        check(False, "释放后继续使用必须失败")
    except ONNXEngineError:
        check(True, "释放后继续使用抛 ONNXEngineError")


# ======================================================================
# B：静态约束
# ======================================================================
def test_b_static():
    section("B. 静态约束（源码扫描，不执行）")
    root = os.path.dirname(os.path.abspath(__file__))
    worker = open(os.path.join(root, "ocr", "worker.py"), encoding="utf-8").read()

    body = worker.split("def _ensure_engine", 1)[1].split(
        "def _ensure_recognizer", 1)[0]
    check("PPOCREngine()" in body,
          "worker 的引擎构造点仍保留字面量 PPOCREngine()（默认路径不变）")
    check(not re.search(r"\bOCREngine\s*\(", body),
          "worker 的引擎构造点不构造 Tesseract 引擎")
    check("except" not in body,
          "worker 的引擎构造点没有任何 except（不存在静默回退）")

    for fn in ("worker.py", "onnx_engine.py", "onnx_preprocess.py"):
        src = open(os.path.join(root, "ocr", fn), encoding="utf-8").read()
        top = [ln for ln in src.splitlines()
               if re.match(r"^(import|from)\s+onnxruntime", ln)]
        check(not top,
              "ocr/%s 不在模块顶层导入 onnxruntime（生产 venv 没有它）" % fn)

    for fn in ("onnx_engine.py", "onnx_preprocess.py"):
        code = _code_only(os.path.join(root, "ocr", fn))
        check("runtime/ppocr" not in code,
              "ocr/%s 的可执行路径不引用 runtime/ppocr" % fn)
        check("inference.yml" not in code,
              "ocr/%s 的运行时不读 Paddle 侧 inference.yml" % fn)

    # session 状态必须住在引擎上，不能放到 recognizer
    check("ensure_session_for_roi" not in
          open(os.path.join(root, "ocr", "room_code.py"), encoding="utf-8").read(),
          "RoomCodeRecognizer 完全不感知 ONNX session（未被改动）")

    try:
        import ocr.onnx_engine  # noqa: F401
        check(True, "生产 venv（无 onnxruntime）下 import ocr.onnx_engine 成功")
    except Exception as e:  # noqa: BLE001
        check(False, "import ocr.onnx_engine 失败", str(e)[:60])


# ======================================================================
# C：session 生命周期
# ======================================================================
def test_c_session(engine, image):
    section("C. session 生命周期：一 ROI 一 shape / ROI 变化重建")
    import numpy as np
    import cv2

    roi = _roi_for(image)
    engine.ensure_session_for_roi(roi, image)
    st = engine.backend_status
    check(st["pinned_width"] == 320,
          "真实 ROI（宽高比 3.79）钉到宽度 320", "width=%s" % st["pinned_width"])
    check(st["provider_actual"] == "DmlExecutionProvider",
          "实际 provider 是 DirectML", st["provider_actual"])
    check(len(st["shapes_seen"]) == 1,
          "warmup 后当前 session 只见过 1 个 shape", str(st["shapes_seen"]))

    gen1 = engine.session_generation

    # 同一个 ROI 反复调用：必须廉价 no-op，不重建
    for _ in range(3):
        engine.ensure_session_for_roi(roi, image)
    check(engine.session_generation == gen1,
          "同一 ROI 重复 ensure 不重建 session")

    # 识别器会做等比 escalation（1.5x / 2.0x）—— 绝不能让第二个 shape 进 session
    h, w = image.shape[:2]
    for s in (1.5, 2.0):
        big = cv2.resize(image, (int(round(w * s)), int(round(h * s))),
                         interpolation=cv2.INTER_CUBIC)
        try:
            engine.recognize(big)
        except Exception:  # noqa: BLE001
            pass
    st = engine.backend_status
    check(len(st["shapes_seen"]) == 1,
          "1.5x/2.0x escalation 后当前 session 仍然只有 1 个 shape（钉宽护栏生效）",
          str(st["shapes_seen"]))
    check(engine.session_generation == gen1, "escalation 不触发 session 重建")

    # ROI 真正变化 -> 重建，且新 session 同样只有一个 shape
    wide = np.zeros((40, 600, 3), dtype=np.uint8)     # 宽高比 15 -> 宽度 720
    engine.ensure_session_for_roi(_roi_for(wide), wide)
    st = engine.backend_status
    check(engine.session_generation == gen1 + 1,
          "ROI 变化 -> session 重建", "gen %d -> %d" % (gen1, engine.session_generation))
    check(st["pinned_width"] == 720,
          "新 ROI 的钉宽随之变为 720", "width=%s" % st["pinned_width"])
    check(len(st["shapes_seen"]) == 1,
          "重建后的新 session 仍然只有 1 个 shape", str(st["shapes_seen"]))
    check(len(st["shapes_seen_total"]) >= 2,
          "累计记录保留了跨 session 的 shape（诊断留痕）",
          str(st["shapes_seen_total"]))


# ======================================================================
# D：引擎选择
# ======================================================================
def test_d_selection():
    section("D. DLV_OCR_ENGINE 取值语义")
    from ocr.engine import OCREngineError
    import ocr.worker as w

    saved = os.environ.get("DLV_OCR_ENGINE")
    try:
        # 默认现在是 onnx：旧的 NVIDIA-only Paddle 模型资源已删除，
        # ppocr 只在**显式**指定时才使用（兼容分支保留，但不再是默认）。
        for value, expect in ((None, "onnx"), ("", "onnx"), ("ppocr", "ppocr"),
                              ("PPOCR", "ppocr"), (" onnx ", "onnx")):
            if value is None:
                os.environ.pop("DLV_OCR_ENGINE", None)
            else:
                os.environ["DLV_OCR_ENGINE"] = value
            got = w._selected_engine_kind()
            check(got == expect, "DLV_OCR_ENGINE=%r -> %r" % (value, expect), got)

        for value in ("bogus", "paddle", "tesseract"):
            os.environ["DLV_OCR_ENGINE"] = value
            try:
                w._selected_engine_kind()
                check(False, "未知取值 %r 必须硬报错（不静默回落）" % value)
            except OCREngineError:
                check(True, "未知取值 %r 硬报错" % value)
    finally:
        if saved is None:
            os.environ.pop("DLV_OCR_ENGINE", None)
        else:
            os.environ["DLV_OCR_ENGINE"] = saved


# ======================================================================
# E：资源定位
# ======================================================================
def test_e_locator():
    section("E. 模型 / charset 定位")
    from ocr.onnx_engine import ONNXModelLocator, ONNXModelNotFoundError
    from ocr.onnx_preprocess import (
        CHARSET_ENTRIES, CHARSET_FINAL_SIZE, load_charset,
    )

    model = ONNXModelLocator.find_model_path()
    charset = ONNXModelLocator.find_charset_path()
    check(os.path.isfile(model), "模型解析到真实文件", model[-40:])
    check(os.path.isfile(charset), "charset 解析到真实文件", charset[-30:])
    check(os.path.basename(charset) == "charset.txt",
          "charset 文件名是 charset.txt（不是 inference.yml）")

    runtime_onnx = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "runtime", "onnx")
    check(os.path.normcase(os.path.dirname(model)) == os.path.normcase(runtime_onnx),
          "解析到**随包目录** runtime/onnx/（不是开发兜底路径）")

    entries = load_charset(charset)
    check(len(entries) == CHARSET_ENTRIES,
          "charset 项数与运行时常量一致",
          "%d vs %d" % (len(entries), CHARSET_ENTRIES))
    check(len(entries) + 2 == CHARSET_FINAL_SIZE,
          "最终字符表长度一致（blank + dict + space）", str(CHARSET_FINAL_SIZE))

    check(os.path.isfile(os.path.join(runtime_onnx, "charset.txt"))
          and not os.path.isfile(os.path.join(runtime_onnx, "inference.yml")),
          "runtime/onnx/ 里没有 inference.yml（只投 .onnx + charset.txt）")

    saved = os.environ.get("DLV_ONNX_MODEL_PATH")
    try:
        os.environ["DLV_ONNX_MODEL_PATH"] = r"D:\__no_such_onnx__.onnx"
        try:
            ONNXModelLocator.find_model_path()
            check(False, "环境变量指向坏路径时必须硬失败")
        except ONNXModelNotFoundError:
            check(True, "环境变量指向坏路径时硬失败、不回落")
    finally:
        if saved is None:
            os.environ.pop("DLV_ONNX_MODEL_PATH", None)
        else:
            os.environ["DLV_ONNX_MODEL_PATH"] = saved


# ======================================================================
# F：端到端识别（需要真实数据集）
# ======================================================================
def test_f_recognize(engine, image, real):
    section("F. 端到端识别")
    if not real:
        print("  (数据集缺失，跳过文本断言；结构断言已在 A/C 段完成)")
        return
    from ocr.onnx_engine import READING_LABEL
    engine.ensure_session_for_roi(_roi_for(image), image)

    readings = list(engine.iter_readings(image))
    check(len(readings) <= 1, "每帧最多一条读法（本引擎是单读法后端）")
    if readings:
        label, text = readings[0]
        check(label == READING_LABEL, "读法标签是 %r" % READING_LABEL, label)
        check(bool(text), "读出的文本非空", "text=%r" % text)
    else:
        print("  (本帧为空读法：合法结果，不产出任何读法)")

    details = engine.recognize(image, return_details=True)
    check(set(details) == {"text", "confidence", "psm_used", "details"},
          "return_details=True 的键与其它引擎一致", str(sorted(details)))
    check(details["psm_used"] is None,
          "psm_used 如实为 None（ONNX 无 PSM 概念，不编造编号）")


def main() -> int:
    print("=" * 68)
    print("ONNX (DirectML) OCR backend acceptance test")
    print("=" * 68)

    test_b_static()
    test_d_selection()
    test_e_locator()

    if not _onnx_available():
        return skip("当前解释器没有 onnxruntime。请用隔离环境运行，例如：\n"
                    "  set PYTHONPATH=D:\\TIikTok_Ocr\\_onnx_export_lab\\deps_dml")

    from ocr.onnx_engine import ONNXEngine, ONNXModelNotFoundError
    try:
        engine = ONNXEngine()
    except ONNXModelNotFoundError as e:
        return skip("ONNX 产物不可用：%s\n"
                    "请先供应 runtime/onnx/（onnx + charset.txt），"
                    "charset 由 scripts/export_onnx_charset.py 生成。" % e)

    real_image = _roi_image()
    image = real_image if real_image is not None else _synth()
    print("\n输入图像: %s (%dx%d)" % (
        "真实 ROI" if real_image is not None else "合成占位图",
        image.shape[1], image.shape[0]))

    try:
        test_a_contract(engine, image)     # 结束时 shutdown 掉这个实例
        fresh = ONNXEngine()
        try:
            test_c_session(fresh, image)
            test_f_recognize(fresh, image, real_image is not None)
        finally:
            fresh.shutdown()
    finally:
        try:
            engine.shutdown()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "=" * 68)
    if _FAILURES:
        print("失败 %d 项：" % len(_FAILURES))
        for item in _FAILURES:
            print("  - %s" % item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
