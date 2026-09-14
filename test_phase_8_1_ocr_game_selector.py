#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8.1: OCR 游戏档选择器回归测试

覆盖：
    - UI 只暴露两款具体游戏，不暴露 "auto"
    - 选择结果真的传到了 OCRWorker(profile=...)
    - 运行中切换只换档：不重启 OCR 线程、不碰视频流、不阻塞 GUI
    - 300ms 识别间隔、ROI、剪贴板、受保护入口语义不变
    - 构造 OCRWorker / 切换档位都不会在 GUI 线程构造 OCREngine

隔离说明：
    在构造 MainWindow 之前把 APPDATA 指向临时目录，
    既不会读取也不会改写用户真实的 license.json；
    连接与 OCR 都用桩替换，不启动真实 FFmpeg/Tesseract。
"""

import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-ocr-game-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
import ocr.worker as worker_mod  # noqa: E402
from ocr.profiles import DELTA_FORCE, PROFILES, VALORANT, get_profile  # noqa: E402
from ocr.roi_manager import ROI, ROIManager  # noqa: E402
from ocr.room_code import RoomCodeRecognizer, SingleFrameResolver  # noqa: E402
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
# 桩
# ----------------------------------------------------------------------
class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _StubOCRWorker:
    """记录构造参数与档位切换，不启动线程。"""

    instances = []

    def __init__(self, frame_buffer_getter=None, roi_manager=None, interval_ms=300,
                 profile=None, scale=1.0, recognizer=None):
        self.frame_buffer_getter = frame_buffer_getter
        self.roi_manager = roi_manager
        self.interval_ms = interval_ms
        self.profile = profile
        self.scale = scale
        self.profile_changes = []
        self.started_count = 0
        self.stopped = False
        self.text_updated = _Sig()
        self.copy_requested = _Sig()
        self.error_occurred = _Sig()
        self.game_mismatch = _Sig()   # Phase 8.6：选错游戏提醒（无参数）
        self.started = _Sig()
        self.finished = _Sig()
        _StubOCRWorker.instances.append(self)

    def set_profile(self, profile):
        self.profile_changes.append(profile)
        self.profile = profile

    def start_recognition(self):
        self.started_count += 1
        self.started.emit()

    def stop_recognition(self):
        self.stopped = True


class _StubROISelector:
    def __init__(self, frame, parent=None):
        self.roi_selected = _Sig()
        self.roi_selection_canceled = _Sig()

    def show(self):
        pass

    def exec(self):
        return 0


class _StubConnectWorker:
    def __init__(self, live_url, on_success, on_error):
        pass

    def start(self):
        pass

    def is_running(self):
        return False


class _StubROIManager:
    has_roi = True


class _FakeBuffer:
    def __init__(self):
        self.cleared = False

    def get(self):
        return None

    def clear(self):
        self.cleared = True


class _FakeReader:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeBridge:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _EngineRecorder:
    """记录 OCREngine 的构造线程（用来证明 GUI 线程没有被卡住）。"""

    instances = []
    threads = []

    def __init__(self, *args, **kwargs):
        _EngineRecorder.instances.append(self)
        _EngineRecorder.threads.append(threading.current_thread())

    def recognize(self, image, preprocess=True):
        return ""


def install_stubs() -> None:
    mw._ConnectWorker = _StubConnectWorker
    mw.ROISelector = _StubROISelector
    mw.OCRWorker = _StubOCRWorker
    mw.ExpiredDialog = _StubROISelector  # 不会被用到，仅兜底


def build_window():
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
    return window


def set_active(window) -> None:
    window._apply_license_result(
        LicenseResult(LicenseStatus.ACTIVATED, "已激活",
                      expires_at=datetime.now(timezone.utc) + timedelta(days=30))
    )


def simulate_connected(window) -> None:
    window._connected = True
    window._frame_buffer = _FakeBuffer()
    window._roi_manager = _StubROIManager()
    window._connect_btn.setText("断开")


def select_game(window, profile_id: str) -> None:
    """模拟用户在下拉框里选择某款游戏。"""
    index = window._ocr_game_combo.findData(profile_id)
    window._ocr_game_combo.setCurrentIndex(index)


# ======================================================================
# 1. UI 只暴露具体游戏
# ======================================================================
def test_selector_options():
    section("[1] 选择器：只暴露两款具体游戏，不暴露 auto")
    window = build_window()
    combo = window._ocr_game_combo

    ids = [combo.itemData(i) for i in range(combo.count())]
    labels = [combo.itemText(i) for i in range(combo.count())]

    check(combo.count() == 2, f"下拉框只有 2 个选项 (实际 {combo.count()})")
    check(ids == [DELTA_FORCE.profile_id, VALORANT.profile_id],
          f"选项对应注册表里的两个档: {ids}")
    check("三角洲行动" in labels and "无畏契约" in labels, f"选项文案: {labels}")
    check(not any("auto" in str(value).lower() for value in ids + labels),
          "UI 上没有 auto 选项")
    check(len(mw.OCR_GAME_CHOICES) == 2
          and all(pid in PROFILES for pid, _ in mw.OCR_GAME_CHOICES),
          "UI 选项全部来自既有 profile 注册表")

    section("[1b] auto 仍然只作为内部兼容档存在")
    check("auto" in PROFILES and get_profile("auto").profile_id == "auto",
          "auto 仍在注册表里（供内部默认/测试使用）")
    check(get_profile(None) is PROFILES["auto"], "不传档位时仍回落到 auto")

    section("[1c] 默认选中一款具体游戏")
    check(combo.currentData() == DELTA_FORCE.profile_id,
          f"默认选中 三角洲行动 (实际 {combo.currentData()})")
    check(window._ocr_profile_id == DELTA_FORCE.profile_id,
          "窗口内部状态与下拉框一致")
    check(window._ocr_profile_id != "auto", "默认档不是 auto")
    check(get_profile(window._ocr_profile_id) is DELTA_FORCE,
          "默认档解析出的是 Delta Force 格式，不是 auto 多格式")

    window.close()


# ======================================================================
# 2. 选择结果到达 OCRWorker
# ======================================================================
def test_profile_reaches_worker():
    section("[2] 选择的档位传到 OCRWorker(profile=...)")
    window = build_window()
    set_active(window)
    simulate_connected(window)

    _StubOCRWorker.instances.clear()
    window._start_ocr()
    check(len(_StubOCRWorker.instances) == 1, "OCR 启动时构造了 worker")
    worker = _StubOCRWorker.instances[-1]
    check(worker.profile == DELTA_FORCE.profile_id,
          f"默认（三角洲行动）-> profile={worker.profile!r}")
    check(worker.interval_ms == 300, f"识别间隔仍是 300ms (实际 {worker.interval_ms})")

    # 切到无畏契约后重新启动 OCR
    select_game(window, VALORANT.profile_id)
    _StubOCRWorker.instances.clear()
    window._start_ocr()
    worker = _StubOCRWorker.instances[-1]
    check(worker.profile == VALORANT.profile_id,
          f"选择无畏契约 -> profile={worker.profile!r}")
    check(worker.interval_ms == 300, "切档后识别间隔不变")

    window._stop_ocr()
    window.close()


def test_live_switch_reaches_running_worker():
    section("[3] 运行中切换：档位即时生效")
    window = build_window()
    set_active(window)
    simulate_connected(window)
    _StubOCRWorker.instances.clear()

    window._start_ocr()
    worker = _StubOCRWorker.instances[-1]
    check(worker.profile == DELTA_FORCE.profile_id, "起始档位: 三角洲行动")

    select_game(window, VALORANT.profile_id)
    check(worker.profile_changes == [VALORANT.profile_id],
          f"运行中的 worker 收到切档通知 (实际 {worker.profile_changes})")
    check(worker.profile == VALORANT.profile_id, "worker 当前档位已更新")
    check(window._ocr_profile_id == VALORANT.profile_id, "窗口状态已更新")
    check("OCR 识别游戏" in window.statusBar().currentMessage(),
          "状态栏提示已切换游戏（沿用既有状态栏行为）")

    window._stop_ocr()
    window.close()


# ======================================================================
# 3. 切换不影响视频连接 / 不重启 OCR 线程
# ======================================================================
def test_switch_does_not_touch_video_or_restart():
    section("[4] 切档：不重启视频流、不重启 OCR 线程、不阻塞 GUI")
    window = build_window()
    set_active(window)
    simulate_connected(window)
    window._reader = _FakeReader()
    window._frame_bridge = _FakeBridge()
    _StubOCRWorker.instances.clear()

    window._start_ocr()
    worker = _StubOCRWorker.instances[-1]
    check(window._ocr_running is True and window._ocr_btn.text() == "识别中",
          "切档前 OCR 处于运行中（模拟 started 信号）")

    started = time.perf_counter()
    select_game(window, VALORANT.profile_id)
    elapsed = time.perf_counter() - started
    check(elapsed < 1.0, f"切档立即返回 (实测 {elapsed * 1000:.0f}ms)")

    check(window._ocr_worker is worker, "切档没有重建 OCR worker")
    check(worker.stopped is False and worker.started_count == 1,
          "切档没有停掉/重启 OCR 线程")

    check(window._connected is True, "切档后仍处于连接状态")
    check(window._reader is not None and not window._reader.stopped,
          "FFmpegReader 未被停止")
    check(window._frame_bridge is not None and not window._frame_bridge.stopped,
          "帧桥接线程未被停止")
    check(window._frame_buffer is not None and not window._frame_buffer.cleared,
          "帧缓冲区未被清空")
    check(window._connect_btn.text() == "断开", "连接按钮状态未被切档改变")
    check(window._ocr_running is True and window._ocr_btn.text() == "识别中",
          "切档后 OCR 仍在运行，按钮文案不变")

    section("[4b] ROI 与受保护入口语义不变")
    check(window._roi_manager is not None and window._roi_manager.has_roi,
          "ROI 未被切档清除")
    window._stop_ocr()
    check(window._ocr_running is False, "OCR 仍可正常停止")
    window.close()


def test_expired_still_blocked():
    section("[5] 许可证语义不变：未激活仍然拦住 OCR 启动")
    window = build_window()
    window._apply_license_result(
        LicenseResult(LicenseStatus.NOT_ACTIVATED, "尚未激活许可证")
    )
    simulate_connected(window)

    _StubOCRWorker.instances.clear()
    select_game(window, VALORANT.profile_id)
    check(window._ocr_profile_id == VALORANT.profile_id,
          "未激活时选择器仍可用（它只是偏好设置，不是受保护功能）")

    window._start_ocr()
    check(len(_StubOCRWorker.instances) == 0,
          "未激活：OCR 仍然被拒绝启动（受保护入口不变）")
    check("无法启动 OCR 识别" in window.statusBar().currentMessage(),
          "未激活：仍给出既有的拒绝原因")
    window.close()


# ======================================================================
# 4. 引擎仍然只在工作线程构造
# ======================================================================
def test_no_engine_on_gui_thread():
    section("[6] 构造/切档都不会在 GUI 线程构造 OCREngine")
    roi_manager = ROIManager()
    roi_manager.set_roi(ROI(0, 0, 100, 50, 100, 50))

    _EngineRecorder.instances.clear()
    _EngineRecorder.threads.clear()

    with _patch_engine():
        worker = worker_mod.OCRWorker(
            frame_buffer_getter=lambda: None,
            roi_manager=roi_manager,
            interval_ms=300,
            profile=DELTA_FORCE.profile_id,
        )
        check(worker._engine is None and worker._recognizer is None,
              "构造 worker 既没有引擎也没有识别器")

        # GUI 线程上切换档位：识别器还没创建，只记档位
        started = time.perf_counter()
        worker.set_profile(VALORANT.profile_id)
        elapsed = time.perf_counter() - started
        check(elapsed < 0.5, f"切档立即返回 (实测 {elapsed * 1000:.0f}ms)")
        check(worker._engine is None and worker._recognizer is None,
              "切档没有顺带构造引擎/识别器")
        check(worker._profile == VALORANT.profile_id, "档位已记录，供后续构造使用")
        check(len(_EngineRecorder.instances) == 0,
              "OCR 启动路径仍然没有构造 OCREngine")

    section("[6b] 引擎已存在时，切档只换识别规则")
    engine = _EngineRecorder()
    # 本组只校验「换档切的是格式规则，不改引擎/线程」，
    # 因此显式用单帧直通裁决器，让一次调用就能看到格式判定结果；
    # 时序一致性由 V2.3 的 TemporalResolver 单独覆盖（8.3）。
    worker2 = worker_mod.OCRWorker(
        frame_buffer_getter=lambda: None,
        roi_manager=roi_manager,
        engine=engine,
        profile=DELTA_FORCE.profile_id,
        resolver=SingleFrameResolver(),
    )
    worker2._recognizer = RoomCodeRecognizer(engine, profile=DELTA_FORCE.profile_id)
    check(worker2._recognizer.profile.profile_id == DELTA_FORCE.profile_id,
          "初始识别器为 Delta Force")

    worker2.set_profile(VALORANT.profile_id)
    check(worker2._recognizer.profile.profile_id == VALORANT.profile_id,
          "切档后识别器换成无畏契约（引擎未重建）")
    check(worker2._engine is engine, "引擎实例未被替换")
    check(len(_EngineRecorder.instances) == 1, "没有构造新的 OCREngine")

    # 识别行为随档位改变：6 位码只在无畏契约档下被接受
    check(worker2._recognizer.recognize_text("ABC123").code == "ABC123",
          "无畏契约档认得 6 位码")
    worker2.set_profile(DELTA_FORCE.profile_id)
    check(worker2._recognizer.recognize_text("ABC123") is None,
          "三角洲档不再接受 6 位码（显式档位消除了长度歧义）")
    check(worker2._recognizer.recognize_text("ABC1234").code == "ABC1234",
          "三角洲档认得 7 位码")


class _patch_engine:
    """临时把 worker 模块里的 OCREngine 换成记录器。"""

    def __enter__(self):
        self._saved = worker_mod.OCREngine
        worker_mod.OCREngine = _EngineRecorder
        return self

    def __exit__(self, *exc):
        worker_mod.OCREngine = self._saved
        return False


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])
    install_stubs()

    print("=" * 70, flush=True)
    print("Phase 8.1: OCR game-profile selector suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print("=" * 70, flush=True)

    test_selector_options()
    test_profile_reaches_worker()
    test_live_switch_reaches_running_worker()
    test_switch_does_not_touch_video_or_restart()
    test_expired_still_blocked()
    test_no_engine_on_gui_thread()

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
