# -*- coding: utf-8 -*-
"""
性能监控 (Phase 1 - Step 6)

功能：
    统计 FPS、延迟、分辨率、运行时间、帧数、FFmpeg 状态

延迟计算：
    capture_time  — FrameBridge 写入 buffer 时记录
    display_time  — GUI 显示帧时记录
    latency_ms   = display_time - capture_time

    即"帧到达 buffer → GUI 消费"的等待。帧消费由 frameReady 通知驱动后
    （Phase B-2），该值不再包含固定轮询周期的等待，指标含义不变、量级下降。

    当前延迟：最近一次帧的延迟
    平均延迟：指数移动平均（EMA），无需缓存历史帧

禁止：
    queue
    历史帧列表
"""

import collections
import logging
import threading
import time

logger = logging.getLogger("DouyinLowLatencyViewer.utils.performance")

__all__ = ["PerformanceMonitor", "SampleWindow"]

#: 环形采样窗口默认容量（Phase B-1：只保留最近若干个样本，不逐帧留日志）
DEFAULT_SAMPLE_CAPACITY = 300

# ---- Phase B-1 跳帧归类阈值 ----
# 这些阈值只用于把观测读数归类成"稳定 / 持续追赶 / 突发"，
# 与"真实直播延迟"无关，不得据此推断端到端延迟。
_SKIP_BURST_FRAMES_PER_S = 30.0       # 单个采样周期内跳帧数 ≥ 该值 → 突发
_SKIP_SUSTAINED_FRAMES_PER_S = 1.0    # 近期平均跳帧率 ≥ 该值 → 持续追赶
_SKIP_RECENT_SAMPLES = 10             # 判定"持续"时回看的采样周期数


class SampleWindow:
    """固定长度环形采样窗口 + 轻量分位数统计。

    - 只保留最近 ``capacity`` 个样本用于分位数计算（deque 环形复用）；
    - ``count`` / ``mean`` / ``max`` 为全量累计，因此 ``max`` 是历史峰值而非窗口内峰值；
    - 刻意不加锁：由持有者（PerformanceMonitor）统一在锁内调用，避免额外锁竞争。
    """

    __slots__ = ("_samples", "_count", "_total", "_max")

    def __init__(self, capacity: int = DEFAULT_SAMPLE_CAPACITY):
        self._samples = collections.deque(maxlen=capacity)
        self._count = 0
        self._total = 0.0
        self._max = 0.0

    def add(self, value) -> None:
        """记录一个样本（O(1)，超出容量时自动淘汰最旧样本）"""
        v = float(value)
        self._samples.append(v)
        self._count += 1
        self._total += v
        if v > self._max:
            self._max = v

    def recent(self, n: int) -> list:
        """返回最近 n 个样本（按时间升序）"""
        if n <= 0:
            return []
        return list(self._samples)[-n:]

    def reset(self) -> None:
        self._samples.clear()
        self._count = 0
        self._total = 0.0
        self._max = 0.0

    def stats(self) -> dict:
        """返回 count / samples / mean / p50 / p95 / p99 / max（无样本时全为 0）"""
        n = len(self._samples)
        if n == 0:
            return {
                "count": self._count, "samples": 0, "mean": 0.0,
                "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0,
            }
        ordered = sorted(self._samples)

        def _pct(p: float) -> float:
            return ordered[int((len(ordered) - 1) * p)]

        count = self._count
        return {
            "count": count,
            "samples": n,
            "mean": self._total / count if count else 0.0,
            "p50": _pct(0.50),
            "p95": _pct(0.95),
            "p99": _pct(0.99),
            "max": self._max,
        }


