# -*- coding: utf-8 -*-
"""
数据集落盘（Phase: Dataset Generator）。

产出结构：
    LiveLens-OCR-Dataset-YYYYMMDD-HHMMSS/
    ├── images/            （本阶段为空，只放 .gitkeep；真实图片下一阶段接入）
    ├── samples.csv
    ├── truth.csv
    ├── dataset.json
    ├── manifest.sha256
    └── README.md

重要边界
    **本阶段不生成任何合成图片**。samples.csv 里的 image 列是**约定的目标路径**，
    下一阶段由真实采集/Teacher 流程把图片放进去。这样 metadata schema 先冻结，
    下游可以照着写，而不会因为"先随便生成点图"把 schema 带偏。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .code_generator import GenerationPlan
from .dedupe import find_exact_duplicates, sha256_file
from .schemas import (
    FORMATS,
    SAMPLE_FIELDS,
    SAMPLE_RESERVED_FIELDS,
    TRUTH_FIELDS,
)

__all__ = [
    "DATASET_PREFIX",
    "SAMPLE_INTERVAL_MS",
    "create_dataset",
    "build_sample_rows",
    "build_truth_rows",
    "walk_dataset_files",
    "write_manifest",
    "verify_manifest",
    "DatasetResult",
]

#: 目录名前缀。带时间戳，天然不会覆盖上一批。
DATASET_PREFIX = "LiveLens-OCR-Dataset"

#: 同一房间码内相邻样本的时间戳间隔（毫秒）。
#: 这只是**约定**：下一阶段接真实采集时，timestamp_ms 应改成真实采样时刻；
#: 本阶段用固定步长是为了让 metadata 完全可复现。
SAMPLE_INTERVAL_MS = 100


@dataclass
class DatasetResult:
    root: str
    plan: GenerationPlan
    sample_count: int
    truth_count: int
    manifest_path: str
    dataset_json: dict = field(default_factory=dict)

    def print_table(self, out=print) -> None:
        out("%-12s %-10s %-10s" % ("Format", "Codes", "Samples"))
        for f in self.plan.formats:
            out("%-12s %-10d %-10d" % (f.label, f.unique_code_count, f.sample_count))
        out("-" * 32)
        out("%-12s %-10d %-10d" % ("Total", self.plan.total_unique_codes,
                                   self.plan.total_samples))


def build_sample_rows(plan: GenerationPlan) -> List[dict]:
    """把配额计划展开成 samples.csv 的行。

    sample_id 形如 `aaa123-ABC123-00`：格式 / 房间码 / 序号都在里面，
    单看 id 就能定位到源，不需要再查表。
    """
    rows: List[dict] = []
    for fmt_plan in plan.formats:
        for code in fmt_plan.codes:
            for k in range(fmt_plan.samples_per_code):
                sid = "%s-%s-%02d" % (fmt_plan.format_id, code, k)
                rows.append({
                    "sample_id": sid,
                    "image": "images/%s.png" % sid,
                    "format_id": fmt_plan.format_id,
                    "room_code": code,
                    "session_id": "%s:%s" % (fmt_plan.format_id, code),
                    "timestamp_ms": k * SAMPLE_INTERVAL_MS,
                })
    return rows


def build_truth_rows(sample_rows: Sequence[dict]) -> List[dict]:
    """真值表。

    码是生成器产出的，真值**由构造确定**，不是 OCR 识别结果，
    因此 validation_status 一律 confirmed、source 一律 generator。
    """
    return [{
        "sample_id": r["sample_id"],
        "image": r["image"],
        "format_id": r["format_id"],
        "room_code": r["room_code"],
        "validation_status": "confirmed",
        "source": "generator",
    } for r in sample_rows]


def _write_csv(path: str, fields: Sequence[str], rows: Sequence[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields), lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)


def walk_dataset_files(root: str, exclude: Sequence[str] = ("manifest.sha256",)
                       ) -> List[Tuple[str, str]]:
    """返回 [(相对路径(正斜杠), 绝对路径)]，按相对路径排序。"""
    ex = {e.replace("\\", "/") for e in exclude}
    out: List[Tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel in ex:
                continue
            out.append((rel, full))
    return sorted(out)


def write_manifest(root: str,
                   exclude: Sequence[str] = ("manifest.sha256",)) -> str:
    """写 manifest.sha256：每行 `<sha256>  <相对路径>`。

    manifest 自身不参与（否则自指）。dataset.json 会被写进去，
    所以顺序必须是：先写 dataset.json，再算 manifest。
    """
    path = os.path.join(root, "manifest.sha256")
    lines = ["%s  %s" % (sha256_file(full), rel)
             for rel, full in walk_dataset_files(root, exclude)]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def verify_manifest(root: str) -> List[str]:
    """校验 manifest 与磁盘是否一致，返回不一致项（空列表 = 通过）。"""
    path = os.path.join(root, "manifest.sha256")
    if not os.path.isfile(path):
        return ["缺少 manifest.sha256"]
    problems: List[str] = []
    listed = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, _, rel = line.partition("  ")
            listed.add(rel)
            full = os.path.join(root, rel.replace("/", os.sep))
            if not os.path.isfile(full):
                problems.append("manifest 里的文件不存在: %s" % rel)
            elif sha256_file(full) != digest:
                problems.append("SHA-256 不符: %s" % rel)
    for rel, _full in walk_dataset_files(root, ("manifest.sha256",)):
        if rel not in listed:
            problems.append("磁盘上有但 manifest 未记录: %s" % rel)
    return problems


README_TEMPLATE = """# {name}

