# -*- coding: utf-8 -*-
"""
真实 ROI 采集（Phase 2: Dataset Collector）。

复用**项目已验证的实时帧链路**，不新建第二套抓取链：
    stream.registry.build_default_registry()  -> 平台适配器
    stream.resolver.StreamResolver.resolve(url) -> StreamInfo（含选中的 CDN 地址）
    stream.ffmpeg_reader.FFmpegReader(...)    -> read_frame() 返回 BGR uint8

ROI 裁剪语义与生产一致（见 ocr/worker.py:_crop_roi）：
    正常：frame[y:y+h, x:x+w]
    越界：若 ROI 带源尺寸则按 source->frame 比例缩放后再裁
ROI 数据结构在本模块内自带（ROIRect），**刻意不 import ocr.roi_manager** ——
`import ocr` 会连带执行 ocr/__init__.py（worker / PySide6 / 剪贴板），
给数据集工具引入一堆无关初始化。语义保持一致即可，不是复用同一个类。

写入图片用 cv2.imencode + ndarray.tofile，而不是 cv2.imwrite：
本机用户名带全角括号，cv2.imwrite 在非 ASCII 路径上会静默失败。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import cv2
import numpy as np

__all__ = [
    "ROIRect",
    "FrameSource",
    "LiveFrameSource",
    "ReplayFrameSource",
    "crop_roi",
    "parse_roi",
    "SampleWriter",
    "imwrite_unicode",
    "imread_unicode",
]


# ----------------------------------------------------------------------
# ROI（语义与 ocr/roi_manager.ROI 一致）
# ----------------------------------------------------------------------
@dataclass
class ROIRect:
    x: int
    y: int
    width: int
    height: int
    source_width: int = 0
    source_height: int = 0

    def scale_to(self, target_width: int, target_height: int) -> "ROIRect":
        if self.source_width <= 0 or self.source_height <= 0:
            return ROIRect(self.x, self.y, self.width, self.height,
                           target_width, target_height)
        sx, sy = target_width / self.source_width, target_height / self.source_height
        return ROIRect(int(self.x * sx), int(self.y * sy),
                       int(self.width * sx), int(self.height * sy),
                       target_width, target_height)

    def as_tuple(self) -> tuple:
        return (self.x, self.y, self.width, self.height)


def parse_roi(text: str) -> ROIRect:
    """解析 `x,y,w,h`（CLI 用）。"""
    parts = [p.strip() for p in str(text).replace("x", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("ROI 需要 4 个数字 x,y,w,h，收到: %r" % text)
    x, y, w, h = (int(float(p)) for p in parts)
    if w <= 0 or h <= 0:
        raise ValueError("ROI 宽高必须为正: %r" % text)
    return ROIRect(x, y, w, h)


def crop_roi(frame: np.ndarray, roi: ROIRect) -> np.ndarray:
    """按 ROI 裁出识别区域。越界处理与 ocr/worker.py:_crop_roi 一致。"""
    fh, fw = frame.shape[:2]
    if roi.x < 0 or roi.y < 0 or roi.x + roi.width > fw or roi.y + roi.height > fh:
        if roi.source_width > 0 and roi.source_height > 0:
            r = roi.scale_to(fw, fh)
            return frame[r.y:r.y + r.height, r.x:r.x + r.width]
        x, y = max(0, roi.x), max(0, roi.y)
        return frame[y:y + min(roi.height, fh - y), x:x + min(roi.width, fw - x)]
    return frame[roi.y:roi.y + roi.height, roi.x:roi.x + roi.width]


# ----------------------------------------------------------------------
# 图像读写（非 ASCII 路径安全）
# ----------------------------------------------------------------------
def imwrite_unicode(path: str, image: np.ndarray) -> int:
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", image)
    if not ok:
        raise OSError("PNG 编码失败: %s" % path)
    data = buf.tobytes()
    with open(path, "wb") as fh:
        fh.write(data)
    return len(data)


def imread_unicode(path: str, flags=cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------
# 帧源
# ----------------------------------------------------------------------
class FrameSource:
    """帧源协议：read() 返回 BGR uint8 (H,W,3) 或 None；close() 释放。"""

    name = "frame-source"

    def read(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    @property
    def size(self) -> tuple:
        return (0, 0)


class LiveFrameSource(FrameSource):
    """真实直播帧源（项目的 StreamResolver + FFmpegReader）。"""

    name = "live"

    def __init__(self, reader, info):
        self._reader = reader
        self._info = info

    @classmethod
    def open(cls, url: str, *, use_cuda: bool = False,
             first_frame_timeout_s: float = 60.0,
             width: Optional[int] = None, height: Optional[int] = None
             ) -> "LiveFrameSource":
        from stream.ffmpeg_reader import FFmpegReader
        from stream.registry import build_default_registry
        from stream.resolver import StreamResolver

        info = StreamResolver(build_default_registry()).resolve(url)
        reader = FFmpegReader(info.selected.url,
                              width=width or info.width,
                              height=height or info.height,
                              use_cuda=use_cuda)
        reader.start()
        src = cls(reader, info)
        deadline = time.time() + first_frame_timeout_s
        while time.time() < deadline:
            if src.read() is not None:
                return src
            time.sleep(0.05)
        src.close()
        raise TimeoutError("直播流 %.0fs 内没有拿到首帧" % first_frame_timeout_s)

    @property
    def info(self):
        return self._info

    def read(self) -> Optional[np.ndarray]:
        f = self._reader.read_frame()
        if f is None:
            return None
        return f if getattr(f, "size", 0) else None

    @property
    def size(self) -> tuple:
        return tuple(getattr(self._reader, "frame_shape", (0, 0))[:2])

    def close(self) -> None:
        try:
            self._reader.stop()
        except Exception:  # noqa: BLE001
            pass


class ReplayFrameSource(FrameSource):
    """按顺序重放一组图片（离线/回归用）。图片必须来自真实采集。"""

    name = "replay"

    def __init__(self, paths: Sequence[str]):
        self._paths: List[str] = list(paths)
        self._i = 0
        self._size = (0, 0)

    def read(self) -> Optional[np.ndarray]:
        while self._i < len(self._paths):
            img = imread_unicode(self._paths[self._i])
            self._i += 1
            if img is not None:
                self._size = img.shape[:2]
                return img
        return None

    @property
    def size(self) -> tuple:
        return self._size

    def close(self) -> None:
        return None


# ----------------------------------------------------------------------
# 样本写入
# ----------------------------------------------------------------------
class SampleWriter:
    """把 ROI 图写到 <dataset>/images/<sample_id>.png，返回相对路径。"""

    def __init__(self, dataset_dir: str):
        self.images_dir = os.path.join(dataset_dir, "images")
        os.makedirs(self.images_dir, exist_ok=True)
        self._n = 0
        self._bytes = 0

    @property
    def count(self) -> int:
        return self._n

    @property
    def bytes_written(self) -> int:
        return self._bytes

    def write(self, image: np.ndarray, prefix: str = "s") -> str:
        """写入一张 ROI，返回**相对数据集根**的路径（正斜杠）。"""
        self._n += 1
        name = "%s%06d.png" % (prefix, self._n)
        full = os.path.join(self.images_dir, name)
        self._bytes += imwrite_unicode(full, image)
        return "images/%s" % name
