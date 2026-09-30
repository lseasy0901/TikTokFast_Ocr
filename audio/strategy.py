# -*- coding: utf-8 -*-
"""
音频缓冲策略：Sync（同步）与 LowLatency（极速）

策略只管"缓冲区里放多少、丢不丢、什么时候可以播"，不管传输与输出：
    - on_chunk(data)  收一块 PCM（字节流追加）
    - take(n)         取出最多 n 字节供输出播放
    - playable()      当前是否有可播数据
    - should_start()  是否已积攒到起播水位（首次起播 / 欠载后重灌）

线程约定：策略实例只被音频 worker 线程访问，内部不加锁。
"""

from audio.models import AudioMode, ms_to_bytes

__all__ = ["SyncStrategy", "LowLatencyStrategy", "create_strategy"]


class _BaseStrategy:
    """bytearray 环形语义的线性缓冲（头部消费、尾部追加、头部可裁剪）"""

    name = "base"

    def __init__(self):
        self._buf = bytearray()

    def on_chunk(self, data: bytes) -> None:
        self._buf.extend(data)

    def take(self, n: int) -> bytes:
        """取出最多 n 字节；不足 n 时给多少取多少"""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def pushfront(self, data: bytes) -> None:
        """把未写完的数据放回头部（保持 PCM 顺序！绝不能追加到尾部）"""
        if data:
            self._buf[:0] = data

    def playable(self) -> int:
        return len(self._buf)

    def clear(self) -> None:
        self._buf.clear()


class SyncStrategy(_BaseStrategy):
    """同步模式：优先"不断音"。

    - 起播水位 80ms：攒够再开播，之后只要缓冲非空就连续输出；
    - 欠载后重新等水位，宁可静音几十毫秒也不输出断断续续的声音；
    - 安全上限 2s：消费端长时间停滞时的保护性丢弃（正常永远到不了）。
    """

    name = "sync"

    START_MS = 80
    MAX_MS = 2000

    def __init__(self):
        super().__init__()
        self._armed = True            # True = 在等起播水位
        self._start = ms_to_bytes(self.START_MS)
        self._max = ms_to_bytes(self.MAX_MS)

    def on_chunk(self, data: bytes) -> None:
        super().on_chunk(data)
        if len(self._buf) > self._max:
            # 安全阀：丢掉最旧的，只保留上限（正常播放永远触发不到）
            del self._buf[:-self._max]

    def should_start(self) -> bool:
        if not self._armed:
            return True
        if len(self._buf) >= self._start:
            self._armed = False
            return True
        return False

    def on_underrun(self) -> None:
        """输出欠载：重新武装，等重新灌到起播水位再继续"""
        self._armed = True
        self._buf.clear()


class LowLatencyStrategy(_BaseStrategy):
    """极速模式：优先"最新声音"。

    - 起播水位 30ms：几乎立刻开播；
    - 缓冲超过 120ms 立即从头部丢弃（丢旧优先于堆积延迟）；
    - 欠载不重灌，来什么播什么（接受偶发静音换取低延迟）。
    """

    name = "low_latency"

    START_MS = 30
    MAX_MS = 120

    def __init__(self):
        super().__init__()
        self._start = ms_to_bytes(self.START_MS)
        self._max = ms_to_bytes(self.MAX_MS)

    def on_chunk(self, data: bytes) -> None:
        super().on_chunk(data)
        if len(self._buf) > self._max:
            del self._buf[:-self._max]

    def should_start(self) -> bool:
        return len(self._buf) >= self._start

    def on_underrun(self) -> None:
        pass


def create_strategy(mode: AudioMode) -> _BaseStrategy:
    """按模式创建策略实例（模式切换 = 换新策略实例）"""
    if mode == AudioMode.LOW_LATENCY:
        return LowLatencyStrategy()
    return SyncStrategy()
