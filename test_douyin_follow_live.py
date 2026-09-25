#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音「关注页直播」链接兼容 —— 验证脚本

问题链接（真实用户上报）：
    https://www.douyin.com/follow/live/242240793655?anchor_id=111122930648

房间号由**结构**决定，不是由最长数字或 URL 形态决定：
    房间号 = 路径里「直播语义段」后面紧跟的那个数字段。
因此 /live/{id}、/follow/live/{id}、/root/live/{id} 走同一条规则；
anchor_id / r_id / r_player 等与房间号无关的参数一律忽略。

覆盖：
    A. URL → 适配器匹配 & room_id 提取（纯结构，不发请求）
    B. 关注页链接的解析流程（网络全部打桩）
    C. 标准直播链接的行为没有改变
    D. 真实网络验证（默认跳过，需显式开启）

**关于"模拟通过"与"真实播放成功"的区别**：
    A/B/C 全部使用桩网络，只能证明"给定这些响应时程序判断正确"，
    不能证明抖音线上接口现在就是这个样子。
    D 节才是真实请求，默认不跑（需要联网，且线上页面随时可能变）。
    真实结论请看 --live 模式输出与任务报告。

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_douyin_follow_live.py`` 运行，不依赖 pytest。
"""

import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

logging.disable(logging.CRITICAL)

from stream.adapters.douyin import (  # noqa: E402
    DouyinAdapter,
    DouyinStream,
    DouyinStreamError,
)
from stream.models import StreamResolveError  # noqa: E402
from stream.registry import build_default_registry  # noqa: E402

FOLLOW_URL = ("https://www.douyin.com/follow/live/242240793655"
              "?anchor_id=111122930648")
FOLLOW_ID = "242240793655"
ANCHOR_ID = "111122930648"
ROOM_URL = "https://live.douyin.com/123456789"

_PASSED = 0
_FAILED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {message}")
        return True
    _FAILED += 1
    print(f"  [FAIL] {message}")
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ======================================================================
# 桩网络
# ======================================================================
class _FakeResp:
    def __init__(self, status_code, text, payload=None, url=""):
        self.status_code = status_code
        self.text = text
        self.url = url
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    """按 room_id 分发的会话桩，记录每一次请求。

    :param streams: {room_id: 播放地址}；不在表里的 room_id 视为"无可播放地址"
    :param errors:  {room_id: 异常}；命中时该候选直接抛错（模拟未开播等）
    """

    def __init__(self, streams=None, errors=None, status_code=0):
        self.streams = streams or {}
        self.errors = errors or {}
        self.status_code = status_code
        self.cookies = self
        self.requested_room_ids: list[str] = []

    def get_dict(self):
        return {}

    def get(self, url, **kw):
        # 首页取 Cookie / 直播间页面：一律返回空页面，
        # 让解析链落到 API 分支，由房间号决定成败
        return _FakeResp(200, "<html></html>", url=url)

    def post(self, url, **kw):
        room_id = str((kw.get("params") or {}).get("room_id", ""))
        self.requested_room_ids.append(room_id)

        if room_id in self.errors:
            raise self.errors[room_id]

        stream_url = self.streams.get(room_id)
        if stream_url is None:
            # 接口正常，但这个 id 换不出房间
            return _FakeResp(200, "", {"status_code": 0,
                                       "data": {"data": []}}, url=url)
        return _FakeResp(200, "", {
            "status_code": self.status_code,
            "data": {"data": [{
                "status": 2,
                "stream_url": {"flv_pull_url": {"FULL_HD1": stream_url}},
            }]},
        }, url=url)


def new_stream(url, **session_kw):
    """构造一个网络被完全桩住的 DouyinStream，并跳过分辨率探测。"""
    ds = DouyinStream(url)
    ds._session = _FakeSession(**session_kw)
    ds._detect_video_resolution = lambda u: None
    return ds


# ======================================================================
# A. URL 解析（纯字符串，不发请求）
# ======================================================================
def test_url_parsing() -> None:
    section("A. URL → 适配器匹配 & 候选提取")

    registry = build_default_registry()
    adapter = DouyinAdapter()

    # A1. 标准直播链接：一个候选，行为与从前一致
    check(adapter.can_handle(ROOM_URL), "标准直播链接被抖音适配器接住")
    check(
        DouyinStream._parse_room_id_candidates(ROOM_URL) == ["123456789"],
        "标准链接只给出一个候选",
    )
    check(registry.get_adapter(ROOM_URL) is not None, "注册表能匹配标准链接")

    # A2. 关注页链接：回归点 —— 从前这个链接根本进不来
    check(
        adapter.can_handle(FOLLOW_URL),
        "关注页链接被抖音适配器接住（回归：此前 can_handle 直接拒绝）",
    )
    check(
        registry.get_adapter(FOLLOW_URL) is not None,
        "注册表能匹配关注页链接（回归：此前报『没有可处理该地址的直播平台』）",
    )
    check(
        DouyinStream._parse_room_id_candidates(FOLLOW_URL) == [FOLLOW_ID],
        "只认路径里 live 后面的 id；anchor_id 不再作为候选",
    )

    # A3. 缺少 anchor_id
    check(
        DouyinStream._parse_room_id_candidates(
            f"https://www.douyin.com/follow/live/{FOLLOW_ID}") == [FOLLOW_ID],
        "缺少 anchor_id 时只给出路径 id 一个候选",
    )
    check(
        DouyinStream._parse_room_id_candidates(
            f"https://www.douyin.com/follow/live/{FOLLOW_ID}?anchor_id=abc")
        == [FOLLOW_ID],
        "anchor_id 非数字时忽略它，不会当成候选",
    )

    # A4. 非直播链接：不能因为放宽了域名就把整站认领进来
    invalid = (
        "https://www.douyin.com/video/123456",
        "https://www.douyin.com/user/abc",
        "https://www.douyin.com/search/123456",
        "https://www.douyin.com/discover",
        "https://www.douyin.com/",
        "https://www.douyin.com/video/999?anchor_id=123",
        "https://example.com/follow/live/123",
        "https://www.bilibili.com/live/123",
        "",
    )
    for url in invalid:
        check(
            not adapter.can_handle(url) and not registry.get_adapter(url),
            f"非直播/无关链接仍被拒绝：{url or '(空)'}",
        )

    # A5. 直播语义成立但房间号非法：**要**被接住，报"URL格式错误"而不是
    #     "没有可处理该地址的直播平台" —— 后者对用户是误导
    for url in ("https://www.douyin.com/follow/live/abc",
                "https://www.douyin.com/live/",
                "https://live.douyin.com/"):
        check(adapter.can_handle(url),
              f"直播语义成立就该被接住（房间号的合法性留给 resolve 报错）：{url}")
        check(DouyinStream._parse_room_id_candidates(url) == [],
              f"取不到合法房间号时返回空（不猜）：{url}")
        try:
            DouyinStream(url).get_room_id()
            check(False, f"非法房间号应当报错：{url}")
        except StreamResolveError as e:
            check("URL格式错误" in str(e), f"报错为 URL 格式错误：{e}")


# ======================================================================
# B. 关注页解析流程（桩网络）
# ======================================================================
def test_follow_resolution() -> None:
    section("B. 关注页链接的解析流程（桩网络）")

    # B1. 主候选（路径 id）就能换出流 —— 不该再去试 anchor_id
    ds = new_stream(FOLLOW_URL, streams={FOLLOW_ID: "http://x/a.flv"})
    info = ds.get_stream_info()
    check(info.room_id == FOLLOW_ID, "主候选成功时 room_id 来自该候选")
    check(ds._session.requested_room_ids == [FOLLOW_ID],
          f"主候选成功时只请求一次（实际 {ds._session.requested_room_ids}）")
    check(len(info.candidates) >= 1, "仍然走既有的候选流/选流逻辑")

    # B2. 关键：路径 id 换不出房间时，**不允许**拿 anchor_id 当房间号去试
    ds = new_stream(FOLLOW_URL, streams={ANCHOR_ID: "http://x/b.flv"})
    try:
        ds.get_stream_info()
        check(False, "路径 id 取不到流时应当报错，而不是拿 anchor_id 顶替")
    except StreamResolveError:
        check(True, "路径 id 取不到流时如实报错")
    check(ds._session.requested_room_ids == [FOLLOW_ID],
          f"anchor_id 绝不被当作房间号请求（实际 {ds._session.requested_room_ids}）")

    # B3. 取不到流 → 报错，错误里的 room_id 就是路径 id
    ds = new_stream(FOLLOW_URL, streams={})
    try:
        ds.get_stream_info()
        check(False, "取不到流时应当抛错")
    except StreamResolveError as e:
        check(bool(str(e)), f"报错信息可读：{e}")
        check(ds.room_id == FOLLOW_ID, "失败后 room_id 仍是路径 id（便于排查）")
    check(ds._session.requested_room_ids == [FOLLOW_ID], "只请求了路径 id")

    # B4. 路径 id 抛"未开播"之类的明确错误 → 如实抛出，不拿 anchor_id 顶替
    ds = new_stream(
        FOLLOW_URL,
        streams={ANCHOR_ID: "http://x/c.flv"},
        # 用生产代码真正会抛的那个类型：直播信息链对明确的 DouyinStreamError
        # 是"原样上抛"，对其它异常才会吞掉并落到"所有解析方式均失败"
        errors={FOLLOW_ID: DouyinStreamError("房间未开播")},
    )
    try:
        ds.get_stream_info()
        check(False, "明确错误应当抛出")
    except StreamResolveError as e:
        check("未开播" in str(e), f"原样抛出路径 id 的错误：{e}")
    check(ds._session.requested_room_ids == [FOLLOW_ID],
          "明确错误后也不再尝试 anchor_id")

    # B5. 抛出的错误里带的就是路径 id（用户给的那个 id）
    ds = new_stream(
        FOLLOW_URL,
        errors={FOLLOW_ID: StreamResolveError("房间未开播"),
                ANCHOR_ID: StreamResolveError("主播不存在")},
    )
    try:
        ds.get_stream_info()
        check(False, "抛错时应当抛出异常")
    except StreamResolveError as e:
        check(FOLLOW_ID in str(e), f"抛出的是路径 id 的失败信息：{e}")
    check(ANCHOR_ID not in ds._session.requested_room_ids,
          "anchor_id 从未被请求过")

    # B6. 请求失败（网络层异常）不被吞掉
    ds = new_stream(FOLLOW_URL, errors={
        FOLLOW_ID: ConnectionError("连接超时"),
        ANCHOR_ID: StreamResolveError("主播不存在"),
    })
    try:
        ds.get_stream_info()
        check(False, "网络失败应当向上报告，不能静默返回空")
    except Exception as e:  # noqa: BLE001
        check(isinstance(e, (ConnectionError, StreamResolveError)),
              f"网络失败被如实抛出：{type(e).__name__}: {e}")


# ======================================================================
# C. 标准链接行为不变
# ======================================================================
def test_standard_url_unchanged() -> None:
    section("C. 标准直播链接行为不变")

    ds = new_stream(ROOM_URL, streams={"123456789": "http://x/d.flv"})
    info = ds.get_stream_info()
    check(info.room_id == "123456789", "标准链接仍能解析")
    check(ds._session.requested_room_ids == ["123456789"],
          f"标准链接只请求一次（实际 {ds._session.requested_room_ids}）")

    ds = new_stream(ROOM_URL, streams={})
    try:
        ds.get_stream_info()
        check(False, "标准链接无流时应当报错")
    except StreamResolveError:
        check(True, "标准链接无流时报错行为不变")

    # 非法 URL 仍然报 URL 格式错误，而不是"平台不支持"
    ds = new_stream("https://live.douyin.com/")
    try:
        ds.get_room_id()
        check(False, "缺少房间号的标准链接应当报错")
    except StreamResolveError as e:
        check("URL格式错误" in str(e), f"报错为 URL 格式错误：{e}")


# ======================================================================
# D. 真实网络（默认跳过）
# ======================================================================
def test_live_network(url: str) -> None:
    section("D. 真实网络验证（会真的访问抖音）")
    print(f"  目标 URL: {url}")
    ds = DouyinStream(url)
    try:
        candidates = DouyinStream._parse_room_id_candidates(url)
        print(f"  候选 id: {candidates}")
        room_id = ds.get_room_id()
        print(f"  主候选 room_id: {room_id}")
        info = ds.get_stream_info()
        print(f"  成功: room_id={info.room_id} 候选数={len(info.candidates)}")
        print(f"  选中地址: {info.selected.url[:100]}")
        check(True, "真实网络解析成功")
    except Exception as e:  # noqa: BLE001
        print(f"  失败阶段: 候选={ds._room_id_candidates or '(未解析)'} "
              f"当前 room_id={ds.room_id}")
        print(f"  异常类型: {type(e).__name__}")
        print(f"  异常内容: {e}")
        check(False, f"真实网络解析失败（不是模拟问题）：{type(e).__name__}")


def main() -> int:
    test_url_parsing()
    test_follow_resolution()
    test_standard_url_unchanged()

    if "--live" in sys.argv:
        idx = sys.argv.index("--live")
        target = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else FOLLOW_URL
        test_live_network(target)
    else:
        section("D. 真实网络验证")
        print("  [SKIP] 默认不跑真实网络。加 --live [URL] 才会真的访问抖音。")
        print("         上面的 A/B/C 全部是桩网络 —— 只能证明判断逻辑正确，")
        print("         不能证明线上接口现在就是这个响应。")

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
