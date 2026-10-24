# -*- coding: utf-8 -*-
"""日志与崩溃捕获(2026-10-25 补齐,此前本文件一直是空占位)。

背景:此前日志只 basicConfig 到 stdout——提权重定向时 GBK 乱码、无落盘
(用户侧排障无据可查)、无轮转(理论上无限增长)、未捕获异常直接吞掉。
本模块一次补齐:

    setup_logging()        UTF-8 控制台 + RotatingFileHandler(2MB×5)落盘
    install_crash_hooks()  sys/threading excepthook → 崩溃文件 + 日志

落盘位置:%LOCALAPPDATA%/LiveLens/logs/livelens.log(环境变量 DLV_LOG_DIR
可覆盖,测试用)。崩溃文件:logs/crash-<时间戳>.log(独立命名,方便用户
一键发来排障)。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path

__all__ = ["setup_logging", "install_crash_hooks", "log_dir"]

_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5
_ROOT_LOGGER = "DouyinLowLatencyViewer"


def _base_dir() -> Path:
    override = os.environ.get("DLV_LOG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "LiveLens"


def log_dir() -> Path:
    return _base_dir() / "logs"


def setup_logging(level: int = logging.DEBUG) -> Path:
    """初始化根日志:控制台(UTF-8)+ 滚动文件(UTF-8)。

    必须在 QApplication 之前调用(和原 basicConfig 同位)。幂等:重复调用
    只调整级别,不重复挂 handler。
    :return: 日志文件路径(排障时让用户找这个文件)。
    """
    # 控制台乱码根因:Windows 下 stderr 默认 GBK,重定向到文件时中文
    # 全部变 mojibake(提权运行排障时踩过)。统一 UTF-8。
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 重配失败维持现状
                pass

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "livelens.log"
        root = logging.getLogger(_ROOT_LOGGER)
        if not root.handlers:
            fmt = logging.Formatter(
                "[%(asctime)s %(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S")
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(console)
            root.addHandler(file_handler)
            root.setLevel(level)
        else:
            root.setLevel(level)
        return log_path
    except OSError:
        # 磁盘/权限落不了盘:控制台日志仍可用,绝不能因日志崩掉主程序
        return Path(tempfile.gettempdir()) / "livelens.log"


def _write_crash(kind: str, exc_type, value, tb) -> None:
    """崩溃快照独立落盘 + 日志 critical。钩子里绝不抛出(抛=雪崩)。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = log_dir() / f"crash-{stamp}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"LiveLens crash report  {stamp}\n")
            fh.write(f"kind: {kind}\n\n")
            traceback.print_exception(exc_type, value, tb, file=fh)
        logging.getLogger(_ROOT_LOGGER).critical(
            "未捕获异常(%s),崩溃报告已写入 %s", kind, path,
            exc_info=(exc_type, value, tb))
    except Exception:  # noqa: BLE001
        pass


def install_crash_hooks() -> None:
    """安装全局异常钩子:主线程 + 子线程。

    子线程异常默认只打印到 stderr 就消失,同样收进崩溃报告——
    拉流/OCR 工作线程的死因从此有据可查。
    """
    def _sys_hook(exc_type, value, tb):
        _write_crash("main-thread", exc_type, value, tb)
        sys.__excepthook__(exc_type, value, tb)   # 保持默认行为不惊讶

    def _thread_hook(args):
        _write_crash(f"thread:{args.thread.name if args.thread else '?'}",
                     args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
