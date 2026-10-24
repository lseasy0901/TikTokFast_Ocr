# -*- coding: utf-8 -*-
"""统一激活弹窗（Phase UI-3，Studio Minimal）。

设计：
    只呈现「一个 KEY 输入框 + 一个立即激活按钮」。**不再让用户选择在线/离线**——
    分派由 KEY 本身决定，规则就是现有授权实现里那条唯一的分派依据：
        normalize_key(key) ∈ OFFLINE_KEYS  → 离线激活（本地池，不联网）
        否则                                → 在线激活（服务器兑换 + 签名验证）
    刻意不做"在线失败就试离线"：那条路会让无效在线 KEY 去碰离线池，
    既掩盖真实失败原因，也让两条授权路径的语义互相污染。

    失败：在输入框下方显示简短错误，**保留用户输入**便于修正重试。
    成功：切换到结果视图，展示真实状态 / 有效期 / 功能权限（有才显示），
          并提供「开始使用」按钮。

线程约束（与既有实现一致）：
    网络请求与 susi_helper 子进程调用都在后台线程执行，结果经 Qt Signal
    排队回到 GUI 线程；对话框自身不触碰任何生命周期方法。
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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import GlassDialog
from gui.theme import Metrics, Palette
from utils.license_manager import (
    STATUS_TEXT,
    LicenseManager,
    LicenseResult,
    LicenseStatus,
)
from utils.offline_activation import OFFLINE_KEYS, normalize_key

logger = logging.getLogger("DouyinLowLatencyViewer.gui.activation")

def _qss() -> str:
    return f"""
QLabel {{ background: none; }}
#actTitle {{ font-size: 16px; font-weight: 600; color: {Palette.TEXT}; }}
#actHint {{ font-size: 12px; color: {Palette.TEXT_DIM}; }}
#actError {{ font-size: 12px; color: {Palette.DANGER_TEXT}; }}
#actState {{ font-size: 12px; color: {Palette.TEXT_DIM}; }}
#actState[tone="ok"] {{ color: {Palette.OK_TEXT}; }}
#actBig {{ font-size: 20px; font-weight: 600; color: {Palette.TEXT};
           letter-spacing: 1px; }}
#actMeta {{ font-size: 12px; color: {Palette.TEXT_DIM};
            font-family: {Palette.MONO_FAMILY}; }}
