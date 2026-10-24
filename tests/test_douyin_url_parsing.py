#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音直播间 URL 结构解析 —— 参数化验证

规则（唯一一条）：
    房间号 = 路径里「直播语义段」后面紧跟的那个数字段。

因此 /live/{id}、/follow/live/{id}、/root/live/{id} 落到同一条规则，
不需要为每种形态各写一条正则。

明确不做的事（每一项都有对应用例）：
    不取最长数字；不拿 anchor_id / r_id / r_player 当房间号；不按 URL 长度猜；
    路径里另有数字但不在 live 语义位置上时，不认它；无法确定时返回"无法识别"。

覆盖（题目要求的 11 类）：
    A. 当前成功 URL            F. query 参数干扰
    B. 当前失败 URL            G. anchor_id 干扰
    C. /live/<id>              H. 非直播 Douyin URL
    D. /follow/live/<id>       I. 非 Douyin URL
    E. /root/live/<id>         J. malformed URL
                               K. path 多个数字但只有一个在 live 语义位上
    L. query fallback（room_id / webcast_id）
    M. 适配器匹配与注册表路由（能接住 ⟺ 解析得出，不分叉）

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_douyin_url_parsing.py`` 运行，不依赖 pytest。
纯字符串判断，不联网、不起线程。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from stream.adapters.douyin import DouyinAdapter, DouyinStream  # noqa: E402
from stream.models import StreamResolveError                    # noqa: E402
from stream.registry import build_default_registry              # noqa: E402

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
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def expect_id(url: str, expected: str, note: str) -> None:
    """该 URL 必须解析出指定的房间号。"""
    got = DouyinStream._parse_room_id_candidates(url)
    check(got == [expected],
          f"{note} -> {expected}（实际 {got}）")


def expect_rejected(url: str, note: str) -> None:
    """该 URL 必须完全不被当作直播间（不误解析）。"""
    adapter = DouyinAdapter()
    got = DouyinStream._parse_room_id_candidates(url)
    handled = adapter.can_handle(url)
    check(got == [] and not handled,
          f"{note}：不识别（候选={got} can_handle={handled}）")


def expect_live_without_id(url: str, note: str) -> None:
    """直播语义成立、但取不到合法房间号：接住 + 返回空 + 报 URL 格式错误。"""
    adapter = DouyinAdapter()
    check(adapter.can_handle(url), f"{note}：直播语义成立，应被接住")
    check(DouyinStream._parse_room_id_candidates(url) == [],
          f"{note}：取不到合法房间号时返回空（不猜）")
    try:
        DouyinStream(url).get_room_id()
        check(False, f"{note}：应当报错")
    except StreamResolveError as e:
        check("URL格式错误" in str(e), f"{note}：报 URL 格式错误 -> {e}")


# ======================================================================
section("A. 当前成功 URL（回归基线）")
# ======================================================================
expect_id("https://live.douyin.com/123456789", "123456789",
          "标准直播间链接")
expect_id("https://live.douyin.com/96670111738", "96670111738",
          "标准直播间链接（长 id）")
expect_id("http://live.douyin.com/123456789", "123456789",
          "http（非 https）")
expect_id("https://LIVE.DOUYIN.COM/123456789", "123456789",
          "主机名大小写不敏感")
expect_id("https://live.douyin.com/123456789/", "123456789",
          "带尾部斜杠")
expect_id("https://live.douyin.com/96670111738?foo=bar", "96670111738",
          "标准链接带无关查询串")


# ======================================================================
section("B. 当前失败 URL（本次要修好的）")
# ======================================================================
expect_id("https://www.douyin.com/live/96670111738", "96670111738",
          "www 域 /live/<id>")
expect_id("https://www.douyin.com/root/live/123456789", "123456789",
          "www 域 /root/live/<id>")
expect_id(
    "https://www.douyin.com/follow/live/765977289242"
    "?anchor_id=3705804398273424&is_game_live=0&is_paidlive=0"
    "&r_id=f_765977289242&r_player=1&watch_paidlive_status=regular",
    "765977289242",
    "真实上报链接：一堆无关参数都不干扰",
)


# ======================================================================
section("C/D/E. 三条 live 路径形态走同一条规则")
# ======================================================================
expect_id("https://www.douyin.com/live/96670111738", "96670111738",
          "/live/<id>")
expect_id("https://www.douyin.com/follow/live/765977289242", "765977289242",
          "/follow/live/<id>")
