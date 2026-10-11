# -*- coding: utf-8 -*-
"""逐帧诊断追踪 (H1 延迟与卡顿归因)

为什么需要新模块（而不是复用现有设施）：
    - ``utils/startup_timing.StartupTiming`` 每个阶段只 ``mark`` 一次
      （``dict.setdefault``），结构上只能描述"一次尝试"的分段，无法逐帧追踪；
    - ``utils/performance.PerformanceMonitor`` 只有聚合 ``SampleWindow``，
      且采集侧只保存**单个标量** ``_capture_time``，没有帧标识，无法把两个阶段
      配对成"同一帧"。

本模块只做三件事：
    1. 按**帧标识（seq）**记录各阶段时间点，写入固定容量环形缓冲；
    2. 记录若干计数器（覆盖 / 跳帧 / 通知合并 / 消费缺口 / 重复绘制 / 关联失败）；
    3. 以低频（默认 1 Hz）由**独立线程**把汇总写成 JSONL。

设计约束（与 H1 任务要求一致）：
    - 开关 ``DLV_DIAG_TRACE=1`` 启用，**默认关闭**；关闭时所有方法立即返回，
      热路径只多一次布尔判断；
    - 只用 ``time.perf_counter()``（Windows = QueryPerformanceCounter，分辨率
      100ns）。**不使用 ``time.monotonic()``**：在本环境它是 ``GetTickCount64``，
      分辨率仅 15.6ms，且与 perf_counter 起点不同，无法跨模块相减；
    - 热路径只做：一次取时 + 一次 deque.append + 若干 int 自增；
    - 不逐帧写盘；汇总由独立线程完成，采集路径从不做 IO 或字符串格式化；
    - 环形缓冲固定容量，溢出即丢最旧（不扩容）；
    - 无法直接观测的阶段（像素真正上屏）不在本模块伪造。

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

#: 环形缓冲容量（事件条数）。5 阶段 × 60fps ≈ 300 事件/秒 → 约 40 秒窗口。
DEFAULT_RING_CAPACITY = 12000

#: 单个段保留的样本数（用于分位数）
DEFAULT_SAMPLE_CAPACITY = 4096

_TRUTHY = ("1", "true", "yes", "on")


def _env_on(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


class DiagTrace:
    """逐帧诊断追踪器（线程安全、低开销、默认关闭）。"""

    def __init__(self, ring_capacity: int = DEFAULT_RING_CAPACITY,
                 sample_capacity: int = DEFAULT_SAMPLE_CAPACITY):
        self._on = _env_on("DLV_DIAG_TRACE")
        self._lock = threading.Lock()
        self._ring = collections.deque(maxlen=ring_capacity)
        self._samples = {name: collections.deque(maxlen=sample_capacity)
                         for name, _, _ in PAIRS}
        # ---- 计数器（热路径：纯 int 自增，GIL 下足够）----
        self.c_stored = 0
        self.c_bridge = 0
        self.c_consume = 0
        self.c_set = 0
        self.c_paint = 0
        self.c_skipped = 0          # reader 主动跳过的帧（合法跳帧）
        self.c_consume_gap = 0      # GUI 消费时跳过的 buffer 帧数（单槽覆盖所致）
        self.c_paint_repeat = 0     # paintEvent 重复绘制同一 seq
        self.c_assoc_fail = 0       # 段耗时 < 0 或 seq 缺失
        self.c_ring_overflow = 0    # 环形缓冲淘汰事件数（近似）
        # 只读镜像：由调用方在低频汇总时写入
        self.ext_overwritten = 0
        self.ext_notify = 0
        self.ext_coalesced = 0
        self._last_painted_seq = None
        self._last_consumed_seq = None
        self._writer = None
        self._writer_stop = None
        self._file = None
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------
    # 开关
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
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
        self._ring.append((stage, seq, time.perf_counter()))

    def count_skip(self, n: int = 1) -> None:
        """reader 主动跳过的帧数（合法跳帧，不是关联失败）。"""
        if not self._on:
            return
        self.c_skipped += n

    def bump(self, name: str, n: int = 1) -> None:
        """通用计数器自增（热路径；关闭时立即返回）。"""
        if not self._on:
            return
        setattr(self, name, getattr(self, name) + n)

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

    # ------------------------------------------------------------------
    # 低频汇总（非热路径）
    # ------------------------------------------------------------------
    def _snapshot_events(self):
        """防御性复制环形缓冲（可能正被其它线程 append）。"""
        try:
            return list(self._ring)
        except RuntimeError:      # deque mutated during iteration
            return []

    @staticmethod
    def _pct(ordered, p):
        return ordered[int((len(ordered) - 1) * p)]

    def snapshot(self) -> dict:
        """返回结构化汇总；同时把本周期样本喂给内部样本窗。"""
        if not self._on:
            return {}
        events = self._snapshot_events()

        # seq -> {stage: t}
        by_seq = {}
        for stage, seq, t in events:
            if seq is None:
                self.c_assoc_fail += 1
                continue
            d = by_seq.get(seq)
            if d is None:
                d = by_seq[seq] = {}
            d[stage] = t

        seg_values = {name: [] for name, _, _ in PAIRS}
        for seq, d in by_seq.items():
            for name, a, b in PAIRS:
                ta = d.get(a)
                tb = d.get(b)
                if ta is None or tb is None:
                    continue
                delta = tb - ta
                if delta < 0:
                    self.c_assoc_fail += 1     # 时钟倒退/配对错误
                    continue
                seg_values[name].append(delta * 1000.0)

        out = {}
        for name, vals in seg_values.items():
            if vals:
                ordered = sorted(vals)
                self._samples[name].extend(ordered)
            allv = self._samples[name]
            if allv:
                ordered = sorted(allv)
                out[name] = {
                    "count": len(allv),
                    "mean": sum(ordered) / len(ordered),
                    "p50": self._pct(ordered, 0.50),
                    "p95": self._pct(ordered, 0.95),
                    "p99": self._pct(ordered, 0.99),
                    "max": ordered[-1],
                }
            else:
                out[name] = {"count": 0, "mean": 0.0, "p50": 0.0,
                             "p95": 0.0, "p99": 0.0, "max": 0.0}

        return {
            "t_rel_s": time.perf_counter() - self._t0,
            "counters": {
                "stored": self.c_stored,
                "bridge": self.c_bridge,
                "consume": self.c_consume,
                "set": self.c_set,
                "paint": self.c_paint,
                "skipped": self.c_skipped,
                "consume_gap": self.c_consume_gap,
                "paint_repeat": self.c_paint_repeat,
                "assoc_fail": self.c_assoc_fail,
                "buffer_overwritten": self.ext_overwritten,
                "notify": self.ext_notify,
                "notify_coalesced": self.ext_coalesced,
                "ring_events": len(events),
            },
            "segments_ms": out,
        }

    # ------------------------------------------------------------------
    # 低频输出线程（绝不阻塞采集 / 桥接 / GUI 热路径）
    # ------------------------------------------------------------------
    def start_output(self, path: str | None = None, interval_s: float = 1.0) -> str | None:
        """启动低频 JSONL 输出线程。返回实际使用的文件路径（未启用时 None）。

        只由主线程在连接建立后调用一次。
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
        with self._lock:
            if self._writer is not None:
                return self._file
            self._file = path
            self._writer_stop = threading.Event()
            self._writer = threading.Thread(
                target=self._writer_loop,
                args=(path, max(0.2, interval)),
                name="diag-trace-writer",
                daemon=True,
            )
            self._writer.start()
        logger.info("[DIAG] 逐帧追踪已启用，输出: %s (间隔 %.1fs)", path, interval)
        return path

    def stop_output(self) -> None:
        """停止输出线程（幂等）。"""
        with self._lock:
            stop = self._writer_stop
            self._writer = None
            self._writer_stop = None
        if stop is not None:
            stop.set()

    def _writer_loop(self, path: str, interval: float) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                while not (self._writer_stop and self._writer_stop.is_set()):
                    time.sleep(interval)
                    try:
                        snap = self.snapshot()
                        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
                        f.flush()
                    except Exception:  # noqa: BLE001 - 诊断输出不得影响主流程
                        pass
        except Exception as e:  # noqa: BLE001
            logger.warning("[DIAG] 追踪输出线程异常退出: %s", e)


#: 全局单例。关闭状态下所有方法都是"一次布尔判断 + 返回"。
TRACE = DiagTrace()
