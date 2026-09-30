# -*- coding: utf-8 -*-
"""
音频数据模型与常量

只放"定义"：模式、状态、配置、统计。任何行为逻辑都在别的模块里。
与 stream/ffmpeg_reader 的 PCM 输出参数保持一致（48kHz/16bit/双声道）。
"""

import enum
import threading

__all__ = [
    "AudioMode", "AudioState", "AudioConfig", "AudioStats",
    "BYTES_PER_SECOND", "SAMPLE_RATE", "CHANNELS", "SAMPLE_WIDTH",
]

# ---- PCM 参数（与 stream.ffmpeg_reader._AUDIO_* 保持一致） ----
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2                      # s16le
#: 每秒字节数 = 48000 * 2ch * 2B
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


def ms_to_bytes(ms: float) -> int:
    """毫秒 → PCM 字节数（用于 buffer 策略配置）"""
    return int(BYTES_PER_SECOND * ms / 1000)


class AudioMode(enum.Enum):
    """播放模式（单一事实来源；UI 下拉顺序与默认值见 _MODE_ITEMS/AudioConfig）"""
    #: 极速模式（默认）：始终偏向最新声音，超过上限立即丢弃旧缓冲。
    #: 实测同步模式的缓冲水位跟不上最新直播画面，故默认极速（2026-09-30）。
    LOW_LATENCY = "low_latency"
    #: 同步模式：缓冲到起播水位后连续播放，宁可多一点固定延迟也不断音
    SYNC = "sync"


class AudioState(enum.Enum):
    """AudioService 对外可见状态"""
    IDLE = "idle"                     # 未连接 / 已停止
    STARTING = "starting"             # 已启动，等待首块 PCM
    PLAYING = "playing"               # 正在播放
    NO_AUDIO_TRACK = "no_track"       # 当前直播无声音
    ERROR = "error"                   # 音频错误（不影响视频/OCR）
    STOPPED = "stopped"               # 已随会话停止


class AudioConfig:
    """运行时可变配置（音量 / 模式）。

    GUI 线程写、音频线程读，用一把轻量锁保证可见性；量小，无竞争热点。
    """

    def __init__(self, volume: float = 0.8,
                 mode: AudioMode = AudioMode.LOW_LATENCY,
                 sample_rate: int = 48000, channels: int = 2,
                 sink_buffer_ms: int = 250):
        self._lock = threading.Lock()
        self._volume = min(max(float(volume), 0.0), 1.0)
        self._mode = mode
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._sink_buffer_ms = int(sink_buffer_ms)

    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        with self._lock:
            self._volume = min(max(float(value), 0.0), 1.0)

    @property
    def mode(self) -> AudioMode:
        with self._lock:
            return self._mode

    @mode.setter
    def mode(self, value: AudioMode) -> None:
        with self._lock:
            self._mode = value

    @property
    def sample_rate(self) -> int:
        with self._lock:
            return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, value: int) -> None:
        with self._lock:
            self._sample_rate = int(value)

    @property
    def channels(self) -> int:
        with self._lock:
            return self._channels

    @channels.setter
    def channels(self, value: int) -> None:
        with self._lock:
            self._channels = int(value)

    @property
    def sink_buffer_ms(self) -> int:
        with self._lock:
            return self._sink_buffer_ms

    @sink_buffer_ms.setter
    def sink_buffer_ms(self, value: int) -> None:
        with self._lock:
            self._sink_buffer_ms = int(value)


class AudioStats:
    """诊断统计（只增不减，读取无需锁，字段宽度足够）"""

    def __init__(self):
        self.chunks_received = 0      # 收到 PCM 块数
        self.chunks_dropped = 0       # 队列满被丢弃的块数
        self.bytes_played = 0         # 已写入输出的字节数
        self.underruns = 0            # 输出欠载次数
