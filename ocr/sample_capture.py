# -*- coding: utf-8 -*-
"""
真实 ROI 样本采集（默认关闭）—— 给离线准确率基准提供输入

采到的就是**识别器真正看到的那一帧 ROI 像素**（worker 里 `_crop_roi()` 的输出，
OCR 之前、预处理之前），因此离线基准拿它重跑引擎不会与线上有任何差异。

只采集，不参与任何判定：
    - 不改识别、纠正、PSM 仲裁、时序确认与剪贴板；
    - 默认关闭。关闭时 `capture()` 只做一次布尔判断就返回 None；
    - 采满了就停（见上限），不会无限增长、不会拖慢实时识别；
    - 写盘失败只记日志并自动关闭采集，绝不把异常抛进识别循环。

上限与代价：
    - `max_samples`（默认 400）与 `max_bytes`（默认 64MB）双重封顶，
      任一触顶就停止采集并开始计数 `skipped`；
    - 每帧采集的代价 = 一次 PNG 编码落盘 + 一次索引行追加。按 640x120 的
      ROI 实测约 5~15ms，而识别间隔是 300ms —— 只在**未达上限**期间存在，
      且可用 max_samples 精确控制总量。

磁盘布局：
    <dir>/samples.csv      索引（列见 INDEX_HEADER），首次采集时写表头
    <dir>/images/000001.png 每个样本一张 PNG，文件名与索引的「图像文件」列一致

标注：
    本模块只负责采图与索引，**不猜标准答案**。人工标注另存为一份
    `index,标准房间码` 的 CSV（见 bench_phase_ocr7_offline_accuracy.py 的
    --labels）。没标注的样本在基准里记为「未标注」，既不算对也不算错。
"""

import csv
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.sample_capture")

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_SAMPLES",
    "INDEX_HEADER",
    "SampleCapture",
    "SampleRecord",
    "format_capture_time",
]

#: 索引 CSV 的列序（落盘与离线基准共用同一份定义）
INDEX_HEADER: Sequence[str] = (
    "序号", "时间", "游戏档", "图像文件", "宽", "高",
    "PSM读法", "候选", "备注",
)

#: 写 CSV 用 UTF-8 + BOM：Windows Excel 认中文表头。
CSV_ENCODING = "utf-8-sig"
CSV_LINE_TERMINATOR = "\r\n"

#: 默认上限。400 帧 ≈ 300ms 间隔下约 2 分钟，正好覆盖一轮 100 码测试
#: （每个码展示约 1s -> 100 秒 -> 约 330 帧）；按 640x120 的 PNG 约 30KB 算，
#: 400 张不到 15MB。
DEFAULT_MAX_SAMPLES = 400
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: 读法列的分隔符，与诊断 CSV 保持一致，便于两边对照。
_SEPARATOR = " ‖ "


def format_capture_time(moment: datetime) -> str:
    """采集时刻 ``HH:MM:SS.mmm``（与诊断 / 复制记录 CSV 写法一致）。"""
    return f"{moment.strftime('%H:%M:%S')}.{moment.microsecond // 1000:03d}"


@dataclass(frozen=True)
class SampleRecord:
    """一个已落盘的样本。"""

    index: int
    captured_at: datetime
    profile_id: str
    image_name: str
    width: int
    height: int
    readings_text: str
    candidate: str
    note: str
    image_bytes: int

    @property
    def time_text(self) -> str:
        return format_capture_time(self.captured_at)

    def row(self) -> tuple:
        """索引 CSV 一行（列序与 :data:`INDEX_HEADER` 一致）。"""
        return (self.index, self.time_text, self.profile_id, self.image_name,
                self.width, self.height, self.readings_text,
                self.candidate, self.note)

    @property
    def path(self) -> str:
        return self.image_name


