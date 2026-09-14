#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7.2-6.6: 过期访问控制回归测试

目标：验证「许可证过期」不只是改变顶部状态文案，而是真正阻止受保护功能。

覆盖的受保护入口：
    - 新建直播流连接  (MainWindow._connect)
    - 选择识别区域    (MainWindow._on_roi_select_clicked)
    - 启动 OCR 识别   (MainWindow._start_ocr)

并验证：
    - 到期时正在运行的 OCR 会被停止
    - 到期【不会】强制断开已经建立的视频流
    - 应用保持打开时也能检测到到期（专用低频定时器）

隔离说明：
    测试在构造 MainWindow 之前把 APPDATA 指向临时目录，
    因此既不会读取也不会改写用户真实的 license.json。
    连接用的 _stream_history 也被替换为桩，避免写 stream_history.json。

策略变更（本次修订）：
    7 天免费试用已移除 —— 本测试第 [9] 节断言「未激活 = 拒绝」，
    取代原先「NOT_ACTIVATED 仍按试用期放行」的断言。
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 必须在导入/构造 LicenseStore 之前设置（default_data_dir 在构造时读取该变量）
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-access-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui.main_window as mw  # noqa: E402
from utils.license_manager import LicenseResult, LicenseStatus  # noqa: E402

# ======================================================================
# 测试脚手架
# ======================================================================
_PASSED = 0
_FAILED = 0


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
# 桩：替换网络/子进程/GUI 交互，只观察「入口是否被放行」
# ----------------------------------------------------------------------
class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _StubConnectWorker:
    instances = []

    def __init__(self, live_url, on_success, on_error):
        self.live_url = live_url
        _StubConnectWorker.instances.append(self)

    def start(self):
        pass

    def is_running(self):
        return False


class _StubROISelector:
    instances = []

    def __init__(self, frame, parent=None):
        self.roi_selected = _Sig()
        self.roi_selection_canceled = _Sig()
        _StubROISelector.instances.append(self)

    def show(self):
        pass


class _StubOCRWorker:
    instances = []

    def __init__(self, frame_buffer_getter=None, roi_manager=None, interval_ms=300,
                 profile=None, scale=1.0, recognizer=None):
        self.interval_ms = interval_ms
        self.profile = profile
        self.started_flag = False
        self.stopped = False
        self.text_updated = _Sig()
        self.copy_requested = _Sig()
        self.error_occurred = _Sig()
        self.game_mismatch = _Sig()   # Phase 8.6：选错游戏提醒（无参数）
        self.started = _Sig()
        self.finished = _Sig()
        _StubOCRWorker.instances.append(self)

    def start_recognition(self):
        self.started_flag = True

    def stop_recognition(self):
        self.stopped = True

    def set_profile(self, profile):
        self.profile = profile


class _StubExpiredDialog:
    instances = []

    def __init__(self, parent=None):
        _StubExpiredDialog.instances.append(self)

    def exec(self):
        pass


class _StubROIManager:
    has_roi = True


class _StubHistory:
    """替代 StreamHistory，避免测试写入 stream_history.json。"""

    def __init__(self):
        self.added = []

    def add_or_update(self, url, nickname=""):
        self.added.append(url)

    def get_all(self):
        return []


class _FakeFrame:
    shape = (720, 1280, 3)


class _FakeReader:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeBuffer:
    def __init__(self):
        self.cleared = False

    def get(self):
        return _FakeFrame()

    def clear(self):
        self.cleared = True


class _FakeBridge:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def install_stubs() -> None:
    mw._ConnectWorker = _StubConnectWorker
    mw.ROISelector = _StubROISelector
    mw.OCRWorker = _StubOCRWorker
    mw.ExpiredDialog = _StubExpiredDialog


def reset_recorders() -> None:
    _StubConnectWorker.instances.clear()
    _StubROISelector.instances.clear()
    _StubOCRWorker.instances.clear()
    _StubExpiredDialog.instances.clear()


# ======================================================================
# 状态辅助
# ======================================================================
def build_window():
    """构造真实 MainWindow 并等待启动时的许可证后台校验完成。"""
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
    return window


