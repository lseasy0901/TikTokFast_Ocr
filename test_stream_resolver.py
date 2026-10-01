# -*- coding: utf-8 -*-
"""平台流解析解耦回归测试（Phase 1）

覆盖目标：
    1. 房间 URL -> room_id
    2. pace_f 解析路径
    3. RENDER_DATA 解析路径
    4. 正则 fallback
    5. API fallback
    6. FLV 优先于 HLS
    7. 清晰度选择顺序
    8. candidate -> selected stream
    9. 宽高信息传递
    10. 非法 URL / 无流异常
    11. MainWindow -> StreamResolver -> PlatformAdapter 链路

全部使用固定 fixture + 桩网络，不依赖实时抖音页面。
"""

import json
import logging
import os
import sys
import threading
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.disable(logging.CRITICAL)

import numpy as np  # noqa: E402

from stream.adapters.douyin import (  # noqa: E402
    DouyinAdapter, DouyinStream, DouyinStreamError,
)
from stream.models import StreamCandidate, StreamInfo, StreamResolveError  # noqa: E402
from stream.registry import PlatformRegistry, build_default_registry  # noqa: E402
from stream.resolver import StreamResolver  # noqa: E402
from stream.selector import select  # noqa: E402
from stream.video_info import VideoInfo  # noqa: E402

ROOM_URL = "https://live.douyin.com/123456789"
ROOM_ID = "123456789"

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))


# ----------------------------------------------------------------------
# 固定 fixture
# ----------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, text="", payload=None, url=""):
        self.status_code = status_code
        self.text = text
        self.url = url
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    """按 URL 分发的 requests.Session 桩"""

    def __init__(self, page_html="", api_payload=None, api_status=200):
        self.page_html = page_html
        # 默认给一个"接口正常但房间里没有流"的响应，让 fallback 链的终点可确定
        self.api_payload = ({"status_code": 0, "data": {"data": []}}
                            if api_payload is None else api_payload)
        self.api_status = api_status
        self.cookies = self

    def get_dict(self):
        return {}

    def update(self, cookies):
        """Cookie 持久化回灌用（_ensure_session_cookies）；桩无状态，直接忽略"""
        return None

    def get(self, url, **kw):
        if url.rstrip("/") == "https://live.douyin.com":
            return _FakeResp(200, "<html>home</html>", url=url)
        return _FakeResp(200, self.page_html, url=url)

    def post(self, url, **kw):
        return _FakeResp(self.api_status, "", self.api_payload, url=url)


_ESCAPED_MARKER = '\\"stream_url\\":'


def pace_f_html(stream_url_obj):
    """构造含 __pace_f chunk 的页面（JS 转义形态，与真实页面一致）"""
    js = json.dumps(stream_url_obj, separators=(",", ":")).replace('"', '\\"')
    chunk = "prefix" + _ESCAPED_MARKER + js + "suffix"
    return '<html><script>self.__pace_f.push([1,"%s"]);</script></html>' % chunk


def render_data_html(stream_data_obj, status=2):
    """构造含 RENDER_DATA 的页面"""
    payload = {
        "__DEFAULT_SCOPE__": {
            "roomInfo": {
                "room": {"status": status, "status_msg": ""},
                "liveStreamInfo": {
                    "streamData": json.dumps(stream_data_obj, separators=(",", ":")),
                },
            }
        }
    }
    return ('<html><script id="RENDER_DATA" type="application/json">%s</script></html>'
            % quote(json.dumps(payload)))


def regex_html(flv="http://pull-flv-l1.flive.douyincdn.com/room/abc.flv?e=1&d=2",
               hls=None):
    parts = [f'<div data-u="{flv}"></div>']
    if hls:
        parts.append(f'<div data-u="{hls}"></div>')
    return "<html><body>" + "".join(parts) + "</body></html>"


def api_payload(stream_url_obj, status=2):
    return {
        "status_code": 0,
        "data": {"data": [{"status": status, "stream_url": stream_url_obj}]},
    }


