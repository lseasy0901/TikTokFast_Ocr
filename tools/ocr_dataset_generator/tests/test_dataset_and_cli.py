# -*- coding: utf-8 -*-
"""
数据集落盘 / 去重 / CLI 测试。覆盖任务书 §7 的 9-10 项与 §4/§5/§6 的结构要求。
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))

import numpy as np  # noqa: E402

from tools.ocr_dataset_generator import cli  # noqa: E402
from tools.ocr_dataset_generator.code_generator import (  # noqa: E402
    generate_balanced_codes,
)
from tools.ocr_dataset_generator.dataset import (  # noqa: E402
    create_dataset, verify_manifest,
)
from tools.ocr_dataset_generator.dedupe import (  # noqa: E402
    dhash_from_array, find_exact_duplicates, hamming_distance, sha256_bytes,
    sha256_file,
)
from tools.ocr_dataset_generator.schemas import (  # noqa: E402
    SAMPLE_FIELDS, TRUTH_FIELDS,
)

SEED = 20260927


class TestDedupe(unittest.TestCase):
    """[9] SHA-256 正常工作"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlv-dedupe-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, data: bytes):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_sha256_file_matches_hashlib(self):
        import hashlib
        payload = b"LiveLens OCR dataset" * 1000
        p = self._write("a.bin", payload)
        self.assertEqual(sha256_file(p), hashlib.sha256(payload).hexdigest())
        self.assertEqual(sha256_bytes(payload), sha256_file(p))

    def test_sha256_empty_and_large(self):
        import hashlib
        self.assertEqual(sha256_bytes(b""),
                         hashlib.sha256(b"").hexdigest())
        big = os.urandom(3 * 1024 * 1024 + 7)     # 跨多块
        p = self._write("big.bin", big)
        self.assertEqual(sha256_file(p), hashlib.sha256(big).hexdigest())

    def test_exact_duplicates_detected(self):
        a = self._write("a.bin", b"same")
        b = self._write("b.bin", b"same")
        c = self._write("c.bin", b"different")
        groups = find_exact_duplicates([a, b, c])
        self.assertEqual(len(groups), 1)
        only = list(groups.values())[0]
        self.assertEqual(sorted(os.path.basename(x) for x in only), ["a.bin", "b.bin"])

    def test_perceptual_hash_and_distance(self):
        # 同一图 → 距离 0；加噪后 → 很小；不同图 → 明显更大
        base = (np.arange(0, 64 * 64, dtype="uint8").reshape(64, 64))
        same = dhash_from_array(base)
        self.assertEqual(hamming_distance(same, dhash_from_array(base)), 0)
        rng = np.random.default_rng(0)
        noise = np.clip(base.astype(int) + rng.integers(-2, 3, base.shape),
                        0, 255).astype("uint8")
        near = hamming_distance(same, dhash_from_array(noise))
        other = dhash_from_array(rng.integers(0, 255, (64, 64), dtype="uint8"))
        far = hamming_distance(same, other)
        self.assertLessEqual(near, 8)
        self.assertGreaterEqual(far, near)
        self.assertEqual(len(same), 16)     # 64 bit → 16 hex 字符
        with self.assertRaises(ValueError):
            hamming_distance("ff", "ffff")


