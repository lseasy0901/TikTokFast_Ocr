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

from gui.theme import Palette


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
        title_label.setStyleSheet(f"""
            QLabel {{
                font-size: 15px;
                font-weight: 600;
                color: {Palette.DANGER_TEXT};
                background: none;
            }}
        """)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(title_label)

        # 详细消息
        detail_label = QLabel("许可证已到期，请续费后继续使用")
        detail_label.setStyleSheet(f"""
            QLabel {{
                font-size: 13px;
                color: {Palette.TEXT_DIM};
                background: none;
                line-height: 1.4;
            }}
        """)
        detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(detail_label)

        layout.addLayout(message_layout)

        # 按钮区域
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        # 前往官网充值按钮
        # 主 / 次按钮直接复用主题里的按钮样式
        recharge_btn = QPushButton("前往官网充值")
        recharge_btn.setFixedHeight(36)
        recharge_btn.setProperty("variant", "primary")
        recharge_btn.setMinimumWidth(120)
        recharge_btn.clicked.connect(self._on_recharge_clicked)

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setFixedHeight(36)
        close_btn.setMinimumWidth(96)
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