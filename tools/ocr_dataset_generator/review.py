# -*- coding: utf-8 -*-
"""
人工复核队列（Phase 2: Dataset Collector）。

**哪些样本需要人看** 的判定集中在这里。判定只看"可观测证据"，
不看任何期望答案。

任务书 §11 列出的入队原因：
    Teacher 无结果 / 多候选冲突 / consensus 不足 / 格式不合法 /
    人工 GT 与 Teacher 不一致 / room code 变化边界 / 重复或疑似重复
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ReviewItem",
    "ReviewDecision",
    "decide_review",
    "REVIEW_REASONS",
]

#: 固定的原因码（写进 CSV，便于统计）
REVIEW_SEGMENT_REASONS = (
    "teacher_no_result",      # Teacher 整段没有结果
    "teacher_conflict",       # 段内出现多个不同读法
    "consensus_insufficient",  # 票数/占比/连续度不够
    "format_invalid",         # Teacher 读法不满足任何格式
    "truth_teacher_mismatch",  # 人工 GT 与 Teacher consensus 不一致
    "unresolved_no_manual_truth",  # 没人工 GT，一律进复核（不得自动当 Gold）
    "no_segment",             # 整段没有形成合法 segment（读法不符目标格式）
)
REVIEW_SAMPLE_REASONS = (
    "sequence_boundary",      # 房间码变化边界附近的连续帧
    "duplicate_exact",        # SHA-256 完全相同
    "duplicate_near",         # dHash 近似重复
)
REVIEW_REASONS = REVIEW_SEGMENT_REASONS + REVIEW_SAMPLE_REASONS


@dataclass
class ReviewDecision:
    needed: bool
    reason: str = ""
    review_status: str = "pending"

    def __bool__(self) -> bool:
        return self.needed


def decide_review(segment, consensus, *, config=None) -> ReviewDecision:
    """对一个 segment（及其 consensus）判定是否需要人工复核。

    参数只用到"观测到的证据"：consensus 的统计量、格式合法性、
    以及**人工输入**的 ground_truth。Teacher 的读法不参与"真值"判断。
    """
    reasons = []

    if consensus is None or not consensus.valid_votes:
        reasons.append("teacher_no_result")
    else:
        if consensus.unique_candidate_count > 1:
            reasons.append("teacher_conflict")
        if not consensus.format_valid:
            reasons.append("format_invalid")
        if not consensus.high_confidence and consensus.reason in (
                "insufficient_votes", "low_majority", "low_consecutive"):
            reasons.append("consensus_insufficient")

    manual = getattr(segment, "ground_truth", None)
    if not manual:
        # 没有人工真值：必须进复核，且**不得**用 Teacher consensus 顶替
        reasons.append("unresolved_no_manual_truth")
    elif consensus is not None and consensus.code and consensus.code != manual:
        reasons.append("truth_teacher_mismatch")

    if not reasons:
        return ReviewDecision(False)
    return ReviewDecision(True, reason=";".join(dict.fromkeys(reasons)))


def decide_sample_review(*, boundary: bool = False, exact_dup: bool = False,
                         near_dup: bool = False) -> ReviewDecision:
    """逐样本的复核判定（边界帧 / 重复）。"""
    reasons = []
    if boundary:
        reasons.append("sequence_boundary")
    if exact_dup:
        reasons.append("duplicate_exact")
    if near_dup:
        reasons.append("duplicate_near")
    if not reasons:
        return ReviewDecision(False)
    return ReviewDecision(True, reason=";".join(reasons))