def set_active(window, days: int = 30) -> None:
    """注入一份「已激活」状态，与原启动恢复走同一条映射路径。"""
    window._apply_license_result(
        LicenseResult(
            LicenseStatus.ACTIVATED,
            "已激活",
            expires_at=datetime.now(timezone.utc) + timedelta(days=days),
        )
    )


def set_expired(window) -> None:
    """注入一份「已过期」状态。"""
    window._apply_license_result(
        LicenseResult(LicenseStatus.EXPIRED, "许可证已过期")
    )


def simulate_connected(window) -> None:
    """模拟「已连接直播流」的运行时状态（不启动真实 FFmpeg）。"""
    window._connected = True
    window._frame_buffer = _FakeBuffer()
    window._roi_manager = _StubROIManager()
    window._connect_btn.setText("断开")


# ======================================================================
# 1. ACTIVE：受保护功能应当放行
# ======================================================================
def test_active_allows_protected_actions():
    section("[1] ACTIVE license: protected actions allowed")
    window = build_window()
    set_active(window)

    check(not window._access_status.is_expired(),
          "ACTIVE: predicate reports not expired")

    reset_recorders()
    window._url_input.setText("https://live.douyin.com/123456")
    window._on_connect_clicked()
    check(len(_StubConnectWorker.instances) == 1,
          "ACTIVE: connect proceeds (worker constructed)")
    check(window._connect_btn.isEnabled() is False,
          "ACTIVE: connect button reflects in-flight connection, not a block")

    window2 = build_window()
    set_active(window2)
    simulate_connected(window2)
    reset_recorders()
    window2._on_roi_select_clicked()
    check(len(_StubROISelector.instances) == 1,
          "ACTIVE: ROI selection proceeds (selector constructed)")

    window3 = build_window()
    set_active(window3)
    simulate_connected(window3)
    reset_recorders()
    window3._start_ocr()
    check(len(_StubOCRWorker.instances) == 1,
          "ACTIVE: OCR starts (worker constructed)")

    section("[1b] ACTIVE license: UI enable state")
    window4 = build_window()
    set_active(window4)
    window4._on_access_status_changed()
    check(window4._connect_btn.isEnabled(), "ACTIVE: connect button enabled")
    check(window4._url_input.isEnabled(), "ACTIVE: URL input enabled")
    check(window4._license_btn.isEnabled(), "ACTIVE: activation button enabled")

    simulate_connected(window4)
    window4._on_access_status_changed()
    check(window4._roi_btn.isEnabled(), "ACTIVE+connected: ROI button enabled")
    check(window4._ocr_btn.isEnabled(), "ACTIVE+connected+ROI: OCR button enabled")


# ======================================================================
# 2. EXPIRED：三个入口必须被阻止
# ======================================================================
def test_expired_blocks_connect():
    section("[2] EXPIRED license: connect blocked")
    window = build_window()
    set_expired(window)

    check(window._access_status.is_expired(),
          "EXPIRED: predicate reports expired")

    reset_recorders()
    window._url_input.setText("https://live.douyin.com/123456")
    window._on_connect_clicked()
    check(len(_StubConnectWorker.instances) == 0,
          "EXPIRED: connect blocked (no worker constructed)")
    check("无法连接直播流" in window.statusBar().currentMessage(),
          "EXPIRED: denial reason shown in status bar")
    check(window._connect_btn.isEnabled() is False,
          "EXPIRED: connect button visibly disabled")
    check(window._url_input.isEnabled() is False,
          "EXPIRED: URL input visibly disabled")


def test_expired_blocks_roi():
    section("[3] EXPIRED license: ROI selection blocked")
    window = build_window()
    set_expired(window)
    simulate_connected(window)
    window._on_access_status_changed()

    reset_recorders()
    window._on_roi_select_clicked()
    check(len(_StubROISelector.instances) == 0,
          "EXPIRED: ROI selection blocked (no selector constructed)")
    check("无法选择识别区域" in window.statusBar().currentMessage(),
          "EXPIRED: ROI denial reason shown")
    check(window._roi_btn.isEnabled() is False,
          "EXPIRED: ROI button visibly disabled while connected")


