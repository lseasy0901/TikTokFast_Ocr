# -*- coding: utf-8 -*-
"""Phase 1 编排器：跑 GPU baseline -> 跑 CPU -> 出对比报告。

每个设备一个**独立子进程**，环境变量分开设置：

    GPU 进程：继承当前环境（要求 CUDA 可见；不可用则如实记 unavailable）
    CPU 进程：CUDA_VISIBLE_DEVICES=-1（显卡对本进程不可见）

用法：
    python tools/ocr_cpu_bench/run_phase1.py
    python tools/ocr_cpu_bench/run_phase1.py --rounds 5 --outdir tools/ocr_cpu_bench/results

产物（默认在 tools/ocr_cpu_bench/results/ 下）：
    gpu_run.json / cpu_run.json / compare.json / PHASE1_CPU_FEASIBILITY_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.ocr_cpu_bench.device_engine import CUDA_HIDDEN_ENV  # noqa: E402


def _force_utf8_io() -> None:
    """本进程的 stdout/stderr 用 UTF-8 + replace，避免中文日志在 GBK 控制台炸掉。

    子进程侧用 PYTHONUTF8=1（见 child_env()）保证它写出来的是 UTF-8，
    两侧一致才不会出现"解码出 U+FFFD 再拿去打印"的二次失败。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def child_env() -> dict:
    """给子进程的环境：UTF-8 输出，且由本函数集中管理。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Phase 1：PP-OCRv6 GPU/CPU 可行性验证")
    p.add_argument("--dataset", default=None, help="数据集目录；默认自动搜索")
    p.add_argument("--outdir", default=os.path.join(HERE, "results"))
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--warmup", type=int, default=10)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip-gpu", action="store_true", help="不跑 GPU baseline（结果里如实记为 unavailable）")
    p.add_argument("--skip-cpu", action="store_true", help="不跑 CPU（仅重出 GPU 结果时用）")
    p.add_argument("--omp-threads", default=None,
                   help="强制子进程的 OMP_NUM_THREADS；缺省=从子进程环境里移除该变量"
                        "（生产口径：GUI 进程里它本来就是未设置的）")
    return p.parse_args(argv)


def run_device(device: str, args, out_json: str) -> tuple:
    """跑一个设备的子进程。返回 (ok, log_text)。"""
    cmd = [sys.executable, os.path.join(HERE, "bench_device.py"),
           "--device", device, "--out", out_json,
           "--rounds", str(args.rounds), "--warmup", str(args.warmup)]
    if args.dataset:
        cmd += ["--dataset", args.dataset]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.omp_threads is not None:
        cmd += ["--omp-threads", str(args.omp_threads)]

    env = child_env()
    # 本编排器自己不 import paddle，因此 env 里的 OMP_NUM_THREADS 只可能来自
    # 启动它的那个进程。若那个进程 import 过 paddle，值会是 "1"，子进程照抄
    # 就会退化成单线程（实测慢 3 倍）。基准要测的是"用户机器上的正常启动"，
    # 所以默认把继承值丢掉，并把这件事写进日志和报告。
    inherited_omp = env.pop("OMP_NUM_THREADS", None)
    if inherited_omp is not None:
        print("[run_phase1] 注意：启动环境带有 OMP_NUM_THREADS=%s（通常来自某个 "
              "import 过 paddle 的父进程），已从子进程环境中移除，以免把继承效应"
              "当成推理耗时。要测这个配置请显式 --omp-threads %s。"
              % (inherited_omp, inherited_omp), file=sys.stderr)
    if args.omp_threads is not None:
        env["OMP_NUM_THREADS"] = str(args.omp_threads)
    if device == "cpu":
        env.update(CUDA_HIDDEN_ENV)

    print(f"\n=== 运行 {device.upper()} 基准 ===\n$ {' '.join(cmd)}", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    dt = time.perf_counter() - t0
    log = (proc.stdout or "") + (proc.stderr or "")
    print(log, end="" if log.endswith("\n") else "\n", flush=True)
    print(f"=== {device.upper()} 结束 rc={proc.returncode} 用时 {dt:.1f}s ===", flush=True)
    return proc.returncode == 0 and os.path.isfile(out_json), log


def main(argv=None) -> int:
    _force_utf8_io()
    args = parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)
    gpu_json = os.path.join(args.outdir, "gpu_run.json")
    cpu_json = os.path.join(args.outdir, "cpu_run.json")

    status = {}

    if not args.skip_gpu:
        ok, _ = run_device("gpu", args, gpu_json)
        status["gpu"] = "ok" if ok else "failed"
        if not ok:
            print("[run_phase1] GPU baseline 运行失败：报告里将记为 unavailable，"
                  "不做任何 CPU/GPU 对比推断。", file=sys.stderr)
    else:
        status["gpu"] = "skipped"

    if not args.skip_cpu:
        ok, _ = run_device("cpu", args, cpu_json)
        status["cpu"] = "ok" if ok else "failed"
        if not ok:
            print("[run_phase1] CPU 基准失败，无法出报告。", file=sys.stderr)
            return 1
    else:
        status["cpu"] = "skipped"
        if not os.path.isfile(cpu_json):
            print("[run_phase1] --skip-cpu 但没有现成的 cpu_run.json", file=sys.stderr)
            return 1

    usable_gpu = status["gpu"] == "ok" and os.path.isfile(gpu_json)
    report = os.path.join(args.outdir, "PHASE1_CPU_FEASIBILITY_REPORT.md")
    cmd = [sys.executable, os.path.join(HERE, "compare_devices.py"),
           "--cpu", cpu_json, "--out-md", report,
           "--out-json", os.path.join(args.outdir, "compare.json")]
    if usable_gpu:
        cmd += ["--gpu", gpu_json]
    else:
        # 显式写进报告：GPU baseline 不可用，且原因是可查的（设备运行状态）。
        with open(os.path.join(args.outdir, "gpu_unavailable.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("GPU baseline unavailable\nreason=%s\n" % status.get("gpu"))

    print(f"\n=== 对比 ===\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    print(proc.stdout or "", end="")
    print(proc.stderr or "", end="")
    if proc.returncode != 0:
        print("[run_phase1] 对比失败", file=sys.stderr)
        return proc.returncode

    print("\n产物：")
    for name in ("gpu_run.json", "cpu_run.json", "compare.json",
                 "PHASE1_CPU_FEASIBILITY_REPORT.md"):
        p = os.path.join(args.outdir, name)
        if os.path.isfile(p):
            print(f"  {p}  ({os.path.getsize(p)} bytes)")
    print(f"\n设备运行状态: {json.dumps(status, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
