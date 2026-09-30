# -*- coding: utf-8 -*-
"""
AudioService：音频生命周期门面

唯一对外入口。GUI（main_window）只跟这个类说话：
    start / stop / restart / set_volume / set_mode

设计约束：
    - 音频失败绝不抛出：所有异常在内部转成 ERROR 状态回调
    - 不依赖 Qt（回调交付状态；GUI 侧用 Signal 转线程）
    - 不持有 FFmpeg 进程：PCM 由 FFmpegReader 的回调送进来，本类只消费
      （feed 的 data 语义与 FFmpegReader.audio_pcm_callback 相同：
       bytes = PCM 块，b"" = 音频流结束，None = 无音频轨）
"""

import logging
import threading

from audio.models import AudioConfig, AudioMode, AudioState, AudioStats
from audio.output import QtAudioOutput
from audio.worker import AudioWorker

logger = logging.getLogger("DouyinLowLatencyViewer.audio.service")

__all__ = ["AudioService"]


class AudioService:
    """音频功能门面：状态机 + worker 生命周期 + GUI 配置入口"""

    def __init__(self, output_factory=None, volume: float = 0.8,
                 mode: AudioMode = AudioMode.LOW_LATENCY):
        """
        :param output_factory: 输出工厂（None = 生产环境 QtAudioOutput）；
                     测试注入 Fake 以脱离声卡。
        """
        self._config = AudioConfig(volume=volume, mode=mode)
        self._stats = AudioStats()

        self._state = AudioState.IDLE
        self._state_cb = None            # GUI 侧注册的回调（可能从音频线程调用）
        self._state_lock = threading.Lock()

        self._worker: AudioWorker | None = None
        self._output_factory = output_factory or self._default_output_factory

    def _default_output_factory(self) -> QtAudioOutput:
        return QtAudioOutput(
            volume=self._config.volume,
            sample_rate=self._config.sample_rate,
            channels=self._config.channels,
            sink_buffer_ms=self._config.sink_buffer_ms,
        )

    # ---- 对外：状态订阅 ----
    def set_state_callback(self, cb) -> None:
        """注册状态回调 cb(AudioState, message: str)。

        回调可能从音频线程调用；GUI 侧必须自行转线程（如经 Qt Signal）。
        """
        self._state_cb = cb

    def _set_state(self, state: AudioState, message: str = "") -> None:
        with self._state_lock:
            self._state = state
        cb = self._state_cb
        if cb is not None:
            try:
                cb(state, message)
            except Exception:  # noqa: BLE001 - 回调异常不得影响状态机
                logger.warning("[AUDIO] 状态回调异常（已忽略）", exc_info=True)

    @property
    def state(self) -> AudioState:
        with self._state_lock:
            return self._state

    @property
    def stats(self) -> AudioStats:
        return self._stats

    # ---- 对外：生命周期 ----
    def start(self) -> None:
        """开始接收并播放（连接成功后由 main_window 调用；幂等）"""
        with self._state_lock:
            if self._worker is not None:
                return
            worker = AudioWorker(
                self._config, self._stats,
                self._output_factory,
                state_cb=self._on_worker_ready,
            )
            self._worker = worker
        # 先置 STARTING 再起线程：worker 若立即失败，ERROR 不会被 STARTING 覆盖
        self._set_state(AudioState.STARTING)
        worker.start()
        logger.info("[AUDIO] service started (volume=%.2f, mode=%s)",
                    self._config.volume, self._config.mode.value)

    def stop(self) -> None:
        """停止并释放（断开/关窗时由 main_window 调用；幂等）"""
        with self._state_lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            worker.stop()
        self._set_state(AudioState.IDLE)
        logger.info("[AUDIO] service stopped")

    def restart(self) -> None:
        """重启（为未来"断线整体重连"预留：stop + start 的原子入口）"""
        logger.info("[AUDIO] service restart")
        self.stop()
        self.start()

    # ---- 对外：配置 ----
    def set_volume(self, value: float) -> None:
        """设置音量 0.0~1.0（GUI 线程安全；worker 侧即时生效）"""
        self._config.volume = value

    def set_mode(self, mode: AudioMode) -> None:
        """切换播放模式（下一次写入即用新策略）"""
        self._config.mode = mode
        logger.info("[AUDIO] mode -> %s", mode.value)

    def set_format(self, sample_rate: int, channels: int) -> None:
        """切换 PCM 格式（音质对比 A/B/C 用）。

        注意：FFmpeg 输出与输出设备两端的格式在**下一次 start/restart** 时
        重新按此配置建立；运行中直接切换无效（须 restart）。
        """
        self._config.sample_rate = int(sample_rate)
        self._config.channels = int(channels)
        logger.info("[AUDIO] format -> %dHz/%dch（restart 后生效）",
                    sample_rate, channels)

    @property
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    def channels(self) -> int:
        return self._config.channels

    @property
    def mode(self) -> AudioMode:
        return self._config.mode

    @property
    def volume(self) -> float:
        return self._config.volume

    # ---- FFmpegReader.audio_pcm_callback 契约 ----
    def feed(self, data) -> None:
        """接收 FFmpegReader 送来的音频数据（可能从音频读取线程调用）。

        b""   = 音频流结束：若从头就没收到过数据 → 判定无音频轨；
        None  = FFmpegReader 明确判定无音频轨；
        bytes = PCM 块。
        """
        if data is None:
            logger.info("[AUDIO] 当前直播无声音")
            self._set_state(AudioState.NO_AUDIO_TRACK)
            return

        if data == b"":
            with self._state_lock:
                worker = self._worker
            if worker is not None and self._stats.chunks_received == 0:
                # 音频通道 EOF 且从未有数据：同样按无音频轨处理
                logger.info("[AUDIO] 音频通道无数据，判定无音频轨")
                self._set_state(AudioState.NO_AUDIO_TRACK)
            return

        with self._state_lock:
            worker = self._worker
        if worker is None:
            return                      # 未启动（OCR 暂停等场景）：直接丢弃
        if self._state is AudioState.STARTING:
            self._set_state(AudioState.PLAYING)
        # 在投递线程同步计数：EOF 判定"从未收到数据"不能依赖 worker 的异步消费
        self._stats.chunks_received += 1
        worker.feed(data)

    # ---- 内部 ----
    def _on_worker_ready(self, ok: bool, message: str) -> None:
        """worker 线程内回调：输出打开成功/失败"""
        if ok:
            # 有 PCM 才切 PLAYING；先占住"设备已就绪"
            if self._state is AudioState.STARTING:
                pass    # 等 feed 到第一块 PCM 再切，避免"无声却显示播放中"
        else:
            logger.error("[AUDIO] 音频输出不可用: %s", message)
            self._set_state(AudioState.ERROR, message)
