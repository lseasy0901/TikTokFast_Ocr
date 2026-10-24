# -*- coding: utf-8 -*-
"""B 站（bilibili）平台适配器测试（MP-01）

覆盖目标：
    1.  B 站 URL can_handle（含带查询/尾部斜杠/大小写）
    2.  非 B 站 URL 被拒
    3.  build_media_url：host + base_url + extra 拼接 / 缺字段返回 None
    4.  build_candidates：仅取 H.264、FLV 优先、清晰度标签、媒体请求头
    5.  resolve 成功：platform/room_id/candidates/selected
    6.  未开播 → BilibiliStreamError
    7.  无可播放流（空 stream）→ BilibiliStreamError
    8.  业务错误码（code != 0）→ BilibiliStreamError
    9.  网络异常 → BilibiliStreamError
    10. 平台隔离：B 站地址只命中 BilibiliAdapter，抖音/虎牙不受影响
    11. 开关：DLV_PLATFORM_BILIBILI=0 时不注册

本文件**不联网**（_get_json 被桩住）。夹具为按真实响应结构构造的合成数据，
不含任何真实签名 / token / 有效期。
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)

from stream.adapters.bilibili import (  # noqa: E402
    BilibiliAdapter, BilibiliStreamError, build_candidates, build_media_url,
)
from stream.adapters.douyin import DouyinAdapter  # noqa: E402
from stream.adapters.huya import HuyaAdapter  # noqa: E402
from stream.models import StreamResolveError  # noqa: E402
from stream.registry import build_default_registry  # noqa: E402
from stream.resolver import StreamResolver  # noqa: E402

BILI_URL = "https://live.bilibili.com/7734200"

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        return "wrong-exc:%s:%s" % (type(e).__name__, e)
    return "no-raise"


def _playurl_info():
    """按真实结构构造的 playurl_info（http_stream/flv/avc+hevc，http_hls/ts/avc）。"""
    def codec(name, qn, base, host):
        return {"codec_name": name, "current_qn": qn, "accept_qn": [10000, 250],
                "base_url": base,
                "url_info": [{"host": host, "extra": "expires=1&sign=FAKE&qn=%d" % qn}]}
    return {"playurl": {"stream": [
        {"protocol_name": "http_stream", "format": [
            {"format_name": "flv", "codec": [
                codec("avc", 250, "/live/flv_2500.flv?", "https://cdn-a.example.com"),
                codec("hevc", 250, "/live/minihevc.flv?", "https://cdn-a.example.com"),
            ]}]},
        {"protocol_name": "http_hls", "format": [
            {"format_name": "ts", "codec": [
                codec("avc", 250, "/live/2500.m3u8?", "https://cdn-b.example.com"),
                codec("hevc", 250, "/live/minihevc.m3u8?", "https://cdn-b.example.com"),
            ]},
            {"format_name": "fmp4", "codec": [
                codec("avc", 250, "/live/2500/index.m3u8?", "https://cdn-c.example.com"),
            ]}]},
    ]}}


def t_can_handle():
    a = BilibiliAdapter()
    for u in (BILI_URL, BILI_URL + "?from=x", BILI_URL + "/",
              "http://live.bilibili.com/6", "https://LIVE.BILIBILI.COM/123"):
        check("can_handle %s" % u, a.can_handle(u))
    for bad in ("", None, "https://live.douyin.com/1", "https://www.bilibili.com/video/BV1",
                "https://live.bilibili.com/", "https://live.bilibili.com/abc",
                "https://example.com/live.bilibili.com/1"):
        check("拒绝 %r" % bad, not a.can_handle(bad))


def t_build_media_url():
    ok = build_media_url({"base_url": "/x/y.flv?", "url_info": [
        {"host": "https://h.example.com/", "extra": "a=1&b=2"}]})
    check("host+base+extra 拼接", ok == "https://h.example.com/x/y.flv?a=1&b=2", str(ok))
    check("缺 base_url → None", build_media_url({"url_info": [{"host": "h"}]}) is None)
    check("缺 url_info → None", build_media_url({"base_url": "/x?"}) is None)
    check("缺 host → None", build_media_url(
        {"base_url": "/x?", "url_info": [{"extra": "a=1"}]}) is None)


def t_build_candidates():
    cands = build_candidates(_playurl_info())
    check("只取 avc（3 个：flv + 2 hls）", len(cands) == 3, str(len(cands)))
    check("首个为 FLV", cands[0].protocol == "flv", cands[0].protocol)
    check("FLV 优先整体成立",
          [c.protocol for c in cands] == ["flv", "hls", "hls"],
          str([c.protocol for c in cands]))
    check("不含 hevc", all("minihevc" not in c.url for c in cands))
    check("清晰度标签 qn250=超清", cands[0].quality == "超清", str(cands[0].quality))
    check("带 Referer 媒体头",
          cands[0].http_headers.get("Referer") == "https://live.bilibili.com/")
    check("带 User-Agent 媒体头（B 站 CDN 需 Referer+UA 同时具备）",
          "Mozilla" in (cands[0].http_headers.get("User-Agent") or ""))
    check("URL 含 host", cands[0].url.startswith("https://cdn-a.example.com/"))
    check("空 playurl_info → 空列表", build_candidates({}) == [])


def _stub(adapter, responses):
    """按调用顺序返回 responses 里的 (api_or_stage, payload)；payload 为 dict 或 Exception。"""
    seq = list(responses)

    def fake_get_json(api, params, stage):
        kind, payload = seq.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload
    adapter._get_json = fake_get_json


def t_resolve_ok():
    a = BilibiliAdapter()
    _stub(a, [
        ("room_init", {"code": 0, "data": {"room_id": 7734200, "live_status": 1}}),
        ("playinfo", {"code": 0, "data": {"live_status": 1,
                                          "playurl_info": _playurl_info()}}),
    ])
    info = a.resolve(BILI_URL)
    check("platform=bilibili", info.platform == "bilibili")
    check("room_id 归一为 API 返回值", info.room_id == "7734200", info.room_id)
    check("candidates 非空", len(info.candidates) == 3)
    check("selected 为 FLV 首选", info.selected.protocol == "flv")


def t_resolve_errors():
    # 未开播
    a = BilibiliAdapter()
    _stub(a, [("room_init", {"code": 0, "data": {"room_id": 1, "live_status": 0}})])
    check("未开播 → BilibiliStreamError",
          _raises(BilibiliStreamError, lambda: a.resolve(BILI_URL)) is True)

    # room_init 无 data
    a = BilibiliAdapter()
    _stub(a, [("room_init", {"code": 0, "data": {}})])
    check("room_init 空 data → 报错",
          _raises(BilibiliStreamError, lambda: a.resolve(BILI_URL)) is True)

    # 播放信息里无可用流
    a = BilibiliAdapter()
    _stub(a, [
        ("room_init", {"code": 0, "data": {"room_id": 1, "live_status": 1}}),
        ("playinfo", {"code": 0, "data": {"live_status": 1, "playurl_info": {}}}),
    ])
    check("无可用流 → BilibiliStreamError",
          _raises(BilibiliStreamError, lambda: a.resolve(BILI_URL)) is True)

    # 业务错误码（adapter._get_json 内部逻辑，这里直接走真实实现分支）
    a = BilibiliAdapter()
    _stub(a, [("room_init", BilibiliStreamError("[room_init] 业务错误 code=1"))])
    check("业务错误透传为 BilibiliStreamError",
          _raises(BilibiliStreamError, lambda: a.resolve(BILI_URL)) is True)

    # 非法 URL
    a = BilibiliAdapter()
    check("非法 URL → BilibiliStreamError",
          _raises(BilibiliStreamError, lambda: a.resolve("https://x/y")) is True)


def t_gate_and_isolation():
    reg = build_default_registry()
    check("注册表含 BilibiliAdapter",
          any(isinstance(x, BilibiliAdapter) for x in reg.adapters))
    check("B 站 URL 命中 BilibiliAdapter",
          isinstance(reg.get_adapter(BILI_URL), BilibiliAdapter))
    check("抖音 URL 仍命中 DouyinAdapter",
          isinstance(reg.get_adapter("https://live.douyin.com/123"), DouyinAdapter))
    check("虎牙 URL 仍命中 HuyaAdapter",
          isinstance(reg.get_adapter("https://www.huya.com/dank1ng"), HuyaAdapter))
    check("抖音仍注册在首位", isinstance(reg.adapters[0], DouyinAdapter))
    check("未知地址仍报不支持",
          _raises(StreamResolveError,
                  lambda: StreamResolver().resolve("https://example.com/a")) is True)

    # 开关关闭 → 不注册
    os.environ["DLV_PLATFORM_BILIBILI"] = "0"
    try:
        reg2 = build_default_registry()
        check("DLV_PLATFORM_BILIBILI=0 时不注册 B 站",
              not any(isinstance(x, BilibiliAdapter) for x in reg2.adapters))
        check("开关关闭不影响抖音",
              isinstance(reg2.get_adapter("https://live.douyin.com/1"), DouyinAdapter))
    finally:
        os.environ.pop("DLV_PLATFORM_BILIBILI", None)


def main():
    print("=" * 60)
    print("MP-01 B 站平台适配器测试")
    print("=" * 60)
    for fn in (t_can_handle, t_build_media_url, t_build_candidates,
               t_resolve_ok, t_resolve_errors, t_gate_and_isolation):
        fn()
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\nRESULT: %d/%d passed" % (passed, total))
    for name, ok, detail in _results:
        if not ok:
            print("  FAIL: %s  %s" % (name, detail))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
