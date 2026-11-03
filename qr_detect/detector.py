# -*- coding: utf-8 -*-
"""QR 检测器 —— 从「闪-live_player」(DouyinQR, live-player.exe) 逆向还原的完整管线。

原程序流程（函数地址注释标出处），本模块按相同顺序复刻：

  detect(config, rgb, w, h, stride, result)          [0x140032090]
    ├─ 灰度转换 (150R+77G+29B)>>8                    [0x140031ec0]
    │    （注：系数与 BT.601 相比 R/G 对调，属原版笔误；
    │      本实现用标准 BT.601，zbar 二值化后无实际差异）
    ├─ 全局对比度拉伸 round((v-min)*255/(max-min))    [0x140031650]
    ├─ 多尺度阶梯重试（按画面最大边 M）：
    │     M>1280 → 试 1280
    │     M>1600 → 试 1600
    │     M>960  → 试 960
    │     M>640  → 试 640
    │     M>1440 → 试 1920
    │     最后   → 原始尺寸
    │    （每个档位内部 [0x1400318c0] 都做：拉伸 → 密度(3,3)扫 →
    │      密度(5,5)重扫 → 反色+密度(4,4)再扫；坐标按缩放比映射回原始帧）
    └─ zbar 只开 QRCODE                              [0x140013590]
         set_config(NONE, ENABLE, 0) + set_config(QRCODE, ENABLE, 1)
         set_format('Y800')

结果：内容文本 + 原始帧坐标系的包围盒/多边形（供视频画面高亮）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError as e:  # pragma: no cover
    raise ImportError("qr_detect.detector 需要 opencv-python") from e

from .zbar_backend import ZbarScanner

# 原版密度三段（[0x140032090] 原始尺寸路径实测值）
_PASS_DENSITIES = (
    (3, 3),   # 第一遍
    (5, 5),   # 第二遍（同图更稀疏）
    (4, 4),   # 第三遍（反色图）
)


@dataclass
class QRDetection:
    """一次成功的检测。坐标全部是**输入帧**的像素坐标。"""
    content: bytes                                   # 解码原始字节
    bbox: Tuple[int, int, int, int]                  # (x0, y0, x1, y1) 包围盒
    polygon: List[Tuple[int, int]] = field(default_factory=list)  # 定位多边形
    source_size: Tuple[int, int] = (0, 0)            # (w, h) 输入帧
    scan_scale: float = 1.0                          # 命中档位的缩放（信息用）

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("utf-8", "replace")


def _contrast_stretch(gray: np.ndarray) -> np.ndarray:
    """全局 min-max 拉伸 [0x140031650]：out = ((v-min)*255 + (range-1)//2) // range。"""
    lo = int(gray.min())
    hi = int(gray.max())
    if hi <= lo:
        return gray
    rng = hi - lo
    half = (rng - 1) // 2
    out = ((gray.astype(np.int32) - lo) * 255 + half) // rng
    return np.clip(out, 0, 255).astype(np.uint8)


def _scale_ladder(max_dim: int) -> List[int]:
    """复刻原版的档位顺序 [0x140032090]。返回目标最大边列表，最后 0 表示原始尺寸。

    例：1920 输入 → [1280, 1600, 960, 640, 1920, 0(原始)]
        1000 输入 → [960, 640, 0]
    """
    ladder: List[int] = []
    if max_dim > 1280:
        ladder.append(1280)
    if max_dim > 1600:
        ladder.append(1600)
    if max_dim > 960:
        ladder.append(960)
    if max_dim > 640:
        ladder.append(640)
    if max_dim > 1440:
        ladder.append(1920)
    ladder.append(0)
    return ladder


def _resize_maxdim(gray: np.ndarray, target_max: int) -> np.ndarray:
    """等比缩放到最大边 = target_max（原版 new=(orig*t-1+M)/M 的进位除法近似）。"""
    h, w = gray.shape[:2]
    m = max(h, w)
    if m == target_max:
        return gray
    scale = target_max / m
    nw = max(int(round(w * scale)), 1)
    nh = max(int(round(h * scale)), 1)
    interp = cv2.INTER_AREA if target_max < m else cv2.INTER_CUBIC
    return cv2.resize(gray, (nw, nh), interpolation=interp)


class QrDetector:
    """有状态检测器（复用 zbar scanner，线程不安全，一个线程一个实例）。"""

    def __init__(self) -> None:
        self._scanner = ZbarScanner()
        self._last_gray_size: Tuple[int, int] = (0, 0)

    # ------------------------------------------------------------------
    def detect(self, frame_bgr: np.ndarray) -> Optional[QRDetection]:
        """检测一帧 BGR 图像（LiveLens 的帧缓冲即 BGR24 ndarray）。"""
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        h, w = frame_bgr.shape[:2]
        if w <= 0 or h <= 0:
            return None

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = _contrast_stretch(gray)

        seen_sizes = set()
        for target in _scale_ladder(max(h, w)):
            scaled = gray if target == 0 else _resize_maxdim(gray, target)
            sh, sw = scaled.shape[:2]
            if (sw, sh) in seen_sizes and target != 0:
                # 原版在 1920 输入时会扫两次同尺寸（1920 档 + 原始档），此处去重提速
                continue
            seen_sizes.add((sw, sh))

            hit = self._scan_with_retries(scaled)
            if hit is not None:
                symbol, scale_pass = hit
                det = self._to_detection(symbol, sw, sh, w, h)
                det.scan_scale = scale_pass
                return det
        return None

    # ------------------------------------------------------------------
    def _scan_with_retries(self, scaled_gray: np.ndarray):
        """一个档位内的三段扫描：原样(3,3) → 原样(5,5) → 反色(4,4)。"""
        scanner = self._scanner
        images = [scaled_gray, scaled_gray, cv2.bitwise_not(scaled_gray)]
        for (dx, dy), img in zip(_PASS_DENSITIES, images):
            scanner.set_density(dx, dy)
            symbols = scanner.scan(img, img.shape[1], img.shape[0])
            if symbols:
                # 原版取第一个非空 QRCODE 符号
                return symbols[0], (dx, dy)
        return None

    @staticmethod
    def _to_detection(symbol, sw: int, sh: int, ow: int, oh: int) -> QRDetection:
        """把扫描坐标系的符号映射回原始帧坐标（原版 [0x140031504] 尾部）。"""
        sx = ow / sw
        sy = oh / sh

        def pt(x, y):
            return int(round(x * sx)), int(round(y * sy))

        polygon = [pt(x, y) for (x, y) in symbol.polygon]
        if polygon:
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            bbox = (min(xs), min(ys), max(xs), max(ys))
        else:
            # 原版允许无定位点的符号（仅内容）
            bbox = (0, 0, ow, oh)
        return QRDetection(
            content=symbol.data,
            bbox=bbox,
            polygon=polygon,
            source_size=(ow, oh),
        )

    def close(self) -> None:
        if self._scanner:
            self._scanner.close()
            self._scanner = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
