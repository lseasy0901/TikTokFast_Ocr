# -*- coding: utf-8 -*-
"""A2 合成序列验证：NCC 滞后估计的符号、单位与边界条件（冻结规则测试）。

用已知偏移的合成序列验证算法，不依赖真实直播间：
  - 正 lag 语义（B 比 A 晚）；
  - 已知偏移的恢复精度（±1 网格帧）；
  - 峰值/旁瓣质量门禁；
  - 静止内容 → insufficient；
  - 序列过短 → insufficient；
  - 分辨率不一致时自动缩放。
"""
import numpy as np
import pytest

from utils.ncc_lag import estimate_lag_s

FPS = 60.0
W, H = 80, 45


def _make_seq(rng, n, lag_s, base_wander=8.0):
    """生成一对序列：A 从 0 开始；B 显示与 A 相同内容但晚 lag_s 秒。

    内容用"滚动的亮块 + 随机纹理"保证时间可辨识且非静止。
    """
    total = n + abs(int(lag_s * FPS)) + 10
    frames = np.zeros((total, H, W), dtype=np.uint8)
    rng2 = np.random.default_rng(12345)
    noise = rng2.integers(0, 12, size=(total, H, W)).astype(np.int16)
    # 两个非公比速度的亮块 → 联合模式远超搜索窗，内容非周期
    p1 = np.cumsum(rng2.integers(1, 4, size=total)) % W        # 随机游走
    p2 = (np.arange(total) * 7) % W
    for i in range(total):
        frames[i, :, p1[i]:p1[i] + 6] += 90
        frames[i, 10:35, p2[i]:p2[i] + 5] += 70
    frames = np.clip(frames + noise, 0, 255).astype(np.uint8)

    t0 = 100.0                       # 公共时钟起点（任意）
    ta = t0 + np.arange(n) / FPS
    seq_a = [(float(t), frames[i].tobytes()) for i, t in enumerate(ta)]
    # B：显示内容比 A 晚 lag_s → B 的第 i 帧取内容帧 i + lag_s*FPS
    off = int(round(lag_s * FPS))
    tb = t0 + np.arange(n) / FPS
    seq_b = [(float(t), frames[i + off].tobytes())
             for i, t in enumerate(tb)]
    return seq_a, seq_b


def test_positive_lag_recovered():
    seq_a, seq_b = _make_seq(np.random.default_rng(1), 600, 1.0)
    r = estimate_lag_s(seq_a, seq_b, max_lag_s=3.0)
    assert r["quality"] == "ok"
    assert abs(r["lag_s"] - 1.0) <= 1.5 / FPS


def test_negative_lag_recovered():
    seq_a, seq_b = _make_seq(np.random.default_rng(2), 600, -0.5)
    r = estimate_lag_s(seq_a, seq_b, max_lag_s=3.0)
    assert r["quality"] == "ok"
    assert abs(r["lag_s"] - (-0.5)) <= 1.5 / FPS


def test_zero_lag():
    seq_a, seq_b = _make_seq(np.random.default_rng(3), 600, 0.0)
    r = estimate_lag_s(seq_a, seq_b, max_lag_s=2.0)
    assert r["quality"] == "ok"
    assert abs(r["lag_s"]) <= 1.5 / FPS


def test_margin_rejects_ambiguous():
    # 静止内容（无亮块滚动）→ 应判 insufficient 或 low_margin，不得给出高置信结论
    n = 600
    rng = np.random.default_rng(4)
    base = rng.integers(0, 255, size=(1, H, W)).astype(np.uint8)
    frames = np.repeat(base, n + 10, axis=0)
    t0 = 100.0
    ta = t0 + np.arange(n) / FPS
    seq_a = [(float(t), frames[i].tobytes()) for i, t in enumerate(ta)]
    seq_b = [(float(t + 0.4), frames[i + 5].tobytes()) for i, t in enumerate(ta)]
    r = estimate_lag_s(seq_a, seq_b, max_lag_s=2.0)
    assert r["quality"] in ("insufficient", "low_margin", "error")


def test_short_sequences_rejected():
    seq_a, seq_b = _make_seq(np.random.default_rng(5), 10, 1.0)
    r = estimate_lag_s(seq_a, seq_b)
    assert r["quality"] == "insufficient"


def test_resolution_mismatch_auto_resized():
    seq_a, seq_b = _make_seq(np.random.default_rng(6), 600, 0.8)
    # 把 B 的帧转成不同分辨率（模拟缩放），算法应自动重采样
    import cv2
    seq_b2 = [(t, cv2.resize(np.frombuffer(f, dtype=np.uint8).reshape(H, W),
                             (160, 90)).tobytes()) for t, f in seq_b]
    r = estimate_lag_s(seq_a, seq_b2, max_lag_s=3.0)
    assert r["quality"] == "ok"
    assert abs(r["lag_s"] - 0.8) <= 2.5 / FPS
