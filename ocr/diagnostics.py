# -*- coding: utf-8 -*-
"""
OCR 诊断日志（默认关闭）

目的：把**一次 OCR 执行**在各阶段已经存在的数据旁路记录下来，供离线分析
「为什么这一帧读错了 / 没读出来」。它只旁观，不参与判定：

    - 不改任何识别、纠正、PSM 仲裁、时序确认规则。本模块不认识那些规则，
      只接收识别器/裁决器**已经产出的**结果，因此不可能与它们不一致。
    - 默认关闭。关闭时 begin() 直接返回 None，不建记录、不占内存、不写文件。
    - 记录只活在内存里，导出必须由用户显式触发（诊断 CSV 与"最终复制记录"
      的 CSV 是两份独立文件，互不影响）。

各列的数据来源（取不到的字段留空，绝不伪造）：

    序号        本次运行内 OCR 执行的序号，从 1 开始（按执行先后）
    时间        执行开始的本地时间 HH:MM:SS.mmm
    游戏档      当前选择的游戏档（profile_id）
    格式        该档配置的格式（名字 + 位模式），即"当前游戏配置"
    耗时ms      本帧 recognize_frame() 的用时（不含取帧/裁剪）
    PSM读法     各 PSM 实际返回的原始文本，形如 `6:HJKT6402 ‖ 7:HJKT6462`。
                只在引擎提供多读法接口（iter_readings）时才有值；
                提前停在第 N 个 PSM 时，后面几个 PSM 根本没跑，故不出现。
    各读法候选  每个读法经「候选提取 → 逐位纠正 → 严格校验」后的候选码，
                用与上一列**完全相同**的标签（同一 PSM 编号），因此两列可以
                逐个对上；该读法没有合法候选时记 `-`。
                括号里是纠正/大小写折叠情况，便于看清这个候选是怎么来的。
    仲裁结果    这一帧最终采用的结论：`选定:X` / `无候选` / `读法歧义` /
                `放弃:游戏冲突`；放大复看改写了结果时记 `选定:X(放大复看)`。
    连续确认    候选交给裁决器后的结论：成立 / 重复成立 / 确认中 / 未成立。
                直接读裁决器的公开只读状态得到，本模块不重新推导一遍规则。
    确认窗口    裁决器观测窗口的快照（`-` 表示那一帧没读出结果）。它是**证据**，
                不是判定——是否成立只看上一列。
    复制请求    本帧请求复制到剪贴板的房间码（未请求则空）
    复制结果    成功 / 跳过:与上次相同（剪贴板去重过滤，未写入）/
                失败:<原因>；未回填（还没轮到/被中断）时为**空**
    备注        放大复看、提前停止、游戏冲突、异常等附加事实

线程与开销：
    - 热路径（识别线程）只做「取值 + 追加到当前执行」；所有格式化都推迟到
      导出时。关闭时热路径上只剩一次 None/布尔判断。
    - 每帧记录只在上报（finish）时进入列表；列表与复制结果回填都由锁保护，
      因为复制结果来自 GUI 线程。
"""

import csv
import io
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.diagnostics")

__all__ = [
    "CSV_HEADER",
    "DEFAULT_MAX_RECORDS",
    "ExecutionTrace",
    "OCRDiagnostics",
    "ReadingNote",
    "format_execution_time",
]

#: 内存里保留的诊断记录上限（约 1KB/条，30000 条 ≈ 30MB）。
#:
#: 取值依据是实测的两条曲线：gen-2 GC 停顿与保活对象数成正比（60000 条 22ms、
#: 120000 条 43ms），内存按 ~1KB/条线性增长。按每帧约 300ms、平均一帧一条算，
#: 30000 条覆盖约 2.5 小时连续记录 —— 对"刚出问题的那一段"够用，同时把
#: GC 停顿和内存都钉在可接受的范围里。溢出丢最早的记录（见
#: OCRDiagnostics._drop_overflow_locked），丢弃条数会明确报出来。
DEFAULT_MAX_RECORDS = 30000

#: 诊断 CSV 的列序。与「最终复制记录」CSV 完全独立，两者不共用文件。
CSV_HEADER: Sequence[str] = (
    "序号", "时间", "游戏档", "格式", "耗时ms",
    "PSM读法", "各读法候选", "仲裁结果", "连续确认", "确认窗口",
    "复制请求", "复制结果", "备注",
)

