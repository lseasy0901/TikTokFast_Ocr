# -*- coding: utf-8 -*-
"""OCR 模型替换验收：PP-OCRv6 后端 + 生产识别链路

覆盖：
    [A] 后端契约 iter_readings / recognize / shutdown
    [B] GPU 约束与「无回退」硬约束
    [C] 真实 ROI 推理（含全量 89 张的准确率锚定）
    [D] worker 生命周期（工作线程内初始化、一会话一模型、停机释放、重启重建、
        GUI 不阻塞）
    [E] 真实链路 ROI -> OCRWorker -> PP-OCRv6 -> RoomCodeRecognizer
        -> TemporalResolver -> copy_requested -> 剪贴板

只验证**替换后的后端**：不重写既有 OCR 测试，也不改任何识别/调度代码。

运行环境
--------
需要带 paddle 运行时的解释器（本机：系统 Python 3.11，
paddlepaddle-gpu 3.3.0 / paddleocr 3.7.0 / paddlex 3.7.2）。
生产 venv 目前**不含** paddle，用 venv 跑本文件会明确报「跳过」并返回跳过码，
不会伪装成通过。

用法：
    python test_ppocrv6_backend.py
"""

import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 本用例集测的是 **PP-OCRv6(Paddle) 后端**：显式选中它。
# 默认后端已改为 onnx（旧的 Paddle 模型资源随 Step 2 验收删除），不显式选择的话
# worker 会走 ONNX 分支，本文件靠 monkeypatch worker_mod.PPOCREngine 观察到的
# 实例数会恒为 0（那不是回归，是用例选错了后端）。
os.environ["DLV_OCR_ENGINE"] = "ppocr"

import numpy as np
import cv2

FAILURES = []

ROI_DIR = r"C:\Users\ZhuanZ（无密码）\Desktop\无畏契约数据集\_ocr_v3_dataset\roi"

#: 与离线基准同一批 ROI；基准里 medium_rec 的结构化精确命中为 261/3 = 87/89。
EXPECTED_STRUCTURED_EXACT = 87
EXPECTED_SAMPLES = 89

#: 两张已知的失败样本（基准里 medium_rec 也过不去 / 给出同一个错码），
#: 用来锚定「替换后仍与基准逐字一致」。
KNOWN_CASES = {
    "9c908656e0d2cc6b.png": "",        # 基准里 medium 也是空（未成立）
    "ef2814bf0b135400.png": "EXO076",  # 基准里 medium 也读成 EXO076
}


def check(cond, msg, extra=""):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s %s" % (msg, extra))
        FAILURES.append(msg)
    return bool(cond)


def section(title):
    print("\n" + title)


def skip(reason):
    print("\n" + "=" * 68)
    print("SKIPPED: %s" % reason)
    print("=" * 68)
    return 77


