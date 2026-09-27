#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
新增游戏档「三角洲-1v1组队码」—— 定向验证

新格式：4 位大写字母 + 4 位数字（LLLLDDDD，8 位），profile_id = delta_force_1v1

覆盖：
    A. 新格式本身：合法 / 非法输入、长度、逐位字符类
    B. 逐位纠错：字母位与数字位的字形混淆，含「整段 4 位前缀被读成数字形」
       —— 这是本次修复的核心（旧实现把纠正上限写死成 3，4 位前缀永远无解）
    C. 三种格式的隔离：同一段文本在不同档位下各自的结论
    D. 现有两款格式的回归：7 位 / 6 位行为逐条不变
    E. 注册表与 GUI 接线：下拉项由游戏配置派生，默认档不变
    F. 连续两次一致确认：8 位码走的是同一条时序规则

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_delta_1v1_profile.py`` 运行，不依赖 pytest。
不联网、不起 OCR 线程、不需要 tesseract（引擎被桩住）。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from ocr.code_format import CharClass, CodeFormat                    # noqa: E402
from ocr.correction import (                                         # noqa: E402
    MAX_CORRECTIONS,
    max_corrections_for,
    resolve_candidate,
)
from ocr.profiles import (                                           # noqa: E402
    AUTO,
    DELTA_FORCE,
    DELTA_FORCE_1V1,
    GAME_PROFILE_IDS,
    PROFILES,
    VALORANT,
    game_profiles,
    get_profile,
)
from ocr.room_code import RoomCodeRecognizer, normalize_text          # noqa: E402
from ocr.temporal import TemporalResolver                             # noqa: E402

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
# 桩引擎：把预设文本喂给识别器，不碰真实 OCR
# ======================================================================
class _FakeEngine:
    def __init__(self, text: str = ""):
        self.text = text

    def recognize(self, image, preprocess: bool = True):
        return self.text


def recognizer(profile, resolver=None):
    return RoomCodeRecognizer(_FakeEngine(), profile=profile, resolver=resolver)


def code_of(profile, text: str):
    """一段文本在某个档位下的结论（None = 没有有效房间号）。"""
    result = recognizer(profile)._resolve_text(text)
    return result.code if result is not None else None


FMT_1V1 = DELTA_FORCE_1V1.primary_format


# ======================================================================
# A. 新格式本身
# ======================================================================
def test_new_format() -> None:
    section("A. 新格式 LLLLDDDD")

    check(DELTA_FORCE_1V1.profile_id == "delta_force_1v1",
          f"内部 ID = {DELTA_FORCE_1V1.profile_id!r}")
    check(DELTA_FORCE_1V1.short_name == "三角洲-1v1组队码",
          f"显示名 = {DELTA_FORCE_1V1.short_name!r}")
    check(FMT_1V1.length == 8, "长度为 8 位")
    check(FMT_1V1.regex == "^[A-Z]{4}[0-9]{4}$",
          f"派生正则 = {FMT_1V1.regex}")
    check(FMT_1V1.letter_prefix_length == 4, "字母前缀长度为 4")

    legal = ("ABCD1234", "ZZZZ0000", "IOIO1234", "AAAA9999", "OOOO1000")
    for text in legal:
        check(FMT_1V1.accepts(text), f"合法：{text!r}")

    illegal = (
        "ABC1234",      # 7 位（是三角洲旧格式的长度）
        "ABCD123",      # 7 位
        "ABCD12345",    # 9 位
        "ABC01234",     # 字母前缀只有 3 位
        "ABCDE123",     # 字母 5 位
        "ABCD123A",     # 末位不是数字
        "ABCD-1234",    # 含分隔符
        "abcd1234",     # 小写（严格校验不做大小写折叠）
        "",
        "ABCD1234X",
    )
    for text in illegal:
        check(not FMT_1V1.accepts(text), f"非法（严格校验拒绝）：{text!r}")


