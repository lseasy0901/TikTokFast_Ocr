# -*- coding: utf-8 -*-
"""
数据集校验（Phase 2: Dataset Collector）。

两类检查：
    1. 完整性 —— manifest.sha256 对得上、必备文件在、解析得出。
    2. **防泄漏** —— samples.csv 不得含答案；Teacher 不得依赖期望值；
       truth 的 label_source 不得出现 teacher。
"""

from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Tuple

from .dataset import verify_manifest
from .schemas import (
    LABEL_SOURCES,
    REAL_SAMPLE_FIELDS,
    REAL_TRUTH_FIELDS,
)

__all__ = ["verify_dataset", "REQUIRED_FILES"]

REQUIRED_FILES = (
    "images", "samples.csv", "truth.csv", "teacher_raw.csv",
    "teacher_consensus.csv", "segments.csv", "review_queue.csv",
    "dataset.json", "manifest.sha256", "README.md",
)

#: 绝不允许出现在 samples.csv 列名里的东西（大小写不敏感子串）
FORBIDDEN_SAMPLE_COLUMNS = ("ground_truth", "room_code", "code", "label",
                            "truth", "answer", "teacher")


def _read_csv(path: str) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def verify_dataset(dataset_dir: str) -> Tuple[List[str], Dict[str, object]]:
    problems: List[str] = []
    info: Dict[str, object] = {"dataset_dir": dataset_dir}

    if not os.path.isdir(dataset_dir):
        return ["目录不存在: %s" % dataset_dir], info

    for name in REQUIRED_FILES:
        if not os.path.exists(os.path.join(dataset_dir, name)):
            problems.append("缺少 %s" % name)

    # ---- manifest ----
    if os.path.isfile(os.path.join(dataset_dir, "manifest.sha256")):
        problems += verify_manifest(dataset_dir)

    # ---- samples.csv 字段与防泄漏 ----
    sp = os.path.join(dataset_dir, "samples.csv")
    if os.path.isfile(sp):
        rows = _read_csv(sp)
        cols = list(rows[0].keys()) if rows else []
        info["samples"] = len(rows)
        info["sample_columns"] = ",".join(cols)
        if list(cols) != list(REAL_SAMPLE_FIELDS):
            problems.append("samples.csv 列不符，期望 %s，实际 %s"
                            % (list(REAL_SAMPLE_FIELDS), cols))
        lowered = [c.lower() for c in cols]
        for bad in FORBIDDEN_SAMPLE_COLUMNS:
            if any(bad == c or bad in c for c in lowered):
                problems.append("samples.csv 出现疑似答案列: %r" % bad)
        ids = [r["sample_id"] for r in rows]
        if len(set(ids)) != len(ids):
            problems.append("samples.csv 有重复 sample_id")

    # ---- truth.csv 来源必须合法 ----
    tp = os.path.join(dataset_dir, "truth.csv")
    if os.path.isfile(tp):
        rows = _read_csv(tp)
        cols = list(rows[0].keys()) if rows else []
        info["truth"] = len(rows)
        if list(cols) != list(REAL_TRUTH_FIELDS):
            problems.append("truth.csv 列不符，期望 %s，实际 %s"
                            % (list(REAL_TRUTH_FIELDS), cols))
        for r in rows:
            src = (r.get("label_source") or "").strip()
            if src not in LABEL_SOURCES:
                problems.append("truth.csv 出现非法 label_source: %r "
                                "(只允许 %s —— teacher 永远不能是真值来源)"
                                % (src, list(LABEL_SOURCES)))
                break

    # ---- 引用完整性 ----
    if os.path.isfile(sp) and os.path.isfile(tp):
        sids = {r["sample_id"] for r in _read_csv(sp)}
        tids = {r["sample_id"] for r in _read_csv(tp)}
        if sids - tids:
            problems.append("有 %d 个 sample 在 truth.csv 里没有对应行"
                            % len(sids - tids))
        missing = []
        for r in _read_csv(sp):
            p = os.path.join(dataset_dir, r["image"].replace("/", os.sep))
            if not os.path.isfile(p):
                missing.append(r["image"])
        if missing:
            problems.append("有 %d 张图片缺失（例: %s）" % (len(missing), missing[:3]))
        info["images"] = len(sids) - len(missing)

    # ---- dataset.json 的防泄漏自述 ----
    jp = os.path.join(dataset_dir, "dataset.json")
    if os.path.isfile(jp):
        with open(jp, encoding="utf-8") as fh:
            meta = json.load(fh)
        info["dataset_kind"] = meta.get("dataset_kind")
        info["seed"] = meta.get("seed", "-")
        guard = meta.get("leak_guard") or {}
        if guard and guard.get("samples_csv_contains_room_code"):
            problems.append("dataset.json 自述 samples.csv 含 room_code")
    return problems, info
