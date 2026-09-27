# -*- coding: utf-8 -*-
"""
房间码生成器测试。覆盖任务书 §7 的 1-8 项。

只验证**实际生成出来的字符串**，不测"函数存在"。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from tools.ocr_dataset_generator.code_generator import (  # noqa: E402
    LETTERS, QuotaError, generate_balanced_codes, generate_code,
    generate_unique_codes, validate_code,
)
from tools.ocr_dataset_generator.schemas import FORMATS, FORMAT_IDS  # noqa: E402

SEED = 20260927


class TestFormatGeneration(unittest.TestCase):
    """[1] 三种格式生成正确 / [2] regex 全通过 / [3] 长度 / [4] 字符集"""

    def test_three_formats_and_shape(self):
        for fid, fmt in FORMATS.items():
            for code in generate_unique_codes(fid, 200, SEED):
                # [2] regex
                self.assertRegex(code, fmt.regex)
                # [3] 长度
                self.assertEqual(len(code), fmt.length,
                                 "%s 应为 %d 位: %r" % (fid, fmt.length, code))
                # [4] 字符集：前缀全大写字母、后缀全数字
                self.assertTrue(code[:fmt.letters].isalpha())
                self.assertTrue(code[:fmt.letters].isupper())
                self.assertTrue(all(c in LETTERS for c in code[:fmt.letters]))
                self.assertTrue(code[fmt.letters:].isdigit())
                # 不允许小写 / 字母数字交错（用"非字母即数字且顺序固定"整体判）
                self.assertEqual(re.sub(r"[A-Z0-9]", "", code), "")

    def test_lengths_are_exactly_6_7_8(self):
        self.assertEqual(len(generate_unique_codes("aaa123", 50, SEED)[0]), 6)
        self.assertEqual(len(generate_unique_codes("bbbb123", 50, SEED)[0]), 7)
        self.assertEqual(len(generate_unique_codes("aaaa1234", 50, SEED)[0]), 8)

    def test_generate_code_single(self):
        for fid, fmt in FORMATS.items():
            code = generate_code(fid)
            self.assertRegex(code, fmt.regex)

    def test_validate_code_rejects_bad_shapes(self):
        good = "ABC123"
        self.assertTrue(validate_code("aaa123", good))
        for bad in ("abc123", "ABC12", "ABC1234", "ABC 123", "ABC12D",
                    "A1C123", "AB-123", "", "ABC123\n"):
            self.assertFalse(validate_code("aaa123", bad),
                             "不该通过: %r" % bad)
        # 跨格式必须互不通过（不允许变形格式）
        self.assertFalse(validate_code("bbbb123", "ABC123"))
        self.assertFalse(validate_code("aaaa1234", "ABC123"))
        self.assertFalse(validate_code("aaa123", "ABCD1234"))
        # ABCD123 正好是 bbbb123 的合法形状，必须通过；跨格式要拿长度不符的来试
        self.assertTrue(validate_code("bbbb123", "ABCD123"))
        self.assertFalse(validate_code("bbbb123", "ABCD1234"))
        self.assertFalse(validate_code("aaaa1234", "ABCD123"))
        self.assertFalse(validate_code("aaaa1234", "ABCD12345"))

    def test_unknown_format_raises(self):
        with self.assertRaises(KeyError):
            generate_code("zzz999")


class TestUniquenessAndBalance(unittest.TestCase):
    """[5] 每种格式 100 个码无重复 / [6] 三种格式数量平衡"""

    def test_100_codes_unique_per_format(self):
        for fid in FORMAT_IDS:
            codes = generate_unique_codes(fid, 100, SEED)
            self.assertEqual(len(codes), 100)
            self.assertEqual(len(set(codes)), 100, "%s 出现重复码" % fid)

    def test_balanced_plan_counts(self):
        plan = generate_balanced_codes(per_format_codes=100,
                                       samples_per_code=20, seed=SEED)
        self.assertEqual(len(plan.formats), 3)
        for f in plan.formats:
            self.assertEqual(f.unique_code_count, 100)
            self.assertEqual(f.samples_per_code, 20)
            self.assertEqual(f.sample_count, 2000)     # 配额追踪到样本数
            self.assertEqual(len(set(f.codes)), 100)
        # [6] 平衡：三种格式的码数与样本数完全相等
        self.assertEqual({f.unique_code_count for f in plan.formats}, {100})
        self.assertEqual({f.sample_count for f in plan.formats}, {2000})
        self.assertEqual(plan.total_unique_codes, 300)
        self.assertEqual(plan.total_samples, 6000)

    def test_quota_error_when_exceeding_code_space(self):
        with self.assertRaises(QuotaError):
            generate_unique_codes("aaa123", FORMATS["aaa123"].code_space + 1, SEED)

    def test_quota_error_on_bad_params(self):
        with self.assertRaises(QuotaError):
            generate_balanced_codes(per_format_codes=0, samples_per_code=20)
        with self.assertRaises(QuotaError):
            generate_balanced_codes(per_format_codes=10, samples_per_code=0)


class TestReproducibility(unittest.TestCase):
    """[7] seed 相同可复现 / [8] seed 不同产生不同结果"""

    def test_same_seed_reproduces(self):
        a = generate_balanced_codes(20, 3, seed=SEED)
        b = generate_balanced_codes(20, 3, seed=SEED)
        self.assertEqual([f.codes for f in a.formats],
                         [f.codes for f in b.formats])

    def test_different_seed_differs(self):
        a = generate_balanced_codes(100, 2, seed=SEED)
        b = generate_balanced_codes(100, 2, seed=SEED + 1)
        for fa, fb in zip(a.formats, b.formats):
            self.assertNotEqual(fa.codes, fb.codes,
                                "%s 在不同 seed 下产出了同一批码" % fa.format_id)
            # 但也必须仍然合法
            for code in fb.codes:
                self.assertRegex(code, FORMATS[fb.format_id].regex)

    def test_format_isolation(self):
        """改一种格式的生成量，不应影响另一种格式的码（RNG 按格式派生）。"""
        a = generate_balanced_codes(30, 1, seed=SEED, formats=("aaa123", "aaaa1234"))
        b = generate_balanced_codes(30, 1, seed=SEED, formats=("aaa123",))
        self.assertEqual(a.formats[0].codes, b.formats[0].codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
