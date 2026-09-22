# -*- coding: utf-8 -*-
"""流解析入口：URL → StreamInfo。

这是核心播放层唯一需要认识的解析接口。
MainWindow 不再 import 任何平台模块，也不再接收平台特有的裸 dict。
"""

from .models import StreamInfo, StreamResolveError
from .registry import PlatformRegistry, build_default_registry

__all__ = ["StreamResolver", "StreamInfo", "StreamResolveError"]


class StreamResolver:
    """把直播间 URL 解析为统一的 StreamInfo。"""

    def __init__(self, registry: PlatformRegistry | None = None):
        self._registry = registry if registry is not None else build_default_registry()

    @property
    def registry(self) -> PlatformRegistry:
        return self._registry

    def resolve(self, live_url: str) -> StreamInfo:
        """解析直播间 URL。

        :raises StreamResolveError: 无匹配平台，或平台解析失败
        """
        if not live_url or not isinstance(live_url, str):
            raise StreamResolveError("URL格式错误：直播间URL为空或类型不正确")
        return self._registry.resolve(live_url.strip())