def test_expired_blocks_ocr():
    section("[4] EXPIRED license: OCR blocked")
    window = build_window()
    set_expired(window)
    simulate_connected(window)
    window._on_access_status_changed()

    reset_recorders()
    window._start_ocr()
    check(len(_StubOCRWorker.instances) == 0,
          "EXPIRED: OCR blocked (no worker constructed)")
    check("无法启动 OCR 识别" in window.statusBar().currentMessage(),
          "EXPIRED: OCR denial reason shown")
    check(window._ocr_btn.isEnabled() is False,
          "EXPIRED: OCR button visibly disabled")


# ======================================================================
# 3. EXPIRED：停止正在运行的 OCR，但不断开视频流
# ======================================================================
def test_expired_stops_running_ocr():
    section("[5] EXPIRED while OCR running: OCR stops")
    window = build_window()
    set_active(window)
    simulate_connected(window)
    reset_recorders()
    window._start_ocr()
    check(len(_StubOCRWorker.instances) == 1, "precondition: OCR worker created")
    worker = _StubOCRWorker.instances[-1]

    # 模拟 OCR 已进入运行态（真实运行时由 started 信号置位）
    window._ocr_running = True
    window._ocr_btn.setText("识别中")

    set_expired(window)

    check(worker.stopped is True,
          "EXPIRED: running OCR worker was stopped")
    check(window._ocr_running is False,
          "EXPIRED: OCR running flag cleared")
    check(window._ocr_btn.isEnabled() is False,
          "EXPIRED: OCR button disabled after stop")


def test_expired_does_not_disconnect():
    section("[6] EXPIRED does NOT forcibly disconnect the stream")
    window = build_window()
    set_active(window)
    simulate_connected(window)
    window._reader = _FakeReader()
    window._frame_buffer = _FakeBuffer()
    window._frame_bridge = _FakeBridge()

    set_expired(window)

    check(window._connected is True,
          "EXPIRED: connection state preserved (still connected)")
    check(window._reader is not None and not window._reader.stopped,
          "EXPIRED: FFmpegReader left running")
    check(window._frame_buffer is not None and not window._frame_buffer.cleared,
          "EXPIRED: frame buffer left intact")
    check(window._frame_bridge is not None and not window._frame_bridge.stopped,
          "EXPIRED: frame bridge left running")
    check(window._connect_btn.isEnabled(),
          "EXPIRED+connected: disconnect entry still available")

    section("[6b] EXPIRED + connected: user can still disconnect")
    window._on_connect_clicked()
    check(window._connected is False,
          "EXPIRED: disconnect still works (user not trapped)")
    check(window._connect_btn.isEnabled() is False,
          "EXPIRED: after disconnect, new connection stays blocked")
    check(window._url_input.isEnabled() is False,
          "EXPIRED: after disconnect, URL input stays blocked")


# ======================================================================
# 4. 应用保持打开时检测到期
# ======================================================================
def test_mid_session_expiry_detected():
    section("[7] Expiration detected while the application stays open")
    window = build_window()

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    window._apply_license_result(
        LicenseResult(LicenseStatus.ACTIVATED, "已激活", expires_at=expires_at)
    )

    check(not window._access_status.is_expired(),
          "precondition: still valid right after activation")
    # 1 秒后到期，落在既有 24 小时预警窗口内，因此顶部显示「即将到期」而非「已激活」。
    # 这里断言的是「仍处于有效状态」，不是某个具体文案。
    label_before = window._access_status_label.text()
    check(("已激活" in label_before or "即将到期" in label_before),
          "precondition: header reflects a still-valid license")
    check("许可证已过期" not in label_before,
          "precondition: header does not yet report expired")
    check(window._connect_btn.isEnabled(),
          "precondition: connect allowed while valid")

    # 不做任何重启、不重新注入许可证 —— 只让真实时间跨过 expires，
    # 再让专用低频定时器回调跑一次。
    time.sleep(1.3)

    check(window._access_status.is_expired(),
          "expiry reached while app remains open (predicate)")

    window._on_access_timer_timeout()

    check("许可证已过期" in window._access_status_label.text(),
          "header updated to expired without restart")
    check(window._connect_btn.isEnabled() is False,
          "connect blocked after mid-session expiry")

    reset_recorders()
    window._url_input.setText("https://live.douyin.com/123456")
    window._on_connect_clicked()
    check(len(_StubConnectWorker.instances) == 0,
          "mid-session expiry actually blocks a new connection")


