# -*- coding: utf-8 -*-
"""补全链接解析回归(2026-10-25):短链/文本提取/纯数字/变体路径/NET-320。"""
import os
import sys
from types import ModuleType

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream.adapters.douyin import DouyinStream  # noqa: E402
from stream.resolver import StreamResolveError, StreamResolver  # noqa: E402


# ---------------------------------------------------------------- 纯本地解析
def test_short_link_is_accepted_without_network_parse():
    """v.douyin.com 被接住;本地解析不出 room_id 是预期(需网络展开)。"""
    assert DouyinStream.is_live_url("https://v.douyin.com/iAbCde/")
    with pytest.raises(StreamResolveError):
        DouyinStream("https://v.douyin.com/iAbCde/").get_room_id()


def test_fragment_form():
    assert DouyinStream("https://live.douyin.com/#/live/555").get_room_id() == "555"


def test_from_path_variant():
    assert (DouyinStream("https://live.douyin.com/from/70000/96670111738")
            .get_room_id() == "96670111738")


def test_bare_digits_room_id():
    assert DouyinStream("96670111738").get_room_id() == "96670111738"


def test_canonical_regression():
    assert (DouyinStream("https://live.douyin.com/96670111738")
            .get_room_id() == "96670111738")
    assert (DouyinStream("https://live.douyin.com/follow/live/123")
            .get_room_id() == "123")


# ---------------------------------------------------------------- prepare_url
def test_prepare_extracts_url_from_text():
    text = "来我直播间看比赛 https://v.douyin.com/iAbCde/ 打开抖音"
    assert DouyinStream.prepare_url(text) == "https://v.douyin.com/iAbCde/"


def test_prepare_prefers_live_over_short():
    text = "https://v.douyin.com/aabbcc/ 以及 https://live.douyin.com/96670111738"
    assert DouyinStream.prepare_url(text) == "https://live.douyin.com/96670111738"


def test_prepare_digits():
    assert DouyinStream.prepare_url("96670111738").endswith("96670111738")


def test_prepare_canonical_passthrough():
    assert (DouyinStream.prepare_url("https://live.douyin.com/123")
            == "https://live.douyin.com/123")


def test_prepare_garbage_raises_net320():
    with pytest.raises(StreamResolveError) as e:
        DouyinStream.prepare_url("今天天气不错")
    assert "NET-320" in str(e.value)


# ---------------------------------------------------------------- 注册表路由
def test_registry_prepare_input():
    r = StreamResolver()
    assert (r.prepare_input("来我直播间 https://v.douyin.com/iAbCde/ 看看")
            == "https://v.douyin.com/iAbCde/")
    assert r.prepare_input("96670111738").endswith("96670111738")
    with pytest.raises(StreamResolveError) as e:
        r.prepare_input("今天天气不错")
    assert "NET-320" in str(e.value)


def test_registry_resolve_accepts_digits():
    """resolve 走 prepare_input 的等价链路由 _ConnectWorker 负责;
    本用例证明 resolve(规范化后的 URL) 正常。"""
    r = StreamResolver()
    info = r.resolve(r.prepare_input("96670111738"))
    assert info.platform == "douyin" and info.room_id == "96670111738"


# ---------------------------------------------------------------- 短链展开
def test_get_room_id_expands_short_link(monkeypatch):
    """get_room_id 对短链做网络展开(展开函数打桩,不发真实请求)。"""
    class FakeResp:
        status_code = 200
        url = "https://live.douyin.com/96670111738"

    fake = ModuleType("requests")
    fake.RequestException = Exception
    fake.get = lambda *a, **k: FakeResp()
    monkeypatch.setattr("stream.adapters.douyin.requests", fake)
    assert (DouyinStream("https://v.douyin.com/iAbCde/")
            .get_room_id() == "96670111738")
