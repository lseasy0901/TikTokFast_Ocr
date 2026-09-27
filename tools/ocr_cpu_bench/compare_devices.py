# -*- coding: utf-8 -*-
"""GPU / CPU 两份基准结果 -> 精度对比 + 延迟对比 + 逐帧差异清单。

指标口径（全部在这里定义一次，报告里的数字只能是这里算出来的）
--------------------------------------------------------------
标准答案 GT：数据集 labels.csv 的 ground_truth。**它不是人工标注**，
而是采集时 GPU 生产链路的原始读法（见数据集 README）。因此：

  * 「raw_*」指标（引擎原始读法 vs GT）在 GPU 侧接近满分是**自证**，
    不能读成"GPU 准确率 100%"；
  * 它真正的用途是**跨设备一致性**：同一批图上 CPU 的原始读法若与
    GPU 不同，差异会直接体现在这些数字上，并被 diff 清单逐条列出。

原始读法 (Pass 1, engine.recognize)
    raw_exact_match      规范化后与 GT 逐字符相同
    raw_char_accuracy    1 - Σ编辑距离 / Σlen(GT)（聚合，不是逐帧平均）
    raw_no_read          输出为空串
    raw_wrong_length     非空但长度与 GT 不同
    raw_invalid_format   长度对，但有位不满足该游戏格式的字符类
                         （长度对 + 格式对 + 内容不同 = raw_wrong_code）

校验输出 (Pass 2, RoomCodeRecognizer.recognize_frame，即生产后处理)
    code_exact   == GT
    code_wrong   非 None 且 != GT      <- 产品上最危险的一类
    code_none    无结果（严格校验/纠正后的正常"没读到"）

规范化只做一次（去空白 + 大写），两个设备共用同一条。

用法：
    python tools/ocr_cpu_bench/compare_devices.py \\
        --gpu out/gpu.json --cpu out/cpu.json \\
        --out-md PHASE1_CPU_FEASIBILITY_REPORT.md --out-json out/compare.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ocr.code_format import CodeFormat                                     # noqa: E402
from ocr.profiles import PROFILES                                          # noqa: E402


def levenshtein(a: str, b: str) -> int:
    """编辑距离（逐字符，不区分大小写已在上游规范化）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def format_for(profile_id: str, length: int) -> Optional[CodeFormat]:
    """取该样本格式：优先按档里的长度精确匹配，否则 None（不猜）。"""
    profile = PROFILES.get(profile_id)
    if profile is None:
        return None
    for fmt in profile.formats:
        if fmt.length == length:
            return fmt
    return None


