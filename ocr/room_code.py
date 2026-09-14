# -*- coding: utf-8 -*-
"""
房间号识别管线 (OCR V2.1 - Format-Aware OCR)

    GameProfile -> CodeFormat -> ROI 帧 -> 轻量预处理(可选缩放) -> OCR 引擎
        -> 文本清洗 -> 候选提取 -> 逐位纠正 -> 严格校验 -> RoomCodeCandidate

一帧有多个 OCR 读法时（每个 PSM 一个），先各自走完上面这条管线，
再在【已通过严格校验的候选之间】仲裁——见 _arbitrate()。
仲裁只看「谁更少改动」：纠正次数少者优先，其次大小写折叠少者。
因此仲裁不会把合法房间号变成非法房间号，也不会引入模糊匹配。

对外契约（重要）：
    recognize_frame() / recognize_text() 只会返回
        - 通过严格校验的房间号（RoomCodeCandidate）
        - 或 None
    绝不返回未通过校验的文本。调用方不需要、也不应该再猜测识别结果的含义。

选错游戏（只判定、不切换）：
    某一帧的读法【明显】说明用户选的游戏不对时（判据见 _detect_conflict），
    recognize_frame() 同样返回 None，并通过 last_conflict 暴露判定证据，
    上层据此提醒用户重新选择。判定【不改变】任何档位：
    本模块既不会自动切换游戏，也不会给出「应该选哪款」的建议。

不做的事：
    - 不改动 OCR 引擎的线程/启动行为（引擎仍然由工作线程延迟构造）
    - 本文件不做多帧累积：时序一致性在 ocr/temporal.py 的 TemporalResolver 中，
      通过 CandidateResolver 接口接入（默认仍是单帧直通 SingleFrameResolver）
    - 不做任何全局字符替换（见 ocr/correction.py 的有限映射）
"""

import logging
import re
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .code_format import CodeFormat
from .correction import MAX_CORRECTIONS, ResolvedCandidate, resolve_candidate
from .profiles import GameProfile, game_profiles, get_profile

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.room_code")

__all__ = [
    "SUPPORTED_SCALES",
    "DEFAULT_SCALE",
    "RoomCodeCandidate",
    "GameSelectionConflict",
    "RoomCodeRecognizer",
    "CandidateResolver",
    "SingleFrameResolver",
    "normalize_text",
    "extract_runs",
]

#: 允许的缩放档位。默认 1.0（不放大）——放大会改变笔画粗细，
#: 对 I/1、O/0 这类字形本来就容易互相误判的字符有风险，
#: 因此放大只是「可配置的实验能力」，不作为默认行为。
SUPPORTED_SCALES: Tuple[float, ...] = (1.0, 1.5, 2.0)
DEFAULT_SCALE = 1.0

#: 读法里出现「本该大写、却被读成小写」的位时，放大重读一次的倍率。
#: 实测依据：同样是 'GGM180'，紧贴房间号的小图在 1.0x 下三个 PSM 都读成
#: 'GoM180'（第二个 G 的横杠太细，退化成圆形），放大到 1.5x/2.0x 后
#: 三个 PSM 都读回 'GGM180'。放大只用来「看清那几位的字形」，
#: 因此结果还必须满足 _escalate() 的逐位一致约束。
_ESCALATION_SCALE = 2.0

#: 连续的可疑字符段：字母、数字，以及 '|'（'I' 的常见误读形态）。
#: 其它字符（空白、标点、中文）一律作为分隔符，起到分词作用。
_RUN_RE = re.compile(r"[A-Za-z0-9|]+")

#: 允许把被分隔符切开的若干段重新拼回时的最大段数
_MAX_JOIN_PARTS = 3

#: 候选来源质量（越小越可信）。同一游戏有多个格式时，
#: 先比质量再比纠正次数——「长度恰好合适的整段」永远优先于
#: 「从一个更长的段里滑窗截出来的子串」。
_QUALITY_EXACT = 0     # 单个连续段，长度恰好等于格式长度
_QUALITY_WINDOW = 1    # 从过长的连续段里滑窗取出
_QUALITY_JOINED = 2    # 由多个被分隔符切开的段拼回


