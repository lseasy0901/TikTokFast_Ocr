# -*- coding: utf-8 -*-
"""逐帧诊断追踪 (H1 延迟与卡顿归因)

为什么需要新模块（而不是复用现有设施）：
    - ``utils/startup_timing.StartupTiming`` 每个阶段只 ``mark`` 一次
      （``dict.setdefault``），结构上只能描述"一次尝试"的分段，无法逐帧追踪；
    - ``utils/performance.PerformanceMonitor`` 只有聚合 ``SampleWindow``，
      且采集侧只保存单个标量 ``_capture_time``，没有帧标识，无法把两个阶段
      配对成"同一帧"。

本模块做四件事：
    1. 按**帧标识（seq）**记录各阶段时间点，写入固定容量环形缓冲；
    2. 记录若干计数器（覆盖 / 跳帧 / 通知合并 / 消费缺口 / 重复绘制 / 关联失败）；
    3. 以低频（默认 1 Hz）由**独立线程**把汇总写成 JSONL；
    4. 记录 FFmpeg ``stdout.read()`` 的阻塞耗时与结果（与解析耗时分开）。

统计口径（重要，FIX-1 / FIX-6 的契约）
-------------------------------------
``snapshot()`` 是**增量消费**语义：它把环形缓冲中**尚未被消费**的事件取走
（popleft），处理完即丢弃。因此：

    - 同一批事件被重复 ``snapshot()`` **不会**重复计数；
    - ``assoc_fail`` 是**累计失败事件数**，每个事件最多计数一次；
    - ``window.*`` 描述**本次快照消费到的那一批**事件；
    - ``cumulative.*`` 描述自进程启动以来的累计量。

两套口径的分子分母都取自各自明确的集合，不存在混用。

时钟：只用 ``time.perf_counter()``（Windows = QueryPerformanceCounter，100ns）。
**不使用** ``time.monotonic()``：本环境它是 ``GetTickCount64``，分辨率仅
15.6ms 且与 perf_counter 起点不同，既不足以刻画 16.7ms 帧周期，也不可跨模块相减。

开关：环境变量 ``DLV_DIAG_TRACE=1`` 启用，**默认关闭**。
**注意：``_on`` 在模块导入时读取一次，运行期间不可动态切换**（要切换需重启进程）。
关闭时所有采集方法只做一次布尔判断即返回。

阶段（seq 全链统一 = FFmpegReader 的 frame_id）：
    store   → FFmpegReader._store_frame()      读出完整帧并落槽
    bridge  → LatestFrameBuffer.update()       桥接线程写入单槽
    consume → LatestFrameBuffer.get_with_seq() GUI 线程取走
    set     → VideoWidget.set_frame() 返回     QImage/QPixmap 构造完成
    paint   → VideoWidget.paintEvent() 结束    绘制完成（**不等于已上屏**）
"""

import collections
import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger("DouyinLowLatencyViewer.utils.diag_trace")

__all__ = [
    "TRACE", "DiagTrace",
    "S_STORE", "S_BRIDGE", "S_CONSUME", "S_SET", "S_PAINT",
    "STAGES", "PAIRS",
]

# ---- 阶段名 ----
S_STORE = "store"
S_BRIDGE = "bridge"
S_CONSUME = "consume"
S_SET = "set"
S_PAINT = "paint"

STAGES = (S_STORE, S_BRIDGE, S_CONSUME, S_SET, S_PAINT)

#: (段名, 起点阶段, 终点阶段)。段名即"起点→终点"的耗时。
PAIRS = (
    ("store_bridge", S_STORE, S_BRIDGE),
    ("bridge_consume", S_BRIDGE, S_CONSUME),
    ("consume_set", S_CONSUME, S_SET),
    ("set_paint", S_SET, S_PAINT),
    ("store_paint", S_STORE, S_PAINT),
)

#: 待消费事件的环形缓冲容量（条）。5 阶段 × 60fps ≈ 300 事件/秒 → 约 40 秒。
DEFAULT_RING_CAPACITY = 12000

