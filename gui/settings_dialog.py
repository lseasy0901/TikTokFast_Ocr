# -*- coding: utf-8 -*-
"""设置中心（Phase UI-3，Studio Minimal）。

结构：左侧分类导航 + 右侧内容区。

只放**实际存在**的设置：
    授权  —— 真实激活状态 / 剩余有效期 / 「追加授权」入口（复用统一激活弹窗）
    关于  —— LiveLens、真实版本号、已配置的官网地址、联系方式（含复制）

刻意没有的：
    - 不新增任何设置项（没有可配置项就不摆空白分类）
    - 不做自动更新通知的重设计（本阶段范围外）
    - 不触碰直播 / OCR / ROI：打开或关闭本弹窗不会调用任何生命周期方法

线程与依赖：纯 GUI，无网络、无子进程；「追加授权」只是打开既有激活弹窗。
"""

import logging
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import GlassDialog
from gui.activation_dialog import ActivationDialog
from gui.theme import Metrics, Palette
from utils.license_manager import LicenseManager, LicenseStatus
from utils.ui_prefs import PERF_PINNED, get_pref, set_pref
from utils.update_source import DEFAULT_UPDATE_SOURCE

logger = logging.getLogger("DouyinLowLatencyViewer.gui.settings")

#: 作者联系方式（RELEASE-01：统一为 QQ，不再使用微信）
CONTACT_QQ = "1752924057"

#: 购买授权跳转（未激活界面「购买授权」按钮打开的淘宝链接）
PURCHASE_URL = "https://m.tb.cn/h.jaMe2gX?tk=CfF8TI9nJew"


def website_url() -> str:
    """已配置的官网地址；从**既有配置**的更新源推导（不新编造域名）。

    取 DEFAULT_UPDATE_SOURCE 的 scheme + host：它已经是产品配置过的域名，
    这里只是把路径去掉得到官网地址。
    """
    try:
        parts = urlsplit(DEFAULT_UPDATE_SOURCE)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:  # noqa: BLE001
        pass
    return ""


_QSS = f"""
QLabel {{ background: none; }}
#setNav {{ background: transparent; border: none; outline: none;
           color: {Palette.TEXT_DIM}; font-size: 12px; }}
#setNav::item {{ padding: 7px 10px; border-radius: {Metrics.RADIUS}px; }}
#setNav::item:hover {{ background-color: {Palette.HOVER}; color: {Palette.TEXT}; }}
#setNav::item:selected {{ background-color: {Palette.ACCENT_DIM}; color: {Palette.TEXT}; }}
#setTitle {{ font-size: 14px; font-weight: 600; color: {Palette.TEXT}; }}
#setLabel {{ font-size: 11px; color: {Palette.TEXT_MUTED}; letter-spacing: 0.6px; }}
#setValue {{ font-size: 13px; color: {Palette.TEXT}; }}
#setMono {{ font-size: 12px; color: {Palette.TEXT_DIM};
            font-family: {Palette.MONO_FAMILY}; }}
#setHint {{ font-size: 11px; color: {Palette.TEXT_MUTED}; }}

"""


