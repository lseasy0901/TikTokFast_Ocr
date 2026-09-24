# -*- coding: utf-8 -*-
"""
播放启动分段计时 (Phase 3.2)

一次「连接 → 首帧」的耗时分解，横跨 stream 与 gui 两层：

    candidate_selected                    （MainWindow 决定试这个候选）
      → ffprobe_start / ffprobe_end       （FFmpegReader 构造器；仅未探测候选才有）
      → ffmpeg_spawn                      （Popen 返回）
      → first_decoded_frame               （首个完整帧从 rawvideo 管道读出）
      → first_display                     （GUI 线程首次取到该帧）

约束（与 Phase 3.2 的硬性要求一致）：
    - 只记录，不改变任何播放行为；
    - 每个阶段只标记一次；一次尝试结束时输出 **1 条** 日志，不逐帧、不周期性输出；
    - 无法可靠测量的阶段显式标为 unavailable，不用近似值或推断值填充；
    - 不引入线程 / 全局状态：计时对象由一次尝试的持有者（MainWindow）创建，
      按需传给 FFmpegReader，尝试结束后不再被引用。
    - 时钟只有一种：``time.perf_counter()``，所有耗时都是相对 t0 的差值。

日志里每个阶段有四种取值，互相不可混淆：

    ``12.3ms``            已标记
    ``skipped(原因)``     本次尝试不存在这个阶段（例如分辨率已知 → 不跑 ffprobe）
    ``unavailable(原因)`` 阶段存在，但当前架构下无法可靠测量
    ``未到达``             尝试在到达该阶段之前就结束了（失败 / 取消）

已知无法测量的阶段见 :data:`STAGE_FIRST_INPUT`。
"""

import logging
import threading
import time

logger = logging.getLogger("DouyinLowLatencyViewer.utils.startup_timing")

__all__ = [
    "StartupTiming",
    "STAGE_CANDIDATE_SELECTED",
    "STAGE_FFPROBE_START",
    "STAGE_FFPROBE_END",
    "STAGE_FFMPEG_SPAWN",
    "STAGE_FIRST_FRAME",
    "STAGE_FIRST_DISPLAY",
    "STAGE_FIRST_INPUT",
]

# ---- 阶段名（顺序即链路顺序）----
STAGE_CANDIDATE_SELECTED = "candidate_selected"
STAGE_FFPROBE_START = "ffprobe_start"
STAGE_FFPROBE_END = "ffprobe_end"
STAGE_FFMPEG_SPAWN = "ffmpeg_spawn"
STAGE_FIRST_FRAME = "first_decoded_frame"
STAGE_FIRST_DISPLAY = "first_display"
#: FFmpeg 真正收到首个输入数据（连接建立 / FLV 头到达）的时刻。
#: 当前架构下**无法可靠测量**：生产参数带 ``-loglevel warning``，FFmpeg 不输出
#: "Input #0"/"Stream #0:0" 等启动信息，stderr 里读不到该事件；改 loglevel 属于
#: 改动行为与日志量，超出本阶段范围。保留该阶段名是为了让日志明确说"测不到"，
#: 而不是留空让人误以为已测量。
STAGE_FIRST_INPUT = "first_input_event"

_STAGE_ORDER = (
    STAGE_CANDIDATE_SELECTED,
    STAGE_FFPROBE_START,
    STAGE_FFPROBE_END,
    STAGE_FFMPEG_SPAWN,
    STAGE_FIRST_INPUT,
    STAGE_FIRST_FRAME,
    STAGE_FIRST_DISPLAY,
)

_FIRST_INPUT_REASON = "-loglevel warning 下 FFmpeg 不输出启动信息"

#: 汇总日志里的分段： (段名, 起点候选阶段, 终点阶段)。
#: 起点取"第一个已标记的候选"——这样 ffprobe 被跳过时，
#: spawn 段自动从 candidate_selected 起算，不会出现空洞。
_SEGMENTS = (
    ("probe", (STAGE_FFPROBE_START,), STAGE_FFPROBE_END),
    ("spawn", (STAGE_FFPROBE_END, STAGE_CANDIDATE_SELECTED), STAGE_FFMPEG_SPAWN),
    ("first_frame", (STAGE_FFMPEG_SPAWN,), STAGE_FIRST_FRAME),
    ("first_display", (STAGE_FIRST_FRAME,), STAGE_FIRST_DISPLAY),
)

#: 未标记阶段的文案
_TEXT_UNREACHED = "未到达"


