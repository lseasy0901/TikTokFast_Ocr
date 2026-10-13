# -*- coding: utf-8 -*-
"""平台无关的流数据模型。

边界：
    平台适配器负责产出 StreamCandidate 列表并给出自己的优先级顺序；
    核心播放层只消费 StreamInfo / 选中的 StreamCandidate，
    不需要知道候选来自哪个平台、字段原来叫什么名字。
"""

from dataclasses import dataclass, field

__all__ = ["StreamCandidate", "StreamInfo", "StreamResolveError"]


class StreamResolveError(Exception):
    """流解析失败的统一基类。

    各平台自己的异常类型都应继承它，这样 GUI 只需要捕获一种类型
    就能覆盖"没有可用平台 / 平台解析失败"两种情况。
    """


@dataclass
class StreamCandidate:
    """一个可播放的候选流地址。

    :param url:       可直接交给 FFmpeg 的完整播放地址
    :param protocol:  传输协议，如 "flv" / "hls"
    :param quality:   平台原生的清晰度档位名（如 "HD" / "超清"），仅作展示与溯源
    :param bitrate:   码率（kbps），未知为 None
    :param width/height/fps: 已知时填写，未知为 None
    :param metadata:  平台附加信息（CDN 名等），核心层不解释
    :param http_headers: 播放该地址所需的 HTTP 请求头（平台提供，核心层执行）
    :param runtime: 期望执行该候选的 FFmpeg 运行时标记（B4 裸流："ffmpeg7"）；
                     None = 默认运行时。核心层不解释具体版本，仅用于选择 exe。
                     **RELEASE-02-B5 起保留但生产链路不再消费**：裸流不再是
                     "派生候选"，而是由 GUI 在会话开始时经
                     `utils.ffmpeg7_runtime` 定位并校验外部运行时后，直接以
                     `exe_path=` 传给 FFmpegReader。此字段仅为兼容旧
                     `stream/bare_candidate.py`（已弃用）与既有测试而保留。
    """

    url: str
    protocol: str
    runtime: str | None = None
    quality: str | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    metadata: dict = field(default_factory=dict)
    #: 播放该候选所需的 HTTP 请求头。为空表示"用播放器的默认请求头"。
    #: 放在明确字段而不是 metadata 里，是因为它不是"仅供参考的附加信息"，
    #: 而是播放能否成功的一部分：核心播放层必须原样把它交给 FFmpeg。
    http_headers: dict = field(default_factory=dict)


@dataclass
class StreamInfo:
    """一次解析的完整结果：某个直播间的全部候选流 + 已选中的那一个。

    :param platform:  平台标识（如 "douyin"）
    :param room_id:   平台内的房间标识；仅用于显示与历史记录
    :param candidates: 候选流，按平台自己的优先级排列（越靠前越优先）
    :param selected:  核心选中的候选（由 selector 从 candidates 中选出）
    :param width/height: 选中流的实际分辨率，未探测到为 None
    """

    platform: str
    room_id: str
    candidates: list = field(default_factory=list)
    selected: StreamCandidate | None = None
    width: int | None = None
    height: int | None = None

    @property
    def url(self) -> str | None:
        """选中候选的播放地址（无候选时为 None）"""
        return self.selected.url if self.selected else None

    @property
    def protocol(self) -> str | None:
        """选中候选的协议（无候选时为 None）"""
        return self.selected.protocol if self.selected else None
