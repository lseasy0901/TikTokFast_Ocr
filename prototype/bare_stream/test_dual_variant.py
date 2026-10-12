# -*- coding: utf-8 -*-
"""B3-3 自动化测试：本地 HTTP 夹具覆盖 T1-T5（离线，不依赖直播间）。

夹具由 ffmpeg7 生成（缺二进制则整组跳过）：
  bare_ok.flv    = 增强FLV HEVC（libx265）
  uhd_ok.flv     = 标准FLV H264（libx264）
  audio_only.flv = 仅 AAC
"""
import http.server
import os
import subprocess
import threading
import time

import pytest

EXE7 = r"D:\TikTok_Ocr_Rebuild\runtime\ffmpeg7\ffmpeg.exe"
EXE6 = r"D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ffmpeg\ffmpeg.exe"
FIX_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(EXE7) and os.path.exists(EXE6)),
    reason="需要 ffmpeg7/ffmpeg6 二进制")

def _make_fixtures():
    os.makedirs(FIX_DIR, exist_ok=True)
    src = "testsrc2=size=320x180:rate=30"
    jobs = [
        ("bare_ok.flv", ["-f", "lavfi", "-i", src + ":rate=30", "-t", "8",
                         "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt",
                         "yuv420p", "-c:a", "aac"]),
        ("uhd_ok.flv", ["-f", "lavfi", "-i", src + ":rate=30", "-t", "8",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac"]),
        ("audio_only.flv", ["-f", "lavfi", "-i",
                            "sine=frequency=440:rate=48000", "-t", "8",
                            "-c:a", "aac"]),
    ]
    for name, args in jobs:
        out = os.path.join(FIX_DIR, name)
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            continue
        subprocess.run([EXE7, "-hide_banner", "-loglevel", "error", "-y"]
                       + args + [out], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def http_server():
    _make_fixtures()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=FIX_DIR, **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % port
    srv.shutdown()


def _u(base, name):
    return base + "/" + name


def _wait_seen(dvs, states, timeout=25.0):
    """等待 states_seen 历史中出现任一目标态（状态是瞬态的）。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if set(states) & set(dvs.states_seen):
            return True
        time.sleep(0.1)
    return False


def test_t1_bare_ok(http_server):
    import dual_variant_source as m
    s = m.DualVariantSource(_u(http_server, "bare_ok.flv"),
                                  _u(http_server, "uhd_ok.flv"),
                                  EXE7, EXE6, first_frame_timeout=8,
                                  hwaccel=False)
    s.start()
    ok = _wait_seen(s, ["BARE_STREAMING"])
    try:
        assert ok, (s.states_seen, s.errors)
        assert s.variant == "bare"
        assert len(s.frames) > 10, len(s.frames)
    finally:
        s.stop()
    assert "STREAM_END" in s.states_seen


def test_t2_bare_404_fallback(http_server):
    import dual_variant_source as m
    s = m.DualVariantSource(_u(http_server, "no_such_bare.flv"),
                            _u(http_server, "uhd_ok.flv"),
                            EXE7, EXE6, first_frame_timeout=8, hwaccel=False)
    s.start()
    ok = _wait_seen(s, ["UHD_STREAMING", "ERROR"])
    try:
        assert ok and s.variant == "uhd", (s.states_seen, s.errors)
        assert any(e[1] == "fallback_start" for e in s.log)
    finally:
        s.stop()


def test_t3_audio_only_fallback(http_server):
    import dual_variant_source as m
    s = m.DualVariantSource(_u(http_server, "audio_only.flv"),
                            _u(http_server, "uhd_ok.flv"),
                            EXE7, EXE6, first_frame_timeout=8, hwaccel=False)
    s.start()
    ok = _wait_seen(s, ["UHD_STREAMING", "ERROR"])
    try:
        assert ok and s.variant == "uhd", (s.states_seen, s.errors)
    finally:
        s.stop()


def test_t4_decoder_unsupported_fallback(http_server):
    """裸流内容 + 强制 ffmpeg6（无法解 HEVC）→ 必须回退。"""
    import dual_variant_source as m
    s = m.DualVariantSource(_u(http_server, "bare_ok.flv"),
                            _u(http_server, "uhd_ok.flv"),
                            EXE7, EXE6, first_frame_timeout=8,
                            hwaccel=False)
    s.exe7 = EXE6          # 显式错误版本（模拟误选），验证状态机仍能回退
    s.start()
    ok = _wait_seen(s, ["UHD_STREAMING", "ERROR"])
    try:
        assert ok and s.variant == "uhd", (s.states_seen, s.errors)
    finally:
        s.stop()


def test_t5_both_fail_error(http_server):
    import dual_variant_source as m
    s = m.DualVariantSource(_u(http_server, "no_such.flv"),
                            _u(http_server, "no_such.flv"),
                            EXE7, EXE6, first_frame_timeout=5, hwaccel=False)
    s.start()
    ok = _wait_seen(s, ["ERROR"], timeout=25)
    assert ok, (s.states_seen, s.errors)
    assert any(e[1] == "final_error" for e in s.log)
