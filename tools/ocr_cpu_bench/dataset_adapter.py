# -*- coding: utf-8 -*-
"""真实数据集适配（只读）。

数据集是 LiveLens 自己的实时采集数据集（`LiveLens-OCR-Dataset-*`），
结构（见其 README.md）：

    dataset.json      元数据（采样间隔、格式分布、标注口径）
    labels.csv        sample_id,ground_truth      <- 标准答案
    samples.csv       sample_id,image,format_id,timestamp_ms
    images/*.png      ROI 裁剪图（BGR，PNG 无损）

本模块**只读**数据集：不写、不改、不规范化、不搬迁任何文件。
如果字段与 benchmark 需要不完全匹配，就在这里做适配，而不是去动数据集。

关于 ground_truth 的口径（重要，直接决定报告里指标的解读方式）：
    数据集 README 写明 `ground_truth = PP-OCRv6 原始读法（测试阶段，无人工标注）`，
    即这批"标准答案"本身是**当前 GPU 生产链路的输出**。因此
    「CPU 对 GT 的准确率」实际上主要度量的是 **CPU 与 GPU 的一致性**，
    而不是绝对识别能力。报告里必须如实这么写，不能当成人工标注的准确率。

图像读取：
    Windows 上 cv2.imread() 对非 ASCII 路径（本机用户名含全角括号）会直接
    失败并返回 None。生产/采集侧用的是 np.fromfile + cv2.imdecode，
    这里沿用同一条路径，保证读出来的像素与采集时一致。
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import cv2
import numpy as np

#: 数据集目录名前缀（采集器固定用这个前缀 + 时间戳）。
DATASET_PREFIX = "LiveLens-OCR-Dataset-"

#: 采集器约定的格式名 -> 房间号长度。用于在 labels.csv 缺 format_id 时兜底。
_FORMAT_ID_LENGTH = {"aaa123": 6, "bbbb123": 7, "aaaa1234": 8}

#: 房间号长度 -> 游戏档。与 ocr/profiles.py 的三个档一一对应：
#:   6 位 LLLDDD   -> valorant        (无畏契约)
#:   7 位 LLLDDDD  -> delta_force     (三角洲行动)
#:   8 位 LLLLDDDD -> delta_force_1v1 (三角洲 1v1 组队码)
_LENGTH_TO_PROFILE = {6: "valorant", 7: "delta_force", 8: "delta_force_1v1"}


class DatasetError(RuntimeError):
    """数据集无法定位或结构不可用。"""


@dataclass(frozen=True)
class Sample:
    """一条已标注样本。image_path 可能不存在（见 missing_reason）。"""

    sample_id: str
    image_path: Optional[str]
    ground_truth: str
    format_id: str
    profile_id: str

    @property
    def usable(self) -> bool:
        return self.image_path is not None

    def load_image(self) -> np.ndarray:
        """读出 ROI 图像（uint8 BGR (H, W, 3)）——与生产 ROI 切片同一格式。

        异常：
            DatasetError: 图像缺失或解码失败。
        """
        if self.image_path is None:
            raise DatasetError(f"样本 {self.sample_id} 没有可用图像")
        return imread_unicode(self.image_path)


def imread_unicode(path: str) -> np.ndarray:
    """非 ASCII 路径安全的读图（np.fromfile + imdecode，UTF-8 路径可用）。"""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        raise DatasetError(f"图像为空或无法读取: {path}")
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise DatasetError(f"图像解码失败: {path}")
    return image


def default_search_roots() -> List[str]:
    """自动搜索数据集时依次查看的根目录（存在才搜）。"""
    roots: List[str] = []
    home = Path(os.path.expanduser("~"))
    roots.append(str(home / "Documents"))
    roots.append(str(home / "Desktop"))
    roots.append(str(home))
    # 项目根：仓库里也可能放过一份数据集。
    roots.append(str(Path(__file__).resolve().parents[2]))
    seen, out = set(), []
    for r in roots:
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)
    return out


def discover_dataset(explicit: Optional[str] = None) -> str:
    """定位数据集目录，返回绝对路径。

    顺序：
        1. explicit（命令行显式指定）
        2. 搜索 default_search_roots() 下的 DATASET_PREFIX* 目录，取**最新**的一个
           （按目录名里的时间戳排序，采集器命名即时间戳，字典序 = 时间序）

    异常：
        DatasetError: 都没找到。
    """
    if explicit:
        if not os.path.isdir(explicit):
            raise DatasetError(f"指定的数据集目录不存在: {explicit}")
        return os.path.abspath(explicit)

    found: List[str] = []
    for root in default_search_roots():
        try:
            for name in os.listdir(root):
                if name.startswith(DATASET_PREFIX) and os.path.isdir(os.path.join(root, name)):
                    found.append(os.path.join(root, name))
        except OSError:
            continue
    if not found:
        raise DatasetError(
            "未找到数据集。请用 --dataset 显式指定 "
            f"{DATASET_PREFIX}<session> 目录。"
        )
    return os.path.abspath(sorted(found)[-1])


def _read_csv(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []
    # utf-8-sig: 采集器写出的 CSV 带 BOM，不剥掉第一列名字会变成 '﻿sample_id'。
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def normalize_gt(text: Optional[str]) -> str:
    """标准答案规范化：去掉空白、转大写。

    只做这一种规范化，且**两个设备用同一条**——否则 GPU/CPU 的差异会被
    规范化规则吃掉或被制造出来。不做任何字符纠正。
    """
    if not text:
        return ""
    return "".join(str(text).split()).upper()


def profile_for(length: int, format_id: str = "") -> str:
    """按房间号长度（或采集器的 format_id）选游戏档。

    长度是权威依据：三个档的格式长度互不相同（6/7/8），
    与 ocr/profiles.py 的注册一致。format_id 只作为长度缺失时的兜底。
    """
    if length in _LENGTH_TO_PROFILE:
        return _LENGTH_TO_PROFILE[length]
    length = _FORMAT_ID_LENGTH.get((format_id or "").strip().lower(), 0)
    if length in _LENGTH_TO_PROFILE:
        return _LENGTH_TO_PROFILE[length]
    # 不猜：交给调用方决定（默认档）。报告里会单列这部分。
    return "auto"


def load_samples(dataset_dir: str) -> List[Sample]:
    """读 labels.csv + samples.csv，返回样本列表（只读）。

    缺图、缺标注的样本不会让整个加载失败：缺图的样本以 image_path=None
    保留在列表里，由调用方计入"不可用"，避免静默丢样本。
    """
    if not os.path.isdir(dataset_dir):
        raise DatasetError(f"数据集目录不存在: {dataset_dir}")

    labels = {row["sample_id"]: row.get("ground_truth", "")
              for row in _read_csv(os.path.join(dataset_dir, "labels.csv"))
              if row.get("sample_id")}
    if not labels:
        raise DatasetError(f"{dataset_dir} 下没有可用的 labels.csv（或缺 ground_truth 列）")

    images_field = {row["sample_id"]: row
                    for row in _read_csv(os.path.join(dataset_dir, "samples.csv"))
                    if row.get("sample_id")}

    samples: List[Sample] = []
    for sample_id, gt_raw in labels.items():
        gt = normalize_gt(gt_raw)
        rel = (images_field.get(sample_id, {}).get("image") or "").strip()
        fmt = (images_field.get(sample_id, {}).get("format_id") or "").strip()
        rel = rel or f"images/{sample_id}.png"
        rel = rel.replace("\\", "/")
        abs_path = os.path.join(dataset_dir, *rel.split("/"))
        samples.append(Sample(
            sample_id=sample_id,
            image_path=abs_path if os.path.isfile(abs_path) else None,
            ground_truth=gt,
            format_id=fmt,
            profile_id=profile_for(len(gt), fmt),
        ))
    samples.sort(key=lambda s: s.sample_id)
    return samples


def dataset_fingerprint(dataset_dir: str, samples: Sequence[Sample]) -> dict:
    """数据集身份快照：路径、样本数、缺图数、标注口径。（只读，不改数据集。）"""
    meta_path = os.path.join(dataset_dir, "dataset.json")
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8-sig") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
    per_length: dict = {}
    for s in samples:
        per_length[str(len(s.ground_truth))] = per_length.get(str(len(s.ground_truth)), 0) + 1
    return {
        "dir": os.path.abspath(dataset_dir),
        "name": os.path.basename(os.path.abspath(dataset_dir)),
        "samples": len(samples),
        "usable_images": sum(1 for s in samples if s.usable),
        "missing_images": [s.sample_id for s in samples if not s.usable],
        "gt_length_histogram": per_length,
        "profiles": sorted({s.profile_id for s in samples}),
        "labeling": meta.get("labeling", {}),
        "sampling": meta.get("sampling", {}),
    }
