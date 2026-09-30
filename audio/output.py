# -*- coding: utf-8 -*-
"""
音频输出封装（QAudioSink push 模式）

本模块是 PySide6.QtMultimedia 的唯一接触点：
    - 延迟导入：tests / headless 环境不触碰 QtMultimedia 也能 import audio 包
    - AudioOutput 接口：AudioWorker 只依赖这个接口，测试用 FakeOutput 替换
    - 在创建它的线程里使用（AudioWorker 线程），不跨线程调用

打开失败（无声卡 / 格式不支持）以 AudioOutputError 抛出，由 service 转为
ERROR 状态，绝不向上游传播。
"""

import logging

logger = logging.getLogger("DouyinLowLatencyViewer.audio.output")

__all__ = ["AudioOutputError", "QtAudioOutput", "DEFAULT_FORMAT"]


#: 默认 PCM 参数（须与 stream.ffmpeg_reader 的音频输出一致）；供日志与测试断言
DEFAULT_FORMAT = {"sample_rate": 48000, "channels": 2, "sample_width": 2}
#: 设备缓冲目标时长（毫秒）。Qt 默认缓冲在 WASAPI 共享模式下只有几毫秒，
#: 任何调度抖动都会欠载产生爆音 —— 必须显式放大。稳定优先取 250ms。
DEFAULT_SINK_BUFFER_MS = 250


class AudioOutputError(Exception):
    """音频输出设备不可用 / 打开失败"""


class QtAudioOutput:
    """QAudioSink push 模式封装"""

    def __init__(self, volume: float, sample_rate: int | None = None,
                 channels: int | None = None, sink_buffer_ms: int | None = None):
        self._sink = None
        self._io = None
        self._volume = volume
        self._sample_rate = sample_rate or DEFAULT_FORMAT["sample_rate"]
        self._channels = channels or DEFAULT_FORMAT["channels"]
        self._sink_buffer_ms = sink_buffer_ms or DEFAULT_SINK_BUFFER_MS

    def open(self) -> None:
        """创建并打开 QAudioSink（必须在 worker 线程调用）"""
        try:
            from PySide6.QtMultimedia import (
                QAudio,
                QAudioFormat,
                QAudioSink,
                QMediaDevices,
            )
        except ImportError as e:  # 运行环境缺 QtMultimedia：显式报错而非崩溃
            raise AudioOutputError(f"QtMultimedia 不可用: {e}") from e

        fmt = QAudioFormat()
        fmt.setSampleRate(self._sample_rate)
        fmt.setChannelCount(self._channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            raise AudioOutputError("没有可用的音频输出设备")
        if not device.isFormatSupported(fmt):
            raise AudioOutputError(
                f"输出设备不支持 {self._sample_rate}Hz/16bit/"
                f"{self._channels}ch PCM"
            )

        self._sink = QAudioSink(device, fmt)
        # 显式放大设备内部缓冲（默认值太小是卡顿/爆音的主要来源）：
        # 目标 250ms，设备按需取整；必须在 start() 之前设置。
        target = int(self._sample_rate * self._channels * 2
                     * self._sink_buffer_ms / 1000)
        self._sink.setBufferSize(target)
        self._sink.setVolume(self._volume)
        self._io = self._sink.start()          # push 模式：返回可写的 QIODevice
        if self._io is None or self._sink.error().value != QAudio.Error.NoError.value:
            raise AudioOutputError(f"QAudioSink 打开失败: {self._sink.error()}")
        logger.info("[AUDIO] 输出已打开: %s (%dHz/%dch, 设备缓冲 %dB)",
                    device.description(), self._sample_rate, self._channels,
                    self._sink.bufferSize())

    def write(self, data: bytes) -> int:
        """写入 PCM，返回实际接受的字节数（小于 len(data) 表示设备暂时满）"""
        if self._io is None:
            return 0
        return int(self._io.write(data))

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = value
        if self._sink is not None:
            self._sink.setVolume(value)

    @property
    def is_underrun(self) -> bool:
        """push 模式下 IdleState = 设备已把缓冲播完且无新数据（真欠载）"""
        if self._sink is None:
            return False
        from PySide6.QtMultimedia import QAudio
        return self._sink.state().value == QAudio.State.IdleState.value

    def close(self) -> None:
        """停止并释放（幂等；在创建它的线程里调用）"""
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:  # noqa: BLE001 - 已在关闭路径，不再外抛
                pass
            self._sink = None
            self._io = None
