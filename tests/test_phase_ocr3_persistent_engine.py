# -*- coding: utf-8 -*-
"""Phase OCR-3 验收：常驻 Tesseract 引擎 vs 旧 pytesseract 子进程路径。

覆盖：
    [B/C] correctness A/B（clean / hard ROI、PSM 6/7/11、escalation、空 ROI、
          whitelist、连续 200 次）—— 两条后端输出必须**逐字一致**
    [D]   shutdown 后无残留（句柄全部释放、无子进程、无临时文件）
    [E]   常驻初始化失败 -> 自动回退 legacy，识别继续可用
    [F]   一线程一引擎（同一实例两个线程各持独立句柄，并发识别都正确）

真机运行（需要随包 Tesseract），用法：
    venv/Scripts/python.exe test_phase_ocr3_persistent_engine.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from bench_phase_ocr1_latency import make_card          # 与 OCR-1 同一张卡
from ocr import engine as engine_mod
from ocr.engine import OCREngine
from ocr.profiles import DELTA_FORCE
from ocr.room_code import RoomCodeRecognizer
from ocr.temporal import TemporalResolver

FAILURES = []


def section(title):
    print("\n" + title)


def check(cond, msg, extra=""):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s %s" % (msg, extra))
        FAILURES.append(msg)
    return bool(cond)


def tesseract_processes():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tesseract.exe", "/NH"],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    return out.lower().count("tesseract.exe")


def tess_temp_files():
    tmp = tempfile.gettempdir()
    try:
        return [f for f in os.listdir(tmp) if f.startswith("tess_")]
    except OSError:
        return []


# ----------------------------------------------------------------------
# [B/C] correctness A/B
# ----------------------------------------------------------------------
def test_output_equivalence():
    section("[B/C] 输出逐字一致：clean / hard × PSM 6/7/11")
    persistent = OCREngine()
    legacy = OCREngine(backend="legacy")
    check(persistent.backend == "persistent", "默认后端是常驻引擎")
    check(legacy.backend == "legacy", "可显式选择 legacy 后端")

    cards = {"clean": make_card("ABX1234", False),
             "hard": make_card("IOI1000", False)}
    for name, img in cards.items():
        for psm in (6, 7, 11):
            a = persistent._run_psm(img, psm)
            b = legacy._run_psm(img, psm)
            check(a == b, "%s psm=%d 输出一致" % (name, psm), "%r vs %r" % (a, b))

    section("[B/C] 非 3 通道布局：灰度 / 4 通道（旧路径一直支持，常驻路径必须等价）")
    import cv2
    gray = np.full((80, 320), 255, dtype=np.uint8)
    cv2.putText(gray, "AB1234", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
    rgba = cv2.cvtColor(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2BGRA)
    for label, img in (("灰度 2-D", gray), ("4 通道", rgba)):
        for psm in (6, 7):
            a = persistent._run_psm(img, psm)
            b = legacy._run_psm(img, psm)
            check(a == b, "%s psm=%d 两后端输出一致" % (label, psm),
                  "%r vs %r" % (a, b))
        check("1234" in (a + b), "%s 确实识别出了文字" % label, "%r / %r" % (a, b))

    section("[B/C] 空 ROI（全黑图）")
    # 注意：空白图 + 单行模式(PSM 7) 下 Tesseract 会"读出"噪声（实测 'ee'），
    # 两条后端都如此 —— 这正是上层严格校验存在的意义。这里要求的是
    # **两后端逐字一致**，并且识别管线必须把它判为"无结果"。
    black = np.zeros((70, 260, 3), dtype=np.uint8)
    for psm in (6, 7, 11):
        a = persistent._run_psm(black, psm)
        b = legacy._run_psm(black, psm)
        check(a == b, "全黑图 psm=%d 两后端逐字一致（实测 %r）" % (psm, a),
              "%r vs %r" % (a, b))
    resolver = TemporalResolver()
    rec_black = RoomCodeRecognizer(persistent, profile=DELTA_FORCE.profile_id,
                                   resolver=resolver)
    cand = rec_black.recognize_frame(black)
    check(cand is None, "全黑图经严格校验后不产生任何房间号候选", repr(cand))

    section("[B/C] whitelist 生效且两后端一致")
    restricted = OCREngine(whitelist="AB")
    restricted_legacy = OCREngine(whitelist="AB", backend="legacy")
    img = make_card("ABX1234", False)
    a = restricted._run_psm(img, 6)
    b = restricted_legacy._run_psm(img, 6)
    full = persistent._run_psm(img, 6)
    check(a == b, "受限 whitelist 下两后端一致", "%r vs %r" % (a, b))
    check(a != full, "受限 whitelist 确实改变了输出（配置生效）", "%r vs %r" % (a, full))

    section("[B/C] 完整识别管线（含 correction / 仲裁 / escalation / 时序）")
    for name, text in (("clean", "ABX1234"), ("hard", "IOI1000"),
                       ("case-fold", "ggm1180")):
        img = make_card(text, False)
        outs = []
        for backend in ("persistent", "legacy"):
            eng = OCREngine(backend=backend)
            resolver = TemporalResolver()
            rec = RoomCodeRecognizer(eng, profile=DELTA_FORCE.profile_id,
                                     resolver=resolver)
            seen = []
            for _ in range(2):                      # 两次识别 -> 时序成立
                cand = rec.recognize_frame(img)
                seen.append(cand.code if cand else None)
            outs.append(seen)
        check(outs[0] == outs[1],
              "%s：两后端在同一管线下给出相同结果" % name, repr(outs))
        if name == "case-fold":
            check(any(o is not None for o in outs[0]),
                  "case-fold 用例确实走进了识别管线（非全空）", repr(outs[0]))

    section("[B/C] 连续 200 次调用输出稳定且与 legacy 一致")
    img = make_card("ABX1234", False)
    first = persistent._run_psm(img, 6)
    drift = None
    for i in range(200):
        cur = persistent._run_psm(img, 6)
        if cur != first:
            drift = (i, cur)
            break
    check(drift is None, "常驻引擎 200 次连续调用无漂移", repr(drift))
    check(first == legacy._run_psm(img, 6), "200 次之后仍与 legacy 一致")

    persistent.shutdown()
    legacy.shutdown()
    restricted.shutdown()
    restricted_legacy.shutdown()


# ----------------------------------------------------------------------
# [D] shutdown 无残留
# ----------------------------------------------------------------------
def test_shutdown_clean():
    section("[D] shutdown 后无残留")
    before_proc = tesseract_processes()
    before_files = len(tess_temp_files())

    eng = OCREngine()
    img = make_card("ABX1234", False)
    eng._run_psm(img, 6)                      # 触发句柄创建
    status = eng.backend_status()
    check(status["live_engines"] == 1, "已有 1 个常驻句柄", repr(status))
    check(tesseract_processes() == before_proc,
          "常驻路径不产生任何 tesseract.exe 子进程",
          "%d -> %d" % (before_proc, tesseract_processes()))

    handles = list(eng._engines.values())
    eng.shutdown()
    check(eng.backend_status()["live_engines"] == 0, "shutdown 后句柄归零")
    check(all(h.closed for h in handles), "句柄都已标记释放")
    eng.shutdown()                            # 幂等
    check(eng.backend_status()["live_engines"] == 0, "shutdown 幂等")

    check(tesseract_processes() == before_proc, "shutdown 后没有残留子进程")
    check(len(tess_temp_files()) <= before_files, "常驻路径不产生临时文件",
          repr(tess_temp_files()[:3]))

    # shutdown 之后仍可用（会自动重建句柄），只是要重新付一次初始化
    t0 = time.perf_counter()
    text = eng._run_psm(img, 6)
    check(text == "ABX1234", "shutdown 后再次调用仍正确", repr(text))
    check((time.perf_counter() - t0) * 1000 > 20,
          "该次调用包含重新初始化（符合预期）")
    eng.shutdown()


# ----------------------------------------------------------------------
# [E] 初始化失败回退 legacy
# ----------------------------------------------------------------------
def test_fallback_on_init_failure():
    section("[E] 常驻初始化失败 -> 自动回退 legacy")

    # E1：tessdata 路径不存在（真实初始化失败，不打桩）
    eng = OCREngine()
    eng._tessdata_path = os.path.join(tempfile.gettempdir(), "no_such_tessdata")
    img = make_card("ABX1234", False)
    text = eng._run_psm(img, 6)
    check(text == "ABX1234", "tessdata 不存在时仍能识别（走 legacy）", repr(text))
    status = eng.backend_status()
    check(status["persistent_error"] is not None, "记录了回退原因", repr(status))
    check(status["live_engines"] == 0, "没有残留的半成品引擎")
    text2 = eng._run_psm(img, 7)
    check(text2 == "ABX1234", "回退后后续调用继续可用（legacy）", repr(text2))
    eng.shutdown()

    # E2：DLL 加载失败（用打桩模拟缺 DLL）——构造只应被尝试一次
    calls = {"n": 0}
    real_cls = engine_mod._ResidentEngine

    class Boom:
        def __init__(self, *a, **kw):
            calls["n"] += 1
            raise OSError("模拟 DLL 加载失败")

    engine_mod._ResidentEngine = Boom
    try:
        eng2 = OCREngine()
        for _ in range(3):
            check(eng2._run_psm(img, 6) == "ABX1234", "DLL 失败后仍能识别")
        check(calls["n"] == 1, "初始化失败只尝试一次，不反复重试", repr(calls))
        check(eng2.backend_status()["persistent_error"] is not None,
              "记录了 DLL 失败原因")
        eng2.shutdown()
    finally:
        engine_mod._ResidentEngine = real_cls


# ----------------------------------------------------------------------
# [F] 一线程一引擎
# ----------------------------------------------------------------------
def test_thread_binding():
    section("[F] 一线程一引擎：同实例两个线程并发识别")
    eng = OCREngine()
    img = make_card("ABX1234", False)
    results = {}
    errors = []

    def work(idx):
        try:
            outs = [eng._run_psm(img, 6) for _ in range(20)]
            results[idx] = set(outs)
        except Exception as e:  # noqa: BLE001
            errors.append("线程%d: %s" % (idx, e))

    ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check(not errors, "两个线程并发调用无异常", repr(errors))
    check(all(v == {"ABX1234"} for v in results.values()),
          "两个线程的输出都正确且稳定", repr(results))
    status = eng.backend_status()
    check(status["live_engines"] == 2, "每个线程各持一个句柄（绝不共享）", repr(status))
    eng.shutdown()
    check(eng.backend_status()["live_engines"] == 0, "两个句柄都被释放")


def test_worker_lifecycle():
    """OCRWorker 停止时释放常驻引擎（资源随线程生命周期回收）"""
    section("[D2] worker 停止 -> 引擎释放")
    from PySide6.QtWidgets import QApplication
    from ocr.roi_manager import ROI, ROIManager
    from ocr.worker import OCRWorker

    app = QApplication.instance() or QApplication([])   # noqa: F841
    engine = OCREngine()
    img = make_card("ABX1234", False)

    class Buf:
        def get(self):
            return img

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, img.shape[1], img.shape[0],
                            img.shape[1], img.shape[0]))
    worker = OCRWorker(frame_buffer_getter=lambda: Buf(),
                       roi_manager=roi_manager,
                       engine=engine,
                       interval_ms=50,
                       profile=DELTA_FORCE.profile_id)
    worker.start_recognition()
    deadline = time.time() + 10
    while time.time() < deadline and engine.backend_status()["live_engines"] == 0:
        app.processEvents()
        time.sleep(0.01)
    check(engine.backend_status()["live_engines"] == 1,
          "worker 运行中持有 1 个常驻引擎", repr(engine.backend_status()))
    worker.stop_recognition()
    deadline = time.time() + 10
    while time.time() < deadline and engine.backend_status()["live_engines"] != 0:
        app.processEvents()
        time.sleep(0.01)
    check(engine.backend_status()["live_engines"] == 0,
          "worker 停止后引擎已释放", repr(engine.backend_status()))
    check(not worker.isRunning(), "worker 线程已退出")


def main():
    print("=" * 68)
    print("Phase OCR-3：常驻 Tesseract 引擎 vs 旧 pytesseract 路径")
    print("=" * 68)
    test_output_equivalence()
    test_shutdown_clean()
    test_fallback_on_init_failure()
    test_thread_binding()
    test_worker_lifecycle()

    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
