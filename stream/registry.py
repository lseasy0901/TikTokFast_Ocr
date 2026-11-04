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

    def prepare_input(self, raw: str) -> str:
        """把用户粘贴的任意内容规范化为可解析 URL(2026-10-25 补全解析)。

        逐适配器询问(平台各自从文本提取链接/识别房间号);都认不出时:
        raw 本身是 http(s) URL 就原样返回(交给 resolve 报"无匹配平台"),
        否则抛 StreamResolveError(NET-320,用户可见错误码)。
        """
        text = (raw or "").strip()
        if not text:
            raise StreamResolveError("NET-320 请输入直播间地址")
        for adapter in self._adapters:
            prepared = None
            try:
                prepared = adapter.prepare_url(text)
            except Exception:  # noqa: BLE001 - 单个适配器失败不影响其它平台
                continue
            if prepared:
                return prepared
        if text.lower().startswith(("http://", "https://")):
            return text
        raise StreamResolveError(
            "NET-320 无法从粘贴内容中识别直播间链接:"
            "请使用抖音 App 分享按钮的「复制链接」后重试")

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
    """构建内置注册表（抖音 + 虎牙 + B 站）。

    各平台按主机名互斥（live.douyin.com / huya.com / live.bilibili.com），
    因此注册顺序不影响匹配结果；抖音保持在前，维持既有解析路径的优先地位。
    新增平台只增强其自身 URL 的处理能力，不改变既有平台的任何行为。
    """
    from .adapters.douyin import DouyinAdapter
    from .adapters.huya import HuyaAdapter
    from .adapters.bilibili import BilibiliAdapter

    reg = PlatformRegistry()
    reg.register(DouyinAdapter())
    reg.register(HuyaAdapter())
    # B 站为新增平台。可控入口：DLV_PLATFORM_BILIBILI 显式设为 0/false/off
    # 时不注册（一键回退到"B 站地址报不支持"的旧行为）。
    if _bilibili_enabled():
        reg.register(BilibiliAdapter())
    return reg


_TRUTHY_OFF = ("0", "false", "no", "off")


def _bilibili_enabled() -> bool:
    """B 站适配器开关（默认开）。设 DLV_PLATFORM_BILIBILI=0 即关闭。"""
    import os
    return (os.environ.get("DLV_PLATFORM_BILIBILI") or "").strip().lower() \
        not in _TRUTHY_OFF