#: 单个段保留的样本数（用于分位数）
DEFAULT_SAMPLE_CAPACITY = 4096

#: 未完成配对的 seq 最多保留多少个（防止长期泄漏）
DEFAULT_OPEN_SEQ_CAPACITY = 512

#: ``stop_output`` 默认等待 writer 线程退出的秒数
DEFAULT_STOP_TIMEOUT = 2.0

_TRUTHY = ("1", "true", "yes", "on")


def _env_on(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


class DiagTrace:
    """逐帧诊断追踪器（线程安全、低开销、默认关闭）。"""

    def __init__(self, ring_capacity: int = DEFAULT_RING_CAPACITY,
                 sample_capacity: int = DEFAULT_SAMPLE_CAPACITY,
                 open_seq_capacity: int = DEFAULT_OPEN_SEQ_CAPACITY):
        #: 开关在**导入时**读取一次，运行期间固定（要切换需重启进程）。
        self._on = _env_on("DLV_DIAG_TRACE")
        self._lock = threading.Lock()          # 只保护 writer 生命周期，不在热路径上
        self._pending = collections.deque(maxlen=ring_capacity)
        self._samples = {name: collections.deque(maxlen=sample_capacity)
                         for name, _, _ in PAIRS}
        self._open: dict = {}                  # seq -> {stage: t}，未完成配对
        self._open_capacity = open_seq_capacity

        # ---- 事件累计计数器（note 时自增，永不复算）----
        self.c_stored = 0
        self.c_bridge = 0
        self.c_consume = 0
        self.c_set = 0
        self.c_paint = 0
        self.c_skipped = 0          # reader 主动跳过的帧（合法跳帧）
        self.c_consume_gap = 0      # GUI 消费时跳过的 buffer 帧数（单槽覆盖所致）
        self.c_paint_repeat = 0     # paintEvent 重复绘制同一 seq
        # ---- 事件流转计数（用于确认"不重复消费"）----
        self.c_events_noted = 0
        self.c_events_drained = 0
        self.c_events_lost = 0      # 环形缓冲覆盖导致的丢失
        # ---- 关联失败：**累计**，每个事件最多计一次 ----
        self.c_assoc_fail = 0
        #: 最近若干次关联失败的明细（有界）：(pair 名或 "no_seq", seq)。
        #: 目的：assoc_fail 不再是"只增黑盒"——失败可归因。
        self._fail_log: collections.deque = collections.deque(maxlen=64)
        # ---- 只读镜像：由调用方在低频汇总时写入 ----
        self.ext_overwritten = 0
        self.ext_notify = 0
        self.ext_coalesced = 0
        # ---- stdout.read() 观测 ----
        self.c_read_calls = 0
        self.c_read_eof = 0
        self.c_read_partial = 0
        self.c_read_error = 0
        self._read_ms = collections.deque(maxlen=sample_capacity)
        self._read_bytes = collections.deque(maxlen=sample_capacity)
        self._parse_ms = collections.deque(maxlen=sample_capacity)
        # ---- 非帧级事件（FIX-7）：EOF / 断流 / 看门狗击杀 / 停滞重启等。
        # 稀有事件，只在发生时自增；dict 读写仅在事件线程上，无热路径开销。
        self._events: dict = {}
        self._last_event: tuple | None = None   # (name, t_perf)
        self._last_painted_seq = None
        # ---- writer 生命周期 ----
        self._writer: threading.Thread | None = None
        self._writer_stop: threading.Event | None = None
        self._file: str | None = None
        self.output_errors = 0
        self.last_output_error: str | None = None
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------
    # 开关
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """是否启用。**在模块导入时确定，运行期间不可切换。**"""
        return self._on

    # ------------------------------------------------------------------
    # 热路径：记录
    # ------------------------------------------------------------------
    def note(self, stage: str, seq) -> None:
        """记录"阶段 stage 在 seq 这一帧上发生"。

        热路径：一次取时 + 一次 append。关闭时立即返回。
        """
        if not self._on:
            return
        self._pending.append((stage, seq, time.perf_counter()))
        self.c_events_noted += 1

    def bump(self, name: str, n: int = 1) -> None:
        """通用计数器自增（热路径；关闭时立即返回）。"""
        if not self._on:
            return
        setattr(self, name, getattr(self, name) + n)

    def count_skip(self, n: int = 1) -> None:
        """reader 主动跳过的帧数（合法跳帧，不是关联失败）。"""
        if not self._on:
            return
        self.c_skipped += n

    def count_consume_gap(self, gap: int) -> None:
        """GUI 消费时跨过的 buffer 帧数（>0 表示发生了单槽覆盖）。"""
        if not self._on:
            return
        if gap > 0:
            self.c_consume_gap += gap

    def note_paint(self, seq) -> None:
        """记录一次 paintEvent 完成，并统计是否重复绘制同一帧。"""
        if not self._on:
            return
        if seq is not None and seq == self._last_painted_seq:
            self.c_paint_repeat += 1
        self._last_painted_seq = seq

    # ---- stdout.read() 观测（FIX-4）----
    def note_read(self, duration_ms: float, nbytes: int, outcome: str) -> None:
        """记录一次 ``stdout.read()`` 的结果。

        该耗时**包含等待数据到达的阻塞时间**，不等于纯 FFmpeg 解码耗时。

        :param outcome: "ok" | "eof" | "partial" | "error"
        """
        if not self._on:
            return
        self.c_read_calls += 1
        if outcome == "eof":
            self.c_read_eof += 1
        elif outcome == "partial":
            self.c_read_partial += 1
        elif outcome == "error":
            self.c_read_error += 1
        self._read_ms.append(float(duration_ms))
        self._read_bytes.append(int(nbytes))

    def note_parse(self, duration_ms: float) -> None:
        """记录一次"字节 → numpy 帧对象"的解析/构造耗时（不含等待数据）。"""
        if not self._on:
            return
        self._parse_ms.append(float(duration_ms))

    # ---- 非帧级事件观测（FIX-7）----
    def note_event(self, name: str) -> None:
        """记录一次非帧级事件（EOF / 断流 / 看门狗击杀 / 停滞重启 / 读异常）。

        与逐帧 ``note`` 分开：事件不携带 seq、不参与配对，只累计次数并
        记录最近一次发生时刻，用于把卡顿与供给中断在时间上对齐。
        """
        if not self._on:
            return
        self._events[name] = self._events.get(name, 0) + 1
        self._last_event = (name, time.perf_counter())

    # ------------------------------------------------------------------
    # 低频汇总：**增量消费**语义
    # ------------------------------------------------------------------
    def _drain(self):
        """取走所有尚未消费的事件（原子 popleft，无锁）。"""
        out = []
        pending = self._pending
        while True:
            try:
                out.append(pending.popleft())
            except IndexError:
                break
        return out

    @staticmethod
    def _stats(deq):
        if not deq:
            return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0,
                    "p99": 0.0, "max": 0.0}
        ordered = sorted(deq)
        n = len(ordered)
        pick = lambda p: ordered[int((n - 1) * p)]      # noqa: E731
        return {"count": n, "mean": sum(ordered) / n,
                "p50": pick(0.50), "p95": pick(0.95),
                "p99": pick(0.99), "max": ordered[-1]}

    def snapshot(self) -> dict:
        """**消费**尚未处理的事件并返回汇总。

        语义：增量消费。同一批事件重复调用不会重复计数（FIX-1 / FIX-6）。
        """
        if not self._on:
            return {}
        batch = self._drain()
        self.c_events_drained += len(batch)
        # 环形缓冲覆盖导致的丢失（note 过但已被挤掉）
        lost = self.c_events_noted - self.c_events_drained - len(self._pending)
        if lost > 0:
            self.c_events_lost += lost
            self.c_events_drained += lost

        window_fail = 0
        for stage, seq, t in batch:
            if seq is None:
                # 无关联事件：每个事件只计一次
                self.c_assoc_fail += 1
                window_fail += 1
                self._fail_log.append(("no_seq:" + stage, None))
                continue
            slot = self._open.get(seq)
            if slot is None:
                slot = self._open[seq] = {"t": {}, "done": set()}
            # 同一 (seq, stage) 保留**首次**观测：后续重复事件（GUI 对同一帧
            # 的重复 consume / 重复 paint）不覆盖首次时序，也不参与二次配对。
            slot["t"].setdefault(stage, t)

        # 完成配对 → 喂样本窗（每个 (seq, pair) 只配**一次**）
        for seq, slot in list(self._open.items()):
            times = slot["t"]
            done = slot["done"]
            for name, a, b in PAIRS:
                if name in done:
                    continue
                ta = times.get(a)
                tb = times.get(b)
                if ta is None or tb is None:
                    continue
                delta = tb - ta
                if delta < 0:
                    # 负 delta 同样终结该 (seq,pair)：时序倒挂是结构性事件，
                    # 重试只会重复计数（契约：每事件最多计一次）。
                    done.add(name)
                    self.c_assoc_fail += 1
                    window_fail += 1
                    self._fail_log.append((name, seq))
                    continue
                done.add(name)             # 该 (seq,pair) 终结，重复事件不再配对
                times.pop(a, None)         # 起点已消费；终点保留给链上下一对
                self._samples[name].append(delta * 1000.0)
            if not times and not done:
                self._open.pop(seq, None)

        # 有界：只保留最近的 seq 槽位
        if len(self._open) > self._open_capacity:
            for key in sorted(self._open)[:len(self._open) - self._open_capacity]:
                self._open.pop(key, None)

        window_events = len(batch)
        return {
            "t_rel_s": time.perf_counter() - self._t0,
            # 本次快照消费到的那一批（分子分母同源）
            "window": {
                "events": window_events,
                "assoc_fail": window_fail,
                "fail_rate": (window_fail / window_events) if window_events else 0.0,
            },
            # 自启动以来的累计量
            "cumulative": {
                "events_noted": self.c_events_noted,
                "events_drained": self.c_events_drained,
                "events_lost": self.c_events_lost,
                "assoc_fail": self.c_assoc_fail,
                "fail_rate": (self.c_assoc_fail / self.c_events_drained)
                if self.c_events_drained else 0.0,
            },
            "counters": {
                "stored": self.c_stored,
                "bridge": self.c_bridge,
                "consume": self.c_consume,
                "set": self.c_set,
                "paint": self.c_paint,
                "skipped": self.c_skipped,
                "consume_gap": self.c_consume_gap,
                "paint_repeat": self.c_paint_repeat,
                "buffer_overwritten": self.ext_overwritten,
                "notify": self.ext_notify,
                "notify_coalesced": self.ext_coalesced,
                "pending_events": len(self._pending),
                "open_seqs": len(self._open),
                "output_errors": self.output_errors,
                "events": dict(self._events),
                "last_event": (self._last_event[0],
                               self._last_event[1] - self._t0)
                if self._last_event else None,
                "fail_log": list(self._fail_log),
            },
            "read": {
                "calls": self.c_read_calls,
                "eof": self.c_read_eof,
                "partial": self.c_read_partial,
                "error": self.c_read_error,
                "block_ms": self._stats(self._read_ms),
                "bytes": self._stats(self._read_bytes),
            },
            "parse_ms": self._stats(self._parse_ms),
            "segments_ms": {name: self._stats(self._samples[name])
                            for name, _, _ in PAIRS},
        }

    # ------------------------------------------------------------------
    # 低频输出线程（绝不阻塞采集 / 桥接 / GUI 热路径）
    # ------------------------------------------------------------------
    @property
    def output_path(self) -> str | None:
        """当前（或最近一次）输出文件路径；未启用/未启动时为 None。"""
        return self._file

    @property
    def writer_alive(self) -> bool:
        th = self._writer
        return bool(th is not None and th.is_alive())

    def start_output(self, path: str | None = None,
                     interval_s: float = 1.0) -> str | None:
        """启动低频 JSONL 输出线程。

        成功契约：**文件已能被打开追加**且 writer 线程已启动，才返回路径；
        否则返回 ``None`` 并记录 ``last_output_error``（不静默吞异常）。

        重复调用：若 writer 仍在运行，直接返回当前路径（不新建第二个 writer）。

        :return: 成功时为实际使用的文件路径；失败为 None
        """
        if not self._on:
            return None
        if path is None:
            path = os.environ.get("DLV_DIAG_TRACE_FILE") or os.path.join(
                tempfile.gettempdir(), "livelens_diag_trace_%d.jsonl" % os.getpid())
        try:
            interval = float(os.environ.get("DLV_DIAG_TRACE_INTERVAL") or interval_s)
        except ValueError:
            interval = interval_s
        interval = max(0.2, interval)

        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                return self._file
            # 成功契约：先验证可写（只试开一次，不在热路径上）
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
            except OSError as e:
                self.last_output_error = "open failed: %s" % e
                logger.error("[DIAG] 追踪输出无法写入 %s: %s", path, e)
                return None
            stop = threading.Event()
            self._writer_stop = stop
            self._file = path
            th = threading.Thread(
                target=self._writer_loop, args=(path, interval, stop),
                name="diag-trace-writer", daemon=True)
            self._writer = th
            th.start()
        logger.info("[DIAG] 逐帧追踪已启用，输出: %s (间隔 %.1fs)", path, interval)
        return path

    def stop_output(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
        """停止 writer 线程。

        契约：返回 True 表示**已确认线程退出**；返回 False 表示在 ``timeout``
        内未退出（显式报告失败，不假装成功）。不无限等待。

        重复调用：幂等（第二次返回 True）。
        """
        with self._lock:
            stop = self._writer_stop
            th = self._writer
            self._writer = None
            self._writer_stop = None
        if stop is not None:
            stop.set()
        if th is None:
            return True
        th.join(timeout=timeout)
        if th.is_alive():
            logger.error("[DIAG] writer 线程在 %.1fs 内未退出（线程泄漏风险）", timeout)
            return False
        return True

    def _writer_loop(self, path: str, interval: float, stop: threading.Event) -> None:
        """writer 主循环。

        - 只读**局部** ``stop`` 事件（不读 self._writer_stop，避免被置 None 后条件恒真）；
        - 写失败计入 output_errors，不静默；
        - 文件句柄由 with 保证释放。
        """
        try:
            with open(path, "a", encoding="utf-8") as f:
                while not stop.wait(interval):
                    try:
                        snap = self.snapshot()
                        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
                        f.flush()
                    except OSError as e:
                        self.output_errors += 1
                        self.last_output_error = "write failed: %s" % e
                        logger.warning("[DIAG] 追踪写盘失败 #%d: %s",
                                       self.output_errors, e)
                    except Exception as e:  # noqa: BLE001 - 诊断不得影响主流程
                        self.output_errors += 1
                        self.last_output_error = "snapshot failed: %s" % e
        except OSError as e:
            self.output_errors += 1
            self.last_output_error = "open failed: %s" % e
            logger.error("[DIAG] 追踪输出线程无法打开 %s: %s", path, e)


#: 全局单例。关闭状态下所有方法都是"一次布尔判断 + 返回"。
TRACE = DiagTrace()

# H6 生产采集修复：启用时自动启动 JSONL 输出。
# 此前仅 feed/queue 生效而无人调用 start_output()——生产环境设置
# DLV_DIAG_TRACE=1 不会产生任何文件。输出路径与间隔可用
# DLV_DIAG_TRACE_FILE / DLV_DIAG_TRACE_INTERVAL 覆盖；
# 启动失败（路径不可写等）不抛出：记录 last_output_error 并继续采集入内存环形缓冲。
if TRACE.enabled:
    TRACE.start_output()