#: 读法与候选之间的分隔符。刻意不用 '|'：横线被读成 '|' 是这台引擎上常见的
#: 误读形态，用它当分隔符会让读法本身和分隔符分不清。
_SEPARATOR = " ‖ "

#: 确认窗口里「这一帧没读出结果」的占位（TemporalResolver 用 None 表示）。
_EMPTY_OBSERVATION = "-"

#: 写 CSV 用 UTF-8 + BOM（Windows Excel 认中文表头），行尾 CRLF。
CSV_ENCODING = "utf-8-sig"
CSV_LINE_TERMINATOR = "\r\n"

#: 复制结果的三种取值（空字符串 = 尚未回填）
COPY_SUCCESS = "成功"
COPY_SKIPPED = "跳过:与上次相同"
COPY_FAILED_PREFIX = "失败:"

#: 「不属于任何一次识别」的事件写在备注列时的前缀。
#:
#: 这类事件（worker 级异常、会话结束时的丢帧统计）发生时本线程没有正在写的
#: ExecutionTrace，因此单独成一条只带时间与备注的记录行——好让诊断 CSV 自带
#: 完整时间线，又**不改变既有 13 列结构**（列结构一动，既有解析方与测试全要改）。
WORKER_ERROR_PREFIX = "worker异常:"
FRAME_DROP_PREFIX = "丢帧:"


def format_execution_time(moment: datetime) -> str:
    """执行时间：``HH:MM:SS.mmm``（毫秒，与复制记录 CSV 的写法一致）。"""
    return f"{moment.strftime('%H:%M:%S')}.{moment.microsecond // 1000:03d}"


@dataclass(frozen=True)
class ReadingNote:
    """某一个 PSM 读法，以及它经校验/纠正后的候选（候选可能没有）。"""

    psm: Optional[int]
    raw_text: str
    phase: str = ""              # "" = 主读；"放大" = 放大复看那一轮
    candidate_code: str = ""     # 空 = 该读法没有通过严格校验的候选
    detail: str = ""             # 纠正/折叠情况（由候选推导，不重新计算）

    @property
    def label(self) -> str:
        """这一读法的标签：``[阶段]PSM``（拿不到 PSM 编号时是 ``-``）。

        读法列与候选列用**同一个**标签，两列才能一一对上——
        分析时看的就是「PSM 6 读出来的是什么、它算出的候选又是什么」。
        """
        psm = _EMPTY_OBSERVATION if self.psm is None else str(self.psm)
        return f"{self.phase}{psm}"

    def render_reading(self) -> str:
        return f"{self.label}:{self.raw_text}"

    def render_candidate(self) -> str:
        box = f"({self.detail})" if self.detail else ""
        return f"{self.label}:{self.candidate_code or _EMPTY_OBSERVATION}{box}"


@dataclass
class ExecutionTrace:
    """一次 OCR 执行的全部诊断字段（可写：复制结果在 GUI 线程回填）。"""

    index: int
    started_at: datetime
    profile_id: str = ""
    formats: str = ""
    elapsed_ms: Optional[float] = None
    readings: List[ReadingNote] = field(default_factory=list)
    arbitration: str = ""
    confirmation: str = ""
    window: str = ""
    copy_requested: str = ""
    copy_result: str = ""
    notes: List[str] = field(default_factory=list)
    #: 复制结果是否已回填（一个复制请求只对应一条记录）
    copy_resolved: bool = False

    @property
    def time_text(self) -> str:
        return format_execution_time(self.started_at)

    def add_note(self, text: str) -> None:
        if text:
            self.notes.append(text)

    def row(self) -> tuple:
        """CSV 一行（列序与 :data:`CSV_HEADER` 一致）。"""
        return (
            self.index,
            self.time_text,
            self.profile_id,
            self.formats,
            "" if self.elapsed_ms is None else f"{self.elapsed_ms:.1f}",
            _SEPARATOR.join(note.render_reading() for note in self.readings),
            _SEPARATOR.join(note.render_candidate() for note in self.readings),
            self.arbitration,
            self.confirmation,
            self.window,
            self.copy_requested,
            self.copy_result,
            "; ".join(self.notes),
        )


