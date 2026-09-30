# -*- coding: utf-8 -*-
"""
AudioWorker：音频播放线程

职责：从队列收 PCM → 策略缓冲 → 写入输出设备。一条线程跑到底，
不创建任何 Qt 跨线程对象（输出设备在本线程内打开/关闭）。

与上游的契约（由 AudioService 维持）：
    - feed(data) 永不阻塞：队列满时丢最旧的块（保最新、保视频不反压）
    - stop() 之后本线程在 stop_timeout 秒内退出
"""

import logging
import queue
import threading
import time

from audio.models import AudioConfig, AudioStats
from audio.output import AudioOutputError
from audio.strategy import create_strategy

logger = logging.getLogger("DouyinLowLatencyViewer.audio.worker")

__all__ = ["AudioWorker"]


class AudioWorker:
    """单线程音频泵：queue → strategy → output"""

    #: 队列容量（块数）。每块 8KB ≈ 43ms；容量 = 约 2s 的吸收能力，
    #: 超过即丢旧块 —— 播放延迟由策略管，队列只做解耦。
    QUEUE_SIZE = 48
    #: stop 时等待线程退出的超时（秒）
    STOP_TIMEOUT = 3.0
    #: 每次写入输出的单片大小（字节，约 50ms @ 48k/16/2）
    WRITE_CHUNK = 9600
    #: 设备缓冲写满时的重试间隔（秒）：远小于一个音频块时长，避免欠载
    RETRY_INTERVAL_S = 0.004
    #: 无数据时的取块等待（秒）；期间输出设备靠已写入的缓冲维持播放
    IDLE_POLL_S = 0.05

    def __init__(self, config: AudioConfig, stats: AudioStats,
                 output_factory, state_cb=None):
        """
        :param config: 共享配置（音量/模式），由 service 持有
        :param stats: 共享统计
        :param output_factory: () -> output 对象（open/write/volume/close）。
                     生产环境传 QtAudioOutput，测试传 Fake。
        :param state_cb: 回调 cb(ok: bool, message: str)，open 成功/失败时调用。
                     在本线程调用，接收方负责线程安全。
        """
        self._config = config
        self._stats = stats
        self._output_factory = output_factory
        self._state_cb = state_cb

        self._queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._running = False
        self._thread: threading.Thread | None = None

    # ---- 上游接口（service 调用） ----
    def feed(self, data: bytes) -> None:
        """投入一块 PCM；队列满时丢最旧块。永不阻塞、永不抛异常。"""
        if not self._running:
            return
        dropped = False
        while True:
            try:
                self._queue.put_nowait(data)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()   # 丢最旧
                    dropped = True
                except queue.Empty:
                    break
        if dropped:
            self._stats.chunks_dropped += 1

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="audio-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止线程并释放输出（幂等）"""
        self._running = False
        thread = self._thread
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=self.STOP_TIMEOUT)
        self._thread = None
        # 唤醒可能在 get() 上等待的线程
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    # ---- 内部 ----
    def _run(self):
        """线程主循环：打开输出 → 泵数据 → 退出时释放"""
        strategy = create_strategy(self._config.mode)
        output = None
        try:
            output = self._output_factory()
            output.volume = self._config.volume
            output.open()
            if self._state_cb:
                self._state_cb(True, "")
        except AudioOutputError as e:
            logger.error("[AUDIO] 输出打开失败: %s", e)
            if self._state_cb:
                self._state_cb(False, str(e))
            return
        except Exception as e:  # noqa: BLE001 - 任何输出异常都不得外溢
            logger.error("[AUDIO] 输出打开异常: %s", e)
            if self._state_cb:
                self._state_cb(False, f"输出打开异常: {e}")
            return

        last_mode = self._config.mode
        try:
            while self._running:
                # 模式切换 = 换新策略（缓冲语义完全不同，不做迁移）
                mode = self._config.mode
                if mode is not last_mode:
                    strategy = create_strategy(mode)
                    last_mode = mode

                # 音量变化即时同步到输出（GUI 线程改 config，本线程探测生效）
                vol = self._config.volume
                if vol != output.volume:
                    output.volume = vol

                # 1) 无事可做（缓冲空 / 未达起播水位）→ 在队列上阻塞等数据。
                #    这是循环的**唯一空闲阻塞点**：任何"有部分数据但不该播"或
                #    "已排空但设备还没欠载"的状态都不允许空转——否则本线程
                #    热自旋霸占 GIL，把视频读取线程饿掉帧（FPS 崩到个位数
                #    的历史事故，见 AudioQualityOptimization_Report.md 第 8 节）。
                if strategy.playable() == 0 or not strategy.should_start():
                    try:
                        chunk = self._queue.get(timeout=self.IDLE_POLL_S)
                    except queue.Empty:
                        # 醒来无事：只在真欠载时交还策略决定是否重灌
                        if strategy.playable() == 0 and self._queue.empty() \
                                and output.is_underrun:
                            strategy.on_underrun()
                            self._stats.underruns += 1
                        continue
                    strategy.on_chunk(chunk)
                    while True:     # 顺手排空队列，平滑水位
                        try:
                            strategy.on_chunk(self._queue.get_nowait())
                        except queue.Empty:
                            break

                # 2) 泵：把策略缓冲写进输出。设备缓冲写满时**原地短退避重试**
                #    （sleep 释放 GIL，不构成自旋），绝不回头等新数据——否则
                #    设备缓冲（毫秒级）一被播完就是欠载爆音。
                while self._running and strategy.playable() > 0 \
                        and strategy.should_start():
                    data = strategy.take(min(self.WRITE_CHUNK,
                                             strategy.playable()))
                    written = output.write(data)
                    self._stats.bytes_played += written
                    if written < len(data):
                        # 未写完部分放回头部（保持 PCM 顺序）
                        strategy.pushfront(data[written:])
                        time.sleep(self.RETRY_INTERVAL_S)
        except Exception as e:  # noqa: BLE001 - 播放循环异常只记日志
            logger.error("[AUDIO] 播放循环异常（音频停止，视频不受影响）: %s", e,
                         exc_info=True)
            if self._state_cb:
                self._state_cb(False, f"播放异常: {e}")
        finally:
            if output is not None:
                try:
                    output.close()
                except Exception:  # noqa: BLE001
                    pass
