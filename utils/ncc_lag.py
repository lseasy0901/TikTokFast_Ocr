# -*- coding: utf-8 -*-
"""时间戳帧序列的内容滞后估计（H7-A2 冻结算法）。

约定（合成验证冻结，不得更改）：
  - 输入：两个"到达时间戳帧序列" (t, frame)；
  - lag = 残差网格上 argmax NCC 的帧数 × 网格步长；
  - lag > 0 表示 B 序列比 A 序列**晚**显示同一内容（B 更慢）；
  - 返回 lag_s（秒）、峰值、次峰、peak_margin、有效匹配率。

方法：共同墙钟网格重采样（searchsorted 取最近帧）→ 逐帧去均值归一化
→ 全 lag 范围 NCC。质量门禁：peak_margin = 峰值 − 次大旁瓣（lag ≥ ±0.3s 外），
margin 不足或匹配率低 → 质量标记为低，调用方不得据此下结论。
"""
import numpy as np

__all__ = ["estimate_lag_s"]


def estimate_lag_s(seq_a, seq_b, max_lag_s=3.0, grid_fps=60.0,
                   static_frame_std_min=3.0):
    """估计 B 相对 A 的内容滞后（秒，正 = B 更慢）。

    :param seq_a: [(t, frame_bytes), ...] t 为 perf_counter 秒（各自时钟域须一致）
    :param seq_b: 同上
    :param max_lag_s: 搜索范围（±）
    :param grid_fps: 重采样网格帧率
    :param static_frame_std_min: 帧内 std 低于此值视为静止帧，剔除后样本不足则失败
    :return: dict(lag_s, peak, sidelobe, margin, n_pairs, quality, error)
             quality ∈ {"ok", "low_margin", "insufficient", "error"}
    """
    try:
        if len(seq_a) < 30 or len(seq_b) < 30:
            return {"quality": "insufficient", "error": "序列过短"}
        ta = np.array([t for t, _ in seq_a], dtype=np.float64)
        tb = np.array([t for t, _ in seq_b], dtype=np.float64)
        fa = np.frombuffer(b"".join(f for _, f in seq_a), dtype=np.uint8)
        fb = np.frombuffer(b"".join(f for _, f in seq_b), dtype=np.uint8)
        ha, wa = _frame_shape(len(seq_a), len(fa) // len(seq_a))
        hb, wb = _frame_shape(len(seq_b), len(fb) // len(seq_b))
        Fa = fa.reshape(len(seq_a), ha, wa).astype(np.float32)
        Fb = fb.reshape(len(seq_b), hb, wb).astype(np.float32)
        if (ha, wa) != (hb, wb):
            Fb = _resize_stack(Fb, (ha, wa))

        t0 = max(ta[0], tb[0])
        t1 = min(ta[-1], tb[-1])
        if t1 - t0 < 5.0:
            return {"quality": "insufficient", "error": "重叠窗 <5s"}
        grid = np.arange(t0, t1, 1.0 / grid_fps)

        def resample(ts, F):
            idx = np.clip(np.searchsorted(ts, grid), 0, len(F) - 1)
            return F[idx].reshape(len(grid), -1)

        A = resample(ta, Fa)
        B = resample(tb, Fb)
        # 静止帧剔除（双方其一静止的网格点不参与）
        sa = A.std(axis=1)
        sb = B.std(axis=1)
        keep = (sa >= static_frame_std_min) & (sb >= static_frame_std_min)
        if keep.sum() < grid_fps * 5:
            return {"quality": "insufficient", "error": "有效动态帧不足"}
        A, B = A[keep], B[keep]
        A = A - A.mean(axis=1, keepdims=True)
        B = B - B.mean(axis=1, keepdims=True)
        An = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
        Bn = B / (np.linalg.norm(B, axis=1, keepdims=True) + 1e-9)

        max_lag = int(max_lag_s * grid_fps)
        best_lag, best_v = None, -2.0
        curve = []
        for lag in range(-max_lag, max_lag + 1):
            a0 = max(0, lag)
            a1 = min(len(An), len(Bn) + lag)
            n = a1 - a0
            if n < grid_fps * 3:
                curve.append((lag, np.nan))
                continue
            # 逐帧像素点积(=帧级 NCC, 单位向量) → 对帧取均值 ∈ [-1,1]。
            # 历史教训（H7-A0 审计）: .mean() 按像素平均会缩小 N_pixels 倍;
            # .sum() 按像素+时间双轴求和会放大 N_pairs 倍。两者都已实测踩坑。
            prod = An[a0:a1] * Bn[a0 - lag:a1 - lag]
            v = float(prod.sum(axis=1).mean())
            curve.append((lag, v))
            if v > best_v:
                best_v, best_lag = v, lag
        if best_lag is None:
            return {"quality": "insufficient", "error": "无有效 lag"}

        # 次大旁瓣：|lag| ≥ 0.3s 之外的最大值
        side = [v for l, v in curve
                if not np.isnan(v) and abs(l - best_lag) >= 0.3 * grid_fps]
        sidelobe = max(side) if side else 0.0
        margin = best_v - sidelobe
        lag_s = best_lag / grid_fps
        quality = "ok" if margin > 0.02 else "low_margin"
        return {"lag_s": round(lag_s, 4), "peak": round(best_v, 5),
                "sidelobe": round(sidelobe, 5), "margin": round(margin, 5),
                "n_pairs": int(keep.sum()), "quality": quality,
                "convention": "lag>0 = B 比 A 晚显示同一内容"}
    except Exception as e:  # noqa: BLE001
        return {"quality": "error", "error": str(e)[:200]}


def _frame_shape(n, size):
    """推断 (h, w)；仅支持几种诊断用小尺寸，避免随意假设。"""
    for h, w in ((90, 160), (45, 80), (270, 480), (180, 320), (96, 80)):
        if h * w == size:
            return h, w
    s = int(size ** 0.5)
    return size // s, s


def _resize_stack(F, shape):
    import cv2
    return np.stack([cv2.resize(f, (shape[1], shape[0])) for f in F])