def new_stream(page_html="", api_payload_=None):
    """构造一个网络被完全桩住的 DouyinStream"""
    ds = DouyinStream(ROOM_URL)
    ds._session = _FakeSession(page_html=page_html, api_payload=api_payload_)
    return ds


def stub_resolution(ds, w=1280, h=720):
    ds._detect_video_resolution = lambda url: VideoInfo(w, h, "yuv420p", 30.0, "h264")
    return ds


FLV_HD1 = "http://pull-flv-l1.douyincdn.com/a/hd1.flv?x=1"
FLV_FULLHD = "http://pull-flv-l1.douyincdn.com/a/fullhd.flv?x=1"
HLS_HD1 = "http://pull-hls-l1.douyincdn.com/a/hd1.m3u8?x=1"


# ----------------------------------------------------------------------
# 1. room_id
# ----------------------------------------------------------------------
def t_room_id():
    print("\n[1] 房间 URL -> room_id")
    check("合法 URL 解析出 room_id", DouyinStream(ROOM_URL).get_room_id() == ROOM_ID)
    check("带查询串的 URL 仍可解析",
          DouyinStream(ROOM_URL + "?foo=1").get_room_id() == ROOM_ID)
    check("非抖音 URL 抛 DouyinStreamError",
          _raises(DouyinStreamError, lambda: DouyinStream("https://www.huya.com/x").get_room_id()))
    check("空 URL 抛 DouyinStreamError",
          _raises(DouyinStreamError, lambda: DouyinStream("").get_room_id()))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ----------------------------------------------------------------------
# 2. pace_f
# ----------------------------------------------------------------------
def t_pace_f():
    print("\n[2] pace_f 解析路径")
    obj = {"flv_pull_url": {"HD1": FLV_HD1}, "hls_pull_url": {"HD1": HLS_HD1}}
    ds = stub_resolution(new_stream(pace_f_html(obj)))
    result = ds.get_stream_url()
    check("pace_f 命中并返回 flv", result["stream_type"] == "flv", str(result))
    check("返回的 URL 正确", result["stream_url"] == FLV_HD1)
    check("room_id 一致", result["room_id"] == ROOM_ID)

    info = stub_resolution(new_stream(pace_f_html(obj))).get_stream_info()
    check("StreamInfo 候选数 >= 2", len(info.candidates) >= 2, str(len(info.candidates)))
    check("StreamInfo.selected 是第一个候选",
          info.selected is info.candidates[0])


# ----------------------------------------------------------------------
# 3. RENDER_DATA
# ----------------------------------------------------------------------
def t_render_data():
    print("\n[3] RENDER_DATA 解析路径")
    obj = {"flv_pull_url": {"HD1": FLV_HD1}}
    ds = stub_resolution(new_stream(render_data_html(obj)))
    result = ds.get_stream_url()
    check("RENDER_DATA 命中", result["stream_url"] == FLV_HD1, str(result))

    # 未开播（status != 2）
    # 注意：RENDER_DATA 分支的 except 只记日志、不 re-raise（既有行为），
    # 所以"未开播"会被吞掉并继续走后面的 fallback，最终以链尾的错误抛出。
    # 这里只断言异常类型，不锁定消息 —— 本阶段不改变既有回退语义。
    ds = stub_resolution(new_stream(render_data_html(obj, status=4)))
    check("RENDER_DATA 未开播最终抛 DouyinStreamError",
          _raises(DouyinStreamError, ds.get_stream_url))


# ----------------------------------------------------------------------
# 4. regex fallback
# ----------------------------------------------------------------------
def t_regex():
    print("\n[4] 正则 fallback")
    ds = stub_resolution(new_stream(regex_html()))
    result = ds.get_stream_url()
    check("正则命中 flv", result["stream_type"] == "flv" and ".flv" in result["stream_url"],
          str(result))

    hls_url = "http://pull-hls-l1.flive.douyincdn.com/room/abc.m3u8?e=1"
    ds = stub_resolution(new_stream(regex_html(flv="http://example.com/x.txt", hls=hls_url)))
    result = ds.get_stream_url()
    check("无 flv 时回退 hls", result["stream_type"] == "hls", str(result))

    info = stub_resolution(new_stream(regex_html())).get_stream_info()
    check("正则路径退化为单候选", len(info.candidates) == 1, str(len(info.candidates)))
    check("正则路径 selected 可用", info.url == info.candidates[0].url)


