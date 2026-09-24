# -*- coding: utf-8 -*-
"""候选流 Failover：核心侧的「这个候选起不来，换下一个」。

设计边界（本阶段刻意保持最小）：
    候选顺序**完全由平台适配器决定**。本模块只维护「试到第几个了」这一件事：
    不重排、不评分、不测速、不比较码率、不测 RTT —— 所以它不认识 CDN、
    不认识平台、不认识 FFmpeg，也不认识 Qt。

    它也不持有播放器。创建/销毁 FFmpegReader、FrameBridge、定时器属于调用方
    （MainWindow）的职责；本模块只在被问到时回答「下一个该试谁」。

    「失败」的定义由调用方给出，本模块不做判断 —— 调用方观察到一次播放
    尝试确定起不来之后，调 :meth:`note_failure` 结算它，再调
    :meth:`next_candidate` 前进。

    只处理「从未成功播放」的候选。曾经出过首帧的候选说明它启动成功了，
    其后续断流属于直播结束，不是 failover 的范畴（本阶段不做自动重连）。
    这个判断同样由调用方完成，本模块只提供 :meth:`note_success` 让调用方
    在播放真正建立后终止迭代。
"""

__all__ = ["CandidateFailover"]


class CandidateFailover:
    """在一次连接会话内，按平台给定的顺序逐个尝试候选流。

    状态机很薄，只有三个变量：

    - ``_index``：本次（或下一次）尝试的候选下标，初始 -1
    - ``_pending``：是否有一个已发出但尚未结算的尝试
    - ``_cancelled`` / ``_settled``：会话是否已终止

    因此 :meth:`next_candidate` 在同一个尝试上重复调用是幂等的，
    :meth:`note_failure` 重复调用也不会一次跳过两个候选。
    """

    def __init__(self, info):
        """
        :param info: StreamInfo；只用它的 candidates（按平台优先级已排好序）
                     与 selected（平台选中的那一个，仅作兜底）。
                     候选列表里 url 为空的条目会被跳过 —— 它们不是一个
                     可尝试的尝试。
        """
        candidates = [c for c in (getattr(info, "candidates", None) or ())
                      if c is not None and getattr(c, "url", None)]

        # 兜底：适配器理论上总是产出 candidates（selected 就是其中的第一个），
        # 但万一只有 selected，也要能尝试一次，而不是直接判定无候选。
        selected = getattr(info, "selected", None)
        if selected is not None and getattr(selected, "url", None) \
                and selected not in candidates:
            candidates.insert(0, selected)

        self._candidates = candidates
        self._index = -1
        self._pending = False
        self._cancelled = False
        self._settled = False
        self._exhausted = not candidates    # 没有候选 = 一开始就耗尽
        self._failed = 0

    # ------------------------------------------------------------------
    # 迭代
    # ------------------------------------------------------------------
    def next_candidate(self):
        """返回下一个要尝试的候选；没有则返回 None。

        首次调用返回候选 A。之后只有在上一次尝试被 :meth:`note_failure`
        结算过之后才会前进一格；因此重复调用本方法不会推进下标，
        重复 note_failure() 也不会一次跳过两个候选。

        返回 None 的三种情况：候选耗尽、已 cancel、已 settled
        （后两者通过 :attr:`cancelled` / :attr:`settled` 区分）。
        """
        if self._cancelled or self._settled:
            return None

        if self._pending:
            # 上一次发出的尝试尚未结算：回答"还是它"，不推进
            return self._candidates[self._index]

        if self._index + 1 >= len(self._candidates):
            self._settled = True        # 候选耗尽
            self._exhausted = True
            return None

        self._index += 1
        self._pending = True
        return self._candidates[self._index]

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def note_failure(self):
        """结算当前尝试：这个候选起不来。

        **幂等**：没有在途尝试（或已 cancel / 已 settled）时返回 False
        且不改变任何状态，因此连续调用两次不会让下一次
        :meth:`next_candidate` 跳过两个候选。

        :return: True = 本次调用确实结算了一个在途尝试
        """
        if self._cancelled or self._settled or not self._pending:
            return False
        self._pending = False
        self._failed += 1
        return True

    def note_success(self):
        """结算当前尝试：这个候选播放成功。

        成功后迭代立即终止（settled），不会再换候选 —— 播放建立之后
        发生的事情由调用方按自己的规则处理，不属于 failover。

        :return: True = 本次调用确实终止了迭代
        """
        if self._cancelled or self._settled:
            return False
        self._settled = True
        self._pending = False
        return True

    def cancel(self):
        """取消整次迭代（断开 / 关闭窗口）。

        幂等。cancel 之后 :meth:`next_candidate` 永远返回 None，
        且不可能再启动任何候选。
        """
        self._cancelled = True
        self._pending = False

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        """是否已被取消"""
        return self._cancelled

    @property
    def settled(self) -> bool:
        """迭代是否已终止（候选耗尽，或播放已成功建立）"""
        return self._settled

    @property
    def exhausted(self) -> bool:
        """是否因为候选耗尽而终止（区别于播放成功建立后的 settled）"""
        return self._exhausted

    @property
    def total(self) -> int:
        """可尝试的候选总数"""
        return len(self._candidates)

    @property
    def index(self) -> int:
        """当前尝试的下标；尚未开始为 -1"""
        return self._index

    @property
    def attempt_number(self) -> int:
        """当前是第几次尝试（1 起算）；尚未开始为 0"""
        return self._index + 1

    @property
    def candidate(self):
        """当前正在尝试的候选；尚未开始为 None"""
        if 0 <= self._index < len(self._candidates):
            return self._candidates[self._index]
        return None

    @property
    def failed_count(self) -> int:
        """已结算为失败的候选数"""
        return self._failed

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志
        return (
            f"<CandidateFailover attempt={self.attempt_number}/{self.total} "
            f"failed={self._failed} pending={self._pending} "
            f"cancelled={self._cancelled} settled={self._settled}>"
        )