#actKey {{
    background-color: {Palette.INPUT_BG};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS}px;
    padding: 8px 10px;
    color: {Palette.TEXT};
    font-family: {Palette.MONO_FAMILY};
    font-size: 13px;
}}
#actKey:focus {{ border-color: {Palette.TEXT_DISABLED}; }}
#actKey:disabled {{ background-color: {Palette.DISABLED_BG}; color: {Palette.TEXT_DISABLED}; }}
"""


class LicenseWorker:
    """后台线程：执行一次许可证任务，不阻塞 GUI（与既有实现同构）。"""

    def __init__(self, task, on_done, name: str = "license-worker"):
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
        except Exception as e:  # noqa: BLE001 - 任何异常都不能让线程静默死掉
            logger.exception("许可证任务异常")
            result = LicenseResult(LicenseStatus.ACTIVATION_FAILED, f"激活异常: {e}")
        self._on_done(result)


def dispatch_activation(manager: LicenseManager, license_key: str) -> LicenseResult:
    """按 KEY 决定走在线还是离线（**唯一**分派入口，供弹窗与测试共用）。

    规则来自现有授权实现：内置离线 KEY 池命中 → 离线；否则在线。
    绝不在任何一条路径失败后再尝试另一条。
    """
    if normalize_key(license_key) in OFFLINE_KEYS:
        logger.info("[LICENSE] 命中内置离线 KEY 池 → 走离线激活")
        return manager.activate_offline(license_key)
    logger.info("[LICENSE] 未命中离线池 → 走在线激活")
    return manager.activate(license_key)


def _format_expiry(result: LicenseResult) -> str:
    """把签名凭据里的过期时间转成本地时区展示（只读，不做任何计算）。"""
    expires = result.expires_at
    if expires is None:
        return "永久"
    try:
        return expires.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 - 时区库缺失时退回 ISO 日期
        return str(expires)[:16]


class ActivationDialog(GlassDialog):
    """统一的激活 / 追加授权弹窗。

    Acrylic 只尝试一次（首次显示时），失败即保持 QSS 的不透明深色 —— 见
    :func:`gui.theme.apply_dialog_acrylic`。它是纯视觉的，不改变任何授权行为。
    """

    _activation_done_sig = Signal(object)

    def __init__(self, manager: LicenseManager, parent=None):
        super().__init__("激活 LiveLens", parent)
        self._manager = manager
        self._worker: LicenseWorker | None = None
        self._result: LicenseResult | None = None

        self.setFixedSize(440, 320)

        self._activation_done_sig.connect(self._on_activation_done)
        self._setup_ui()
        self._refresh_state_line()
        self._key_input.setFocus()

    # ------------------------------------------------------------------
    # 对外接口（调用方只依赖这两个）
    # ------------------------------------------------------------------
    def result_data(self) -> LicenseResult | None:
        """本次成功激活的结果；未成功为 None（不改变任何本地状态）。"""
        return self._result

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _setup_ui(self):
        # 基类玻璃卡 QSS + 本对话框的控件细节（拼接而非覆盖）
        self.setStyleSheet(self.styleSheet() + _qss())
        outer = self.card_layout

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_input_page())
        self._stack.addWidget(self._build_success_page())
        outer.addWidget(self._stack, 1)

    def _build_input_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # 卡片头已有「激活 LiveLens」（GlassDialog 基类），内层不再重复
        hint = QLabel("输入许可证密钥后点击「立即激活」。密钥与设备绑定，激活后凭据保存在本机。")
        hint.setObjectName("actHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        self._state_label = QLabel()
        self._state_label.setObjectName("actState")
        self._state_label.setWordWrap(True)
        col.addWidget(self._state_label)

        self._key_input = QLineEdit()
        self._key_input.setObjectName("actKey")
        self._key_input.setPlaceholderText("许可证密钥")
        self._key_input.setFixedHeight(38)
        self._key_input.returnPressed.connect(self._on_activate_clicked)
        self._key_input.textChanged.connect(self._clear_error)
        col.addWidget(self._key_input)

        self._error_label = QLabel("")
        self._error_label.setObjectName("actError")
        self._error_label.setWordWrap(True)
        col.addWidget(self._error_label)

        col.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self._close_btn = QPushButton("稍后再说")
        self._close_btn.setFixedHeight(34)
        self._close_btn.setMinimumWidth(96)
        self._close_btn.clicked.connect(self.reject)
        row.addWidget(self._close_btn)

        self._activate_btn = QPushButton("立即激活")
        self._activate_btn.setFixedHeight(34)
        self._activate_btn.setMinimumWidth(120)
        self._activate_btn.setProperty("variant", "primary")
        self._activate_btn.clicked.connect(self._on_activate_clicked)
        row.addWidget(self._activate_btn)
        col.addLayout(row)
        return page

    def _build_success_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        self._ok_label = QLabel("已激活")
        self._ok_label.setObjectName("actBig")
        col.addWidget(self._ok_label)

        self._expiry_label = QLabel()
        self._expiry_label.setObjectName("actMeta")
        self._expiry_label.setWordWrap(True)
        col.addWidget(self._expiry_label)

        # 功能权限只在凭据里真的有 features 时显示（不虚构授权信息）
        self._features_label = QLabel()
        self._features_label.setObjectName("actMeta")
        self._features_label.setWordWrap(True)
        col.addWidget(self._features_label)

        col.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._start_btn = QPushButton("开始使用")
        self._start_btn.setFixedHeight(34)
        self._start_btn.setMinimumWidth(120)
        self._start_btn.setProperty("variant", "primary")
        self._start_btn.clicked.connect(self.accept)
        row.addWidget(self._start_btn)
        col.addLayout(row)
        return page

    # ------------------------------------------------------------------
    # 状态 / 交互
    # ------------------------------------------------------------------
    def _refresh_state_line(self):
        """展示**真实**的当前授权状态（读取既有结果，不做任何推断）。"""
        current = self._manager.last_result
        if current is not None and current.status is LicenseStatus.ACTIVATED:
            self._state_label.setText(
                f"当前状态：{current.display_text} · 有效期至 {_format_expiry(current)}"
            )
            self._state_label.setProperty("tone", "ok")
        elif current is not None and current.status is not LicenseStatus.NOT_ACTIVATED:
            self._state_label.setText(f"当前状态：{current.display_text}")
            self._state_label.setProperty("tone", "")
        else:
            self._state_label.setText("当前状态：未激活")
            self._state_label.setProperty("tone", "")
        self._state_label.style().unpolish(self._state_label)
        self._state_label.style().polish(self._state_label)

    def _clear_error(self):
        if self._error_label.text():
            self._error_label.setText("")

    def _set_busy(self, busy: bool):
        self._key_input.setEnabled(not busy)
        self._activate_btn.setEnabled(not busy)
        self._close_btn.setEnabled(not busy)
        self._activate_btn.setText("激活中…" if busy else "立即激活")
        self.setCursor(Qt.BusyCursor if busy else Qt.ArrowCursor)

    def _on_activate_clicked(self):
        # 重复点击保护：任务在飞时直接忽略（后台同样有锁兜底）
        if self._worker is not None and self._worker.is_running():
            return

        license_key = self._key_input.text().strip()
        if not license_key:
            self._error_label.setText("请输入许可证密钥")
            return

        self._set_busy(True)
        self._error_label.setText("")
        self._worker = LicenseWorker(
            task=lambda: dispatch_activation(self._manager, license_key),
            on_done=self._activation_done_sig.emit,
        )
        self._worker.start()

    def _on_activation_done(self, result: LicenseResult):
        """后台线程完成（经 Qt 排队回到 GUI 线程）。"""
        self._set_busy(False)

        if result.is_activated:
            self._result = result
            self._key_input.setEnabled(False)
            self._ok_label.setText(result.display_text)
            self._expiry_label.setText(f"有效期至 {_format_expiry(result)}")
            features = sorted(result.features) if result.features else []
            self._features_label.setText(
                "功能权限：" + "、".join(features) if features else ""
            )
            self._stack.setCurrentIndex(1)
            self._start_btn.setFocus()
            return

        # 失败：留在输入页，保留用户输入，只在下方给出行内错误
        self._result = None
        detail = STATUS_TEXT.get(result.status, "激活失败")
        self._error_label.setText(
            f"{detail}：{result.message}" if result.message else detail
        )
        self._refresh_state_line()
        self._key_input.setFocus()
