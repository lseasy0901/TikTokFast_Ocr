# -*- coding: utf-8 -*-
"""Phase 3.4 视频 E2E 延迟归因 —— 诊断用 harness（不参与产品运行）。

三种模式，都**不修改**任何生产代码/参数：

  socket    纯 HTTP 读取原始 FLV，解析 tag 时间戳，记录
            (tag 时间戳, 到达本地 socket 的墙钟时间)。完全不经过 FFmpeg。
            → 测"我们收到的数据有多旧"（源侧：编码 + CDN + 网络）。

  decode    用**生产同一条 FFmpeg 命令**（input/output 参数逐字取自带
            FFmpegReader._build_ffmpeg_cmd，仅额外插入 -vf showinfo 与
            -loglevel info 用于诊断），自己读 rawvideo 管道、按帧索引关联
            showinfo 的 pts_time，记录 (媒体 pts, 读到的墙钟时间)。
            → 测"解码出来的帧相对本地墙钟落后多少"，即 FFmpeg 输入/demux/
              解码这一段的贡献。

  livelens  真实 MainWindow(offscreen) + 真实 FFmpegReader + 真实
            FrameBridge/LatestFrameBuffer/VideoWidget，只在**读取侧**
            包裹观测（不改实现）：每帧到达/显示时刻、latency_ms、
            set_frame/paint 耗时、frame_id 间隔、管道积压、跳帧。
            → 测 LiveLens 本地消费路径贡献。

用法：
    python bench_phase_3_4_latency_attribution.py --mode socket   --url <直播间URL> --secs 120 --out socket.json
    python bench_phase_3_4_latency_attribution.py --mode decode   --url <直播间URL> --secs 120 --out decode.json
    python bench_phase_3_4_latency_attribution.py --mode livelens --url <直播间URL> --secs 120 --out live.json
    三个模式可以并发跑（--tag 用来区分同一次会话的输出文件）。
"""

import argparse
import json
import logging
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stream.ffmpeg_reader import FFmpegReader
from stream.models import StreamCandidate, StreamInfo
from stream.registry import build_default_registry
from stream.resolver import StreamResolver
from stream.video_info import build_probe_cmd, parse_probe_output

logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")

FLV_HEADER_SIZE = 9
FLV_TAG_HEADER = 11
FLV_VIDEO, FLV_AUDIO, FLV_SCRIPT = 9, 8, 18


# ----------------------------------------------------------------------
# 把媒体时间轴锚到真实世界：平台页面里的开播时间
# ----------------------------------------------------------------------
def fetch_start_epoch(url):
    """返回 (开播 epoch 秒, 平台名)；取不到返回 (None, None)。

    直播流的 FLV/H.264 时间戳是"编码器开播以来的毫秒数"。知道开播的墙钟
    时刻后，tag/帧的年龄 = now - (start_epoch + ts/1000)，即该内容从开播
    到抵达本机的真实时长 —— 这是本次归因唯一能把"媒体时间"翻译成
    "真实世界延迟"的锚点。

    锚点自身的不确定度：开播时间只有秒级精度（±1s），且编码器时间轴原点
    未必恰好等于平台记录的开播秒。因此绝对年龄带 ±1~2s 量级的系统误差，
    但**随时间的变化量**（漂移、跳变）不受此误差影响。
    """
    adapter = build_default_registry().get_adapter(url)
    if adapter is None:
        return None, None
    fetch = getattr(adapter, "_fetch_room_page", None)
    if fetch is None:
        return None, None
    try:
        html = fetch(url)
    except Exception as e:  # noqa: BLE001 - 锚点取不到不影响相对测量
        print("[anchor] 开播时间获取失败: %s" % str(e)[:100])
        return None, getattr(adapter, "name", None)
    m = re.search(r'"startTime"\s*:\s*"?(\d{9,13})', html)
    if not m:
        return None, getattr(adapter, "name", None)
    value = int(m.group(1))
    if value > 10 ** 11:            # 毫秒
        value //= 1000
    return value, getattr(adapter, "name", None)


def age_ms(epoch_now, start_epoch, media_seconds):
    """内容年龄（ms）：媒体时间轴上的这一点，距离真实世界现在多久"""
    if start_epoch is None or media_seconds is None:
        return None
    return (epoch_now - (start_epoch + media_seconds)) * 1000.0


