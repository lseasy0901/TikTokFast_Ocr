# -*- coding: utf-8 -*-
"""
采集编排 + 数据集落盘（Phase 2: Dataset Collector）。

把各模块串起来：
    FrameSource -> crop_roi -> PPOCRTeacher -> SegmentTracker
                -> SampleWriter -> samples / truth / teacher_raw /
                   teacher_consensus / segments / review_queue -> manifest

职责边界：
    capture.py  帧与 ROI        teacher.py  只调 PP-OCRv6
    consensus.py 多帧一致性      segment.py  segment 生命周期
    review.py   复核判定         dataset.py  纯落盘/hash
    本文件只做编排与"什么时候存哪一帧"。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from .capture import ROIRect, SampleWriter, crop_roi
from .consensus import ConsensusConfig
from .dataset import write_manifest
from .dedupe import find_exact_duplicates
from .review import decide_review, decide_sample_review
from .schemas import (
    REAL_SAMPLE_FIELDS,
    REAL_TRUTH_FIELDS,
    REVIEW_FIELDS,
    SEGMENT_FIELDS,
    TEACHER_CONSENSUS_FIELDS,
    TEACHER_RAW_FIELDS,
)
from .segment import DEFAULT_SEGMENT_GAP_MS, Segment, SegmentTracker

__all__ = ["CollectConfig", "Collector", "CollectResult", "write_real_dataset"]

#: 数据集目录前缀（与合成 roster 区分开）
REAL_DATASET_PREFIX = "LiveLens-OCR-Dataset-REAL"


@dataclass
class CollectConfig:
    format_id: str
    roi: ROIRect
    dataset_dir: str
    session_id: str
    #: 人工输入的当前真实房间码（§4 步骤 4）。None = 未输入 -> unresolved
    ground_truth: Optional[str] = None
    #: 采样策略（§5）—— 全部来自 CLI，代码里不写死
    stable_interval_ms: int = 500
    burst_size: int = 10
    burst_interval_ms: int = 100
    #: 其它
    max_samples: int = 0            # 0 = 不限
    duration_s: float = 0.0         # 0 = 不限
    #: 连续读不到帧多少次就收工。流断了/源耗尽时不能无限空转。
    max_empty_reads: int = 300
    gap_ms: int = DEFAULT_SEGMENT_GAP_MS
    switch_votes: int = 2
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)


@dataclass
class CollectResult:
    dataset_dir: str
    session_id: str
    format_id: str
    samples: int
    segments: int
    bursts: int
    elapsed_s: float
    teacher_errors: int
    dataset_json: dict = field(default_factory=dict)


class Collector:
    def __init__(self, config: CollectConfig, source, teacher, log=print):
        self.cfg = config
        self.source = source
        self.teacher = teacher
        self.log = log
        self.writer = SampleWriter(config.dataset_dir)
        self.tracker = SegmentTracker(
            config.session_id, config.format_id, gap_ms=config.gap_ms,
            switch_votes=config.switch_votes, config=config.consensus,
            ground_truth=config.ground_truth)
        self.rows: List[dict] = []          # teacher_raw 的行
        self.bursts = 0
        self.teacher_errors = 0

    # ------------------------------------------------------------------
    def _save(self, roi_image: np.ndarray, ts_ms: int, reading,
               sequence: bool) -> None:
        sid = "%s-%s-%06d" % (self.cfg.format_id, self.cfg.session_id,
                              self.writer.count + 1)
        rel = self.writer.write(roi_image)
        self.rows.append({
            "sample_id": sid, "segment_id": "",
            "image": rel, "timestamp_ms": ts_ms,
            "sequence": "burst" if sequence else "stable",
            "raw_text": reading.raw_text, "normalized_text": reading.normalized_text,
            "score": "" if reading.score is None else round(reading.score, 4),
            "ok": int(reading.ok), "error": reading.error,
            "elapsed_ms": round(reading.elapsed_ms, 2),
        })
        self.tracker.attach(sid)

    def _read_roi(self, image: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(crop_roi(image, self.cfg.roi))

    # ------------------------------------------------------------------
    def run(self) -> CollectResult:
        cfg = self.cfg
        os.makedirs(cfg.dataset_dir, exist_ok=True)
        t_start = time.perf_counter()
        next_stable = 0.0
        last_ts_ms = 0
        empty_reads = 0
        self.log("开始采集: format=%s session=%s roi=%s"
                 % (cfg.format_id, cfg.session_id, cfg.roi.as_tuple()))
        while True:
            if cfg.duration_s and time.perf_counter() - t_start >= cfg.duration_s:
                break
            if cfg.max_samples and self.writer.count >= cfg.max_samples:
                break
            frame = self.source.read()
            if frame is None:
                empty_reads += 1
                if empty_reads >= cfg.max_empty_reads:
                    self.log("  连续 %d 次没有读到帧，停止采集（帧源结束或流断开）"
                             % empty_reads)
                    break
                time.sleep(0.01)
                continue
            empty_reads = 0
            ts_ms = int((time.perf_counter() - t_start) * 1000)

            image = self._read_roi(frame)
            if image.size == 0:
                continue
            reading = self.teacher.infer(image)      # 只给图像
            if not reading.ok and reading.error:
                self.teacher_errors += 1

            from .code_generator import validate_code
            code = reading.normalized_text or None
            valid = bool(code) and validate_code(cfg.format_id, code)

            closed = self.tracker.observe(code, ts_ms, valid)
            if closed is not None:
                # 房间码刚变化 -> 采一段连续帧做序列（§5 sequence capture）
                self.log("  segment %s 结束 (%d 样本) -> 采 burst %d 帧"
                         % (closed.segment_id, closed.sample_count, cfg.burst_size))
                self.bursts += 1
                for _ in range(max(1, cfg.burst_size)):
                    f2 = self.source.read()
                    if f2 is None:
                        time.sleep(cfg.burst_interval_ms / 1000.0)
                        continue
                    img2 = self._read_roi(f2)
                    if img2.size == 0:
                        continue
                    r2 = self.teacher.infer(img2)
                    ts2 = int((time.perf_counter() - t_start) * 1000)
                    self._save(img2, ts2, r2, sequence=True)
                    c2 = r2.normalized_text or None
                    self.tracker.observe(c2, ts2,
                                         bool(c2) and validate_code(cfg.format_id, c2))
                    time.sleep(cfg.burst_interval_ms / 1000.0)
                next_stable = time.perf_counter() + cfg.stable_interval_ms / 1000.0
                continue

            # 稳定采样（§5 independent sample）
            now = time.perf_counter()
            if ts_ms - last_ts_ms >= 0 and now >= next_stable:
                self._save(image, ts_ms, reading, sequence=False)
                next_stable = now + cfg.stable_interval_ms / 1000.0
                last_ts_ms = ts_ms
                if self.writer.count % 10 == 0:
                    self.log("  已采集 %d 帧" % self.writer.count)

        self.tracker.close()
        return CollectResult(
            dataset_dir=cfg.dataset_dir, session_id=cfg.session_id,
            format_id=cfg.format_id, samples=self.writer.count,
            segments=len(self.tracker.closed), bursts=self.bursts,
            elapsed_s=time.perf_counter() - t_start,
            teacher_errors=self.teacher_errors)


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------
def _write_csv(path: str, fields: Sequence[str], rows: Sequence[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields), lineterminator="\r\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def teacher_audit(sample_rows) -> dict:
    """§15：只有存在**独立人工真值**的样本才计入审计。

    真值通过参数传入（由 truth.csv 决定），Teacher 侧看不到它。
    """
    from collections import defaultdict
    from .code_generator import validate_code
    buckets = defaultdict(lambda: {"n": 0, "exact": 0, "chars": 0.0,
                                   "no_read": 0, "wrong": 0, "format_invalid": 0})
    for r in sample_rows:
        gt = r.get("_gt") or ""
        if not gt:
            continue                      # 没有人工真值 -> 不进分母
        fmt = r["format_id"]
        b = buckets[fmt]
        b["n"] += 1
        got = r["normalized_text"]
        if not validate_code(fmt, got):
            b["format_invalid"] += 1
        if not got:
            b["no_read"] += 1
        elif got == gt:
            b["exact"] += 1
        else:
            b["wrong"] += 1
        b["chars"] += max(0.0, 1.0 - _lev(got, gt) / max(1, len(gt)))
    out = {}
    tot = {"n": 0, "exact": 0, "chars": 0.0, "no_read": 0, "wrong": 0,
           "format_invalid": 0}
    for fmt, b in sorted(buckets.items()):
        out[fmt] = {
            "samples": b["n"],
            "exact_match": b["exact"],
            "exact_rate": round(b["exact"] / b["n"], 4) if b["n"] else 0.0,
            "character_accuracy": round(b["chars"] / b["n"], 4) if b["n"] else 0.0,
            "no_read": b["no_read"], "wrong": b["wrong"],
            "format_invalid": b["format_invalid"],
        }
        for k in ("n", "exact", "chars", "no_read", "wrong", "format_invalid"):
            tot[k] += b[k]
    out["_total"] = {
        "samples": tot["n"],
        "exact_match": tot["exact"],
        "exact_rate": round(tot["exact"] / tot["n"], 4) if tot["n"] else 0.0,
        "character_accuracy": round(tot["chars"] / tot["n"], 4) if tot["n"] else 0.0,
        "no_read": tot["no_read"], "wrong": tot["wrong"],
        "format_invalid": tot["format_invalid"],
    }
    return out


def write_real_dataset(dataset_dir: str,
                       config: "CollectConfig",
                       rows: List[dict],
                       segments: List[Segment],
                       result: Optional["CollectResult"] = None,
                       timestamp: Optional[_dt.datetime] = None) -> dict:
    """把一次采集会话落成完整数据集，返回 dataset.json 的内容。"""
    ts = timestamp or _dt.datetime.now()
    seg_of_sample: Dict[str, Segment] = {}
    for seg in segments:
        for sid in seg.sample_ids:
            seg_of_sample[sid] = seg
    for r in rows:
        seg = seg_of_sample.get(r["sample_id"])
        r["segment_id"] = seg.segment_id if seg else ""

    # ---- samples.csv：**不含答案** ----
    # format_id / session_id 来自本次采集的 config（teacher_raw 行里没有它们），
    # 其余字段来自行本身。绝不从这里带出任何答案。
    sample_rows = []
    for r in rows:
        sample_rows.append({
            "sample_id": r["sample_id"], "image": r["image"],
            "format_id": config.format_id, "session_id": config.session_id,
            "segment_id": r["segment_id"], "timestamp_ms": r["timestamp_ms"],
        })
    _write_csv(os.path.join(dataset_dir, "samples.csv"),
               REAL_SAMPLE_FIELDS, sample_rows)

    # ---- truth.csv：答案只在这里；人工没给就是 unresolved ----
    # **每个样本都必须有一行**。早先这里是"按 segment 展开"，结果一个样本都没
    # 归入 segment 时（Teacher 读出的东西不满足任何格式）truth.csv 会只剩表头，
    # 样本被静默丢掉 —— 这是实测到的缺陷，已改成按样本遍历。
    truth_rows = []
    for r in rows:
        seg = seg_of_sample.get(r["sample_id"])
        gt = (seg.ground_truth if seg else "") or ""
        truth_rows.append({
            "sample_id": r["sample_id"],
            "ground_truth": gt,
            "label_source": "manual" if gt else "unresolved",
            "review_status": "",
        })
    gt_by_sample = {t["sample_id"]: t["ground_truth"] for t in truth_rows}

    # ---- teacher_raw.csv（原样保存） ----
    _write_csv(os.path.join(dataset_dir, "teacher_raw.csv"),
               TEACHER_RAW_FIELDS, rows)

    # ---- teacher_consensus.csv / segments.csv ----
    cons_rows, seg_rows = [], []
    for seg in segments:
        c = seg.consensus
        if c is None:
            continue
        row = {"session_id": seg.session_id, "segment_id": seg.segment_id,
               "format_id": seg.format_id}
        row.update(c.as_dict())
        cons_rows.append(row)
        seg_rows.append(seg.as_dict())
    _write_csv(os.path.join(dataset_dir, "teacher_consensus.csv"),
               TEACHER_CONSENSUS_FIELDS, cons_rows)
    _write_csv(os.path.join(dataset_dir, "segments.csv"),
               SEGMENT_FIELDS, seg_rows)

    # ---- 去重（只报告，不删除） ----
    paths = [os.path.join(dataset_dir, r["image"].replace("/", os.sep))
             for r in rows]
    exact = find_exact_duplicates(paths)
    exact_map = {p: h for h, v in exact.items() for p in v}

    # ---- review_queue.csv ----
    review_rows, review_by_sample = [], {}
    for seg in segments:
        d = decide_review(seg, seg.consensus, config=config.consensus)
        if not d.needed:
            continue
        raw = _dominant_raw(rows, seg)
        for sid in seg.sample_ids:
            row = _sample_row(rows, sid)
            review_rows.append({
                "sample_id": sid, "image": row["image"] if row else "",
                "segment_id": seg.segment_id,
                "ground_truth": seg.ground_truth or "",
                "teacher_consensus":
                    (seg.consensus.code or "") if seg.consensus else "",
                "teacher_raw": raw, "reason": d.reason,
                "review_status": d.review_status,
            })
            review_by_sample[sid] = d.review_status

    # 未归入任何 segment 的样本（Teacher 读法不满足目标格式 / 全程无有效读法）
    # 同样必须进复核队列 —— 否则它们既没有真值也没有人工复核提示，等于消失。
    for r in rows:
        if seg_of_sample.get(r["sample_id"]) is not None:
            continue
        if r["sample_id"] in review_by_sample:
            continue
        review_rows.append({
            "sample_id": r["sample_id"], "image": r["image"],
            "segment_id": "", "ground_truth": "",
            "teacher_consensus": "",
            "teacher_raw": r["raw_text"],
            "reason": "no_segment" if r["ok"] else "teacher_no_result",
            "review_status": "pending",
        })
        review_by_sample[r["sample_id"]] = "pending"

    burst_ids = {r["sample_id"] for r in rows if r["sequence"] == "burst"}
    for r in rows:
        p = os.path.join(dataset_dir, r["image"].replace("/", os.sep))
        sd = decide_sample_review(boundary=r["sample_id"] in burst_ids,
                                  exact_dup=p in exact_map)
        if sd.needed and r["sample_id"] not in review_by_sample:
            review_rows.append({
                "sample_id": r["sample_id"], "image": r["image"],
                "segment_id": r["segment_id"], "ground_truth": "",
                "teacher_consensus": "", "teacher_raw": r["normalized_text"],
                "reason": sd.reason, "review_status": sd.review_status,
            })
            review_by_sample[r["sample_id"]] = sd.review_status
    _write_csv(os.path.join(dataset_dir, "review_queue.csv"),
               REVIEW_FIELDS, review_rows)

    for t in truth_rows:
        t["review_status"] = review_by_sample.get(t["sample_id"], "ok")
    _write_csv(os.path.join(dataset_dir, "truth.csv"),
               REAL_TRUTH_FIELDS, truth_rows)

    # ---- §15 Teacher 审计 ----
    # teacher_raw 行里没有 format_id（它不属于"Teacher 原始输出"），
    # 审计时从 config 补上；_gt 也只在这里注入，供审计用。
    audited = [dict(r, format_id=config.format_id,
                    _gt=gt_by_sample.get(r["sample_id"], "")) for r in rows]
    audit = teacher_audit(audited)

    meta = {
        "dataset_kind": "real",
        "dataset_name": os.path.basename(dataset_dir.rstrip("\\/")),
        "created_at": ts.isoformat(timespec="seconds"),
        "generator": {"name": "LiveLens OCR Dataset Collector",
                      "phase": "dataset-collector", "version": "0.2.0"},
        "session": {
            "session_id": config.session_id,
            "format_id": config.format_id,
            "roi": list(config.roi.as_tuple()),
            "ground_truth_given": bool(config.ground_truth),
            "elapsed_s": round(result.elapsed_s, 1) if result else None,
            "teacher_errors": result.teacher_errors if result else None,
        },
        "sampling": {
            "stable_interval_ms": config.stable_interval_ms,
            "burst_size": config.burst_size,
            "burst_interval_ms": config.burst_interval_ms,
        },
        "consensus_config": config.consensus.as_dict(),
        "counts": {
            "samples": len(rows),
            "segments": len(segments),
            "bursts": result.bursts if result else None,
            "review_items": len(review_rows),
            "teacher_no_result_frames": sum(1 for r in rows if not r["ok"]),
        },
        "labeling_contract": {
            "ground_truth": "仅来自采集时的人工输入（independent manual）",
            "teacher_raw": "PP-OCRv6 逐帧原始输出（审计证据，不是真值）",
            "teacher_consensus": "PP-OCRv6 多帧汇总（预标注，不是真值）",
            "rule": "Teacher 结果永远不会自动成为 ground_truth",
        },
        "leak_guard": {
            "samples_csv_contains_room_code": False,
            "teacher_input": "image only",
        },
        "schemas": {
            "samples.csv": list(REAL_SAMPLE_FIELDS),
            "truth.csv": list(REAL_TRUTH_FIELDS),
            "teacher_raw.csv": list(TEACHER_RAW_FIELDS),
            "teacher_consensus.csv": list(TEACHER_CONSENSUS_FIELDS),
            "segments.csv": list(SEGMENT_FIELDS),
            "review_queue.csv": list(REVIEW_FIELDS),
        },
        "teacher_audit": audit,
        "dedupe": {
            "exact_duplicate_groups": len(exact),
            "exact_duplicate_files": sorted(p for v in exact.values() for p in v),
            "policy": "只报告，不自动删除",
        },
        "manifest": "manifest.sha256",
    }
    with open(os.path.join(dataset_dir, "dataset.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    with open(os.path.join(dataset_dir, "README.md"), "w",
              encoding="utf-8") as fh:
        fh.write(_readme(meta))
    write_manifest(dataset_dir)
    return meta


def _sample_row(rows, sid):
    for r in rows:
        if r["sample_id"] == sid:
            return r
    return None


def _dominant_raw(rows, seg) -> str:
    for r in rows:
        if r["segment_id"] == seg.segment_id and r["normalized_text"]:
            return r["raw_text"]
    return ""


README_TPL = """# {dataset_name}

