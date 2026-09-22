# -*- coding: utf-8 -*-
"""平台注册表：URL → PlatformAdapter。

刻意保持最简：一个有序列表 + 线性匹配。
不做动态加载、不做 entry-point 插件，因为当前只有抖音一个平台，
多一个平台的成本只是 register() 一行。
"""

from .models import StreamResolveError

__all__ = ["PlatformRegistry", "build_default_registry"]


class PlatformRegistry:
    """按注册顺序线性匹配适配器（先注册的优先）。"""

    def __init__(self):
        self._adapters = []

    def register(self, adapter) -> None:
        """注册一个适配器（重复注册同一类型会被忽略）"""
        if adapter is None:
            return
        if any(type(a) is type(adapter) for a in self._adapters):
            return
        self._adapters.append(adapter)

    @property
    def adapters(self):
        return list(self._adapters)

    def get_adapter(self, url: str):
        """返回能处理该 URL 的适配器；没有则 None"""
        for a in self._adapters:
            try:
                if a.can_handle(url):
                    return a
            except Exception:  # noqa: BLE001 - 单个适配器的判断失败不应影响其它平台
                continue
        return None

    def resolve(self, url: str):
        """用匹配到的适配器解析；没有适配器时抛 StreamResolveError"""
        adapter = self.get_adapter(url)
        if adapter is None:
            raise StreamResolveError(f"没有可处理该地址的直播平台: {url}")
        return adapter.resolve(url)


def build_default_registry() -> PlatformRegistry:
    """构建内置注册表（目前只有抖音）"""
    from .adapters.douyin import DouyinAdapter

    reg = PlatformRegistry()
    reg.register(DouyinAdapter())
    return reg
