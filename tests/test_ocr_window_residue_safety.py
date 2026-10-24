#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
滑窗残差安全门（阶段一）—— 验证脚本

背景（第二轮 100 码回归，完整诊断 CSV 在案）：
    'UJMI8506' 被 OCR 原始读成 'UJIMI8506'（9 字符）。旧实现对这个过长的
    连续段滑窗，得到两个不同的有效窗口：
        off=0  'UJIMI850' -> UJMI1850   （1 次纠正）
        off=1  'JIMI8506' -> JIMI8506   （0 次纠正）
    排序键 (质量, 纠正次数) 于是选中了零纠正的【错位】窗口；它因为「零纠正」
    被当成最可信的读法提前停止，连续两帧后错误复制。真码根本不在原文里，
    任何仲裁都救不回来——这一帧唯一正确的出路是不给结果。

本脚本验证的规则：
    滑窗候选只在「残差」>= _MIN_WINDOW_RESIDUE(2) 时才被接受。残差 =
    段长 - 格式长度 = 窗口之外剩下的字符数（与窗口偏移无关）。残差为 1 时，
    多出来的那一个字符无法判定是相邻文本还是房间号内部的错位，一律拒绝。

边界要说清楚（本脚本最后一条如实记录）：
    这是【边界收紧】，不是彻底修复。残差 >= 2 时滑窗照旧生效，因此
    'UJIMI8506x' 仍会取到错位的 'JIMI8506'。真正的修复在 OCR 读出那一层，
    本规则只覆盖「多出恰好 1 个字符」这一种表现形态。

覆盖：
    A. 回归日志里的三个异常串不再给出任何候选
    B. 精确码与逐位纠错不回归
    C. 相邻文本粘连（残差 >= 2）仍可滑窗：前缀侧与尾随侧
    D. 残差 = 1 一律拒绝（本规则的核心）
    E. 已有质量排序（EXACT 优于 WINDOW）不变
    F. 时序二次确认行为不变
    G. 如实记录残差 >= 2 时的剩余风险

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_window_residue_safety.py`` 运行，不依赖 pytest。
不联网、不起 OCR 线程、不需要 tesseract（引擎被桩住）。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from ocr.profiles import (                                      # noqa: E402
    AUTO,
    DELTA_FORCE,
    DELTA_FORCE_1V1,
    VALORANT,
)
from ocr.room_code import (                                     # noqa: E402
    _MIN_WINDOW_RESIDUE,
    RoomCodeRecognizer,
)
from ocr.temporal import TemporalResolver                       # noqa: E402

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
# 桩引擎：只喂文本，不碰真实 OCR
# ======================================================================
class _FakeEngine:
    def recognize(self, image, preprocess: bool = True):
        return ""


def recognizer(profile, resolver=None):
    return RoomCodeRecognizer(_FakeEngine(), profile=profile, resolver=resolver)


def code_of(profile, text: str):
    """一段文本在某个档位下的结论（None = 没有有效房间号）。"""
    result = recognizer(profile)._resolve_text(text)
    return result.code if result is not None else None


FMT_1V1 = DELTA_FORCE_1V1.primary_format
FMT_7 = DELTA_FORCE.primary_format
FMT_6 = VALORANT.primary_format


# ======================================================================
# A. 回归日志里的三个异常串
# ======================================================================
def test_logged_failures() -> None:
    section("A. 第二轮回归日志中的三个异常串（1v1 档）")

    check(_MIN_WINDOW_RESIDUE == 2,
          f"残差下限 = {_MIN_WINDOW_RESIDUE}")

    # (原始读法, 旧实现给出的错候选, 真值)
    cases = (
        ("UJIMI8506", "JIMI8506", "UJMI8506"),   # 标准 #38：实际错误复制
        ("QORTP8641", "ORTP8641", "QRTP8641"),   # 标准 #21：单帧未成立
        ("TYGH57380", "TYGH5738", "TYGH5730"),   # 标准 #55：单帧未成立
    )
    for raw, wrong, truth in cases:
        residue = len(raw) - FMT_1V1.length
        got = code_of("delta_force_1v1", raw)
        check(residue == 1,
              f"{raw!r} 的残差 = {residue}（本规则针对的就是残差 1）")
        check(got is None,
              f"{raw!r} -> 无候选（旧实现返回错候选 {wrong!r}；真值 {truth!r}）")
        check(got != wrong, f"{raw!r} 不再返回日志中的错候选 {wrong!r}")