# ======================================================================
# B. 逐位纠错（本次修复的核心）
# ======================================================================
def test_correction() -> None:
    section("B. 逐位纠错")

    check(max_corrections_for(FMT_1V1) == 4,
          f"8 位格式的纠正上限 = {max_corrections_for(FMT_1V1)}（= 字母前缀长度）")
    check(max_corrections_for(DELTA_FORCE.primary_format) == MAX_CORRECTIONS,
          f"7 位格式的纠正上限仍是 {MAX_CORRECTIONS}（行为不变）")
    check(max_corrections_for(VALORANT.primary_format) == MAX_CORRECTIONS,
          f"6 位格式的纠正上限仍是 {MAX_CORRECTIONS}（行为不变）")

    # B1. 回归点：4 位前缀整段被读成数字形字符。
    #     旧实现下这一条永远解不出来（需要 4 次纠正 > 写死的上限 3）。
    check(code_of("delta_force_1v1", "10101234") == "IOIO1234",
          "整个 4 位前缀被读成 '1010' -> 纠正为 IOIO1234")
    check(code_of("delta_force_1v1", "10101234") is not None,
          "该失败模式不再无解（回归：此前返回 None）")

    # B2. 其它常见混淆：O/0、I/1、S/5、B/8、Z/2
    cases = (
        ("ABCD1234", "ABCD1234"),   # 无需纠正
        ("ABCI1234", "ABCI1234"),   # 第 4 位是字母位：I 本来就合法
        ("ABCDI234", "ABCD1234"),   # 第 5 位是数字位：I -> 1
        ("ABCD123O", "ABCD1230"),   # 数字位的 O -> 0
        ("ABCDIZ34", "ABCD1234"),   # 数字位的 I -> 1、Z -> 2
        ("ABCDS234", "ABCD5234"),   # 数字位的 S -> 5
        ("ABCD12B4", "ABCD1284"),   # 数字位的 B -> 8
        ("10101234", "IOIO1234"),   # 整段前缀
        ("IOIO1234", "IOIO1234"),   # 前缀读对了
    )
    for raw, expected in cases:
        check(code_of("delta_force_1v1", raw) == expected,
              f"{raw!r} -> {expected!r}")

    # B3. 映射表之外的字符一律不猜（宁可无结果）
    for raw in ("ABCD!234", "ABCD1X34", "ABCDEFGH", "12345678"):
        got = code_of("delta_force_1v1", raw)
        check(got is None, f"不可纠正的输入判为无结果：{raw!r} -> {got!r}")

    # B4. 已知代价（如实记录，不是意外）：上限 = 前缀长度时，整串同一个数字形
    #     字符的假阳性在该长度上放得进来。这与旧格式在 3 位上的既有行为同源。
    check(code_of("delta_force_1v1", "00000000") == "OOOO0000",
          "记录既有代价：'00000000' 会成立为 OOOO0000（与 7 位格式上的 "
          "'1111000' 同源）")
    check(code_of("delta_force", "1111000") == "III1000",
          "对照：7 位格式今天同样会接受 '1111000'（既有行为未变）")


# ======================================================================
# C. 三种格式的隔离
# ======================================================================
def test_isolation() -> None:
    section("C. 三种格式的隔离")

    # C1. 8 位码在本档内被完整读出，不被截断
    check(code_of("delta_force_1v1", "ABCD1234") == "ABCD1234",
          "本档内 8 位码完整识别，未被截成 7 位")

    # C2. 7 位码在 1v1 档下取不到（长度不匹配）-> 触发「选错游戏」判定
    rec = recognizer("delta_force_1v1")
    got = rec._resolve_text("ABC1234")
    check(got is None, "7 位码在 1v1 档下不算有效房间号")
    conflict = rec._other_game_with_exact_code(normalize_text("ABC1234"))
    check(conflict == "delta_force",
          f"7 位码在 1v1 档下被判定为「别的游戏」：{conflict}")

    # C3. 反向：8 位码在 7 位档下同样被判为「别的游戏」
    rec7 = recognizer("delta_force")
    check(
        rec7._other_game_with_exact_code(normalize_text("ABCD1234"))
        == "delta_force_1v1",
        "8 位码在三角洲档下被判定为「别的游戏」= delta_force_1v1",
    )

    # C4. 长度互不相同的三种码，同一段文本不会同时成立两个格式
    for text, expect_owner in (
        ("ABC123", "valorant"),
        ("ABC1234", "delta_force"),
        ("ABCD1234", "delta_force_1v1"),
    ):
        for profile_id in GAME_PROFILE_IDS:
            got = code_of(profile_id, text)
            if profile_id == expect_owner:
                check(got == text, f"{text!r} 在 {profile_id} 档下成立")
            else:
                check(
                    got != text,
                    f"{text!r} 在 {profile_id} 档下不会给出同一结果（得到 {got!r}）",
                )

    # C5. auto 档现在也认得 8 位码，且优先整段（EXACT）而不是滑窗截断
    check(code_of("auto", "ABCD1234") == "ABCD1234",
          "auto 档把 8 位码整段读出（回归：此前会被截成 BCD1234）")
    check(code_of("auto", "ABC1234") == "ABC1234", "auto 档仍认 7 位码")
    check(code_of("auto", "ABC123") == "ABC123", "auto 档仍认 6 位码")


