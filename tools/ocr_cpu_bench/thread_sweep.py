# -*- coding: utf-8 -*-
"""CPU 线程数敏感性实验：OMP_NUM_THREADS 对 PP-OCRv6 CPU 推理耗时的影响。

为什么单独做这个
----------------
Phase 1 第一次量 CPU 时拿到 p50≈1600ms，比后来单独量到的 ≈500ms 慢 3 倍。
追下去发现不是机器负载，而是 **OMP_NUM_THREADS**：

  * OpenMP 运行库在**被加载时**读 OMP_NUM_THREADS，oneDNN 据此决定线程池大小；
  * paddle 在 import 时会把 `OMP_NUM_THREADS=1` 写进自己的 os.environ，
    于是任何「由 import 过 paddle 的进程拉起的子进程」都会继承 =1，
    在加载时退化成单线程 —— 实测慢 3 倍。

这不是本项目的代码缺陷，但它会同时影响**测量**和**部署**：
用户机器上如果有别的软件把 OMP_NUM_THREADS 设成 1，CPU 推理就会一直是慢档。
所以这里把整条曲线量出来，作为可行性判断的一部分。

用法：
    python tools/ocr_cpu_bench/thread_sweep.py --values unset,1,2,4,8,20 --rounds 1
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))

#: 第一个值默认 "unset"：不设置 OMP_NUM_THREADS，即生产环境的常态。
DEFAULT_VALUES = ("unset", "1", "2", "4", "8", "20")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="CPU 推理耗时的线程数敏感性")
    p.add_argument("--values", default=",".join(DEFAULT_VALUES),
                   help="逗号分隔的 OMP_NUM_THREADS 取值；'unset' = 不设置")
    p.add_argument("--dataset", default=None)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--outdir", default=os.path.join(HERE, "results"))
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)
    rows = []
    cpus = os.cpu_count()

    for raw in [v.strip() for v in args.values.split(",") if v.strip()]:
        out = os.path.join(args.outdir, "threads_%s.json" % raw)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 每个子进程都在干净环境里跑：继承值一律丢掉，由本实验显式指定。
        env.pop("OMP_NUM_THREADS", None)
        if raw != "unset":
            env["OMP_NUM_THREADS"] = raw
        env["CUDA_VISIBLE_DEVICES"] = "-1"

        cmd = [sys.executable, os.path.join(HERE, "bench_device.py"),
               "--device", "cpu", "--out", out,
               "--rounds", str(args.rounds), "--warmup", str(args.warmup)]
        if args.dataset:
            cmd += ["--dataset", args.dataset]
        if args.limit:
            cmd += ["--limit", str(args.limit)]

        print("\n=== OMP_NUM_THREADS=%s ===" % raw, flush=True)
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not os.path.isfile(out):
            print((proc.stdout or "") + (proc.stderr or ""), file=sys.stderr)
            print("[sweep] %s 失败 rc=%d" % (raw, proc.returncode), file=sys.stderr)
            continue

        with open(out, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        t = data["timing"]["infer"]
        row = {
            "omp_num_threads": raw,
            "configured_threads": cpus if raw == "unset" else int(raw),
            "p50_ms": t.get("p50_ms"), "p95_ms": t.get("p95_ms"),
            "max_ms": t.get("max_ms"), "mean_ms": t.get("mean_ms"),
            "min_ms": t.get("min_ms"), "stdev_ms": t.get("stdev_ms"),
            "count": t.get("count"),
            "load_ms": data["timing"]["load_ms"],
        }
        rows.append(row)
        print("[sweep] %-6s p50=%.0fms p95=%.0fms max=%.0fms (n=%d)"
              % (raw, row["p50_ms"] or -1, row["p95_ms"] or -1,
                 row["max_ms"] or -1, row["count"] or 0), flush=True)

    if not rows:
        print("[sweep] 没有任何成功的结果", file=sys.stderr)
        return 1

    summary = {
        "cpu_logical": cpus,
        "rounds": args.rounds,
        "warmup": args.warmup,
        "rows": rows,
    }
    path = os.path.join(args.outdir, "thread_sweep.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    base = next((r for r in rows if r["omp_num_threads"] == "unset"), rows[0])
    print("\n| OMP_NUM_THREADS | p50 (ms) | p95 (ms) | max (ms) | 相对 unset |")
    print("|---|---|---|---|---|")
    for r in rows:
        factor = (r["p50_ms"] / base["p50_ms"]) if base["p50_ms"] else None
        print("| %s | %.0f | %.0f | %.0f | %s |" % (
            r["omp_num_threads"], r["p50_ms"], r["p95_ms"], r["max_ms"],
            "1.00x" if factor is None else "%.2fx" % factor))
    print("\n结果: %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
