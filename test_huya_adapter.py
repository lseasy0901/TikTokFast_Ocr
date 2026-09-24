# -*- coding: utf-8 -*-
"""虎牙平台适配器测试（Phase 2）

覆盖目标：
    1.  Huya URL can_handle
    2.  非 Huya URL rejected
    3.  stream JS 成功解析
    4.  gameStreamInfoList 找到
    5.  FLV candidate 数量正确
    6.  CDN 信息正确
    7.  sStreamName 正确参与 URL 构造
    8.  sFlvUrlSuffix 正确
    9.  AntiCode 正确进入 query
    10. protocol == flv
    11. metadata 正确
    12. 无有效流时抛 StreamResolveError
    13. StreamResolver -> HuyaAdapter
    14. 默认 Registry 同时包含 Douyin + Huya

    15/16（抖音 resolver 测试、frameReady 测试）由各自的测试文件覆盖，
    不在本文件内重复；本文件不联网（fetch 与 ffprobe 都被桩住）。

数据来源：tests/fixtures/huya_dank1ng.html —— Huya 实验阶段保存的真实页面
（与 D:\\TIikTok_Ocr\\HuyaStreamExperiment\\huya_dank1ng.html 逐字节一致）。
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.disable(logging.CRITICAL)

from stream.adapters.douyin import DouyinAdapter  # noqa: E402
from stream.adapters.huya import (  # noqa: E402
    HuyaAdapter, HuyaStreamError, build_candidates, build_flv_url,
    locate_room_data, parse_stream_object,
)
from stream.models import StreamCandidate, StreamInfo, StreamResolveError  # noqa: E402
from stream.registry import build_default_registry  # noqa: E402
from stream.resolver import StreamResolver  # noqa: E402
from stream.video_info import VideoInfo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "fixtures", "huya_dank1ng.html")

HUYA_URL = "https://www.huya.com/dank1ng"

# 期望值独立于被测算法的实现，直接来自对 fixture 的静态观察
EXPECTED_CDNS = ["AL", "TX", "HS"]
EXPECTED_STREAM_NAME = ("333393123-333393123-1431912559996305408"
                        "-666909702-10057-A-0-1-imgplus")
EXPECTED_FLV_BASE = {
    "AL": "http://al.flv.huya.com/src",
    "TX": "http://tx.flv.huya.com/src",
    "HS": "http://hs.flv.huya.com/src",
}

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"   {detail}" if detail and not cond else ""))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


class _StubAdapter(HuyaAdapter):
    """网络被完全桩住的 HuyaAdapter：解析逻辑是真的，只有 IO 是假的。"""

    def __init__(self, html="", video_info=None):
        self._html = html
        self._video_info = video_info
        self.requested = []

    def _fetch_room_page(self, url):
        self.requested.append(url)
        if self._html is None:
            raise HuyaStreamError("虎牙房间页请求失败: 桩网络不可用")
        return self._html

    def _detect_video_resolution(self, stream_url):
        self.probed = stream_url
        return self._video_info


def synth_html(room_obj):
    """构造一个带内联 stream 数据块的极简页面（键名带引号）"""
    payload = json.dumps({"data": [room_obj]}, ensure_ascii=False,
                         separators=(",", ":"))
    return f"<html><script>window.__x = {{stream: {payload}}};</script></html>"


# ----------------------------------------------------------------------
# 1 / 2. can_handle
# ----------------------------------------------------------------------
def t_can_handle():
    print("\n[1] Huya URL can_handle")
    a = HuyaAdapter()
    check("https://www.huya.com/<room>", a.can_handle(HUYA_URL))
    check("带查询串的 URL", a.can_handle(HUYA_URL + "?from=home"))
    check("无 www 前缀", a.can_handle("https://huya.com/dank1ng"))
    check("http（非 https）", a.can_handle("http://www.huya.com/dank1ng"))
    check("大小写不敏感", a.can_handle("https://WWW.HUYA.COM/DANK1NG"))
    check("带尾部斜杠", a.can_handle("https://www.huya.com/dank1ng/"))

    print("\n[2] 非 Huya URL rejected")
    for bad in (
        "https://www.douyin.com/123456",
        "https://live.douyin.com/557481980778",
        "https://www.huya.com.cn/dank1ng",     # 后缀不同的域名
        "https://not-huya.com/dank1ng",
        "https://www.huya.com/",               # 首页，没有房间段
        "https://www.huya.com",
        "https://evil.com/www.huya.com/dank1ng",
        "ftp://www.huya.com/dank1ng",
        "",
        None,
    ):
        check(f"拒绝 {bad!r}", not a.can_handle(bad))


# ----------------------------------------------------------------------
# 3 / 4. JS 解析
# ----------------------------------------------------------------------
def t_parse():
    print("\n[3] stream JS 成功解析")
    html = load_fixture()
    obj = parse_stream_object(html)
    check("解析出 dict", isinstance(obj, dict))
    check("顶层键符合预期",
          set(obj.keys()) == {"count", "data", "vMultiStreamInfo",
                              "iFrameRate", "iWebDefaultBitRate"},
          str(sorted(obj.keys())))
    check("data 有 1 个房间节点", isinstance(obj.get("data"), list)
          and len(obj["data"]) == 1)

    # 裸键名兼容路径（页面形态变化时仍然可用）
    raw = 'var x = {stream: {data:[{gameLiveInfo:{uid:1},' \
          'gameStreamInfoList:[{sCdnType:"AL"}]}]}};'
    check("键名未加引号时也能解析",
          parse_stream_object(raw)["data"][0]["gameStreamInfoList"][0]["sCdnType"] == "AL")

    print("\n[4] gameStreamInfoList 找到")
    room = locate_room_data(obj)
    check("定位到房间节点", isinstance(room, dict))
    check("含 gameLiveInfo", bool(room.get("gameLiveInfo")))
    entries = room.get("gameStreamInfoList")
    check("gameStreamInfoList 非空列表",
          isinstance(entries, list) and len(entries) == 3, str(len(entries or [])))
    check("条目含构造 URL 所需字段",
          all(all(k in e for k in ("sCdnType", "sStreamName", "sFlvUrl",
                                   "sFlvUrlSuffix", "sFlvAntiCode"))
              for e in entries))


# ----------------------------------------------------------------------
# 5 - 11. 候选流
# ----------------------------------------------------------------------
def t_candidates():
    print("\n[5] FLV candidate 数量正确")
    room = locate_room_data(parse_stream_object(load_fixture()))
    cands = build_candidates(room["gameStreamInfoList"])
    check("生成 3 个候选", len(cands) == 3, str(len(cands)))
    check("全部是 StreamCandidate",
          all(isinstance(c, StreamCandidate) for c in cands))

    print("\n[10] protocol == flv")
    check("所有候选 protocol == 'flv'",
          all(c.protocol == "flv" for c in cands),
          str([c.protocol for c in cands]))

    print("\n[6] CDN 信息正确")
    cdn_order = [c.metadata.get("cdn") for c in cands]
    check("CDN 顺序与页面 gameStreamInfoList 一致", cdn_order == EXPECTED_CDNS,
          str(cdn_order))
    for c, cdn in zip(cands, EXPECTED_CDNS):
        check(f"{cdn} 的 sFlvUrl 正确", c.url.startswith(EXPECTED_FLV_BASE[cdn] + "/"),
              c.url.split("?")[0])

    print("\n[7] sStreamName 正确参与 URL 构造")
    for c, cdn in zip(cands, EXPECTED_CDNS):
        check(f"{cdn} URL 含 {EXPECTED_STREAM_NAME[:18]}...",
              f"/{EXPECTED_STREAM_NAME}." in c.url, c.url.split("?")[0])
        check(f"{cdn} metadata.stream_name 一致",
              c.metadata.get("stream_name") == EXPECTED_STREAM_NAME)

    print("\n[8] sFlvUrlSuffix 正确")
    for c, cdn in zip(cands, EXPECTED_CDNS):
        check(f"{cdn} 以 .flv 结尾（后缀来自页面）",
              c.url.split("?")[0].endswith(f"{EXPECTED_STREAM_NAME}.flv"))

    print("\n[9] AntiCode 正确进入 query")
    for c, entry in zip(cands, room["gameStreamInfoList"]):
        anti = entry["sFlvAntiCode"]
        base, sep, query = c.url.partition("?")
        check(f"{entry['sCdnType']} query 与 sFlvAntiCode 完全一致",
              sep == "?" and query == anti)
        check(f"{entry['sCdnType']} 含 wsSecret / wsTime",
              "wsSecret=" in query and "wsTime=" in query)
        check(f"{entry['sCdnType']} 未自行改写鉴权参数",
              query == entry["sFlvAntiCode"])

    print("\n[11] metadata 正确")
    check("metadata 只含真实页面字段 (cdn, stream_name)",
          all(set(c.metadata.keys()) == {"cdn", "stream_name"} for c in cands),
          str([sorted(c.metadata) for c in cands]))
    check("未伪造每流码率", all(c.bitrate is None for c in cands))
    check("未伪造清晰度名", all(c.quality is None for c in cands))
    check("未伪造分辨率/帧率",
          all(c.width is None and c.height is None and c.fps is None for c in cands))

    # URL 拼接规则本身
    check("build_flv_url 缺 AntiCode 时判为无效",
          build_flv_url({"sFlvUrl": "http://a/src", "sStreamName": "n",
                         "sFlvUrlSuffix": "flv"}) is None)
    check("build_flv_url 缺 sStreamName 时判为无效",
          build_flv_url({"sFlvUrl": "http://a/src", "sFlvUrlSuffix": "flv",
                         "sFlvAntiCode": "x=1"}) is None)


# ----------------------------------------------------------------------
# 12. 无有效流
# ----------------------------------------------------------------------
def t_no_stream():
    print("\n[12] 无有效流时抛 StreamResolveError")
    html = load_fixture()

    # 12a. 页面没有 stream 数据块
    check("无 stream 数据块",
          _raises(HuyaStreamError, lambda: parse_stream_object("<html>nothing</html>")))
    check("HuyaStreamError 是 StreamResolveError 子类",
          issubclass(HuyaStreamError, StreamResolveError))

    # 12b. gameStreamInfoList 为空
    empty = synth_html({"gameLiveInfo": {"nick": "x"}, "gameStreamInfoList": []})
    check("gameStreamInfoList 为空",
          _raises(StreamResolveError, lambda: _StubAdapter(empty).resolve(HUYA_URL)))

    # 12c. 条目都缺 AntiCode
    no_anti = synth_html({
        "gameLiveInfo": {"nick": "x"},
        "gameStreamInfoList": [
            {"sCdnType": "AL", "sStreamName": "n", "sFlvUrl": "http://a/src",
             "sFlvUrlSuffix": "flv"}],
    })
    check("条目缺 AntiCode -> 无有效候选",
          _raises(StreamResolveError, lambda: _StubAdapter(no_anti).resolve(HUYA_URL)))

    # 12d. 没有 gameLiveInfo（未开播）
    no_live = synth_html({"gameStreamInfoList": [
        {"sCdnType": "AL", "sStreamName": "n", "sFlvUrl": "http://a/src",
         "sFlvUrlSuffix": "flv", "sFlvAntiCode": "wsSecret=1"}]})
    check("缺 gameLiveInfo -> 判定为未开播",
          _raises(StreamResolveError, lambda: _StubAdapter(no_live).resolve(HUYA_URL)))

    # 12e. 网络失败
    check("页面请求失败",
          _raises(StreamResolveError, lambda: _StubAdapter(None).resolve(HUYA_URL)))

    # 12f. 非虎牙 URL 直接拒绝
    check("非虎牙 URL 在 resolve 层被拒",
          _raises(StreamResolveError,
                  lambda: _StubAdapter(html).resolve("https://live.douyin.com/123")))

    # 12g. 不依赖 iIsReplay / eLiveStatus / roomInfo
    check("fixture 中确实不存在 iIsReplay/eLiveStatus/roomInfo",
          all(t not in html for t in ("iIsReplay", "eLiveStatus", "roomInfo")))


# ----------------------------------------------------------------------
# 13. StreamResolver -> HuyaAdapter
# ----------------------------------------------------------------------
def t_resolver_integration():
    print("\n[13] StreamResolver -> HuyaAdapter")
    r = StreamResolver()
    check("registry 为虎牙 URL 选中 HuyaAdapter",
          isinstance(r.registry.get_adapter(HUYA_URL), HuyaAdapter))
    check("registry 为抖音 URL 仍选中 DouyinAdapter",
          isinstance(r.registry.get_adapter(
              "https://live.douyin.com/557481980778"), DouyinAdapter))

    # 用桩网络走一遍完整 resolve，验证 StreamInfo 形状
    adapter = _StubAdapter(load_fixture(),
                           video_info=VideoInfo(1280, 720, "yuv420p", 60.0, "h264"))
    info = adapter.resolve(HUYA_URL)

    check("返回 StreamInfo", isinstance(info, StreamInfo))
    check("platform == 'huya'", info.platform == "huya", str(info.platform))
    check("room_id 来自 URL 路径", info.room_id == "dank1ng", str(info.room_id))
    check("candidates 3 个", len(info.candidates) == 3)
    check("selected 是首个候选", info.selected is info.candidates[0])
    check("StreamInfo.url 来自 selected", info.url == info.candidates[0].url)
    check("StreamInfo.protocol == 'flv'", info.protocol == "flv")
    check("width/height 来自探测器", (info.width, info.height) == (1280, 720),
          f"{info.width}x{info.height}")
    check("只对选中候选做了一次探测", adapter.probed == info.url)

    # 探测失败时仍应拿到可用地址（与抖音一致）
    info2 = _StubAdapter(load_fixture(), video_info=None).resolve(HUYA_URL)
    check("探测失败仍返回地址", bool(info2.url))
    check("探测失败时 width 为 None（交给调用方兜底）", info2.width is None)

    # Resolver 抛出的是统一异常类型
    check("无适配器时抛 StreamResolveError",
          _raises(StreamResolveError,
                  lambda: r.resolve("https://www.example.com/room")))


# ----------------------------------------------------------------------
# 14. 默认 Registry
# ----------------------------------------------------------------------
def t_registry():
    print("\n[14] 默认 Registry 同时包含 Douyin + Huya")
    reg = build_default_registry()
    names = [type(a).__name__ for a in reg.adapters]
    check("注册表含 2 个适配器", len(reg.adapters) == 2, str(names))
    check("含 DouyinAdapter 与 HuyaAdapter",
          set(names) == {"DouyinAdapter", "HuyaAdapter"}, str(names))
    check("抖音在前（保持既有优先级）", names[0] == "DouyinAdapter", str(names))

    # 两个平台按主机名互斥
    for url, expect in (("https://live.douyin.com/1", "DouyinAdapter"),
                        ("https://www.huya.com/x", "HuyaAdapter")):
        got = type(reg.get_adapter(url)).__name__
        check(f"{url} -> {expect}", got == expect, got)


def main():
    print("=" * 60)
    print("Phase 2 虎牙适配器 单元测试（不联网）")
    print("=" * 60)
    if not os.path.exists(FIXTURE):
        print(f"缺少 fixture: {FIXTURE}")
        return 1
    for fn in (t_can_handle, t_parse, t_candidates, t_no_stream,
               t_resolver_integration, t_registry):
        fn()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} passed")
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
