# -*- coding: utf-8 -*-
"""RELEASE-02-B5-CLEANUP: FFmpegReader 生命周期与有界恢复测试（本地夹具，离线可复现）。

本文件取代原 `test_reader_fallback.py`（旧 `_uhd` 回退路径的测试）。
回退已整体移除，因此这里断言的是**唯一路径 + 有界恢复**的行为：

  R1 `FFmpegReader` 不再接受 `fallback` 参数（结构性断言：旧回退入口不存在）
  R2 坏 URL → 报错结束、不出帧、不 hang、**stream_url 不被改写**
  R3 exe_path 覆盖生效（cmd[0] 使用指定二进制）
  R4 ffmpeg>=7（显式 exe_path）不得注入 -thread_queue_size；默认路径逐字不变
  R5 解码器不支持证据 → decode_failed 置位（用于有界重启，而非切旧路径）
  R6 stop() 释放子进程与线程
  R7 连续多次 start/stop 无残留
  R8 stop() 幂等
  R9 实例上不存在任何回退状态（`_fallback` / `_fallback_used` / `_switch_to_fallback`）
"""
import http.server
import os
import subprocess
import threading
import time

import pytest

#: 本地夹具用的 FFmpeg（优先外部 ffmpeg7；否则用随包运行时）。
FF7 = r"D:\TikTok_Ocr_Rebuild\runtime\ffmpeg7\ffmpeg.exe"
EXE_BUNDLED = r"D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ffmpeg\ffmpeg.exe"
FIX_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE = os.path.join(FIX_DIR, "sample_ok.mp4")


def _gen_exe():
    return FF7 if os.path.exists(FF7) else EXE_BUNDLED


pytestmark = pytest.mark.skipif(not os.path.exists(_gen_exe()),
                                reason="需要可用的 ffmpeg 二进制")


def _make_fixture():
    os.makedirs(FIX_DIR, exist_ok=True)
    if os.path.exists(SAMPLE) and os.path.getsize(SAMPLE) > 10000:
        return
    subprocess.run(
        [_gen_exe(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "30",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         SAMPLE],
        capture_output=True, timeout=180)


@pytest.fixture(scope="module")
def http_server():
    _make_fixture()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=FIX_DIR, **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % port
    srv.shutdown()


def _frames(reader, seconds=20.0, want=5):
    """消费读取线程可用的帧（read_frame_with_seq 轮询）。"""
    got = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds and len(got) < want:
        f, s, t = reader.read_frame_with_seq()
        if f is not None:
            got.append((s, f.shape))
        time.sleep(0.02)
    return got


# ======================================================================
# R1 / R9：旧回退入口不存在（结构性断言）
# ======================================================================
def test_r1_reader_has_no_fallback_parameter():
    import inspect

    from stream.ffmpeg_reader import FFmpegReader
    params = inspect.signature(FFmpegReader.__init__).parameters
    assert "fallback" not in params, \
        "旧 `_uhd` 回退已移除：构造参数不应再有 fallback"


def test_r9_instance_has_no_fallback_state(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                     use_cuda=False)
    for attr in ("_fallback", "_fallback_used"):
        assert not hasattr(r, attr), f"不应再有回退状态字段 {attr}"
    assert not hasattr(r, "_switch_to_fallback"), "不应再有回退实现"
    assert not hasattr(r, "fallback_used"), "不应再有回退状态属性"


# ======================================================================
# R2：坏 URL → 报错结束，不 hang，不改写 stream_url
# ======================================================================
def test_r2_bad_url_errors_without_rewriting_url(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    bad = http_server + "/no_such_stream.flv"
    r = FFmpegReader(bad, width=320, height=180, use_cuda=False)
    r.start()
    try:
        frames = _frames(r, seconds=8.0)
        assert len(frames) == 0, "坏 URL 不应出帧"
        assert r.stream_url == bad, "stream_url 不得被改写（无回退目标）"
        assert not r.is_running or r.error
    finally:
        r.stop()


# ======================================================================
# R3：exe_path 覆盖
# ======================================================================
def test_r3_exe_path_override(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    marker = r"D:\no_such_ffmpeg.exe"
    r = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                     use_cuda=False, exe_path=marker)
    cmd = r._build_ffmpeg_cmd()
    assert cmd[0] == marker
    assert r._exe_path == marker
    r.stop()


# ======================================================================
# R4：ffmpeg>=7 参数兼容（49f9a23 判据）
# ======================================================================
def test_r4_thread_queue_size_never_injected(http_server):
    """唯一运行时是外部 FFmpeg 7：`-thread_queue_size` 不得再被注入。

    该选项在 ffmpeg>=7 被归到 muxer（输出）侧，作为输入选项会被 CLI 直接
    拒绝（"cannot be applied to input url"）→ 整条命令开流失败（实测）。
    旧实现按 `exe_path` 是否传入分支注入；内置 FFmpeg 6 移除后该分支已删。
    """
    from stream.ffmpeg_reader import FFmpegReader

    r_ext = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                         use_cuda=False, exe_path=r"D:\ffmpeg7\ffmpeg.exe")
    cmd_ext = r_ext._build_ffmpeg_cmd()
    assert "-thread_queue_size" not in cmd_ext, \
        "ffmpeg>=7 运行时不得注入 -thread_queue_size（否则整条命令开流失败）"

    # 未显式传 exe_path（回落到已校验的 FFmpeg 7 运行时）同样不得注入
    r_fallback = FFmpegReader(http_server + "/sample_ok.mp4", width=320,
                              height=180, use_cuda=False)
    cmd_fallback = r_fallback._build_ffmpeg_cmd()
    assert "-thread_queue_size" not in cmd_fallback
    assert os.path.exists(cmd_fallback[0]) or cmd_fallback[0].endswith(".exe"), \
        f"回落解析出的运行时路径应可执行: {cmd_fallback[0]}"


# ======================================================================
# R5：解码失败信号（用于有界重启，不切旧路径）
# ======================================================================
class _FakeProc:
    def __init__(self, lines):
        self.stderr = [ln.encode("utf-8") for ln in lines]
        self.stdout = None
        self.returncode = None

    def poll(self):
        return None

    def kill(self):
        pass


def test_r5_decode_failure_flag_has_no_fallback_gate():
    """没有配置回退也要置位 —— 旧实现的 `self._fallback` 条件已移除。"""
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader("https://x/flv", width=320, height=180, use_cuda=False)
    assert r.decode_failed is False
    r._drain_stderr(_FakeProc(
        ["[flv @ 0x1] Video codec hevc not implemented. Update your FFmpeg"]))
    assert r.decode_failed is True, \
        "解码器不支持必须被记录（看门狗据此有界重启）"


# ======================================================================
# R6 / R7 / R8：资源生命周期
# ======================================================================
def test_r6_stop_releases_process_and_threads(http_server):
    from stream.ffmpeg_reader import FFmpegReader

    before = threading.active_count()
    r = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                     use_cuda=False)
    r.start()
    _frames(r, seconds=6.0, want=2)
    r.stop()

    assert r._process is None, "stop() 必须清空子进程引用"
    assert r.is_running is False
    assert r.connected is False
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline and threading.active_count() > before:
        time.sleep(0.05)
    assert threading.active_count() <= before + 1, \
        f"stop() 后线程未回收: before={before} after={threading.active_count()}"


