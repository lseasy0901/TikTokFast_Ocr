# -*- coding: utf-8 -*-
"""
Segment 生命周期（Phase 2: Dataset Collector）。

一个 segment = 同一个房间码 + 连续稳定出现。房间码一变（或超时/停止），
旧 segment 关闭、新 segment 开始。

**关键：segment 不自己决定真值。** 它只承载两个来源不同的东西：
    ground_truth        —— 人工在采集时输入的独立真值（可能没有）
    teacher_consensus   —— Teacher 多帧汇总（预标注）
两列在 CSV 里始终分开（§8 / §10）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .consensus import ConsensusConfig, ConsensusResult, compute_consensus

__all__ = ["Segment", "SegmentTracker", "DEFAULT_SEGMENT_GAP_MS"]

#: 多久没有新的有效读法就认为当前 segment 结束（毫秒）
DEFAULT_SEGMENT_GAP_MS = 3000


@dataclass
class Segment:
    """一个 room-code segment 的全部记录。"""

    session_id: str
    segment_id: str
    format_id: str
    start_timestamp_ms: int
    end_timestamp_ms: int = 0
    #: 采集时人工输入的独立真值。None = 用户没输入 -> unresolved
    ground_truth: Optional[str] = None
    sample_ids: List[str] = field(default_factory=list)
    readings: List[Optional[str]] = field(default_factory=list)
    consensus: Optional[ConsensusResult] = None
    note: str = ""

    @property
    def sample_count(self) -> int:
        return len(self.sample_ids)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_timestamp_ms - self.start_timestamp_ms)

    @property
    def truth_status(self) -> str:
        """truth.csv 的 validation_status。

        人工没输入就是 unresolved —— **绝不**用 Teacher consensus 顶上。
        """
        return "confirmed" if self.ground_truth else "unresolved"

    def finalize(self, config: ConsensusConfig = None) -> ConsensusResult:
        self.consensus = compute_consensus(
            self.readings, self.format_id,
            config or ConsensusConfig())
        return self.consensus

    def as_dict(self) -> dict:
        c = self.consensus or ConsensusResult()
        return {
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "format_id": self.format_id,
            "ground_truth": self.ground_truth or "",
            "truth_status": self.truth_status,
            "start_timestamp_ms": self.start_timestamp_ms,
            "end_timestamp_ms": self.end_timestamp_ms,
            "sample_count": self.sample_count,
            **{k: v for k, v in c.as_dict().items()},
        }


class SegmentTracker:
    """按"读法变化"切分 segment。

    规则（全部可配）：
        * 当前读法与当前 segment 的 consensus 码不同，且新读法出现次数达到
          `switch_votes` 次 -> 关闭旧 segment，开新的（这是"房间码变化"）；
        * 距上一个有效读法超过 `gap_ms` -> 关闭并停顿（避免把两段中间的空窗
          并进同一个 segment）。
    只有**通过格式校验**的读法参与切分 —— 否则 Teacher 的偶发误读会把一个
    连续 segment 切碎。
    """

    def __init__(self, session_id: str, format_id: str,
                 gap_ms: int = DEFAULT_SEGMENT_GAP_MS,
                 switch_votes: int = 2,
                 config: ConsensusConfig = None,
                 ground_truth: Optional[str] = None):
        self.session_id = session_id
        self.format_id = format_id
        #: 采集时人工输入的房间码 —— 这是**独立真值来源**，会写进每个新 segment。
        #: 没有它时 segment 的 truth_status 一律 unresolved（绝不用 Teacher 顶替）。
        self.ground_truth = (ground_truth or "").strip() or None
        self.gap_ms = int(gap_ms)
        self.switch_votes = max(1, int(switch_votes))
        self.config = config or ConsensusConfig()
        self.closed: List[Segment] = []
        self.current: Optional[Segment] = None
        self._seq = 0
        self._pending_code: Optional[str] = None
        self._pending_votes = 0
        self._last_seen_ms: Optional[int] = None

    # ------------------------------------------------------------------
    def _new_segment(self, timestamp_ms: int,
                     ground_truth: Optional[str] = None) -> Segment:
        self._seq += 1
        return Segment(
            session_id=self.session_id,
            segment_id="SEG%04d" % self._seq,
            format_id=self.format_id,
            start_timestamp_ms=int(timestamp_ms),
            ground_truth=(ground_truth or self.ground_truth) or None,
        )

    def close(self) -> Optional[Segment]:
        if self.current is None:
            return None
        self.current.finalize(self.config)
        self.closed.append(self.current)
        seg, self.current = self.current, None
        self._pending_code, self._pending_votes = None, 0
        return seg

    # ------------------------------------------------------------------
    def observe(self, code: Optional[str], timestamp_ms: int,
                valid: bool) -> Optional[Segment]:
        """喂一个读法。返回**被关闭**的 segment（如果这一帧触发了切换）。"""
        closed = None

        if (self.current is not None and self._last_seen_ms is not None
                and timestamp_ms - self._last_seen_ms > self.gap_ms):
            closed = self.close()

        if not valid or not code:
            # 无效帧不切分、不计入 consensus，但也不结束 segment（等 gap 超时）
            if self.current is not None:
                self.current.end_timestamp_ms = max(
                    self.current.end_timestamp_ms, int(timestamp_ms))
            return closed

        self._last_seen_ms = int(timestamp_ms)

        if self.current is None:
            self.current = self._new_segment(timestamp_ms)
        elif self._current_code() != code:
            # 读法可能只是抖了一下：要连续 N 次不同才真的切
            if code == self._pending_code:
                self._pending_votes += 1
            else:
                self._pending_code, self._pending_votes = code, 1
            if self._pending_votes >= self.switch_votes:
                closed = self.close()
                self.current = self._new_segment(timestamp_ms)
            else:
                self.current.readings.append(None)   # 计一票空，不污染 consensus
                return closed

        if self.current is not None:
            self._pending_code, self._pending_votes = None, 0
            self.current.readings.append(code)
            self.current.end_timestamp_ms = int(timestamp_ms)
        return closed

    def _current_code(self) -> Optional[str]:
        if self.current is None or not self.current.readings:
            return None
        from collections import Counter
        valid = [r for r in self.current.readings if r]
        if not valid:
            return None
        return Counter(valid).most_common(1)[0][0]

    # ------------------------------------------------------------------
    def attach(self, sample_id: str) -> None:
        """把刚写入的样本挂到当前 segment 上。"""
        if self.current is not None:
            self.current.sample_ids.append(sample_id)