LiveLens **real live-stream ROI** dataset, collected by
`tools/ocr_dataset_generator`.

- collected_at: {created_at}
- session: `{session_id}`   format: `{format_id}`   roi: `{roi}`
- samples: **{samples}** real ROI images (pre-labelled by PP-OCRv6 Teacher)

## Labelling contract (important)

| column | source | is it ground truth |
|---|---|---|
| `truth.csv: ground_truth` | **independent manual input at capture time** | YES |
| `teacher_consensus.csv` | PP-OCRv6 multi-frame aggregation | NO (pre-label) |
| `teacher_raw.csv` | PP-OCRv6 per-frame raw output | NO (audit evidence) |

**A Teacher result never becomes ground_truth automatically.** A room code with
no manual input is written as `label_source = unresolved` and goes to
`review_queue.csv`.

## Leak guard

- `samples.csv` has **no** `room_code` / `ground_truth` column, so a benchmark
  reading it cannot see the answer.
- The Teacher adapter signature is `infer(image)` - it accepts no expected value.

## Files

| file | content |
|---|---|
| `images/` | real ROI PNGs |
| `samples.csv` | {sample_fields} |
| `truth.csv` | {truth_fields} |
| `teacher_raw.csv` | per-frame Teacher output |
| `teacher_consensus.csv` | per-segment consensus stats |
| `segments.csv` | segment lifecycle |
| `review_queue.csv` | items needing human review |
| `manifest.sha256` | SHA-256 of every file except itself |
"""


def _readme(meta) -> str:
    s = meta["session"]
    return README_TPL.format(
        dataset_name=meta["dataset_name"], created_at=meta["created_at"],
        session_id=s["session_id"], format_id=s["format_id"], roi=s["roi"],
        samples=meta["counts"]["samples"],
        sample_fields=", ".join("`%s`" % x for x in REAL_SAMPLE_FIELDS),
        truth_fields=", ".join("`%s`" % x for x in REAL_TRUTH_FIELDS))