def implied_origin(pairs):
    """媒体时间轴隐含的"开播墙钟时刻"（中位数）

    :param pairs: [(收到该媒体时间的绝对墙钟 epoch, 媒体秒数)]
    """
    vals = sorted(w - m for w, m in pairs if w is not None and m is not None)
    return vals[len(vals) // 2] if vals else None


def anchor_delta(origin, start_epoch):
    """隐含开播时刻 − 平台页面 startTime（秒）。差值大 ⇒ 锚点不可信。"""
    if origin is None or start_epoch is None:
        return None
    return origin - start_epoch



# ----------------------------------------------------------------------
# 统计工具
# ----------------------------------------------------------------------
def stats(values):
    """一组数的描述统计（空集返回 None）"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    ordered = sorted(vals)

    def pct(p):
        return ordered[int((len(ordered) - 1) * p)]

    return {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "min": ordered[0],
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": ordered[-1],
        "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
    }


def windowed(series, key, seconds=20.0):
    """把 (t, value) 序列按时间窗分段求均值，看趋势/周期性"""
    if not series:
        return []
    t0 = series[0][0]
    buckets = {}
    for t, item in series:
        value = item[key] if isinstance(item, dict) else item
        if value is None:
            continue
        idx = int((t - t0) // seconds)
        buckets.setdefault(idx, []).append(value)
    return [
        {"window_s": [round(i * seconds, 1), round((i + 1) * seconds, 1)],
         "n": len(v), "mean": statistics.fmean(v)}
        for i, v in sorted(buckets.items())
    ]


def gaps(times):
    """相邻时刻间隔的分位数（用于'连续无新帧最长时长'）"""
    if len(times) < 2:
        return {"n": len(times)}
    deltas = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
    return stats(deltas)


# ----------------------------------------------------------------------
# 取一个"真的能出数据"的候选（AL 会 403，与生产 failover 行为一致）
# ----------------------------------------------------------------------
def probe_size_with_headers(stream_url, headers, timeout=8):
    """带平台请求头跑一次 ffprobe（生产 reader 就是这么喂 ffmpeg 的）。

    注意：`video_info.get_video_info` 不带平台头，对虎牙这类"校验 UA"的 CDN
    必然失败（虎牙适配器的分辨率探测因此一直拿不到尺寸）——这里补上头。
    """
    cmd = build_probe_cmd(stream_url)
    headers = dict(headers or {})
    if headers:
        ua = headers.pop("User-Agent", None)
        if ua:
            cmd = cmd[:-1] + ["-user_agent", ua] + [cmd[-1]]
        if headers:
            blob = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items())
            cmd = cmd[:-1] + ["-headers", blob] + [cmd[-1]]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout).stdout.decode("utf-8", errors="replace")
        return parse_probe_output(out)
    except Exception:  # noqa: BLE001 - 探测失败按"不可用"处理
        from stream.video_info import VideoInfo
        return VideoInfo()


def pick_working_candidate(info):
    """挑一个**真能播**的候选：以"带平台头 ffprobe 能拿到尺寸"为准。

    这与生产 failover 的判据一致（虎牙 AL/TX 常被 CDN 拒绝），保证三个探针
    测的是同一路 CDN，可比。
    """
    for cand in (info.candidates or []):
        if not cand.url:
            continue
        vin = probe_size_with_headers(cand.url, cand.http_headers)
        if vin.width and vin.height:
            print("[candidate] 可播: cdn=%s %dx%d codec=%s fps=%s"
                  % (cand.metadata.get("cdn"), vin.width, vin.height,
                     vin.codec_name, vin.fps))
            return cand
        print("[candidate] 不可播: cdn=%s" % cand.metadata.get("cdn"))
    return None


# ======================================================================
# 模式 1：socket —— 原始 FLV tag 时间戳 vs 本地墙钟
# ======================================================================
def run_socket(args):
    info = StreamResolver().resolve(args.url)
    cand = pick_working_candidate(info)
    if cand is None:
        return {"error": "no working candidate"}
    start_epoch, platform = fetch_start_epoch(args.url)
    print("[anchor] platform=%s start_epoch=%s" % (platform, start_epoch))

    req = urllib.request.Request(cand.url, headers=dict(cand.http_headers or {}))
    resp = urllib.request.urlopen(req, timeout=10)
    print("[socket] connected, reading raw FLV tags...")

    t0 = time.perf_counter()
    epoch0 = time.time()
    deadline = t0 + args.secs
    buf = bytearray()
    video, audio, script = [], [], []
    n_bytes = 0
    header_done = False

    while time.perf_counter() < deadline:
        chunk = resp.read(65536)
        if not chunk:
            break
        n_bytes += len(chunk)
        buf += chunk

        if not header_done:
            if len(buf) < FLV_HEADER_SIZE + 4:
                continue
            buf = buf[FLV_HEADER_SIZE + 4:]     # 跳过 9 字节头 + PreviousTagSize0
            header_done = True

        while True:
            if len(buf) < FLV_TAG_HEADER:
                break
            tag_type = buf[0]
            data_size = int.from_bytes(buf[1:4], "big")
            ts = int.from_bytes(buf[4:7], "big") | (buf[7] << 24)
            total = FLV_TAG_HEADER + data_size + 4
            if len(buf) < total:
                break
            wall = time.perf_counter()
            record = (round(wall - t0, 6), ts, time.time())
            if tag_type == FLV_VIDEO:
                video.append(record)
            elif tag_type == FLV_AUDIO:
                audio.append(record)
            else:
                script.append(record)
            del buf[:total]

    resp.close()
    print("[socket] tags: video=%d audio=%d other=%d" % (len(video), len(audio), len(script)))

    # 内容年龄：这一刻媒体时间轴上的这一点，距离真实世界现在多久
    ages = [(w, age_ms(epoch, start_epoch, ts / 1000.0))
            for (w, ts, epoch) in video]
    ages = [(w, a) for w, a in ages if a is not None]
    off_values = [a for _, a in ages]
    # 稳态：丢掉连接初期 CDN 补发积压的那一段
    steady = [a for w, a in ages if w >= 10.0]
    result = {
        "mode": "socket",
        "source": args.url,
        "platform": platform,
        "start_epoch": start_epoch,
        "epoch0": epoch0,
        "tag_series": [[w, ts] for (w, ts, _) in video],
        "cdn": cand.metadata.get("cdn"),
        "candidate_protocol": cand.protocol,
        "secs": args.secs,
        "bytes_read": n_bytes,
        "video_tags": len(video),
        "audio_tags": len(audio),
        "other_tags": len(script),
        "first_video_tag_ts_ms": video[0][1] if video else None,
        "last_video_tag_ts_ms": video[-1][1] if video else None,
        "media_seconds_advanced": (
            (video[-1][1] - video[0][1]) / 1000.0 if len(video) > 1 else None),
        "content_age_ms": stats(off_values),
        "content_age_ms_steady": stats(steady),
        "content_age_trend": windowed(ages, 1),
        "tag_arrival_gap_ms": gaps([w for w, _, _ in video]),
        # 媒体时间轴隐含的"开播时刻"（= 收到该 tag 的墙钟 − 该 tag 的时间戳）。
        # 与平台页面里的 startTime 一比，就知道锚点是否可信。
        "implied_stream_origin_epoch": implied_origin(
            [(epoch0 + w, ts / 1000.0) for (w, ts, _) in video]),
        "anchor_delta_s": anchor_delta(
            implied_origin([(epoch0 + w, ts / 1000.0) for (w, ts, _) in video]),
            start_epoch),
        "series": [[w, ts] for (w, ts, _) in video],
    }
    return result


# ======================================================================
# 模式 2：decode —— 生产同参 FFmpeg + showinfo，管道自读
# ======================================================================
def run_decode(args):
    info = StreamResolver().resolve(args.url)
    cand = pick_working_candidate(info)
    if cand is None:
        return {"error": "no working candidate"}

    start_epoch, platform = fetch_start_epoch(args.url)
    print("[anchor] platform=%s start_epoch=%s" % (platform, start_epoch))
    vin = probe_size_with_headers(cand.url, cand.http_headers, timeout=10)
    if not (vin.width and vin.height):
        return {"error": "size probe failed"}
    w, h = vin.width, vin.height

    # --via-proxy：最小消费路径也走同一套"上游到达时刻"参照，便于与生产路径 A/B
    proxy = None
    if args.via_proxy:
        proxy = FlvProxy(cand.url, cand.http_headers)
        proxy.start()
        cand = type(cand)(url=proxy.local_url, protocol=cand.protocol,
                          http_headers=cand.http_headers, metadata={"cdn": "proxy"})
        print("[proxy] 上游→本地 %s" % proxy.local_url)
    frame_size = w * h * 3
    print("[decode] %dx%d codec=%s fps=%s" % (w, h, vin.codec_name, vin.fps))

    # 生产命令：input/output 参数逐字来自 FFmpegReader（此处不启动生产进程）
    builder = FFmpegReader(cand.url, width=w, height=h,
                           http_headers=cand.http_headers)
    cmd = builder._build_ffmpeg_cmd()
    # 诊断增强（只影响本探针，不落回生产）：
    #   -loglevel info   showinfo 的日志级别
    #   -copyts          保留 FLV 原始（开播以来）时间戳。ffmpeg 默认把首帧
    #                    时间戳平移成 0，那会把"内容年龄"的锚点整个丢掉；
    #                    生产路径不用 -copyts（rawvideo 不带时间戳，无影响）。
    if "-loglevel" in cmd:
        cmd[cmd.index("-loglevel") + 1] = "info"
    cmd.insert(cmd.index("-i"), "-copyts")
    cmd.insert(cmd.index("-an"), "-vf")         # 放在输出选项区
    cmd.insert(cmd.index("-vf") + 1, "showinfo")
    print("[decode] cmd: %s" % " ".join(cmd))

    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_lines = []

    def drain():
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)
    threading.Thread(target=drain, daemon=True).start()

    t0 = time.perf_counter()
    epoch0 = time.time()
    deadline = t0 + args.secs
    arrivals = []            # (t, bytes_len)
    pts_list = []            # showinfo 的 pts_time（按帧序）
    stdout_reads = []
    while time.perf_counter() < deadline:
        data = proc.stdout.read(frame_size)
        if not data or len(data) < frame_size:
            break
        now = time.perf_counter()
        stdout_reads.append((round(now - t0, 6), len(data), time.time()))
        arrivals.append(now)

    proc.kill()
    try:
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass

    # 关联：第 k 个 showinfo 行 ↔ 第 k 个管道读
    for line in stderr_lines:
        if "showinfo" in line and "pts_time:" in line:
            try:
                pts = float(line.split("pts_time:")[1].split()[0])
            except (IndexError, ValueError):
                continue
            pts_list.append(pts)

    proxy_lag = None
    if proxy:
        proxy.stop()
        if proxy.tags:
            f_up = _interp_from_epoch_tags(proxy.tags)
            lags = []
            for i in range(min(len(pts_list), len(stdout_reads))):
                base = f_up(pts_list[i])
                if base is not None:
                    lags.append(((epoch0 + stdout_reads[i][0]) - base) * 1000.0)
            proxy_lag = stats(lags)

    n = min(len(pts_list), len(stdout_reads))
    ages = []
    for i in range(n):
        wall, _, epoch = stdout_reads[i]
        a = age_ms(epoch, start_epoch, pts_list[i])
        if a is not None:
            ages.append((wall, a))
    diag = [ln for ln in stderr_lines
            if any(k in ln.lower() for k in ("buffer", "drop", "late", "timing", "vbv", "queue"))]

    return {
        "mode": "decode",
        "source": args.url,
        "platform": platform,
        "start_epoch": start_epoch,
        "epoch0": epoch0,
        "cdn": cand.metadata.get("cdn"),
        "secs": args.secs,
        "size": [w, h],
        "codec": vin.codec_name,
        "cmd": cmd,
        "frames_read": len(arrivals),
        "showinfo_frames": len(pts_list),
        "paired_frames": n,
        "decode_fps": (len(arrivals) / max(1e-6, (arrivals[-1] - arrivals[0])))
        if len(arrivals) > 1 else None,
        "first_pts_time": pts_list[0] if pts_list else None,
        "last_pts_time": pts_list[-1] if pts_list else None,
        "media_seconds_advanced": (pts_list[-1] - pts_list[0]) if len(pts_list) > 1 else None,
        "via_proxy": bool(args.via_proxy),
        "proxy_video_tags": len(proxy.tags) if proxy else 0,
        "client_decoded_lag_ms": proxy_lag,
        "content_age_ms": stats([a for _, a in ages]),
        "content_age_ms_steady": stats([a for w, a in ages if w >= 10.0]),
        "content_age_trend": windowed(ages, 1),
        "frame_interval_ms": gaps(arrivals),
        "ffmpeg_diag_lines": diag[:40],
        "stderr_tail": stderr_lines[-15:],
        "series": [[round(t, 6), pts_list[i]] for i, (t, _, _) in enumerate(stdout_reads[:n])],
    }


# ======================================================================
# 模式 3：livelens —— 生产路径 + 只读包裹观测
# ======================================================================
#: 生产 reader 的 stderr 诊断版：只多写 (墙钟, pts) 日志，不改任何行为
PTS_LOG = []
_READER_CLS = {"cls": None}


def _make_probe_reader():
    """返回一个 FFmpegReader 子类：命令里多一个 -vf showinfo（诊断用）。

    刻意不改生产文件：这里只是把 MainWindow 用的 reader 换成子类，
    命令的**输入参数与输出参数完全沿用父类**，仅插入 showinfo 与 INFO 日志级别。
    """
    class _ProbeReader(FFmpegReader):
        def _build_ffmpeg_cmd(self):
            cmd = super()._build_ffmpeg_cmd()
            if "-loglevel" in cmd:
                cmd[cmd.index("-loglevel") + 1] = "info"
            cmd.insert(cmd.index("-i"), "-copyts")   # 保留原始时间戳（诊断用）
            cmd.insert(cmd.index("-an"), "-vf")
            cmd.insert(cmd.index("-vf") + 1, "showinfo")
            return cmd

        def _drain_stderr(self, proc):
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if "pts_time:" in line:
                    try:
                        pts = float(line.split("pts_time:")[1].split()[0])
                        PTS_LOG.append((time.time(), pts))
                    except (IndexError, ValueError):
                        pass
                self._stderr_tail.append(line)

    return _ProbeReader


def run_livelens(args):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import gui.main_window as mw
    from utils.performance import PerformanceMonitor

    info = StreamResolver().resolve(args.url)
    start_epoch, platform = fetch_start_epoch(args.url)
    print("[anchor] platform=%s start_epoch=%s" % (platform, start_epoch))

    # --via-proxy：把候选地址换成"本地代理 → 上游"，这样"上游到达时刻"与
    # "解码/显示时刻"来自**同一条连接**，客户端贡献可被干净地差分出来。
    proxy = None
    if args.via_proxy:
        cand = pick_working_candidate(info)
        if cand is None:
            return {"error": "no working candidate"}
        proxy = FlvProxy(cand.url, cand.http_headers)
        proxy.start()
        # 尺寸用上游探测结果显式给出：本次测的是稳态链路，不是启动路径
        # （否则 reader 会再开一次连接去探测，把代理的两次连接交织在一起）
        vin = probe_size_with_headers(cand.url, cand.http_headers)
        info = StreamInfo(platform=info.platform, room_id=info.room_id,
                          candidates=[StreamCandidate(url=proxy.local_url,
                                                      protocol="flv",
                                                      http_headers=cand.http_headers)],
                          selected=None, width=vin.width, height=vin.height)
        info.selected = info.candidates[0]
        print("[proxy] 上游=%s 本地=%s" % (cand.metadata.get("cdn"), proxy.local_url))

    app = QApplication.instance() or QApplication([])

    # 生产 reader 换成诊断子类（--with-showinfo 时），否则用原类
    if args.with_showinfo:
        _READER_CLS["cls"] = mw.FFmpegReader
        mw.FFmpegReader = _make_probe_reader()
        print("[livelens] 已注入 showinfo（诊断子类，不改生产文件）")
    win = mw.MainWindow()

    t0 = time.perf_counter()
    epoch0 = time.time()
    arrivals = []      # FrameBridge 写入 buffer 的时刻（= 解码帧到达 LiveLens）
    displays = []      # GUI 取走帧的时刻 + 该帧 latency_ms
    display_pts = []   # GUI 取走帧的时刻 + 该帧媒体 pts（来自 showinfo，误差≤1帧）
    setframes = []
    paints = []
    id_samples = []    # (t, frame_id) 用于算"连续无新帧"与到达间隔
    samples = []       # 周期性 snapshot

    # 只读包裹 PerformanceMonitor 的四个观测钩子（在**类**上包，这样 failover
    # 换掉 perf 实例后依然有效）。不改任何实现，只记录时刻与耗时。
    orig = {name: getattr(PerformanceMonitor, name)
            for name in ("on_capture", "on_display", "on_set_frame", "on_paint_event")}

    def on_capture(self):
        orig["on_capture"](self)
        arrivals.append(time.perf_counter() - t0)

    display_ages = []

    def on_display(self):
        orig["on_display"](self)
        t = time.perf_counter() - t0
        displays.append((t, self.current_latency_ms))
        # 生产路径显示的这一帧 = reader 存下的最新帧；它的媒体时间 =
        # 此刻已收到的最后一条 showinfo pts（误差 ≤1 帧 + stderr 排空延迟）。
        if PTS_LOG:
            pts = PTS_LOG[-1][1]
            display_pts.append((t, pts))
            if start_epoch is not None:
                a = age_ms(time.time(), start_epoch, pts)
                if a is not None:
                    display_ages.append((t, a))

    def on_set_frame(self, duration_ms):
        orig["on_set_frame"](self, duration_ms)
        setframes.append((time.perf_counter() - t0, duration_ms))

    def on_paint_event(self, duration_ms, drew_frame):
        orig["on_paint_event"](self, duration_ms, drew_frame)
        paints.append((time.perf_counter() - t0, duration_ms, bool(drew_frame)))

    PerformanceMonitor.on_capture = on_capture
    PerformanceMonitor.on_display = on_display
    PerformanceMonitor.on_set_frame = on_set_frame
    PerformanceMonitor.on_paint_event = on_paint_event
    print("[livelens] 观测已挂载（只读包裹，不改实现）")

    win._handle_connect_success(info)
    ticker = time.time()
    while time.perf_counter() - t0 < args.secs:
        app.processEvents()
        if win._reader is not None:
            id_samples.append((time.perf_counter() - t0, win._reader.frame_id))
        if time.time() - ticker >= 0.25:
            ticker = time.time()
            if win._reader is not None and win._perf is not None:
                snap = win._perf.snapshot()
                samples.append({
                    "t": round(time.perf_counter() - t0, 3),
                    "frames_read": win._reader.frames_read,
                    "frames_skipped": win._reader.frames_skipped,
                    "frame_id": win._reader.frame_id,
                    "connected": win._reader.connected,
                    "pipe": dict(snap.get("pipe") or {}),
                    "display_fps": snap.get("fps"),
                    "produced_fps": snap.get("produced_fps"),
                    "set_frame_p50": snap.get("set_frame_p50"),
                    "set_frame_p95": snap.get("set_frame_p95"),
                    "paint_p95": snap.get("paint_p95"),
                    "skipped_rate": snap.get("skipped_rate"),
                    "latency_now": snap.get("current_latency_ms"),
                    "runtime_s": snap.get("runtime_s"),
                })
        time.sleep(0.002)

    reader = win._reader
    stderr_tail = list(getattr(reader, "_stderr_tail", []) or [])
    first_frame_ms = getattr(reader, "first_frame_ms", None)
    win.close()

    # 同一连接上的差分：上游到达 vs 解码 / 显示
    proxy_tags = list(proxy.tags) if proxy else []
    if proxy:
        proxy.stop()
    per_frame = []
    if proxy_tags:
        f_up = _interp_from_epoch_tags(proxy_tags)
        for ep, pts in PTS_LOG:
            base = f_up(pts)
            if base is not None:
                per_frame.append(((ep - base) * 1000.0, ep, pts))
        d_display = []
        for w, pts in display_pts:
            base = f_up(pts)
            if base is not None:
                d_display.append(((epoch0 + w) - base) * 1000.0)

    lat = [v for _, v in displays]
    gap_series = [t for t, _ in id_samples]
    # 只在 frame_id 变化处算"到达间隔"，避免 2ms 轮询噪声
    changes = []
    last_id = None
    for t, fid in id_samples:
        if last_id is not None and fid != last_id:
            changes.append(t)
        last_id = fid
    steady_ages = [a for t, a in display_ages if t >= 10.0]
    return {
        "mode": "livelens",
        "source": args.url,
        "platform": platform,
        "start_epoch": start_epoch,
        "epoch0": epoch0,
        "display_pts_series": display_pts,
        "decode_pts_series": PTS_LOG,
        "secs": args.secs,
        "with_showinfo": bool(args.with_showinfo),
        "candidates": len(info.candidates or []),
        "first_frame_ms": first_frame_ms,
        "decoded_frames": reader.frames_read if reader else None,
        "skipped_frames": reader.frames_skipped if reader else None,
        "displayed_frames": len(displays),
        "buffer_to_display_latency_ms": stats(lat),
        "display_to_display_interval_ms": gaps([t for t, _ in displays]),
        "decoded_arrival_interval_ms": gaps(arrivals),
        "frame_id_change_interval_ms": gaps(changes),
        "set_frame_ms": stats([d for _, d in setframes]),
        "paint_ms": stats([d for _, d, _ in paints]),
        "max_gap_without_new_frame_ms": (
            max(g for g in [(b - a) * 1000.0 for a, b in zip(changes, changes[1:])])
            if len(changes) > 1 else None),
        "reader_pipe_backlog_bytes": (reader.pipe_backlog_stats() if reader else None),
        "latency_trend": windowed([(t, v) for t, v in displays], 1),
        "displayed_frame_content_age_ms": stats([a for _, a in display_ages]),
        "displayed_frame_content_age_ms_steady": stats(steady_ages),
        "displayed_frame_age_trend": windowed(display_ages, 1),
        "showinfo_lines": len(PTS_LOG),
        # ---- 同一连接差分（--via-proxy 时才有）：客户端自己的全部贡献 ----
        "via_proxy": bool(args.via_proxy),
        "proxy_video_tags": len(proxy_tags),
        "proxy_tag_series": [[round(w - epoch0, 6), ts] for w, ts in proxy_tags],
        "proxy_delivery_interval_ms": gaps([w for w, _ in proxy_tags]),
        "client_decoded_lag_ms": stats([d for d, _, _ in per_frame]),
        "client_displayed_lag_ms": stats(d_display),
        "client_display_lag_trend": windowed(
            [(ep - epoch0, d) for d, ep, _ in per_frame], 20.0),
        "samples": samples,
        "reader_stderr_tail": stderr_tail,
    }


# ======================================================================
# 本地 HTTP-FLV 代理：给"同一连接"打上游到达时间戳
# ======================================================================
class FlvProxy(threading.Thread):
    """把上游 HTTP-FLV 转发给本机 FFmpeg，同时记录每个 tag 到达本机的墙钟。

    为什么要这一层：跨连接比较（两个 TCP 会话各测一次）会被"连接初期 CDN
    补发积压的快慢"污染，秒级误差；只在**同一条连接**上比较"上游到达时刻"
    与"解码/显示时刻"，才能干净地量出客户端自己加了多少延迟。

    只做两件事：转发字节、按 FLV 结构记 (wall, tag_ts)。不做缓存、不做改写。
    """

    def __init__(self, upstream_url, headers, chunk=8192):
        super().__init__(daemon=True)
        self.upstream_url = upstream_url
        self.headers = dict(headers or {})
        self.chunk = chunk
        self.tags = []              # [(wall_epoch, ts_ms)] 视频 tag 到达本机的时刻
        self.all_tags = []          # 含音频
        self.started = threading.Event()
        self.error = None
        self._stopped = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]

    @property
    def local_url(self):
        return "http://127.0.0.1:%d/live.flv" % self.port

    def run(self):
        """逐条接受客户端连接（探测/播放可能是两次连接，必须都能服务）"""
        while not self._stopped:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_guarded, args=(conn,),
                             daemon=True).start()

    def _serve_guarded(self, conn):
        try:
            self._serve(conn)
        except Exception as e:  # noqa: BLE001 - 代理异常只影响本次测量
            self.error = str(e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve(self, conn):
        # 1. 读完客户端的 HTTP 请求头
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = conn.recv(4096)
            if not part:
                return
            buf += part
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: video/x-flv\r\n"
                     b"Connection: close\r\n\r\n")
        self.started.set()

        # 2. 拉上游并边转发边解析 tag
        req = urllib.request.Request(self.upstream_url, headers=self.headers)
        resp = urllib.request.urlopen(req, timeout=10)
        parse_buf = bytearray()
        header_done = False
        while True:
            chunk = resp.read(self.chunk)
            if not chunk:
                break
            # 先取"上游到达时刻"再转发：转发可能因为客户端读得慢而阻塞，
            # 若在 sendall 之后取时刻，会把阻塞时间算成上游到达时间，
            # 让客户端延迟看起来偏小。
            wall = time.time()
            found = []
            parse_buf += chunk
            if not header_done:
                if len(parse_buf) < FLV_HEADER_SIZE + 4:
                    continue
                parse_buf = parse_buf[FLV_HEADER_SIZE + 4:]
                header_done = True
            while len(parse_buf) >= FLV_TAG_HEADER:
                tag_type = parse_buf[0]
                data_size = int.from_bytes(parse_buf[1:4], "big")
                ts = int.from_bytes(parse_buf[4:7], "big") | (parse_buf[7] << 24)
                total = FLV_TAG_HEADER + data_size + 4
                if len(parse_buf) < total:
                    break
                found.append((wall, ts, tag_type))
                del parse_buf[:total]
            try:
                conn.sendall(chunk)
            except OSError:
                break
            for wall, ts, tag_type in found:
                self.all_tags.append((wall, ts))
                if tag_type == FLV_VIDEO:
                    self.tags.append((wall, ts))
        resp.close()

    def stop(self):
        self._stopped = True
        try:
            self._sock.close()
        except OSError:
            pass


# ======================================================================
# 模式 4：analyze —— 跨探针的**无锚点**差分归因
# ======================================================================
def _interp_from_tags(tag_series, epoch0):
    """媒体时间 → 该内容到达 socket 的绝对墙钟（按 socket 序列线性插值）"""
    pts = [(epoch0 + w, ts / 1000.0) for w, ts in tag_series]
    pts.sort(key=lambda x: x[1])

    def f(media_seconds):
        if not pts or media_seconds < pts[0][1] or media_seconds > pts[-1][1]:
            return None
        lo, hi = 0, len(pts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if pts[mid][1] <= media_seconds:
                lo = mid
            else:
                hi = mid
        (w1, t1), (w2, t2) = pts[lo], pts[hi]
        if t2 == t1:
            return w1
        return w1 + (w2 - w1) * (media_seconds - t1) / (t2 - t1)

    return f


def _interp_from_epoch_tags(tags):
    """[(wall_epoch, ts_ms)] → f(media_seconds) = 该内容到达本机的绝对墙钟"""
    pts = sorted(((w, ts / 1000.0) for w, ts in tags), key=lambda x: x[1])

    def f(media_seconds):
        if not pts or media_seconds < pts[0][1] or media_seconds > pts[-1][1]:
            return None
        lo, hi = 0, len(pts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if pts[mid][1] <= media_seconds:
                lo = mid
            else:
                hi = mid
        (w1, t1), (w2, t2) = pts[lo], pts[hi]
        return w1 + (w2 - w1) * (media_seconds - t1) / (t2 - t1) if t2 != t1 else w1

    return f


def _rate(series, epoch0):
    """媒体秒数 / 墙钟秒数（1.0 = 实时；>1 = 正在追赶积压）"""
    if len(series) < 2:
        return None
    w0, m0 = epoch0 + series[0][0], series[0][1]
    w1, m1 = epoch0 + series[-1][0], series[-1][1]
    return (m1 - m0) / (w1 - w0) if w1 > w0 else None


def run_analyze(args):
    docs = []
    for path in args.inputs:
        with open(path, encoding="utf-8") as f:
            docs.append(json.load(f))
    sockets = [d for d in docs if d.get("mode") == "socket" and d.get("tag_series")]
    live = next((d for d in docs if d.get("mode") == "livelens"), None)
    decode = next((d for d in docs if d.get("mode") == "decode"), None)
    if not sockets:
        return {"error": "需要至少一个 socket 结果作为参照"}

    ref = sockets[0]
    f_ref = _interp_from_tags(ref["tag_series"], ref["epoch0"])

    out = {
        "mode": "analyze",
        "reference_socket": ref.get("cdn"),
        "ref_media_rate": _rate(ref["tag_series"], ref["epoch0"]),
        "ref_implied_origin_epoch": ref.get("implied_stream_origin_epoch"),
        "ref_anchor_delta_s": ref.get("anchor_delta_s"),
        "ref_tag_arrival_gap_ms": ref.get("tag_arrival_gap_ms"),
    }

    # 控制项：第二个 socket 连接对同一内容时间轴的到达时刻差 = 方法噪声下限
    if len(sockets) > 1:
        f2 = _interp_from_tags(sockets[1]["tag_series"], sockets[1]["epoch0"])
        diffs = []
        for w, ts in ref["tag_series"]:
            m = ts / 1000.0
            a, b = f_ref(m), f2(m)
            if a is not None and b is not None:
                diffs.append((b - a) * 1000.0)
        out["control_socket_to_socket_ms"] = stats(diffs)

    def attribution(label, points, epoch0):
        """points: [(wall_rel, media_seconds)] → 相对 socket 到达时刻的滞后"""
        deltas = []
        for w, m in points:
            base = f_ref(m)
            if base is not None:
                deltas.append(((epoch0 + w) - base) * 1000.0)
        return (label, deltas)

    if decode:
        _, dl = attribution("decode", decode.get("series") or [], decode["epoch0"])
        out["decode_vs_socket_ms"] = stats(dl)

    if live:
        if live.get("decode_pts_series"):
            # PTS_LOG 已是 (绝对 epoch, pts)
            pts = [(0.0, p) for _, p in live["decode_pts_series"]]
            base = [(time.time(), 0.0)]
            deltas = []
            for ep, p in live["decode_pts_series"]:
                b = f_ref(p)
                if b is not None:
                    deltas.append((ep - b) * 1000.0)
            out["livelens_decoded_vs_socket_ms"] = stats(deltas)
        if live.get("display_pts_series"):
            deltas = []
            for w, p in live["display_pts_series"]:
                b = f_ref(p)
                if b is not None:
                    deltas.append(((live["epoch0"] + w) - b) * 1000.0)
            out["livelens_displayed_vs_socket_ms"] = stats(deltas)
            if live.get("decode_pts_series"):
                out["livelens_decoded_fps"] = (
                    len(live["decode_pts_series"]) / max(1e-6, args.secs))
        out["livelens_internal_ms"] = live.get("buffer_to_display_latency_ms")
        out["livelens_set_frame_ms"] = live.get("set_frame_ms")
        out["livelens_paint_ms"] = live.get("paint_ms")
        out["livelens_skipped_frames"] = live.get("skipped_frames")
        out["livelens_max_media_rate"] = (
            _rate(live.get("decode_pts_series") or [], live["epoch0"])
            if live.get("decode_pts_series") else None)
    return out


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["socket", "decode", "livelens", "analyze"])
    ap.add_argument("--url", default="")
    ap.add_argument("--secs", type=float, default=120.0)
    ap.add_argument("--with-showinfo", action="store_true",
                    help="livelens 模式：给生产 reader 注入 -vf showinfo（诊断子类）")
    ap.add_argument("--via-proxy", action="store_true",
                    help="livelens 模式：候选地址换成本地 FLV 代理，"
                         "以便在同一连接上差分出客户端自身延迟")
    ap.add_argument("--inputs", nargs="*", default=[],
                    help="analyze 模式：要一起分析的探针 JSON（socket/decode/livelens）")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.mode == "socket":
        res = run_socket(args)
    elif args.mode == "decode":
        res = run_decode(args)
    elif args.mode == "livelens":
        res = run_livelens(args)
    else:
        res = run_analyze(args)

    text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("[done] -> %s" % args.out)
    summary = {k: v for k, v in res.items() if k != "series" and k != "samples"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
