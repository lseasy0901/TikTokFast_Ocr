# -*- coding: utf-8 -*-
"""卡点诊断追踪(2026-10-25,用户指定机制):软件整体不可用时,报出代表
"卡在哪个阶段+为什么"的错误码,并**先于一切落盘**——包括软件卡死
(无异常无进展)的场景:状态文件里永远留着"最后进入的阶段"。

三层:
    1. 阶段域码表(docs/ERROR_CODES.md 是注册表,文档即源):
       SYS 启动 / LIC 授权 / NET 连接解析 / PLY 播放 / OCR 识别 /
       QR 二维码 / AJ 自动加入 / UPD 更新
       失败码 = 阶段 + 3 位编号(1xx 前置缺失,2xx 网络/外部,
       3xx 内部错误,9xx 等待超时/卡死)
    2. 状态落盘:%LOCALAPPDATA%/LiveLens/logs/state.json(原子写)——
       每次进入关键阶段即写,崩溃/卡死后该文件就是"卡点"证据。
    3. 透出:fail() 返回结构化对象给调用方弹 toast/弹窗;意见反馈页
       「复制诊断信息」一键打包(错误码+日志尾)。

约束:追踪器自身绝不抛异常、绝不阻塞(写失败静默)——诊断不能成为
新的故障点。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("DouyinLowLatencyViewer.stage")

__all__ = ["StageFailure", "StageTracker", "stage_tracker", "read_last_state"]

_STATE_NAME = "state.json"


def _state_path() -> str:
    override = os.environ.get("DLV_LOG_DIR")
    if override:
        return os.path.join(override, _STATE_NAME)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
            os.path.join("~", ".local", "share"))
    return os.path.join(base, "LiveLens", "logs", _STATE_NAME)


@dataclass
class StageFailure:
    """一次"整体功能不可用"的失败(给 UI 层弹 toast/弹窗)。"""

    code: str          # 如 "NET-302"
    detail: str        # 用户可读的一句话
    hint: str = ""     # 可选:用户可执行的建议

    def user_text(self) -> str:
        text = f"错误码 {self.code}:{self.detail}"
        if self.hint:
            text += f"(建议:{self.hint})"
        return text


@dataclass
class _State:
    stage: str = ""
    code: str = ""
    detail: str = ""
    since: float = field(default_factory=time.time)


class StageTracker:
    """进程级卡点追踪(单例 stage_tracker)。线程安全。"""

    def __init__(self) -> None:
        self._lock = __import__("threading").Lock()
        self._state = _State()

    # ------------------------------------------------------------------
    def enter(self, code: str, detail: str = "") -> None:
        """进入某个关键阶段(等待点之前调用)。落盘,供卡死后定位。"""
        with self._lock:
            self._state = _State(stage=code.split("-")[0], code=code,
                                 detail=detail,
                                 since=time.time())
        self._flush()

    def fail(self, code: str, detail: str, hint: str = "") -> StageFailure:
        """整体功能不可用的失败:记录 + 落盘 + 返回给 UI 透出。"""
        logger.error("[STAGE] %s %s", code, detail)
        with self._lock:
            self._state = _State(stage=code.split("-")[0], code=code,
                                 detail=detail, since=time.time())
        self._flush()
        return StageFailure(code=code, detail=detail, hint=hint)

    def clear(self) -> None:
        """回到正常态(成功后调用;状态文件删除)。"""
        with self._lock:
            self._state = _State()
        try:
            os.remove(_state_path())
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _flush(self) -> None:
        try:
            path = _state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({
                    "stage": self._state.stage,
                    "code": self._state.code,
                    "detail": self._state.detail,
                    "since": datetime_iso(self._state.since),
                }, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 - 诊断绝不成为新故障点
            pass


def datetime_iso(t: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(
        timespec="seconds")


def read_last_state() -> Optional[dict]:
    """读取上次卡点状态(崩溃/卡死后的定位证据);无/损坏返回 None。"""
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


stage_tracker = StageTracker()
