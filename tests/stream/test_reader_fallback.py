# -*- coding: utf-8 -*-
"""B4: FFmpegReader 回退路径集成测试（本地 HTTP 夹具，离线可复现）。

场景：
  F1 主 URL 404 → 回退 URL 正常出帧，stream_url 已切换；
  F2 无回退配置 + 主 URL 404 → 保持原行为（错误结束，不 hang）；
  F3 exe_path 覆盖生效（cmd[0] 使用指定二进制）。
"""
import http.server
import os
import subprocess
import threading
import time

import pytest

EXE6 = r"D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ffmpeg\ffmpeg.exe"
FIX_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
UHD_FLV = os.path.join(FIX_DIR, "uhd_ok.mp4")

pytestmark = pytest.mark.skipif(not os.path.exists(EXE6),
                                reason="需要 ffmpeg6 二进制")


def _make_fixture():
    os.makedirs(FIX_DIR, exist_ok=True)
    if os.path.exists(UHD_FLV) and os.path.getsize(UHD_FLV) > 10000:
        return
    exe7 = r"D://TikTok_Ocr_Rebuild//runtime//ffmpeg7//ffmpeg.exe"
    gen = exe7 if os.path.exists(exe7) else EXE6
    subprocess.run(
        [gen, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "30",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         UHD_FLV],
        capture_output=True, timeout=180)


@pytest.fixture(scope="module")
def http_server():
    _make_fixture()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
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