def load_run(path: str, device: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("device") != device:
        raise SystemExit(f"{path} 的 device={data.get('device')!r}，期望 {device!r}")
    return data


def evaluate(run: dict) -> dict:
    """把一份单设备结果算成指标 + 逐帧判定。"""
    frames = run["frames"]
    total = len(frames)
    n_exact = n_no_read = n_wrong_len = n_invalid_fmt = n_raw_wrong = 0
    n_code_exact = n_code_wrong = n_code_none = 0
    n_error = n_conflict = n_corrected = 0
    edit_sum = gt_len_sum = 0
    details: List[dict] = []

    for f in frames:
        gt = f["ground_truth"]
        raw = f["raw_text"] or ""
        fmt = format_for(f["profile_id"], len(gt))
        edit = levenshtein(raw, gt) if gt else 0
        edit_sum += edit
        gt_len_sum += len(gt)

        raw_exact = bool(gt) and raw == gt
        no_read = raw == ""
        wrong_len = (not no_read) and len(raw) != len(gt)
        invalid_fmt = False
        if fmt is not None and not no_read and not wrong_len:
            invalid_fmt = any(not fmt.accepts_char(ch, i) for i, ch in enumerate(raw))

        if raw_exact:
            n_exact += 1
        elif no_read:
            n_no_read += 1
        elif wrong_len:
            n_wrong_len += 1
        elif invalid_fmt:
            n_invalid_fmt += 1
        else:
            n_raw_wrong += 1

        code = f["code"]
        if code is None:
            n_code_none += 1
        elif code == gt:
            n_code_exact += 1
        else:
            n_code_wrong += 1
        if f.get("error"):
            n_error += 1
        if f.get("conflict"):
            n_conflict += 1
        if f.get("corrections"):
            n_corrected += 1

        details.append({
            "sample_id": f["sample_id"],
            "gt": gt,
            "raw": raw,
            "code": code,
            "raw_exact": raw_exact,
            "edit": edit,
            "profile_id": f["profile_id"],
            "corrections": f.get("corrections", 0),
            "case_fold": f.get("case_fold", 0),
            "error": f.get("error"),
        })

    return {
        "device": run["device"],
        "total": total,
        "raw": {
            "exact_match": n_exact,
            "exact_match_rate": (n_exact / total) if total else None,
            "char_accuracy": (1.0 - edit_sum / gt_len_sum) if gt_len_sum else None,
            "edit_distance_total": edit_sum,
            "gt_chars_total": gt_len_sum,
            "no_read": n_no_read,
            "wrong_length": n_wrong_len,
            "invalid_format": n_invalid_fmt,
            "wrong_code": n_raw_wrong,
        },
        "code": {
            "exact": n_code_exact,
            "wrong": n_code_wrong,
            "none": n_code_none,
            "corrected_frames": n_corrected,
        },
        "errors": n_error,
        "conflicts": n_conflict,
        "timing": run["timing"],
        "labelize": None,
        "frames": details,
    }


def cross_device(gpu: dict, cpu: dict) -> dict:
    """同一批图的逐帧一致性：原始读法是否一致、最终房间号是否一致。"""
    by_id_gpu = {f["sample_id"]: f for f in gpu["frames"]}
    by_id_cpu = {f["sample_id"]: f for f in cpu["frames"]}
    common = sorted(set(by_id_gpu) & set(by_id_cpu))
    raw_diff, code_diff, gt_diff = [], [], []
    raw_same = code_same = 0
    for sid in common:
        g, c = by_id_gpu[sid], by_id_cpu[sid]
        gt = g["ground_truth"]
        if (g["raw_text"] or "") == (c["raw_text"] or ""):
            raw_same += 1
        else:
            raw_diff.append({"sample_id": sid, "gt": gt,
                             "gpu_raw": g["raw_text"], "cpu_raw": c["raw_text"]})
        if g["code"] == c["code"]:
            code_same += 1
        else:
            code_diff.append({"sample_id": sid, "gt": gt,
                              "gpu_code": g["code"], "cpu_code": c["code"]})
        if g["code"] != c["code"] and (g["code"] == gt or c["code"] == gt):
            gt_diff.append({"sample_id": sid, "gt": gt,
                            "gpu_code": g["code"], "cpu_code": c["code"]})
    n = len(common)
    return {
        "common_samples": n,
        "raw_same": raw_same,
        "raw_agreement": (raw_same / n) if n else None,
        "raw_differences": raw_diff,
        "code_same": code_same,
        "code_agreement": (code_same / n) if n else None,
        "code_differences": code_diff,
        "gt_decided_differences": gt_diff,
    }


def _fmt(v, digits=4):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.2f}%"