# ======================================================================
# B. 精确码与逐位纠错不回归
# ======================================================================
def test_exact_and_correction() -> None:
    section("B. 精确码与逐位纠错不回归（1v1 档）")

    cases = (
        ("ABCD1234", "ABCD1234"),   # 无纠正
        ("ABCDI234", "ABCD1234"),   # 数字位的 I -> 1
        ("ABCD123O", "ABCD1230"),   # 数字位的 O -> 0
        ("ABCDS234", "ABCD5234"),   # 数字位的 S -> 5
        ("10101234", "IOIO1234"),   # 整个 4 位前缀被读成数字形
        ("00000000", "OOOO0000"),   # 既有代价（长度恰好匹配，不经滑窗）
    )
    for raw, expected in cases:
        check(code_of("delta_force_1v1", raw) == expected,
              f"精确码/纠错 {raw!r} -> {expected!r}")

    # 不可纠正的字符仍然不猜
    for raw in ("ABCD!234", "ABCD1X34", "ABCDEFGH", "12345678"):
        got = code_of("delta_force_1v1", raw)
        check(got is None, f"映射表之外仍判为无结果：{raw!r} -> {got!r}")


# ======================================================================
# C. 相邻文本粘连（残差 >= 2）仍可滑窗
# ======================================================================
def test_glued_text_still_slides() -> None:
    section("C. 相邻文本粘连（残差 >= 2）仍可滑窗")

    # 尾随侧粘连：ROI 多框进后面的文字
    check(code_of("delta_force_1v1", "ABCD1234xy") == "ABCD1234",
          "1v1 'ABCD1234xy' -> ABCD1234（尾随残差 2）")
    check(code_of("delta_force", "ABC1234other") == "ABC1234",
          "7 位 'ABC1234other' -> ABC1234（既有测试 8_0 的同一条）")
    check(code_of(AUTO.profile_id, "ABC1234other") == "ABC1234",
          "auto 'ABC1234other' -> ABC1234（既有测试 8_0 的同一条）")

    # 前缀侧粘连：码前还有一段文字（合法非零偏移，必须保留）
    check(code_of("delta_force_1v1", "IDABCD1234") == "ABCD1234",
          "1v1 'IDABCD1234' -> ABCD1234（前缀残差 2，偏移 2）")
    check(code_of("delta_force", "IDABC1234") == "ABC1234",
          "7 位 'IDABC1234' -> ABC1234（前缀残差 2，偏移 2）")

    # 非零偏移取到的候选仍然是零纠正零折叠的干净结果
    resolved = recognizer("delta_force_1v1")._resolve_text("IDABCD1234")
    check(resolved is not None and resolved.correction_count == 0
          and resolved.case_fold_count == 0,
          "非零偏移的候选本身零纠正零折叠，来源质量才是它唯一的代价")


# ======================================================================
# D. 残差 = 1 一律拒绝（本规则的核心）
# ======================================================================
def test_residue_one_rejected() -> None:
    section("D. 残差 = 1 一律拒绝（不滑窗）")

    cases = (
        ("delta_force_1v1", "ABCD12345", FMT_1V1),   # 码后多 1 个数字
        ("delta_force_1v1", "XABCD1234", FMT_1V1),   # 码前多 1 个字母
        ("delta_force", "ABC1234x", FMT_7),          # 7 位档同样收紧
        ("valorant", "ABC123x", FMT_6),              # 6 位档同样收紧
    )
    for profile_id, raw, fmt in cases:
        residue = len(raw) - fmt.length
        got = code_of(profile_id, raw)
        check(residue == 1, f"{profile_id} {raw!r} 残差 = {residue}")
        check(got is None,
              f"{profile_id} {raw!r} -> 无候选（不能把多出来的 1 个字符"
              f"当成相邻文本或码内错位中的任何一方）")

    # 规则只作用在「过长的段」上：长度恰好匹配的段照旧走 EXACT 路径
    check(_MIN_WINDOW_RESIDUE == 2,
          "残差下限仍是 2，且 EXACT 路径不经过这道门（见 B 段 '00000000'）")


