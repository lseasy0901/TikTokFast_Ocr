#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线 OCR 准确率基准（Phase OCR-7）

只做两件事：把「真实样本 + 标准答案」变成可重复的准确率数字；把历史异常
整理成可核对的清单。**不接入实时识别链路，也不改动任何复制结果。**

四种入口：

  1) --build-registry   从两轮实测的诊断 CSV 重建异常清单
        证据来自 CSV 里逐帧记录的真实读法，不是人工誊写；每个异常挑
        「与标准码最接近的那一帧」作为代表帧（末位/单字符差异最大者），
        错复制优先挑真正请求了复制的那一帧。
        没有图像，因此「图像样本」列一律为「待采集」—— 不伪造。

  2) --samples DIR      图像离线基准（需要真实 ROI 样本）
        读 DIR/samples.csv 索引；对每个**已标注**样本，用**真实 OCR 引擎**
        重跑 PSM 6/7/11，并走**现有候选逻辑**（RoomCodeRecognizer._resolve_text
        与 recognize_frame），输出：
            原始识别正确率（逐 PSM + 任一 PSM 命中）
            候选正确率 / 错误候选数 / 无候选数
            每阶段耗时（全 PSM 扫一遍 vs 线上会走的提前停止路径）
        未标注样本只计入「未标注」，既不算对也不算错。

  3) --make-labels DIR  生成人工标注模板（序号,时间,识别读法,标准房间码）
        识别读法只是**给你参考**的线索，标准房间码留空由人填 ——
        绝不把 OCR 自己的输出当成标准答案。

  4) --diagnostics CSV  已记录读法的离线 PSM 对照（无需图像）
        直接用真实运行中记下的 PSM 6/7/11 原文做统计：读法数分布、长度分布
        （插入型误读）、三档一致率，并把 --registry 里每个异常码对应的
        真实读法取出来核对。

用法示例：
    venv/Scripts/python.exe bench_phase_ocr7_offline_accuracy.py \\
        --samples "%USERPROFILE%/Documents/LiveLens-OCR样本/20260925-050000"
    venv/Scripts/python.exe bench_phase_ocr7_offline_accuracy.py \\
        --diagnostics "…/LiveLens-OCR诊断-20260925-044343.csv" --registry ocr_anomaly_registry.csv