expect_id("https://www.douyin.com/root/live/123456789", "123456789",
          "/root/live/<id>")
expect_id("https://www.douyin.com/a/b/c/live/424242", "424242",
          "任意前缀深度：只看 live 语义位")
expect_id("https://douyin.com/follow/live/242240793655", "242240793655",
          "不带 www 的裸域名")


# ======================================================================
section("F. query 参数干扰")
# ======================================================================
expect_id("https://www.douyin.com/follow/live/765977289242?anchor_id=1",
          "765977289242", "anchor_id 干扰")
expect_id("https://www.douyin.com/follow/live/765977289242?r_id=999",
          "765977289242", "r_id 干扰")
expect_id("https://www.douyin.com/follow/live/765977289242?room_id=888",
          "765977289242", "路径解优先于 query 解")
expect_id("https://www.douyin.com/live/555?anchor_id=666&r_id=777"
          "&r_player=1&is_paidlive=0", "555", "多个 query 参数同时干扰")


# ======================================================================
section("G. anchor_id 绝不能被当成房间号")
# ======================================================================
expect_id("https://www.douyin.com/follow/live/242240793655"
          "?anchor_id=111122930648", "242240793655",
          "anchor_id 更长也不采用（不取最长数字）")
# 只给 anchor_id、路径里没有直播语义 -> 不是直播间，不能靠它凑一个 id
expect_rejected("https://www.douyin.com/video/123456?anchor_id=999",
                "视频页带 anchor_id")


# ======================================================================
section("H. 非直播 Douyin URL（不能误解析）")
# ======================================================================
for url, note in (
    ("https://www.douyin.com/video/123456", "普通视频页"),
    ("https://www.douyin.com/user/MS4wLjABAAAA", "用户主页"),
    ("https://www.douyin.com/search/123456", "搜索页"),
    ("https://www.douyin.com/discover", "发现页"),
    ("https://www.douyin.com/", "首页"),
    ("https://www.douyin.com/lives/123", "lives（不是 live 段）"),
    ("https://www.douyin.com/alive/123", "alive（不是 live 段）"),
    ("https://www.douyin.com/livehouse/123", "livehouse（不是 live 段）"),
):
    expect_rejected(url, note)


# ======================================================================
section("I. 非 Douyin URL")
# ======================================================================
for url, note in (
    ("https://www.bilibili.com/live/123456", "B 站"),
    ("https://www.huya.com/123456", "虎牙"),
    ("https://example.com/follow/live/123456", "任意站点"),
    ("https://live.douyin.com.evil.com/live/123", "仿冒域名后缀"),
    ("https://notdouyin.com/live/123", "域名里含 douyin 但并非抖音"),
    ("https://douyin.com.evil.com/live/1", "douyin.com 作子域前缀"),
):
    expect_rejected(url, note)


# ======================================================================
section("J. malformed / 边界输入")
# ======================================================================
for url, note in (
    ("", "空字符串"),
    ("   ", "全空白"),
    ("not a url", "无 scheme 的普通文本"),
    ("https://", "只有 scheme"),
    ("//www.douyin.com/live/123", "无 scheme（协议相对）"),
    ("https://www.douyin.com:notaport/live/123", "端口非法"),
    ("ftp://www.douyin.com/live/123", "非 http(s) 协议"),
):
    got = DouyinStream._parse_room_id_candidates(url)
    check(got == [], f"{note}：不解析（候选={got}）")

check(DouyinStream._parse_room_id_candidates(None) == [],
      "None 输入不抛异常、不解析")
check(DouyinStream._parse_room_id_candidates(12345) == [],
      "非字符串输入不抛异常、不解析")


# ======================================================================
section("K. path 多个数字，只有 live 语义位上的那个才算")
# ======================================================================
expect_id("https://www.douyin.com/video/111111/live/222222", "222222",
          "video 段在前、live 段在后")
expect_id("https://www.douyin.com/user/333333/live/444444", "444444",
          "user 段在前、live 段在后")
expect_id("https://www.douyin.com/99999/live/12345678?anchor_id=7777777",
          "12345678", "更长的不在 live 位上也不采用")
expect_id("https://www.douyin.com/live/1/2/3", "1",
          "live 后紧跟的数字段即房间号")
expect_id("https://live.douyin.com/111/222", "111",
          "live 域名取第一段")


