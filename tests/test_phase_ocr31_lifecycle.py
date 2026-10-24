# -*- coding: utf-8 -*-
"""Phase OCR-3.1：常驻引擎的生命周期与恢复验证（任务 C）。

覆盖：
    [C1] 反复「启动 OCR → 识别 → 停止」20 轮：每轮后句柄归零、无线程/进程/临时文件残留，
         且一个会话内引擎只初始化一次（不重复加载模型）
    [C2] 停止后再次启动（断流重连的等价路径）：新 worker 能正常工作，旧会话无残留
    [C3] 同一实例 shutdown 后可继续使用（自动重建句柄）
    [C4] 原生访问冲突的可捕获性实测：把「两线程共享一个句柄」放在**子进程**里跑，
         记录它是抛出 Python 异常（可捕获）还是直接杀死进程 —— 结论只描述实测，
         不宣称"原生崩溃一定能被 Python 回退接住"

真机运行（需要随包 Tesseract）：
    venv/Scripts/python.exe test_phase_ocr31_lifecycle.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from bench_phase_ocr1_latency import make_card
from ocr import engine as engine_mod
from ocr.engine import OCREngine
from ocr.profiles import DELTA_FORCE
from ocr.roi_manager import ROI, ROIManager
from ocr.worker import OCRWorker

FAILURES = []
CARD = None


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
    try:
        return [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("tess_")]
    except OSError:
        return []


class Buf:
    def __init__(self, img):
        self._img = img

    def get(self):
        return self._img


def make_worker(engine):
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, CARD.shape[1], CARD.shape[0],
                            CARD.shape[1], CARD.shape[0]))
    return OCRWorker(frame_buffer_getter=lambda: Buf(CARD),
                     roi_manager=roi_manager,
                     engine=engine,
                     interval_ms=50,
                     profile=DELTA_FORCE.profile_id)


def wait_for(pred, app, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        app.processEvents()
        time.sleep(0.01)
    return bool(pred())


# ----------------------------------------------------------------------
# [C1] 反复启停：无残留 + 一个会话只初始化一次
# ----------------------------------------------------------------------
def test_repeated_start_stop(app):
    section("[C1] 反复启动/停止 20 轮：句柄、线程、进程、临时文件 + 不重复初始化")
    baseline_threads = threading.active_count()
    baseline_procs = tesseract_processes()
    baseline_files = len(tess_temp_files())
    first_ms, warm_ms = [], []
    try:
        for cycle in range(20):
            engine = OCREngine()
            worker = make_worker(engine)
            t0 = time.perf_counter()
            worker.start_recognition()
            if not wait_for(lambda: worker.recognition_count >= 1, app, 5.0):
                check(False, "第 %d 轮：worker 未完成首轮识别" % (cycle + 1))
                worker.stop_recognition()
                break
            t_first = time.perf_counter()
            first_ms.append((t_first - t0) * 1000.0)
            worker.stop_recognition()
            wait_for(lambda: not worker.isRunning(), app, 5.0)

            status = engine.backend_status()
            if not check(status["live_engines"] == 0,
                         "第 %d 轮：停止后句柄归零" % (cycle + 1), repr(status)):
                break
    finally:
        pass

    check(tesseract_processes() == baseline_procs,
          "20 轮后无残留 tesseract.exe 进程",
          "%d -> %d" % (baseline_procs, tesseract_processes()))
    check(len(tess_temp_files()) <= baseline_files, "20 轮后无临时文件残留")
    check(threading.active_count() <= baseline_threads + 1,
          "线程数回到基线（无线程泄漏）",
          "%d -> %d" % (baseline_threads, threading.active_count()))

    # 「引擎不重复初始化」的直接证据（单线程、无打桩）：同一实例第 1 次调用
    # 要付模型加载，后续调用不付。刻意不 monkeypatch 引擎类 —— 实测那种打桩
    # 本身会扰动 QThread 的 Python 线程记录，把测量工具的影响当成产品问题。
    eng = OCREngine()
    t0 = time.perf_counter()
    eng._run_psm(CARD, 6)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    warm = []
    for _ in range(3):
        t0 = time.perf_counter()
        eng._run_psm(CARD, 6)
        warm.append((time.perf_counter() - t0) * 1000.0)
    eng.shutdown()
    warm_ms = sorted(warm)[1]
    check(cold_ms > 20.0 and warm_ms < cold_ms * 0.4,
          "引擎每次会话只初始化一次（首调用 %.0fms 含模型加载，后续 %.0fms）"
          % (cold_ms, warm_ms))
    check(len(first_ms) == 20, "20 轮都完成了首轮识别", "%d 轮" % len(first_ms))
    print("  20 轮：线程 %d->%d；引擎首调用 %.0fms / 后续 %.0fms"
          % (baseline_threads, threading.active_count(), cold_ms, warm_ms))
    return {"cycles": len(first_ms), "cold_ms": round(cold_ms, 1),
            "warm_ms": round(warm_ms, 1)}


# ----------------------------------------------------------------------
# [C2] 停止后再次启动（断流重连等价路径）
# ----------------------------------------------------------------------
def test_restart_after_stop(app):
    section("[C2] 停止后再启动：新会话可用，旧会话无残留")
    engine1 = OCREngine()
    w1 = make_worker(engine1)
    w1.start_recognition()
    check(wait_for(lambda: engine1.backend_status()["live_engines"] == 1, app, 5.0),
          "第一次 OCR 会话引擎就绪")
    w1.stop_recognition()
    wait_for(lambda: not w1.isRunning(), app, 5.0)
    check(engine1.backend_status()["live_engines"] == 0, "第一次会话句柄已释放")

    # 重连等价路径：新会话用新的 worker + 新引擎
    engine2 = OCREngine()
    w2 = make_worker(engine2)
    texts = []
    w2.text_updated.connect(lambda t: texts.append(t))
    w2.start_recognition()
    check(wait_for(lambda: engine2.backend_status()["live_engines"] == 1, app, 5.0),
          "第二次 OCR 会话引擎就绪")
    check(wait_for(lambda: "ABX1234" in texts, app, 10.0),
          "重连后仍能识别出房间号", repr(texts[:3]))
    w2.stop_recognition()
    wait_for(lambda: not w2.isRunning(), app, 5.0)
    check(engine2.backend_status()["live_engines"] == 0, "第二次会话句柄已释放")
    check(not w1.isRunning() and not w2.isRunning(), "两个 worker 线程都已退出")

    section("[C3] 同一实例 shutdown 后继续可用（自动重建句柄）")
    e = OCREngine()
    t1 = e._run_psm(CARD, 6)
    e.shutdown()
    t2 = e._run_psm(CARD, 6)
    check(t1 == t2 == "ABX1234", "shutdown 前后识别结果一致", "%r / %r" % (t1, t2))
    e.shutdown()


# ----------------------------------------------------------------------
# [C4] 原生访问冲突的可捕获性（子进程内实测，不污染本进程）
# ----------------------------------------------------------------------
def test_native_fault_catchability():
    section("[C4] 原生访问冲突：能否被 Python 回退接住（子进程实测）")
    code = (
        "import sys, threading\n"
        "sys.path.insert(0, '@ROOT@')\n"
        "from bench_phase_ocr1_latency import make_card\n"
        "from ocr.engine import OCREngine\n"
        "CARD = make_card('ABX1234', False)\n"
        "eng = OCREngine()\n"
        "eng._run_psm(CARD, 6)\n"
        "print('child: handle ready', flush=True)\n"
        "caught = []\n"
        "def run():\n"
        "    try:\n"
        "        for _ in range(30):\n"
        "            eng._run_psm(CARD, 6)\n"
        "    except BaseException as e:\n"
        "        caught.append('%s: %s' % (type(e).__name__, e))\n"
        "ts = [threading.Thread(target=run) for _ in range(2)]\n"
        "[t.start() for t in ts]; [t.join() for t in ts]\n"
        "print('child: caught=%d %s' % (len(caught), caught[:1]), flush=True)\n"
    ).replace("@ROOT@", os.path.abspath("."))

    log = os.path.join(tempfile.gettempdir(), "ocr31_fault_child.log")
    with open(log, "wb") as fh:
        proc = subprocess.Popen([sys.executable, "-u", "-c", code],
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        proc.wait(timeout=120)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait(timeout=10)
    with open(log, "rb") as fh:
        out = fh.read().decode("utf-8", "replace").strip()
    caught = "caught=" in out and "caught=0" not in out
    print("  子进程退出码=%s 超时=%s" % (proc.returncode, timed_out))
    print("  子进程输出尾部: %r" % out[-200:])
    if timed_out:
        check(False, "共享句柄并发导致子进程卡死（不可自动恢复）")
    elif caught:
        check(True, "本次实测：原生访问冲突以 Python 异常形式抛出（该场景可被捕获并回退）")
    elif proc.returncode == 0:
        check(True, "本次实测：并发共享句柄未触发故障（未观察到异常）")
    else:
        check(True, "本次实测：进程被原生故障终止（Python 无法捕获，只能靠进程隔离）")
    print("  说明：以上只描述本次实测。原生故障是否可捕获取决于故障位置与线程，"
          "不能作为「崩溃一定能回退」的依据；"
          "进程内常驻引擎的崩溃隔离弱于旧的子进程路径。")


def main():
    global CARD
    print("=" * 68)
    print("Phase OCR-3.1 任务 C：常驻引擎生命周期与恢复")
    print("=" * 68)
    app = QApplication.instance() or QApplication([])
    CARD = make_card("ABX1234", False)
    test_repeated_start_stop(app)
    test_restart_after_stop(app)
    test_native_fault_catchability()

    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
