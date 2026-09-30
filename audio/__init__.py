# -*- coding: utf-8 -*-
"""LiveLens 音频模块（AudioService 功能域）

结构：
    service.py   AudioService    生命周期门面（GUI 唯一接触点）
    worker.py    AudioWorker     播放线程：队列 → 策略 → 输出
    output.py    QtAudioOutput   QAudioSink push 封装（PySide6 自带，零新依赖）
    strategy.py  Sync/LowLatency 缓冲策略
    models.py    模式/状态/配置/统计

数据流（与视频/OCR 完全并行，互不依赖）：
    FFmpeg(单实例, 同 -i) ─┬─ stdout rawvideo   → 现有视频/OCR 链路（未改动）
                          └─ tcp://127.0.0.1:N → FFmpegReader(音频线程)
                              → AudioService.feed → AudioWorker → 声卡
"""

from audio.models import AudioMode, AudioState
from audio.service import AudioService

__all__ = ["AudioService", "AudioMode", "AudioState"]
