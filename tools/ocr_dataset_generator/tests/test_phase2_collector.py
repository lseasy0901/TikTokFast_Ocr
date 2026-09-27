# -*- coding: utf-8 -*-
"""
Phase 2 采集链路测试：capture / teacher / consensus / segment / review / dataset。

覆盖任务书 §16 的三条防泄漏测试与 §17 的 15 项。

测试里用**桩**帧源与桩 Teacher（合成图片只出现在测试夹具里）；
真实采集由 CLI 走 stream.FFmpegReader + ocr.ppocr_engine。
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, _ROOT)

from tools.ocr_dataset_generator.capture import (  # noqa: E402
    ROIRect, FrameSource, crop_roi, parse_roi,
)
from tools.ocr_dataset_generator.consensus import (  # noqa: E402
    ConsensusConfig, compute_consensus,
)
from tools.ocr_dataset_generator.review import (  # noqa: E402
    decide_review, decide_sample_review,
)
from tools.ocr_dataset_generator.runner import (  # noqa: E402
    CollectConfig, Collector, teacher_audit, write_real_dataset,
)
from tools.ocr_dataset_generator.schemas import (  # noqa: E402
    REAL_SAMPLE_FIELDS, REAL_TRUTH_FIELDS, TEACHER_RAW_FIELDS,
)
from tools.ocr_dataset_generator.segment import SegmentTracker  # noqa: E402
from tools.ocr_dataset_generator.teacher import (  # noqa: E402
    PPOCRTeacher, TeacherResult, normalize_text,
)
from tools.ocr_dataset_generator.verify import verify_dataset  # noqa: E402

FMT = "aaa123"          # [A-Z]{3}[0-9]{3}


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
class ScriptedSource(FrameSource):
    """按脚本吐帧的桩帧源。测试专用；真实采集走 FFmpegReader。"""

    def __init__(self, n):
        self.remaining = n
        self._size = (60, 200)

    def read(self):
        if self.remaining <= 0:
            return None
        self.remaining -= 1
        img = np.full((60, 200, 3), 30, np.uint8)
        img[10:50, 20:180] = 200
        return img

    @property
    def size(self):
        return self._size


class ScriptedTeacher:
    """按脚本返回读法的桩 Teacher。只接受图像。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.seen_args = []

    def infer(self, image):
        # 只记录"收到的东西是不是图像"，不接收任何期望值
        self.seen_args.append(type(image).__name__)
        text = self.script[self.i] if self.i < len(self.script) else ""
        self.i += 1
        return TeacherResult(raw_text=text or "", normalized_text=normalize_text(text),
                             score=0.9 if text else None, ok=bool(text),
                             error="" if text else "无结果")

    def close(self):
        return None


def make_collector(tmp, script, *, gt=None, frames=None, **kw):
    cfg = CollectConfig(
        format_id=FMT, roi=ROIRect(0, 0, 200, 60),
        dataset_dir=os.path.join(tmp, kw.pop("name", "ds")),
        session_id=kw.pop("session_id", "S001"), ground_truth=gt, **kw)
    # max_empty_reads 调小：桩源耗尽后立刻收工，避免测试空转
    cfg.max_empty_reads = 3
    teacher = ScriptedTeacher(script)
    col = Collector(cfg, ScriptedSource(frames or len(script) + 4), teacher,
                    log=lambda *a: None)
    return cfg, col, teacher


# ----------------------------------------------------------------------
# §17-1..3 schema
# ----------------------------------------------------------------------
class TestSchemas(unittest.TestCase):

    def test_sample_schema_has_no_ground_truth(self):
        """§17-1 / §16：samples.csv 的字段里没有答案。"""
        for bad in ("ground_truth", "room_code", "teacher_consensus", "label"):
            self.assertNotIn(bad, REAL_SAMPLE_FIELDS)
        self.assertEqual(list(REAL_SAMPLE_FIELDS)[:2], ["sample_id", "image"])

    def test_truth_schema(self):
        """§17-2"""
        self.assertEqual(list(REAL_TRUTH_FIELDS),
                         ["sample_id", "ground_truth", "label_source",
                          "review_status"])

    def test_teacher_raw_schema(self):
        """§17-3：原始输出必须完整保留"""
        for col in ("raw_text", "normalized_text", "score", "elapsed_ms", "ok"):
            self.assertIn(col, TEACHER_RAW_FIELDS)