class SampleCapture:
    """ROI 样本采集器：默认关闭、有上限、失败不影响识别。"""

    def __init__(self,
                 directory: str,
                 enabled: bool = False,
                 max_samples: int = DEFAULT_MAX_SAMPLES,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self._dir = str(directory)
        self._enabled = bool(enabled)
        self._max_samples = int(max_samples)
        self._max_bytes = int(max_bytes)
        self._lock = threading.RLock()
        self._records: List[SampleRecord] = []
        self._bytes = 0
        self._skipped = 0
        self._failed = False
        self._io_error_logged = False

    # ------------------------------------------------------------------
    # 开关 / 只读视图
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def directory(self) -> str:
        return self._dir

    @property
    def max_samples(self) -> int:
        return self._max_samples

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def bytes_written(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def skipped(self) -> int:
        """因为触顶（或写盘失败）而没采的帧数 —— 如实计数，不静默。"""
        with self._lock:
            return self._skipped

    @property
    def failed(self) -> bool:
        """是否因为写盘失败而自动关闭。"""
        with self._lock:
            return self._failed

    @property
    def records(self) -> Tuple[SampleRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def set_enabled(self, enabled: bool) -> None:
        """开关采集。开启不清空已采样本，关闭也不删已落盘的文件。"""
        self._enabled = bool(enabled)
        logger.info("ROI 样本采集已%s（目录 %s）",
                    "开启" if self._enabled else "关闭", self._dir)

    def clear_records(self) -> None:
        """只清内存里的索引（不动磁盘文件）。测试与「换一批」用。"""
        with self._lock:
            self._records.clear()
            self._bytes = 0
            self._skipped = 0

    # ------------------------------------------------------------------
    # 采集（识别线程调用）
    # ------------------------------------------------------------------
    def capture(self,
                image: np.ndarray,
                profile_id: str = "",
                readings: Optional[Sequence[Tuple[Optional[int], str]]] = None,
                candidate: Optional[str] = None,
                note: str = "",
                now: Optional[datetime] = None) -> Optional[SampleRecord]:
        """采集一帧 ROI。关闭时只做一次布尔判断。

        任何异常都在内部消化：写盘失败会记一次日志并自动关闭采集，
        绝不让采集把识别循环搞崩（采集是旁路，识别是主路）。

        参数：
            image: ROI 像素（BGR，与识别器看到的一致）
            readings: 该帧各 PSM 的原始读法 [(psm, 文本), ...]，可为 None
            candidate: 该帧最终候选房间码（None = 无候选）
            note: 附加说明（如"游戏冲突"）
        """
        if not self._enabled or self._failed:
            return None
        if image is None or getattr(image, "size", 0) == 0:
            return None

        with self._lock:
            if len(self._records) >= self._max_samples or self._bytes >= self._max_bytes:
                self._skipped += 1
                return None
            index = len(self._records) + 1

        try:
            image_name, written = self._write_image(index, image)
        except Exception as exc:                       # noqa: BLE001 - 旁路不许外抛
            with self._lock:
                self._failed = True
                self._skipped += 1
            if not self._io_error_logged:
                self._io_error_logged = True
                logger.warning("ROI 样本采集写盘失败，已自动关闭采集: %s", exc)
            return None

        height, width = image.shape[:2]
        record = SampleRecord(
            index=index,
            captured_at=now if now is not None else datetime.now(),
            profile_id=profile_id or "",
            image_name=image_name,
            width=int(width),
            height=int(height),
            readings_text=self._render_readings(readings),
            candidate=(candidate or ""),
            note=note or "",
            image_bytes=written,
        )

        with self._lock:
            self._records.append(record)
            self._bytes += written
            first = len(self._records) == 1
        self._append_index(record, header=first)
        return record

    # ------------------------------------------------------------------
    # 内部：落盘
    # ------------------------------------------------------------------
    def _write_image(self, index: int, image: np.ndarray) -> Tuple[str, int]:
        image_dir = os.path.join(self._dir, "images")
        os.makedirs(image_dir, exist_ok=True)
        name = os.path.join("images", f"{index:06d}.png")
        path = os.path.join(self._dir, name)
        # cv2.imwrite 不接受中文路径，用 imencode + 自己写字节
        ok, buffer = cv2.imencode(".png", image)
        if not ok:
            raise OSError("PNG 编码失败")
        payload = buffer.tobytes()
        with open(path, "wb") as handle:
            handle.write(payload)
        return name.replace(os.sep, "/"), len(payload)

    def _append_index(self, record: SampleRecord, header: bool) -> None:
        """把一行追加到索引 CSV。表头只在第一个样本时写。"""
        path = os.path.join(self._dir, "samples.csv")
        os.makedirs(self._dir, exist_ok=True)
        # newline="" 由我们自己控制行尾，否则 Windows 上会把 \r\n 再翻一倍。
        with open(path, "a", encoding=CSV_ENCODING, newline="") as handle:
            writer = csv.writer(handle, lineterminator=CSV_LINE_TERMINATOR)
            if header:
                writer.writerow(INDEX_HEADER)
            writer.writerow(record.row())

    @staticmethod
    def _render_readings(
        readings: Optional[Sequence[Tuple[Optional[int], str]]]
    ) -> str:
        if not readings:
            return ""
        parts = []
        for psm, text in readings:
            label = "-" if psm is None else str(psm)
            parts.append(f"{label}:{'' if text is None else text}")
        return _SEPARATOR.join(parts)

    # ------------------------------------------------------------------
    # 摘要（GUI 状态栏用）
    # ------------------------------------------------------------------
    def summary(self) -> str:
        with self._lock:
            count, written, skipped = len(self._records), self._bytes, self._skipped
        message = (f"已采集 {count} 个 ROI 样本（{written / 1024 / 1024:.1f} MB）")
        if skipped:
            message += f"，触顶未采 {skipped} 帧"
        if self._failed:
            message += "，写盘失败已停止"
        return message
