# -*- coding: utf-8 -*-
"""Phase 7 · 基线 vs UI 分支的交替 A/B（同会话、同解释器、同一份探针）。

为什么必须交替
--------------
启动耗时受**文件系统缓存预热**影响：同一进程内第二次运行会明显更快。
因此"先跑基线、再跑改造"必然得出"改造更慢"的假结论。本脚本按
base → ui → base → ui … 交替执行，每轮各一次，最后取中位数，
把单向漂移抵消掉。

为什么用子进程
--------------
`ui_baseline_probe.py` 是**整进程**测量（import / 构造 / 首帧 / RSS / 空闲 CPU），
必须每次都是干净进程，不能在同一个 Python 进程里连续跑两次。

用法：
    <venv-python> tests/ui_redesign/ab_baseline_vs_ui.py \
        --base D:/TIikTok_Ocr/_wt_uilg_base \
        --ui   D:/TIikTok_Ocr/_wt_uilg \
        --rounds 3 --cpu-window 6
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

METRICS = (
    "import_ms",
    "window_construct_ms",
    "first_paint_ms",
    "rss_idle_mb",
    "idle_cpu_mean_pct",
    "idle_cpu_max_pct",
    "qtimer_count",
    "qtimer_active",
)


def run_probe(python: str, worktree: str, out_json: str, out_png: str,
              cpu_window: float, timeout: float, offscreen: bool = False) -> dict:
    probe = os.path.join(worktree, "tests", "ui_redesign", "ui_baseline_probe.py")
    if not os.path.exists(probe):
        raise SystemExit(f"探针不存在：{probe}")
    cmd = [python, probe, "--json", out_json, "--screenshot", out_png,
           "--cpu-window", str(cpu_window)]
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    if offscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(cmd, cwd=worktree, env=env, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if not os.path.exists(out_json):
        raise SystemExit(
            f"探针未产出 JSON（{worktree}）\nSTDOUT:\n{proc.stdout[-2000:]}\n"
            f"STDERR:\n{proc.stderr[-2000:]}")
    with open(out_json, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["_worktree"] = worktree
    data["_exit"] = proc.returncode
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="基线 worktree（d3b2c78）")
    ap.add_argument("--ui", required=True, help="UI 改造 worktree")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cpu-window", type=float, default=6.0)
    ap.add_argument("--outdir", default="docs/ui-redesign/evidence/ab")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--offscreen", action="store_true",
                    help="子进程使用 offscreen 平台（消除窗口最小化/遮挡带来的噪声）")
    args = ap.parse_args()

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    python = sys.executable

    rows: list[dict] = []
    for i in range(1, args.rounds + 1):
        for tag, wt in (("base", args.base), ("ui", args.ui)):
            out_json = os.path.join(outdir, f"r{i}_{tag}.json")
            out_png = os.path.join(outdir, f"r{i}_{tag}.png")
            data = run_probe(python, os.path.abspath(wt), out_json, out_png,
                             args.cpu_window, args.timeout, args.offscreen)
            rows.append({"round": i, "tag": tag, "data": data})
            print(f"[r{i}] {tag:4s} ok={data.get('ok')} "
                  f"import={data.get('import_ms')} construct={data.get('window_construct_ms')} "
                  f"paint={data.get('first_paint_ms')} rss={data.get('rss_idle_mb')} "
                  f"cpu={data.get('idle_cpu_mean_pct')} timers={data.get('qtimer_count')}/"
                  f"{data.get('qtimer_active')} exposed={data.get('window_exposed')}",
                  flush=True)

    def med(tag: str, key: str):
        vals = [r["data"].get(key) for r in rows if r["tag"] == tag]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(statistics.median(vals), 2) if vals else None

    summary = {}
    for key in METRICS:
        b, u = med("base", key), med("ui", key)
        summary[key] = {"base": b, "ui": u,
                        "delta": round(u - b, 2) if (b is not None and u is not None) else None}
    result = {"rounds": args.rounds, "cpu_window_s": args.cpu_window,
              "platform": "offscreen" if args.offscreen else "native",
              "summary": summary, "rows": rows}
    path = os.path.join(outdir, "ab_summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("\n=== median (base vs ui) ===")
    for key, v in summary.items():
        print(f"{key:24s} base={v['base']!s:>10}  ui={v['ui']!s:>10}  delta={v['delta']}")
    print("\n->", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