# ----------------------------------------------------------------------
# 5. API fallback
# ----------------------------------------------------------------------
def t_api():
    print("\n[5] API fallback")
    obj = {"flv_pull_url": {"HD1": FLV_HD1}}
    ds = stub_resolution(new_stream("<html></html>", api_payload(obj)))
    result = ds.get_stream_url()
    check("页面无数据时走 API", result["stream_url"] == FLV_HD1, str(result))

    ds = stub_resolution(new_stream("<html></html>", api_payload(obj, status=1)))
    try:
        ds.get_stream_url()
        check("API 未开播抛异常", False, "no exception")
    except DouyinStreamError as e:
        check("API 未开播抛 DouyinStreamError", "未开播" in str(e), str(e))


# ----------------------------------------------------------------------
# 6. FLV 优先于 HLS
# ----------------------------------------------------------------------
def t_flv_first():
    print("\n[6] FLV 优先于 HLS")
    obj = {"hls_pull_url": {"HD1": HLS_HD1}, "flv_pull_url": {"HD1": FLV_HD1}}
    ds = stub_resolution(new_stream(pace_f_html(obj)))
    check("flv_pull_url 优先于 hls_pull_url",
          ds.get_stream_url()["stream_url"] == FLV_HD1)
    info = stub_resolution(new_stream(pace_f_html(obj))).get_stream_info()
    check("候选列表中 flv 排在 hls 之前",
          info.candidates[0].protocol == "flv"
          and any(c.protocol == "hls" for c in info.candidates))

    # 只有 hls 时用 hls（不凭空造 flv）
    obj_hls_only = {"hls_pull_url": {"HD1": HLS_HD1}}
    ds = stub_resolution(new_stream(pace_f_html(obj_hls_only)))
    check("只有 hls 时返回 hls", ds.get_stream_url()["stream_url"] == HLS_HD1)


# ----------------------------------------------------------------------
# 7. 清晰度选择顺序
# ----------------------------------------------------------------------
def t_quality_order():
    print("\n[7] 清晰度选择顺序")
    url_of = lambda k: f"http://pull-flv-l1.douyincdn.com/a/{k}.flv"

    ds = stub_resolution(new_stream(pace_f_html(
        {"flv_pull_url": {"SD": url_of("sd"), "HD1": url_of("hd1"),
                          "FULL_HD1": url_of("fhd")}})))
    check("FULL_HD1 > HD1 > SD（原画优先，第 2 步修订）",
          ds.get_stream_url()["stream_url"] == url_of("fhd"))

    ds = stub_resolution(new_stream(pace_f_html(
        {"flv_pull_url": {"SD": url_of("sd"), "FULL_HD1": url_of("fhd")}})))
    check("无 HD1 时取 FULL_HD1", ds.get_stream_url()["stream_url"] == url_of("fhd"))

    ds = stub_resolution(new_stream(pace_f_html(
        {"flv_pull_url": {"SD": url_of("sd")}})))
    check("仅有 SD 时取 SD", ds.get_stream_url()["stream_url"] == url_of("sd"))

    # 未知键回退到字典顺序的第一个
    ds = stub_resolution(new_stream(pace_f_html(
        {"flv_pull_url": {"WEIRD_A": url_of("wa"), "WEIRD_B": url_of("wb")}})))
    check("未知键回退到字典序第一个", ds.get_stream_url()["stream_url"] == url_of("wa"))

    # 完整顺序枚举（第 2 步修订：原画优先，见 _QUALITY_PRIORITY 注释）
    all_keys = ("FULL_HD1", "full", "origin", "FHD1", "FHD",
                "HD1", "HD", "SD1", "SD2", "SD", "0", "1")
    info = stub_resolution(new_stream(pace_f_html(
        {"flv_pull_url": {k: url_of(k) for k in all_keys}}))).get_stream_info()
    got = [c.quality for c in info.candidates]
    check("枚举顺序与 _QUALITY_PRIORITY 一致", got == list(all_keys), str(got))

    # 字符串字段
    ds = stub_resolution(new_stream(pace_f_html({"pull_url": url_of("only")})))
    check("字符串字段可用", ds.get_stream_url()["stream_url"] == url_of("only"))


