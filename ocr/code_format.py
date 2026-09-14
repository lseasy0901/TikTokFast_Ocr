# -*- coding: utf-8 -*-
"""
房间号格式定义 (OCR V2.1 - Format-Aware OCR)

一个 CodeFormat 只回答一个问题：房间号第 i 位必须是什么字符类。

为什么不是「一个正则字段」：
    逐位字符类是【严格校验 / 候选提取 / 逐位纠正】三者共用的唯一事实来源。
    只存一个正则的话，纠正逻辑得反过来解析正则，格式也无法按位被消费。
    逐位序列天然支持：
        - 同一游戏多种格式（GameProfile 持有多个 CodeFormat）
        - 未来新增长度/字符类组合（from_spec("LLLDDDDDD") 即可）
    由逐位序列派生出的 regex 只是给日志和外部调用方看的便捷视图。

本模块不做任何字符替换：
    I/O/1/0 是否合法完全由所在位的字符类决定——字母位接受 I/O，
    数字位接受 1/0。纠正见 ocr/correction.py（逐位、有限映射）。
"""

import re
from enum import Enum
from typing import Optional, Tuple

__all__ = ["CharClass", "CodeFormat", "LETTER_CHARS", "DIGIT_CHARS"]


class CharClass(Enum):
    """某一位允许的字符类。"""

    LETTER = "letter"   # A-Z（只接受大写；小写属于规范化，由纠正层折成大写）
    DIGIT = "digit"     # 0-9


LETTER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGIT_CHARS = frozenset("0123456789")

_CLASS_CHARS = {CharClass.LETTER: LETTER_CHARS, CharClass.DIGIT: DIGIT_CHARS}
_CLASS_REGEX = {CharClass.LETTER: "[A-Z]", CharClass.DIGIT: "[0-9]"}

# from_spec() 规格串里表示字符类的字母
_SPEC_CLASSES = {"L": CharClass.LETTER, "D": CharClass.DIGIT}


def _regex_body(positions: Tuple[CharClass, ...]) -> str:
    """
    把逐位字符类折叠成人类可读的正则体：

        (LETTER, LETTER, LETTER, DIGIT, DIGIT) -> "[A-Z]{3}[0-9]{2}"

    只是展示视图；连续相同字符类合并成量词，便于日志和外部文档
    直接对上约定写法（例如 ^[A-Z]{3}[0-9]{4}$）。
    """
    parts = []
    index = 0
    while index < len(positions):
        current = positions[index]
        run = 1
        while index + run < len(positions) and positions[index + run] is current:
            run += 1
        parts.append(_CLASS_REGEX[current] + (f"{{{run}}}" if run > 1 else ""))
        index += run
    return "".join(parts)


class CodeFormat:
    """
    定长房间号格式：逐位字符类序列。

    参数：
        name: 格式标识（例如 "delta_force_7"）
        positions: 逐位字符类，长度即房间号长度
        description: 人类可读说明（仅用于日志/UI）

    示例：
        CodeFormat.from_spec("valorant_6", "LLLDDD")
        # 前 3 位 A-Z，后 3 位 0-9，正好 6 位
    """

    __slots__ = ("_name", "_positions", "_description", "_regex")

    def __init__(self, name: str, positions: Tuple[CharClass, ...], description: str = ""):
        if not name:
            raise ValueError("CodeFormat 需要非空的 name")
        if not positions:
            raise ValueError("CodeFormat 至少需要一位")
        for pos in positions:
            if not isinstance(pos, CharClass):
                raise ValueError(f"positions 只能是 CharClass，收到 {pos!r}")

        self._name = name
        self._positions = tuple(positions)
        self._description = description or name
        self._regex = re.compile("^" + _regex_body(self._positions) + "$")

    # ------------------------------------------------------------------
    # 构造辅助
    # ------------------------------------------------------------------
    @classmethod
    def from_spec(cls, name: str, spec: str, description: str = "") -> "CodeFormat":
        """
        用紧凑规格串构造格式：L = 字母位，D = 数字位。

        这是把「新格式」加进项目的唯一写法，
        不需要在 OCR 引擎里新增任何分支。
        """
        if not spec:
            raise ValueError("spec 不能为空")
        try:
            positions = tuple(_SPEC_CLASSES[ch.upper()] for ch in spec)
        except KeyError as exc:
            raise ValueError(
                f"spec 只能包含 'L'(字母) 或 'D'(数字)，收到 {exc.args[0]!r}"
            ) from None
        return cls(name, positions, description)

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def positions(self) -> Tuple[CharClass, ...]:
        return self._positions

    @property
    def length(self) -> int:
        return len(self._positions)

    @property
    def regex(self) -> str:
        """派生正则（只读视图，校验不使用它）。"""
        return self._regex.pattern

    def expected_class(self, index: int) -> CharClass:
        """第 index 位期望的字符类。"""
        return self._positions[index]

    # ------------------------------------------------------------------
    # 严格校验
    # ------------------------------------------------------------------
    def accepts_char(self, char: str, index: int) -> bool:
        """单个字符是否满足第 index 位的字符类（不做任何大小写/形状纠正）。"""
        return char in _CLASS_CHARS[self._positions[index]]

    def accepts(self, text: Optional[str]) -> bool:
        """
        严格校验：长度必须精确相等，且每一位都必须满足该位的字符类。

        不做 strip、不做填充、不做替换——这类工作属于上游的清洗/纠正层。
        """
        if not isinstance(text, str) or len(text) != self.length:
            return False
        return all(
            char in _CLASS_CHARS[pos] for char, pos in zip(text, self._positions)
        )

    def describe(self) -> str:
        """例如 'LLLDDDD (7 位)'，供日志与未来的 UI 提示使用。"""
        spec = "".join({CharClass.LETTER: "L", CharClass.DIGIT: "D"}[p] for p in self._positions)
        return f"{spec} ({self.length} 位)"

    def __repr__(self) -> str:
        return f"<CodeFormat {self._name} {self.describe()}>"