@dataclass(frozen=True)
class _FormatMatch:
    """某个格式给出的最佳候选，以及它的来源质量。"""

    candidate: "RoomCodeCandidate"
    quality: int


@dataclass(frozen=True)
class GameSelectionConflict:
    """
    「所选游戏与识别内容明显冲突」的判定证据。

    仅供内部日志与上层提醒使用——它是【判定依据】，不是建议：
    任何调用方都不得据此自动切换游戏档，面向用户的文案也不得暴露这里
    的字段（游戏档名、位数、格式名都属于技术细节，见产品要求）。
    """

    #: 用户当前选择的档（判定为选错的那一个）
    selected_profile_id: str
    #: 这帧内容里形状明确属于的另一款具体游戏（只用于日志）
    other_profile_id: str

    def describe(self) -> str:
        """日志用摘要。注意：不得直接展示给用户。"""
        return (f"所选档={self.selected_profile_id}, "
                f"读到的码形状属于 {self.other_profile_id}")


@dataclass(frozen=True)
class RoomCodeCandidate:
    """一个通过严格校验的房间号候选。"""

    code: str
    profile_id: str
    format_name: str
    raw_text: str                # OCR 原始输出（调试用）
    source_text: str             # 实际参与校验/纠正的那一段
    corrections: Tuple[object, ...] = ()
    scale: float = DEFAULT_SCALE
    #: 该读法里「本该大写、却被读成小写」的位数（不计入 corrections）。
    #: 它是字形不确定的信号：>0 表示这几位可能认错，需要放大复看。
    case_fold_count: int = 0
    #: 该结果是否与最近一次成立的房间号相同（由时序裁决器标注）。
    #: 单帧直通时恒为 False；调用方据此避免重复的剪贴板/输出事件。
    repeated: bool = False

    @property
    def correction_count(self) -> int:
        return len(self.corrections)

    @property
    def corrected(self) -> bool:
        return bool(self.corrections)

    def correction_summary(self) -> str:
        return ", ".join(str(c) for c in self.corrections)

    def __str__(self) -> str:
        return self.code


class CandidateResolver:
    """
    候选裁决接口（预留）。

    已有实现：
        - SingleFrameResolver：单帧直通（默认，识别到什么就返回什么）
        - TemporalResolver（ocr/temporal.py）：连续 N 次一致才成立

    新增裁决器只需实现同样的 offer()（以及可选的 reset()），
    并在构造 RoomCodeRecognizer 时传入，识别管线本身不需要任何改动。

    约定：传进来的 candidate 一定是【已经通过严格校验】的房间号或 None，
    裁决器不得再参与格式校验、纠正或模糊匹配。
    """

    def offer(self, candidate: Optional[RoomCodeCandidate]) -> Optional[RoomCodeCandidate]:
        raise NotImplementedError

    def reset(self) -> None:
        """清空裁决器内部状态（默认无状态，不需要处理）。"""
        return None


class SingleFrameResolver(CandidateResolver):
    """单帧直通：这一帧识别到什么就返回什么（可能是 None）。"""

    def offer(self, candidate: Optional[RoomCodeCandidate]) -> Optional[RoomCodeCandidate]:
        return candidate


def normalize_text(text: Optional[str]) -> str:
    """
    清洗 OCR 文本——只做「去噪」，不做任何字符替换。

    - 所有空白（含换行/制表/全角空格）折叠成单个空格，
      既去掉行结构，又保留分词边界：
      换行直接删除会把相邻的词粘成一个长串，反而制造歧义。
    - 去掉零宽字符与 BOM。
    字母、数字、'|' 全部原样保留，交给逐位校验/纠正去判断。
    """
    if not text:
        return ""
    cleaned = text.replace("﻿", "").replace("​", "")
    return re.sub(r"\s+", " ", cleaned, flags=re.UNICODE).strip()


def extract_runs(text: str) -> List[str]:
    """把清洗后的文本切成连续的字母数字段（'|' 保留在段内）。"""
    return _RUN_RE.findall(text or "")