class SettingsDialog(GlassDialog):
    """居中设置中心。

    Acrylic 与激活弹窗同一套策略：首次显示尝试一次，失败即保持不透明深色
    （见 :func:`gui.theme.apply_dialog_acrylic`）。
    """

    NAV_WIDTH = 132

    def __init__(self, manager: LicenseManager, version: str,
                 access_status=None, append_license_callback=None,
                 perf_pinned_callback=None, parent=None):
        """
        :param manager:   LicenseManager（只读它的 last_result 展示真实状态）
        :param version:   真实版本号（由调用方给出，弹窗不自行推断）
        :param access_status: AccessStatus（取真实状态文案）
        :param append_license_callback: 追加授权成功后的回调（把结果交回主窗口）
        :param perf_pinned_callback: 「常驻显示性能指标」开关变化时的回调
            （布尔值 = 是否常驻；主窗口用它即时切换状态栏/诊断弹层）
        """
        super().__init__("设置", parent)
        self._manager = manager
        self._version = version
        self._access_status = access_status
        self._on_appended = append_license_callback
        self._on_perf_pinned = perf_pinned_callback

        self.setFixedSize(560, 420)
        self._setup_ui()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        self.setStyleSheet(self.styleSheet() + _QSS)
        outer = QHBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.card_layout.addLayout(outer, 1)

        # 左侧分类
        nav_box = QWidget()
        nav_box.setFixedWidth(self.NAV_WIDTH)
        nav_col = QVBoxLayout(nav_box)
        nav_col.setContentsMargins(14, 18, 8, 14)
        nav_col.setSpacing(8)
        self._nav = QListWidget()
        self._nav.setObjectName("setNav")
        self._nav.setFrameShape(QFrame.NoFrame)
        self._nav.setFocusPolicy(Qt.NoFocus)
        for label in ("授权", "偏好", "关于"):
            self._nav.addItem(QListWidgetItem(label))
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        nav_col.addWidget(self._nav, 1)
        outer.addWidget(nav_box)

        # 右侧内容
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_license_page())
        self._stack.addWidget(self._build_prefs_page())
        self._stack.addWidget(self._build_about_page())
        outer.addWidget(self._stack, 1)

        self._nav.setCurrentRow(0)

    # ------------------------------------------------------------------
    # 偏好页
    # ------------------------------------------------------------------
    def _build_prefs_page(self) -> QWidget:
        """偏好：目前只有一项 —— 性能指标常驻还是收进「诊断」弹层。

        「只放实际存在的设置」原则下，这一页随着第一个真实偏好出现；
        不为凑数摆空开关。
        """
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        title = QLabel("偏好")
        title.setObjectName("setTitle")
        col.addWidget(title)

        self._perf_pinned_cb = QCheckBox("在状态栏常驻显示性能指标")
        self._perf_pinned_cb.setChecked(bool(get_pref(PERF_PINNED, False)))
        self._perf_pinned_cb.toggled.connect(self._on_perf_pinned_toggled)
        col.addWidget(self._perf_pinned_cb)

        hint = QLabel("关闭时，延迟 / 帧率 / 渲染等诊断指标收纳在"
                      "状态栏右下角的「诊断」弹层里。")
        hint.setObjectName("setHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        col.addStretch(1)
        return page

    def _on_perf_pinned_toggled(self, checked: bool) -> None:
        set_pref(PERF_PINNED, bool(checked))
        if self._on_perf_pinned is not None:
            self._on_perf_pinned(bool(checked))

    # ------------------------------------------------------------------
    # 授权页
    # ------------------------------------------------------------------
    def _build_license_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        title = QLabel("授权")
        title.setObjectName("setTitle")
        col.addWidget(title)

        col.addWidget(self._label("当前状态"))
        self._license_state = QLabel()
        self._license_state.setObjectName("setValue")
        self._license_state.setWordWrap(True)
        col.addWidget(self._license_state)

        col.addWidget(self._label("有效期"))
        self._license_expiry = QLabel()
        self._license_expiry.setObjectName("setMono")
        self._license_expiry.setWordWrap(True)
        col.addWidget(self._license_expiry)

        col.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._append_btn = QPushButton("追加授权")
        self._append_btn.setFixedHeight(32)
        self._append_btn.setMinimumWidth(112)
        self._append_btn.setProperty("variant", "primary")
        self._append_btn.setToolTip("使用另一个许可证密钥为当前设备追加授权时长")
        self._append_btn.clicked.connect(self._on_append_license)
        row.addWidget(self._append_btn)
        col.addLayout(row)
        self._refresh_license_page()
        return page

    def _refresh_license_page(self):
        """只展示真实状态：有凭据才显示有效期，未激活就直说未激活。"""
        current = self._manager.last_result
        if self._access_status is not None:
            self._license_state.setText(self._access_status.get_status_text())
        elif current is not None:
            self._license_state.setText(current.display_text)
        else:
            self._license_state.setText("未激活")

        if current is not None and current.status is LicenseStatus.ACTIVATED \
                and current.expires_at is not None:
            try:
                self._license_expiry.setText(
                    "至 " + current.expires_at.astimezone().strftime("%Y-%m-%d %H:%M"))
            except Exception:  # noqa: BLE001 - 时区库缺失时退回 ISO 日期
                self._license_expiry.setText("至 " + str(current.expires_at)[:16])
        elif current is not None and current.status is LicenseStatus.ACTIVATED:
            self._license_expiry.setText("永久")
        else:
            self._license_expiry.setText("无有效授权")

    def _on_append_license(self):
        """追加授权：复用统一激活弹窗；成功后就地刷新并把结果交回主窗口。"""
        dialog = ActivationDialog(self._manager, self)
        dialog.exec()
        result = dialog.result_data()
        if result is None:
            return
        logger.info("[SETTINGS] 追加授权成功，状态=%s", result.display_text)
        if self._on_appended is not None:
            self._on_appended(result)
        self._refresh_license_page()

    # ------------------------------------------------------------------
    # 关于页
    # ------------------------------------------------------------------
    def _build_about_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        title = QLabel("关于")
        title.setObjectName("setTitle")
        col.addWidget(title)

        name = QLabel("LiveLens")
        name.setObjectName("setValue")
        col.addWidget(name)

        col.addWidget(self._label("版本"))
        version = QLabel(self._version)
        version.setObjectName("setMono")
        col.addWidget(version)

        site = website_url()
        if site:
            col.addWidget(self._label("官网"))
            col.addLayout(self._copy_row(site, site))

        # 联系方式已迁到主窗口标题栏的「联系作者」按钮，这里不再重复一份
        col.addStretch(1)
        return page

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("setLabel")
        return label

    def _copy_row(self, label: str, value: str) -> QHBoxLayout:
        """一行「标签 + 值 + 复制按钮」；复制走系统剪贴板（与 OCR 规则无关）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel(f"{label}  ")
        name.setObjectName("setMono")
        row.addWidget(name)

        text = QLabel(value)
        text.setObjectName("setMono")
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(text)

        hint = QLabel("")
        hint.setObjectName("setHint")
        row.addWidget(hint)

        btn = QPushButton("复制")
        btn.setFixedHeight(26)
        btn.setMinimumWidth(56)

        def _copy():
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(value)
                hint.setText("已复制")

        btn.clicked.connect(_copy)
        row.addWidget(btn)
        row.addStretch(1)
        return row

    def _on_nav_changed(self, row: int):
        if row >= 0:
            self._stack.setCurrentIndex(row)
