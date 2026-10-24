# -*- coding: utf-8 -*-
"""
Phase 2.5 验证：CandidateFailover（纯逻辑，无 Qt / 无 FFmpeg / 无平台）

覆盖：
    1. A → B → C 顺序
    2. A 失败后 B
    3. 全部耗尽
    4. cancel 后不能继续
    5. note_failure 重复调用不会跳过两个 candidate
    6. 已 settled 后不能继续
    7. 空 candidates
    8. 单 candidate
    9. next_candidate 幂等 / note_success 终止迭代
   10. 边界：候选顺序不被重排、空 url 条目被跳过、无平台依赖

运行： python test_phase_2_5_candidate_failover.py
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream.failover import CandidateFailover
from stream.models import StreamCandidate, StreamInfo

_PASS = 0
_FAIL = 0


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  ✓ %s" % msg)
    else:
        _FAIL += 1
        print("  ✗ %s" % msg)


def section(title):
    print("\n%s" % title)


def make_info(urls, selected_index=0):
    """构造一个带 N 个候选的 StreamInfo（selected 默认是第一个）"""
    candidates = [
        StreamCandidate(url=u, protocol="flv", quality="HD") for u in urls
    ]
    selected = candidates[selected_index] if candidates else None
    return StreamInfo(
        platform="fake",
        room_id="room",
        candidates=candidates,
        selected=selected,
        width=1920,
        height=1080,
    )


A, B, C = "https://a.example/stream.flv", "https://b.example/stream.flv", \
          "https://c.example/stream.flv"


# ======================================================================
# [1] A → B → C 顺序
# ======================================================================
def test_order():
    section("[1] A → B → C 顺序")
    f = CandidateFailover(make_info([A, B, C]))

    first = f.next_candidate()
    check(first.url == A, "首次尝试 A")
    check(f.total == 3 and f.attempt_number == 1, "total=3, attempt=1")

    f.note_failure()
    second = f.next_candidate()
    check(second.url == B, "A 失败后是 B")

    f.note_failure()
    third = f.next_candidate()
    check(third.url == C, "B 失败后是 C")

    f.note_failure()
    check(f.next_candidate() is None, "C 失败后没有更多候选")
    check(f.settled is True, "迭代已终止")
    check(f.exhausted is True, "以'候选耗尽'的方式终止")


# ======================================================================
# [2] A 失败后 B
# ======================================================================
def test_a_then_b():
    section("[2] A 失败后 B（且顺序与平台给出的完全一致）")
    info = make_info([A, B])
    f = CandidateFailover(info)

    check(f.next_candidate().url == A, "先试 A")
    f.note_failure()
    got = f.next_candidate()
    check(got.url == B, "再试 B")
    check(got is info.candidates[1], "返回的就是平台给出的那个对象，未被复制/重建")

    f.note_failure()
    check(f.next_candidate() is None, "B 之后没有候选")
    check(f.failed_count == 2, "失败计数为 2")


# ======================================================================
# [3] 全部耗尽
# ======================================================================
def test_exhausted():
    section("[3] 全部耗尽")
    f = CandidateFailover(make_info([A, B, C]))
    seen = []
    while True:
        c = f.next_candidate()
        if c is None:
            break
        seen.append(c.url)
        f.note_failure()

    check(seen == [A, B, C], "依次尝试了全部候选，顺序不变")
    check(f.exhausted is True and f.settled is True, "标记为耗尽 + 已终止")
    check(f.next_candidate() is None, "耗尽后再问仍是 None")
    check(f.failed_count == 3, "三个候选都记为失败")


# ======================================================================
# [4] cancel 后不能继续
# ======================================================================
def test_cancel():
    section("[4] cancel 后不能继续")
    f = CandidateFailover(make_info([A, B, C]))
    check(f.next_candidate().url == A, "cancel 前正常取到 A")

    f.cancel()
    check(f.cancelled is True, "标记为已取消")
    check(f.next_candidate() is None, "cancel 后不能取到 B")
    check(f.next_candidate() is None, "重复询问仍是 None")
    check(f.note_failure() is False, "cancel 后 note_failure 不生效")

    # cancel 幂等
    f.cancel()
    check(f.cancelled is True and f.next_candidate() is None, "cancel 可重复调用")

    # 未开始就 cancel
    g = CandidateFailover(make_info([A, B]))
    g.cancel()
    check(g.next_candidate() is None, "一开始就 cancel，第一个候选也不启动")


# ======================================================================
# [5] note_failure 重复调用不会跳过两个 candidate
# ======================================================================
def test_double_note_failure():
    section("[5] note_failure 重复调用不会跳过两个 candidate")
    f = CandidateFailover(make_info([A, B, C]))

    check(f.next_candidate().url == A, "取到 A")
    check(f.note_failure() is True, "第一次 note_failure 结算了 A")
    check(f.note_failure() is False, "第二次 note_failure 是空操作（无在途尝试）")
    check(f.note_failure() is False, "第三次同样无效")

    nxt = f.next_candidate()
    check(nxt is not None and nxt.url == B,
          "下一次拿到的仍是 B（没有被跳过两个），实际 %s"
          % (nxt.url if nxt else None))
    check(f.failed_count == 1, "只结算了一个失败")

    # 同一候选上重复 next_candidate 也是幂等的
    check(f.next_candidate() is nxt, "未结算时重复 next_candidate 返回同一个候选")


# ======================================================================
# [6] 已 settled 后不能继续
# ======================================================================
def test_settled():
    section("[6] 已 settled 后不能继续")

    # 6a. 播放成功 → settled
    f = CandidateFailover(make_info([A, B, C]))
    f.next_candidate()
    check(f.note_success() is True, "note_success 结算成功")
    check(f.settled is True, "迭代终止")
    check(f.next_candidate() is None, "成功后不再换候选")
    check(f.note_failure() is False, "settled 后 note_failure 不生效")
    check(f.exhausted is False, "成功终止不算'耗尽'")

    # 6b. 耗尽 → settled
    g = CandidateFailover(make_info([A]))
    g.next_candidate()
    g.note_failure()
    check(g.next_candidate() is None, "耗尽")
    check(g.settled is True, "标记终止")
    check(g.note_success() is False, "settled 后 note_success 不生效")


# ======================================================================
# [7] 空 candidates
# ======================================================================
def test_empty():
    section("[7] 空 candidates")

    f = CandidateFailover(make_info([]))
    check(f.total == 0, "没有可尝试的候选")
    check(f.next_candidate() is None, "直接返回 None")
    check(f.settled is True and f.exhausted is True, "立即判定耗尽")
    check(f.note_failure() is False, "没有在途尝试可结算")

    # candidates 属性缺失（防御性）
    class _Bare:
        candidates = None
        selected = None

    g = CandidateFailover(_Bare())
    check(g.next_candidate() is None, "candidates 为 None 时同样安全返回 None")

    # 只有 selected、没有 candidates（防御性兜底）
    only = StreamCandidate(url=A, protocol="flv")
    info = StreamInfo(platform="fake", room_id="r", candidates=[], selected=only)
    h = CandidateFailover(info)
    check(h.next_candidate() is only, "只有 selected 时仍能尝试一次")


# ======================================================================
# [8] 单 candidate
# ======================================================================
def test_single():
    section("[8] 单 candidate")
    f = CandidateFailover(make_info([A]))

    check(f.total == 1, "只有一个候选")
    check(f.next_candidate().url == A, "首次尝试 A")
    f.note_failure()
    check(f.next_candidate() is None, "失败后无候选可换")
    check(f.settled is True and f.exhausted is True, "直接耗尽")

    # 单候选成功
    g = CandidateFailover(make_info([A]))
    g.next_candidate()
    g.note_success()
    check(g.next_candidate() is None, "成功后同样不换候选")
    check(g.exhausted is False, "成功不算耗尽")


# ======================================================================
# [9] 顺序不被重排 / 空 url 被跳过
# ======================================================================
def test_no_reorder():
    section("[9] 顺序不被重排，空 url 条目被跳过")
    cands = [
        StreamCandidate(url="", protocol="flv"),        # 空 url：不是一个尝试
        StreamCandidate(url=B, protocol="flv"),
        StreamCandidate(url=A, protocol="flv"),         # 故意与字母序相反
        StreamCandidate(url=C, protocol="flv"),
    ]
    info = StreamInfo(platform="fake", room_id="r", candidates=cands,
                      selected=cands[1])
    f = CandidateFailover(info)

    got = []
    while True:
        c = f.next_candidate()
        if c is None:
            break
        got.append(c.url)
        f.note_failure()

    check(got == [B, A, C], "严格保持平台给出的顺序，未重排、未按字母序整理")
    check(f.total == 3, "空 url 条目不计入可尝试候选")


# ======================================================================
# [10] 依赖边界：无 Qt / 无 FFmpeg / 无平台
# ======================================================================
def test_isolation():
    section("[10] 依赖边界")
    import ast

    import stream.failover as mod

    # 只检查真正被 import 的模块名（docstring 里提到"不认识 Qt"是说明，不是依赖）
    imported = set()
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    check(imported == set(),
          "模块没有任何 import（实际 %s），因此不依赖 Qt/FFmpeg/平台"
          % (sorted(imported) or "空"))

    source = inspect.getsource(mod)
    for forbidden in ("subprocess", "requests", "socket"):
        check(forbidden not in source, "源码不出现 %s" % forbidden)

    f = CandidateFailover(make_info([A, B]))
    check(not hasattr(f, "sort") and not hasattr(f, "score"),
          "没有排序 / 评分接口")
    check(not hasattr(f, "probe") and not hasattr(f, "measure"),
          "没有探测 / 测速接口")
    check(not hasattr(f, "retry") and not hasattr(f, "reset"),
          "没有重试接口（不重试已失败的候选）")


def main():
    print("=" * 68)
    print("Phase 2.5：CandidateFailover 单元验证")
    print("=" * 68)
    test_order()
    test_a_then_b()
    test_exhausted()
    test_cancel()
    test_double_note_failure()
    test_settled()
    test_empty()
    test_single()
    test_no_reorder()
    test_isolation()

    print("\n" + "=" * 68)
    print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    print("=" * 68)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