# ======================================================================
# E. 已有质量排序不变
# ======================================================================
def test_quality_ranking() -> None:
    section("E. 已有质量排序（EXACT 优于 WINDOW）不变")

    # 同一帧里两个段：'XYZ789' 长度精确（EXACT），'ABCD1234' 需要滑窗
    # （WINDOW）——质量在前的排序键必须让精确段胜出。
    check(code_of("valorant", "XYZ789 ABCD1234") == "XYZ789",
          "valorant 'XYZ789 ABCD1234' -> XYZ789（EXACT 胜过 WINDOW）")

    # auto 档：8 位码走 8 位格式的 EXACT，不被 7 位格式截断
    check(code_of(AUTO.profile_id, "ABCD1234") == "ABCD1234",
          "auto 'ABCD1234' -> ABCD1234（EXACT 优先，不被截成 BCD1234）")
    check(code_of(AUTO.profile_id, "ABC1234") == "ABC1234", "auto 仍认 7 位码")
    check(code_of(AUTO.profile_id, "ABC123") == "ABC123", "auto 仍认 6 位码")

    # 拼回（JOINED）路径不经过滑窗门：长度必须精确相等
    check(code_of("delta_force", "ABC 1234") == "ABC1234",
          "'ABC 1234' 仍能拼回 ABC1234（JOINED 路径未受影响）")


# ======================================================================
# F. 时序二次确认行为不变
# ======================================================================
def test_temporal_confirmation() -> None:
    section("F. 时序二次确认行为不变")

    # 被拒绝的读法连候选都没有，自然永远凑不齐确认 —— 也就永远不会复制
    resolver = TemporalResolver()
    rec = recognizer("delta_force_1v1", resolver=resolver)
    for _ in range(4):
        rec.recognize_text("UJIMI8506")
    check(resolver.last_emitted_code is None,
          "旧实现里错误复制的 'UJIMI8506' 连续喂 4 帧也不会成立")
    check(resolver.observations and all(
        item is None for item in resolver.observations),
        f"观测窗口里全是「没读出来」：{resolver.observations}")

    # 正常码照旧：第 2 次一致观测成立，再出现是 repeated
    resolver2 = TemporalResolver()
    rec2 = recognizer("delta_force_1v1", resolver=resolver2)
    check(rec2.recognize_text("ABCD1234") is None, "第 1 次观测不成立（需连续 2 次）")
    first = rec2.recognize_text("ABCD1234")
    check(first is not None and first.code == "ABCD1234",
          f"第 2 次一致观测后成立：{first.code if first else None}")
    again = rec2.recognize_text("ABCD1234")
    check(again is not None and again.repeated,
          "同一房间号重复出现时 repeated=True（调用方据此不重复复制）")


# ======================================================================
# G. 剩余风险：如实记录，不是断言修复完成
# ======================================================================
def test_remaining_risk_documented() -> None:
    section("G. 如实记录：残差 >= 2 时错位窗口仍会胜出")

    # 同一条错位读法，只是在末尾多一个字符让残差变成 2，滑窗就重新生效。
    # 这说明本规则收紧的是「多出恰好 1 个字符」这一种形态，
    # 不是彻底解决错位——彻底解决要靠 OCR 读出那一层（多 PSM 观测）。
    for raw, still_wrong in (
        ("UJIMI8506x", "JIMI8506"),
        ("QORTP8641x", "ORTP8641"),
        ("TYGH57380x", "TYGH5738"),
    ):
        residue = len(raw) - FMT_1V1.length
        got = code_of("delta_force_1v1", raw)
        check(residue == 2 and got == still_wrong,
              f"已知残留：{raw!r}（残差 {residue}）仍取到错位窗口 {got!r}"
              f"—— 本规则不覆盖这一形态")


def main() -> int:
    test_logged_failures()
    test_exact_and_correction()
    test_glued_text_still_slides()
    test_residue_one_rejected()
    test_quality_ranking()
    test_temporal_confirmation()
    test_remaining_risk_documented()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
