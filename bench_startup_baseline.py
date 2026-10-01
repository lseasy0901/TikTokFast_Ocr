# -*- coding: utf-8 -*-
"""
启动延迟基线测量（迁移审计第 0 步：先量化，再改代码）

目的：
    把「LiveLens 打开同一直播间比 live-player 慢约 1 秒」分解成可归因的分段：
        解析总耗时（含其中串行的 ffprobe 分辨率探测）
        + FFmpeg 启动到首帧耗时
        = 端到端加载耗时
    这是「改前基线」。改造（P0-4 探测上限 / P0-1 fflags / P0-2 reconnect）落地后
    用同一条命令复测，两份数据对比即为验收证据。

用法（需要真实在播的直播间 URL）：
    python bench_startup_baseline.py https://live.douyin.com/xxxxxx --runs 3
    python bench_startup_baseline.py https://www.huya.com/xxxxxx --runs 3 --no-cuda

输出：
    每轮一行：resolve_ms / 其中 ffprobe_ms / first_frame_ms / total_ms
    末尾给中位数汇总。全程 INFO 日志同步打印请求链细节（Cookie/页面/探测）。

注意：
    - 直播间必须在播，否则解析直接报"未开播"。
    - 请与 live-player 在相近时间对同一房间交替测量（CDN 与房间热度随时段漂移）。
    - 本脚本只读流、不渲染、不 OCR；测量完立即断开。
"""

import argparse
import logging
import statistics
import sys
import time

from stream.models import StreamInfo
from stream.resolver import StreamResolver, StreamResolveError
from stream.ffmpeg_reader import FFmpegReader
from stream import video_info as video_info_mod

logger = logging.getLogger("bench_startup_baseline")


class _ProbeTimer:
    """包一层 get_video_info，把每次 ffprobe 的耗时收集起来（不改其行为）"""

    def __init__(self):
        self.elapsed_ms = []
        self._orig = video_info_mod.get_video_info

    def install(self):
        timer = self

        def wrapped(stream_url, timeout=10):
            t0 = time.perf_counter()
            try:
                return timer._orig(stream_url, timeout=timeout)
            finally:
                timer.elapsed_ms.append((time.perf_counter() - t0) * 1000.0)

        video_info_mod.get_video_info = wrapped
        video_info_mod.build_probe_cmd  # 保持模块引用活跃（明确不改命令本身）

    def uninstall(self):
        video_info_mod.get_video_info = self._orig


def measure_once(resolver: StreamResolver, url: str, use_cuda: bool,
                 timeout_s: float = 25.0) -> dict:
    """按生产顺序完整跑一次：解析（含探测）→ reader 首帧。返回分段耗时。"""
    probe = _ProbeTimer()
    probe.install()
    try:
        t0 = time.perf_counter()
        info = resolver.resolve(url)
        resolve_ms = (time.perf_counter() - t0) * 1000.0
    except StreamResolveError as e:
        raise SystemExit(f"[失败] 解析失败：{e}")
    finally:
        probe.uninstall()

    assert isinstance(info, StreamInfo)
    candidate = info.selected
    if candidate is None or not candidate.url:
        raise SystemExit("[失败] 解析成功但没有可用候选流")

    reader = FFmpegReader(
        candidate.url,
        width=info.width,
        height=info.height,
        use_cuda=use_cuda,
        http_headers=candidate.http_headers or None,
        audio_enabled=False,          # 测量脚本不接音频，贴近"只看画面"的最小路径
    )
    reader.start()
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if reader.error:
                raise SystemExit(f"[失败] 播放失败：{reader.error}")
            if reader.first_frame_ms is not None:
                break
            time.sleep(0.01)
        else:
            raise SystemExit(f"[失败] {timeout_s:.0f}s 内未收到首帧")
    finally:
        first_frame_ms = reader.first_frame_ms
        reader.stop()

    ffprobe_ms = probe.elapsed_ms[-1] if probe.elapsed_ms else 0.0
    return {
        "platform": info.platform,
        "quality": candidate.quality or "-",
        "resolve_ms": resolve_ms,
        "ffprobe_ms": ffprobe_ms,
        "first_frame_ms": first_frame_ms,
        "total_ms": resolve_ms + first_frame_ms,
    }


def main():
    parser = argparse.ArgumentParser(description="LiveLens 启动延迟基线测量")
    parser.add_argument("room_url", help="直播间 URL（必须在播）")
    parser.add_argument("--runs", type=int, default=3, help="测量轮数（默认 3）")
    parser.add_argument("--cooldown", type=float, default=2.0,
                        help="两轮之间的间隔秒数（默认 2，避免 CDN 热连接干扰）")
    parser.add_argument("--no-cuda", action="store_true", help="禁用 CUDA 硬解")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    rows = []
    resolver = StreamResolver()
    for i in range(1, args.runs + 1):
        print(f"\n===== 第 {i}/{args.runs} 轮 =====")
        row = measure_once(resolver, args.room_url, use_cuda=not args.no_cuda)
        rows.append(row)
        print("[结果] platform={platform} quality={quality} "
              "resolve={resolve_ms:.0f}ms (ffprobe {ffprobe_ms:.0f}ms) "
              "first_frame={first_frame_ms:.0f}ms total={total_ms:.0f}ms".format(**row))
        if i < args.runs:
            time.sleep(args.cooldown)

    print("\n===== 汇总（中位数）=====")
    for key, label in (("resolve_ms", "解析(含探测)"),
                       ("ffprobe_ms", "  其中ffprobe"),
                       ("first_frame_ms", "FFmpeg到首帧"),
                       ("total_ms", "端到端加载")):
        values = [r[key] for r in rows]
        print(f"{label:12s} {statistics.median(values):8.0f} ms   "
              f"(min {min(values):.0f} / max {max(values):.0f})")
    print("\n把这段汇总发给审计流程：这就是改造前基线（改后用同一命令复测对比）。")


if __name__ == "__main__":
    main()