"""

import argparse
import collections
import csv
import os
import statistics
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np                                              # noqa: E402

from ocr.code_format import CodeFormat                          # noqa: E402
from ocr.profiles import GAME_PROFILE_IDS, get_profile          # noqa: E402
from ocr.room_code import RoomCodeRecognizer                    # noqa: E402

REGISTRY_COLUMNS = ("轮次", "标准房间码", "结果", "实际输出",
                    "PSM6读法", "PSM7读法", "PSM11读法", "机制", "图像样本")

#: 逐条异常的机制判定。依据是诊断 CSV 里那一帧的真实读法 + 现有代码路径，
#: 不是猜测；每条都能在下面 _MECHANISM 注释里对应到具体证据。
_MECHANISM = {
    ("R3", "HJKT6402"): "末位 0->6（OCR 直接读成 6，数字位合法，纠错层无从介入）",
    ("R3", "CVBN9502"): "末位 0->6（同上）",
    ("R3", "QRTP8641"): "首位 Q->O：OCR 直接读成合法字母 O，无需纠正即通过校验",
    ("R3", "AZQX7304"): "Q->0 的 OCR 误读，再被纠错表 0->O 补成合法字母（三档一致）",
    ("R3", "XYZT5872"): "段长 9（插入 1 字符）-> 残差 1 被拒绝，无候选",
    ("R3", "HJKL4750"): "段长 9（插入 1 字符）-> 残差 1 被拒绝，无候选",
    ("R3", "QWER5062"): "数字位读到 Q，LETTER_TO_DIGIT 无 Q 映射 -> 不可纠正，无候选",
    ("R3", "HNJM3086"): "段长 9（插入 1 字符）-> 残差 1 被拒绝，无候选",
    ("R3", "LPOU8247"): "前两帧段长 9 被拒；第 3 帧读对但只此一帧，未达连续 2 次",
    ("R2", "UJMI8506"): "滑窗错位：9 字符段滑出零纠正的偏移 1 候选（残差门已拦截）",
    ("R2", "PQAZ1740"): "前两帧末位 'e'（映射表外不可纠正）；第 3 帧读对但只此一帧",
    ("R2", "BFRG9510"): "末位 0->6（OCR 直接读成 6）",
    ("R2", "BNML9730"): "末位 0->6（同上）",
    ("R2", "QRTP8641"): "滑窗错位：9 字符段滑出偏移 1 的零纠正候选（残差门已拦截）",
    ("R2", "TYGH5730"): "段长 9（多出 1 字符）滑窗取到偏移 0 的错位候选（残差门已拦截）",
}


# ======================================================================
# 小工具
# ======================================================================
def _ms(text: str) -> int:
    """``HH:MM:SS.mmm`` -> 当日毫秒。"""
    hms, milli = text.split(".")
    h, m, s = hms.split(":")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(milli)


def _read_csv(path: str):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _split_readings(text: str):
    """把 ``6:ABC ‖ 7:ABC`` 拆成 {psm: 文本}。"""
    out = {}
    for part in (text or "").split("‖"):
        part = part.strip()
        if ":" in part:
            label, value = part.split(":", 1)
            out[label.strip()] = value
    return out


def _similarity(raw: str, standard: str) -> int:
    """同一位置上字符相同的个数（用于挑「最接近标准码」的代表帧）。"""
    return sum(1 for a, b in zip(raw, standard) if a == b)


# ======================================================================
# 1) 从诊断 CSV 重建异常清单
# ======================================================================
def build_registry(args) -> int:
    """从两轮的诊断 CSV 重建异常清单。"""
    rounds = []
    for number, directory in ((2, args.round2_dir), (3, args.round3_dir)):
        if not directory:
            continue
        diag = _first(directory, "LiveLens-OCR诊断-*.csv")
        copies = _first(directory, "LiveLens-房间码-*.csv")
        answers = _first(directory, "livelen_ocr_answers.csv")
        if not (diag and copies and answers):
            print(f"  [跳过] 第 {number} 轮目录缺少诊断/复制/标准码文件: {directory}")
            continue
        rounds.append((number, _read_csv(diag), _read_csv(copies), _read_csv(answers)))

    rows = []
    for number, diag, copies, answers in rounds:
        std = {int(r["序号"]): r["标准房间码"] for r in answers}
        copied = [(int(r["序号"]), r["复制时间"], r["房间码"]) for r in copies]
        for kind, index, expected, actual, when in _align(std, copied):
            if expected is None:
                # 复制记录比标准序列还长：对不上了，不是「某个码的异常」。
                # 与其编一条没有标准码的异常，不如跳过。
                print(f"  [跳过] 第 {number} 轮有 1 条对不上标准序列的记录（序号 {index}）")
                continue
            rows.append(_representative(number, kind, index, expected, actual, when, diag))

    with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REGISTRY_COLUMNS),
                                lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    if not rows:
        # 仍然写一个只有表头的文件：让调用方拿到的路径一定存在，
        # 而不是「有时有文件有时没有」。
        print(f"已写入 {args.out}：0 条异常（复制记录与标准序列对不上，"
              f"或该轮没有异常）")
        return 0
    print(f"已写入 {args.out}：{len(rows)} 条异常")
    for row in rows:
        print("  R%s %-9s %-14s 实际=%-9s PSM6=%-10s PSM7=%-10s PSM11=%s"
              % (row["轮次"], row["标准房间码"], row["结果"], row["实际输出"],
                 row["PSM6读法"], row["PSM7读法"], row["PSM11读法"]))
    return 0


def _first(directory: str, pattern: str):
    import glob
    hits = sorted(glob.glob(os.path.join(directory, pattern)))
    return hits[0] if hits else None


def _align(std, copied):
    """把复制记录对齐到标准序列，产出 (kind, 标准序号, 标准码, 实际码, 时刻)。"""
    out = []
    i = 1
    for _, when, code in copied:
        if std.get(i) == code:
            i += 1
        elif std.get(i + 1) == code:
            out.append(("漏复制", i, std[i], None, when))
            i += 2
        else:
            out.append(("错复制", i, std.get(i), code, when))
            i += 1
    return out


def _representative(number, kind, index, standard, actual, when, diag) -> dict:
    """给一个异常挑代表帧，并填出各 PSM 的真实读法。

    - 错复制：优先挑**真正请求了复制**的那一帧（最权威）；
      没有就退回"与标准码最接近"的帧。
    - 漏复制：在【该码显示期间】里挑与标准码最接近的帧。显示期间 =
      上一个被复制的码出现之后 到 下一个码被复制之前，因此以复制时刻为
      右边界向前取一段。
    """
    def readings_of(row):
        return _split_readings(row["PSM读法"]) if row else {}

    pick = None
    if kind == "错复制":
        pick = next((r for r in diag if r.get("复制请求") == actual), None)
        if pick is None:
            pick = next((r for r in diag
                         if r.get("仲裁结果") == f"选定:{actual}"), None)
    if pick is None:
        edge = _ms(when)
        window = [r for r in diag
                  if edge - 1600 <= _ms(r["时间"]) <= edge - 60
                  and readings_of(r)]
        pick = max(window, key=lambda r: max(
            (_similarity(v, standard) for v in readings_of(r).values()),
            default=-1)) if window else None

    readings = readings_of(pick)
    return {
        "轮次": number,
        "标准房间码": standard,
        "结果": kind,
        "实际输出": actual or "(无)",
        "PSM6读法": readings.get("6", ""),
        "PSM7读法": readings.get("7", ""),
        "PSM11读法": readings.get("11", ""),
        "机制": _MECHANISM.get((f"R{number}", standard), ""),
        "图像样本": "待采集",
    }


# ======================================================================
# 2) 图像离线基准
# ======================================================================
def run_samples(args) -> int:
    import cv2
    from ocr.engine import OCREngine

    index_path = os.path.join(args.samples, "samples.csv")
    if not os.path.exists(index_path):
        print(f"找不到样本索引：{index_path}")
        return 1
    samples = _read_csv(index_path)
    labels = {}
    if args.labels:
        for row in _read_csv(args.labels):
            key = (row.get("序号") or row.get("样本") or "").strip()
            value = (row.get("标准房间码") or "").strip().upper()
            if key and value:
                labels[int(key)] = value
    if not samples:
        print("样本索引是空的。")
        return 1

    engine = OCREngine()
    # 逐样本用同一套真实识别器；游戏档取样本自己记的那个（导出的就是它）
    recognizers = {}
    stats = collections.Counter()
    per_sample = []
    sweep_ms, prod_ms = [], []

    for row in samples:
        index = int(row["序号"])
        standard = labels.get(index)
        path = os.path.join(args.samples, row["图像文件"].replace("/", os.sep))
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            stats["读图失败"] += 1
            continue
        profile_id = row.get("游戏档") or GAME_PROFILE_IDS[0]
        recognizer = recognizers.get(profile_id)
        if recognizer is None:
            recognizer = RoomCodeRecognizer(engine, profile=profile_id)
            recognizers[profile_id] = recognizer

        # 全 PSM 扫一遍（真实引擎），再走现有候选逻辑
        start = time.perf_counter()
        readings = list(engine.iter_readings(image, preprocess=True))
        sweep = (time.perf_counter() - start) * 1000.0
        sweep_ms.append(sweep)

        start = time.perf_counter()
        candidate = recognizer.recognize_frame(image)      # 线上真正走的路径
        prod = (time.perf_counter() - start) * 1000.0
        prod_ms.append(prod)

        raw_by_psm = {psm: text for psm, text in readings}
        hit_psm = [psm for psm, text in readings if standard and text == standard]
        final = candidate.code if candidate is not None else None

        if standard is None:
            stats["未标注"] += 1
        else:
            stats["已标注"] += 1
            if hit_psm:
                stats["原始命中"] += 1
            if final == standard:
                stats["候选正确"] += 1
            elif final is None:
                stats["无候选"] += 1
            else:
                stats["错误候选"] += 1

        per_sample.append({
            "序号": index, "时间": row["时间"], "游戏档": profile_id,
            "标准房间码": standard or "", "候选": final or "",
            "结果": _verdict(standard, final),
            "PSM6原文": raw_by_psm.get(6, ""), "PSM7原文": raw_by_psm.get(7, ""),
            "PSM11原文": raw_by_psm.get(11, ""),
            "命中PSM": ",".join(str(p) for p in hit_psm),
            "全PSM耗时ms": f"{sweep:.1f}", "线上路径耗时ms": f"{prod:.1f}",
            "图像文件": row["图像文件"],
        })

    _print_sample_report(stats, per_sample, sweep_ms, prod_ms)
    if args.out:
        with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(per_sample[0].keys()),
                                    lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(per_sample)
        print(f"逐样本明细已写入 {args.out}")
    return 0


def _verdict(standard, final):
    if standard is None:
        return "未标注"
    if final == standard:
        return "正确"
    return "无候选" if final is None else "错误候选"


def _print_sample_report(stats, per_sample, sweep_ms, prod_ms) -> None:
    labeled = stats["已标注"]
    print("=" * 74)
    print(f"样本 {stats['已标注'] + stats['未标注']} 个"
          f"（已标注 {labeled}，未标注 {stats['未标注']}）")
    if stats["读图失败"]:
        print(f"读图失败 {stats['读图失败']} 个（已跳过）")
    print("=" * 74)
    if not labeled:
        print("没有任何已标注样本 —— 无法给出准确率。")
        print("先用 --make-labels 生成标注模板，填入标准房间码后再跑。")
        print("（没有真实样本时，本基准不会给出任何数字，也不伪造结论。）")
        return
    print(f"原始识别正确率（任一 PSM 原文 == 标准码）："
          f"{stats['原始命中']}/{labeled} = {100.0 * stats['原始命中'] / labeled:.1f}%")
    print(f"候选正确率（最终候选 == 标准码）："
          f"{stats['候选正确']}/{labeled} = {100.0 * stats['候选正确'] / labeled:.1f}%")
    print(f"错误候选数：{stats['错误候选']}   无候选数：{stats['无候选']}")
    for name, values in (("全 PSM 扫描", sweep_ms), ("线上提前停止路径", prod_ms)):
        if values:
            ordered = sorted(values)
            print(f"{name}耗时：均值 {statistics.mean(values):.1f}ms  "
                  f"中位 {statistics.median(values):.1f}ms  "
                  f"p95 {ordered[int(len(ordered) * 0.95)]:.1f}ms  "
                  f"最大 {max(values):.1f}ms")


def make_labels(args) -> int:
    """生成人工标注模板：标准房间码留空，由人填。"""
    index_path = os.path.join(args.make_labels, "samples.csv")
    if not os.path.exists(index_path):
        print(f"找不到样本索引：{index_path}")
        return 1
    samples = _read_csv(index_path)
    out = args.out or os.path.join(args.make_labels, "labels.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["序号", "时间", "识别读法(仅供参考)", "标准房间码(请填写)"])
        for row in samples:
            writer.writerow([row["序号"], row["时间"], row["PSM读法"], ""])
    print(f"标注模板已写入 {out}（{len(samples)} 行）")
    print("请只填写最后一列；识别读法那一列是 OCR 自己的输出，不是标准答案。")
    return 0


# ======================================================================
# 3) 已记录读法的离线 PSM 对照
# ======================================================================
def run_diagnostics(args) -> int:
    """用真实运行里记下的 PSM 读法做对照（无需图像）。"""
    rows = _read_csv(args.diagnostics)
    if not rows:
        print("诊断 CSV 是空的。")
        return 1

    readings_per_frame = collections.Counter()
    length_counter = collections.Counter()
    three_frame = []
    for row in rows:
        parts = [p.strip() for p in (row["PSM读法"] or "").split("‖") if p.strip()]
        readings_per_frame[len(parts)] += 1
        for part in parts:
            if ":" in part:
                value = part.split(":", 1)[1]
                if value:
                    length_counter[len(value)] += 1
        if len(parts) == 3:
            three_frame.append(parts)

    print("=" * 74)
    print(f"诊断 CSV：{args.diagnostics}")
    print(f"帧数 {len(rows)}")
    print("=" * 74)
    print("每帧读法数分布：", dict(sorted(readings_per_frame.items())))
    print("读法文本长度分布：", dict(sorted(length_counter.items())))
    fmt_len = _expected_length(args)
    inserted = length_counter.get(fmt_len + 1, 0)
    if inserted:
        print(f"  其中长度为「格式长度({fmt_len}) + 1」的读法 {inserted} 个 —— "
              f"插入型误读，会被残差门拒绝（表现为漏识别，不再是错复制）")

    agree = sum(1 for parts in three_frame
                if len({p.split(":", 1)[1] for p in parts}) == 1)
    if three_frame:
        print(f"三档都跑了的帧 {len(three_frame)} 个，"
              f"其中三档读法完全一致 {agree} 个 "
              f"({100.0 * agree / len(three_frame):.0f}%)")
        print("  -> 完全一致的帧，PSM 仲裁无从纠正；分歧的帧才有仲裁空间")

    if args.registry and os.path.exists(args.registry):
        print()
        registry = _read_csv(args.registry)
        if args.round:
            registry = [e for e in registry
                        if str(e["轮次"]) == str(args.round)]
        print(f"异常清单核对（在该 CSV 里找对应的真实读法）"
              f"{'，只取第 %s 轮' % args.round if args.round else ''}：")
        if not args.round:
            print("  注意：未指定 --round 时，其它轮次的同码条目会匹配到本 CSV 里"
                  "「同一个码被读对」的帧，那不是该异常的复现。")
        for entry in registry:
            standard = entry["标准房间码"]
            actual = entry["实际输出"]
            frames = [r for r in rows
                      if _matches(standard, actual, r["PSM读法"])]
            agree = collections.Counter()
            seen = collections.Counter()
            for frame in frames:
                vals = list(_split_readings(frame["PSM读法"]).values())
                for value in vals:
                    seen[value] += 1
                if len(vals) == 3:
                    agree["三档一致" if len(set(vals)) == 1 else "三档分歧"] += 1
            verdict = "（本 CSV 里没有该异常的帧）" if not frames else ""
            print(f"  R{entry['轮次']} {standard} [{entry['结果']}] "
                  f"实际={actual} 命中帧 {len(frames)} {verdict}"
                  f"{dict(agree) if agree else ''}")
            if seen:
                top = "，".join(f"{v}×{n}" for v, n in seen.most_common(4))
                print(f"      真实读法：{top}")
            print(f"      机制：{entry['机制']}  |  图像样本：{entry['图像样本']}")
    return 0


def _matches(standard: str, actual: str, readings_text: str) -> bool:
    """这一帧是否属于该异常。

    判据刻意收紧，避免把同前缀的无关码算进来：
        - 某个读法**恰好等于**被复制过的错码（错复制最权威的证据）；或
        - 某个读法与标准码等长、只差 1 个字符（单字符替换）；或
        - 某个读法比标准码长 1，删掉任意一个字符后与标准码**完全相同**
          （纯插入型误读 —— 这是本轮最主要的漏识别形态，必须认出来）。
    """
    if actual and actual != "(无)" and actual in readings_text:
        return True
    for value in _split_readings(readings_text).values():
        if not value:
            continue
        if len(value) == len(standard) and _similarity(value, standard) >= len(standard) - 1:
            return True
        if len(value) == len(standard) + 1 and any(
            _similarity(value[:i] + value[i + 1:], standard) == len(standard)
            for i in range(len(value))
        ):
            return True
    return False


def _expected_length(args) -> int:
    profile = get_profile(args.profile)
    fmt: CodeFormat = profile.primary_format
    return fmt.length


# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="LiveLens 离线 OCR 准确率基准")
    parser.add_argument("--samples", help="样本目录（含 samples.csv）")
    parser.add_argument("--labels", help="人工标注 CSV（序号,标准房间码）")
    parser.add_argument("--make-labels", dest="make_labels",
                        help="为样本目录生成标注模板")
    parser.add_argument("--diagnostics", help="诊断 CSV（做已记录读法的离线对照）")
    parser.add_argument("--registry", help="异常清单 CSV")
    parser.add_argument("--round", help="只看清单里某一轮的条目（如 3）")
    parser.add_argument("--build-registry", action="store_true",
                        help="从两轮实测 CSV 重建异常清单")
    parser.add_argument("--round2-dir", help="第二轮导出目录")
    parser.add_argument("--round3-dir", help="第三轮导出目录")
    parser.add_argument("--profile", default=GAME_PROFILE_IDS[1],
                        help="判定「格式长度」用的游戏档（默认 1v1 的 8 位档）")
    parser.add_argument("--out", help="输出文件（逐样本明细 / 标注模板 / 清单）")
    args = parser.parse_args()

    if args.build_registry:
        if not args.out:
            args.out = os.path.join(PROJECT_ROOT, "ocr_anomaly_registry.csv")
        return build_registry(args)
    if args.make_labels:
        return make_labels(args)
    if args.samples:
        return run_samples(args)
    if args.diagnostics:
        return run_diagnostics(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
