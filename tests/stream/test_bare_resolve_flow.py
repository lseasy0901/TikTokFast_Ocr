# -*- coding: utf-8 -*-
"""RELEASE-02-B5: 裸流主路径的会话流程测试（GUI 线程不阻塞 / 取消 / 会话隔离）。

覆盖任务书 Phase 3.A + 3.B 与 Phase 4「回退与生命周期」的关键断言：

  1. 抖音 + 运行时可用 + 取到裸流 → reader 用裸流 URL + 解析出的 exe_path
     + fallback=(原候选, None)；会话内只有 1 个 reader（单管线）。
  2. 抖音 + 运行时可用 + 取不到裸流 → 走标准路径（无 exe_path），不打扰用户。
  3. 抖音 + 运行时文件完全缺失 → 立即（同步）走标准路径 + 用户可见提示。
  4. 抖音 + 运行时探测失败 → 后台 worker 判定 → 提示 + 标准路径。
  5. 非抖音（虎牙 / B 站）→ 不请求页面、不起 worker、立即走标准路径。
  6. 显式关闭开关（DLV_BARE_STREAM=0）→ 同上。
  7. **页面直取不在 GUI 线程执行**（Phase 3.A 核心）。
  8. 断开后到达的迟到结果被丢弃（不启动 reader、不崩溃）。
  9. 新一轮会话的结果不被旧一轮覆盖（req_id 隔离）。
 10. 窗口关闭后到达的结果被丢弃（_closing 守卫）。

全程离线：FFmpegReader / _FrameBridge / 运行时探测 / 页面直取 全部被替换。
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pytest
from PySide6.QtWidgets import QApplication

import gui.main_window as mw
from utils import ffmpeg7_runtime as RUNTIME
from stream.models import StreamCandidate, StreamInfo

app = QApplication.instance() or QApplication(sys.argv)

BARE = "https://pull-flv-l1.douyincdn.com/stage/stream-111.flv?unique_id=stream-111&t_id=x"
UHD = "https://pull-flv-l1.douyincdn.com/stage/stream-111_uhd.flv?k=1"
FAKE_EXE = r"D:\fake\runtime\ffmpeg7\ffmpeg.exe"

CREATED = []


class FakeReader:
    """记录构造参数的 reader 替身（必须接住 exe_path / fallback）。"""

    def __init__(self, url, width=None, height=None, use_cuda=True,
                 http_headers=None, timing=None,
                 audio_enabled=False, audio_pcm_callback=None,
                 audio_sample_rate=None, audio_channels=None,
                 protocol=None, exe_path=None, fallback=None):
        self.url = url
        self.exe_path = exe_path
        self.fallback = fallback
        self.protocol = protocol
        self.startup_timing = timing
        self.width = width or 1280
        self.height = height or 720
        self.connected = False
        self.is_running = False
        self.error = None
        self.frames_read = 0
        self.first_frame_ms = None
        self.frames_skipped = 0
        self._frame_id = 0
        self._frame = None
        CREATED.append(self)

    def start(self):
        self.is_running = True
        self.connected = True
        self.first_frame_ms = 10.0
        self.frames_read = 3
        self._frame_id = 3

    def stop(self):
        self.is_running = False
        self.connected = False

    @property
    def frame_id(self):
        return self._frame_id

    def read_frame(self):
        return self._frame

    def pipe_backlog_stats(self):
        return {"samples": 0, "last": 0, "max": 0, "p50": 0, "p95": 0}


class RecordingBridge(mw._FrameBridge):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)


_real_reader = mw.FFmpegReader
_real_bridge = mw._FrameBridge


def pump(seconds=1.5, until=None):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if until is not None and until():
            app.processEvents()
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(until and until())


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    """替换 reader/bridge，并默认把运行时探测设为"不可用"，逐用例再覆盖。"""
    del CREATED[:]
    monkeypatch.setattr(mw, "FFmpegReader", FakeReader)
    monkeypatch.setattr(mw, "_FrameBridge", RecordingBridge)
    monkeypatch.delenv("DLV_BARE_STREAM", raising=False)
    # 启动探测线程：给一个确定的不可用结论，避免真实拉起进程
    monkeypatch.setattr(RUNTIME, "status",
                        lambda refresh=False: RUNTIME.Ffmpeg7Status(
                            False, RUNTIME.REASON_NOT_FOUND, detail="test"))
    monkeypatch.setattr(RUNTIME, "find_existing_candidates", lambda: [])
    monkeypatch.setattr(RUNTIME, "resolved_exe", lambda: None)
    yield
    mw.FFmpegReader = _real_reader
    mw._FrameBridge = _real_bridge


def _build_window():
    w = mw.MainWindow()
    w.show()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.003)
    return w


def _info(urls, platform="douyin", room="111"):
    cands = [StreamCandidate(url=u, protocol="flv",
                             http_headers={"User-Agent": "UA"}) for u in urls]
    return StreamInfo(platform=platform, room_id=room, candidates=cands,
                      selected=cands[0] if cands else None,
                      width=None, height=None)


def _connect(window, urls, platform="douyin", room="111"):
    window._handle_connect_success(_info(urls, platform, room))
    window._frame_timer.stop()


def _runtime_ok(monkeypatch):
    monkeypatch.setattr(RUNTIME, "status",
                        lambda refresh=False: RUNTIME.Ffmpeg7Status(
                            True, RUNTIME.REASON_OK, path=FAKE_EXE,
                            version="N-test", avcodec_major=63, gpl=True))
    monkeypatch.setattr(RUNTIME, "find_existing_candidates",
                        lambda: [FAKE_EXE])


# ======================================================================
# 1. 抖音 + 运行时可用 + 取到裸流 → 裸流主路径
# ======================================================================
def test_douyin_bare_success_uses_bare_url_and_runtime(monkeypatch):
    _runtime_ok(monkeypatch)
    monkeypatch.setattr(mw, "video_bare_url_for", lambda p, r: BARE)
    w = _build_window()
    try:
        _connect(w, [UHD])
        assert pump(2.0, lambda: len(CREATED) >= 1), "应创建 reader"
        r = CREATED[0]
        assert r.url == BARE, "默认走裸流主路径"
        assert r.exe_path == FAKE_EXE, "使用解析出的外部运行时"
        assert r.fallback == (UHD, None), "回退到原候选 + 默认运行时"
        assert len(CREATED) == 1, "单管线：只创建一个 reader"
        assert w._bare_worker is None, "worker 完成后应释放引用"
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 2. 抖音 + 运行时可用 + 取不到裸流 → 标准路径（不提示）
# ======================================================================
def test_douyin_bare_not_found_falls_back_silently(monkeypatch):
    _runtime_ok(monkeypatch)
    monkeypatch.setattr(mw, "video_bare_url_for", lambda p, r: None)
    w = _build_window()
    try:
        _connect(w, [UHD])
        assert pump(2.0, lambda: len(CREATED) >= 1)
        r = CREATED[0]
        assert r.url == UHD and r.exe_path is None and r.fallback is None
        assert w._bare_url is None
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 3. 抖音 + 运行时文件完全缺失 → 同步回退 + 用户可见提示
# ======================================================================
def test_douyin_runtime_missing_immediate_fallback_with_notice(monkeypatch):
    fetched = []
    monkeypatch.setattr(mw, "video_bare_url_for",
                        lambda p, r: fetched.append(1) or BARE)
    w = _build_window()
    try:
        _connect(w, [UHD])
        # 同步路径：不需要 pump 就应已创建 reader
        assert len(CREATED) == 1, "运行时缺失时应立即启动标准路径"
        r = CREATED[0]
        assert r.url == UHD and r.exe_path is None
        assert fetched == [], "运行时缺失时不得请求页面"
        assert w._bare_worker is None
        msg = w.statusBar().currentMessage()
        assert "低延迟播放组件" in msg, f"应有用户可见提示，实际={msg!r}"
        # 提示文案不得泄漏技术细节
        for leak in ("DLV_", "ffmpeg.exe", "\\", "/", "Traceback"):
            assert leak not in msg
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 4. 抖音 + 运行时探测失败（后台判定）→ 提示 + 标准路径
# ======================================================================
def test_douyin_runtime_invalid_worker_reports_and_falls_back(monkeypatch):
    monkeypatch.setattr(RUNTIME, "find_existing_candidates", lambda: [FAKE_EXE])
    monkeypatch.setattr(RUNTIME, "status",
                        lambda refresh=False: RUNTIME.Ffmpeg7Status(
                            False, RUNTIME.REASON_VERSION_UNREADABLE,
                            path=FAKE_EXE, detail="banner stripped"))
    called = []
    monkeypatch.setattr(mw, "video_bare_url_for",
                        lambda p, r: called.append(1) or BARE)
    w = _build_window()
    try:
        _connect(w, [UHD])
        assert pump(2.0, lambda: len(CREATED) >= 1)
        r = CREATED[0]
        assert r.url == UHD and r.exe_path is None, "运行时无效 → 标准路径"
        assert called == [], "运行时无效时不应再请求页面"
        assert "低延迟播放组件" in w.statusBar().currentMessage()
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 5/6. 非抖音 / 显式关闭 → 不请求页面、不起 worker
# ======================================================================
@pytest.mark.parametrize("platform,env", [
    ("huya", None),
    ("bilibili", None),
    ("douyin", "0"),
])
def test_not_applicable_goes_straight_to_standard(monkeypatch, platform, env):
    if env is not None:
        monkeypatch.setenv("DLV_BARE_STREAM", env)

    def _boom(*a, **kw):
        raise AssertionError("不该请求页面")
    monkeypatch.setattr(mw, "video_bare_url_for", _boom)
    w = _build_window()
    try:
        _connect(w, [UHD], platform=platform)
        assert len(CREATED) == 1, "应同步立即启动标准路径"
        assert CREATED[0].url == UHD and CREATED[0].exe_path is None
        assert w._bare_worker is None
        assert w.statusBar().currentMessage() != ""
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 7. 页面直取不得在 GUI 线程执行
# ======================================================================
def test_page_fetch_runs_off_gui_thread(monkeypatch):
    _runtime_ok(monkeypatch)
    seen = {}

    def _record(p, r):
        seen["thread"] = threading.current_thread().name
        seen["is_main"] = threading.current_thread() is threading.main_thread()
        return BARE
    monkeypatch.setattr(mw, "video_bare_url_for", _record)

    w = _build_window()
    try:
        _connect(w, [UHD])
        assert pump(2.0, lambda: "thread" in seen), "worker 应执行页面直取"
        assert seen["is_main"] is False, \
            f"页面直取不得在 GUI 主线程执行（实际={seen['thread']}）"
        assert seen["thread"] == "bare-resolve-worker"
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 8. 断开后到达的迟到结果被丢弃
# ======================================================================
def test_late_result_after_disconnect_is_discarded(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()

    def _slow(p, r):
        gate.wait(timeout=5.0)
        return BARE
    monkeypatch.setattr(mw, "video_bare_url_for", _slow)

    w = _build_window()
    try:
        _connect(w, [UHD])
        assert w._bare_worker is not None, "应已启动 worker"
        w._disconnect()
        n_after_disconnect = len(CREATED)
        gate.set()                       # 放行迟到结果
        pump(1.5)
        assert len(CREATED) == n_after_disconnect, \
            "断开后不得再启动任何 reader"
        assert w._bare_url is None
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 9. 新一轮会话不被旧结果覆盖
# ======================================================================
def test_stale_result_does_not_override_new_session(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()
    BARE_NEW = "https://pull-flv-l1.douyincdn.com/stage/stream-222.flv" \
               "?unique_id=stream-222&t_id=y"

    def _fetch(p, r):
        if r == "111":
            gate.wait(timeout=5.0)
            return BARE
        return BARE_NEW
    monkeypatch.setattr(mw, "video_bare_url_for", _fetch)

    w = _build_window()
    try:
        _connect(w, [UHD], room="111")
        w._disconnect()
        # 第二轮会话（新房间）：worker 立即返回
        _connect(w, [UHD], room="222")
        assert pump(2.0, lambda: len(CREATED) >= 1)
        r_new = CREATED[-1]
        assert r_new.url == BARE_NEW, "新会话必须用自己的裸流结果"
        # 放行第一轮的迟到结果
        gate.set()
        pump(1.0)
        assert r_new.url == BARE_NEW, "旧结果不得覆盖新会话"
        assert w._bare_url == BARE_NEW
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 10. 窗口关闭后到达的结果被丢弃
# ======================================================================
def test_result_after_close_is_discarded(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()
    monkeypatch.setattr(mw, "video_bare_url_for",
                        lambda p, r: (gate.wait(timeout=5.0), BARE)[1])

    w = _build_window()
    _connect(w, [UHD])
    assert w._bare_worker is not None
    w.close()                             # 触发 closeEvent → _closing = True
    n = len(CREATED)
    gate.set()
    pump(1.5)                             # 不得崩溃、不得启动 reader
    assert w._closing is True
    assert len(CREATED) == n


# ======================================================================
# 11. 每次会话最多一次回退（会话级裸流失败抑制）
# ======================================================================
def test_bare_failure_marks_session_and_stops_retrying(monkeypatch):
    """reader 报 fallback_used 后，后续候选不得再尝试裸流。"""
    _runtime_ok(monkeypatch)
    monkeypatch.setattr(mw, "video_bare_url_for", lambda p, r: BARE)
    w = _build_window()
    try:
        _connect(w, [UHD, "https://x/stream-111_hd.flv"])
        assert pump(2.0, lambda: len(CREATED) >= 1)
        first = CREATED[0]
        assert first.url == BARE
        # 模拟：裸流失败并已回退（fallback_used=True），且从未出帧
        first.frames_read = 0
        first.first_frame_ms = None
        first.error = "连接失败"
        first.is_running = False
        monkeypatch.setattr(type(first), "fallback_used",
                            property(lambda self: True), raising=False)
        w._frame_timer.stop()
        w._on_frame_timeout()
        w._frame_timer.stop()
        assert w._bare_url is None, "本会话裸流应被判定不可用"
        assert len(CREATED) >= 2, "应继续尝试下一个候选"
        assert CREATED[-1].url != BARE, "后续候选不得再走裸流"
    finally:
        w._disconnect()
        w.close()
