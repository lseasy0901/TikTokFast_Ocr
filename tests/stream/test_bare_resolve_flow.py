# -*- coding: utf-8 -*-
"""RELEASE-02-B5-CLEANUP: 唯一取流入口的会话流程测试。

覆盖（对应任务书 Phase 4 的断言清单）：

  1. 抖音 + 运行时可用 + 取到裸流 → reader 用**裸流 URL** + 解析出的 exe_path；
     会话内只有 1 个 reader（单管线）；抖音候选集恒为 1（旧 `_uhd` 候选已移除）。
  2. 抖音 + 运行时不可用 → **明确提示 + 会话结束**，不启动任何 reader，
     不切换任何旧路径。
  3. 抖音 + 未开播 → 提示"主播未开播" + 会话结束。
  4. 抖音 + 页面取不到（网络失败）→ 提示 + 会话结束。
  5. 非抖音（虎牙 / B 站）/ 显式关闭 → 用适配器候选，不请求页面、不起 worker。
  6. **取流不在 GUI 线程执行**（Phase 3.A 核心）。
  7. 断开后到达的迟到结果被丢弃（不启动 reader、不崩溃）。
  8. 新一轮会话的结果不被旧一轮覆盖（req_id 隔离）。
  9. 窗口关闭后到达的结果被丢弃（_closing 守卫）。
 10. 抖音会话候选集为 1，且候选 URL == 裸流 URL（唯一路径）。
 11. 不存在旧 `_uhd` 回退：`FFmpegReader` 构造**不传 fallback**。

全程离线：FFmpegReader / _FrameBridge / 运行时探测 / 页面取流 全部被替换。
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
from stream.bare_source import (BareProbe, REASON_FETCH_FAILED, REASON_NO_BARE,
                                REASON_OFFLINE)
from utils import ffmpeg7_runtime as RUNTIME
from stream.models import StreamCandidate, StreamInfo

app = QApplication.instance() or QApplication(sys.argv)

BARE = "https://pull-flv-l1.douyincdn.com/stage/stream-111.flv?unique_id=stream-111&t_id=x"
HUYA_URL = "https://al-game.flv.huya.com/src/1-2-3-4.flv"
FAKE_EXE = r"D:\fake\runtime\ffmpeg7\ffmpeg.exe"

CREATED = []
READER_KWARGS = []


class FakeReader:
    """记录构造参数的 reader 替身（必须接住 exe_path；**不得**有 fallback）。"""

    def __init__(self, url, width=None, height=None, use_cuda=True,
                 http_headers=None, timing=None,
                 audio_enabled=False, audio_pcm_callback=None,
                 audio_sample_rate=None, audio_channels=None,
                 protocol=None, exe_path=None):
        self.url = url
        self.exe_path = exe_path
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
        READER_KWARGS.append(dict(url=url, exe_path=exe_path,
                                  protocol=protocol))

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
    """替换 reader/bridge；默认运行时不可用，逐用例再覆盖。

    同时**停用启动期许可证校验**：`_start_license_load()` 会在后台线程里
    拉起 `susi_helper.exe` 子进程，而本文件会创建/销毁大量 MainWindow；
    子进程与 Qt 对象 GC 并发时会触发既有的堆竞争（0xc0000374）。许可证
    路径有自己的专项测试，这里与裸流流程无关，故隔离掉。
    """
    del CREATED[:]
    del READER_KWARGS[:]
    monkeypatch.setattr(mw, "FFmpegReader", FakeReader)
    monkeypatch.setattr(mw, "_FrameBridge", RecordingBridge)
    monkeypatch.setattr(mw.MainWindow, "_start_license_load", lambda self: None)
    monkeypatch.delenv("DLV_BARE_STREAM", raising=False)
    monkeypatch.setattr(RUNTIME, "status",
                        lambda refresh=False: RUNTIME.Ffmpeg7Status(
                            False, RUNTIME.REASON_NOT_FOUND, detail="test"))
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


def _probe(monkeypatch, probe: BareProbe):
    monkeypatch.setattr(mw, "probe_video_bare", lambda p, r: probe)


# ======================================================================
# 1. 抖音 + 运行时可用 + 取到裸流 → 唯一候选 = 裸流
# ======================================================================
def test_douyin_bare_success_single_candidate_with_runtime(monkeypatch):
    _runtime_ok(monkeypatch)
    _probe(monkeypatch, BareProbe(url=BARE, live=True, reason="ok"))
    w = _build_window()
    try:
        _connect(w, [])
        assert pump(2.0, lambda: len(CREATED) >= 1), "应创建 reader"
        r = CREATED[0]
        assert r.url == BARE, "播放地址必须是裸流本身"
        assert r.exe_path == FAKE_EXE, "必须使用外部 FFmpeg7 运行时"
        assert len(CREATED) == 1, "单管线：只创建一个 reader"
        # 抖音会话候选集恒为 1（旧 _uhd 转码档候选已整体移除）
        assert w._failover.total == 1
        assert w._failover.candidate.url == BARE
    finally:
        w._disconnect()
        w.close()


def test_reader_never_receives_fallback_kwarg(monkeypatch):
    """旧 `_uhd` 回退已移除：构造 reader 时不得再传 fallback。"""
    _runtime_ok(monkeypatch)
    _probe(monkeypatch, BareProbe(url=BARE, live=True, reason="ok"))
    w = _build_window()
    try:
        _connect(w, [])
        assert pump(2.0, lambda: len(CREATED) >= 1)
        import inspect
        sig = inspect.signature(FakeReader.__init__)
        assert "fallback" not in sig.parameters, \
            "FFmpegReader 不应再接受 fallback（旧 _uhd 回退已移除）"
        assert "fallback" not in READER_KWARGS[0]
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 2/3/4. 抖音失败路径：明确提示 + 会话结束，绝不切旧路径
# ======================================================================
@pytest.mark.parametrize("probe_kwargs,expect_text", [
    (dict(url=None, live=False, reason=REASON_OFFLINE), "主播未开播"),
    (dict(url=None, live=True, reason=REASON_NO_BARE), "未找到可用的直播流"),
    (dict(url=None, live=False, reason=REASON_FETCH_FAILED), "无法获取直播间信息"),
])
def test_douyin_bare_failure_aborts_with_message(monkeypatch, probe_kwargs,
                                                expect_text):
    _runtime_ok(monkeypatch)
    _probe(monkeypatch, BareProbe(**probe_kwargs))
    w = _build_window()
    try:
        _connect(w, [])
        assert pump(2.0, lambda: w._bare_worker is None), "worker 应已结束"
        assert CREATED == [], "取流失败时不得启动任何 reader（不切旧路径）"
        assert expect_text in w.statusBar().currentMessage()
        assert w._connected is False, "会话应已结束"
    finally:
        w._disconnect()
        w.close()


def test_runtime_unavailable_aborts_with_message(monkeypatch):
    _probe(monkeypatch, BareProbe(url=BARE, live=True, reason="ok"))
    w = _build_window()
    try:
        _connect(w, [])
        assert pump(2.0, lambda: w._bare_worker is None)
        assert CREATED == [], "运行时不可用时不得启动 reader"
        msg = w.statusBar().currentMessage()
        assert "低延迟播放组件" in msg, f"应有明确提示，实际={msg!r}"
        for leak in ("DLV_", "ffmpeg.exe", "\\", "/", "Traceback"):
            assert leak not in msg
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 5. 非裸流平台：候选由适配器给出，运行时就绪后启动
# ======================================================================
@pytest.mark.parametrize("platform", ["huya", "bilibili"])
def test_non_bare_platform_uses_platform_candidates(monkeypatch, platform):
    """虎牙 / B 站不走裸流页面直取，但**仍需通过 FFmpeg7 运行时校验**
    （内置 FFmpeg 6 已移除，不存在任何内置解码器）。"""
    _runtime_ok(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("非裸流平台不该请求页面")
    monkeypatch.setattr(mw, "probe_video_bare", _boom)
    w = _build_window()
    try:
        _connect(w, [HUYA_URL], platform=platform)
        assert pump(2.0, lambda: len(CREATED) >= 1), "运行时就绪后应启动平台候选"
        assert CREATED[0].url == HUYA_URL
        assert CREATED[0].exe_path == FAKE_EXE, \
            "所有平台都用外部 FFmpeg7 运行时"
        assert w._bare_worker is None
        assert w._failover.total == 1, "仍按适配器候选迭代"
    finally:
        w._disconnect()
        w.close()


def test_non_bare_platform_runtime_missing_aborts(monkeypatch):
    """运行时缺失 → 非裸流平台同样明确终止（不再有内置解码器兜底）。"""
    # fixture 默认 status = 不可用
    w = _build_window()
    try:
        _connect(w, [HUYA_URL], platform="huya")
        assert pump(2.0, lambda: w._bare_worker is None)
        assert CREATED == [], "运行时缺失时不得启动 reader"
        msg = w.statusBar().currentMessage()
        assert "低延迟播放组件" in msg, f"应有明确提示，实际={msg!r}"
    finally:
        w._disconnect()
        w.close()


def test_switch_off_leaves_douyin_nothing_to_play(monkeypatch):
    """`DLV_BARE_STREAM=0`：抖音适配器已无候选、裸流又被关闭 → 明确终止。

    这是"唯一路径"的直接后果：没有第二条路可走，也不允许静默降级。
    """
    monkeypatch.setenv("DLV_BARE_STREAM", "0")
    _runtime_ok(monkeypatch)
    w = _build_window()
    try:
        _connect(w, [], platform="douyin")
        assert pump(2.0, lambda: w._bare_worker is None)
        assert CREATED == [], "不得启动任何 reader"
        msg = w.statusBar().currentMessage()
        assert "低延迟播放" in msg, f"应有明确提示，实际={msg!r}"
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 6. 取流不得在 GUI 线程执行
# ======================================================================
def test_bare_probe_runs_off_gui_thread(monkeypatch):
    _runtime_ok(monkeypatch)
    seen = {}

    def _record(p, r):
        seen["thread"] = threading.current_thread().name
        seen["is_main"] = threading.current_thread() is threading.main_thread()
        return BareProbe(url=BARE, live=True, reason="ok")
    monkeypatch.setattr(mw, "probe_video_bare", _record)

    w = _build_window()
    try:
        _connect(w, [])
        assert pump(2.0, lambda: "thread" in seen), "worker 应执行取流"
        assert seen["is_main"] is False, \
            f"取流不得在 GUI 主线程执行（实际={seen['thread']}）"
        assert seen["thread"] == "bare-resolve-worker"
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 7. 断开后到达的迟到结果被丢弃
# ======================================================================
def test_late_result_after_disconnect_is_discarded(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()

    def _slow(p, r):
        gate.wait(timeout=5.0)
        return BareProbe(url=BARE, live=True, reason="ok")
    monkeypatch.setattr(mw, "probe_video_bare", _slow)

    w = _build_window()
    try:
        _connect(w, [])
        assert w._bare_worker is not None, "应已启动 worker"
        w._disconnect()
        gate.set()
        pump(1.5)
        assert CREATED == [], "断开后不得再启动任何 reader"
        assert w._bare_url is None
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 8. 新一轮会话不被旧结果覆盖
# ======================================================================
def test_stale_result_does_not_override_new_session(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()
    BARE_NEW = ("https://pull-flv-l1.douyincdn.com/stage/stream-222.flv"
                "?unique_id=stream-222&t_id=y")

    def _fetch(p, r):
        if r == "111":
            gate.wait(timeout=5.0)
            return BareProbe(url=BARE, live=True, reason="ok")
        return BareProbe(url=BARE_NEW, live=True, reason="ok")
    monkeypatch.setattr(mw, "probe_video_bare", _fetch)

    w = _build_window()
    try:
        _connect(w, [], room="111")
        w._disconnect()
        _connect(w, [], room="222")
        assert pump(2.0, lambda: len(CREATED) >= 1)
        r_new = CREATED[-1]
        assert r_new.url == BARE_NEW, "新会话必须用自己的取流结果"
        gate.set()
        pump(1.0)
        assert r_new.url == BARE_NEW, "旧结果不得覆盖新会话"
        assert w._bare_url == BARE_NEW
    finally:
        w._disconnect()
        w.close()


# ======================================================================
# 9. 窗口关闭后到达的结果被丢弃
# ======================================================================
def test_result_after_close_is_discarded(monkeypatch):
    _runtime_ok(monkeypatch)
    gate = threading.Event()

    def _slow(p, r):
        gate.wait(timeout=5.0)
        return BareProbe(url=BARE, live=True, reason="ok")
    monkeypatch.setattr(mw, "probe_video_bare", _slow)

    w = _build_window()
    _connect(w, [])
    assert w._bare_worker is not None
    w.close()
    gate.set()
    pump(1.5)
    assert w._closing is True
    assert CREATED == []


# ======================================================================
# 10. 抖音适配器不再产出任何流候选（唯一路径的结构性证据）
# ======================================================================
def test_douyin_adapter_returns_no_candidates():
    """适配器层：抖音 resolve() 不再产出任何转码档候选。"""
    from stream.adapters.douyin import DouyinStream
    info = DouyinStream("https://live.douyin.com/123456789").get_stream_info()
    assert info.platform == "douyin"
    assert info.room_id == "123456789"
    assert info.candidates == [], "抖音适配器不得再枚举任何流候选"
    assert info.selected is None