# ----------------------------------------------------------------------
# 8. candidate -> selected
# ----------------------------------------------------------------------
def t_selection():
    print("\n[8] candidate -> selected stream")
    a = StreamCandidate(url="u1", protocol="flv", quality="HD1")
    b = StreamCandidate(url="u2", protocol="hls", quality="HD1")
    check("select 取第一个", select([a, b]) is a)
    check("空列表返回 None", select([]) is None)
    check("跳过无 url 的候选",
          select([StreamCandidate(url="", protocol="flv"), b]) is b)

    info = StreamInfo(platform="douyin", room_id="1", candidates=[a, b], selected=select([a, b]))
    check("StreamInfo.url 来自 selected", info.url == "u1")
    check("StreamInfo.protocol 来自 selected", info.protocol == "flv")
    check("无 selected 时 url 为 None", StreamInfo(platform="x", room_id="1").url is None)


# ----------------------------------------------------------------------
# 9. 宽高传递
# ----------------------------------------------------------------------
def t_size():
    print("\n[9] 宽高信息传递")
    obj = {"flv_pull_url": {"HD1": FLV_HD1}}
    # 迁移审计刀1 新契约：解析层不再探测分辨率（FFmpegReader 从子进程
    # stderr 自报，同一连接零额外开销），StreamInfo.width 恒为 None。
    info = stub_resolution(new_stream(pace_f_html(obj)), 1280, 720).get_stream_info()
    check("width 恒 None（探测已下沉到 reader）", info.width is None)
    check("height 恒 None", info.height is None)

    ds = new_stream(pace_f_html(obj))
    ds._detect_video_resolution = lambda url: None      # 旧钩子保留：不应再被调用
    info = ds.get_stream_info()
    check("无探测路径下 width 仍为 None", info.width is None)
    check("仍能拿到地址", info.url == FLV_HD1)


# ----------------------------------------------------------------------
# 10. 非法 URL / 无流
# ----------------------------------------------------------------------
def t_errors():
    print("\n[10] 非法 URL / 无流异常")
    # 页面完全无数据，且 API 返回一个"已开播但没有可用流"的房间
    empty_room = api_payload({})
    ds = stub_resolution(new_stream("<html>nothing here</html>", empty_room))
    try:
        ds.get_stream_url()
        check("无流时抛异常", False, "no exception")
    except DouyinStreamError as e:
        check("无流时抛 DouyinStreamError", "未找到可用流地址" in str(e), str(e))

    # 未注册平台的 URL：resolver 层在发请求之前就拒绝。
    # 注意：Phase 2 起 huya.com 已被 HuyaAdapter 接管，不再是"未支持平台"，
    # 这里改用仍未接入的平台，避免测试退化成一次真实网络请求。
    r = StreamResolver()
    try:
        r.resolve("https://www.douyu.com/9999")
        check("未支持平台 URL 在 resolver 层被拒", False, "no exception")
    except StreamResolveError as e:
        check("未支持平台 URL 抛 StreamResolveError", "直播平台" in str(e), str(e))

    try:
        r.resolve("")
        check("空 URL 抛 StreamResolveError", False, "no exception")
    except StreamResolveError:
        check("空 URL 抛 StreamResolveError", True)

    check("DouyinStreamError 是 StreamResolveError 子类",
          issubclass(DouyinStreamError, StreamResolveError))