def test_r7_repeated_start_stop_no_leak(http_server):
    from stream.ffmpeg_reader import FFmpegReader

    before = threading.active_count()
    for _ in range(3):
        r = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                         use_cuda=False)
        r.start()
        _frames(r, seconds=5.0, want=1)
        r.stop()
        assert r._process is None

    deadline = time.perf_counter() + 4.0
    while time.perf_counter() < deadline and threading.active_count() > before + 1:
        time.sleep(0.05)
    assert threading.active_count() <= before + 1, \
        f"多轮 start/stop 后线程泄漏: before={before} " \
        f"after={threading.active_count()}"


def test_r8_stop_is_idempotent(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader(http_server + "/sample_ok.mp4", width=320, height=180,
                     use_cuda=False)
    r.start()
    _frames(r, seconds=4.0, want=1)
    r.stop()
    r.stop()
    r.stop()
    assert r._process is None


# ======================================================================
# R10：无任何运行时 → 可读错误（不再有内置解码器兜底）
# ======================================================================
def test_r10_no_runtime_raises_readable_error(monkeypatch):
    from stream.ffmpeg_reader import FFmpegReader
    from utils import ffmpeg7_runtime as RUNTIME

    monkeypatch.setattr(RUNTIME, "status",
                        lambda refresh=False: RUNTIME.Ffmpeg7Status(
                            False, RUNTIME.REASON_NOT_FOUND, detail="test"))
    monkeypatch.setattr(RUNTIME, "resolved_exe", lambda: None)
    RUNTIME.reset_cache()

    r = FFmpegReader("https://x/live.flv", width=320, height=180,
                     use_cuda=False, exe_path=None)
    with pytest.raises(RuntimeError) as ei:
        r._build_ffmpeg_cmd()
    msg = str(ei.value)
    assert "低延迟播放组件" in msg, f"错误应指向组件缺失，实际={msg!r}"
    for leak in ("Traceback", "WinError", "0x"):
        assert leak not in msg
