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

from utils.diag_trace import TRACE, S_BRIDGE, S_CONSUME

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
        #: 当前槽位中帧的来源帧号（FFmpegReader.frame_id），仅用于 H1 诊断关联
        self._seq: int | None = None
        self._frame_id: int = 0
        self._update_count: int = 0  # [DEBUG]
        #: 单槽覆盖导致被丢掉的帧数：update() 覆盖掉了一帧**从未被 get() 读过**
        #: 的帧时 +1。只统计，不改变覆盖行为。
        self._dropped_count: int = 0
        #: 当前格子里这一帧是否已经被 get() 读过（用于判定下一次覆盖是否丢帧）
        self._consumed: bool = False

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def update(self, frame: np.ndarray, seq: int | None = None) -> None:
        """
        写入最新帧（覆盖旧帧 = 主动丢帧）

        :param frame: numpy.ndarray (H, W, 3) BGR
        :param seq:   该帧的来源帧号（FFmpegReader.frame_id）。仅用于 H1 诊断
                      把同一帧在各阶段关联起来；不参与任何覆盖/丢弃决策。
        """
        with self._lock:
            # 旧帧还没被读走就被这次写入覆盖 => 记一次丢帧。
            # 这是**计数**，覆盖行为本身与从前逐字相同。
            if self._frame is not None and not self._consumed:
                self._dropped_count += 1
            self._frame = frame
            self._seq = seq
            self._frame_id += 1
            self._update_count += 1
            self._consumed = False
            dropped = self._dropped_count
        if TRACE.enabled:
            TRACE.ext_overwritten = dropped
            TRACE.note(S_BRIDGE, seq)
            TRACE.bump("c_bridge")
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
            if self._frame is not None:
                self._consumed = True
            return self._frame

    def get_with_seq(self):
        """读取当前最新帧及其来源帧号。

        :return: (frame, seq)；无帧时 (None, None)
        """
        with self._lock:
            frame = self._frame
            seq = self._seq
            if frame is not None:
                self._consumed = True
        if TRACE.enabled and frame is not None:
            # 仅记录**真实消费**：空缓冲轮询（frame=None）不是关联事件，
            # 计入 no_seq 会把 assoc_fail 语义污染成"轮询空转计数"。
            TRACE.note(S_CONSUME, seq)
            TRACE.bump("c_consume")
        return frame, seq

    def clear(self) -> None:
        """清空缓冲，重置帧计数。

        与既有行为一致：只重置 ``frame_id``（``update_count`` 本来就不重置），
        统计用的 ``dropped_count`` 同样**不**重置——它是整个会话的累计值。
        """
        with self._lock:
            self._frame = None
            self._seq = None
            self._frame_id = 0
            self._consumed = False

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

    @property
    def dropped_count(self) -> int:
        """本会话因单槽覆盖而被丢掉的帧数（从未被读取就遭覆盖）。

        设计上「只留最新帧」本就会丢掉大量帧，所以**绝对值大是正常的**；
        要看的是它与 :attr:`update_count` 的比值随时间的**变化**——
        卡顿时比值会升高，这正是真实直播验收里区分「视频侧卡」与
        「OCR 侧慢」需要的信号。
        """
        with self._lock:
            return self._dropped_count

    @property
    def update_count(self) -> int:
        """本会话累计写入的帧数（丢帧率的分母）。"""
        with self._lock:
            return self._update_count