# ----------------------------------------------------------------------
# 11. MainWindow -> Resolver -> Adapter
# ----------------------------------------------------------------------
def t_end_to_end():
    print("\n[11] MainWindow -> StreamResolver -> PlatformAdapter 链路")
    from gui.main_window import _ConnectWorker

    obj = {"flv_pull_url": {"HD1": FLV_HD1}, "hls_pull_url": {"HD1": HLS_HD1}}
    page = pace_f_html(obj)

    orig_fetch = DouyinStream._fetch_room_page
    DouyinStream._fetch_room_page = lambda self: page
    # 刀1 后解析层不再调用 _detect_video_resolution（方法已移除），无需再桩
    try:
        got = {}

        def on_success(info):
            got["info"] = info

        def on_error(msg):
            got["error"] = msg

        resolver = StreamResolver()
        w = _ConnectWorker(live_url=ROOM_URL, on_success=on_success,
                           on_error=on_error, resolver=resolver)
        w.start()
        for _ in range(200):
            if got:
                break
            time.sleep(0.02)

        check("worker 回调成功", "info" in got, str(got.get("error")))
        info = got.get("info")
        check("回调拿到的是 StreamInfo", isinstance(info, StreamInfo))
        if isinstance(info, StreamInfo):
            check("platform=douyin", info.platform == "douyin")
            check("room_id 正确", info.room_id == ROOM_ID)
            check("url 正确", info.url == FLV_HD1)
            check("protocol 正确", info.protocol == "flv")
            check("width/height 恒 None（探测已下沉到 reader）",
                  (info.width, info.height) == (None, None))
            check("candidates 非空", len(info.candidates) >= 2)
    finally:
        DouyinStream._fetch_room_page = orig_fetch

    # 失败路径也要走通
    orig_fetch = DouyinStream._fetch_room_page
    orig_init_session = DouyinStream._init_session
    orig_api = DouyinStream._get_stream_from_api
    DouyinStream._fetch_room_page = lambda self: "<html>nothing</html>"
    # 页面无数据时的 API 兜底改桩：这条测试验证"失败 → error 回调"的接线，
    # 不应依赖真实抖音接口的可达性（限频/风控会让它挂满超时窗口）
    def _stub_api(self):
        raise DouyinStreamError("[API] 桩：接口不可用")
    DouyinStream._get_stream_from_api = _stub_api
    DouyinStream._init_session = lambda self: None
    try:
        got = {}
        w = _ConnectWorker(live_url=ROOM_URL, on_success=lambda i: got.setdefault("info", i),
                           on_error=lambda m: got.setdefault("error", m),
                           resolver=StreamResolver())
        w.start()
        for _ in range(200):
            if got:
                break
            time.sleep(0.02)
        check("失败路径回调 error", "error" in got and "info" not in got, str(got))
    finally:
        DouyinStream._fetch_room_page = orig_fetch
        DouyinStream._init_session = orig_init_session
        DouyinStream._get_stream_from_api = orig_api

    # registry / resolver 结构
    # Phase 2 起注册表同时包含抖音与虎牙；抖音仍注册在首位，且按主机名互斥，
    # 因此抖音的解析路径不受影响（这里只断言"含抖音"与"抖音优先"）。
    reg = build_default_registry()
    check("默认注册表含 DouyinAdapter", isinstance(reg.adapters[0], DouyinAdapter))
    check("注册表含 2 个适配器（抖音 + 虎牙）", len(reg.adapters) == 2,
          str([type(a).__name__ for a in reg.adapters]))
    check("registry 按 URL 选中 adapter",
          isinstance(StreamResolver().registry.get_adapter(ROOM_URL), DouyinAdapter))
    check("重复注册被忽略",
          (lambda r: (r.register(DouyinAdapter()), len(r.adapters) == 1)[1])(PlatformRegistry()))


def main():
    print("=" * 60)
    print("Phase 1 平台流解析解耦 回归测试")
    print("=" * 60)
    for fn in (t_room_id, t_pace_f, t_render_data, t_regex, t_api, t_flv_first,
               t_quality_order, t_selection, t_size, t_errors, t_end_to_end):
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
