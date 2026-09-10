# -*- coding: utf-8 -*-
"""
性能监控 (Phase 1 - Step 6)

功能：
    统计 FPS、延迟、分辨率、运行时间、帧数、FFmpeg 状态

延迟计算：
    capture_time  — FrameBridge 写入 buffer 时记录
    display_time  — GUI 显示帧时记录
    latency_ms   = display_time - capture_time

    当前延迟：最近一次帧的延迟
    平均延迟：指数移动平均（EMA），无需缓存历史帧

禁止：
    queue
    历史帧列表
"""

import logging
import threading
import time

logger = logging.getLogger("DouyinLowLatencyViewer.utils.performance")

__all__ = ["PerformanceMonitor"]


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

        # ---- FPS 统计 ----
        self._display_count: int = 0           # 已显示帧总数
        self._fps_last_time: float = 0.0      # 上次 FPS 计算时间
        self._fps_last_count: int = 0          # 上次 FPS 计算时的显示帧数
        self._fps: float = 0.0                 # 当前 FPS

        # ---- 运行时间 ----
        self._start_time: float = 0.0          # 监控启动时间

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
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

    def reset(self):
        """重置所有统计（断开连接时调用）"""
        with self._lock:
            self._capture_time = 0.0
            self._current_latency_ms = 0.0
            self._avg_latency_ms = 0.0
            self._has_latency = False
            self._display_count = 0
            self._fps_last_time = 0.0
            self._fps_last_count = 0
            self._fps = 0.0
            self._start_time = 0.0

    # ------------------------------------------------------------------
    # 数据采集
    # ------------------------------------------------------------------
    def on_capture(self):
        """
        帧采集事件：FrameBridge 写入 buffer 时调用

        记录当前时间作为 capture_time，供后续计算延迟
        """
        now = time.perf_counter()
        with self._lock:
            self._capture_time = now

    def on_display(self):
        """
        帧显示事件：GUI 显示帧时调用

        计算当前延迟 = display_time - capture_time
        更新 EMA 平均延迟
        累计显示帧数，刷新 FPS
        """
        now = time.perf_counter()
        with self._lock:
            # 计算延迟
            if self._capture_time > 0.0:
                latency_ms = (now - self._capture_time) * 1000.0
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
            return {
                "fps": self._fps,
                "current_latency_ms": self._current_latency_ms,
                "avg_latency_ms": self._avg_latency_ms,
                "display_frames": self._display_count,
                "runtime_s": runtime,
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