def build_markdown(cpu_eval: dict, gpu_eval: Optional[dict], cross: Optional[dict],
                   cpu_run: dict, gpu_run: Optional[dict]) -> str:
    L: List[str] = []
    A = L.append

    A("# Phase 1 — PP-OCRv6 CPU 可行性验证报告")
    A("")
    A(f"生成时间：{cpu_run.get('generated_at', '')}")
    A("")

    # --- 模型与环境 ---------------------------------------------------
    A("## 0. 被测对象与环境")
    A("")
    for label, run in (("GPU baseline", gpu_run), ("CPU", cpu_run)):
        if run is None:
            A(f"**{label}**：unavailable（未提供结果文件）")
            A("")
            continue
        m, e, iso = run["model"], run["environment"], run.get("isolation", {})
        A(f"**{label}**")
        A("")
        A(f"- model_dir: `{m['dir']}`")
        A(f"- inference.pdiparams: {m.get('params_bytes')} bytes, sha256 `{m.get('params_sha256')}`")
        A(f"- paddle {e.get('paddle_version')} / paddlex {e.get('paddlex_version')} "
          f"/ python {e.get('python')}")
        A(f"- paddle 编译含 CUDA: {e.get('paddle_compiled_with_cuda')}, "
          f"本进程可见 GPU: {e.get('paddle_device_count')}, "
          f"CUDA_VISIBLE_DEVICES={e.get('cuda_visible_devices_env')}")
        A(f"- engine backend_status: {json.dumps(run.get('backend_status', {}), ensure_ascii=False)}")
        if run["device"] == "cpu":
            A(f"- CPU 隔离: isolated={iso.get('isolated')}, "
              f"可见 GPU={iso.get('visible_cuda_devices')}")
        A("")

    if gpu_run is not None and cpu_run is not None:
        same_model = (gpu_run["model"].get("params_sha256")
                      == cpu_run["model"].get("params_sha256"))
        A(f"**同一模型校验：{'通过' if same_model else '不通过（两份结果的模型 sha256 不同！）'}**")
        A("")

    # --- 数据集 -------------------------------------------------------
    d = cpu_run["dataset"]
    A("## 1. 数据集")
    A("")
    A(f"- 目录：`{d['dir']}`")
    A(f"- 样本：{d['samples']} 条，可用图像 {d['usable_images']} 张，"
      f"缺图 {len(d['missing_images'])} 张")
    A(f"- GT 长度分布：{d['gt_length_histogram']}，游戏档：{d['profiles']}")
    A(f"- 标注口径：{json.dumps(d.get('labeling', {}), ensure_ascii=False)}")
    A("")
    A("> **GT 口径警告**：本数据集的 ground_truth 是采集时 GPU 生产链路的原始读法，"
      "不是人工标注。因此「对 GT 的准确率」度量的是**与 GPU 的一致性**，"
      "不能当作绝对识别能力。跨设备的真实结论看第 3 节的逐帧差异清单。")
    A("")

    # --- 精度 ---------------------------------------------------------
    A("## 2. 精度对比（同一批图、同一模型、同一后处理规则）")
    A("")
    A("| 指标 | GPU | CPU | 差异 |")
    A("|---|---|---|---|")
    rows = [
        ("样本数 total", lambda x: x["total"]),
        ("原始读法 exact_match", lambda x: x["raw"]["exact_match"]),
        ("原始读法 exact_match_rate", lambda x: _pct(x["raw"]["exact_match_rate"])),
        ("字符准确率 char_accuracy", lambda x: _pct(x["raw"]["char_accuracy"])),
        ("原始 no_read（空输出）", lambda x: x["raw"]["no_read"]),
        ("原始 wrong_length", lambda x: x["raw"]["wrong_length"]),
        ("原始 invalid_format", lambda x: x["raw"]["invalid_format"]),
        ("原始 wrong_code（长度格式对但内容错）", lambda x: x["raw"]["wrong_code"]),
        ("校验后 code_exact", lambda x: x["code"]["exact"]),
        ("校验后 wrong_code", lambda x: x["code"]["wrong"]),
        ("校验后 no_read（无结果）", lambda x: x["code"]["none"]),
        ("做过纠正的帧", lambda x: x["code"]["corrected_frames"]),
        ("推理异常 error", lambda x: x["errors"]),
        ("游戏冲突帧", lambda x: x["conflicts"]),
    ]
    for name, get in rows:
        gv = get(gpu_eval) if gpu_eval else "n/a"
        cv = get(cpu_eval)
        A(f"| {name} | {gv} | {cv} | {'—' if gpu_eval is None else ''} |")
    A("")

    # --- 跨设备一致性 --------------------------------------------------
    A("## 3. 跨设备一致性（CPU 是否保持 GPU 的识别能力）")
    A("")
    if cross is None:
        A("**GPU baseline unavailable**：没有 GPU 结果，无法做跨设备对比。")
        A("这不是 CPU 的问题，只是本次没有可比的基线，不作任何推断。")
        A("")
    else:
        A(f"- 共同样本：{cross['common_samples']}")
        A(f"- 原始读法一致：{cross['raw_same']} / {cross['common_samples']}"
          f"（{_pct(cross['raw_agreement'])}）")
        A(f"- 最终房间号一致：{cross['code_same']} / {cross['common_samples']}"
          f"（{_pct(cross['code_agreement'])}）")
        A(f"- 其中「一设备对、另一设备错」的样本：{len(cross['gt_decided_differences'])} 条")
        A("")
        if cross["raw_differences"]:
            A("### 原始读法不同的样本")
            A("")
            A("| sample | GT | GPU 读法 | CPU 读法 |")
            A("|---|---|---|---|")
            for r in cross["raw_differences"]:
                A(f"| {r['sample_id']} | {r['gt']} | `{r['gpu_raw']}` | `{r['cpu_raw']}` |")
            A("")
        else:
            A("原始读法逐帧完全一致——CPU 未改变任何一帧的识别内容。")
            A("")
        if cross["code_differences"]:
            A("### 最终房间号不同的样本")
            A("")
            A("| sample | GT | GPU | CPU |")
            A("|---|---|---|---|")
            for r in cross["code_differences"]:
                A(f"| {r['sample_id']} | {r['gt']} | `{r['gpu_code']}` | `{r['cpu_code']}` |")
            A("")
        else:
            A("最终房间号逐帧完全一致。")
            A("")
        if cross["gt_decided_differences"]:
            A("### 一设备命中、另一设备未命中（可判定优劣的差异）")
            A("")
            A("| sample | GT | GPU | CPU | 命中方 |")
            A("|---|---|---|---|---|")
            for r in cross["gt_decided_differences"]:
                winner = "GPU" if r["gpu_code"] == r["gt"] else "CPU"
                A(f"| {r['sample_id']} | {r['gt']} | `{r['gpu_code']}` | "
                  f"`{r['cpu_code']}` | {winner} |")
            A("")

    # --- 延迟 ---------------------------------------------------------
    A("## 4. 延迟对比")
    A("")
    A("单次推理 = `engine.recognize()` 一次调用（即生产 300ms 识别间隔里的那一次），"
      "不含图像解码。")
    A("")
    A("| 指标 | GPU | CPU |")
    A("|---|---|---|")
    t_gpu = (gpu_eval or {}).get("timing")
    t_cpu = cpu_eval["timing"]

    def _row(name, key, digits=1):
        def val(t):
            if not t:
                return "n/a"
            v = t.get(key)
            return "n/a" if v is None else f"{v:.{digits}f}"
        A(f"| {name} | {val(t_gpu)} | {val(t_cpu)} |")

    _row("模型加载 load_ms", "load_ms", 0)
    _row("预热总耗时 warmup_total_ms", "warmup_total_ms", 0)
    for key, name in (("p50_ms", "P50"), ("p95_ms", "P95"), ("p99_ms", "P99"),
                      ("max_ms", "Max"), ("mean_ms", "Mean"), ("min_ms", "Min"),
                      ("stdev_ms", "标准差")):
        def val2(t, k=key):
            if not t:
                return "n/a"
            v = (t.get("infer") or {}).get(k)
            return "n/a" if v is None else f"{v:.2f}"
        A(f"| 推理 {name} (ms) | {val2(t_gpu)} | {val2(t_cpu)} |")
    gc = (t_gpu or {}).get("infer", {}).get("count")
    cc = (t_cpu.get("infer") or {}).get("count")
    A(f"| 推理样本数 | {gc if gc is not None else 'n/a'} | {cc} |")
    A("")

    A("### 预热曲线（单张首次调用的算子初始化代价）")
    A("")
    for label, t in (("GPU", t_gpu), ("CPU", t_cpu)):
        if not t:
            continue
        w = t.get("warmup_calls_ms") or []
        if w:
            A(f"- {label}: 首次 {w[0]:.1f}ms → 末次 {w[-1]:.1f}ms（共 {len(w)} 次，"
              f"合计 {t.get('warmup_total_ms'):.0f}ms）")
    A("")

    A("### CPU 延迟在 300ms 间隔下的余量")
    A("")
    p95 = (t_cpu.get("infer") or {}).get("p95_ms")
    p99 = (t_cpu.get("infer") or {}).get("p99_ms")
    mx = (t_cpu.get("infer") or {}).get("max_ms")
    if p95 is not None:
        A(f"- P95 {p95:.1f}ms 占 300ms 间隔的 {p95 / 300 * 100:.0f}%")
        A(f"- P99 {p99:.1f}ms 占 {p99 / 300 * 100:.0f}%；Max {mx:.1f}ms 占 {mx / 300 * 100:.0f}%")
        if mx > 300:
            A("")
            A(f"> **注意**：最慢一次 {mx:.1f}ms 已超过 300ms 识别间隔。"
              "单帧超时不会阻塞视频管线（OCR 在工作线程、丢旧帧优先），"
              "但会让该帧的识别机会丢失，实时性需要按 P95/P99 而不是均值评估。")
    A("")

    return "\n".join(L) + "\n"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="GPU/CPU PP-OCRv6 基准对比")
    p.add_argument("--gpu", default=None, help="GPU 结果 JSON；缺省视为 GPU baseline unavailable")
    p.add_argument("--cpu", required=True, help="CPU 结果 JSON")
    p.add_argument("--out-md", default=None, help="报告 markdown 输出路径")
    p.add_argument("--out-json", default=None, help="对比结果 JSON 输出路径")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cpu_run = load_run(args.cpu, "cpu")
    gpu_run = load_run(args.gpu, "gpu") if args.gpu and os.path.isfile(args.gpu) else None
    if args.gpu and gpu_run is None:
        print(f"[compare] 警告：--gpu {args.gpu} 不存在，按 GPU baseline unavailable 处理",
              file=sys.stderr)

    cpu_eval = evaluate(cpu_run)
    gpu_eval = evaluate(gpu_run) if gpu_run else None
    cross = cross_device(gpu_run, cpu_run) if gpu_run else None

    md = build_markdown(cpu_eval, gpu_eval, cross, cpu_run, gpu_run)
    if args.out_md:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_md)) or ".", exist_ok=True)
        with open(args.out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"[compare] 报告已写入 {args.out_md}", file=sys.stderr)

    payload = {"cpu": cpu_eval, "gpu": gpu_eval, "cross": cross}
    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)) or ".", exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"[compare] 对比数据已写入 {args.out_json}", file=sys.stderr)

    if not args.out_md and not args.out_json:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
