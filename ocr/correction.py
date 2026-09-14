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
    "Correction",
    "ResolvedCandidate",
    "resolve_char",
    "resolve_candidate",
]

#: 单个候选允许的最大纠正位数。超过则判定为「不像房间号」而放弃。
#:
#: 取 3（= 两个已支持格式的字母前缀长度），原因是一次实测结论：
#: Tesseract 对这种字形会把整个字母前缀都读成数字形字符——
#:     "IOI1000" 读成 "1011000"，"OOO000" 读成 "000000"，
#:     "IOI123"  读成 "101123"
#: 而两张映射表都是【单射】（'1' 在字母位只可能是 'I'，'0' 只可能是 'O'），
#: 所以这里不是「多猜几位」，而是允许「整段前缀都被字形混淆改写过」。
#: 实测把上限提到 3 时，纯数字噪声（"1234567"/"9876543"）、
#: 单词（"HELLO"/"ABCABC"）仍然全部判为无结果——因为映射表之外的
#: 字符一律不猜。上限仍可通过构造参数调低（例如 1 表示只容忍单点误读）。
MAX_CORRECTIONS = 3

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
