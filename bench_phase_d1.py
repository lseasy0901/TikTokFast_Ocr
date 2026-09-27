# -*- coding: utf-8 -*-
"""Phase D-1 视频渲染链路性能审计 —— 临时 benchmark（不参与产品运行）。

三种模式，均复用生产代码路径，不改动任何生产文件：

  live       本地 HTTP 节流 FLV 源 → 真实 FFmpegReader(生产参数) → 真实
             LatestFrameBuffer → 真实 _FrameBridge/_FrameNotifier → 真实
             VideoWidget.set_frame/paintEvent。整条链路与线上一致，只有源
             不是抖音 CDN。
  decompose  复刻 set_frame / paintEvent 内部的单步调用并单独计时（不修改
             生产代码，只复制同样的调用序列）。
  capacity   真实 FFmpegReader 直读本地文件（不节流），测解码+管道吞吐上限。

用法示例：
    python bench_phase_d1.py --mode live --file s720.flv --w 1280 --h 720 \
        --pane 840x730 --secs 65 --out out_720.json
"""

import argparse
import http.server
import json
import os
import socketserver
import statistics
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from gui.main_window import _FrameBridge
from gui.video_widget import VideoWidget
from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor

PORT = 8899


# ----------------------------------------------------------------------
# 节流 HTTP-FLV 源（模拟直播推流速率）
# ----------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server
        data = srv.payload
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Connection", "close")
        self.end_headers()

        chunk = 8192
        bps = srv.bytes_per_sec
        t0 = time.perf_counter()
        sent = 0
        while sent < len(data) and srv.running:
            n = min(chunk, len(data) - sent)
            try:
                self.wfile.write(data[sent:sent + n])
            except Exception:
                return
            sent += n
            due = sent / bps
            spent = time.perf_counter() - t0
            if due > spent:
                time.sleep(due - spent)
        # 保持连接直到被测进程主动断开，避免提前 EOF 被当成"断流"
        while srv.running:
            time.sleep(0.2)


class _ThrottledServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_throttled_source(path: str, duration_s: float):
    size = os.path.getsize(path)
    srv = _ThrottledServer(("127.0.0.1", PORT), _Handler)
    srv.payload = open(path, "rb").read()
    srv.bytes_per_sec = size / duration_s
    srv.running = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ----------------------------------------------------------------------
# 记录完整样本的性能监控器（复用生产 SampleWindow，另存全量样本）
# 只在 benchmark 侧继承，生产代码不动。
# ----------------------------------------------------------------------
class RecordingPerf(PerformanceMonitor):
    def __init__(self):
        super().__init__()
        self.sf = []
        self.pt = []
        self.pt_nodraw = 0

    def on_set_frame(self, d):
        self.sf.append(d)
        super().on_set_frame(d)

    def on_paint_event(self, d, drew):
        self.pt.append(d)
        if not drew:
            self.pt_nodraw += 1
        super().on_paint_event(d, drew)


def pct(vals, p):
    if not vals:
        return 0.0
    o = sorted(vals)
    return o[int((len(o) - 1) * p)]


def stats_block(vals):
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 3),
        "p50": round(pct(vals, 0.50), 3),
        "p95": round(pct(vals, 0.95), 3),
        "p99": round(pct(vals, 0.99), 3),
        "max": round(max(vals), 3),
    }


# ----------------------------------------------------------------------
# live 模式：完整链路
# ----------------------------------------------------------------------
def run_live(args):
    app = QApplication(sys.argv)
    win = QWidget()
    win.setWindowTitle("Phase D-1 bench")
    pw, ph = (int(x) for x in args.pane.lower().split("x"))
    win.resize(pw, ph)
    lay = QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    widget = VideoWidget()
    lay.addWidget(widget)
    win.show()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.01)
    actual = (widget.width(), widget.height())

    reader = None
    source = None
    url = args.url
    if args.file:
        source = start_throttled_source(args.file, args.source_duration)
        url = f"http://127.0.0.1:{PORT}/live.flv"

    reader = FFmpegReader(url, width=args.w, height=args.h, use_cuda=True)
    buf = LatestFrameBuffer()
    perf = RecordingPerf()
    perf.start()
    widget.attach_monitor(perf)

    bridge = _FrameBridge(reader, buf, perf)
    notifier = bridge.notifier

    last_id = [-1]

    def on_frame_ready():
        # 完全对应生产 MainWindow._on_frame_ready / _consume_latest_frame
        notifier.clear_pending()
        fid = buf.frame_id
        if fid != last_id[0]:
            frame = buf.get()
            if frame is not None:
                perf.on_display()
                widget.set_frame(frame)
                last_id[0] = fid

    notifier.frameReady.connect(on_frame_ready)

    # 周期性采样（对应生产 _on_frame_timeout 中的采样调用）
    def sample():
        perf.sample_produced(buf.frame_id)
        if reader is not None and reader.is_running:
            perf.sample_skipped(reader.frames_skipped)
            perf.set_pipe_stats(reader.pipe_backlog_stats())

    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(1000)

    reader.start()
    bridge.start()

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.secs:
        app.processEvents()
        time.sleep(0.002)
    elapsed = time.perf_counter() - t0

    snap = perf.snapshot()
    result = {
        "mode": "live",
        "resolution": f"{args.w}x{args.h}",
        "widget_size": f"{actual[0]}x{actual[1]}",
        "source": url,
        "elapsed_s": round(elapsed, 1),
        "connected": reader.connected,
        "reader_error": reader.error,
        "frames_read": reader.frames_read,
        "decode_fps": round(reader.frames_read / elapsed, 1),
        "display_fps": round(len(perf.sf) / elapsed, 1),
        "skipped": reader.frames_skipped,
        "input_frame_changes": snap["input_frame_changes"],
        "produced_fps": round(snap["produced_fps"], 1),
        "set_frame": stats_block(perf.sf),
        "paintEvent": stats_block(perf.pt),
        "paint_nodraw": perf.pt_nodraw,
        "pipe": snap["pipe"],
        # 生产 SampleWindow（300 样本环形窗口）读数，用于对照产品面板
        "prod_window_set_frame": {
            "p50": round(snap["set_frame_p50"], 2),
            "p95": round(snap["set_frame_p95"], 2),
            "p99": round(snap["set_frame_p99"], 2),
            "max": round(snap["set_frame_max"], 2),
        },
        "prod_window_paint": {
            "p50": round(snap["paint_p50"], 2),
            "p95": round(snap["paint_p95"], 2),
            "p99": round(snap["paint_p99"], 2),
            "max": round(snap["paint_max"], 2),
        },
    }
    timer.stop()
    bridge.stop()
    reader.stop()
    if source is not None:
        source.running = False
        source.shutdown()
    return result