class TestDatasetStructure(unittest.TestCase):
    """§4 目录结构 / §5 去重不破坏配额"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlv-ds-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, codes=10, spc=3, name="ds"):
        plan = generate_balanced_codes(codes, spc, seed=SEED)
        return plan, create_dataset(self.tmp, plan, codes, spc, name=name)

    def test_required_files_exist(self):
        _plan, res = self._make()
        for name in ("images", "samples.csv", "truth.csv", "dataset.json",
                     "manifest.sha256", "README.md"):
            self.assertTrue(os.path.exists(os.path.join(res.root, name)),
                            "缺少 %s" % name)
        self.assertTrue(os.path.isdir(os.path.join(res.root, "images")))

    def test_samples_csv_schema_and_counts(self):
        plan, res = self._make(codes=10, spc=3)
        with open(os.path.join(res.root, "samples.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0].keys()), list(SAMPLE_FIELDS))
        self.assertEqual(len(rows), plan.total_samples)
        self.assertEqual(len({r["sample_id"] for r in rows}), len(rows))
        # 配额：每个房间码恰好 spc 个样本
        per_code = {}
        for r in rows:
            per_code[(r["format_id"], r["room_code"])] = \
                per_code.get((r["format_id"], r["room_code"]), 0) + 1
        self.assertEqual(len(per_code), plan.total_unique_codes)
        self.assertEqual(set(per_code.values()), {3})
        # 每个样本的格式与码必须自洽（用生成时的正则复验）
        from tools.ocr_dataset_generator.schemas import FORMATS
        for r in rows:
            self.assertRegex(r["room_code"], FORMATS[r["format_id"]].regex)

    def test_truth_csv_schema(self):
        _plan, res = self._make()
        with open(os.path.join(res.root, "truth.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0].keys()), list(TRUTH_FIELDS))
        self.assertEqual({r["validation_status"] for r in rows}, {"confirmed"})
        self.assertEqual({r["source"] for r in rows}, {"generator"})

    def test_no_images_generated(self):
        """本阶段不生成图片：images/ 里除占位文件外必须为空。"""
        _plan, res = self._make()
        files = [f for f in os.listdir(os.path.join(res.root, "images"))]
        self.assertEqual(files, [".gitkeep"])

    def test_dataset_json_tracks_quota_not_just_total(self):
        plan, res = self._make(codes=7, spc=4)
        with open(os.path.join(res.root, "dataset.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["seed"], SEED)
        self.assertEqual(meta["totals"]["formats"], 3)
        self.assertEqual(meta["totals"]["unique_codes"], 21)
        self.assertEqual(meta["totals"]["samples"], 84)
        for f in meta["formats"]:
            self.assertEqual(f["unique_code_count"], 7)
            self.assertEqual(f["samples_per_code"], 4)
            self.assertEqual(f["sample_count"], 28)
            self.assertIn("regex", f)
        # 预留字段只声明，不写进 CSV
        self.assertIn("teacher_raw", meta["sample_schema"]["reserved_fields"])
        self.assertNotIn("teacher_raw", meta["sample_schema"]["fields"])
        self.assertFalse(meta["images"]["present"])

    def test_manifest_covers_files_and_verifies(self):
        _plan, res = self._make()
        self.assertEqual(verify_manifest(res.root), [])
        with open(os.path.join(res.root, "manifest.sha256"),
                  encoding="utf-8") as fh:
            listed = fh.read()
        for name in ("samples.csv", "truth.csv", "dataset.json", "README.md"):
            self.assertIn(name, listed)
        self.assertNotIn("manifest.sha256  ", listed)   # 不自指

    def test_manifest_detects_tampering(self):
        _plan, res = self._make()
        with open(os.path.join(res.root, "samples.csv"), "a", encoding="utf-8") as fh:
            fh.write("x\r\n")
        problems = verify_manifest(res.root)
        self.assertTrue(any("SHA-256 不符" in p for p in problems), problems)

    def test_dedupe_report_does_not_change_quota(self):
        """去重只报告，不能把配额改小。"""
        dup = os.path.join(self.tmp, "dup_a.bin")
        dup2 = os.path.join(self.tmp, "dup_b.bin")
        for p in (dup, dup2):
            with open(p, "wb") as fh:
                fh.write(b"identical")
        plan = generate_balanced_codes(5, 2, seed=SEED)
        res = create_dataset(self.tmp, plan, 5, 2, name="ds_dup",
                             dedupe_paths=[dup, dup2])
        with open(os.path.join(res.root, "dataset.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["dedupe"]["sha256_duplicate_groups"], 1)
        self.assertEqual(len(meta["dedupe"]["sha256_duplicate_files"]), 2)
        # 配额不变
        self.assertEqual(meta["totals"]["samples"], plan.total_samples)
        with open(os.path.join(res.root, "samples.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), plan.total_samples)


class TestCLI(unittest.TestCase):
    """[10] CLI 正常创建 dataset metadata"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlv-cli-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_creates_dataset(self):
        rc = cli.main(["--output", self.tmp, "--seed", str(SEED),
                       "--codes-per-format", "100",
                       "--samples-per-code", "20",
                       "--name", "cli_ds"])
        self.assertEqual(rc, 0)
        root = os.path.join(self.tmp, "cli_ds")
        with open(os.path.join(root, "dataset.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["totals"]["unique_codes"], 300)
        self.assertEqual(meta["totals"]["samples"], 6000)
        self.assertEqual(meta["seed"], SEED)
        with open(os.path.join(root, "samples.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 6000)
        self.assertEqual(verify_manifest(root), [])

    def test_cli_dry_run_writes_nothing(self):
        rc = cli.main(["--output", self.tmp, "--dry-run", "--name", "nope"])
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_cli_formats_subset(self):
        rc = cli.main(["--output", self.tmp, "--formats", "aaa123",
                       "--codes-per-format", "5", "--samples-per-code", "2",
                       "--name", "one_fmt"])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.tmp, "one_fmt", "dataset.json"),
                  encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["totals"]["formats"], 1)
        self.assertEqual(meta["totals"]["samples"], 10)
        self.assertEqual(meta["formats"][0]["format_id"], "aaa123")

    def test_cli_bad_format_exits(self):
        with self.assertRaises(SystemExit):
            cli.main(["--output", self.tmp, "--formats", "zzz999"])

    def test_cli_refuses_to_overwrite(self):
        for _ in range(2):
            rc = cli.main(["--output", self.tmp, "--codes-per-format", "2",
                           "--samples-per-code", "1", "--name", "same"])
        self.assertEqual(rc, 3)   # 第二次被 FileExistsError 拦下


if __name__ == "__main__":
    unittest.main(verbosity=2)
