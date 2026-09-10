# -*- coding: utf-8 -*-
"""
过期对话框

许可证到期时显示的对话框
"""

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox
)
from PySide6.QtCore import Qt


class ExpiredDialog(QDialog):
    """许可证过期对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用期限已到期")
        self.setModal(True)
        self.setFixedSize(400, 200)
        self._setup_ui()

    def _setup_ui(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 消息区域
        message_layout = QVBoxLayout()
        message_layout.setSpacing(12)

        # 标题
        title_label = QLabel("使用期限已到期")
        title_label.setStyleSheet("""
            QLabel {
                font-size: 16px;
                font-weight: 600;
                color: #e53e3e;
                background: none;
            }
        """)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(title_label)

        # 详细消息
        detail_label = QLabel("免费试用已结束，请充值后继续使用")
        detail_label.setStyleSheet("""
            QLabel {
                font-size: 13px;
                color: #a0aec0;
                background: none;
                line-height: 1.4;
            }
        """)
        detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(detail_label)

        layout.addLayout(message_layout)

        # 按钮区域
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        # 前往官网充值按钮
        recharge_btn = QPushButton("前往官网充值")
        recharge_btn.setFixedHeight(36)
        recharge_btn.setStyleSheet("""
            QPushButton {
                padding: 8px 24px;
                border: 1px solid #4a90e2;
                border-radius: 6px;
                background-color: #4a90e2;
                color: white;
                font-size: 13px;
                font-weight: 500;
                min-height: 36px;
            }
            QPushButton:hover {
                background-color: #357abd;
            }
            QPushButton:pressed {
                background-color: #2968a3;
            }
        """)
        recharge_btn.clicked.connect(self._on_recharge_clicked)

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet("""
            QPushButton {
                padding: 8px 24px;
                border: 1px solid #4a5568;
                border-radius: 6px;
                background-color: #2d3748;
                color: #e2e8f0;
                font-size: 13px;
                font-weight: 500;
                min-height: 36px;
            }
            QPushButton:hover {
                background-color: #4a5568;
            }
            QPushButton:pressed {
                background-color: #1a202c;
            }
        """)
        close_btn.clicked.connect(self.accept)

        button_layout.addWidget(recharge_btn)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)

    def _on_recharge_clicked(self):
        """充值按钮点击事件（占位）"""
        # 目前只是一个占位功能
        QMessageBox.information(
            self,
            "提示",
            "充值功能暂未开放，请稍后再试。"
        )