class OCRDiagnostics:
    """诊断记录器：默认关闭，开启后按执行顺序追加内存记录。

    关闭时所有热路径方法都是廉价的空操作（不建对象、不占内存、不写文件）。

    记录量有上限（默认 :data:`DEFAULT_MAX_RECORDS`），超过后丢**最早**的记录。
    原先是不设上限的（"丢事件比占内存更糟"），实测下来这个取舍站不住：
    Python 的 gen-2 GC 停顿与保活对象数成正比（60000 条时单次回收 22ms、
    120000 条时 43ms —— 每帧新增 6 个被长期保活的容器对象），内存也按
    约 1KB/条线性增长。上限把这两样都钉住；丢了多少条如实记在
    :attr:`dropped` 里，导出时会告诉用户，不静默丢数据。
    """

    def __init__(self, enabled: bool = False,
                 max_records: Optional[int] = DEFAULT_MAX_RECORDS):
        self._enabled = bool(enabled)
        self._lock = threading.RLock()
        self._records: List[ExecutionTrace] = []
        self._next_index = 1
        #: 每个线程「正在写」的那一次执行。识别只在一个工作线程里跑，
        #: 用 thread-local 就不必为热路径加锁，也不会串到别的线程。
        self._local = threading.local()
        #: 房间码 -> 等待回填复制结果的执行（按请求先后排队）。
        #: 有了它，回填是一次 O(1) 出队，而不是从表头扫到匹配为止 ——
        #: 后者在几万条记录时会把 OCR 热路径挡在锁外面（实测 40000 条 0.9ms
        #: 且随记录数线性增长）。
        self._pending_copies: Dict[str, Deque[ExecutionTrace]] = {}
        self._max_records = None if max_records is None else int(max_records)
        self._dropped = 0

    # ------------------------------------------------------------------
    # 开关 / 只读视图
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """开启 / 关闭诊断。关闭只影响**之后**的执行，已有记录一律保留。"""
        self._enabled = bool(enabled)
        logger.info("OCR 诊断日志已%s", "开启" if self._enabled else "关闭")

    @property
    def records(self) -> Tuple[ExecutionTrace, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def max_records(self) -> Optional[int]:
        """保留上限；None = 不设上限。"""
        return self._max_records

    @property
    def dropped(self) -> int:
        """因超过上限而被丢弃的最早记录条数（0 = 一条都没丢）。"""
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._pending_copies.clear()
            self._dropped = 0
            self._next_index = 1

    def __len__(self) -> int:
        return self.count

    def __bool__(self) -> bool:
        return self.count > 0

    # ------------------------------------------------------------------
    # 热路径：识别线程
    # ------------------------------------------------------------------
    def begin(self, profile_id: str = "", formats: str = "") -> Optional[ExecutionTrace]:
        """开始一次执行。诊断关闭时返回 None（调用方无需分支，各 note 都容忍 None）。"""
        if not self._enabled:
            return None
        with self._lock:
            index = self._next_index
            self._next_index += 1
        trace = ExecutionTrace(
            index=index, started_at=datetime.now(),
            profile_id=profile_id, formats=formats,
        )
        self._local.trace = trace
        return trace

    def current(self) -> Optional[ExecutionTrace]:
        """本线程正在写的执行（识别器内部用；没有则 None）。"""
        return getattr(self._local, "trace", None)

    def note_reading(self, psm, raw_text, candidate, phase: str = "") -> None:
        """记录一个 PSM 读法及其校验结果。由识别器在**已经算出**候选后调用。"""
        if not self._enabled:
            return
        trace = self.current()
        if trace is None:
            return
        trace.readings.append(ReadingNote(
            psm=psm,
            raw_text="" if raw_text is None else str(raw_text),
            phase=phase,
            candidate_code="" if candidate is None else candidate.code,
            detail="" if candidate is None else _describe_candidate(candidate),
        ))

    def note_decided(self, text: str) -> None:
        """本帧的仲裁结论（由识别器在既有判定完成后写入，不做二次判断）。"""
        if not self._enabled:
            return
        trace = self.current()
        if trace is not None:
            trace.arbitration = text

    def add_note(self, text: str) -> None:
        """给本线程这次执行追加一条附加事实（放大复看、提前停止等）。"""
        if not self._enabled:
            return
        trace = self.current()
        if trace is not None:
            trace.add_note(text)

    # ------------------------------------------------------------------
    # 会话级事件（不属于任何一次识别）
    # ------------------------------------------------------------------
    def note_worker_error(self, text: str) -> None:
        """记录一条 worker 级异常，写进诊断 CSV 的备注列。

        引擎初始化失败、主循环兜底异常都属于这一类。它们**只增加记录**：
        调用方原本的 logger 与 error_occurred 信号保持不变，异常处理流程不变。
        """
        self._record_event(f"{WORKER_ERROR_PREFIX}{text}")

    def note_frame_drops(self, dropped: int, total: int) -> None:
        """记录本会话最终的丢帧统计（frame_buffer 单槽覆盖导致）。

        写进备注列，形如 ``丢帧:1234/5678（21.7%）``。``total`` 为 0 时比值写作
        0.0%，不产生除零。
        """
        dropped = int(dropped)
        total = int(total)
        rate = (100.0 * dropped / total) if total > 0 else 0.0
        self._record_event(
            f"{FRAME_DROP_PREFIX}{dropped}/{total}（{rate:.1f}%）"
        )

    def _record_event(self, text: str) -> None:
        """把一条「会话级事件」落成记录行。

        有正在写的执行时挂到它的备注上；没有时就地为这条事件建一条
        只带时间与备注的记录——既不改列结构，也不丢事件。
        诊断关闭（或本线程没有记录器）时是廉价空操作。
        """
        if not self._enabled:
            return
        trace = self.current()
        if trace is not None:
            trace.add_note(text)
            return
        trace = self.begin()
        if trace is None:
            return
        trace.add_note(text)
        self.finish(trace)

    # ------------------------------------------------------------------
    # 识别线程：收尾与阶段结论
    # ------------------------------------------------------------------
    def note_confirmation(self, trace: Optional[ExecutionTrace],
                          verdict: str, window: Optional[Sequence[Optional[str]]] = None) -> None:
        """记录连续确认结论与观测窗口快照（都由调用方从裁决器只读状态取得）。"""
        if trace is None:
            return
        trace.confirmation = verdict
        if window is not None:
            trace.window = "[" + ", ".join(
                _EMPTY_OBSERVATION if item is None else str(item) for item in window
            ) + "]"

    def note_copy_requested(self, trace: Optional[ExecutionTrace], code: str) -> None:
        """本帧请求复制到剪贴板的房间码。只表示**已请求**，不代表写成功。

        同时把这次执行挂进回填索引（按请求先后排队），供 GUI 线程 O(1) 认领。
        """
        if trace is None:
            return
        trace.copy_requested = code or ""
        if not code:
            return
        with self._lock:
            self._pending_copies.setdefault(code, deque()).append(trace)

    def finish(self, trace: Optional[ExecutionTrace],
               elapsed_ms: Optional[float] = None, note: str = "") -> None:
        """结束一次执行并入库。诊断关闭（trace 为 None）时什么都不做。"""
        if trace is None:
            return
        if elapsed_ms is not None:
            trace.elapsed_ms = float(elapsed_ms)
        trace.add_note(note)
        self._local.trace = None
        with self._lock:
            self._records.append(trace)
            self._drop_overflow_locked()

    def _drop_overflow_locked(self) -> None:
        """超过上限时丢【最早】的记录（调用方必须已持锁）。

        策略：诊断是给"刚刚出问题的那一段"用的，丢最早比丢最新更有用；
        丢了多少条累计在 :attr:`dropped`，导出时告知用户 —— 不静默丢数据。
        一次只丢溢出的部分，稳态下每帧最多 memmove 一次指针数组（微秒级）。
        """
        if self._max_records is None:
            return
        overflow = len(self._records) - self._max_records
        if overflow <= 0:
            return
        del self._records[:overflow]
        first_time = self._dropped == 0
        self._dropped += overflow
        if first_time:
            logger.warning(
                "[DIAG] 诊断记录已达上限 %d 条，此后丢弃最早的记录"
                "（丢弃条数会在导出时给出）", self._max_records)

    # ------------------------------------------------------------------
    # GUI 线程：复制结果回填
    # ------------------------------------------------------------------
    def note_copy_success(self, code: str) -> None:
        """房间码确实写进了系统剪贴板。"""
        self._resolve_copy(code, COPY_SUCCESS)

    def note_copy_skipped(self, code: str) -> None:
        """剪贴板没有写入：被 ClipboardManager 的「与上次相同」去重过滤掉了。"""
        self._resolve_copy(code, COPY_SKIPPED)

    def note_copy_failure(self, code: str, error: Optional[str] = None) -> None:
        """剪贴板写入失败。"""
        self._resolve_copy(code, f"{COPY_FAILED_PREFIX}{error or '未知错误'}")

    def _resolve_copy(self, code: str, result: str) -> None:
        """把复制结果回填给**最早一条**请求了该房间码、且尚未回填的执行。

        复制请求按发出顺序由 GUI 线程处理（Qt 队列连接保序），因此按先进先出
        配对，配的必然是产生该请求的那次执行 —— 索引里的队列就是这个顺序。

        用索引出队而不是从表头扫：扫描是 O(记录数) 且必须在锁内做，几万条时
        会把 OCR 热路径挡在锁外（实测 40000 条 0.9ms 且线性增长）。

        找不到匹配（例如诊断是中途才开启的）时**不新建记录**：凭空造一条
        没有识别上下文的"复制记录"只会让分析时误以为它来自某次 OCR 执行。
        """
        with self._lock:
            pending = self._pending_copies.get(code)
            if pending:
                trace = pending.popleft()
                if not pending:
                    del self._pending_copies[code]
                trace.copy_result = result
                trace.copy_resolved = True
                return
        logger.debug("[DIAG] 复制结果无对应执行，未回填: %s -> %s", code, result)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _snapshot(self) -> List[ExecutionTrace]:
        """锁内只做一次浅拷贝，把逐行渲染留到锁外。

        这是本模块最关键的一条：``begin()/note_*/finish()`` 与回填共用同一把
        锁，而渲染是 O(记录数) 的（每行要拼读法、候选、窗口等字符串）。
        原实现把整段渲染放在 ``with self._lock`` 里，于是**一次导出就会把
        OCR 热路径整段挡在外面** —— 实测 60021 条时 ``begin()`` 最长被阻塞
        489ms、30021 条时 221ms，正是"开启诊断后 OCR 明显卡顿"的来源。

        浅拷贝只需要复制指针数组，是整条路径里唯一必须持锁的一步（毫秒以下）。
        """
        with self._lock:
            return list(self._records)

    def to_csv_text(self) -> str:
        """渲染完整诊断 CSV（含表头）。落盘与预览走同一段代码。"""
        rows = [trace.row() for trace in self._snapshot()]
        return _write_csv(rows)

    def export_csv(self, path: Union[str, "os.PathLike"]) -> int:
        """把诊断记录写成 CSV，返回记录条数。导出不清空、不改动任何记录。

        导出期间 OCR 热路径不受影响：渲染与落盘都不持锁（见 :meth:`_snapshot`）。
        """
        rows = [trace.row() for trace in self._snapshot()]
        text = _write_csv(rows)
        # newline="" 由我们自己控制行尾，否则 Windows 上会把 \r\n 再翻一倍。
        with open(path, "w", encoding=CSV_ENCODING, newline="") as handle:
            handle.write(text)
        logger.info("[DIAG] 已导出 %d 条诊断记录到 %s", len(rows), path)
        return len(rows)


def _describe_candidate(candidate) -> str:
    """候选的来源说明：纠正了几位、哪几位；有没有大小写折叠。

    只读候选自己已有的字段，不重新计算任何东西。
    """
    parts = []
    if candidate.correction_count:
        parts.append(f"纠正{candidate.correction_count}处[{candidate.correction_summary()}]")
    if candidate.case_fold_count:
        parts.append(f"折叠{candidate.case_fold_count}位")
    return " ".join(parts)


def _write_csv(rows: Sequence[tuple]) -> str:
    """把行渲染成 CSV 文本（转义交给 csv 模块）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=CSV_LINE_TERMINATOR)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return buffer.getvalue()
