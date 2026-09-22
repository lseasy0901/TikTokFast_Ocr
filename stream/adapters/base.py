# -*- coding: utf-8 -*-
"""平台适配器接口。

职责边界：
    平台负责"这个 URL 对应什么直播间、从哪里能拿到流、有哪些候选流"；
    核心负责"拿到候选流之后怎么播放"。
    因此本接口只暴露两件事：能不能处理、解析成什么。
"""

from abc import ABC, abstractmethod

from ..models import StreamInfo

__all__ = ["PlatformAdapter"]


class PlatformAdapter(ABC):
    """一个直播平台的适配器。

    实现类必须保持与 GUI / Qt / FFmpeg 无关：只做网络请求与数据解析，
    返回统一的 StreamInfo。播放、渲染、OCR 都不属于这里。
    """

    #: 平台标识，写入 StreamInfo.platform
    name: str = ""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """这个 URL 是否属于本平台。

        必须只依据 URL 本身判断，不做任何网络请求。
        """

    @abstractmethod
    def resolve(self, url: str) -> StreamInfo:
        """解析直播间 URL，返回统一 StreamInfo。

        :raises StreamResolveError: 解析失败（未开播、无可用流、网络异常等）
        """
