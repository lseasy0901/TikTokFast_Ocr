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

import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.disable(logging.CRITICAL)

from stream.adapters.douyin import DouyinAdapter, DouyinStream  # noqa: E402
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
# B. 关注页链接的解析结果（RELEASE-02-B5-CLEANUP 后的新契约）
# ======================================================================
def test_follow_resolution() -> None:
    section("B. 关注页链接的解析结果（新契约：只给房间标识，不发请求）")

    # B1. room_id 来自路径 id，不是 anchor_id
    info = DouyinStream(FOLLOW_URL).get_stream_info()
    check(info.room_id == FOLLOW_ID, "room_id 来自路径里的 live 段")
    check(info.room_id != ANCHOR_ID, "anchor_id 绝不被当作房间号")

    # B2. 唯一拉流架构：适配器不再枚举任何流候选
    check(info.candidates == [], "candidates 恒为空（旧 _uhd 候选已整体移除）")
    check(info.selected is None, "selected 恒为 None")
    check(info.url is None, "适配器层不产出可播放地址（取流交给裸流页面直取）")

    # B3. 解析过程**不发任何网络请求**（旧实现在这里会请求页面/API）
    class _NoNet:
        """任何网络调用都视为失败：新架构下 resolve 应当纯本地。"""

        def __init__(self):
            self.calls = []

        def get(self, *a, **kw):
            self.calls.append(("get", a[:1]))
            raise AssertionError("新架构下 resolve 不应发起网络请求")

        def post(self, *a, **kw):
            self.calls.append(("post", a[:1]))
            raise AssertionError("新架构下 resolve 不应发起网络请求")

    ds = DouyinStream(FOLLOW_URL)
    ds._session = _NoNet()
    info = ds.get_stream_info()
    check(info.room_id == FOLLOW_ID, "纯本地解析出 room_id")
    check(ds._session.calls == [], "解析过程零网络请求")

    # B4. 非法房间号如实报错，不拿 anchor_id 顶替
    bad = ("https://www.douyin.com/follow/live/abc"
           "?anchor_id=111122930648")
    try:
        DouyinStream(bad).get_stream_info()
        check(False, "路径 id 非法时应当报错，而不是拿 anchor_id 顶替")
    except StreamResolveError as e:
        check("URL格式错误" in str(e), f"报错为 URL 格式错误：{e}")

    # B5. anchor_id 从未出现在任何 room_id 位置
    for url in (FOLLOW_URL,
                f"https://www.douyin.com/follow/live/{FOLLOW_ID}"
                f"?anchor_id=abc",
                f"https://www.douyin.com/follow/live/{FOLLOW_ID}"):
        got = DouyinStream._parse_room_id_candidates(url)
        check(got == [FOLLOW_ID] and ANCHOR_ID not in got,
              f"anchor_id 不作为房间号：{url}")


# ======================================================================
# C. 标准链接行为
# ======================================================================
def test_standard_url_unchanged() -> None:
    section("C. 标准直播链接行为")

    info = DouyinStream(ROOM_URL).get_stream_info()
    check(info.room_id == "123456789", "标准链接仍能解析")
    check(info.candidates == [], "标准链接同样不产出流候选")
    check(info.platform == "douyin", "platform 标识不变")

    # 非法 URL 仍然报 URL 格式错误，而不是"平台不支持"
    try:
        DouyinStream("https://live.douyin.com/").get_room_id()
        check(False, "缺少房间号的标准链接应当报错")
    except StreamResolveError as e:
        check("URL格式错误" in str(e), f"报错为 URL 格式错误：{e}")


# ======================================================================
# D. 真实网络（默认跳过）—— 验证房间识别 + 裸流取流
# ======================================================================
def test_live_network(url: str) -> None:
    section("D. 真实网络验证（会真的访问抖音）")
    print(f"  目标 URL: {url}")
    ds = DouyinStream(url)
    try:
        room_id = ds.get_room_id()
        print(f"  room_id: {room_id}")
        check(True, "真实网络下房间识别成功")
    except Exception as e:  # noqa: BLE001
        print(f"  异常类型: {type(e).__name__}")
        print(f"  异常内容: {e}")
        check(False, f"真实网络房间识别失败：{type(e).__name__}")
        return

    # 唯一取流入口的真实网络验证（页面直取裸流）
    try:
        from stream.bare_source import probe_video_bare
        probe = probe_video_bare("douyin", room_id)
        print(f"  取流结论: reason={probe.reason} live={probe.live} "
              f"url={(probe.url or '')[:80]}")
        check(probe.reason in ("ok", "offline", "no_bare_url", "fetch_failed"),
              f"取流返回结构化结论：{probe.reason}")
    except Exception as e:  # noqa: BLE001
        check(False, f"真实网络取流异常：{type(e).__name__}: {e}")


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
        print("         A/B/C 全部是纯本地断言（新架构下 resolve 不发请求）。")

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
