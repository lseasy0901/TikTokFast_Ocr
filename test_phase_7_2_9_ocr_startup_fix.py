#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 启动卡死修复的回归测试

背景（已定位的根因）：
    点击「识别」时 OCREngine 在 GUI 线程被构造：
        _on_ocr_clicked → _start_ocr → OCRWorker(...) → OCREngine()
        → TesseractLocator.get_tesseract_version() → subprocess.run([exe, '--version'])
    打包后 tesseract 位于 _MEIPASS/runtime/tesseract，首次启动要冷加载
    约 157MB 的 DLL（还可能被杀软扫描），于是 GUI 卡死。

本测试断言修复后的两条事实：
    1. 构造 OCRWorker【不会】构造 OCREngine；引擎在 run() 内的工作线程中才创建。
    2. OCREngine 的构造路径【不会】启动 `tesseract --version` 子进程。

隔离说明：
    在构造 MainWindow 之前把 APPDATA 指向临时目录，
    既不会读取也不会改写用户真实的 license.json。
"""

import os
import sys
import tempfile
import threading
import time
import types
from unittest import mock

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-startup-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import ocr.engine as engine_mod  # noqa: E402
import ocr.worker as worker_mod  # noqa: E402
import gui.main_window as mw  # noqa: E402
from ocr.profiles import DELTA_FORCE  # noqa: E402
from ocr.roi_manager import ROIManager, ROI  # noqa: E402
from utils.license_manager import LicenseResult, LicenseStatus  # noqa: E402

_PASSED = 0
_FAILED = 0
app = None


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"    [ok]   {message}", flush=True)
    else:
        _FAILED += 1
        print(f"    [FAIL] {message}", flush=True)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------
class _EngineRecorder:
    """记录 OCREngine 是在哪个线程里被构造的。"""

    instances = []
    threads = []

    def __init__(self, *args, **kwargs):
        _EngineRecorder.instances.append(self)
        _EngineRecorder.threads.append(threading.current_thread())
        self.recognize_calls = 0

    def recognize(self, image, preprocess=True):
        self.recognize_calls += 1
        return ""


def reset_recorder() -> None:
    _EngineRecorder.instances.clear()
    _EngineRecorder.threads.clear()


class _FakeBuffer:
    """最小帧缓冲区桩：get() 返回一帧（或 None）。"""

    def __init__(self, frame=None):
        self._frame = frame

    def get(self):
        return self._frame

    def clear(self):
        self._frame = None


class _StubROIManager:
    has_roi = True

    def get_roi(self):
        return None


class _StubHistory:
    def add_or_update(self, url, nickname=""):
        pass

    def get_all(self):
        return []


def build_licensed_window():
    """构造 MainWindow 并注入一份「已激活」状态（不联网）。"""
    window = mw.MainWindow()
    window.show()
    for _ in range(400):
        app.processEvents()
        time.sleep(0.005)
        if window._license_load_worker and not window._license_load_worker.is_running():
            break
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)

    window._stream_history = _StubHistory()
    window._apply_license_result(
        LicenseResult(
            LicenseStatus.ACTIVATED,
            "已激活",
            expires_at=None,  # 永久授权，避免测试中到期
        )
    )
    # 模拟「已连接直播流」的运行时状态（不启动真实 FFmpeg）
    window._connected = True
    window._frame_buffer = _FakeBuffer()
    window._roi_manager = _StubROIManager()
    window._on_access_status_changed()
    return window


# ======================================================================
# 1. OCREngine 构造路径不再探测 --version
# ======================================================================
def test_engine_construction_has_no_version_probe():
    section("[1] OCREngine construction spawns no `tesseract --version` subprocess")
    fake_pt = types.SimpleNamespace(
        pytesseract=types.SimpleNamespace(tesseract_cmd=None, tessdata_dir=None)
    )

    probed = []
    with mock.patch.object(engine_mod, "TESSERACT_AVAILABLE", True), \
            mock.patch.object(engine_mod, "pytesseract", fake_pt), \
            mock.patch.object(engine_mod.TesseractLocator, "find_tesseract",
                              return_value=r"C:\fake\tesseract.exe"), \
            mock.patch.object(engine_mod.TesseractLocator, "find_tessdata",
                              return_value=r"C:\fake\tessdata"), \
            mock.patch.object(engine_mod.TesseractLocator, "get_tesseract_version",
                              side_effect=lambda *a, **k: probed.append(a)), \
            mock.patch("subprocess.run") as run:
        engine_mod.OCREngine()

        check(not run.called,
              "no subprocess.run() during OCREngine() construction")
        check(not probed,
              "get_tesseract_version() is not called from the construction path")

    # 诊断用途的辅助函数仍然存在（供显式调用）
    check(callable(getattr(engine_mod.TesseractLocator, "get_tesseract_version", None)),
          "TesseractLocator.get_tesseract_version still available for diagnostics")


# ======================================================================
# 2. 构造 OCRWorker 不构造 OCREngine
# ======================================================================
def test_worker_construction_does_not_build_engine():
    section("[2] OCRWorker(...) construction does not construct OCREngine")
    reset_recorder()

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))

    with mock.patch.object(worker_mod, "OCREngine", _EngineRecorder):
        worker = worker_mod.OCRWorker(
            frame_buffer_getter=lambda: None,
            roi_manager=roi_manager,
            interval_ms=100,
            profile=DELTA_FORCE.profile_id,
        )

    check(len(_EngineRecorder.instances) == 0,
          "no OCREngine created while constructing OCRWorker")
    check(worker._engine is None,
          "worker holds no engine until the worker thread starts")


# ======================================================================
# 3. 引擎在【工作线程】内创建
# ======================================================================
def test_engine_is_built_inside_worker_thread():
    section("[3] OCREngine is constructed inside the OCR worker thread")
    reset_recorder()

    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame_buffer = _FakeBuffer(frame)

    with mock.patch.object(worker_mod, "OCREngine", _EngineRecorder):
        worker = worker_mod.OCRWorker(
            frame_buffer_getter=lambda: frame_buffer,
            roi_manager=roi_manager,
            interval_ms=50,
            profile=DELTA_FORCE.profile_id,
        )
        worker.start_recognition()
        for _ in range(200):
            if _EngineRecorder.instances:
                break
            time.sleep(0.01)
        worker.stop_recognition()
        worker.wait(2000)

    check(len(_EngineRecorder.instances) == 1,
          "engine created exactly once, lazily, by the worker")

    if _EngineRecorder.threads:
        built_in = _EngineRecorder.threads[0]
        check(built_in is not threading.main_thread(),
              f"engine built off the GUI thread (thread={built_in.name})")
    else:
        check(False, "engine was never created by the worker")


# ======================================================================
# 4. 点击「识别」：GUI 线程不再构造引擎，且立即返回
# ======================================================================
def test_click_path_does_not_block_gui():
    section("[4] Clicking 「识别」 does not build the engine on the GUI thread")
    window = build_licensed_window()
    reset_recorder()

    with mock.patch.object(worker_mod, "OCREngine", _EngineRecorder):
        started = time.perf_counter()
        window._on_ocr_clicked()          # 真实点击路径
        elapsed = time.perf_counter() - started

        check(elapsed < 2.0,
              f"click returns immediately (elapsed {elapsed * 1000:.0f}ms)")

        check(threading.main_thread() not in _EngineRecorder.threads,
              "OCREngine was never constructed on the GUI thread")

        # 等 worker 线程把引擎建起来（证明只是被移到了工作线程，而不是被删掉）
        for _ in range(300):
            if _EngineRecorder.instances:
                break
            app.processEvents()
            time.sleep(0.01)

        check(len(_EngineRecorder.instances) >= 1,
              "engine is still created — just inside the worker thread")
        check(all(t is not threading.main_thread() for t in _EngineRecorder.threads),
              "every engine construction happened off the GUI thread")

        window._stop_ocr()
        for _ in range(50):
            app.processEvents()
            time.sleep(0.005)

    window.close()


def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])

    print("=" * 70, flush=True)
    print("Phase 7.2-9: OCR startup-freeze regression suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print("=" * 70, flush=True)

    test_engine_construction_has_no_version_probe()
    test_worker_construction_does_not_build_engine()
    test_engine_is_built_inside_worker_thread()
    test_click_path_does_not_block_gui()

    print("\n" + "=" * 70, flush=True)
    total = _PASSED + _FAILED
    if _FAILED == 0:
        print(f"RESULT: {_PASSED}/{total} checks passed, 0 failed", flush=True)
    else:
        print(f"RESULT: {_PASSED}/{total} checks passed, {_FAILED} FAILED", flush=True)
    print("=" * 70, flush=True)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