# ======================================================================
section("L. query fallback（只在路径没有直播语义时启用）")
# ======================================================================
expect_id("https://www.douyin.com/room/0?room_id=123456789", "123456789",
          "显式 room_id 参数")
expect_id("https://www.douyin.com/x?webcast_id=987654321", "987654321",
          "显式 webcast_id 参数")
expect_id("https://www.douyin.com/x?anchor_id=1&r_id=2&room_id=555", "555",
          "只认 room_id，忽略其它参数")
for url, note in (
    ("https://www.douyin.com/x?anchor_id=123456", "只有 anchor_id"),
    ("https://www.douyin.com/x?r_id=123456", "只有 r_id"),
    ("https://www.douyin.com/x?room_ids=123", "参数名不精确匹配"),
):
    expect_rejected(url, note)

# room_id 存在但取值非法：语义上仍是直播链接，接住后由 resolve 报格式错误
expect_live_without_id("https://www.douyin.com/x?room_id=abc",
                       "room_id 非数字")


# ======================================================================
section("N. 数字段的严格性：只认 ASCII 0-9")
# ======================================================================
# str.isdigit() 对全角「１２３」、上标「²」、阿拉伯-印度数字「٣」也返回 True，
# 但那些字符不可能是抖音房间号 —— 位置对了、字符不对，同样按"取不到"处理。
for url, note in (
    ("https://www.douyin.com/live/１２３", "全角数字段"),
    ("https://www.douyin.com/live/²", "上标数字段"),
    ("https://www.douyin.com/live/٣٤٥", "阿拉伯-印度数字段"),
    ("https://live.douyin.com/１２３", "live 域名 + 全角数字段"),
    ("https://www.douyin.com/x?room_id=１２３", "query 里的全角数字"),
):
    got = DouyinStream._parse_room_id_candidates(url)
    check(got == [], f"{note}：不猜、不解析（候选={got}）")

# 合法 ASCII 数字段照常解析（含前导零，不改成整数再还原）
expect_id("https://www.douyin.com/live/007", "007", "前导零原样保留")
expect_id("https://www.douyin.com/follow/live/0", "0", "单个 0 也合法")
expect_id("https://www.douyin.com/LIVE/123", "123", "语义段大小写不敏感")
expect_id("https://www.douyin.com/live/123/", "123", "live 后紧跟的段优先")
expect_id("https://www.douyin.com/follow/live/123/456", "123",
          "只取 live 之后紧跟的那一段")
# live 后面不是数字段：不算，且**不**去后面更远的数字里挑一个
expect_live_without_id("https://www.douyin.com/follow/live/abc/456",
                       "live 后是 abc，后面虽有 456 也不采用")
# 非 ascii 房间号虽被拒，但直播语义成立，仍由 resolve 报 URL 格式错误
expect_live_without_id("https://www.douyin.com/live/１２３",
                       "全角数字段：语义成立，房间号非法")


# ======================================================================
section("M. 适配器匹配 / 注册表路由")
# ======================================================================
adapter = DouyinAdapter()
registry = build_default_registry()

HANDLED = (
    "https://live.douyin.com/96670111738",
    "https://www.douyin.com/live/96670111738",
    "https://www.douyin.com/follow/live/765977289242?anchor_id=1",
    "https://www.douyin.com/root/live/123456789",
)
for url in HANDLED:
    check(adapter.can_handle(url), f"适配器接住：{url}")
    routed = registry.get_adapter(url)
    check(routed is not None and routed.name == "douyin",
          f"注册表路由到抖音：{url}")
    check(adapter.can_handle("  " + url + "  "), f"两端空白不影响匹配：{url}")

# 能接住 ⟺ 能解析出房间号（除"直播语义成立但房间号非法"这一类）
for url, _ in (("https://www.douyin.com/video/1", ""),
               ("https://www.douyin.com/", ""),
               ("https://www.bilibili.com/live/1", "")):
    check(not adapter.can_handle(url) and not registry.get_adapter(url),
          f"不接住也不路由：{url}")

# 「直播语义成立但房间号非法」：要接住，由 resolve 报 URL 格式错误
expect_live_without_id("https://www.douyin.com/follow/live/abc",
                       "follow/live/abc")
expect_live_without_id("https://www.douyin.com/live/", "live/ 后没有段")
expect_live_without_id("https://live.douyin.com/", "live 域名根路径")

print(f"\n{'=' * 74}")
print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
print("=" * 74)
sys.exit(1 if _FAILED else 0)
