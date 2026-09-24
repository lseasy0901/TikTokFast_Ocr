# -*- coding: utf-8 -*-
"""直播流模块

核心播放链（ffmpeg_reader / frame_buffer）与平台无关；
平台解析统一走 StreamResolver -> PlatformAdapter -> StreamInfo。
"""

from .douyin import DouyinStream, DouyinStreamError
from .failover import CandidateFailover
from .ffmpeg_reader import FFmpegReader
from .frame_buffer import LatestFrameBuffer
from .models import StreamCandidate, StreamInfo, StreamResolveError
from .resolver import StreamResolver
from .video_info import get_video_info, VideoInfo

from .adapters.huya import HuyaAdapter, HuyaStreamError

__all__ = [
    "DouyinStream",
    "DouyinStreamError",
    "HuyaAdapter",
    "HuyaStreamError",
    "CandidateFailover",
    "FFmpegReader",
    "LatestFrameBuffer",
    "StreamCandidate",
    "StreamInfo",
    "StreamResolveError",
    "StreamResolver",
    "get_video_info",
    "VideoInfo",
]