# ----------------------------------------------------------------------
# §17-4..5 consensus
# ----------------------------------------------------------------------
class TestConsensus(unittest.TestCase):

    def test_majority_correct(self):
        """§17-4"""
        r = compute_consensus(["ABC123"] * 5 + ["XYZ789"], FMT)
        self.assertEqual(r.code, "ABC123")
        self.assertEqual(r.votes, 5)
        self.assertEqual(r.valid_votes, 6)
        self.assertEqual(r.unique_candidate_count, 2)
        self.assertAlmostEqual(r.majority_ratio, 5 / 6, places=4)
        self.assertEqual(r.max_consecutive, 5)

    def test_no_result_and_conflict(self):
        self.assertEqual(compute_consensus(["", None, ""], FMT).reason,
                         "teacher_no_result")
        self.assertEqual(compute_consensus(["ABC123", "XYZ789"], FMT).reason,
                         "teacher_conflict")

    def test_thresholds_are_configurable(self):
        """§17-5：阈值改变时判定跟着变，说明没有写死。"""
        readings = ["ABC123"] * 3
        loose = ConsensusConfig(min_votes=2, majority_ratio=0.5, min_consecutive=2)
        strict = ConsensusConfig(min_votes=5, majority_ratio=0.99, min_consecutive=9)
        self.assertTrue(compute_consensus(readings, FMT, loose).high_confidence)
        self.assertFalse(compute_consensus(readings, FMT, strict).high_confidence)
        self.assertEqual(compute_consensus(readings, FMT, strict).reason,
                         "insufficient_votes")

    def test_format_invalid_flagged(self):
        r = compute_consensus(["ABCDEF"] * 4, FMT)      # 形状不合法
        self.assertFalse(r.format_valid)
        self.assertFalse(r.high_confidence)
        self.assertEqual(r.reason, "format_invalid")


# ----------------------------------------------------------------------
# §17-6..7 review
# ----------------------------------------------------------------------
class TestReview(unittest.TestCase):

    def _seg(self, gt=None, readings=("ABC123",) * 4):
        cfg, col, _ = make_collector(tempfile.mkdtemp(), [])
        tr = SegmentTracker("S1", FMT, config=ConsensusConfig())
        for i, r in enumerate(readings):
            tr.observe(r, i * 100, True)
        seg = tr.close()
        seg.ground_truth = gt
        return seg

    def test_invalid_format_goes_to_review(self):
        """§17-6"""
        seg = self._seg(gt="ABC123", readings=("ABCDEF",) * 4)
        d = decide_review(seg, compute_consensus(list(seg.readings), FMT))
        self.assertTrue(d.needed)
        self.assertIn("format_invalid", d.reason)

    def test_teacher_gt_mismatch_goes_to_review(self):
        """§17-7"""
        seg = self._seg(gt="AAA111", readings=("ABC123",) * 5)
        c = compute_consensus(list(seg.readings), FMT)
        self.assertNotEqual(c.code, seg.ground_truth)
        d = decide_review(seg, c)
        self.assertTrue(d.needed)
        self.assertIn("truth_teacher_mismatch", d.reason)

    def test_no_manual_truth_goes_to_review(self):
        seg = self._seg(gt=None, readings=("ABC123",) * 5)
        d = decide_review(seg, compute_consensus(list(seg.readings), FMT))
        self.assertTrue(d.needed)
        self.assertIn("unresolved_no_manual_truth", d.reason)

    def test_clean_segment_not_in_review(self):
        seg = self._seg(gt="ABC123", readings=("ABC123",) * 5)
        d = decide_review(seg, compute_consensus(list(seg.readings), FMT))
        self.assertFalse(d.needed, d.reason)

    def test_sample_level_reasons(self):
        self.assertIn("duplicate_exact",
                      decide_sample_review(exact_dup=True).reason)
        self.assertIn("sequence_boundary",
                      decide_sample_review(boundary=True).reason)
        self.assertFalse(decide_sample_review().needed)


