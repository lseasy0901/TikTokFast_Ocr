# -*- coding: utf-8 -*-
"""平台流解析解耦回归测试（RELEASE-02-B5-CLEANUP 后）

覆盖目标：
    1. 房间 URL -> room_id
    2. **唯一路径结构**：抖音 resolve() 只产出房间标识，不再枚举任何流候选
    3. 适配器路由 / 注册表
    4. 非法 URL / 未支持平台异常
    5. MainWindow -> StreamResolver -> PlatformAdapter 链路
    6. 共享候选机制未被误删（虎牙 / B 站仍走候选迭代）
    7. 旧 `_uhd` 提取入口已不存在（结构性断言）

背景：旧 `_uhd` 转码档拉流链路（`flv_pull_url` / `hls_pull_url` /
`flv_pull_url_map` / `hls_pull_url_map` / `pull_url` / `default_pull_url`
六条字段通道、pace_f / RENDER_DATA / 正则 / API 四种抽取方式、SDK 线候选、
清晰度排序、候选→选中）已随 RELEASE-02-B5-CLEANUP 整体移除。
唯一拉流架构为：裸流页面直取（stream/bare_source.py）→ FFmpeg 7 → 解码/显示。
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.disable(logging.CRITICAL)

from stream.adapters.douyin import (  # noqa: E402
    DouyinAdapter, DouyinStream, DouyinStreamError,
)
from stream.failover import CandidateFailover  # noqa: E402
from stream.models import (  # noqa: E402
    StreamCandidate, StreamInfo, StreamResolveError,
)
from stream.registry import PlatformRegistry, build_default_registry  # noqa: E402
from stream.resolver import StreamResolver  # noqa: E402
from stream.selector import select  # noqa: E402

ROOM_URL = "https://live.douyin.com/123456789"
ROOM_ID = "123456789"

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ----------------------------------------------------------------------
# 1. room_id
# ----------------------------------------------------------------------
def t_room_id():
    print("\n[1] 房间 URL -> room_id")
    check("合法 URL 解析出 room_id",
          DouyinStream(ROOM_URL).get_room_id() == ROOM_ID)
    check("带查询串的 URL 仍可解析",
          DouyinStream(ROOM_URL + "?foo=1").get_room_id() == ROOM_ID)
    check("关注页 URL 解析出 room_id",
          DouyinStream("https://www.douyin.com/follow/live/987654321")
          .get_room_id() == "987654321")
    check("非抖音 URL 抛 DouyinStreamError",
          _raises(DouyinStreamError,
                  lambda: DouyinStream("https://www.huya.com/x").get_room_id()))
    check("空 URL 抛 DouyinStreamError",
          _raises(DouyinStreamError, lambda: DouyinStream("").get_room_id()))


# ----------------------------------------------------------------------
# 2. 唯一路径结构：resolve() 不再枚举流候选
# ----------------------------------------------------------------------
def t_single_path_contract():
    print("\n[2] 唯一路径结构：resolve() 只给房间标识，不给流候选")
    info = DouyinStream(ROOM_URL).get_stream_info()
    check("返回 StreamInfo", isinstance(info, StreamInfo))
    check("platform=douyin", info.platform == "douyin")
    check("room_id 正确", info.room_id == ROOM_ID)
    check("candidates 恒为空（旧 _uhd 候选已移除）", info.candidates == [],
          str(info.candidates))
    check("selected 恒为 None", info.selected is None)
    check("info.url 恒为 None（取流不在此层）", info.url is None)
    check("width/height 恒 None（探测下沉到 reader）",
          (info.width, info.height) == (None, None))

    # 适配器层同样不产出候选
    adapter = DouyinAdapter()
    ainfo = adapter.resolve(ROOM_URL)
    check("DouyinAdapter.resolve() 同样无候选", ainfo.candidates == [])


# ----------------------------------------------------------------------
# 3. 适配器路由 / 注册表
# ----------------------------------------------------------------------
def t_routing():
    print("\n[3] 适配器路由 / 注册表")
    reg = build_default_registry()
    check("默认注册表含 DouyinAdapter（首位）",
          isinstance(reg.adapters[0], DouyinAdapter))
    check("注册表含 3 个适配器（抖音 + 虎牙 + B 站）", len(reg.adapters) == 3,
          str([type(a).__name__ for a in reg.adapters]))
    check("注册表含 HuyaAdapter",
          any(type(a).__name__ == "HuyaAdapter" for a in reg.adapters))
    check("注册表含 BilibiliAdapter",
          any(type(a).__name__ == "BilibiliAdapter" for a in reg.adapters))
    check("registry 按 URL 选中 adapter",
          isinstance(StreamResolver().registry.get_adapter(ROOM_URL),
                     DouyinAdapter))
    check("重复注册被忽略",
          (lambda r: (r.register(DouyinAdapter()), len(r.adapters) == 1)[1])(
              PlatformRegistry()))

    check("can_handle: 直播域名根路径", DouyinAdapter().can_handle(ROOM_URL))
    check("can_handle: 普通视频页不接住",
          not DouyinAdapter().can_handle("https://www.douyin.com/video/123"))
    check("can_handle: 非抖音域名不接住",
          not DouyinAdapter().can_handle("https://www.huya.com/123"))


# ----------------------------------------------------------------------
# 4. 非法 URL / 未支持平台
# ----------------------------------------------------------------------
def t_errors():
    print("\n[4] 非法 URL / 未支持平台异常")
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
# 5. MainWindow -> Resolver -> Adapter
# ----------------------------------------------------------------------
def t_end_to_end():
    print("\n[5] MainWindow -> StreamResolver -> PlatformAdapter 链路")
    from gui.main_window import _ConnectWorker

    got = {}

    def on_success(info):
        got["info"] = info

    def on_error(msg):
        got["error"] = msg

    w = _ConnectWorker(live_url=ROOM_URL, on_success=on_success,
                       on_error=on_error, resolver=StreamResolver())
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
        check("候选为空（取流交给裸流页面直取）", info.candidates == [],
              str(info.candidates))
        check("width/height 恒 None", (info.width, info.height) == (None, None))


# ----------------------------------------------------------------------
# 6. 共享候选机制未被误删
# ----------------------------------------------------------------------
def t_shared_candidate_machinery():
    print("\n[6] 共享候选机制（虎牙 / B 站仍在使用）")
    cands = [StreamCandidate(url="https://a/1.flv", protocol="flv"),
             StreamCandidate(url="https://b/2.flv", protocol="flv")]
    check("selector 仍取首个非空候选", select(cands) is cands[0])
    check("selector 空列表返回 None", select([]) is None)

    info = StreamInfo(platform="huya", room_id="1", candidates=cands,
                      selected=cands[0])
    fo = CandidateFailover(info)
    check("CandidateFailover 仍按顺序迭代", fo.next_candidate() is cands[0])
    fo.note_failure()
    check("note_failure 后前进到下一个", fo.next_candidate() is cands[1])
    check("候选总数正确", fo.total == 2)

    # 单候选（抖音裸流会话的形态）也能正常迭代
    single = StreamInfo(platform="douyin", room_id="1", candidates=[cands[0]],
                        selected=cands[0])
    fo1 = CandidateFailover(single)
    check("单候选会话：首个即唯一", fo1.next_candidate() is cands[0])
    fo1.note_failure()
    check("单候选会话：耗尽后返回 None", fo1.next_candidate() is None)
    check("单候选会话：标记为耗尽", fo1.exhausted)


# ----------------------------------------------------------------------
# 7. 旧 `_uhd` 提取入口已不存在（结构性断言）
# ----------------------------------------------------------------------
def t_old_path_removed():
    print("\n[7] 旧 _uhd 提取入口已整体移除")
    removed = (
        "get_stream_url", "_resolve_streams", "_extract_from_pace_f",
        "_extract_from_render_data", "_extract_urls_regex",
        "_get_stream_from_api", "iter_stream_candidates", "_iter_field_urls",
        "_extract_stream_from_data", "_deep_search_stream_url",
        "_extract_sdk_candidates", "_FIELD_PRIORITY", "_QUALITY_PRIORITY",
        "_SDK_VARIANT_PRIORITY", "_last_candidate_source", "_last_page_html",
    )
    still_there = [n for n in removed if hasattr(DouyinStream, n)]
    check("DouyinStream 不再有旧路径入口", not still_there, str(still_there))

    try:
        import stream.bare_candidate  # noqa: F401
        gone = False
    except ImportError:
        gone = True
    check("stream/bare_candidate.py 已删除", gone)
    check("裸流来源模块存在（唯一取流入口）",
          __import__("stream.bare_source", fromlist=["x"]) is not None)


def main():
    print("=" * 60)
    print("平台流解析解耦 回归测试（RELEASE-02-B5-CLEANUP 后）")
    print("=" * 60)
    for fn in (t_room_id, t_single_path_contract, t_routing, t_errors,
               t_end_to_end, t_shared_candidate_machinery, t_old_path_removed):
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
