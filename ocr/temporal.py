# -*- coding: utf-8 -*-
"""
时序候选裁决 (OCR V2.3 - TemporalResolver)

    RoomCodeRecognizer 每帧把裁决输入交给 CandidateResolver：
    要么是一个【已经通过严格校验】的房间号，要么是 None（这一帧没读出合法房间号）。
    TemporalResolver 在两者之上再加一层时序一致性判断：

        同一个房间号连续出现 >= min_agreement 次 -> 允许成立（emit）
        否则                                      -> 返回 None（继续观察）

这样单帧误读不会立刻变成对外结果，而稳定读数依旧在第二帧就出来
（默认 300ms 间隔 -> 约 600ms 后即可确认首个结果，不额外等待满窗口）。

只做时序一致性，不做别的事：
    - 不参与格式校验、不做字符纠正（进来的 candidate 一定是校验过的）
    - 不做模糊匹配：不同房间号之间只做精确字符串比较，绝不「相似就合并」
    - 无概率模型、无 ML、无外部依赖，纯确定性状态机
    - 不阻塞调用方：满足一致性的那一帧立即返回

歧义时宁可不出结果（prefer no result over guessing）：
    X、Y 交替出现时任何一方都凑不齐连续 min_agreement 次，于是始终返回 None。
"""

import logging
from collections import deque
from dataclasses import replace
from typing import Deque, Optional, Tuple

from .room_code import CandidateResolver, RoomCodeCandidate

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.temporal")

__all__ = ["TemporalResolver", "DEFAULT_MIN_AGREEMENT", "DEFAULT_WINDOW_SIZE"]

#: 默认配置：最近 3 次观测里，同一房间号连续出现 2 次即成立。
DEFAULT_MIN_AGREEMENT = 2
DEFAULT_WINDOW_SIZE = 3


class TemporalResolver(CandidateResolver):
    """
    基于「连续一致观测」的候选裁决器。

    参数：
        min_agreement: 成立所需的连续一致次数（含当前帧），默认 2
        window_size: 只保留最近多少次观测，默认 3

    状态：
        _window: 最近 window_size 次裁决输入（房间号或 None），有界
        _last_emitted: 最近一次对外成立的房间号（用于去重）

    语义细节：
        - None 既不计入一致次数，也不打断连续计数（它只表示「这帧没读出来」）；
          一致次数只由真正读出的、且已通过严格校验的房间号累加。
        - 只有【末尾连续】的同一房间号才算一致。X、Y 交替永远不成立。
        - 同一个房间号成立后重复出现，会带着 repeated=True 返回：
          调用方据此避免重复的剪贴板/输出事件，同时保留「当前房间号」的显示。
        - 换成另一个房间号时，它必须重新满足同样的规则（连续 min_agreement 次）
          才能成立。
    """

    def __init__(
        self,
        min_agreement: int = DEFAULT_MIN_AGREEMENT,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ):
        min_agreement = int(min_agreement)
        window_size = int(window_size)

        if window_size < 1:
            raise ValueError(f"window_size 至少为 1，收到 {window_size}")
        if min_agreement < 1:
            raise ValueError(f"min_agreement 至少为 1，收到 {min_agreement}")
        if min_agreement > window_size:
            # 窗口里最多只能放下 window_size 次观测，永远凑不齐 -> 直接拒绝配置
            raise ValueError(
                f"min_agreement({min_agreement}) 不能大于 window_size({window_size})"
            )

        self._min_agreement = min_agreement
        self._window_size = window_size
        self._window: Deque[Optional[str]] = deque(maxlen=window_size)
        self._last_emitted: Optional[str] = None

        logger.info(
            "时序裁决器: 连续一致阈值=%d/%d", self._min_agreement, self._window_size
        )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def min_agreement(self) -> int:
        return self._min_agreement

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def observations(self) -> Tuple[Optional[str], ...]:
        """当前窗口内的观测（房间号或 None），只读快照。"""
        return tuple(self._window)

    @property
    def last_emitted_code(self) -> Optional[str]:
        """最近一次对外成立的房间号（None 表示尚未有结果成立）。"""
        return self._last_emitted

    # ------------------------------------------------------------------
    # 裁决
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空时序状态（换游戏档 / 重新开始识别时使用）。"""
        self._window.clear()
        self._last_emitted = None
        logger.info("时序裁决器状态已重置")

    def offer(
        self, candidate: Optional[RoomCodeCandidate]
    ) -> Optional[RoomCodeCandidate]:
        """喂入这一帧的裁决输入，返回【当前可以对外成立】的候选或 None。

        参数：
            candidate: 已通过严格校验的房间号；None 表示这一帧没有合法结果

        返回：
            连续一致性达标的房间号；未达标返回 None。
            若返回的房间号与上次成立的是同一个，其 repeated 为 True。
        """
        if candidate is None:
            self._window.append(None)
            return None

        code = candidate.code
        self._window.append(code)

        agreement = self._agreement(code)
        if agreement < self._min_agreement:
            logger.debug(
                "[TEMPORAL] 尚未成立: %s 连续 %d/%d (窗口=%s)",
                code, agreement, self._min_agreement, list(self._window),
            )
            return None

        if code == self._last_emitted:
            # 已经成立过的同一房间号：不重复触发输出，但把结果继续交出去
            return replace(candidate, repeated=True)

        self._last_emitted = code
        logger.info(
            "[TEMPORAL] 房间号成立: %s (连续 %d/%d)", code, agreement, self._min_agreement
        )
        return candidate

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _agreement(self, code: str) -> int:
        """窗口末尾与该房间号一致的连续观测数（None 跳过，不打断）。"""
        count = 0
        for observed in reversed(self._window):
            if observed is None:
                continue
            if observed != code:
                break
            count += 1
        return count
