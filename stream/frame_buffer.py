# -*- coding: utf-8 -*-
"""
单帧缓冲 (Phase 1 - Step 5)

设计：
    只保留最新一帧，旧帧直接丢弃。
    宁可丢帧，绝不累计延迟。

接口：
    update(frame)  — 写入最新帧（FFmpeg 读取线程调用）
    get()          — 读取最新帧（GUI 线程调用，返回 None 表示无帧）
    clear()        — 清空缓冲

线程安全：
    threading.Lock 保护读写，无竞争。

禁止：
    queue.Queue
    历史帧列表
"""

import logging
import threading

import numpy as np

logger = logging.getLogger("DouyinLowLatencyViewer.stream.frame_buffer")

__all__ = ["LatestFrameBuffer"]


class LatestFrameBuffer:
    """
    线程安全的单帧缓冲：只保留最新一帧

    - 写入侧（FFmpeg 读取线程）：update(frame) 覆盖旧帧
    - 读取侧（GUI 线程）：get() 返回最新帧或 None
    - frame_id 单调递增，可用于判断是否有新帧
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_id: int = 0
        self._update_count: int = 0  # [DEBUG]

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def update(self, frame: np.ndarray) -> None:
        """
        写入最新帧（覆盖旧帧 = 主动丢帧）

        :param frame: numpy.ndarray (H, W, 3) BGR
        """
        with self._lock:
            self._frame = frame
            self._frame_id += 1
            self._update_count += 1
        if self._update_count <= 3 or self._update_count % 100 == 0:
            logger.info(
                "[BUFFER] frame received: %dx%d update_count=%d",
                frame.shape[1], frame.shape[0], self._update_count,
            )

    def get(self) -> np.ndarray | None:
        """
        读取当前最新帧

        :return: numpy.ndarray (H, W, 3) BGR；无帧时返回 None
        """
        with self._lock:
            return self._frame

    def clear(self) -> None:
        """清空缓冲，重置帧计数"""
        with self._lock:
            self._frame = None
            self._frame_id = 0

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------
    @property
    def frame_id(self) -> int:
        """当前帧序号（单调递增，可用于判断是否有新帧）"""
        with self._lock:
            return self._frame_id

    @property
    def has_frame(self) -> bool:
        """缓冲中是否有帧"""
        with self._lock:
            return self._frame is not None
