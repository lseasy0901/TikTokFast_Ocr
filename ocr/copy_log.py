# -*- coding: utf-8 -*-
"""
房间码复制记录 / CSV 导出

只记录**一件事**：每一次真正写进系统剪贴板的房间码。

也就是说，进入本记录的门槛是「复制成功」这个事件本身，而不是别的什么：

    - 没有经过时序确认（TemporalResolver）的 OCR 读数不会到这里 ——
      OCRWorker 只在候选成立时才发 ``copy_requested``；
    - 确认了但剪贴板写入失败（``ClipboardManager.copy()`` 返回 False）
      也不会到这里 —— 调用点在成功分支里；
    - 房间码本身是否正确**不做二次判断**：本模块不筛选、不纠正、不校验，
      只如实记下确实被复制走的那串字符。

记录是**只追加**的：同一个房间码在不同时间再次成功复制，就是两条记录。
它刻意不参与任何去重 —— 现有的去重有两处，都保持原样、原语不在此处：
``ClipboardManager`` 过滤"与上一次相同"的连续重复，GUI 右栏历史
（MainWindow._add_session_code）把连续重复折叠成一行并只保留最近 50 行。
那两处都是**显示/节流**行为，会丢失事件；本记录是**事件账**，两者互补。

记录只活在本次进程的内存里：不落盘、不随会话持久化，进程退出即消失。
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Union

logger = logging.getLogger("DouyinLowLatencyViewer.ocr.copy_log")

__all__ = ["CSV_HEADER", "CopyRecord", "CopyLog", "format_copy_time"]

#: CSV 表头。列的含义分别是 序号 / 复制时间 / 房间码。
CSV_HEADER: Sequence[str] = ("序号", "复制时间", "房间码")

#: 写 CSV 用的编码：utf-8-sig = UTF-8 + BOM。
#: BOM 是给 Windows Excel 看的 —— 没有它，Excel 会按本地代码页解析，
#: 中文表头直接变乱码。
CSV_ENCODING = "utf-8-sig"

#: 行结束符。Excel 在 Windows 上对 LF 的兼容性不如 CRLF。
CSV_LINE_TERMINATOR = "\r\n"


def format_copy_time(moment: datetime) -> str:
    """把时刻格式化成 ``HH:MM:SS.mmm``（毫秒）。

    ``strftime("%f")`` 给的是微秒（6 位），这里只取前 3 位换成毫秒，
    凑够 3 位再拼接，避免 40ms 被写成 ".40"。
    """
    return f"{moment.strftime('%H:%M:%S')}.{moment.microsecond // 1000:03d}"


@dataclass(frozen=True)
class CopyRecord:
    """一次成功复制的记录：序号（从 1 开始）、复制时刻、最终复制的房间码。"""

    index: int
    copied_at: datetime
    code: str

    @property
    def time_text(self) -> str:
        """复制时间（``HH:MM:SS.mmm``）。"""
        return format_copy_time(self.copied_at)

    def as_row(self) -> tuple:
        """CSV 一行（与 :data:`CSV_HEADER` 列序一致）。"""
        return (self.index, self.time_text, self.code)


class CopyLog:
    """本次运行中成功复制的房间码流水（内存内、只追加）。"""

    def __init__(self) -> None:
        self._records: List[CopyRecord] = []

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record(self, code: str, now: Optional[datetime] = None) -> Optional[CopyRecord]:
        """追加一条记录，返回新记录；空内容不入账（返回 None）。

        调用方**必须**已经确认过剪贴板写入成功 —— 本方法不做、也无法做这件事，
        它只负责记账。``now`` 仅用于测试注入固定时刻；默认取当前本地时间。

        房间码按 ``strip()`` 后的形式记录，与 ``ClipboardManager`` 实际写进
        剪贴板的内容保持一致（它也是先 strip 再 setText）。
        """
        normalized = (code or "").strip()
        if not normalized:
            # 空文本不该走到这里（ClipboardManager 会先过滤掉），兜底不入账：
            # 一条没有房间码的记录没有意义，也不该占掉一个序号。
            logger.debug("[COPY-LOG] 忽略空房间码，不入账")
            return None

        record = CopyRecord(
            index=len(self._records) + 1,
            copied_at=now if now is not None else datetime.now(),
            code=normalized,
        )
        self._records.append(record)
        logger.info("[COPY-LOG] 记录第%d条: %s @ %s",
                    record.index, record.code, record.time_text)
        return record

    def clear(self) -> None:
        """清空记录（序号从 1 重新开始）。"""
        self._records.clear()

    # ------------------------------------------------------------------
    # 读取 / 导出
    # ------------------------------------------------------------------
    @property
    def records(self) -> tuple:
        """全部记录（按复制发生的先后顺序）。"""
        return tuple(self._records)

    @property
    def count(self) -> int:
        """记录条数。"""
        return len(self._records)

    def to_csv_text(self) -> str:
        """生成完整 CSV 文本（含表头）。落盘与预览走的是同一段代码。"""
        return _write_csv(self._records)

    def export_csv(self, path: Union[str, Path]) -> int:
        """把当前记录写成 CSV 文件，返回写入的记录条数。

        导出**不清空**记录，也不改动它：同一个进程里可以反复导出，
        每次得到的内容只取决于当时已有的记录。

        异常：
            OSError: 路径不可写（由调用方负责提示用户）。
        """
        text = self.to_csv_text()
        # newline="" 交给我们自己控制行结束符，否则 Windows 上会把 \r\n 再翻一倍。
        with open(path, "w", encoding=CSV_ENCODING, newline="") as handle:
            handle.write(text)
        logger.info("[COPY-LOG] 已导出 %d 条记录到 %s", len(self._records), path)
        return len(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records)


def _write_csv(records: Sequence[CopyRecord]) -> str:
    """把记录渲染成 CSV 文本（统一走 csv 模块，转义交给它）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=CSV_LINE_TERMINATOR)
    writer.writerow(CSV_HEADER)
    for record in records:
        writer.writerow(record.as_row())
    return buffer.getvalue()