def test_f1_primary_404_falls_back(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    primary = http_server + "/no_such_primary.flv"
    fallback = http_server + "/uhd_ok.mp4"
    r = FFmpegReader(primary, width=320, height=180, use_cuda=False,
                     fallback=(fallback, None))
    r.start()
    try:
        frames = _frames(r)
        assert len(frames) >= 5, "回退后未出帧"
        assert r.stream_url == fallback, "stream_url 未切换到回退流"
        assert r._fallback_used is True
    finally:
        r.stop()


def test_f2_no_fallback_keeps_original_behavior(http_server):
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader(http_server + "/no_such_primary.flv",
                     width=320, height=180, use_cuda=False)
    r.start()
    try:
        frames = _frames(r, seconds=8.0)
        # 原行为：EOF → 错误结束，不出帧、不 hang
        assert len(frames) == 0
        assert (not r.is_running) or (r._fallback is None)
    finally:
        r.stop()


def test_f3_exe_path_override(http_server):
    """exe_path 覆盖：_build_ffmpeg_cmd 的 cmd[0] 使用指定二进制。"""
    from stream.ffmpeg_reader import FFmpegReader
    marker = r"D:\no_such_ffmpeg.exe"
    r = FFmpegReader(http_server + "/uhd_ok.mp4", width=320, height=180,
                     use_cuda=False, exe_path=marker)
    cmd = r._build_ffmpeg_cmd()
    assert cmd[0] == marker
    assert r._exe_path == marker
    r.stop()


# ======================================================================
# RELEASE-02-B5 增补：FFmpeg7 参数兼容 / 回退守卫 / 解码失败信号 / 资源生命周期
# ======================================================================
def test_f4_thread_queue_size_only_for_default_runtime(http_server):
    """49f9a23 的核心判据：ffmpeg>=7（exe_path 显式传入）不得注入
    `-thread_queue_size`（作为输入选项会被 CLI 拒绝）；生产默认路径必须逐字不变。"""
    from stream.ffmpeg_reader import FFmpegReader

    # 生产默认（内置运行时，exe_path 未传）→ 保留该选项（行为不变）
    r_default = FFmpegReader(http_server + "/uhd_ok.mp4", width=320,
                             height=180, use_cuda=False)
    cmd_default = r_default._build_ffmpeg_cmd()
    assert "-thread_queue_size" in cmd_default, "生产默认路径不得改动命令行"

    # 外部 ffmpeg7 运行时（exe_path 显式传入）→ 省略该选项
    r_ext = FFmpegReader(http_server + "/uhd_ok.mp4", width=320, height=180,
                         use_cuda=False, exe_path=r"D:\ffmpeg7\ffmpeg.exe")
    cmd_ext = r_ext._build_ffmpeg_cmd()
    assert "-thread_queue_size" not in cmd_ext, \
        "ffmpeg>=7 运行时不得注入 -thread_queue_size（否则整条命令开流失败）"

    # 其余命令行逐字一致：只在默认路径上多出该选项（含其取值）与可执行文件不同
    def _strip_flag(cmd):
        out, i = [], 0
        while i < len(cmd):
            if cmd[i] == "-thread_queue_size":
                i += 2                      # 连同取值 "1" 一起跳过
                continue
            out.append(cmd[i])
            i += 1
        return out

    a, b = _strip_flag(cmd_default), list(cmd_ext)
    a[0] = b[0] = "<EXE>"
    assert a == b, "除 exe 与该选项外，命令行必须逐字一致"


def test_f5_switch_to_fallback_only_once(http_server):
    """回退守卫：同一实例只允许切换一次（防重复回退）。"""
    from stream.ffmpeg_reader import FFmpegReader
    fb = http_server + "/uhd_ok.mp4"
    r = FFmpegReader(http_server + "/no_such.flv", width=320, height=180,
                     use_cuda=False, fallback=(fb, None))
    assert r.fallback_used is False

    assert r._switch_to_fallback(None) is True, "首次切换应成功"
    assert r.fallback_used is True
    assert r.stream_url == fb
    assert r._exe_path is None, "回退 exe=None → 使用默认（内置）运行时"

    assert r._switch_to_fallback(None) is False, "第二次切换必须被守卫拦下"
    r.stop()


def test_f5b_no_fallback_configured_is_noop():
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader("https://x/flv", width=320, height=180, use_cuda=False)
    assert r._switch_to_fallback(None) is False
    assert r.fallback_used is False


class _FakeProc:
    """只提供 `_drain_stderr` 需要的接口。"""

    def __init__(self, lines):
        self.stderr = [ln.encode("utf-8") for ln in lines]
        self.stdout = None
        self.returncode = None

    def poll(self):
        return None

    def kill(self):
        pass


def test_f6_decode_failure_detected_from_stderr():
    """裸流遇不支持解码器：stderr 'not implemented' → decode_failed 置位
    （看门狗据此快速击杀并回退，无需等满停滞超时）。"""
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader("https://x/flv", width=320, height=180, use_cuda=False,
                     fallback=("https://x/uhd.flv", None))
    assert r.decode_failed is False
    r._drain_stderr(_FakeProc([
        "[flv @ 0x1] Video codec hevc not implemented. Update your FFmpeg version",
    ]))
    assert r.decode_failed is True


def test_f6b_decode_failure_not_flagged_without_fallback():
    """没有配置回退时不置位（保持既有行为，避免无谓提前击杀）。"""
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader("https://x/flv", width=320, height=180, use_cuda=False)
    r._drain_stderr(_FakeProc(["Video codec hevc not implemented"]))
    assert r.decode_failed is False


def test_f7_stop_releases_process_and_threads(http_server):
    """资源释放：stop() 之后子进程引用清空、读取线程退出、无残留。"""
    import threading
    from stream.ffmpeg_reader import FFmpegReader

    before = threading.active_count()
    r = FFmpegReader(http_server + "/uhd_ok.mp4", width=320, height=180,
                     use_cuda=False)
    r.start()
    _frames(r, seconds=6.0, want=2)
    r.stop()

    assert r._process is None, "stop() 必须清空子进程引用"
    assert r.is_running is False
    assert r.connected is False
    # 给 drain-stderr 等守护线程一点退出时间
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline and threading.active_count() > before:
        time.sleep(0.05)
    assert threading.active_count() <= before + 1, \
        f"stop() 后线程未回收: before={before} after={threading.active_count()}"


def test_f8_repeated_start_stop_no_leak(http_server):
    """连续多次开始/停止不产生残留线程与进程。"""
    import threading
    from stream.ffmpeg_reader import FFmpegReader

    before = threading.active_count()
    for _ in range(3):
        r = FFmpegReader(http_server + "/uhd_ok.mp4", width=320, height=180,
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


def test_f9_stop_is_idempotent(http_server):
    """stop() 可重复调用（既有契约，B5 回退路径会多次经过）。"""
    from stream.ffmpeg_reader import FFmpegReader
    r = FFmpegReader(http_server + "/uhd_ok.mp4", width=320, height=180,
                     use_cuda=False)
    r.start()
    _frames(r, seconds=4.0, want=1)
    r.stop()
    r.stop()
    r.stop()
    assert r._process is None
