# -*- coding: utf-8 -*-
"""UI 偏好持久化（极小 JSON，随数据目录走）。

与 stream_history.json 同目录（Windows: %LOCALAPPDATA%/LiveLens/），
不进 config.yaml、不进注册表：这是纯展示偏好，损坏即回默认值，
绝不能影响直播 / OCR / 授权任何主流程。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

__all__ = ["get_pref", "set_pref", "PERF_PINNED"]

logger = logging.getLogger("DouyinLowLatencyViewer.utils.ui_prefs")

#: 键：状态栏是否常驻显示性能指标（默认 False = 收进「诊断」弹层）
PERF_PINNED = "perf_pinned"

_FILENAME = "ui_prefs.json"
_cache: dict[str, Any] | None = None


def _file_path():
    from utils.stream_history import default_data_dir

    return default_data_dir() / _FILENAME


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        with open(_file_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _cache = data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 - 坏文件即回默认，不阻断启动
        logger.warning("ui_prefs.json 读取失败，按默认值处理: %s", e)
    return _cache


def _save() -> None:
    assert _cache is not None
    path = _file_path()
    try:
        os.makedirs(path.parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - 写失败只影响"记住偏好"
        logger.warning("ui_prefs.json 写入失败: %s", e)


def get_pref(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set_pref(key: str, value: Any) -> None:
    _load()
    _cache[key] = value
    _save()
