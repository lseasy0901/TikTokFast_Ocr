# -*- coding: utf-8 -*-
"""
多帧一致性（Phase 2: Dataset Collector）。

一个 segment 里有很多帧，每帧一个 Teacher 读法。**不直接相信单帧**，
而是把同一 segment 的读法汇总成一个 consensus。

产出三个量（任务书 §9）：
    majority_ratio        —— 最常见读法的占比
    unique_candidate_count —— 出现过几种不同读法
    max_consecutive       —— 最长连续相同次数

再加一个"是否算高可信"的判定，阈值**全部可配**。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

__all__ = [
    "ConsensusResult",
    "ConsensusConfig",
    "compute_consensus",
    "DEFAULT_CONSENSUS_CONFIG",
]


@dataclass(frozen=True)
class ConsensusConfig:
    """高可信判定的阈值。默认值见任务书 §9，**不写死在判定里**。"""

    min_votes: int = 3            # 至少 3 次有效识别
    majority_ratio: float = 0.80  # 最常见的读法占比 >= 0.80
    min_consecutive: int = 3      # 最长连续一致 >= 3
    require_valid_format: bool = True

    def as_dict(self) -> dict:
        return {
            "min_votes": self.min_votes,
            "majority_ratio": self.majority_ratio,
            "min_consecutive": self.min_consecutive,
            "require_valid_format": self.require_valid_format,
        }


DEFAULT_CONSENSUS_CONFIG = ConsensusConfig()


@dataclass
class ConsensusResult:
    code: Optional[str] = None                 # 最常见读法（无有效读法则 None）
    votes: int = 0                             # 该读法的票数
    valid_votes: int = 0                       # 有结果的帧数
    total_frames: int = 0
    majority_ratio: float = 0.0
    unique_candidate_count: int = 0
    max_consecutive: int = 0
    format_valid: bool = False
    high_confidence: bool = False
    reason: str = ""
    histogram: List[tuple] = field(default_factory=list)

    @property
    def strength(self) -> float:
        """consensus 强度：票数占比 × 连续度因子，供排序/展示。"""
        if not self.valid_votes:
            return 0.0
        return round(self.majority_ratio * min(1.0, self.max_consecutive / 3.0), 4)

    def as_dict(self) -> dict:
        return {
            "teacher_consensus": self.code or "",
            "consensus_count": self.votes,
            "valid_votes": self.valid_votes,
            "total_frames": self.total_frames,
            "majority_ratio": round(self.majority_ratio, 4),
            "unique_candidate_count": self.unique_candidate_count,
            "max_consecutive": self.max_consecutive,
            "format_valid": int(self.format_valid),
            "high_confidence": int(self.high_confidence),
            "consensus_strength": self.strength,
            "consensus_reason": self.reason,
        }


def _max_consecutive(readings: Sequence[Optional[str]], code: Optional[str]) -> int:
    if not code:
        return 0
    best = cur = 0
    for r in readings:
        if r == code:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_consensus(readings: Sequence[Optional[str]],
                      format_id: str,
                      config: ConsensusConfig = DEFAULT_CONSENSUS_CONFIG,
                      format_validator=None) -> ConsensusResult:
    """把一串逐帧读法汇总成 consensus。

    参数：
        readings: 逐帧的 normalized_text（None/空 = 该帧没有结果）
        format_id: 用于格式合法性判定
        config: 阈值
        format_validator: 可注入的 (format_id, code) -> bool；默认用 schemas 的正则
    """
    if format_validator is None:
        from .code_generator import validate_code as format_validator  # noqa: F811

    valid = [r for r in readings if r]
    res = ConsensusResult(total_frames=len(readings), valid_votes=len(valid))
    if not valid:
        res.reason = "teacher_no_result"
        return res

    counter = Counter(valid)
    res.histogram = counter.most_common()
    res.unique_candidate_count = len(counter)
    code, votes = counter.most_common(1)[0]
    res.code = code
    res.votes = votes
    res.majority_ratio = votes / len(valid)
    res.max_consecutive = _max_consecutive(readings, code)
    res.format_valid = bool(format_validator(format_id, code))

    if res.unique_candidate_count > 1:
        res.reason = "teacher_conflict"
    elif not res.format_valid:
        res.reason = "format_invalid"
    elif len(valid) < config.min_votes:
        res.reason = "insufficient_votes"
    elif res.majority_ratio < config.majority_ratio:
        res.reason = "low_majority"
    elif res.max_consecutive < config.min_consecutive:
        res.reason = "low_consecutive"
    else:
        res.reason = ""
        res.high_confidence = True

    if config.require_valid_format and not res.format_valid:
        res.high_confidence = False
    return res