class PerformanceMonitor:
    """
    轻量级性能监控器

    - on_capture(): FrameBridge 写入 buffer 时调用，记录 capture_time
    - on_display(): GUI 显示帧时调用，计算延迟，累计显示帧数
    - snapshot():   返回当前所有性能指标的快照字典

    线程安全：threading.Lock 保护
    """

    # EMA 平滑因子（越大越跟踪即时值，越小越平滑）
    _EMA_ALPHA = 0.3

    def __init__(self):
        self._lock = threading.Lock()

        # ---- 延迟统计 ----
        self._capture_time: float = 0.0       # 最近一次帧的 capture 时间
        self._current_latency_ms: float = 0.0  # 当前延迟（最新帧）
        self._avg_latency_ms: float = 0.0     # 平均延迟（EMA）
        self._has_latency: bool = False        # 是否已有延迟数据
        #: H1 修正：按来源帧号记录的采集时间（有界，只保留最近若干帧）。
        #: 修复"单个 _capture_time 标量"的关联缺陷：当某帧在写入后、被 GUI
        #: 取走前已被后续帧覆盖时，标量读数只能给出"自最近一次写入以来"的
        #: 时间，**系统性低估**该帧的真实驻留。这里按 seq 精确配对。
        self._capture_by_seq: "collections.OrderedDict[int, float]" = \
            collections.OrderedDict()
        self._capture_seq_capacity: int = 64

        # ---- FPS 统计 ----
        self._display_count: int = 0           # 已显示帧总数
        self._fps_last_time: float = 0.0      # 上次 FPS 计算时间
        self._fps_last_count: int = 0          # 上次 FPS 计算时的显示帧数
        self._fps: float = 0.0                 # 当前 FPS

        # ---- 运行时间 ----
        self._start_time: float = 0.0          # 监控启动时间

        # ---- Phase B-1：播放链路观测（渲染耗时 / 管道积压 / 跳帧速率） ----
        # 只描述 FFmpeg 输出 → 管道 → buffer → set_frame → paintEvent 这段本地链路，
        # 不代表抖音直播端到端延迟。
        self._set_frame = SampleWindow()       # set_frame 耗时 (ms)
        self._paint = SampleWindow()           # 完整 paintEvent 耗时 (ms)
        self._produced = SampleWindow()        # 输入帧率 (fps)
        self._skip_rate = SampleWindow()       # 跳帧速率 (frames/s)
        self._paint_count: int = 0             # paintEvent 总次数
        self._paint_drawn: int = 0             # 其中真正绘制了视频帧的次数
        self._input_frame_changes: int = 0     # 输入 frame_id 变化总次数
        self._skip_rate_last: float = 0.0      # 最近一个采样周期的跳帧速率
        self._skip_delta_last: int = 0         # 最近一个采样周期新增跳帧数
        self._skip_delta_max: int = 0          # 单周期新增跳帧数的历史峰值
        self._skip_total: int | None = None    # 上次采样的累计跳帧数（None=未基准化）
        self._skip_sample_time: float = 0.0
        self._produced_total: int | None = None
        self._produced_sample_time: float = 0.0
        self._produced_fps: float = 0.0
        self._pipe_stats: dict = {}            # 由 FFmpegReader 汇总后写入

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _reset_instrumentation_locked(self) -> None:
        """重置 Phase B-1 观测状态（调用方需已持有 self._lock）"""
        self._set_frame.reset()
        self._paint.reset()
        self._produced.reset()
        self._skip_rate.reset()
        self._paint_count = 0
        self._paint_drawn = 0
        self._input_frame_changes = 0
        self._skip_rate_last = 0.0
        self._skip_delta_last = 0
        self._skip_delta_max = 0
        self._skip_total = None
        self._skip_sample_time = 0.0
        self._produced_total = None
        self._produced_sample_time = 0.0
        self._produced_fps = 0.0
        self._pipe_stats = {}

    def start(self):
        """启动监控（连接成功时调用）"""
        with self._lock:
            self._start_time = time.perf_counter()
            self._fps_last_time = self._start_time
            self._display_count = 0
            self._fps_last_count = 0
            self._fps = 0.0
            self._current_latency_ms = 0.0
            self._avg_latency_ms = 0.0
            self._has_latency = False
            self._capture_time = 0.0
            self._capture_by_seq.clear()
            self._reset_instrumentation_locked()

    def reset(self):
        """重置所有统计（断开连接时调用）"""
        with self._lock:
            self._capture_time = 0.0
            self._capture_by_seq.clear()
            self._current_latency_ms = 0.0
            self._avg_latency_ms = 0.0
            self._has_latency = False
            self._display_count = 0
            self._fps_last_time = 0.0
            self._fps_last_count = 0
            self._fps = 0.0
            self._start_time = 0.0
            self._reset_instrumentation_locked()

    # ------------------------------------------------------------------
    # 数据采集
    # ------------------------------------------------------------------
    def on_capture(self, seq: int | None = None):
        """
        帧采集事件：FrameBridge 写入 buffer 时调用

        记录当前时间作为 capture_time，供后续计算延迟

        :param seq: 来源帧号（FFmpegReader.frame_id）。给出时额外按帧号记录，
                    使 on_display(seq) 能取到**这一帧自己的**采集时间，而不是
                    被后续帧覆盖后的标量。不传时行为与从前逐字相同。
        """
        now = time.perf_counter()
        with self._lock:
            self._capture_time = now
            if seq is not None:
                self._capture_by_seq[seq] = now
                while len(self._capture_by_seq) > self._capture_seq_capacity:
                    self._capture_by_seq.popitem(last=False)

    def on_display(self, seq: int | None = None):
        """
        帧显示事件：GUI 显示帧时调用

        计算当前延迟 = display_time - capture_time
        更新 EMA 平均延迟
        累计显示帧数，刷新 FPS

        :param seq: 来源帧号。给出且命中记录时，用**该帧自己的**采集时间计算
                    驻留（正确的口径）；否则回退到既有标量语义。
        """
        now = time.perf_counter()
        with self._lock:
            captured = None
            if seq is not None:
                captured = self._capture_by_seq.pop(seq, None)
            if captured is None:
                captured = self._capture_time if self._capture_time > 0.0 else None
            # 计算延迟
            if captured is not None:
                latency_ms = (now - captured) * 1000.0
                # 延迟不应为负（时钟单调），钳位保护
                latency_ms = max(0.0, latency_ms)
                self._current_latency_ms = latency_ms

                # EMA 平均延迟
                if not self._has_latency:
                    self._avg_latency_ms = latency_ms
                    self._has_latency = True
                else:
                    self._avg_latency_ms = (
                        self._EMA_ALPHA * latency_ms
                        + (1.0 - self._EMA_ALPHA) * self._avg_latency_ms
                    )

            # 累计显示帧数
            self._display_count += 1

            # 刷新 FPS（每秒更新一次）
            elapsed = now - self._fps_last_time
            if elapsed >= 1.0:
                frame_delta = self._display_count - self._fps_last_count
                self._fps = frame_delta / elapsed
                self._fps_last_time = now
                self._fps_last_count = self._display_count

    # ------------------------------------------------------------------
    # Phase B-1：渲染耗时观测（由 GUI 线程调用，只记录不干预）
    # ------------------------------------------------------------------
    def on_set_frame(self, duration_ms: float) -> None:
        """记录一次 VideoWidget.set_frame() 的耗时"""
        with self._lock:
            self._set_frame.add(duration_ms)

    def on_paint_event(self, duration_ms: float, drew_frame: bool) -> None:
        """记录一次完整 paintEvent 的耗时

        :param duration_ms: 进入 paintEvent → 绘制完成的耗时
        :param drew_frame:  本次是否真正绘制了视频帧（空画面重绘也计入耗时统计）
        """
        with self._lock:
            self._paint.add(duration_ms)
            self._paint_count += 1
            if drew_frame:
                self._paint_drawn += 1

    # ------------------------------------------------------------------
    # Phase B-1：采样（在既有性能刷新周期中调用，不产生逐帧日志）
    # ------------------------------------------------------------------
    def sample_produced(self, frame_id: int) -> None:
        """采样输入帧计数：换算"输入给 GUI 的帧率"与 frame_id 变化总次数"""
        now = time.perf_counter()
        with self._lock:
            if self._produced_total is None:
                self._produced_total = frame_id
                self._produced_sample_time = now
                return
            elapsed = now - self._produced_sample_time
            delta = frame_id - self._produced_total
            self._produced_total = frame_id
            self._produced_sample_time = now
            if delta < 0:
                return          # 缓冲被重建（重连），跳过本次
            self._input_frame_changes += delta
            if elapsed > 0:
                self._produced_fps = delta / elapsed
                self._produced.add(self._produced_fps)

    def sample_skipped(self, total_skipped: int) -> None:
        """采样累计跳帧数：换算最近一个周期的跳过帧数与跳帧速率"""
        now = time.perf_counter()
        with self._lock:
            if self._skip_total is None:
                self._skip_total = total_skipped
                self._skip_sample_time = now
                return
            elapsed = now - self._skip_sample_time
            delta = total_skipped - self._skip_total
            if delta < 0:
                delta = 0       # reader 已更换或重置
            self._skip_total = total_skipped
            self._skip_sample_time = now
            self._skip_delta_last = delta
            if delta > self._skip_delta_max:
                self._skip_delta_max = delta
            if elapsed > 0:
                self._skip_rate_last = delta / elapsed
                self._skip_rate.add(self._skip_rate_last)

    def set_pipe_stats(self, stats: dict) -> None:
        """写入由 FFmpegReader 汇总的管道积压统计（None/空表示无样本）"""
        with self._lock:
            self._pipe_stats = dict(stats) if stats else {}

    def _skip_state_locked(self) -> str:
        """把跳帧读数归类（调用方需已持有 self._lock）

        stable      近若干个周期基本没有跳帧
        catching-up 跳帧速率长期非零 → 链路在持续追赶
        burst       单个周期内跳帧数超过突发阈值 → 短时大量追赶
        unknown     尚无足够样本
        """
        recent = self._skip_rate.recent(_SKIP_RECENT_SAMPLES)
        if not recent:
            return "unknown"
        if max(recent) >= _SKIP_BURST_FRAMES_PER_S:
            return "burst"
        if (sum(recent) / len(recent)) >= _SKIP_SUSTAINED_FRAMES_PER_S:
            return "catching-up"
        return "stable"

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """
        返回当前性能快照（线程安全）

        :return: dict 包含所有性能指标
        """
        with self._lock:
            runtime = (
                (time.perf_counter() - self._start_time)
                if self._start_time > 0.0 else 0.0
            )
            set_frame = self._set_frame.stats()
            paint = self._paint.stats()
            recent_skip = self._skip_rate.recent(_SKIP_RECENT_SAMPLES)
            return {
                "fps": self._fps,
                "current_latency_ms": self._current_latency_ms,
                "avg_latency_ms": self._avg_latency_ms,
                "display_frames": self._display_count,
                "runtime_s": runtime,
                # ---- Phase B-1：本地播放链路观测 ----
                # frame_id 由 LatestFrameBuffer 递增，代表"输入给 GUI 的帧"
                "input_frame_changes": self._input_frame_changes,
                "produced_fps": self._produced_fps,
                "set_frame_count": set_frame["count"],
                "set_frame_p50": set_frame["p50"],
                "set_frame_p95": set_frame["p95"],
                "set_frame_p99": set_frame["p99"],
                "set_frame_max": set_frame["max"],
                "paint_count": self._paint_count,
                "paint_drawn": self._paint_drawn,
                "paint_p50": paint["p50"],
                "paint_p95": paint["p95"],
                "paint_p99": paint["p99"],
                "paint_max": paint["max"],
                "skipped_rate": (
                    sum(recent_skip) / len(recent_skip) if recent_skip else 0.0
                ),
                "skipped_rate_last": self._skip_rate_last,
                "skipped_delta_last": self._skip_delta_last,
                "skipped_delta_max": self._skip_delta_max,
                "skipped_state": self._skip_state_locked(),
                "pipe": dict(self._pipe_stats),
            }

    @property
    def fps(self) -> float:
        with self._lock:
            return self._fps

    @property
    def current_latency_ms(self) -> float:
        with self._lock:
            return self._current_latency_ms

    @property
    def avg_latency_ms(self) -> float:
        with self._lock:
            return self._avg_latency_ms