def _window_offsets(run_length: int, code_length: int) -> Sequence[int]:
    """在过长的段里滑动取窗口；长度恰好相等时只有偏移 0。"""
    if run_length == code_length:
        return (0,)
    return range(run_length - code_length + 1)


class RoomCodeRecognizer:
    """
    OCR Core：把一帧 ROI 图像变成「严格校验过的房间号或 None」。

    参数：
        engine: 已构造的 OCREngine（本类不负责它的构造/线程归属）
        profile: GameProfile 或 profile_id 字符串；None 用默认档
        scale: 预处理缩放，只能是 SUPPORTED_SCALES 之一
        max_corrections: 允许的最大纠正位数
        resolver: 候选裁决器（预留的时序扩展点）
    """

    def __init__(
        self,
        engine,
        profile=None,
        scale: float = DEFAULT_SCALE,
        max_corrections: int = MAX_CORRECTIONS,
        resolver: Optional[CandidateResolver] = None,
    ):
        if float(scale) not in SUPPORTED_SCALES:
            raise ValueError(
                f"不支持的缩放 {scale!r}，可选: {SUPPORTED_SCALES}"
            )

        self._engine = engine
        self._profile: GameProfile = (
            profile if isinstance(profile, GameProfile) else get_profile(profile)
        )
        self._scale = float(scale)
        self._max_corrections = int(max_corrections)
        self._resolver = resolver or SingleFrameResolver()
        #: 最近一次 recognize_frame() 的选错游戏判定结果（None = 没冲突）
        self._last_conflict: Optional[GameSelectionConflict] = None

        logger.info(
            "房间号识别器: 游戏档=%s, 主格式=%s, 缩放=%.1fx, 最大纠正=%d",
            self._profile.profile_id,
            self._profile.primary_format.describe(),
            self._scale,
            self._max_corrections,
        )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def engine(self):
        return self._engine

    @property
    def profile(self) -> GameProfile:
        return self._profile

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def resolver(self) -> CandidateResolver:
        return self._resolver

    @property
    def last_conflict(self) -> Optional["GameSelectionConflict"]:
        """最近一次 recognize_frame() 判定出的「选错游戏」证据；None 表示没冲突。

        上层据此提醒用户，但不得据此切换游戏（见产品规则：绝不自动选择）。
        """
        return self._last_conflict

    # ------------------------------------------------------------------
    # 轻量预处理
    # ------------------------------------------------------------------
    def prepare(self, image: np.ndarray) -> np.ndarray:
        """
        轻量预处理：只在 scale != 1.0 时做一次立方插值缩放。

        刻意不做灰度/二值化/降噪——既有实现已经验证过
        「预处理会把 I 认成 1」，本迭代不改变这条结论。
        """
        if image is None or image.size == 0:
            return image
        if self._scale == 1.0:
            return image  # 恒等，不复制

        height, width = image.shape[:2]
        new_size = (max(1, int(round(width * self._scale))),
                    max(1, int(round(height * self._scale))))
        return cv2.resize(image, new_size, interpolation=cv2.INTER_CUBIC)

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------
    def recognize_frame(self, image: np.ndarray) -> Optional[RoomCodeCandidate]:
        """
        识别一帧 ROI 图像。异常与引擎保持一致（OCREngineError 向上抛）。

        一帧 = 一次裁决器调用：无论内部看了几个 OCR 读法，
        都只把最终结果交给裁决器一次，时序观测才不会被复读污染。
        """
        prepared = self.prepare(image)

        # 记录本帧实际看过的读法文本，用于「选错游戏」判定（不额外跑 OCR）
        seen: List[str] = []
        best = self._decide(prepared, seen)

        # 读法里有小写字形（本该大写的位）说明那几位的字形没看清，
        # 放大一倍复看一次；只有复看结果与当前读法「除可疑位外逐位一致」
        # 才会被采纳（见 _escalate）。
        if best is not None and best.case_fold_count:
            escalated = self._escalate(prepared, best)
            if escalated is not None:
                best = escalated

        # 所选游戏与识别内容明显冲突时【不放行】这一帧：
        # 结果不可信，既不进时序裁决（避免污染窗口/成立错误房间号），
        # 也就永远不会触发剪贴板。判定依据见 _detect_conflict()。
        self._last_conflict = self._detect_conflict(seen)
        if self._last_conflict is not None:
            # debug 级：识别间隔 300ms，冲突会持续到用户重新选择为止，
            # 用 warning 会把日志刷爆。面向用户的提醒由上层去重后发一次。
            logger.debug(
                "[ROOMCODE] 所选游戏与识别内容冲突，本帧结果不予采用: %s",
                self._last_conflict.describe(),
            )
            return self._resolver.offer(None)

        return self._resolver.offer(best)

    def _decide(self,
                image: np.ndarray,
                seen: Optional[List[str]] = None) -> Optional[RoomCodeCandidate]:
        """
        一帧图像 -> 候选或 None：逐个读法算候选，够好就停。

        「够好」= 零纠正且零大小写折叠。排序键（纠正数、折叠数）都是非负的，
        所以零纠正零折叠的读法已经是它能拿到的最优解，后面的 PSM 不可能更好，
        没必要再花一次 OCR（实测一次 OCR 调用约 200ms，而识别间隔只有 300ms）。
        代价：两个读法都是零纠正零折叠却给出不同房间号时，取先出现的那个——
        与改动前「取第一个非空读法」的行为一致，不会比原来更糟。

        参数：
            seen: 传入一个列表时，把本帧消费过的读法文本追加进去，
                  供「选错游戏」判定复用（因此判定不需要再跑一次 OCR）。
        """
        candidates: List[RoomCodeCandidate] = []
        for text in self._iter_reading_texts(image):
            if seen is not None:
                seen.append(text)
            candidate = self._resolve_text(text)
            if candidate is None:
                continue
            candidates.append(candidate)
            if not candidate.correction_count and not candidate.case_fold_count:
                break
        return self._best_of(candidates)

    # ------------------------------------------------------------------
    # 选错游戏判定（只判定、不建议、不切换）
    # ------------------------------------------------------------------
    def _detect_conflict(
        self, texts: Sequence[str]
    ) -> Optional["GameSelectionConflict"]:
        """
        判断这一帧的读法是否【明显】说明用户选错了游戏。

        判据（必须同时成立，宁可不提醒也不误提醒）：
            1. 本帧没有任何【长度恰好等于本档格式长度、且严格校验通过】的整段
               —— 也就是本档的码形状根本没出现；
            2. 但存在另一款具体游戏的格式，能在这帧里找到同样「长度恰好匹配
               且严格校验通过」的整段 —— 那款游戏的码形状明确出现了。

        只做长度匹配 + 严格校验的整段判断，不做滑窗、不做拼段、不做模糊匹配，
        也不需要知道用户「应该」选哪款游戏——判定结果只用于提醒，不用于切换。
        """
        other = None
        for text in texts:
            normalized = normalize_text(text)
            if not normalized:
                continue
            if self._has_exact_code(normalized, self._profile.formats):
                # 本档的码形状明确出现了：用户没选错（另一款游戏的形状即使
                # 同时出现也不该提醒——那可能是 HUD 上的其它文本）
                return None
            if other is None:
                other = self._other_game_with_exact_code(normalized)
        if other is None:
            return None
        return GameSelectionConflict(self._profile.profile_id, other)

    def _has_exact_code(
        self, text: str, formats: Sequence[CodeFormat]
    ) -> bool:
        """文本里是否存在【长度恰好匹配某个格式、且严格校验通过】的整段。"""
        runs = extract_runs(text)
        for fmt in formats:
            for run in runs:
                if len(run) != fmt.length:
                    continue
                if resolve_candidate(run, fmt, self._max_corrections) is not None:
                    return True
        return False

    def _other_game_with_exact_code(self, text: str) -> Optional[str]:
        """哪一款【其它】具体游戏能在这段文本里取到长度恰好匹配的合法码。"""
        for profile in game_profiles():
            if profile.profile_id == self._profile.profile_id:
                continue
            if self._has_exact_code(text, profile.formats):
                return profile.profile_id
        return None

    def _iter_reading_texts(self, image: np.ndarray):
        """按 PSM 顺序产出该帧的 OCR 读法文本（引擎不支持时退回单读法）。"""
        iter_readings = getattr(self._engine, "iter_readings", None)
        if iter_readings is not None:
            for _, text in iter_readings(image, preprocess=True):
                yield text
            return

        # 引擎没有多读法入口（例如测试桩）时保持既有单读法行为
        text = self._engine.recognize(image, preprocess=True)
        if text:
            yield text

    def _best_of(
        self, candidates: Sequence[RoomCodeCandidate]
    ) -> Optional[RoomCodeCandidate]:
        """
        在【多个已校验候选】之间挑最可信的一个，或 None。

        排序键：纠正次数 -> 大小写折叠位数 -> 出现顺序（保证确定性）。
        两个候选代价完全相同却给出不同房间号 = 无法分辨 -> 不赌，返回 None
        （沿用 _match_format 里既有的「宁可不返回」策略）。
        """
        if not candidates:
            return None

        def _key(candidate: RoomCodeCandidate) -> Tuple[int, int]:
            return (candidate.correction_count, candidate.case_fold_count)

        ordered = sorted(candidates, key=lambda c: _key(c))
        best = ordered[0]
        for other in ordered[1:]:
            if _key(other) != _key(best):
                break
            if other.code != best.code:
                logger.debug(
                    "[ROOMCODE] 读法歧义，放弃: %r vs %r",
                    best.code, other.code,
                )
                return None

        return best

    def _escalate(
        self, image: np.ndarray, base: RoomCodeCandidate
    ) -> Optional[RoomCodeCandidate]:
        """
        放大一倍重读，尝试解掉 base 里那些「本该大写却是小写」的可疑位。

        采纳条件（两个都必须满足）：
            - 复看结果自己不带可疑位（否则等于没看清）
            - 复看结果与 base 在【非可疑位】上逐位相同
        第二条是保险：放大是为了看清字形，不是为了换个房间号，
        所以它只能改动那几位可疑字符，不能凭空给出另一个码。
        """
        if image is None or image.size == 0:
            return None

        height, width = image.shape[:2]
        bigger = cv2.resize(
            image,
            (max(1, int(round(width * _ESCALATION_SCALE))),
             max(1, int(round(height * _ESCALATION_SCALE)))),
            interpolation=cv2.INTER_CUBIC,
        )

        escalated = self._decide(bigger)
        if escalated is None or escalated.case_fold_count:
            return None
        if len(escalated.code) != len(base.code):
            return None

        source_text = base.source_text
        if len(source_text) != len(escalated.code):
            return None
        uncertain = {i for i, ch in enumerate(source_text)
                     if ch.islower() and source_text[i].upper() == base.code[i]}
        for index, (base_char, new_char) in enumerate(zip(base.code, escalated.code)):
            if index in uncertain:
                continue
            if base_char != new_char:
                return None

        logger.info(
            "[ROOMCODE] 放大复看 %.1fx: %s -> %s",
            _ESCALATION_SCALE, base.code, escalated.code,
        )
        # 记录真实读法倍率，避免调试时误以为它来自 1.0x 的那次读取
        return replace(escalated, scale=self._scale * _ESCALATION_SCALE)

    def recognize_text(self, raw_text: Optional[str]) -> Optional[RoomCodeCandidate]:
        """
        识别一段 OCR 文本（不涉及图像，便于测试）。

        按 profile.formats 的顺序尝试各格式；任一格式给出唯一最佳候选即返回。
        这是【单读法】入口：计算一次，并交给裁决器一次。
        """
        return self._resolver.offer(self._resolve_text(raw_text))

    def _resolve_text(self, raw_text: Optional[str]) -> Optional[RoomCodeCandidate]:
        """一段 OCR 文本 -> 严格校验通过的候选或 None（纯计算，不经裁决器）。"""
        original = raw_text or ""
        text = normalize_text(original)
        if not text:
            return None

        matches = []
        for fmt in self._profile.formats:
            found = self._match_format(text, fmt, original)
            if found is not None:
                matches.append(found)

        if not matches:
            return None

        # 排序：来源质量 -> 纠正次数 -> 更长的读法优先。
        # 最后一项只在多格式时起作用：OCR 常把房间号与相邻文字粘成一串
        # （实测 "ABC1234 other" 会变成 'ABC1234other'），此时 7 位读法
        # 比 6 位截断读法丢弃的已识别字符更少，假设也更少。
        def _rank(match: "_FormatMatch") -> Tuple[int, int, int]:
            return (
                match.quality,
                match.candidate.correction_count,
                len(match.candidate.code),
            )

        matches.sort(key=lambda m: (_rank(m)[0], _rank(m)[1], -_rank(m)[2]))
        best = matches[0]
        best_rank = _rank(best)

        # 跨格式歧义：代价完全相同（含长度）却给出不同房间号 -> 无法分辨，不赌
        for other in matches[1:]:
            if _rank(other) != best_rank:
                break
            if other.candidate.code != best.candidate.code:
                logger.debug(
                    "[ROOMCODE] 跨格式歧义，放弃: %r vs %r (文本=%r)",
                    best.candidate.code, other.candidate.code, text,
                )
                return None

        return best.candidate

    # ------------------------------------------------------------------
    # 内部：候选提取 + 纠正 + 严格校验
    # ------------------------------------------------------------------
    def _match_format(
        self, text: str, fmt: CodeFormat, original: str
    ) -> Optional[_FormatMatch]:
        runs = extract_runs(text)

        # 一、单个连续段：ROI 恰好框住房间号（绝大多数情况）
        singles: List[Tuple[ResolvedCandidate, int, int, int]] = []
        for run_index, run in enumerate(runs):
            if len(run) < fmt.length:
                continue
            quality = _QUALITY_EXACT if len(run) == fmt.length else _QUALITY_WINDOW
            for offset in _window_offsets(len(run), fmt.length):
                resolved = resolve_candidate(
                    run[offset:offset + fmt.length], fmt, self._max_corrections
                )
                if resolved is not None:
                    singles.append((resolved, quality, run_index, offset))

        # 二、被分隔符切开的段重新拼回（OCR 常在房间号中间插入空格）
        #     只在单个段完全无解时才尝试，且要求拼回后长度精确相等。
        joins: List[Tuple[ResolvedCandidate, int, int, int]] = []
        if not singles:
            for start in range(len(runs)):
                for count in range(2, _MAX_JOIN_PARTS + 1):
                    group = runs[start:start + count]
                    if len(group) < count:
                        break
                    merged = "".join(group)
                    if len(merged) != fmt.length:
                        continue
                    resolved = resolve_candidate(merged, fmt, self._max_corrections)
                    if resolved is not None:
                        joins.append((resolved, _QUALITY_JOINED, start, 0))

        pool = singles or joins
        if not pool:
            logger.debug("[ROOMCODE] 无有效候选 (格式=%s, 文本=%r)", fmt.name, text)
            return None

        # 先看来源质量，再看纠正次数；最后按出现位置定型（保证确定性）
        pool.sort(key=lambda item: (item[1], item[0].correction_count, item[2], item[3]))
        best_resolved, quality, _, _ = pool[0]

        # 同样代价下出现两个不同的房间号 = 无法分辨 -> 宁可不返回，
        # 也不赌其中一个（这是「prefer no result over a wrong result」）。
        for other_resolved, other_quality, _, _ in pool[1:]:
            if (other_quality, other_resolved.correction_count) != (
                quality,
                best_resolved.correction_count,
            ):
                break
            if other_resolved.code != best_resolved.code:
                logger.debug(
                    "[ROOMCODE] 候选歧义，放弃: %r vs %r (文本=%r)",
                    best_resolved.code, other_resolved.code, text,
                )
                return None

        candidate = RoomCodeCandidate(
            code=best_resolved.code,
            profile_id=self._profile.profile_id,
            format_name=fmt.name,
            raw_text=original,
            source_text=best_resolved.source_text,
            corrections=best_resolved.corrections,
            scale=self._scale,
            case_fold_count=best_resolved.case_fold_count,
        )
        if candidate.corrected:
            logger.info(
                "[ROOMCODE] %s <- %r (纠正: %s)",
                candidate.code, candidate.source_text, candidate.correction_summary(),
            )
        return _FormatMatch(candidate, quality)
