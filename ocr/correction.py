# -*- coding: utf-8 -*-
"""
逐位 OCR 纠正 (OCR V2.1 - Format-Aware OCR)

原则（按重要性排序）：
    1. 只在【该位期望的字符类】不满足时才尝试纠正。
    2. 映射表是有限的、显式列出的常量，表里没有的字符一律判为不可纠正。
       宁可无结果，也不给出可能是错的房间号。
    3. 纠正逐位进行，因此同一个字符在不同位置可以有不同结论——
       这正是本模块存在的理由：
           'I' 在字母位保持 'I'（合法），在数字位才可能纠正为 '1'；
           'O' 在字母位保持 'O'（合法），在数字位才可能纠正为 '0'；
           'l'/'L' 在字母位是合法字母，在数字位才可能纠正为 '1'。
    4. 绝不做全局替换。全局 O->0 / I->1 会把合法房间号
       （例如 VALORANT 的 "IOI123"、三角洲的 "OOO1000"）改坏。

映射只覆盖「字形高度相似、方向唯一」的少数几对。
不确定的（例如数字位的 'A'、'G'、'D'）一律不映射。
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from .code_format import DIGIT_CHARS, LETTER_CHARS, CharClass, CodeFormat

__all__ = [
    "DIGIT_TO_LETTER",
    "LETTER_TO_DIGIT",
    "MAX_CORRECTIONS",
    "max_corrections_for",
    "Correction",
    "ResolvedCandidate",
    "resolve_char",
    "resolve_candidate",
]

#: 单个候选允许的最大纠正位数的**下限**。超过则判定为「不像房间号」而放弃。
#:
#: 取 3 的原因是一次实测结论：Tesseract 对这种字形会把整个字母前缀都读成
#: 数字形字符——
#:     "IOI1000" 读成 "1011000"，"OOO000" 读成 "000000"，
#:     "IOI123"  读成 "101123"
#: 当时的两个格式字母前缀都是 3 位，于是这一个常量同时充当了
#: 「噪声上限」与「前缀长度」两个角色。
#:
#: 现在两者分开了：本常量只剩「噪声下限」，每个格式真正需要的上限由
#: :func:`max_corrections_for` 按自己的字母前缀长度给出——否则一出现
#: 4 字母前缀的格式，整个前缀被读错时就会永远无解。
MAX_CORRECTIONS = 3


def max_corrections_for(fmt: CodeFormat, base: int = MAX_CORRECTIONS) -> int:
    """该格式在一次读取里最多允许纠正多少位。

    取「开头连续字母位的个数」与 ``base`` 的较大者：

        LLLDDDD   -> 3（与旧行为完全一致）
        LLLDDD    -> 3（同上）
        LLLLDDDD  -> 4（新增的 4 字母前缀格式，整个前缀被读错时仍可解）

    取较大者而不是直接替换：``base`` 是调用方（以及历史上的常量）表达的
    「噪声底线」，只能在它之上放宽，不会被更短的格式收紧。

    代价要说清楚：上限 = 前缀长度 时，「整串同一个数字形字符」的假阳性
    （"00000000" -> "OOOO0000"）在**该长度上**是放得进来的。这与旧格式在
    3 位上的既有行为同源（"1111000" -> "III1000" 今天就会被接受），
    不是新增的一类风险；换个说法，这是「允许整段前缀被字形混淆改写」
    这条既有约定在新长度上的自然延伸。
    """
    return max(int(base), fmt.letter_prefix_length)

#: 数字位读到「数字形字符」时的有限纠正表（左侧为 OCR 实际读到，右侧为期望字母）
DIGIT_TO_LETTER = {
    "0": "O",
    "1": "I",
    "2": "Z",
    "5": "S",
    "8": "B",
    "|": "I",   # 竖线在字母位几乎总是 I
}

#: 字母位读到「字母形字符」时的有限纠正表（左侧为 OCR 实际读到，右侧为期望数字）
LETTER_TO_DIGIT = {
    "O": "0",
    "o": "0",
    "I": "1",
    "i": "1",
    "l": "1",
    "L": "1",
    "|": "1",
    "S": "5",
    "s": "5",
    "Z": "2",
    "z": "2",
    "B": "8",
    "b": "8",
}


@dataclass(frozen=True)
class Correction:
    """一次逐位纠正：位置、原始字符、替换后的字符。"""

    index: int
    source: str
    replacement: str

    def __str__(self) -> str:
        return f"#{self.index} {self.source}->{self.replacement}"


@dataclass(frozen=True)
class ResolvedCandidate:
    """一段候选文本经过逐位纠正后、且已通过严格校验的结果。"""

    code: str
    corrections: Tuple[Correction, ...]
    source_text: str
    #: 被大小写折掉的位置（源字符是小写、该位本该大写）。
    #: 不算「纠正」（字符类没变），但它是「引擎对这个字形没把握」的证据：
    #: 实测 'GGM180' 在小字号下会被读成 'GoM180'/'Gom180'——第二个 G 的横杠
    #: 细到 Tesseract 认不出来，于是退化成圆形的小写 'o'。
    #: 保留位置信息，供上层「放大重读、只允许改动这些位」使用。
    case_folds: Tuple[int, ...] = ()

    @property
    def correction_count(self) -> int:
        return len(self.corrections)

    @property
    def corrected(self) -> bool:
        return bool(self.corrections)

    @property
    def case_fold_count(self) -> int:
        return len(self.case_folds)

    def correction_summary(self) -> str:
        """"#5 O->0" 这样的可读摘要，供日志使用。"""
        return ", ".join(str(c) for c in self.corrections)


def resolve_char(char: str, expected: CharClass) -> Optional[str]:
    """
    按期望字符类把【单个原始字符】解析为合法字符；不可解析返回 None。

    注意大小写：字母位上的小写字母属于规范化（折成大写），
    不算纠正；数字位上的 'l'/'o' 之类才算纠正。
    """
    if expected is CharClass.LETTER:
        upper = char.upper()
        if upper in LETTER_CHARS:
            return upper
        # 数字形字符出现在字母位：查有限表，查不到就不猜
        return DIGIT_TO_LETTER.get(char, DIGIT_TO_LETTER.get(upper))

    if char in DIGIT_CHARS:
        return char
    # 字母形字符出现在数字位：查有限表，查不到就不猜
    return LETTER_TO_DIGIT.get(char, LETTER_TO_DIGIT.get(char.upper()))


def resolve_candidate(
    text: str,
    fmt: CodeFormat,
    max_corrections: int = MAX_CORRECTIONS,
) -> Optional[ResolvedCandidate]:
    """
    把一段定长候选文本解析成合法房间号。

    返回 None 的情形（一律视为「不是房间号」）：
        - 长度不等于格式长度
        - 任一位既不是合法字符、也不在有限纠正表里
        - 需要纠正的位数超过 max_corrections
        - 纠正后仍未通过严格校验（双保险，正常不会发生）
    """
    if not isinstance(text, str) or len(text) != fmt.length:
        return None

    chars = []
    corrections = []
    folds = []
    for index, char in enumerate(text):
        expected = fmt.expected_class(index)
        resolved = resolve_char(char, expected)
        if resolved is None:
            # 有限映射之外 —— 不猜测，直接放弃该候选
            return None
        # 纯大小写折叠不算「纠正」，只有字符类发生变化才算
        if resolved != char.upper():
            corrections.append(Correction(index, char, resolved))
        elif char != resolved:
            # 只是大小写不同：记下位置（不计入纠正次数）
            folds.append(index)
        chars.append(resolved)

    if len(corrections) > max_corrections:
        return None

    code = "".join(chars)
    if not fmt.accepts(code):
        # 理论上不可达；保留它是为了让「绝不返回非法房间号」这条契约
        # 不依赖上面那些分支的正确性。
        return None

    return ResolvedCandidate(code, tuple(corrections), text, tuple(folds))
