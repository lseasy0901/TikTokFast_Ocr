#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ROI 样本采集 + 离线准确率基准 —— 验证脚本

覆盖：
    A. 采集：默认关闭、开关、数量/字节上限、索引与图像落盘、写盘失败自动关闭
    B. 标注读取与结果统计：模板生成、labels 解析、判定分类、未标注不计入准确率
    C. 异常清单重建：从诊断 CSV + 复制记录 + 标准码对齐，并挑出代表帧
    D. 离线基准端到端：真引擎跑通、逐样本明细列齐全、耗时字段有值
    E. 诊断 CSV 模式：读法分布统计与「插入型误读」的识别

**本脚本不需要 Tesseract 就能跑「逻辑」部分**（A/B/C/E 全是纯逻辑）；
D 段要真引擎，装不了会明确 skip（不伪装成通过）。

D 段用的图像是**合成卡片**，只验证管线跑得通，**不产出任何 LiveLens
准确率结论** —— 真实准确率必须来自 ocr/sample_capture.py 采到的真实 ROI。

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_ocr_sample_capture_and_bench.py`` 运行，不依赖 pytest。
"""

import csv
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np                                              # noqa: E402

import bench_phase_ocr7_offline_accuracy as bench                # noqa: E402
from ocr.sample_capture import (                                # noqa: E402
    INDEX_HEADER,
    SampleCapture,
)

_PASSED = 0
_FAILED = 0
_SKIPPED = 0


def check(condition, message: str) -> bool:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {message}")
        return True
    _FAILED += 1
    print(f"  [FAIL] {message}")
    return False


def skip(message: str) -> None:
    global _SKIPPED
    _SKIPPED += 1
    print(f"  [SKIP] {message}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _image(w=200, h=60, value=200):
    return np.full((h, w, 3), value, dtype=np.uint8)


def _csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


# ======================================================================
# A. 采集
# ======================================================================
def test_capture() -> None:
    section("A. ROI 样本采集")

    root = tempfile.mkdtemp(prefix="dlv-cap-")
    capture = SampleCapture(os.path.join(root, "s"), max_samples=3,
                            max_bytes=10 ** 9)

    check(capture.enabled is False, "默认关闭")
    check(capture.capture(_image()) is None, "关闭时不落盘、直接返回 None")
    check(not os.path.exists(os.path.join(root, "s", "samples.csv")),
          "关闭时连索引文件都不会创建")

    capture.set_enabled(True)
    records = []
    for _ in range(5):
        records.append(capture.capture(
            _image(), profile_id="delta_force_1v1",
            readings=[(6, "ABC1234"), (7, "ABC1234")], candidate="ABC1234"))

    check(sum(1 for r in records if r is not None) == 3,
          "按数量上限只采到 3 个")
    check(capture.count == 3, f"计数 = {capture.count}")
    check(capture.skipped == 2, f"触顶未采如实计数 = {capture.skipped}")
    check(records[0] is not None and records[0].index == 1, "序号从 1 开始")
    check(records[3] is None and records[4] is None, "触顶后不再采集")

    image_dir = os.path.join(root, "s", "images")
    check(sorted(os.listdir(image_dir)) == ["000001.png", "000002.png", "000003.png"],
          "图像按序号落盘")
    check(os.path.getsize(os.path.join(image_dir, "000001.png")) > 0,
          "PNG 不是空文件")

    rows = _csv_rows(os.path.join(root, "s", "samples.csv"))
    check(rows[0] == list(INDEX_HEADER), f"索引表头 = {rows[0]}")
    check(len(rows) == 4, f"索引行数 = 记录数 + 表头（{len(rows)}）")
    check(rows[1][3] == "images/000001.png", "索引里的图像路径可拼接")
    check(rows[1][6] == "6:ABC1234 ‖ 7:ABC1234", f"读法被如实记录：{rows[1][6]}")
    check(rows[1][7] == "ABC1234", "候选被如实记录")
    check(all(len(r) == len(INDEX_HEADER) for r in rows), "每行列数一致")

    # 字节上限单独生效：落盘前不知道 PNG 多大，所以先写一个、之后停
    tiny = SampleCapture(os.path.join(root, "t"), max_samples=100, max_bytes=1)
    tiny.set_enabled(True)
    first = tiny.capture(_image())
    second = tiny.capture(_image())
    check(first is not None and second is None,
          "字节上限很小：写满一个后立即停止（预算是落盘后才可知的）")
    check(tiny.bytes_written >= 1, f"已写入字节被记账：{tiny.bytes_written}")
    check(tiny.skipped == 1, "字节触顶也计入 skipped")

    # 空图不采、异常不外抛
    capture2 = SampleCapture(os.path.join(root, "e"), max_samples=10)
    capture2.set_enabled(True)
    check(capture2.capture(None) is None, "None 图像不采集")
    check(capture2.capture(np.zeros((0, 0, 3), np.uint8)) is None, "空图不采集")

    # 写盘失败：自动关闭 + 计数，且不抛异常
    broken = SampleCapture(os.path.join(root, "\0bad"), max_samples=10)
    broken.set_enabled(True)
    result = broken.capture(_image())
    check(result is None and broken.failed is True,
          "写盘失败时自动关闭采集（不把异常抛进识别循环）")
    check("写盘失败" in broken.summary(), f"summary 如实说明：{broken.summary()}")

    check("已采集 3 个" in capture.summary(), f"summary：{capture.summary()}")


# ======================================================================
# B. 标注与统计
# ======================================================================
def test_labels_and_stats() -> None:
    section("B. 人工标注读取与结果统计")

    check(bench._verdict(None, "ABC1234") == "未标注", "没有标注 -> 未标注")
    check(bench._verdict("ABC1234", "ABC1234") == "正确", "候选等于标注 -> 正确")
    check(bench._verdict("ABC1234", None) == "无候选", "无候选")
    check(bench._verdict("ABC1234", "ABX1234") == "错误候选", "有候选但不同 -> 错误候选")

    # 标注模板：标准房间码必须留空（不能拿 OCR 自己的输出当答案）
    root = tempfile.mkdtemp(prefix="dlv-lab-")
    samples = os.path.join(root, "s")
    capture = SampleCapture(samples, max_samples=5)
    capture.set_enabled(True)
    capture.capture(_image(), profile_id="delta_force_1v1",
                    readings=[(6, "ABC1234")], candidate="ABC1234")

    class _Args:
        make_labels = samples
        out = None

    check(bench.make_labels(_Args()) == 0, "生成标注模板")
    rows = _csv_rows(os.path.join(samples, "labels.csv"))
    check(rows[0][-1] == "标准房间码(请填写)", f"模板表头：{rows[0]}")
    check(rows[1][-1] == "", "标准房间码一列留空，由人填写")
    check(rows[1][2] == "6:ABC1234", "识别读法作为参考线索保留")

    # labels 解析：只认填了标准房间码的行；小写会被折成大写
    labels_path = os.path.join(root, "labels.csv")
    with open(labels_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["序号", "标准房间码"])
        writer.writerow([1, "abcd1234"])
        writer.writerow([2, ""])
        writer.writerow([3, "WXYZ5678"])
    parsed = {}
    for row in bench._read_csv(labels_path):
        key = (row.get("序号") or "").strip()
        value = (row.get("标准房间码") or "").strip().upper()
        if key and value:
            parsed[int(key)] = value
    check(parsed == {1: "ABCD1234", 3: "WXYZ5678"},
          f"空标注被跳过、小写折成大写：{parsed}")


# ======================================================================
# C. 异常清单重建
# ======================================================================
def _write_round(directory, answers, copies, diagnostics):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "livelen_ocr_answers.csv"), "w",
              encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["序号", "标准房间码"])
        for index, code in answers:
            writer.writerow([index, code])
    with open(os.path.join(directory, "LiveLens-房间码-x.csv"), "w",
              encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["序号", "复制时间", "房间码"])
        for index, when, code in copies:
            writer.writerow([index, when, code])
    with open(os.path.join(directory, "LiveLens-OCR诊断-x.csv"), "w",
              encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, lineterminator="\r\n", fieldnames=[
            "序号", "时间", "游戏档", "格式", "耗时ms", "PSM读法", "各读法候选",
            "仲裁结果", "连续确认", "确认窗口", "复制请求", "复制结果", "备注"])
        writer.writeheader()
        writer.writerows(diagnostics)


def test_registry_rebuild() -> None:
    section("C. 异常清单重建（对齐 + 代表帧挑选）")

    root = tempfile.mkdtemp(prefix="dlv-reg-")
    directory = os.path.join(root, "r3")
    # 与真实两轮同一形状：漏与错【分开】，不互相干扰对齐
    answers = [(1, "AAAA1111"), (2, "BBBB2222"), (3, "CCCC3333"),
               (4, "DDDD4444"), (5, "EEEE5555"), (6, "FFFF6666")]
    copies = [(1, "10:00:00.000", "AAAA1111"),
              (2, "10:00:01.000", "BBBB2222"),
              (3, "10:00:02.000", "CCCX3333"),   # 第 3 码被错读后复制
              (4, "10:00:03.000", "DDDD4444"),
              (5, "10:00:04.000", "FFFF6666")]   # 第 5 码整段被漏
    diagnostics = [
        # 第 3 码被错复制的那一帧（带复制请求 —— 最权威的代表帧）
        {"序号": 10, "时间": "10:00:01.900", "游戏档": "delta_force_1v1", "格式": "f",
         "耗时ms": "20", "PSM读法": "6:CCCX3333", "各读法候选": "6:CCCX3333",
         "仲裁结果": "选定:CCCX3333", "连续确认": "成立", "确认窗口": "[]",
         "复制请求": "CCCX3333", "复制结果": "成功", "备注": ""},
        # 第 5 码显示期间的帧：插入型误读（9 字符），落在复制时刻之前
        {"序号": 11, "时间": "10:00:03.400", "游戏档": "delta_force_1v1", "格式": "f",
         "耗时ms": "20", "PSM读法": "6:EEE1E5555 ‖ 7:EEE1E5555 ‖ 11:EEE1E5555",
         "各读法候选": "6:- ‖ 7:- ‖ 11:-", "仲裁结果": "无候选",
         "连续确认": "未成立", "确认窗口": "[]",
         "复制请求": "", "复制结果": "", "备注": ""},
    ]
    _write_round(directory, answers, copies, diagnostics)

    class _Args:
        round2_dir = None
        round3_dir = directory
        out = os.path.join(root, "reg.csv")

    check(bench.build_registry(_Args()) == 0, "重建清单成功")
    rows = bench._read_csv(_Args.out)
    by_code = {r["标准房间码"]: r for r in rows}
    check(set(by_code) == {"CCCC3333", "EEEE5555"},
          f"只列出真正的异常（1 错 + 1 漏）：{sorted(by_code)}")
    check(by_code["EEEE5555"]["结果"] == "漏复制", "漏复制被识别")
    check(by_code["CCCC3333"]["结果"] == "错复制", "错复制被识别")
    check(by_code["CCCC3333"]["实际输出"] == "CCCX3333", "实际输出取自复制记录")
    check(by_code["EEEE5555"]["PSM6读法"] == "EEE1E5555",
          "漏复制挑到与标准码最接近的那一帧（插入型误读）")
    check(by_code["CCCC3333"]["PSM6读法"] == "CCCX3333",
          "错复制优先挑真正请求了复制的那一帧")
    check(all(r["图像样本"] == "待采集" for r in rows),
          "没有图像时一律标记「待采集」，不伪造")

    # 复制记录比标准序列还长时，跳过而不是编一条没有标准码的异常
    extra = os.path.join(root, "r4")
    _write_round(extra, [(1, "AAAA1111")],
                 [(1, "10:00:00.000", "AAAA1111"),
                  (2, "10:00:01.000", "ZZZZ9999")], diagnostics[:1])

    class _Args2:
        round2_dir = None
        round3_dir = extra
        out = os.path.join(root, "reg2.csv")

    check(bench.build_registry(_Args2()) == 0, "全是对不上的记录时也正常返回")
    check(bench._read_csv(_Args2.out) == [],
          "对不上标准序列的记录被跳过（不会产出没有标准码的异常）")


# ======================================================================
# D. 离线基准端到端（需要真引擎）
# ======================================================================
def test_offline_bench_end_to_end() -> None:
    section("D. 离线基准端到端（合成卡，仅验管线）")

    try:
        from ocr.engine import OCREngine
        engine = OCREngine()
        engine.shutdown()
    except Exception as exc:                                   # noqa: BLE001
        skip(f"本机没有可用的 Tesseract，跳过端到端：{exc}")
        return

    from bench_phase_ocr1_latency import make_card

    root = tempfile.mkdtemp(prefix="dlv-e2e-")
    samples = os.path.join(root, "s")
    capture = SampleCapture(samples, max_samples=10)
    capture.set_enabled(True)
    # 合成卡：只验证管线，**不是真实 ROI 样本**
    capture.capture(make_card("ABCD1234", False), profile_id="delta_force_1v1",
                    readings=[(6, "ABCD1234")], candidate="ABCD1234")
    capture.capture(make_card("WXYZ5678", False), profile_id="delta_force_1v1",
                    readings=[(6, "WXYZ5678")], candidate="WXYZ5678")
    noise = np.random.default_rng(7).integers(0, 255, (60, 200, 3), dtype=np.uint8)
    capture.capture(noise, profile_id="delta_force_1v1")

    labels_path = os.path.join(root, "labels.csv")
    with open(labels_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["序号", "标准房间码"])
        writer.writerow([1, "ABCD1234"])
        writer.writerow([2, "WXYZ5678"])

    detail_path = os.path.join(root, "detail.csv")

    class _Args:
        labels = labels_path
        out = detail_path
        profile = "delta_force_1v1"

    args = _Args()
    args.samples = samples          # 类体里不能写 `samples = samples`（同名会当成本地名）
    check(bench.run_samples(args) == 0, "基准跑通")
    rows = bench._read_csv(detail_path)
    check(len(rows) == 3, f"逐样本明细覆盖全部样本：{len(rows)}")
    check(list(rows[0].keys()) == [
        "序号", "时间", "游戏档", "标准房间码", "候选", "结果",
        "PSM6原文", "PSM7原文", "PSM11原文", "命中PSM",
        "全PSM耗时ms", "线上路径耗时ms", "图像文件"],
        "明细列齐全")
    verdicts = {int(r["序号"]): r["结果"] for r in rows}
    check(verdicts[3] == "未标注",
          f"没标注的样本记为未标注、不计入准确率：{verdicts}")
    check(float(rows[0]["全PSM耗时ms"]) > 0 and float(rows[0]["线上路径耗时ms"]) > 0,
          "两类耗时都被记录")
    check(rows[0]["PSM6原文"] != "", "PSM 6 原文被记录（真引擎输出）")

    # 未标注样本存在时，仍只对已标注的算准确率（不把噪声图算成错）
    check(all(int(r["序号"]) == 3 or r["结果"] in ("正确", "错误候选", "无候选")
              for r in rows), "已标注样本只落在三种判定里")


# ======================================================================
# E. 诊断 CSV 模式
# ======================================================================
def test_diagnostics_mode() -> None:
    section("E. 已记录读法的离线对照")

    check(bench._matches("BBBB2222", "(无)", "6:BBB1B2222"), "识别出插入型误读")
    check(bench._matches("BBBB2222", "(无)", "6:BBBB2222"), "等长单字符差也认")
    check(not bench._matches("BBBB2222", "(无)", "6:BBBB9999"),
          "差异过大不算该异常（避免同前缀误匹配）")
    check(not bench._matches("BBBB2222", "(无)", "6:XXXX1111"), "无关码不匹配")
    check(bench._matches("BBBB2222", "BBBX2222", "6:BBBX2222"),
          "被复制过的错码优先匹配")

    root = tempfile.mkdtemp(prefix="dlv-diag-")
    path = os.path.join(root, "diag.csv")
    rows = [
        {"序号": 1, "时间": "10:00:00.000", "PSM读法": "6:AAAA1111", "复制请求": ""},
        {"序号": 2, "时间": "10:00:00.300", "PSM读法": "6:BBB1B2222 ‖ 7:BBB1B2222",
         "复制请求": ""},
        {"序号": 3, "时间": "10:00:00.600", "PSM读法": "6:CCCC3333 ‖ 7:CCCX3333 ‖ 11:CCCC3333",
         "复制请求": ""},
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, lineterminator="\r\n",
                                fieldnames=["序号", "时间", "PSM读法", "复制请求"])
        writer.writeheader()
        writer.writerows(rows)

    class _Args:
        diagnostics = path
        registry = None
        profile = "delta_force_1v1"

    check(bench.run_diagnostics(_Args()) == 0, "诊断模式跑通")


def main() -> int:
    test_capture()
    test_labels_and_stats()
    test_registry_rebuild()
    test_offline_bench_end_to_end()
    test_diagnostics_mode()

    print(f"\n{'=' * 52}")
    print(f"通过 {_PASSED} 项，失败 {_FAILED} 项，跳过 {_SKIPPED} 项")
    print("=" * 52)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