# ----------------------------------------------------------------------
# §17-9..10 segment 生命周期
# ----------------------------------------------------------------------
class TestSegment(unittest.TestCase):

    def test_lifecycle(self):
        """§17-9"""
        tr = SegmentTracker("S1", FMT, switch_votes=1, config=ConsensusConfig())
        tr.observe("ABC123", 0, True)
        tr.observe("ABC123", 100, True)
        closed = tr.observe("XYZ789", 200, True)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.readings, ["ABC123", "ABC123"])
        self.assertEqual(closed.start_timestamp_ms, 0)
        self.assertEqual(closed.end_timestamp_ms, 100)
        tr.observe("XYZ789", 300, True)
        last = tr.close()
        self.assertEqual(last.readings, ["XYZ789", "XYZ789"])
        self.assertEqual([s.segment_id for s in tr.closed],
                         ["SEG0001", "SEG0002"])

    def test_invalid_readings_do_not_split(self):
        tr = SegmentTracker("S1", FMT, switch_votes=1)
        tr.observe("ABC123", 0, True)
        tr.observe("GARBAGE", 100, False)     # 不合法 -> 不切
        tr.observe("ABC123", 200, True)
        seg = tr.close()
        self.assertEqual(seg.readings, ["ABC123", "ABC123"])

    def test_gap_closes_segment(self):
        tr = SegmentTracker("S1", FMT, gap_ms=1000)
        tr.observe("ABC123", 0, True)
        closed = tr.observe("ABC123", 5000, True)   # 超过 gap -> 先关旧的
        self.assertIsNotNone(closed)

    def test_truth_status_never_uses_teacher(self):
        cfg, col, _ = make_collector(tempfile.mkdtemp(), [])
        tr = SegmentTracker("S1", FMT)
        tr.observe("ABC123", 0, True)
        seg = tr.close()
        self.assertEqual(seg.truth_status, "unresolved")   # 没人工输入就是 unresolved
        seg.ground_truth = "ABC123"
        self.assertEqual(seg.truth_status, "confirmed")