def imread_unicode(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


#: 与离线基准**完全相同**的语料定义：只取 status=labeled、真值能解析成
#: ^[A-Z]{3}[0-9]{3}$、且 ROI 文件存在的样本。ROI 目录里的图片比标注多，
#: 不做这层过滤就会把未标注样本算进准确率（99 vs 89）。
_FORMAT_RE = re.compile(r"^[A-Z]{3}[0-9]{3}$")
_TRUTH_KEYS = ("text", "truth", "label", "code", "value")
_SAMPLES = None


def labeled_samples():
    """返回 [(文件名, 真值)]，与基准 load_dataset() 的筛选口径一致。"""
    global _SAMPLES
    if _SAMPLES is not None:
        return _SAMPLES

    ann_dir = os.path.join(os.path.dirname(ROI_DIR), "annotations")
    out = []
    for fn in sorted(os.listdir(ann_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            data = json.loads(
                open(os.path.join(ann_dir, fn), encoding="utf-8").read())
        except Exception:  # noqa: BLE001 - 坏标注直接跳过，与基准一致
            continue
        if data.get("status") != "labeled":
            continue
        truth = None
        for key in _TRUTH_KEYS:
            value = data.get(key)
            if isinstance(value, str) and _FORMAT_RE.match(value.strip().upper()):
                truth = value.strip().upper()
                break
        name = fn[:-5] + ".png"
        if truth and os.path.isfile(os.path.join(ROI_DIR, name)):
            out.append((name, truth))

    _SAMPLES = out
    return out


def roi_files():
    return [name for name, _ in labeled_samples()]


def _truth_of(name):
    for candidate, truth in labeled_samples():
        if candidate == name:
            return truth
    return None


def _paddle_available():
    try:
        import paddle  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ======================================================================
def test_a_contract(engine):
    section("A. 后端契约")

    check(os.path.isdir(engine.model_dir), "模型目录已定位", engine.model_dir)
    check(engine.init_ms is not None and engine.init_ms > 0,
          "记录了模型初始化耗时", repr(engine.init_ms))

    files = roi_files()
    img = imread_unicode(os.path.join(ROI_DIR, files[0]))

    from ocr.ppocr_engine import READING_LABEL

    readings = list(engine.iter_readings(img, preprocess=True))
    check(len(readings) == 1, "一帧只产出一个读法（单遍模型，不编造多读法）",
          repr(readings))
    label, text = readings[0]
    check(label == READING_LABEL,
          "标签是中性的 %r，不是伪造的 PSM 编号" % READING_LABEL, repr(label))
    check(bool(text), "真实 ROI 读出了非空文本", repr(text))

    check(engine.recognize(img, preprocess=True) == text,
          "recognize() 与 iter_readings() 的第一个读法一致")

    details = engine.recognize(img, preprocess=True, return_details=True)
    check(details["text"] == text and details["psm_used"] is None,
          "return_details 里 psm_used 如实为 None（PP-OCRv6 没有 PSM 概念）",
          repr(details))

    # 空输入必须显式报错，绝不能变成「空文本」
    from ocr.ppocr_engine import PPOCREngineError
    for bad, name in ((None, "None"), (np.zeros((0, 0, 3), np.uint8), "空数组")):
        try:
            list(engine.iter_readings(bad, preprocess=True))
            check(False, "空输入(%s)必须抛 PPOCREngineError" % name)
        except PPOCREngineError:
            check(True, "空输入(%s)抛 PPOCREngineError（不是空文本）" % name)

    # 输入契约：只接受生产链上的 uint8 BGR (H, W, 3)。
    # 非法形状必须在**进推理之前**被挡住并说清期望格式 —— PaddleX 对 2-D 灰度
    # 只会抛 IndexError: tuple index out of range，那与真正的推理失败无法区分。
    import cv2 as _cv2
    for bad, name, must_say in (
        (np.full((80, 320), 245, np.uint8), "2-D 灰度", ("(H, W, 3)", "uint8")),
        (np.full((80, 320, 1), 245, np.uint8), "单通道 3-D", ("(H, W, 3)",)),
        (np.full((80, 320, 4), 245, np.uint8), "4 通道 RGBA", ("(H, W, 3)",)),
        (np.full((80, 320, 3), 0.5, np.float32), "float32", ("uint8",)),
    ):
        # iter_readings 是生成器函数：必须真的迭代才会执行校验
        for call, label in (
            (lambda img: list(engine.iter_readings(img, preprocess=True)),
             "iter_readings"),
            (lambda img: engine.recognize(img, preprocess=True), "recognize"),
        ):
            try:
                call(bad)
                check(False, "%s(%s) 必须被拒绝" % (label, name))
            except PPOCREngineError as e:
                message = str(e)
                check(all(s in message for s in must_say),
                      "%s(%s) 报错并说明期望格式（%s）"
                      % (label, name, " / ".join(must_say)),
                      message[:100])
            except Exception as e:  # noqa: BLE001
                check(False, "%s(%s) 抛的是 %s，应是 PPOCREngineError"
                      % (label, name, type(e).__name__))

    # 不做自动转换：非法输入被拒后，合法输入仍原样可用
    good = np.full((80, 320, 3), 245, np.uint8)
    _cv2.putText(good, "AB1234", (20, 55), _cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                 (10, 10, 10), 3)
    check(bool(engine.recognize(good, preprocess=True)),
          "拒绝非法输入后，uint8 BGR 仍能正常识别")

    # 调用方的 ROI 数组不能被改写
    before = img.copy()
    list(engine.iter_readings(img, preprocess=True))
    check(np.array_equal(img, before), "输入 ROI 未被修改")

    # 跨线程使用必须显式拒绝（模型句柄不跨线程）
    box = {}

    def other_thread():
        try:
            list(engine.iter_readings(img, preprocess=True))
            box["result"] = "no-error"
        except Exception as e:  # noqa: BLE001
            box["result"] = type(e).__name__

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(30)
    check(box.get("result") == "PPOCREngineError",
          "非属主线程调用被显式拒绝", repr(box.get("result")))


def test_a2_shutdown():
    section("A2. shutdown 语义")

    from ocr.ppocr_engine import PPOCREngine, PPOCREngineError

    eng = PPOCREngine()
    files = roi_files()
    img = imread_unicode(os.path.join(ROI_DIR, files[0]))

    eng.shutdown()
    eng.shutdown()  # 幂等
    check(True, "shutdown() 可重复调用（幂等）")
    check(eng.backend_status["closed"] is True, "状态标记为已释放")

    try:
        list(eng.iter_readings(img, preprocess=True))
        check(False, "释放后再用必须显式报错")
    except PPOCREngineError:
        check(True, "释放后再用抛 PPOCREngineError")


def test_b_no_fallback():
    section("B. GPU 约束与「无回退」")

    from ocr.engine import OCREngine, OCREngineError
    from ocr.ppocr_engine import (
        PPOCREngine,
        PPOCREngineError,
        PPOCRModelNotFoundError,
        PPOCRModelLocator,
    )

    check(issubclass(PPOCREngineError, OCREngineError),
          "PPOCREngineError 是 OCREngineError 的子类"
          "（worker 既有错误/拆除路径无需改动即可接管）")
    check(callable(getattr(OCREngine, "iter_readings", None)),
          "Tesseract 引擎仍完整保留（对照 / 诊断 / 人工回退）")

    # GPU 强制：非 CUDA 编译版 / 无设备都必须显式失败，不回落 CPU
    class _FakeCuda:
        def __init__(self, n):
            self._n = n

        def device_count(self):
            return self._n

    class _FakeDevice:
        def __init__(self, compiled, n):
            self._c = compiled
            self.cuda = _FakeCuda(n)

        def is_compiled_with_cuda(self):
            return self._c

    for compiled, n, why in ((False, 1, "非 CUDA 编译版"), (True, 0, "无可见 GPU")):
        try:
            PPOCREngine._assert_gpu_available(_FakeDevice(compiled, n))
            check(False, "%s 时必须显式失败" % why)
        except PPOCREngineError:
            check(True, "%s 时显式失败（不回落 CPU）" % why)

    try:
        PPOCRModelLocator.find_model_dir(r"D:\__no_such_ppocr_model__")
        check(False, "模型目录不可用时必须显式失败")
    except PPOCRModelNotFoundError:
        check(True, "模型目录不可用时抛 PPOCRModelNotFoundError")

    # 静态检查：worker 的引擎路径只构造 PPOCREngine，且没有任何 Tesseract 回退
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ocr", "worker.py"), encoding="utf-8").read()
    body = src.split("def _ensure_engine", 1)[1].split("def _ensure_recognizer", 1)[0]
    check("PPOCREngine()" in body, "worker 在工作线程内构造 PPOCREngine")
    # \b 不能省：字符串 "PPOCREngine()" 里含有子串 "OCREngine()"。
    check(not re.search(r"\bOCREngine\s*\(", body),
          "worker 的引擎路径不再构造 Tesseract 引擎")
    check("except" not in body,
          "worker 的引擎构造没有任何 except 分支（不存在静默回退）")


def test_c_real_roi(engine):
    section("C. 真实 ROI 推理与准确率锚定")

    from ocr.room_code import RoomCodeRecognizer

    files = roi_files()
    check(len(files) == EXPECTED_SAMPLES,
          "样本数与离线基准一致 (%d)" % EXPECTED_SAMPLES, str(len(files)))

    recognizer = RoomCodeRecognizer(engine, profile="valorant")
    exact = 0
    empty = 0
    mismatched = []

    for name in files:
        img = imread_unicode(os.path.join(ROI_DIR, name))
        readings = list(engine.iter_readings(img, preprocess=True))
        if not readings or not readings[0][1]:
            empty += 1
        candidate = recognizer.recognize_frame(img)
        code = candidate.code if candidate is not None else ""
        truth = _truth_of(name)
        if code == truth:
            exact += 1
        else:
            mismatched.append((name, truth, code))

    check(empty == 0, "89 张 ROI 全部读出了非空文本（无静默空结果）", str(empty))
    check(all(_truth_of(n) for n in files),
          "89 张都能取到标注真值")

    # 结构化精确命中必须等于离线基准实测值（替换后行为与基准逐字一致）
    check(exact == EXPECTED_STRUCTURED_EXACT,
          "结构化精确命中 %d/%d，与离线基准实测一致"
          % (EXPECTED_STRUCTURED_EXACT, EXPECTED_SAMPLES),
          "实际 %d" % exact)

    got = {n: c for n, _t, c in mismatched}
    check(got == KNOWN_CASES,
          "未命中样本与基准已知的失败样本完全一致",
          "实际 %r" % got)


_TRUTH_CACHE = {}


def _truth_of(name):
    """从离线数据集的标注里取真值（只读）。"""
    if not _TRUTH_CACHE:
        ann_dir = os.path.join(os.path.dirname(ROI_DIR), "annotations")
        import json
        for fn in os.listdir(ann_dir):
            if not fn.endswith(".json"):
                continue
            try:
                data = json.loads(open(os.path.join(ann_dir, fn),
                                       encoding="utf-8").read())
            except Exception:  # noqa: BLE001
                continue
            if data.get("status") != "labeled":
                continue
            for key in ("text", "truth", "label", "code", "value"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    _TRUTH_CACHE[fn[:-5] + ".png"] = v.strip().upper()
                    break
    return _TRUTH_CACHE.get(name)


# ======================================================================
# D + E：真实 worker 生命周期与端到端链路
# ======================================================================
class _RecordingEngine:
    """记录实例数、属主线程与 shutdown 调用的 PP-OCRv6 包装（测试专用）。"""

    instances = []

    def __init__(self, *a, **k):
        from ocr.ppocr_engine import PPOCREngine
        self._inner = PPOCREngine(*a, **k)
        self.owner_thread = threading.get_ident()
        self.shutdown_called = False
        _RecordingEngine.instances.append(self)

    def iter_readings(self, image, preprocess=True):
        return self._inner.iter_readings(image, preprocess=preprocess)

    def recognize(self, image, preprocess=True, return_details=False):
        return self._inner.recognize(image, preprocess=preprocess,
                                     return_details=return_details)

    def backend_status(self):
        return self._inner.backend_status

    def shutdown(self):
        self.shutdown_called = True
        self._inner.shutdown()


class _FrameBufferStub:
    def __init__(self, frame):
        self._frame = frame
        self.dropped_count = 0
        self.update_count = 1

    def get(self):
        return self._frame


class _RoiStub:
    def __init__(self, roi):
        self._roi = roi

    def get_roi(self):
        return self._roi


def _build_frame():
    """把一张真实 ROI 放进一帧里，ROI 就是它在帧中的位置。"""
    from ocr.roi_manager import ROI

    # 用一张能稳定识别的样本（排除已知失败样本）
    files = roi_files()
    sample = None
    for f in files:
        if _truth_of(f) and f not in KNOWN_CASES:
            sample = f
            break
    img = imread_unicode(os.path.join(ROI_DIR, sample))
    pad = 20
    frame = np.zeros((img.shape[0] + pad * 2, img.shape[1] + pad * 2, 3), np.uint8)
    frame[pad:pad + img.shape[0], pad:pad + img.shape[1]] = img
    roi = ROI(pad, pad, img.shape[1], img.shape[0],
              frame.shape[1], frame.shape[0])
    return frame, roi, _truth_of(sample), sample


def test_de_lifecycle_and_chain(app):
    section("D/E. worker 生命周期 + 真实链路")

    import ocr.worker as worker_mod
    from ocr.clipboard import ClipboardManager
    from ocr.worker import OCRWorker

    frame, roi, truth, sample = _build_frame()
    print("  样本 %s 真值 %s 帧尺寸 %s" % (sample, truth, frame.shape))

    original = worker_mod.PPOCREngine
    worker_mod.PPOCREngine = _RecordingEngine
    _RecordingEngine.instances = []

    clipboard = ClipboardManager()
    saved_clip = app.clipboard().text() if app.clipboard() else ""
    main_thread = threading.get_ident()

    try:
        # ---------- 会话 1 ----------
        buffer = _FrameBufferStub(frame)
        worker = OCRWorker(lambda: buffer, _RoiStub(roi),
                           interval_ms=300, profile="valorant")

        t0 = time.perf_counter()
        worker.start_recognition()
        start_ms = (time.perf_counter() - t0) * 1000.0
        check(start_ms < 1000,
              "start_recognition() 立即返回（GUI 不被模型加载阻塞）",
              "耗时 %.0fms" % start_ms)

        copied = []
        worker.copy_requested.connect(lambda t: copied.append(t))
        texts = []
        worker.text_updated.connect(lambda t: texts.append(t))
        errors = []
        worker.error_occurred.connect(lambda e: errors.append(e))

        deadline = time.time() + 40
        while time.time() < deadline and not copied and worker.isRunning():
            app.processEvents()
            time.sleep(0.02)

        check(not errors, "识别过程中没有错误", repr(errors[:2]))
        check(bool(copied), "copy_requested 已发出（真实链路走到剪贴板边界）",
              repr(copied))
        if copied:
            check(copied[0] == truth,
                  "复制的是严格校验通过的真值房间号", repr(copied[0]))

        # 初始化确实发生在工作线程
        check(len(_RecordingEngine.instances) == 1,
              "一个 OCR 会话只创建一个模型实例",
              str(len(_RecordingEngine.instances)))
        eng = _RecordingEngine.instances[0]
        check(eng.owner_thread != main_thread,
              "模型在工作线程内初始化（不是 GUI 线程）")
        check(eng.backend_status()["closed"] is False,
              "会话进行中模型保持常驻")

        # ---------- 停止 ----------
        worker.stop_recognition()
        deadline = time.time() + 15
        while time.time() < deadline and worker.isRunning():
            app.processEvents()
            time.sleep(0.02)
        check(not worker.isRunning(), "worker 线程已退出")
        check(eng.shutdown_called, "停止识别时后端 shutdown() 被调用")
        check(eng.backend_status()["closed"] is True, "模型已释放")

        # ---------- 会话 2：重启必须是全新模型 ----------
        buffer2 = _FrameBufferStub(frame)
        worker2 = OCRWorker(lambda: buffer2, _RoiStub(roi),
                            interval_ms=300, profile="valorant")
        copied2 = []
        worker2.copy_requested.connect(lambda t: copied2.append(t))
        worker2.start_recognition()
        deadline = time.time() + 40
        while time.time() < deadline and not copied2 and worker2.isRunning():
            app.processEvents()
            time.sleep(0.02)
        worker2.stop_recognition()
        deadline = time.time() + 15
        while time.time() < deadline and worker2.isRunning():
            app.processEvents()
            time.sleep(0.02)

        check(len(_RecordingEngine.instances) == 2,
              "重启创建了新的模型实例（没有跨会话常驻）",
              str(len(_RecordingEngine.instances)))
        check(_RecordingEngine.instances[0] is not _RecordingEngine.instances[1],
              "两次会话是两个不同的对象")
        check(_RecordingEngine.instances[0].shutdown_called
              and _RecordingEngine.instances[1].shutdown_called,
              "两次会话结束时都被释放")

        # ---------- 剪贴板闭环 ----------
        if copied:
            ok = clipboard.copy(copied[0])
            check(ok and clipboard.get_last_copied() == copied[0],
                  "剪贴板管理器成功写入该房间号", repr(clipboard.get_last_copied()))
    finally:
        worker_mod.PPOCREngine = original
        if app.clipboard() and saved_clip is not None:
            app.clipboard().setText(saved_clip)   # 还原用户剪贴板


def test_f_worker_wiring_static():
    section("F. 冻结面静态校验")

    root = os.path.dirname(os.path.abspath(__file__))

    def read(rel):
        return open(os.path.join(root, rel), encoding="utf-8").read()

    worker = read(os.path.join("ocr", "worker.py"))
    # 调度相关的方法体必须原样保留
    check("self._stopped_event.wait(self._interval_for_wait() / 1000.0)" in worker,
          "worker 等待语义未变（仍由 _interval_for_wait 决定）")
    check("return min(self._pending_interval_ms, self._interval_ms)" in worker,
          "两速采样语义未变（稳态 300ms / 待确认 100ms）")
    check("_ESCALATION_SCALE" not in worker,
          "worker 里没有引入放大倍率（仍由识别器定义）")

    room = read(os.path.join("ocr", "room_code.py"))
    check("TemporalResolver" not in room or "_resolver" in room,
          "识别器仍只通过 resolver 接口做时序裁决")
    temporal = read(os.path.join("ocr", "temporal.py"))
    check("class TemporalResolver" in temporal, "TemporalResolver 仍在原处")

    ppocr = read(os.path.join("ocr", "ppocr_engine.py"))
    for banned in ("normalize_text", "resolve_candidate", "extract_runs",
                   "CodeFormat", "correction"):
        check(banned not in ppocr,
              "后端里没有模型专属业务规则：%s" % banned)
    top_level = [ln for ln in ppocr.splitlines()
                 if ln.startswith("import paddle") or ln.startswith("from paddlex")]
    check(not top_level,
          "paddle/paddlex 只在运行期加载，模块顶层不导入"
          "（生产 venv 没有 paddle，import ocr 仍必须可用）", repr(top_level))


def main():
    print("=" * 68)
    print("OCR 模型替换验收：PP-OCRv6 后端 + 生产识别链路")
    print("=" * 68)

    if not _paddle_available():
        return skip("当前解释器没有 paddle 运行时（生产 venv 即如此）。"
                    "请用带 paddle 的解释器运行，例如系统 Python 3.11。")

    if not roi_files():
        return skip("找不到 ROI 样本目录: %s" % ROI_DIR)

    from ocr.ppocr_engine import PPOCREngine

    engine = PPOCREngine()
    try:
        test_a_contract(engine)
        test_c_real_roi(engine)
    finally:
        engine.shutdown()

    test_a2_shutdown()
    test_b_no_fallback()

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_de_lifecycle_and_chain(app)

    test_f_worker_wiring_static()

    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
