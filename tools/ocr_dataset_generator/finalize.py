# -*- coding: utf-8 -*-
"""
重新推导数据集的聚合文件（Phase 2: Dataset Collector）。

什么时候用：
    * 采集完了想换 consensus 阈值（不必重采）；
    * 采集时没输入房间码，事后补录人工真值（`--ground-truth` / `--segment-truth`）；
    * 采集过程中断了，想把已落盘的原始文件重新汇总。

输入只有三类**原始文件**：
    samples.csv（谁属于哪个 segment）、teacher_raw.csv（逐帧 Teacher 原文）、
    segments.csv（边界与已有人工真值）。
输出覆盖：teacher_consensus.csv / truth.csv / review_queue.csv /
dataset.json / README.md / manifest.sha256。

**不重新跑 Teacher，也不改图片。**
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional

from .consensus import ConsensusConfig, compute_consensus
from .review import decide_review, decide_sample_review
from .schemas import REVIEW_FIELDS
from .runner import _write_csv, teacher_audit

__all__ = ["finalize_dataset", "load_dataset"]


def _read_csv(path: str) -> List[dict]:
    if not os.path.isfile(path):
        raise FileNotFoundError("缺少文件: %s" % path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_dataset(dataset_dir: str):
    samples = _read_csv(os.path.join(dataset_dir, "samples.csv"))
    raw = _read_csv(os.path.join(dataset_dir, "teacher_raw.csv"))
    segments = _read_csv(os.path.join(dataset_dir, "segments.csv"))
    return samples, raw, segments


class _Seg:
    """finalize 阶段用的轻量 segment（补齐 decide_review 需要的两个属性）。"""

    def __init__(self, row, readings, sample_ids):
        self.segment_id = row.get("segment_id", "")
        self.session_id = row.get("session_id", "")
        self.format_id = row.get("format_id", "")
        self.ground_truth = (row.get("ground_truth") or "").strip() or None
        self.start_timestamp_ms = int(row.get("start_timestamp_ms") or 0)
        self.end_timestamp_ms = int(row.get("end_timestamp_ms") or 0)
        self.sample_ids = sample_ids
        self.readings = readings
        self.consensus = None


def finalize_dataset(dataset_dir: str,
                     consensus_config: Optional[ConsensusConfig] = None,
                     ground_truth_all: Optional[str] = None,
                     segment_truth: Optional[Dict[str, str]] = None) -> dict:
    """重新推导并写回聚合文件，返回新的 dataset.json。"""
    cfg = consensus_config or ConsensusConfig()
    seg_truth = dict(segment_truth or {})
    samples, raw, seg_rows = load_dataset(dataset_dir)

    raw_of = {r["sample_id"]: r for r in raw}
    by_seg: Dict[str, List[str]] = {}
    for s in samples:
        by_seg.setdefault(s["segment_id"], []).append(s["sample_id"])

    segs: List[_Seg] = []
    for row in seg_rows:
        sid = row["segment_id"]
        ids = by_seg.get(sid, [])
        readings = [(raw_of[i]["normalized_text"] or None)
                    for i in ids if i in raw_of]
        seg = _Seg(row, readings, ids)
        override = seg_truth.get(sid) or ground_truth_all
        if override:
            seg.ground_truth = override.strip() or None
        seg.consensus = compute_consensus(readings, seg.format_id, cfg)
        segs.append(seg)

    # ---- consensus / segments.csv ----
    cons_rows, seg_out = [], []
    for seg in segs:
        r = {"session_id": seg.session_id, "segment_id": seg.segment_id,
             "format_id": seg.format_id}
        r.update(seg.consensus.as_dict())
        cons_rows.append(r)
        seg_out.append({
            "session_id": seg.session_id, "segment_id": seg.segment_id,
            "format_id": seg.format_id, "ground_truth": seg.ground_truth or "",
            "truth_status": "confirmed" if seg.ground_truth else "unresolved",
            "start_timestamp_ms": seg.start_timestamp_ms,
            "end_timestamp_ms": seg.end_timestamp_ms,
            "sample_count": len(seg.sample_ids),
            **seg.consensus.as_dict(),
        })
    _write_csv(os.path.join(dataset_dir, "teacher_consensus.csv"),
               list(cons_rows[0].keys()) if cons_rows else
               ["session_id", "segment_id", "format_id"], cons_rows)
    _write_csv(os.path.join(dataset_dir, "segments.csv"),
               list(seg_out[0].keys()) if seg_out else
               ["session_id", "segment_id", "format_id"], seg_out)

    # ---- review_queue ----
    review_rows, review_by_sample = [], {}
    for seg in segs:
        d = decide_review(seg, seg.consensus, config=cfg)
        if not d.needed:
            continue
        dom = next((raw_of[i]["raw_text"] for i in seg.sample_ids
                    if i in raw_of and raw_of[i]["normalized_text"]), "")
        for i in seg.sample_ids:
            review_rows.append({
                "sample_id": i, "image": next(
                    (s["image"] for s in samples if s["sample_id"] == i), ""),
                "segment_id": seg.segment_id,
                "ground_truth": seg.ground_truth or "",
                "teacher_consensus": seg.consensus.code or "",
                "teacher_raw": dom, "reason": d.reason,
                "review_status": "pending"})
            review_by_sample[i] = "pending"
    # 未归入任何 segment 的样本同样要进复核（回归：早先这里会静默丢掉）
    seg_ids = {s["segment_id"] for s in samples}
    for s in samples:
        if s["sample_id"] in review_by_sample:
            continue
        if s["segment_id"] in seg_ids and s["segment_id"]:
            continue
        r = raw_of.get(s["sample_id"], {})
        review_rows.append({
            "sample_id": s["sample_id"], "image": s["image"],
            "segment_id": s["segment_id"], "ground_truth": "",
            "teacher_consensus": "", "teacher_raw": r.get("raw_text", ""),
            "reason": "no_segment" if str(r.get("ok")) == "1" else "teacher_no_result",
            "review_status": "pending"})
        review_by_sample[s["sample_id"]] = "pending"

    burst = {r["sample_id"] for r in raw if r.get("sequence") == "burst"}
    for s in samples:
        sd = decide_sample_review(boundary=s["sample_id"] in burst)
        if sd.needed and s["sample_id"] not in review_by_sample:
            review_rows.append({
                "sample_id": s["sample_id"], "image": s["image"],
                "segment_id": s["segment_id"], "ground_truth": "",
                "teacher_consensus": "", "teacher_raw": "",
                "reason": sd.reason, "review_status": "pending"})
            review_by_sample[s["sample_id"]] = "pending"
    _write_csv(os.path.join(dataset_dir, "review_queue.csv"),
               REVIEW_FIELDS, review_rows)

    # ---- truth.csv ----
    seg_of = {i: seg for seg in segs for i in seg.sample_ids}
    truth_rows = []
    for s in samples:
        i = s["sample_id"]
        seg = seg_of.get(i)
        gt = (seg.ground_truth if seg else "") or ""
        truth_rows.append({
            "sample_id": i, "ground_truth": gt,
            "label_source": "manual" if gt else "unresolved",
            "review_status": review_by_sample.get(i, "ok")})
    _write_csv(os.path.join(dataset_dir, "truth.csv"),
               list(truth_rows[0].keys()) if truth_rows else
               ["sample_id", "ground_truth", "label_source", "review_status"],
               truth_rows)

    # ---- dataset.json（保留原 metadata，只更新变化的部分） ----
    import json
    meta_path = os.path.join(dataset_dir, "dataset.json")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    gt_by_sample = {t["sample_id"]: t["ground_truth"] for t in truth_rows}
    fmt_of = {s["sample_id"]: s["format_id"] for s in samples}
    audited = [dict(r, format_id=fmt_of.get(r["sample_id"], ""),
                    _gt=gt_by_sample.get(r["sample_id"], "")) for r in raw]
    meta["consensus_config"] = cfg.as_dict()
    meta["counts"] = {
        **meta.get("counts", {}),
        "samples": len(samples), "segments": len(segs),
        "review_items": len(review_rows),
        "teacher_no_result_frames": sum(1 for r in raw if str(r.get("ok")) != "1"),
    }
    meta["teacher_audit"] = teacher_audit(audited)
    meta["finalized_at"] = __import__("datetime").datetime.now().isoformat(
        timespec="seconds")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    from .dataset import write_manifest
    write_manifest(dataset_dir)
    return meta
