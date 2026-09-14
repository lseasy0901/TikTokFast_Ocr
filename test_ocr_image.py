#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LiveLens OCR 单图诊断工具（独立测试入口，不属于生产流程）。

用法：
    python test_ocr_image.py

选一张房间号截图 -> 用项目里现成的 OCR 算法识别（valoant 档）-> 显示结果。
本文件不实现任何识别逻辑：格式校验、逐位纠正、候选提取全部来自 ocr/ 下的既有实现。
"""

import sys

import cv2
import numpy as np
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ocr.engine import OCREngine, OCREngineError
from ocr.room_code import RoomCodeRecognizer, SingleFrameResolver

PROFILE_ID = "valorant"
NO_RESULT_TEXT = "未识别"


def load_image(path: str):
    """用 OpenCV 读图（imdecode 以兼容中文/空格路径）。"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def recognize_image(recognizer: RoomCodeRecognizer, path: str) -> str:
    """单张图片 -> 展示用文本（房间号 / 未识别 / 可读的错误说明）。"""
    try:
        image = load_image(path)
    except Exception as exc:
        return f"读取图片失败: {exc}"
    if image is None or image.size == 0:
        return "读取图片失败: 不是有效的 PNG/JPG 图片"

    try:
        candidate = recognizer.recognize_frame(image)
    except OCREngineError as exc:
        return f"OCR 引擎错误: {exc}"
    except Exception as exc:
        return f"OCR 异常: {exc}"

    return candidate.code if candidate is not None else NO_RESULT_TEXT


class OcrTestWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LiveLens OCR 单图测试")
        self.setFixedWidth(560)

        self._recognizer = None

        self._select_btn = QPushButton("选择图片")
        self._select_btn.clicked.connect(self._on_select)

        self._path_label = QLabel("未选择图片")
        self._path_label.setWordWrap(True)

        self._result_label = QLabel("—")
        self._result_label.setStyleSheet("font-size: 22px; font-weight: 600;")
        self._result_label.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self._select_btn)
        layout.addWidget(self._path_label)
        layout.addWidget(QLabel("识别结果:"))
        layout.addWidget(self._result_label)
        self.setLayout(layout)

    def _ensure_recognizer(self) -> RoomCodeRecognizer:
        """首次使用时构造引擎 + 识别器（单图诊断，构造慢可以接受）。"""
        if self._recognizer is None:
            self._recognizer = RoomCodeRecognizer(
                OCREngine(),
                profile=PROFILE_ID,
                resolver=SingleFrameResolver(),  # 单帧直通：一张图立刻出结果
            )
        return self._recognizer

    def _on_select(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择房间号截图", "", "图片 (*.png *.jpg *.jpeg)"
        )
        if not path:
            return

        self._path_label.setText(path)
        self._result_label.setText("识别中…")
        QApplication.processEvents()  # 先刷新界面（引擎首次构造较慢）

        try:
            recognizer = self._ensure_recognizer()
        except Exception as exc:
            self._result_label.setText(f"OCR 引擎初始化失败: {exc}")
            return

        self._result_label.setText(recognize_image(recognizer, path))


def main() -> int:
    app = QApplication(sys.argv)
    window = OcrTestWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
