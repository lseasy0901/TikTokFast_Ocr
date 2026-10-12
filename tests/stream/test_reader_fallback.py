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