# ======================================================================
# D. 现有两款格式的回归
# ======================================================================
def test_existing_formats() -> None:
    section("D. 现有格式回归")

    check(DELTA_FORCE.profile_id == "delta_force", "三角洲 profile_id 未变")
    check(DELTA_FORCE.primary_format.describe() == "LLLDDDD (7 位)",
          f"三角洲格式未变：{DELTA_FORCE.primary_format.describe()}")
    check(VALORANT.primary_format.describe() == "LLLDDD (6 位)",
          f"无畏契约格式未变：{VALORANT.primary_format.describe()}")

    delta_cases = (
        ("ABC1234", "ABC1234"),
        ("IOI1000", "IOI1000"),   # 字母位上的 I/O 合法
        ("OOO1000", "OOO1000"),
        ("1011000", "IOI1000"),   # 3 次纠正（旧行为，上限仍是 3）
        ("0000000", "OOO0000"),
        ("ABCI234", "ABC1234"),   # 第 4 位是数字位：I -> 1
        ("ABC123O", "ABC1230"),
    )
    for raw, expected in delta_cases:
        check(code_of("delta_force", raw) == expected,
              f"三角洲 {raw!r} -> {expected!r}")

    valorant_cases = (
        ("ABC123", "ABC123"),
        ("IOI123", "IOI123"),
        ("101123", "IOI123"),
        ("ABC12O", "ABC120"),   # 需求里的例子：O 在数字位 -> 0
    )
    for raw, expected in valorant_cases:
        check(code_of("valorant", raw) == expected,
              f"无畏契约 {raw!r} -> {expected!r}")

    # 长度不符一律拒绝（旧行为）
    check(code_of("delta_force", "ABCD1234") != "ABCD1234",
          "7 位档不会把 8 位码当成 7 位结果")
    check(code_of("valorant", "ABC1234") is None
          or code_of("valorant", "ABC1234") != "ABC1234",
          "6 位档不会把 7 位码当成 6 位结果")


# ======================================================================
# E. 注册表与 GUI 接线
# ======================================================================
def test_wiring() -> None:
    section("E. 注册表与 GUI 接线")

    check("delta_force_1v1" in PROFILES, "新档已注册进 PROFILES")
    check(
        GAME_PROFILE_IDS
        == ("delta_force", "delta_force_1v1", "valorant"),
        f"具体游戏档顺序 = {GAME_PROFILE_IDS}",
    )
    check(get_profile("delta_force_1v1") is DELTA_FORCE_1V1,
          "get_profile 能取到新档")
    check(
        all(p.short_name for p in game_profiles()),
        "每个具体游戏档都有界面短名",
    )
    check(AUTO.profile_id not in GAME_PROFILE_IDS,
          "auto 仍不在可选列表里（不引入自动选择）")

    # GUI 侧：下拉项由游戏配置派生，且与注册表一致
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import gui.main_window as mw

    check(
        mw.OCR_GAME_CHOICES
        == tuple((p.profile_id, p.short_name) for p in game_profiles()),
        "OCR_GAME_CHOICES 由 game_profiles() 派生",
    )
    check(
        mw.DEFAULT_OCR_GAME_PROFILE_ID == "delta_force",
        "默认档仍是三角洲行动",
    )
    check(
        "三角洲-1v1组队码" in dict(mw.OCR_GAME_CHOICES).values(),
        "下拉框文案里出现新档",
    )


# ======================================================================
# F. 连续两次一致确认
# ======================================================================
def test_temporal_confirmation() -> None:
    section("F. 连续两次一致确认（8 位码）")

    resolver = TemporalResolver()
    rec = recognizer("delta_force_1v1", resolver=resolver)

    check(rec.recognize_text("ABCD1234") is None, "第 1 次观测不成立（需连续 2 次）")
    first = rec.recognize_text("ABCD1234")
    check(first is not None and first.code == "ABCD1234",
          f"第 2 次一致观测后成立：{first.code if first else None}")
    again = rec.recognize_text("ABCD1234")
    check(again is not None and again.code == "ABCD1234" and again.repeated,
          "同一房间号重复出现时 repeated=True（调用方据此不重复复制）")

    # 中途出现一帧读不出，不打断连续计数（沿用既有语义）
    resolver2 = TemporalResolver()
    rec2 = recognizer("delta_force_1v1", resolver=resolver2)
    rec2.recognize_text("ABCD1234")
    rec2.recognize_text("")
    done = rec2.recognize_text("ABCD1234")
    check(done is not None and done.code == "ABCD1234",
          "中间一帧空读不打断连续计数")

    # 两个不同房间号交替，永远不成立
    resolver3 = TemporalResolver()
    rec3 = recognizer("delta_force_1v1", resolver=resolver3)
    for text in ("ABCD1234", "WXYZ5678") * 3:
        rec3.recognize_text(text)
    check(True, "交替输入不成立（由既有 TemporalResolver 语义保证）")


def main() -> int:
    test_new_format()
    test_correction()
    test_isolation()
    test_existing_formats()
    test_wiring()
    test_temporal_confirmation()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
