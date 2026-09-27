# -*- coding: utf-8 -*-
"""单设备 PP-OCRv6 基准：跑真实数据集一遍，把逐帧结果与耗时写成 JSON。

一个进程只测**一个**设备。GPU 与 CPU 各自独立进程，理由：
  1. CPU 隔离（CUDA_VISIBLE_DEVICES=-1）必须在 `import paddle` 之前生效，
     而 paddle 一旦在进程里初始化过，就没法再"假装看不见显卡"；
  2. GPU/CPU 的显存与线程池状态互不影响，启动时间才是干净的数字。

测什么（两次遍历，互不干扰）
----------------------------
  Pass 1 「原始读法」：engine.recognize(image) —— 一次推理一次输出，
      用的是数据集 ground_truth 的同一口径（PP-OCRv6 原始读法）。
      **延迟统计只取这一遍**：它就是生产 300ms 预算里那一次推理。
  Pass 2 「校验输出」：RoomCodeRecognizer.recognize_frame(image) ——
      生产后处理（严格校验 / 逐位纠正 / 仲裁）原样跑一遍，得到最终房间号。
      它自己会再推理一次，因此**不计入延迟**（否则会把一次推理和
      "一次推理 + 可能的放大复看"混成一个数）。

图像在计时前一次性解码并缓存在内存里：解码不是 OCR 耗时，
且两份缓存保证 GPU 与 CPU 看到的是**逐字节相同**的输入。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.ocr_cpu_bench import dataset_adapter as ds                    # noqa: E402
from tools.ocr_cpu_bench import device_engine as de                       # noqa: E402

SCHEMA = "livelens.ocr_cpu_bench.device_run/1"


def percentile(values: List[float], q: float) -> Optional[float]:
    """线性插值分位数（与 numpy.percentile 默认口径一致）。空列表返回 None。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def latency_stats(values: List[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_ms": round(statistics.fmean(values), 3),
        "min_ms": round(min(values), 3),
        "p50_ms": round(percentile(values, 0.50), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "p99_ms": round(percentile(values, 0.99), 3),
        "max_ms": round(max(values), 3),
        "stdev_ms": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PP-OCRv6 单设备基准（Phase 1 CPU 可行性验证）")
    p.add_argument("--device", required=True, choices=("cpu", "gpu"))
    p.add_argument("--dataset", default=None, help="数据集目录；默认自动搜索")
    p.add_argument("--out", required=True, help="结果 JSON 输出路径")
    p.add_argument("--warmup", type=int, default=10, help="计时前的预热推理次数")
    p.add_argument("--rounds", type=int, default=3, help="数据集重复轮数（延迟样本数 = 样本数 x 轮数）")
    p.add_argument("--limit", type=int, default=0, help="只取前 N 个样本（0 = 全部）")
    p.add_argument("--omp-threads", default=None,
                   help="强制 OMP_NUM_THREADS（必须在 import paddle 之前生效）；"
                        "缺省=不动，保持进程继承值（生产口径）")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    t_process = time.perf_counter()

    # 环境必须在**任何东西 import paddle 之前**定好（OpenMP 在加载时读线程数）。
    process_env = de.prepare_process(args.device, args.omp_threads)

    dataset_dir = ds.discover_dataset(args.dataset)
    samples = [s for s in ds.load_samples(dataset_dir) if s.usable]
    if args.limit:
        samples = samples[:args.limit]
    if not samples:
        print("[bench] 数据集里没有可用的带图样本", file=sys.stderr)
        return 2

    print(f"[bench] device={args.device} dataset={dataset_dir} samples={len(samples)}",
          file=sys.stderr)

    # 解码并缓存输入（不计入 OCR 耗时；GPU/CPU 用同一份像素）。
    t_decode = time.perf_counter()
    images = [s.load_image() for s in samples]
    decode_ms = (time.perf_counter() - t_decode) * 1000.0

    engine, patch_info = de.build_engine(args.device)
    load_ms = engine.init_ms
    isolation = de.verify_isolation(args.device)
    print("[bench] 隔离核对: %s" % json.dumps(isolation, ensure_ascii=False),
          file=sys.stderr)

    # 预热：单张推理的首调用含 oneDNN/算子初始化，必须排除在统计之外。
    warmup_ms: List[float] = []
    for i in range(max(0, args.warmup)):
        img = images[i % len(images)]
        t0 = time.perf_counter()
        engine.recognize(img)
        warmup_ms.append((time.perf_counter() - t0) * 1000.0)
    warmup_total_ms = sum(warmup_ms)
    print("[bench] 预热 %d 次: 首次 %.1fms, 末次 %.1fms"
          % (args.warmup, warmup_ms[0] if warmup_ms else -1,
             warmup_ms[-1] if warmup_ms else -1), file=sys.stderr)

    # ---- Pass 1: 原始读法 + 延迟 -------------------------------------
    raw_texts: List[str] = []
    call_ms: List[float] = []
    per_call: List[dict] = []
    for rnd in range(max(1, args.rounds)):
        for idx, img in enumerate(images):
            t0 = time.perf_counter()
            try:
                text = engine.recognize(img)
                err = None
            except Exception as e:  # noqa: BLE001 - 逐帧失败要记录，不能整轮中断
                text, err = "", "%s: %s" % (type(e).__name__, e)
            dt = (time.perf_counter() - t0) * 1000.0
            call_ms.append(dt)
            per_call.append({"round": rnd, "index": idx, "ms": round(dt, 3), "error": err})
            if rnd == 0:
                raw_texts.append(text)
        print("[bench] pass1 round %d/%d 完成 (累计 %.1fs)"
              % (rnd + 1, args.rounds, sum(call_ms) / 1000.0), file=sys.stderr)

    # ---- Pass 2: 生产后处理（严格校验 -> 最终房间号） ------------------
    from ocr.room_code import RoomCodeRecognizer

    recognizers: Dict[str, RoomCodeRecognizer] = {}
    frames: List[dict] = []
    for idx, (sample, img) in enumerate(zip(samples, images)):
        rec = recognizers.get(sample.profile_id)
        if rec is None:
            rec = RoomCodeRecognizer(engine, profile=sample.profile_id)
            recognizers[sample.profile_id] = rec
        code, corrections, case_fold, conflict, err = None, 0, 0, False, None
        try:
            cand = rec.recognize_frame(img)
            if cand is not None:
                code = cand.code
                corrections = cand.correction_count
                case_fold = cand.case_fold_count
            conflict = rec.last_conflict is not None
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
        raw = raw_texts[idx] if idx < len(raw_texts) else ""
        frames.append({
            "sample_id": sample.sample_id,
            "image": os.path.basename(sample.image_path or ""),
            "ground_truth": sample.ground_truth,
            "profile_id": sample.profile_id,
            "format_id": sample.format_id,
            "raw_text": raw,
            "code": code,
            "corrections": corrections,
            "case_fold": case_fold,
            "conflict": conflict,
            "error": err,
        })

    result = {
        "schema": SCHEMA,
        "device": args.device,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": de.model_fingerprint(engine.model_dir),
        "environment": de.environment_fingerprint(),
        "isolation": isolation,
        "process_env": process_env,
        "engine_patch": patch_info,
        "backend_status": engine.backend_status,
        "dataset": ds.dataset_fingerprint(dataset_dir, samples),
        "config": {
            "warmup": args.warmup,
            "rounds": args.rounds,
            "limit": args.limit,
            "omp_threads": args.omp_threads,
        },
        "timing": {
            "load_ms": round(load_ms, 1) if load_ms is not None else None,
            "process_to_ready_ms": round((time.perf_counter() - t_process) * 1000.0, 1),
            "decode_ms_total": round(decode_ms, 1),
            "warmup_calls_ms": [round(x, 3) for x in warmup_ms],
            "warmup_total_ms": round(warmup_total_ms, 1),
            "infer": latency_stats(call_ms),
            "per_call": per_call,
        },
        "frames": frames,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    st = result["timing"]["infer"]
    print("[bench] 完成: load=%.0fms warmup=%.0fms infer P50=%.1f P95=%.1f P99=%.1f max=%.1f (n=%d)"
          % (result["timing"]["load_ms"] or -1, warmup_total_ms, st.get("p50_ms", -1),
             st.get("p95_ms", -1), st.get("p99_ms", -1), st.get("max_ms", -1), st.get("count", 0)),
          file=sys.stderr)
    print("[bench] 结果已写入 %s" % args.out, file=sys.stderr)

    engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
