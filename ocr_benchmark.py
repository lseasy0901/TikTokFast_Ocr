#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR Benchmark Phase 4.2

Benchmark PSM 6/7/11 configurations for English letter/digit recognition.
Measures character completeness, errors, and processing time.
"""

import sys
import os
import time
import statistics
from typing import List, Dict, Tuple, Set
from dataclasses import dataclass
import numpy as np
import cv2

# Add project root to path
project_root = os.path.dirname(os.path.abspath('.'))
sys.path.insert(0, project_root)

from ocr.engine import OCREngine


@dataclass
class BenchmarkCase:
    """测试用例"""
    name: str
    text: str
    description: str
    expected_chars: Set[str]


@dataclass
class BenchmarkResult:
    """单次测试结果"""
    raw_ocr: str
    final_ocr: str
    processing_time: float
    completeness: float
    missing_chars: Set[str]
    extra_chars: Set[str]
    incorrect_chars: Set[str]


@dataclass
class PSMStats:
    """PSM统计信息"""
    psm: int
    total_tests: int
    avg_completeness: float
    avg_time: float
    min_completeness: float
    max_completeness: float
    completeness_std: float
    error_cases: List[str]


def create_test_images() -> List[BenchmarkCase]:
    """创建测试用例"""
    test_cases = [
        BenchmarkCase(
            name="simple_english",
            text="Hello world",
            description="简单英文",
            expected_chars=set("Helloworld")
        ),
        BenchmarkCase(
            name="with_punctuation",
            text="Hello, world!",
            description="包含标点",
            expected_chars=set("Helloworld")
        ),
        BenchmarkCase(
            name="apostrophe",
            text="I don't want spicy food.",
            description="包含撇号",
            expected_chars=set("Idontwantspicyfood")
        ),
        BenchmarkCase(
            name="mixed_case",
            text="Hello World Test 123",
            description="大小写混合",
            expected_chars=set("HelloWorldTest123")
        ),
        BenchmarkCase(
            name="numbers_only",
            text="1234567890",
            description="纯数字",
            expected_chars=set("1234567890")
        ),
        BenchmarkCase(
            name="letters_numbers",
            text="A1B2C3D4E5",
            description="字母数字交替",
            expected_chars=set("A1B2C3D4E5")
        ),
        BenchmarkCase(
            name="repeated_chars",
            text="Aaa Bbb Ccc 111",
            description="重复字符",
            expected_chars=set("AaaBbbCcc111")
        ),
        BenchmarkCase(
            name="long_text",
            text="The quick brown fox jumps over the lazy dog 1234567890",
            description="长文本",
            expected_chars=set("Thequickbrownfoxjumpsoverthelazydog1234567890")
        ),
        BenchmarkCase(
            name="problematic_chars",
            text="I1 l| O0 5S 6G",
            description="易混淆字符",
            expected_chars=set("I1l|O05S6G")
        ),
        BenchmarkCase(
            name="empty_test",
            text="",
            description="空文本",
            expected_chars=set()
        )
    ]

    return test_cases


def create_test_image(text: str, width=300, height=50) -> np.ndarray:
    """创建测试图像"""
    # 白色背景
    image = np.ones((height, width, 3), dtype=np.uint8) * 255

    # 字体设置
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.7
    thickness = 2

    # 获取文本大小
    (text_width, text_height), baseline = cv2.getTextSize(text, font, font_scale, thickness)

    # 计算居中位置
    x = (width - text_width) // 2
    y = (height + text_height) // 2

    # 绘制文本
    cv2.putText(image, text, (x, y), font, font_scale, 0, thickness)

    return image


def filter_alnum_only(text: str) -> str:
    """只保留字母和数字"""
    return ''.join(c for c in text if c.isalnum())


def run_single_benchmark(psm: int, test_case: BenchmarkCase) -> BenchmarkResult:
    """运行单次测试"""
    # 创建测试图像
    image = create_test_image(test_case.text, 300, 50)

    # 创建OCR引擎
    engine = OCREngine(psm_modes=[psm])

    # 测试处理时间
    start_time = time.time()
    raw_result = engine.recognize(image, preprocess=False)  # 不使用预处理
    final_result = filter_alnum_only(raw_result)
    processing_time = time.time() - start_time

    # 分析结果
    expected_chars = test_case.expected_chars
    final_chars = set(final_result)

    # 计算指标
    completeness = len(final_chars & expected_chars) / len(expected_chars) if expected_chars else 1.0

    missing_chars = expected_chars - final_chars
    extra_chars = final_chars - expected_chars

    # 找出不正确的字符（期望有但结果没有，或者结果中有但期望没有）
    incorrect_chars = set()
    # 期望有但结果没有的字符
    for char in missing_chars:
        if char in test_case.text:  # 确实应该有的字符
            incorrect_chars.add(f"missing:{char}")
    # 结果中有但期望没有的字符（额外字符）
    for char in extra_chars:
        incorrect_chars.add(f"extra:{char}")

    return BenchmarkResult(
        raw_ocr=raw_result,
        final_ocr=final_result,
        processing_time=processing_time,
        completeness=completeness,
        missing_chars=missing_chars,
        extra_chars=extra_chars,
        incorrect_chars=incorrect_chars
    )


def run_benchmark_for_psm(psm: int, test_cases: List[BenchmarkCase]) -> PSMStats:
    """为指定PSM运行完整测试"""
    print(f"\n--- Testing PSM {psm} ---")

    results = []

    for test_case in test_cases:
        print(f"  Testing: {test_case.name}")

        # 运行5次取平均
        times = []
        completions = []

        for _ in range(5):
            result = run_single_benchmark(psm, test_case)
            times.append(result.processing_time)
            completions.append(result.completeness)

        # 使用中位数结果
        median_time = statistics.median(times)
        median_completeness = statistics.median(completions)

        print(f"    Median time: {median_time:.4f}s")
        print(f"    Median completeness: {median_completeness:.2%}")

        results.append(result)

    # 计算统计信息
    completeness_values = [r.completeness for r in results]
    time_values = [r.processing_time for r in results]

    # 找出错误案例
    error_cases = []
    for i, result in enumerate(results):
        if result.completeness < 0.9:  # 完整度低于90%
            error_cases.append(test_cases[i].name)

    return PSMStats(
        psm=psm,
        total_tests=len(results),
        avg_completeness=statistics.mean(completeness_values),
        avg_time=statistics.mean(time_values),
        min_completeness=min(completeness_values),
        max_completeness=max(completeness_values),
        completeness_std=statistics.stdev(completeness_values) if len(completeness_values) > 1 else 0,
        error_cases=error_cases
    )


def run_comprehensive_benchmark():
    """运行全面基准测试"""
    print("=== OCR Benchmark Phase 4.2 ===")
    print("Benchmarking PSM 6/7/11 configurations")
    print("Rules: Only [A-Za-z0-9], no preprocessing, no character replacements\n")

    # 创建测试用例
    test_cases = create_test_images()

    # 测试每个PSM
    psm_stats = []
    for psm in [6, 7, 11]:
        stats = run_benchmark_for_psm(psm, test_cases)
        psm_stats.append(stats)

    # 输出汇总报告
    print("\n" + "="*50)
    print("BENCHMARK RESULTS SUMMARY")
    print("="*50)

    best_psm = max(psm_stats, key=lambda s: s.avg_completeness)
    fastest_psm = min(psm_stats, key=lambda s: s.avg_time)

    for stats in psm_stats:
        print(f"\nPSM {stats.psm}:")
        print(f"  Average Completeness: {stats.avg_completeness:.2%}")
        print(f"  Average Time: {stats.avg_time:.4f}s")
        print(f"  Min/Max Completeness: {stats.min_completeness:.2%} - {stats.max_completeness:.2%}")
        print(f"  Completeness Std Dev: {stats.completeness_std:.2%}")
        print(f"  Error Cases (completeness < 90%): {stats.error_cases}")

        if stats == best_psm:
            print(f"  [BEST] PERFORMANCE")
        if stats == fastest_psm:
            print(f"  [FASTEST]")

    # 输出详细案例对比
    print("\n" + "-"*50)
    print("DETAILED CASE COMPARISON")
    print("-"*50)

    print(f"{'Case':<20} {'PSM6':<8} {'PSM7':<8} {'PSM11':<8}")
    print("-"*50)

    for i, test_case in enumerate(test_cases):
        p6_completeness = psm_stats[0].completeness_values[i] if i < len(psm_stats[0].completeness_values) else 0
        p7_completeness = psm_stats[1].completeness_values[i] if i < len(psm_stats[1].completeness_values) else 0
        p11_completeness = psm_stats[2].completeness_values[i] if i < len(psm_stats[2].completeness_values) else 0

        print(f"{test_case.name:<20} {p6_completeness:.2%} {p7_completeness:.2%} {p11_completeness:.2%}")

    return best_psm, psm_stats


if __name__ == "__main__":
    best_psm, all_stats = run_comprehensive_benchmark()

    print("\n" + "="*50)
    print("BENCHMARK COMPLETE")
    print("="*50)
    print(f"Best PSM: {best_psm.psm}")
    print(f"Best Completeness: {best_psm.avg_completeness:.2%}")
    print(f"Best Time: {best_psm.avg_time:.4f}s")
    print(f"Error Cases: {best_psm.error_cases}")