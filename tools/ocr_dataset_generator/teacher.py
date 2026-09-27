# -*- coding: utf-8 -*-
"""
PP-OCRv6 Teacher 适配器（Phase 2: Dataset Collector）。

职责**只有一件**：把 ROI 图像喂给项目现有的 PP-OCRv6，把结果原样带回来。

绝对不做
    * 不自己加载 Paddle 模型（不复制第二套模型加载实现）
    * 不读取 truth.csv / ground_truth / dataset.json 里的目标码
    * 不做"猜答案"的任何事

可注入
    构造函数接受任意实现了 `recognize()` 的对象，测试可以注入桩；
    默认实现才去构造生产用的 `ocr.ppocr_engine.PPOCREngine`。

接口边界
    `infer(image)` 的**唯一入参是图像**。没有 path、没有 sample_id、没有期望值——
    这样"Teacher 看见了答案"在类型层面就不可能发生（测试
    `test_teacher_adapter_accepts_only_image` 会锁住这个签名）。
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

__all__ = [
    "TeacherResult",
    "PPOCRTeacher",
    "TeacherError",
    "normalize_text",
    "READING_LABEL",
]

#: 与 ocr/ppocr_engine.py 的 READING_LABEL 一致（诊断列用）
READING_LABEL = "ppocr"

_WS = re.compile(r"\s+")


class TeacherError(RuntimeError):
    """Teacher 不可用（模型缺失、GPU 不可用、引擎构造失败）。"""


def normalize_text(text: Optional[str]) -> str:
    """与生产 `ocr.room_code.normalize_text` 同语义的轻量实现。

    为什么在这里重写而不是 import：本工具要能独立运行，import
    `ocr.room_code` 会连带执行 `ocr/__init__.py`（它 import worker / PySide6 /
    剪贴板），给数据集工具引入一大串与它无关的初始化副作用。语义保持一致的
    是**结果**：折叠所有空白、去零宽字符与 BOM，不改任何字符本身。
    """
    if not text:
        return ""
    cleaned = text.replace("﻿", "").replace("​", "").replace("‌", "")
    cleaned = cleaned.replace("‍", "").replace("⁠", "")
    return _WS.sub(" ", cleaned).strip()


@dataclass
class TeacherResult:
    """一帧 Teacher 原始输出。**原样保存，不做任何解释**。"""

    raw_text: str = ""
    normalized_text: str = ""
    score: Optional[float] = None
    #: 现有 API（PPOCREngine.recognize(return_details=True)）不提供字符框，
    #: 固定为 None。**不编造**。
    boxes: Optional[list] = None
    #: 本次调用是否成功（False = 引擎报错或没有结果）
    ok: bool = False
    error: str = ""
    elapsed_ms: float = 0.0

    def row(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "score": "" if self.score is None else round(self.score, 4),
            "boxes": "" if self.boxes is None else len(self.boxes),
            "ok": int(self.ok),
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


class PPOCRTeacher:
    """把 PP-OCRv6 包成"图像进、原始文本出"的 Teacher。"""

    def __init__(self, engine: Any = None, model_dir: Optional[str] = None):
        self._engine = engine
        self._owns_engine = engine is None
        self._model_dir = model_dir

    # ------------------------------------------------------------------
    @classmethod
    def available(cls) -> Tuple[bool, str]:
        """探测 Teacher 是否可用（不构造引擎，代价低）。"""
        try:
            import importlib.util
            for mod in ("paddle", "paddlex"):
                if importlib.util.find_spec(mod) is None:
                    return False, "缺少 %s（需要构建环境 requirements-packaging.txt）" % mod
            from ocr.ppocr_engine import PPOCRModelLocator
            path = PPOCRModelLocator.find_model_dir()
            return True, path
        except Exception as exc:  # noqa: BLE001 - 探测失败就是不可用
            return False, "%s: %s" % (type(exc).__name__, exc)

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from ocr.ppocr_engine import PPOCREngine
        except Exception as exc:  # noqa: BLE001
            raise TeacherError("无法导入 PP-OCRv6 后端: %s" % exc) from exc
        try:
            self._engine = PPOCREngine(model_dir=self._model_dir)
        except Exception as exc:  # noqa: BLE001
            raise TeacherError("PP-OCRv6 引擎构造失败: %s" % exc) from exc
        return self._engine

    # ------------------------------------------------------------------
    def infer(self, image) -> TeacherResult:
        """**唯一入参是图像。** 返回该帧的 Teacher 原始输出。"""
        t0 = time.perf_counter()
        try:
            engine = self._ensure_engine()
        except TeacherError as exc:
            return TeacherResult(ok=False, error=str(exc),
                                 elapsed_ms=(time.perf_counter() - t0) * 1000.0)
        try:
            detail = engine.recognize(image, return_details=True)
        except Exception as exc:  # noqa: BLE001 - 推理失败也要如实记录，不吞成"没读到"
            return TeacherResult(ok=False, error="%s: %s" % (type(exc).__name__, exc),
                                 elapsed_ms=(time.perf_counter() - t0) * 1000.0)

        if isinstance(detail, dict):
            raw = detail.get("text") or ""
            score = detail.get("confidence")
        else:                                  # 桩只返回字符串时也接受
            raw, score = (detail or ""), None
        norm = normalize_text(raw)
        boxes = detail.get("details") if isinstance(detail, dict) else None
        return TeacherResult(
            raw_text=raw, normalized_text=norm,
            score=float(score) if isinstance(score, (int, float)) else None,
            boxes=boxes, ok=bool(norm),
            error="" if norm else "无结果",
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ------------------------------------------------------------------
    @property
    def model_dir(self) -> Optional[str]:
        return getattr(self._engine, "model_dir", self._model_dir)

    def close(self) -> None:
        """只释放**自己构造的**引擎；注入进来的由调用方负责。"""
        if self._engine is not None and self._owns_engine:
            try:
                self._engine.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._engine = None