LiveLens OCR Dataset —— 由 `tools/ocr_dataset_generator` 生成。

- 生成时间：{created_at}
- 随机种子：`{seed}`
- 图片：**尚未包含**。本阶段只冻结 metadata schema，`samples.csv` 的 `image`
  列是约定的目标路径，下一阶段由真实采集/Teacher 流程填入。
- 合法真值：码由生成器产出，真值由构造确定（`truth.csv`，source=generator）。

## 配额

| Format | Regex | Length | Codes | Samples/Code | Samples |
|---|---|---|---|---|---|
{quota_rows}
| **Total** | | | **{total_codes}** | | **{total_samples}** |

## 文件

| 文件 | 说明 |
|---|---|
| `images/` | ROI 图片目录（本阶段为空） |
| `samples.csv` | 样本索引：{sample_fields} |
| `truth.csv` | 真值：{truth_fields} |
| `dataset.json` | 全部 metadata（配额、实际计数、格式定义、去重报告） |
| `manifest.sha256` | 每个文件（不含本文件）的 SHA-256 |

## 预留字段（本阶段未写入 samples.csv）

{sample_reserved}

接真实 Teacher 时再加列，避免现在写死空列把"还没到这一步"和"这一步没数据"混为一谈。

## 格式冻结

仅允许大写 `A-Z` 与数字 `0-9`，字母全部位于数字前，不允许小写、其他符号、
字母数字交错或任何变形格式。
"""


def create_dataset(output_root: str,
                   plan: GenerationPlan,
                   codes_per_format: int,
                   samples_per_code: int,
                   name: Optional[str] = None,
                   timestamp: Optional[_dt.datetime] = None,
                   dedupe_paths: Sequence[str] = ()) -> DatasetResult:
    """按 plan 落盘一份数据集，返回 DatasetResult。"""
    ts = timestamp or _dt.datetime.now()
    dir_name = name or "%s-%s" % (DATASET_PREFIX, ts.strftime("%Y%m%d-%H%M%S"))
    root = os.path.join(output_root, dir_name)
    if os.path.exists(root):
        raise FileExistsError("目标目录已存在，不覆盖: %s" % root)

    os.makedirs(os.path.join(root, "images"), exist_ok=True)
    # 空目录在某些打包/传输路径上会丢，放一个占位文件让它稳定存在
    open(os.path.join(root, "images", ".gitkeep"), "w").close()

    samples = build_sample_rows(plan)
    truth = build_truth_rows(samples)
    _write_csv(os.path.join(root, "samples.csv"), SAMPLE_FIELDS, samples)
    _write_csv(os.path.join(root, "truth.csv"), TRUTH_FIELDS, truth)

    # ---- 去重报告（只报告，不改配额） ----
    dup_groups = find_exact_duplicates(list(dedupe_paths)) if dedupe_paths else {}
    per_format = []
    for f in plan.formats:
        fmt = FORMATS[f.format_id]
        per_format.append({
            "format_id": f.format_id,
            "label": f.label,
            "regex": fmt.pattern(),
            "length": fmt.length,
            "letters": fmt.letters,
            "digits": fmt.digits,
            "code_space": fmt.code_space,
            "unique_code_count": f.unique_code_count,
            "samples_per_code": f.samples_per_code,
            "sample_count": f.sample_count,
        })

    meta = {
        "dataset_name": dir_name,
        "created_at": ts.isoformat(timespec="seconds"),
        "generator": {
            "name": "LiveLens OCR Dataset Generator",
            "phase": "dataset-generator",
            "version": "0.1.0",
        },
        "seed": plan.seed,
        "quotas": {
            "codes_per_format": codes_per_format,
            "samples_per_code": samples_per_code,
        },
        "formats": per_format,
        "totals": {
            "formats": len(plan.formats),
            "unique_codes": plan.total_unique_codes,
            "samples": plan.total_samples,
        },
        "sample_schema": {
            "csv": "samples.csv",
            "fields": list(SAMPLE_FIELDS),
            "reserved_fields": list(SAMPLE_RESERVED_FIELDS),
        },
        "truth_schema": {
            "csv": "truth.csv",
            "fields": list(TRUTH_FIELDS),
        },
        "images": {
            "present": False,
            "note": "本阶段不生成图片；samples.csv 的 image 列是目标路径",
        },
        "dedupe": {
            "sha256_duplicate_groups": len(dup_groups),
            "sha256_duplicate_files": sorted(
                p for v in dup_groups.values() for p in v),
            "perceptual_hash": "reserved（dedupe.perceptual_hash，本阶段未对真实图片运行）",
        },
        "manifest": "manifest.sha256",
    }

    with open(os.path.join(root, "dataset.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    quota_rows = "\n".join(
        "| %s | `%s` | %d | %d | %d | %d |"
        % (p["label"], p["regex"], p["length"], p["unique_code_count"],
           p["samples_per_code"], p["sample_count"]) for p in per_format)
    readme = README_TEMPLATE.format(
        name=dir_name, created_at=meta["created_at"], seed=plan.seed,
        quota_rows=quota_rows, total_codes=plan.total_unique_codes,
        total_samples=plan.total_samples,
        sample_fields=", ".join("`%s`" % x for x in SAMPLE_FIELDS),
        truth_fields=", ".join("`%s`" % x for x in TRUTH_FIELDS),
        sample_reserved=", ".join("`%s`" % x for x in SAMPLE_RESERVED_FIELDS),
    )
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(readme)

    manifest = write_manifest(root)
    return DatasetResult(root=root, plan=plan, sample_count=len(samples),
                         truth_count=len(truth), manifest_path=manifest,
                         dataset_json=meta)