def test_dedicated_timer():
    section("[8] Dedicated access timer (not the video/status timer)")
    window = build_window()
    reset_recorders()

    check(window._access_timer is not window._status_timer,
          "_access_timer is a different object from _status_timer")
    check(window._access_status._update_timer is window._access_timer,
          "AccessStatus is driven by the dedicated timer")
    check(window._access_timer.isActive(),
          "dedicated access timer is running after startup")
    check(30000 <= window._access_timer.interval() <= 60000,
          f"cadence within 30-60s (actual {window._access_timer.interval()} ms)")
    check(window._status_timer.isActive() is False,
          "video/status timer is NOT started by the access countdown")

    check(len(_StubExpiredDialog.instances) == 0,
          "periodic refresh does not open the expired dialog")
    window._on_access_timer_timeout()
    check(len(_StubExpiredDialog.instances) == 0,
          "periodic refresh still does not open the expired dialog")


# ======================================================================
# 5. NOT_ACTIVATED：无有效授权即拒绝（7 天免费试用已移除）
# ======================================================================
def test_not_activated_is_blocked():
    section("[9] NOT_ACTIVATED: no valid license → blocked (free trial removed)")
    window = build_window()
    window._apply_license_result(
        LicenseResult(LicenseStatus.NOT_ACTIVATED, "尚未激活许可证")
    )

    check(window._access_status.is_expired() is True,
          "NOT_ACTIVATED is treated as expired / blocked")
    check("未激活" in window._access_status.get_status_text(),
          "NOT_ACTIVATED shows the not-activated status text")
    check(window._connect_btn.isEnabled() is False,
          "NOT_ACTIVATED: connect button visibly disabled")
    check(window._url_input.isEnabled() is False,
          "NOT_ACTIVATED: URL input visibly disabled")

    reset_recorders()
    window._url_input.setText("https://live.douyin.com/123456")
    window._on_connect_clicked()
    check(len(_StubConnectWorker.instances) == 0,
          "NOT_ACTIVATED: connect blocked (no worker constructed)")
    check("无法连接直播流" in window.statusBar().currentMessage(),
          "NOT_ACTIVATED: denial reason shown in status bar")

    section("[9b] fresh install (nothing ever activated) is blocked too")
    window2 = build_window()
    check(window2._access_status.is_expired() is True,
          "startup with no stored license → blocked")
    check(window2._connect_btn.isEnabled() is False,
          "startup with no stored license: connect button disabled")

    reset_recorders()
    window2._on_roi_select_clicked()
    check(len(_StubROISelector.instances) == 0,
          "startup with no stored license: ROI selection blocked")
    window2._start_ocr()
    check(len(_StubOCRWorker.instances) == 0,
          "startup with no stored license: OCR blocked")

    window2._on_access_timer_timeout()
    check(window2._access_status.is_expired() is True,
          "periodic refresh keeps the unlicensed verdict")


# ======================================================================
# main
# ======================================================================
def main() -> int:
    global app
    app = QApplication.instance() or QApplication([])
    install_stubs()

    print("=" * 70, flush=True)
    print("Phase 7.2-6.6: expiration / access-control regression suite", flush=True)
    print(f"isolated APPDATA: {os.environ['APPDATA']}", flush=True)
    print("=" * 70, flush=True)

    test_active_allows_protected_actions()
    test_expired_blocks_connect()
    test_expired_blocks_roi()
    test_expired_blocks_ocr()
    test_expired_stops_running_ocr()
    test_expired_does_not_disconnect()
    test_mid_session_expiry_detected()
    test_dedicated_timer()
    test_not_activated_is_blocked()

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