# ----------------------------------------------------------------------
# capacity 模式：真实解码+管道吞吐（不节流，直读本地文件）
# ----------------------------------------------------------------------
def run_capacity(args):
    reader = FFmpegReader(args.file, width=args.w, height=args.h, use_cuda=True)
    reader.start()
    t0 = time.perf_counter()
    n0 = 0
    time.sleep(1.0)
    n0 = reader.frames_read
    t_start = time.perf_counter()
    while time.perf_counter() - t_start < args.secs:
        time.sleep(0.5)
    elapsed = time.perf_counter() - t_start
    n1 = reader.frames_read
    res = {
        "mode": "capacity",
        "resolution": f"{args.w}x{args.h}",
        "cuda": True,
        "elapsed_s": round(elapsed, 1),
        "frames_read": n1,
        "decode_fps": round((n1 - n0) / elapsed, 1),
        "first_frame_ms": reader.first_frame_ms,
        "pipe": reader.pipe_backlog_stats(),
        "reader_error": reader.error,
    }
    reader.stop()
    return res


# ----------------------------------------------------------------------
# decompose 模式：单步拆分（复刻生产调用序列，不改生产代码）
# ----------------------------------------------------------------------
def run_decompose(args):
    app = QApplication(sys.argv)
    pw, ph = (int(x) for x in args.pane.lower().split("x"))
    win = QWidget()
    win.resize(pw, ph)
    win.show()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)
    target_size = win.size()

    raw = bytes(args.w * args.h * 3)
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 256, size=(args.h, args.w, 3), dtype=np.uint8)
    del raw

    iters = 200
    res = {"mode": "decompose", "resolution": f"{args.w}x{args.h}",
           "widget_size": f"{target_size.width()}x{target_size.height()}",
           "iters": iters}

    def bench(fn):
        for _ in range(10):
            fn()
        ts = []
        for _ in range(iters):
            t = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t) * 1000.0)
        return stats_block(ts)

    res["bgr2rgb_copy"] = bench(lambda: frame[:, :, ::-1].copy())

    rgb = frame[:, :, ::-1].copy()

    def mk_qimage():
        return QImage(rgb.data, args.w, args.h, 3 * args.w, QImage.Format_RGB888)

    res["QImage_ctor"] = bench(mk_qimage)

    qimg = mk_qimage()
    res["QPixmap_fromImage"] = bench(lambda: QPixmap.fromImage(qimg))

    pm = QPixmap.fromImage(qimg)
    res["scaled_smooth"] = bench(
        lambda: pm.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    )
    res["scaled_fast"] = bench(
        lambda: pm.scaled(target_size, Qt.KeepAspectRatio, Qt.FastTransformation)
    )

    scaled = pm.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    offscreen = QPixmap(target_size)

    def full_paint():
        p = QPainter(offscreen)
        p.fillRect(offscreen.rect(), QColor(0, 0, 0))
        s = pm.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap(0, 0, s)
        p.end()

    res["paint_no_roi_total"] = bench(full_paint)

    def paint_precomputed():
        p = QPainter(offscreen)
        p.fillRect(offscreen.rect(), QColor(0, 0, 0))
        p.drawPixmap(0, 0, scaled)
        p.end()

    res["paint_precomputed_total"] = bench(paint_precomputed)

    def paint_roi_total():
        # 生产 paintEvent 在 ROI 存在时会再做一次完全相同的 scaled()
        p = QPainter(offscreen)
        p.fillRect(offscreen.rect(), QColor(0, 0, 0))
        s = pm.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap(0, 0, s)
        s2 = pm.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap(0, 0, s2)
        p.end()

    res["paint_with_roi_total"] = bench(paint_roi_total)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["live", "capacity", "decompose"])
    ap.add_argument("--file")
    ap.add_argument("--url")
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--pane", default="840x730")
    ap.add_argument("--secs", type=float, default=65.0)
    ap.add_argument("--source-duration", type=float, default=75.0)
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.mode == "live":
        res = run_live(args)
    elif args.mode == "capacity":
        res = run_capacity(args)
    else:
        res = run_decompose(args)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