# ----------------------------------------------------------------------
# 端到端
# ----------------------------------------------------------------------
class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlv-p2-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, gt="ABC123", name="ds_e2e", **kw):
        cfg, col, _ = make_collector(self.tmp, script, gt=gt, name=name, **kw)
        res = col.run()
        meta = write_real_dataset(cfg.dataset_dir, cfg, col.rows,
                                  col.tracker.closed, res)
        return cfg.dataset_dir, meta, col

    def test_files_and_manifest(self):
        """§17-11：文件齐、manifest 覆盖、verify 通过"""
        d, meta, _ = self._run(["ABC123"] * 12, stable_interval_ms=0,
                               burst_size=1, burst_interval_ms=0)
        for f in ("images", "samples.csv", "truth.csv", "teacher_raw.csv",
                  "teacher_consensus.csv", "segments.csv", "review_queue.csv",
                  "dataset.json", "manifest.sha256", "README.md"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), "缺少 %s" % f)
        problems, info = verify_dataset(d)
        self.assertEqual(problems, [])
        self.assertEqual(info["samples"], meta["counts"]["samples"])
        listed = [l for l in open(os.path.join(d, "manifest.sha256"),
                                  encoding="utf-8") if l.strip()]
        self.assertGreaterEqual(len(listed), 5)

    def test_ground_truth_not_in_samples(self):
        """§17-13 / §16：答案不得出现在 samples.csv"""
        d, _, _ = self._run(["ABC123"] * 12, gt="ABC123",
                            stable_interval_ms=0, burst_size=1,
                            burst_interval_ms=0)
        with open(os.path.join(d, "samples.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0].keys()), list(REAL_SAMPLE_FIELDS))
        raw_text = open(os.path.join(d, "samples.csv"), encoding="utf-8-sig").read()
        self.assertNotIn("ABC123", raw_text)
        self.assertNotIn("ground_truth", raw_text)
        # 但 truth.csv 里必须有
        with open(os.path.join(d, "truth.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            trows = list(csv.DictReader(fh))
        self.assertEqual({r["ground_truth"] for r in trows}, {"ABC123"})
        self.assertEqual({r["label_source"] for r in trows}, {"manual"})

    def test_no_manual_truth_stays_unresolved(self):
        d, meta, _ = self._run(["ABC123"] * 10, gt=None,
                               stable_interval_ms=0, burst_size=1,
                               burst_interval_ms=0)
        with open(os.path.join(d, "truth.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual({r["label_source"] for r in rows}, {"unresolved"})
        self.assertEqual({r["ground_truth"] for r in rows}, {""})
        # 必须全部进复核
        with open(os.path.join(d, "review_queue.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            rq = list(csv.DictReader(fh))
        self.assertEqual(len(rq), meta["counts"]["samples"])
        self.assertTrue(all("unresolved_no_manual_truth" in r["reason"]
                            for r in rq))

    def test_samples_without_segment_are_not_dropped(self):
        """回归：Teacher 读法不满足目标格式时，样本必须仍有 truth 行 + 复核项。

        实测缺陷：早先 truth.csv 是按 segment 展开的，一个 segment 都没形成时
        truth.csv 只剩表头，90 个样本被静默丢掉。
        """
        d, meta, col = self._run(["NOTACODE"] * 8, gt=None,
                                 stable_interval_ms=0, burst_size=1,
                                 burst_interval_ms=0)
        self.assertEqual(meta["counts"]["segments"], 0)
        with open(os.path.join(d, "truth.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            trows = list(csv.DictReader(fh))
        self.assertEqual(len(trows), meta["counts"]["samples"])
        self.assertEqual({r["label_source"] for r in trows}, {"unresolved"})
        with open(os.path.join(d, "review_queue.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            rq = list(csv.DictReader(fh))
        self.assertEqual(len(rq), meta["counts"]["samples"])
        # 读法不满足格式 -> no_segment；帧源耗尽后的空读 -> teacher_no_result。
        # 两种都是"没被丢掉"，只是原因不同。
        self.assertTrue(all(r["reason"] in ("no_segment", "teacher_no_result")
                            for r in rq), {r["reason"] for r in rq})
        self.assertGreaterEqual(
            sum(1 for r in rq if r["reason"] == "no_segment"), 1)

    def test_finalize_round_trip_and_late_ground_truth(self):
        """§18：finalize 能重跑聚合，也能事后补录人工真值。"""
        from tools.ocr_dataset_generator.finalize import finalize_dataset
        d, meta, _ = self._run(["ABC123"] * 6 + ["NOTACODE"] * 4, gt=None,
                               stable_interval_ms=0, burst_size=1,
                               burst_interval_ms=0)
        n = meta["counts"]["samples"]
        # 幂等重跑
        m2 = finalize_dataset(d)
        self.assertEqual(m2["counts"]["samples"], n)
        self.assertEqual(verify_dataset(d)[0], [])
        # 事后按段补录真值
        seg_id = None
        with open(os.path.join(d, "segments.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            segs = list(csv.DictReader(fh))
        if segs:
            seg_id = segs[0]["segment_id"]
            finalize_dataset(d, segment_truth={seg_id: "ABC123"})
            with open(os.path.join(d, "truth.csv"), encoding="utf-8-sig",
                      newline="") as fh:
                trows = list(csv.DictReader(fh))
            with open(os.path.join(d, "samples.csv"), encoding="utf-8-sig",
                      newline="") as fh:
                in_seg = {r["sample_id"] for r in csv.DictReader(fh)
                          if r["segment_id"] == seg_id}
            manual = {r["sample_id"] for r in trows
                      if r["label_source"] == "manual"}
            self.assertEqual(manual, in_seg)
            self.assertTrue(all(r["ground_truth"] == "ABC123"
                                for r in trows if r["sample_id"] in in_seg))
        self.assertEqual(verify_dataset(d)[0], [])

    def test_ids_unique(self):
        """§17-10"""
        d, meta, col = self._run(["ABC123"] * 30, stable_interval_ms=0,
                                 burst_size=2, burst_interval_ms=0)
        with open(os.path.join(d, "samples.csv"), encoding="utf-8-sig",
                  newline="") as fh:
            ids = [r["sample_id"] for r in csv.DictReader(fh)]
        self.assertEqual(len(set(ids)), len(ids))
        segs = {s.segment_id for s in col.tracker.closed}
        self.assertEqual(len(segs), len(col.tracker.closed))

    def test_duplicate_reported_not_deleted(self):
        """§17-8：重复只报告、不删样本"""
        cfg = CollectConfig(format_id=FMT, roi=ROIRect(0, 0, 200, 60),
                            dataset_dir=os.path.join(self.tmp, "dup"),
                            session_id="S1", ground_truth="ABC123",
                            stable_interval_ms=0, burst_size=1,
                            burst_interval_ms=0)
        cfg.max_empty_reads = 3
        col = Collector(cfg, ScriptedSource(20), ScriptedTeacher(["ABC123"] * 20),
                        log=lambda *a: None)
        res = col.run()
        meta = write_real_dataset(cfg.dataset_dir, cfg, col.rows,
                                  col.tracker.closed, res)
        n_before = meta["counts"]["samples"]
        # 桩帧源每帧像素完全相同 -> 必然有重复
        self.assertGreater(meta["dedupe"]["exact_duplicate_groups"], 0)
        self.assertGreater(len(meta["dedupe"]["exact_duplicate_files"]), 0)
        # 但样本数没被删减
        with open(os.path.join(cfg.dataset_dir, "samples.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), n_before)
        self.assertIn("只报告", meta["dedupe"]["policy"])

    def test_burst_on_code_change(self):
        """§5：房间码变化时采连续帧"""
        d, meta, col = self._run(
            ["ABC123"] * 6 + ["XYZ789"] * 12, gt=None,
            stable_interval_ms=0, burst_size=3, burst_interval_ms=0)
        seqs = {r["sequence"] for r in col.rows}
        self.assertIn("burst", seqs)
        self.assertIn("stable", seqs)
        self.assertGreaterEqual(meta["counts"]["bursts"], 1)

    def test_teacher_audit_by_format(self):
        """§15：Teacher 审计只在有人工真值时计入"""
        rows = [
            {"format_id": "aaa123", "normalized_text": "ABC123", "_gt": "ABC123"},
            {"format_id": "aaa123", "normalized_text": "ABC124", "_gt": "ABC123"},
            {"format_id": "aaa123", "normalized_text": "", "_gt": "ABC123"},
            {"format_id": "aaa123", "normalized_text": "ABCDEF", "_gt": "ABC123"},
            {"format_id": "bbb123", "normalized_text": "", "_gt": ""},
        ]
        a = teacher_audit(rows)
        self.assertEqual(a["aaa123"]["samples"], 4)
        self.assertEqual(a["aaa123"]["exact_match"], 1)
        self.assertEqual(a["aaa123"]["no_read"], 1)
        self.assertEqual(a["aaa123"]["wrong"], 2)      # ABC124 与 ABCDEF
        # "" 与 "ABCDEF" 都不满足 aaa123 的形状，两条都算 format_invalid
        self.assertEqual(a["aaa123"]["format_invalid"], 2)
        self.assertEqual(a["_total"]["samples"], 4)     # 无 GT 的那条不进分母
        self.assertNotIn("bbb123", a)


# ----------------------------------------------------------------------
# §17-12 CLI
# ----------------------------------------------------------------------
class TestCLIHelp(unittest.TestCase):

    def _run(self, args):
        return subprocess.run([sys.executable, "-m", "tools.ocr_dataset_generator"] + args,
                              cwd=_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_help_all_subcommands(self):
        """§17-12"""
        for args in (["--help"], ["collect", "--help"], ["finalize", "--help"],
                     ["verify", "--help"]):
            r = self._run(args)
            self.assertEqual(r.returncode, 0, "%s -> %s" % (args, r.stderr[-300:]))
            self.assertIn("usage", (r.stdout or "").lower())

    def test_collect_requires_source_and_roi(self):
        r = self._run(["collect", "--format", "aaa123"])
        self.assertNotEqual(r.returncode, 0)      # 缺 --url/--replay-dir 与 --roi

    def test_verify_on_missing_dir_fails(self):
        r = self._run(["verify", os.path.join(self.cwd_missing())])
        self.assertNotEqual(r.returncode, 0)

    @staticmethod
    def cwd_missing():
        return os.path.join(tempfile.gettempdir(), "definitely-not-a-dataset-xyz")


# ----------------------------------------------------------------------
# §16 / §17-14,15 隔离性
# ----------------------------------------------------------------------
class TestIsolation(unittest.TestCase):

    def test_teacher_adapter_accepts_only_image(self):
        """§16：infer 的签名里没有任何"期望值"参数。"""
        import inspect
        sig = inspect.signature(PPOCRTeacher.infer)
        params = [p for p in sig.parameters if p != "self"]
        self.assertEqual(params, ["image"])
        for bad in ("truth", "expected", "path", "ground_truth", "code", "label"):
            self.assertNotIn(bad, params)

    def test_teacher_does_not_read_truth(self):
        """§16：Teacher 的**代码**里没有任何读答案的路径。

        查 AST 而不是查源码文本：文档字符串里写"我不读 truth.csv"是说明，
        不是行为；只有代码里的字符串常量与调用才算数。
        """
        import ast
        p = os.path.join(_ROOT, "tools", "ocr_dataset_generator", "teacher.py")
        tree = ast.parse(open(p, encoding="utf-8").read())
        # 模块级 docstring 与所有 docstring 都不算（ast 里它们是 Constant，
        # 这里只排除 docstring 节点）
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds:
                    docstrings.add(ds)
        consts = {n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        consts -= docstrings
        for bad in ("truth.csv", "ground_truth", "room_code", "dataset.json"):
            hits = [c for c in consts if bad in c]
            self.assertEqual(hits, [], "teacher.py 代码里出现 %r: %s" % (bad, hits))
        # 也不该有任何文件读取调用
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                self.assertNotIn(name, ("open", "read_csv", "DictReader"),
                                 "teacher.py 不该读文件")

    def test_import_has_no_production_side_effects(self):
        """§17-14/15：导入数据集工具不得连带初始化生产模块。"""
        code = (
            "import sys;"
            "import tools.ocr_dataset_generator as g;"
            "import tools.ocr_dataset_generator.runner,"
            " tools.ocr_dataset_generator.cli,"
            " tools.ocr_dataset_generator.verify,"
            " tools.ocr_dataset_generator.finalize;"
            "bad=[m for m in ('ocr','gui','stream','utils','main')"
            " if m in sys.modules];"
            "print('BAD='+','.join(bad))"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("BAD=", r.stdout)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "BAD=",
                         "导入把生产模块也拉进来了")

    def test_capture_import_is_lazy(self):
        """capture 模块顶层不 import stream（只在 open() 里按需导入）。"""
        p = os.path.join(_ROOT, "tools", "ocr_dataset_generator", "capture.py")
        src = open(p, encoding="utf-8").read()
        top = src.split("def ", 1)[0]
        self.assertNotIn("import stream", top)
        self.assertIn("from stream.ffmpeg_reader import", src)


# ----------------------------------------------------------------------
# ROI
# ----------------------------------------------------------------------
class TestROI(unittest.TestCase):

    def test_parse_and_crop(self):
        self.assertEqual(parse_roi("10,20,30,40").as_tuple(), (10, 20, 30, 40))
        self.assertEqual(parse_roi("10x20x30x40").as_tuple(), (10, 20, 30, 40))
        with self.assertRaises(ValueError):
            parse_roi("1,2,3")
        with self.assertRaises(ValueError):
            parse_roi("1,2,0,4")

    def test_crop_matches_worker_semantics(self):
        img = np.arange(60 * 200 * 3, dtype=np.uint8).reshape(60, 200, 3)
        out = crop_roi(img, ROIRect(10, 5, 30, 20))
        self.assertEqual(out.shape, (20, 30, 3))
        self.assertTrue(np.array_equal(out, img[5:25, 10:40]))

    def test_out_of_range_scales(self):
        img = np.zeros((100, 100, 3), np.uint8)
        roi = ROIRect(0, 0, 200, 200, source_width=400, source_height=400)
        self.assertEqual(crop_roi(img, roi).shape[:2], (50, 50))


if __name__ == "__main__":
    unittest.main(verbosity=2)
