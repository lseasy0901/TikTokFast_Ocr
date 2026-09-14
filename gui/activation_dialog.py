# -*- coding: utf-8 -*-
"""
许可证激活对话框 - Phase 7.2-6.5

用户输入许可证密钥 → 后台线程完成「服务器兑换 + 本地验证」→ 展示结果。

线程约束（与 _ConnectWorker 一致）：
    网络请求与 susi_helper 子进程调用都在后台线程执行，
    GUI 线程只负责界面更新。结果通过 Qt Signal（自动排队）回到 GUI 线程。
"""

import logging
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from gui.theme import Metrics, Palette
from utils.license_manager import (
    STATUS_TEXT,
    LicenseManager,
    LicenseResult,
    LicenseStatus,
)

logger = logging.getLogger("DouyinLowLatencyViewer.gui.activation")


class LicenseWorker:
    """后台线程：执行一次许可证任务，不阻塞 GUI。

    与 gui.main_window._ConnectWorker 相同的模式：线程内直接调用回调，
    回调再 emit Qt Signal，由 Qt 排队投递回 GUI 线程。
    """

    def __init__(self, task, on_done, name: str = "license-worker"):
        """
        :param task:    无参可调用对象，返回 LicenseResult
        :param on_done: 完成回调，签名为 callback(result: LicenseResult)
        """
        self._task = task
        self._on_done = on_done
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self):
        try:
            result = self._task()
        except Exception as e:  # 兜底：任何异常都不能让线程静默死掉
            logger.exception("许可证任务异常")
            result = LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, f"激活异常: {e}"
            )
        self._on_done(result)


class ActivationDialog(QDialog):
    """许可证激活对话框。"""

    # 跨线程安全 Signal：后台线程 → GUI 线程
    _activation_done_sig = Signal(object)

    def __init__(self, manager: LicenseManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._worker: LicenseWorker | None = None
        self._result: LicenseResult | None = None

        self.setWindowTitle("激活许可证")
        self.setModal(True)
        self.setFixedSize(420, 260)

        self._activation_done_sig.connect(self._on_activation_done)

        self._setup_ui()
        self._show_current_status()

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------
    def result_data(self) -> LicenseResult | None:
        """激活成功时的结果；未成功则为 None。"""
        return self._result

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        # 内联样式只保留「这个对话框独有的排版」，颜色全部取自主题调色板，
        # 避免这里和 QSS 各写一套十六进制以后互相漂移。
        title = QLabel("激活许可证")
        title.setStyleSheet(
            f"QLabel {{ font-size: 15px; font-weight: 600;"
            f" color: {Palette.TEXT}; background: none; }}"
        )
        layout.addWidget(title)

        hint = QLabel("请输入许可证密钥，激活后凭据将保存在本机。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM}; background: none; }}"
        )
        layout.addWidget(hint)

        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("许可证密钥")
        self._key_input.setFixedHeight(36)
        self._key_input.setStyleSheet(
            f"""
            QLineEdit {{
                padding: 6px 10px;
                border: 1px solid {Palette.BORDER};
                border-radius: {Metrics.RADIUS}px;
                background-color: {Palette.INPUT_BG};
                color: {Palette.TEXT};
                font-size: 13px;
            }}
            QLineEdit:focus {{ border-color: {Palette.ACCENT}; }}
            QLineEdit:disabled {{
                background-color: {Palette.DISABLED_BG};
                color: {Palette.TEXT_DISABLED};
            }}
            """
        )
        self._key_input.returnPressed.connect(self._on_activate_clicked)
        layout.addWidget(self._key_input)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM}; background: none; }}"
        )
        layout.addWidget(self._status_label)

        layout.addStretch()

        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        # 主 / 次按钮直接复用主题里的按钮样式（variant=primary 即强调色）
        self._activate_btn = QPushButton("激活")
        self._activate_btn.setFixedHeight(36)
        self._activate_btn.setProperty("variant", "primary")
        self._activate_btn.setMinimumWidth(96)
        self._activate_btn.clicked.connect(self._on_activate_clicked)

        self._close_btn = QPushButton("关闭")
        self._close_btn.setFixedHeight(36)
        self._close_btn.setMinimumWidth(96)
        self._close_btn.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(self._activate_btn)
        button_layout.addWidget(self._close_btn)
        layout.addLayout(button_layout)

    def _show_current_status(self):
        """打开对话框时显示当前已知状态。"""
        current = self._manager.last_result
        if current is not None and current.status is LicenseStatus.ACTIVATED:
            self._set_status(f"当前状态：{current.display_text}", ok=True)
        else:
            self._set_status("当前状态：未激活")

    def _set_status(self, text: str, ok: bool | None = None):
        color = Palette.TEXT_DIM
        if ok is True:
            color = Palette.OK_TEXT
        elif ok is False:
            color = Palette.DANGER_TEXT
        self._status_label.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {color}; background: none; }}"
        )
        self._status_label.setText(text)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool):
        self._key_input.setEnabled(not busy)
        self._activate_btn.setEnabled(not busy)
        self._close_btn.setEnabled(not busy)
        self._activate_btn.setText("激活中..." if busy else "激活")

    def _on_activate_clicked(self):
        if self._worker is not None and self._worker.is_running():
            return

        license_key = self._key_input.text().strip()
        if not license_key:
            self._set_status("请输入许可证密钥", ok=False)
            return

        self._set_busy(True)
        self._set_status("正在激活，请稍候...")

        # 网络请求 + 子进程验证全部在后台线程完成
        self._worker = LicenseWorker(
            task=lambda: self._manager.activate(license_key),
            on_done=self._activation_done_sig.emit,
        )
        self._worker.start()

    def _on_activation_done(self, result: LicenseResult):
        """后台线程完成（经 Qt 排队回到 GUI 线程）。"""
        self._set_busy(False)

        if result.is_activated:
            self._result = result
            self._key_input.clear()
            self._key_input.setEnabled(False)
            self._activate_btn.setEnabled(False)
            self._activate_btn.setText("已激活")
            self._set_status(f"激活成功：{result.display_text}", ok=True)
            return

        # 失败：保持对话框打开，用户可以修正密钥后重试
        self._result = None
        detail = STATUS_TEXT.get(result.status, "激活失败")
        message = f"{detail}：{result.message}" if result.message else detail
        self._set_status(message, ok=False)
