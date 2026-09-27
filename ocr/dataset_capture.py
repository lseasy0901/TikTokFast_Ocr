# -*- coding: utf-8 -*-
"""
数据集采集旁路（Dataset Capture）—— 可一键关闭的测试入口。

它是什么
    正式 LiveLens 里的一条**纯旁路**：识别线程已经产出「这一帧 ROI 像素 +
    PP-OCRv6 的原始读法」之后，把这两样东西抄一份存下来，供后续离线基准用。
    不重跑 Teacher、不重开视频流、不建第二个 OCR 引擎、不改任何识别逻辑。

唯一的开关
    `DATASET_CAPTURE_ENABLED`
        True  —— 主窗口显示「数据集采集」入口，Recorder 才被构造
        False —— 入口不显示、Recorder 不构造、不建队列、不起线程、
                 不落任何文件；生产路径与"从来没有这个功能"一致

    关闭只需要把下面这一行改成 False，不需要删任何代码。

为什么接在 OCRWorker 现有的 sample_capture 槽位上
    `OCRWorker.__init__(sample_capture=...)` 已经是一个默认 None 的旁路出口，
    每次识别后会把 (roi_image, readings, candidate, note) 交给它。
    本模块用一个**转发对象**（CaptureFanout）顶替那个槽位，把同一份数据同时
    喂给原有的 SampleCapture 和新 Recorder。因此 `ocr/worker.py`、
    `ocr/room_code.py`、`ocr/ppocr_engine.py` **一行都不用改**。

不阻塞的保证
    `offer()` 只在识别线程里做三件事：看间隔、`put_nowait`、返回。
    队列满了就丢帧并计数，**永不阻塞**识别线程；PNG 编码、SHA-256、
    dHash、CSV 追加全部在独立的 writer 线程里做。

真值口径（本测试阶段）
    `ground_truth` 取 **PP-OCRv6 的原始读法**（不是经过逐位纠正的候选）——
    只有符合三种目标格式的才写入 labels.csv，其余进 rejected.csv。
    绝不把非法读法"修正"成合法房间码。

    已知限制：现有链路没有把逐帧 confidence 传到 worker（
    PPOCREngine.iter_readings 只产出文本），所以 teacher_raw.csv 的
    confidence 列**如实留空**，不编造。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

__all__ = [
    "DATASET_CAPTURE_ENABLED",
    "TARGET_FORMATS",
    "match_format",
    "DatasetRecorder",
    "CaptureFanout",
    "CaptureStats",
    "DEFAULT_SAMPLE_INTERVAL_MS",
]

# ======================================================================
# 总开关 —— 关闭数据集功能只需要改这一行
# ======================================================================
#: 数据集采集入口总开关。
#: False 时：UI 不显示入口、Recorder 不构造、不建队列/线程、不落任何文件。
DATASET_CAPTURE_ENABLED = True

#: 采样间隔（毫秒）。§7 要求 300~500ms，且必须可配。
DEFAULT_SAMPLE_INTERVAL_MS = 300

#: 队列上限。满了就丢帧（只计数），绝不阻塞识别线程。
DEFAULT_QUEUE_SIZE = 1024

#: 命中哪个格式就按哪个格式记；三者长度互不相同，因此不会歧义。
TARGET_FORMATS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("aaa123", re.compile(r"^[A-Z]{3}[0-9]{3}\Z")),
    ("bbbb123", re.compile(r"^[A-Z]{4}[0-9]{3}\Z")),
    ("aaaa1234", re.compile(r"^[A-Z]{4}[0-9]{4}\Z")),
)

#: 落盘目录前缀
_DATASET_PREFIX = "LiveLens-OCR-Dataset"

_SAMPLE_FIELDS = ("sample_id", "image", "format_id", "timestamp_ms")
_LABEL_FIELDS = ("sample_id", "ground_truth")
_TEACHER_FIELDS = ("sample_id", "raw_text", "confidence")
_REJECT_FIELDS = ("sample_id", "raw_text", "reason")


def match_format(text: str) -> Optional[str]:
    """文本严格命中三种目标格式之一时返回 format_id，否则 None。

    不做任何纠正 / 大小写折叠 / 截断 —— 不合法就是不合法。
    """
    if not text:
        return None
    for fmt_id, pattern in TARGET_FORMATS:
        if pattern.match(text):
            return fmt_id
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dhash(gray: np.ndarray, size: int = 8) -> str:
    """dHash：缩到 (size+1) x size，逐行比较相邻像素。用 cv2（已是运行依赖）。

    与 tools/ocr_dataset_generator/dedupe.py 的实现同算法，但**刻意不 import
    tools**：那个包不随正式包分发，生产代码依赖它会把打包配置也拖下水
    （见任务 §15「不要修改安装包」）。
    """
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    bits = "".join("1" if b else "0" for b in diff.flatten())
    # 宽度必须是 size*size 位对应的十六进制位数。写成 size*2//4 会得到一个
    # **下限**而不是固定宽度：全零（纯色图）时会产出 "0000"（4 字符），
    # 与其它哈希长度不一致，hamming_distance 会直接判长度不等。
    return "%0*x" % (size * size // 4, int(bits, 2))


@dataclass
class CaptureStats:
    """实时计数（UI 直接读它）。"""

    per_format: Dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    duplicate: int = 0
    dropped: int = 0          # 队列满被丢弃
    skipped_interval: int = 0  # 未到采样间隔被跳过
    written: int = 0

    @property
    def total(self) -> int:
        return sum(self.per_format.values())

    def snapshot(self, targets: Optional[Dict[str, int]] = None) -> dict:
        t = targets or {}
        return {
            "per_format": dict(self.per_format),
            "targets": dict(t),
            "total": self.total,
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "dropped": self.dropped,
            "skipped_interval": self.skipped_interval,
            "written": self.written,
            "complete": bool(t) and all(
                self.per_format.get(k, 0) >= v for k, v in t.items()),
        }


@dataclass
class _Item:
    """识别线程 → writer 线程的轻量投递物。image 是 ROI 的**副本引用**，
    由 writer 线程独占使用（识别线程不会再改它）。"""

    seq: int
    image: np.ndarray
    raw_text: str
    confidence: Optional[float]
    timestamp_ms: int


class DatasetRecorder:
    """把识别线程产出的 (ROI, Teacher 原文) 异步落成数据集。"""

    def __init__(self,
                 root: Optional[str] = None,
                 session_id: Optional[str] = None,
                 sample_interval_ms: int = DEFAULT_SAMPLE_INTERVAL_MS,
                 targets: Optional[Dict[str, int]] = None,
                 queue_size: int = DEFAULT_QUEUE_SIZE):
        self._explicit_root = root
        self._session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.sample_interval_ms = max(0, int(sample_interval_ms))
        self.targets = dict(targets or {})
        self.stats = CaptureStats(per_format={fmt: 0 for fmt, _ in TARGET_FORMATS})

        self._queue: "queue.Queue[Optional[_Item]]" = queue.Queue(maxsize=queue_size)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()          # 只保护计数与文件句柄
        self._seq = 0
        self._last_offer_ms = 0.0
        self._root: Optional[str] = None
        self._sha_seen: Dict[str, str] = {}
        self._phash_seen: Dict[str, str] = {}
        self._csv = {}
        self._errors: List[str] = []

    # ------------------------------------------------------------------
    @property
    def root(self) -> Optional[str]:
        return self._root

    @property
    def running(self) -> bool:
        return self._running

    @property
    def errors(self) -> List[str]:
        return list(self._errors)

    def _resolve_root(self) -> str:
        if self._explicit_root:
            return self._explicit_root
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base = docs if os.path.isdir(docs) else os.getcwd()
        return os.path.join(base, "%s-%s" % (_DATASET_PREFIX, self._session_id))

    # ------------------------------------------------------------------
    def start(self) -> str:
        """建目录、起 writer 线程。幂等。"""
        with self._lock:
            if self._running:
                return self._root
            self._root = self._resolve_root()
            os.makedirs(os.path.join(self._root, "images"), exist_ok=True)
            self._csv = {
                "samples.csv": (_SAMPLE_FIELDS, "a"),
                "labels.csv": (_LABEL_FIELDS, "a"),
                "teacher_raw.csv": (_TEACHER_FIELDS, "a"),
                "rejected.csv": (_REJECT_FIELDS, "a"),
            }
            for name, (fields, _mode) in self._csv.items():
                path = os.path.join(self._root, name)
                if not os.path.isfile(path):
                    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                        csv.DictWriter(fh, fieldnames=list(fields),
                                       lineterminator="\r\n").writeheader()
            self._running = True
            self._thread = threading.Thread(target=self._writer_loop,
                                            name="DatasetWriter", daemon=True)
            self._thread.start()
            return self._root

    def stop(self, timeout: float = 10.0) -> Optional[str]:
        """把队列排空、收尾并写 dataset.json / manifest。幂等。"""
        with self._lock:
            if not self._running:
                return self._root
            self._running = False
        try:
            self._queue.put_nowait(None)          # 毒丸
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._finalize()
        return self._root

    def close(self) -> None:
        self.stop()

    # ------------------------------------------------------------------
    def offer(self, image: np.ndarray, raw_text: str,
              confidence: Optional[float] = None,
              timestamp_ms: Optional[int] = None) -> bool:
        """识别线程调用。**只做间隔判断 + 入队，永不阻塞。**

        返回 True 表示这一帧被接受入队（不代表最终写盘成功）。
        """
        if not self._running or image is None or getattr(image, "size", 0) == 0:
            return False
        now = time.perf_counter() * 1000.0
        if self.sample_interval_ms and (now - self._last_offer_ms) < self.sample_interval_ms:
            with self._lock:
                self.stats.skipped_interval += 1
            return False
        self._last_offer_ms = now

        with self._lock:
            self._seq += 1
            seq = self._seq
        item = _Item(seq=seq, image=image, raw_text=raw_text or "",
                     confidence=confidence,
                     timestamp_ms=int(timestamp_ms if timestamp_ms is not None
                                      else time.time() * 1000))
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self.stats.dropped += 1
            return False

    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if item is None:
                break
            try:
                self._write_one(item)
            except Exception as exc:  # noqa: BLE001 - 写盘失败绝不能弄死线程
                with self._lock:
                    self._errors.append("写盘失败 seq=%d: %s" % (item.seq, exc))

    def _write_one(self, item: _Item) -> None:
        sid = "%06d" % item.seq
        rel = "images/%s.png" % sid
        full = os.path.join(self._root, rel.replace("/", os.sep))

        ok, buf = cv2.imencode(".png", item.image)
        if not ok:
            raise OSError("PNG 编码失败")
        data = buf.tobytes()
        digest = _sha256(data)

        # ---- 完全相同图片不重复进入数据集（§10）----
        if digest in self._sha_seen:
            with self._lock:
                self.stats.duplicate += 1
            self._append("rejected.csv", {"sample_id": sid, "raw_text": item.raw_text,
                                          "reason": "duplicate_exact"})
            return

        fmt_id = match_format(item.raw_text)
        if fmt_id is None:
            with self._lock:
                self.stats.rejected += 1
            self._append("rejected.csv", {"sample_id": sid, "raw_text": item.raw_text,
                                          "reason": "format_invalid"
                                                    if item.raw_text else "empty"})
            return

        # 近重复只记录、不删除（§10）
        gray = cv2.cvtColor(item.image, cv2.COLOR_BGR2GRAY) \
            if item.image.ndim == 3 else item.image
        phash = _dhash(gray)
        near = self._phash_seen.get(phash)

        with open(full, "wb") as fh:
            fh.write(data)
        self._sha_seen[digest] = rel
        self._phash_seen.setdefault(phash, rel)

        self._append("samples.csv", {
            "sample_id": sid, "image": rel, "format_id": fmt_id,
            "timestamp_ms": item.timestamp_ms})
        self._append("labels.csv", {"sample_id": sid, "ground_truth": item.raw_text})
        self._append("teacher_raw.csv", {
            "sample_id": sid, "raw_text": item.raw_text,
            "confidence": "" if item.confidence is None else round(item.confidence, 4)})
        if near is not None:
            self._append("rejected.csv", {"sample_id": sid, "raw_text": item.raw_text,
                                          "reason": "duplicate_near:%s" % near})

        with self._lock:
            self.stats.per_format[fmt_id] = self.stats.per_format.get(fmt_id, 0) + 1
            self.stats.written += 1

    def _append(self, name: str, row: dict) -> None:
        with self._lock:
            fields = self._csv[name][0]
            path = os.path.join(self._root, name)
            with open(path, "a", encoding="utf-8-sig", newline="") as fh:
                csv.DictWriter(fh, fieldnames=list(fields),
                               lineterminator="\r\n").writerow(row)

    # ------------------------------------------------------------------
    def _finalize(self) -> None:
        """写 dataset.json / README.md / manifest.sha256（不含自身）。"""
        if not self._root or not os.path.isdir(self._root):
            return
        snap = self.stats.snapshot(self.targets)
        meta = {
            "dataset_kind": "live-capture",
            "dataset_name": os.path.basename(self._root),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": self._session_id,
            "sampling": {"sample_interval_ms": self.sample_interval_ms},
            "targets": self.targets,
            "counts": {
                "valid": snap["total"], "per_format": snap["per_format"],
                "rejected": snap["rejected"], "duplicate": snap["duplicate"],
                "dropped_queue_full": snap["dropped"],
            },
            "labeling": {
                "ground_truth": "PP-OCRv6 原始读法（本测试阶段不做人工标注）",
                "raw_text": "PP-OCRv6 原始读法，未经任何纠正/美化",
                "confidence": "现有链路未把逐帧置信度传到 worker，本列为空",
                "filter": "只接受 %s" % ", ".join(f for f, _ in TARGET_FORMATS),
            },
            "errors": self._errors[:20],
            "manifest": "manifest.sha256",
        }
        with open(os.path.join(self._root, "dataset.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

        with open(os.path.join(self._root, "README.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(
                "# %s\n\nLiveLens 实时采集数据集（ROI + PP-OCRv6 预标注）。\n\n"
                "- session: `%s`\n- 采样间隔: %d ms\n"
                "- ground_truth = PP-OCRv6 原始读法（测试阶段，无人工标注）\n"
                "- 只接受 %s；其余进 rejected.csv\n"
                "- confidence 列为空：现有链路不向 worker 传逐帧置信度\n"
                % (meta["dataset_name"], self._session_id,
                   self.sample_interval_ms,
                   ", ".join(f for f, _ in TARGET_FORMATS)))

        lines = []
        for dirpath, _dirs, files in os.walk(self._root):
            for name in sorted(files):
                if name == "manifest.sha256":
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self._root).replace("\\", "/")
                with open(full, "rb") as fh:
                    lines.append("%s  %s" % (_sha256(fh.read()), rel))
        with open(os.path.join(self._root, "manifest.sha256"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")


    # ------------------------------------------------------------------
    def capture(self, image, profile_id: str = "", readings=None,
                candidate: str = "", note: str = "") -> None:
        """与 `SampleCapture.capture` **同签名**，好让两者一起挂进 CaptureFanout。

        只取 `readings` 里的 PP-OCRv6 原始读法；`candidate`（经逐位纠正的候选）
        与 `profile_id` 都不参与真值判定 —— 纠正后的结果不能冒充原始输出。
        本方法不抛异常：旁路绝不影响识别主路。
        """
        try:
            self.offer(image, raw_text_from_readings(readings))
        except Exception:  # noqa: BLE001
            pass


class CaptureFanout:
    """顶替 OCRWorker 的 `sample_capture` 槽位，把同一份数据转发给多个接收方。

    存在的意义：让数据集旁路接进**已经存在**的那一个旁路出口，
    从而完全不必要去改 `ocr/worker.py`。任一接收方抛异常都不影响其它接收方，
    也不冒泡到识别线程。
    """

    def __init__(self, *sinks):
        self._sinks = [s for s in sinks if s is not None]

    @property
    def sinks(self) -> list:
        return list(self._sinks)

    def capture(self, image, **kwargs) -> None:
        for sink in self._sinks:
            try:
                sink.capture(image, **kwargs)
            except Exception:  # noqa: BLE001 - 旁路绝不影响识别主路
                pass


def raw_text_from_readings(readings) -> str:
    """从 worker 传来的 readings 里取 PP-OCRv6 的原始读法。

    readings 为 None（诊断未开启）时返回空串 —— 此时这一帧进 rejected，
    绝不拿"纠正后的候选"冒充原始读法。
    """
    if not readings:
        return ""
    # PP-OCRv6 是单读法后端，标签固定为 "ppocr"；兼容其它标签时取第一项。
    for psm, text in readings:
        if str(psm) == "ppocr":
            return text or ""
    return (readings[0][1] or "") if readings else ""
