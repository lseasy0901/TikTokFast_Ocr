# -*- coding: utf-8 -*-
"""平台适配器包。

每个平台一个模块，实现 stream/adapters/base.py 的 PlatformAdapter。
新增平台时只在这里加模块 + 在 stream/registry.py 注册，核心播放层不动。
"""

from .base import PlatformAdapter
from .douyin import DouyinAdapter, DouyinStream, DouyinStreamError
from .huya import HuyaAdapter, HuyaStreamError

__all__ = [
    "PlatformAdapter",
    "DouyinAdapter",
    "DouyinStream",
    "DouyinStreamError",
    "HuyaAdapter",
    "HuyaStreamError",
]
