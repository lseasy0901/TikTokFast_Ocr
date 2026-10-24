# -*- coding: utf-8 -*-
"""
CLI（Phase 1 roster 生成 + Phase 2 真实采集）。

    python -m tools.ocr_dataset_generator --help            # 兼容：合成 roster 生成
    python -m tools.ocr_dataset_generator collect --help    # 真实直播 ROI 采集
    python -m tools.ocr_dataset_generator finalize <dir>
    python -m tools.ocr_dataset_generator verify <dir>

没有子命令时保持 Phase 1 的行为（生成房间码 roster），
这样既有测试与用法不受影响。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from typing import List, Optional, Sequence

from .code_generator import QuotaError, generate_balanced_codes
from .consensus import ConsensusConfig
from .dataset import create_dataset
from .schemas import FORMAT_IDS, FORMATS

__all__ = ["build_parser", "main", "parse_formats", "SUBCOMMANDS"]

SUBCOMMANDS = ("collect", "finalize", "verify")


def parse_formats(value: Optional[str]) -> List[str]:
    """`--formats aaa123,bbbb123` 或重复传多次都接受；空 = 全部 3 种。"""
    if not value:
        return list(FORMAT_IDS)
    ids: List[str] = []
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        if part not in FORMATS:
            raise SystemExit("未知格式 %r；可选: %s" % (part, ", ".join(FORMAT_IDS)))
        if part not in ids:
            ids.append(part)
    return ids or list(FORMAT_IDS)


# ----------------------------------------------------------------------
# Phase 1：roster 生成（保持兼容）
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.ocr_dataset_generator",
        description="LiveLens OCR 数据集工具（roster 生成 + 真实直播 ROI 采集）",
    )
    p.add_argument("--output", default=".", help="数据集父目录（默认当前目录）")
    p.add_argument("--seed", type=int, default=20260927,
                   help="随机种子；相同 seed 必复现（默认 20260927）")
    p.add_argument("--formats", default=None,
                   help="逗号分隔的格式 ID，默认全部: %s" % ",".join(FORMAT_IDS))
    p.add_argument("--codes-per-format", type=int, default=100,
                   help="每种格式的唯一房间码数量（默认 100）")
    p.add_argument("--samples-per-code", type=int, default=20,
                   help="每个房间码的样本数（默认 20）")
    p.add_argument("--name", default=None,
                   help="数据集目录名（默认 LiveLens-OCR-Dataset-<时间戳>）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印配额表，不落盘")
    return p


def _run_roster(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(list(argv))
    formats = parse_formats(args.formats)
    try:
        plan = generate_balanced_codes(
            per_format_codes=args.codes_per_format,
            samples_per_code=args.samples_per_code,
            seed=args.seed, formats=formats)
    except QuotaError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 2

    if args.dry_run:
        print("(dry-run，未落盘)")
        from .dataset import DatasetResult
        DatasetResult(root="", plan=plan, sample_count=plan.total_samples,
                      truth_count=plan.total_samples,
                      manifest_path="").print_table()
        return 0

    try:
        result = create_dataset(
            output_root=args.output, plan=plan,
            codes_per_format=args.codes_per_format,
            samples_per_code=args.samples_per_code, name=args.name)
    except FileExistsError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 3

    print("数据集已生成: %s" % result.root)
    print("")
    result.print_table()
    print("")
    print("seed=%d  samples.csv=%d 行  truth.csv=%d 行"
          % (plan.seed, result.sample_count, result.truth_count))
    print("manifest: %s" % result.manifest_path)
    return 0


# ----------------------------------------------------------------------
# Phase 2：collect / finalize / verify
# ----------------------------------------------------------------------
def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stable-interval-ms", type=int, default=500,
                   help="稳定采样的最小间隔（默认 500）")
    p.add_argument("--burst-size", type=int, default=10,
                   help="房间码变化时的连续帧数（默认 10）")
    p.add_argument("--burst-interval-ms", type=int, default=100,
                   help="burst 内帧间隔（默认 100）")
    p.add_argument("--min-votes", type=int, default=3, help="consensus 最少有效票")
    p.add_argument("--majority", type=float, default=0.80, help="consensus 多数占比下限")
    p.add_argument("--min-consecutive", type=int, default=3, help="consensus 最长连续下限")
    p.add_argument("--gap-ms", type=int, default=3000, help="segment 断裂超时")
    p.add_argument("--switch-votes", type=int, default=2, help="换码需连续几次")


def _build_collect_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="... collect",
                                description="真实直播 ROI 采集 + Teacher 预标注")
    p.add_argument("--format", dest="format_id", required=True,
                   choices=list(FORMAT_IDS), help="目标房间码格式")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="真实直播地址（走项目的 StreamResolver/FFmpegReader）")
    src.add_argument("--replay-dir", help="离线重放目录（必须是真实采集过的 ROI 图）")
    p.add_argument("--roi", required=True, help="识别区域 x,y,w,h")
    p.add_argument("--roi-source", default=None,
                   help="ROI 坐标基于的源尺寸 W,H（与直播尺寸不同时自动换算）")
    p.add_argument("--output", default=".", help="数据集父目录")
    p.add_argument("--session-id", default=None, help="会话 ID（默认按时间生成）")
    p.add_argument("--ground-truth", default=None,
                   help="人工输入的当前真实房间码（独立真值来源；不填则该段 unresolved）")
    p.add_argument("--max-samples", type=int, default=0, help="最多采多少帧（0=不限）")
    p.add_argument("--duration-s", type=float, default=0.0, help="采多久（0=不限）")
    p.add_argument("--use-cuda", action="store_true", help="FFmpeg 解码走 CUDA")
    p.add_argument("--name", default=None, help="数据集目录名")
    _add_common(p)
    return p


def _consensus_of(args) -> ConsensusConfig:
    return ConsensusConfig(min_votes=args.min_votes,
                           majority_ratio=args.majority,
                           min_consecutive=args.min_consecutive)


def _run_collect(argv: Sequence[str]) -> int:
    from .capture import LiveFrameSource, ReplayFrameSource, parse_roi
    from .runner import CollectConfig, Collector, REAL_DATASET_PREFIX, write_real_dataset
    from .teacher import PPOCRTeacher, TeacherError

    args = _build_collect_parser().parse_args(list(argv))

    ok, why = PPOCRTeacher.available()
    if not ok:
        print("[错误] Teacher 不可用: %s" % why, file=sys.stderr)
        return 4

    roi = parse_roi(args.roi)
    if args.roi_source:
        sw, sh = (int(v) for v in args.roi_source.replace("x", ",").split(",")[:2])
        roi.source_width, roi.source_height = sw, sh

    session_id = args.session_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = args.name or "%s-%s" % (REAL_DATASET_PREFIX, session_id)
    dataset_dir = os.path.join(args.output, name)
    if os.path.exists(dataset_dir):
        print("[错误] 目标目录已存在，不覆盖: %s" % dataset_dir, file=sys.stderr)
        return 3

    if args.url:
        try:
            source = LiveFrameSource.open(args.url, use_cuda=args.use_cuda)
        except Exception as exc:  # noqa: BLE001
            print("[错误] 连接直播失败: %s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
            return 5
    else:
        exts = (".png", ".jpg", ".jpeg", ".bmp")
        paths = [os.path.join(args.replay_dir, f)
                 for f in sorted(os.listdir(args.replay_dir))
                 if f.lower().endswith(exts)]
        if not paths:
            print("[错误] replay 目录里没有图片: %s" % args.replay_dir, file=sys.stderr)
            return 5
        print("replay 模式: %d 张真实 ROI 图（不是合成图）" % len(paths))
        source = ReplayFrameSource(paths)

    cfg = CollectConfig(
        format_id=args.format_id, roi=roi, dataset_dir=dataset_dir,
        session_id=session_id, ground_truth=args.ground_truth,
        stable_interval_ms=args.stable_interval_ms,
        burst_size=args.burst_size, burst_interval_ms=args.burst_interval_ms,
        max_samples=args.max_samples, duration_s=args.duration_s,
        gap_ms=args.gap_ms, switch_votes=args.switch_votes,
        consensus=_consensus_of(args))

    teacher = PPOCRTeacher()
    try:
        collector = Collector(cfg, source, teacher)
        try:
            result = collector.run()
        finally:
            source.close()
        meta = write_real_dataset(dataset_dir, cfg, collector.rows,
                                  collector.tracker.closed, result)
    except TeacherError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 4
    finally:
        teacher.close()

    c = meta["counts"]
    print("")
    print("数据集已生成: %s" % dataset_dir)
    print("  samples=%d  segments=%d  bursts=%s  review=%d  teacher_no_result=%d"
          % (c["samples"], c["segments"], c["bursts"], c["review_items"],
             c["teacher_no_result_frames"]))
    print("  ground_truth 来源: %s" % ("人工输入" if args.ground_truth else "无（全部 unresolved）"))
    return 0


def _run_finalize(argv: Sequence[str]) -> int:
    """从已采集的原始文件重新推导 consensus / truth / review / manifest。

    用途：换 consensus 阈值、或采集后补录人工真值，而不必重采。
    """
    from .finalize import finalize_dataset
    p = argparse.ArgumentParser(prog="... finalize",
                                description="重新推导数据集的聚合文件")
    p.add_argument("dataset_dir", help="collect 产出的数据集目录")
    p.add_argument("--ground-truth", default=None,
                   help="给所有 segment 统一补录人工真值")
    p.add_argument("--segment-truth", action="append", default=[],
                   help="按段补录：--segment-truth SEG0001=ABC123（可重复）")
    _add_common(p)
    args = p.parse_args(list(argv))

    seg_truth = {}
    for item in args.segment_truth:
        if "=" not in item:
            print("[错误] --segment-truth 需要 SEG0001=CODE 形式: %r" % item,
                  file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        seg_truth[k.strip()] = v.strip()

    try:
        meta = finalize_dataset(args.dataset_dir, _consensus_of(args),
                                ground_truth_all=args.ground_truth,
                                segment_truth=seg_truth)
    except FileNotFoundError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 3
    c = meta["counts"]
    print("finalize 完成: %s" % args.dataset_dir)
    print("  samples=%d segments=%d review=%d"
          % (c["samples"], c["segments"], c["review_items"]))
    return 0


def _run_verify(argv: Sequence[str]) -> int:
    from .verify import verify_dataset
    p = argparse.ArgumentParser(prog="... verify",
                                description="校验数据集完整性与防泄漏约束")
    p.add_argument("dataset_dir", help="数据集目录")
    args = p.parse_args(list(argv))
    problems, info = verify_dataset(args.dataset_dir)
    for k, v in info.items():
        print("  %-28s %s" % (k, v))
    if problems:
        print("")
        for x in problems:
            print("  [FAIL] %s" % x)
        print("\n校验未通过（%d 项）" % len(problems))
        return 1
    print("\n校验通过")
    return 0


# ----------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        if cmd == "collect":
            return _run_collect(rest)
        if cmd == "finalize":
            return _run_finalize(rest)
        return _run_verify(rest)
    return _run_roster(argv)