class StartupTiming:
    """一次播放尝试的启动分段计时。

    线程安全：标记可能来自 FFmpeg reader 线程，汇总日志来自 GUI 线程。

    典型用法::

        timing = StartupTiming()
        timing.note("attempt=1 proto=flv")
        timing.mark(STAGE_CANDIDATE_SELECTED)
        ...
        timing.log()        # 幂等：一次尝试最多输出一条
    """

    def __init__(self):
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self._marks: dict[str, float] = {}            # 阶段 → 相对 t0 的秒数
        self._states: dict[str, tuple[str, str]] = {}  # 阶段 → (状态, 原因)
        self._notes: list[str] = []
        self._logged = False
        # 该阶段的存在本身就是"无法测量"，在创建时一次性声明，
        # 不散落在各调用点，也不伪造近似值。
        self._states[STAGE_FIRST_INPUT] = ("unavailable", _FIRST_INPUT_REASON)

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def mark(self, stage: str) -> None:
        """标记一个阶段（每个阶段只记第一次，重复调用不覆盖）"""
        with self._lock:
            self._marks.setdefault(stage, time.perf_counter() - self._t0)

    def skip(self, stage: str, reason: str) -> None:
        """标注"本次尝试不存在该阶段"（已有标记时不生效）"""
        self._set_state(stage, "skipped", reason)

    def unavailable(self, stage: str, reason: str) -> None:
        """标注"该阶段无法可靠测量"（已有标记时不生效）"""
        self._set_state(stage, "unavailable", reason)

    def note(self, text: str) -> None:
        """追加一条上下文（不是时间点，例如 attempt / 协议 / 分辨率）"""
        with self._lock:
            if text and text not in self._notes:
                self._notes.append(text)

    def _set_state(self, stage: str, state: str, reason: str) -> None:
        with self._lock:
            if stage in self._marks:
                return
            self._states.setdefault(stage, (state, reason))

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """结构化快照：{"marks": {阶段: ms}, "states": {阶段: (状态, 原因)}, ...}"""
        with self._lock:
            return {
                "notes": list(self._notes),
                "marks": {s: round(v * 1000.0, 1) for s, v in self._marks.items()},
                "states": dict(self._states),
                "segments": {name: ms for name, (_, ms) in self._segments_locked()},
                # total 是带"到哪个阶段为止"的文本，不是纯数值
                "total": self._total_locked(),
            }

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def log(self) -> str | None:
        """输出唯一的启动汇总日志；已经输出过则什么都不做。

        :return: 本次实际输出的正文；重复调用返回 None
        """
        with self._lock:
            if self._logged:
                return None
            self._logged = True
            body = self._format_locked()
        logger.info("[STARTUP] %s", body)
        return body

    # ------------------------------------------------------------------
    # 内部（调用方必须已持有锁）
    # ------------------------------------------------------------------
    def _stage_text_locked(self, stage: str) -> str:
        mark = self._marks.get(stage)
        if mark is not None:
            return f"{mark * 1000.0:.1f}ms"
        state = self._states.get(stage)
        if state is not None:
            return f"{state[0]}({state[1]})"
        return _TEXT_UNREACHED

    def _segments_locked(self):
        """→ [(段名, (文本, 毫秒或 None))]"""
        result = []
        for name, starts, end in _SEGMENTS:
            start = next((s for s in starts if s in self._marks), None)
            end_mark = self._marks.get(end)
            if start is not None and end_mark is not None:
                ms = (end_mark - self._marks[start]) * 1000.0
                result.append((name, (f"{ms:.1f}ms", ms)))
            else:
                # 段的终点没到（skipped/unavailable/未到达）：如实透出状态，
                # 不用 0 或估算值填充
                result.append((name, (self._stage_text_locked(end), None)))
        return result

    def _total_locked(self):
        """最后到达的阶段相对 t0 的耗时 + 该阶段名"""
        for stage in reversed(_STAGE_ORDER):
            mark = self._marks.get(stage)
            if mark is not None:
                return f"{mark * 1000.0:.1f}ms (至 {stage})"
        return _TEXT_UNREACHED

    def _format_locked(self) -> str:
        parts = []
        if self._notes:
            parts.append(" ".join(self._notes))
        parts.append(" ".join(f"{name}={text}"
                              for name, (text, _) in self._segments_locked()))
        parts.append(f"total={self._total_locked()}")
        parts.append(f"{STAGE_FIRST_INPUT}={self._stage_text_locked(STAGE_FIRST_INPUT)}")
        return " | ".join(parts)
