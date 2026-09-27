# -*- coding: utf-8 -*-
"""
房间码生成器（Phase: Dataset Generator）。

职责
    只负责"按格式生成合法且不重复的房间码"，以及配额计划（每种格式多少码、
    每码多少样本）。不做图片、不做 OCR、不碰任何生产代码。

可复现性
    随机数**不用全局 random**，而是每个格式一个独立 Random 实例，种子由
    sha256(f"{seed}:{format_id}") 派生。这样：
      * 同一 seed 必然复现同一批码；
      * 某一种格式的生成量变化不会影响另一种格式的码（不会串号）；
      * 与 PYTHONHASHSEED 等进程级随机化无关。

不硬编码
    生成源只有字符集 A-Z / 0-9 与格式定义，不引用任何真实房间码清单。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from .schemas import DEFAULT_FORMAT_IDS, FORMATS, CodeFormat

__all__ = [
    "generate_code",
    "generate_unique_codes",
    "generate_balanced_codes",
    "validate_code",
    "FormatPlan",
    "GenerationPlan",
    "QuotaError",
]

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"


class QuotaError(ValueError):
    """请求的数量超过该格式的合法码空间（26^n * 10^m）。"""


def _rng_for(seed: int, format_id: str) -> random.Random:
    """派生一个与进程随机化无关的 Random 实例。"""
    digest = hashlib.sha256(("%s:%s" % (seed, format_id)).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def generate_code(format_id: str, rng: random.Random = None) -> str:
    """随机生成**一个**合法房间码。

    参数：
        format_id: aaa123 / bbbb123 / aaaa1234
        rng: 随机源；None 时用一个固定派生种子（仅便于调用，不用于数据集生成）

    返回：
        形如 'ABC123' 的字符串，已通过该格式的 regex 校验。
    """
    fmt = _require(format_id)
    r = rng if rng is not None else _rng_for(0, format_id)
    code = ("".join(r.choice(LETTERS) for _ in range(fmt.letters))
            + "".join(r.choice(DIGITS) for _ in range(fmt.digits)))
    if not fmt.regex.match(code):            # 生成即校验，不允许静默放过
        raise AssertionError("生成器产出了非法码: %r (%s)" % (code, format_id))
    return code


def validate_code(format_id: str, code: str) -> bool:
    """严格校验：必须完全等于该格式的正则（长度、字符集、字母/数字顺序）。"""
    fmt = _require(format_id)
    return isinstance(code, str) and bool(fmt.regex.match(code))


def _require(format_id: str) -> CodeFormat:
    try:
        return FORMATS[format_id]
    except KeyError:
        raise KeyError("未知格式 %r，可选: %s" % (format_id, list(FORMATS)))


def generate_unique_codes(format_id: str, count: int, seed: int) -> List[str]:
    """在同一格式内生成 `count` 个**互不重复**的码。

    做法是"抽到就记集合、重复就重抽"，而不是先枚举全部空间 —— 码空间
    （最小也有 1757.6 万）远大于本阶段的 100～几千，重抽几乎不发生。
    但仍在循环里设了保护：重抽超过阈值就退化为把剩余空间打乱后取，
    避免请求接近空间上限时无限循环。
    """
    fmt = _require(format_id)
    if count < 0:
        raise QuotaError("count 不能为负: %d" % count)
    if count > fmt.code_space:
        raise QuotaError(
            "%s 最多只有 %d 个合法码，请求 %d 个" % (format_id, fmt.code_space, count))

    rng = _rng_for(seed, format_id)
    seen: set = set()
    out: List[str] = []
    misses = 0
    limit = max(1000, count * 50)
    while len(out) < count and misses < limit:
        code = generate_code(format_id, rng)
        if code in seen:
            misses += 1
            continue
        seen.add(code)
        out.append(code)

    if len(out) < count:
        # 接近空间上限时的兜底：把空间打乱后补齐（可复现，因为 rng 已定种子）
        pool = [c for c in _all_codes(fmt) if c not in seen]
        rng.shuffle(pool)
        out.extend(pool[: count - len(out)])
    return out


def _all_codes(fmt: CodeFormat) -> Iterable[str]:
    """枚举该格式的全部合法码（只在兜底路径上用到，正常不会走到）。"""
    def rec(letters: str, need_l: int, need_d: int):
        if need_l:
            for ch in LETTERS:
                yield from rec(letters + ch, need_l - 1, need_d)
        elif need_d:
            for ch in DIGITS:
                yield from rec(letters + ch, 0, need_d - 1)
        else:
            yield letters
    return rec("", fmt.letters, fmt.digits)


@dataclass
class FormatPlan:
    """一种格式的配额计划与实际生成结果。"""

    format_id: str
    label: str
    codes: List[str]
    samples_per_code: int

    @property
    def unique_code_count(self) -> int:
        return len(self.codes)

    @property
    def sample_count(self) -> int:
        return self.unique_code_count * self.samples_per_code

    def as_dict(self) -> dict:
        return {
            "format_id": self.format_id,
            "label": self.label,
            "unique_code_count": self.unique_code_count,
            "samples_per_code": self.samples_per_code,
            "sample_count": self.sample_count,
        }


@dataclass
class GenerationPlan:
    """整份数据集的配额计划（生成器必须同时追踪格式/码数/样本数）。"""

    seed: int
    formats: List[FormatPlan] = field(default_factory=list)

    @property
    def total_unique_codes(self) -> int:
        return sum(f.unique_code_count for f in self.formats)

    @property
    def total_samples(self) -> int:
        return sum(f.sample_count for f in self.formats)

    def as_dict(self) -> dict:
        return {
            "seed": self.seed,
            "formats": [f.as_dict() for f in self.formats],
            "total_unique_codes": self.total_unique_codes,
            "total_samples": self.total_samples,
        }


def generate_balanced_codes(
    per_format_codes: int = 100,
    samples_per_code: int = 20,
    seed: int = 20260927,
    formats: Sequence[str] = DEFAULT_FORMAT_IDS,
) -> GenerationPlan:
    """按配额为每种格式生成码，返回 GenerationPlan。

    默认即第一版 Development Dataset 的目标：
        3 格式 × 100 唯一码 × 20 样本/码 = 6000 样本

    平衡的含义是"每种格式的 unique_code_count 与 sample_count 相等"，
    而不是"随机抓一把总数够就行"。
    """
    if per_format_codes <= 0:
        raise QuotaError("per_format_codes 必须为正: %d" % per_format_codes)
    if samples_per_code <= 0:
        raise QuotaError("samples_per_code 必须为正: %d" % samples_per_code)

    plan = GenerationPlan(seed=int(seed))
    for fid in formats:
        fmt = _require(fid)
        codes = generate_unique_codes(fid, per_format_codes, int(seed))
        plan.formats.append(FormatPlan(fid, fmt.label, codes, int(samples_per_code)))
    return plan
