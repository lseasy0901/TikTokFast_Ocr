# -*- coding: utf-8 -*-
"""
数据集 schema 与房间码格式冻结（Phase: Dataset Generator）。

格式冻结
    本阶段**只支持 3 种格式**，字面量定义如下（长度、字符集、字母/数字顺序
    全部写死，不允许变形、不允许字母数字交错、不允许小写）：

        AAA123     = [A-Z]{3}[0-9]{3}    长度 6
        BBBB123    = [A-Z]{4}[0-9]{3}    长度 7
        AAAA1234   = [A-Z]{4}[0-9]{4}    长度 8

    格式 ID 用全小写的短名（aaa123 / bbbb123 / aaaa1234），与 CLI 的
    --formats 取值一致。

为什么正则只写一次
    生成、校验、配额统计、测试全都从 FORMATS 取同一个正则对象，
    避免"生成的规则"和"校验的规则"各写一遍而悄悄分叉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Tuple

__all__ = [
    "CodeFormat",
    "FORMATS",
    "FORMAT_IDS",
    "DEFAULT_FORMAT_IDS",
    "SAMPLE_FIELDS",
    "SAMPLE_RESERVED_FIELDS",
    "TRUTH_FIELDS",
    "VALIDATION_STATUS_VALUES",
]


@dataclass(frozen=True)
class CodeFormat:
    """一种房间码格式的完整定义。"""

    format_id: str          # aaa123
    label: str              # AAA123 —— 人读的写法
    letters: int            # 前缀字母位数
    digits: int             # 后缀数字位数
    regex: "re.Pattern"

    @property
    def length(self) -> int:
        return self.letters + self.digits

    @property
    def code_space(self) -> int:
        """该格式的合法码总数：26**letters * 10**digits。"""
        return (26 ** self.letters) * (10 ** self.digits)

    def pattern(self) -> str:
        return self.regex.pattern


# 注意结尾用 \Z 而不是 $：Python 的 $ 还会匹配**结尾换行之前**的位置，
# 于是带结尾换行的串会被判为合法 —— 对"格式必须严格"的校验器是致命漏洞
# （本模块的单元测试就是被这一条卡住的）。\Z 只匹配真正的字符串结尾。
FORMATS: Dict[str, CodeFormat] = {
    "aaa123": CodeFormat("aaa123", "AAA123", 3, 3, re.compile(r"^[A-Z]{3}[0-9]{3}\Z")),
    "bbbb123": CodeFormat("bbbb123", "BBBB123", 4, 3, re.compile(r"^[A-Z]{4}[0-9]{3}\Z")),
    "aaaa1234": CodeFormat("aaaa1234", "AAAA1234", 4, 4, re.compile(r"^[A-Z]{4}[0-9]{4}\Z")),
}

#: 稳定的排序（供 CLI 默认值与表格输出使用，不依赖 dict 插入顺序）
FORMAT_IDS: Tuple[str, ...] = ("aaa123", "bbbb123", "aaaa1234")

#: 默认生成的格式集合 = 全部 3 种
DEFAULT_FORMAT_IDS: Tuple[str, ...] = FORMAT_IDS

#: samples.csv 的字段（本阶段就写这些）
SAMPLE_FIELDS: Tuple[str, ...] = (
    "sample_id",
    "image",
    "format_id",
    "room_code",
    "session_id",
    "timestamp_ms",
)

#: 预留字段：本阶段**不写入** samples.csv，只在 dataset.json 与 README 里声明，
#: 等接入真实 Teacher 时再正式加列。写死空列会让"没有这个阶段"和"这个阶段没数据"
#: 混在一起，所以刻意不写。
SAMPLE_RESERVED_FIELDS: Tuple[str, ...] = (
    "teacher_raw",
    "teacher_consensus",
    "review_status",
)

#: truth.csv 的字段
TRUTH_FIELDS: Tuple[str, ...] = (
    "sample_id",
    "image",
    "format_id",
    "room_code",
    "validation_status",
    "source",
)

#: validation_status 的取值
#:   confirmed  —— 来自生成器的真值，天然确定
#:   unresolved —— 看不清/无法确认（留给后续人工或 Teacher 流程）
VALIDATION_STATUS_VALUES: Tuple[str, ...] = ("confirmed", "unresolved")

# ---------------------------------------------------------------------------
# 真实采集数据集（Phase 2）的字段定义
#
# 与上面"合成 roster"的关键区别：**samples.csv 里绝不出现答案**。
# benchmark 读 samples.csv 只拿到"这一帧是什么格式"，拿不到房间码，
# 想泄漏也泄漏不出去。答案单独放 truth.csv。
# ---------------------------------------------------------------------------

#: 真实数据集的 samples.csv —— 从设计上就没有 room_code / ground_truth
REAL_SAMPLE_FIELDS: Tuple[str, ...] = (
    "sample_id",
    "image",
    "format_id",
    "session_id",
    "segment_id",
    "timestamp_ms",
)

#: truth.csv —— 答案只在这里
REAL_TRUTH_FIELDS: Tuple[str, ...] = (
    "sample_id",
    "ground_truth",
    "label_source",
    "review_status",
)

#: label_source 取值：人工输入 / 未解决（**没有** teacher 这个取值）
LABEL_SOURCES: Tuple[str, ...] = ("manual", "unresolved")

TEACHER_RAW_FIELDS: Tuple[str, ...] = (
    "sample_id", "segment_id", "image", "timestamp_ms", "sequence",
    "raw_text", "normalized_text", "score", "ok", "error", "elapsed_ms",
)

TEACHER_CONSENSUS_FIELDS: Tuple[str, ...] = (
    "session_id", "segment_id", "format_id", "teacher_consensus",
    "consensus_count", "valid_votes", "total_frames", "majority_ratio",
    "unique_candidate_count", "max_consecutive", "format_valid",
    "high_confidence", "consensus_strength", "consensus_reason",
)

SEGMENT_FIELDS: Tuple[str, ...] = (
    "session_id", "segment_id", "format_id", "ground_truth", "truth_status",
    "start_timestamp_ms", "end_timestamp_ms", "sample_count",
    "teacher_consensus", "teacher_consensus_strength",
)

REVIEW_FIELDS: Tuple[str, ...] = (
    "sample_id", "image", "segment_id", "ground_truth", "teacher_consensus",
    "teacher_raw", "reason", "review_status",
)
