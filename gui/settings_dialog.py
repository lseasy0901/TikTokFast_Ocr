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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import GlassDialog, IconButton, icon_pixmap, open_external_url
from automation.hotkey import VK_NAMES
from gui.activation_dialog import ActivationDialog
from gui.theme import Metrics, Palette
from utils.license_manager import LicenseManager, LicenseStatus
from gui.theme import current_theme
from utils.ui_prefs import PERF_PINNED, UI_THEME, get_pref, set_pref
from utils.update_source import DEFAULT_UPDATE_SOURCE

logger = logging.getLogger("DouyinLowLatencyViewer.gui.settings")

#: 作者联系方式（RELEASE-01：统一为 QQ，不再使用微信）
CONTACT_QQ = "1752924057"
#: 意见反馈渠道（2026-10-24 用户指定）
CONTACT_EMAIL = "1752924057@qq.com"
CONTACT_QQ_GROUP = "1035921366"

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


def _qss() -> str:
    return f"""
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
#fbCard {{
    background-color: {Palette.INPUT_BG};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.CONTROL_RADIUS}px;
}}
#fbIcon {{
    background-color: {Palette.ELEVATED};
    border-radius: 8px;
}}
#fbTitle {{ font-size: 13px; font-weight: 600; color: {Palette.TEXT};
            background: none; }}
#fbSub {{ font-size: 12px; color: {Palette.TEXT_DIM};
          font-family: {Palette.MONO_FAMILY}; background: none; }}
#fbCopyBtn {{ min-width: 64px; padding: 4px 12px; }}

"""


class SettingsDialog(GlassDialog):
    """居中设置中心。

    Acrylic 与激活弹窗同一套策略：首次显示尝试一次，失败即保持不透明深色
    """

    NAV_WIDTH = 132

    def __init__(self, manager: LicenseManager, version: str,
                 access_status=None, append_license_callback=None,
                 perf_pinned_callback=None, theme_callback=None,
                 automation_callback=None, automation_config=None,
                 start_calibration_callback=None, parent=None):
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
        self._on_theme_switch = theme_callback
        #: 自动加入:配置对象(主窗口持有的 JoinConfig,改动即时生效)+
        #: 变更回调(热键重注册)+ 校准启动回调(关闭设置弹窗并开始校准)
        # None(测试/独立构造)时自建一份:页面构建只读常规字段,
        # 不会因此写盘;改动回调为 None 时也无副作用。
        if automation_config is None:
            from automation.join_config import JoinConfig
            automation_config = JoinConfig.load()
        self._automation_cfg = automation_config
        self._on_automation_changed = automation_callback
        self._start_calibration = start_calibration_callback

        self.setFixedSize(560, 420)
        self._setup_ui()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        self.setStyleSheet(self.styleSheet() + _qss())
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
        for label in ("授权", "偏好", "自动加入", "意见反馈", "关于"):
            self._nav.addItem(QListWidgetItem(label))
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        nav_col.addWidget(self._nav, 1)
        outer.addWidget(nav_box)

        # 右侧内容
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_license_page())
        self._stack.addWidget(self._build_prefs_page())
        self._stack.addWidget(self._build_auto_join_page())
        self._stack.addWidget(self._build_feedback_page())
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

        col.addWidget(self._label("外观主题"))
        theme_row = QHBoxLayout()
        self._theme_buttons: list[QPushButton] = []
        for value, label in (("dark", "深色"), ("light", "浅色")):
            btn = QPushButton(label)
            btn.setObjectName("segmentBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(current_theme() == value)
            btn.clicked.connect(lambda _=False, v=value: self._on_theme_clicked(v))
            theme_row.addWidget(btn)
            self._theme_buttons.append(btn)
        theme_row.addStretch(1)
        col.addLayout(theme_row)

        self._perf_pinned_cb = QCheckBox("在状态栏常驻显示性能指标")
        self._perf_pinned_cb.setChecked(bool(get_pref(PERF_PINNED, False)))
        self._perf_pinned_cb.toggled.connect(self._on_perf_pinned_toggled)
        col.addWidget(self._perf_pinned_cb)

        hint = QLabel("浅色主题下视频画布仍为暗场（观感与 LightC 一致）；"
                      "延迟 / 帧率等诊断指标默认收纳在状态栏「诊断」弹层里。")
        hint.setObjectName("setHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        col.addStretch(1)
        return page

    def _on_theme_clicked(self, value: str) -> None:
        """外观主题分段按钮：互斥选中 + 即时切换。"""
        for btn in self._theme_buttons:
            btn.setChecked(False)
        sender = self.sender()
        sender.setChecked(True)
        set_pref(UI_THEME, value)
        if self._on_theme_switch is not None:
            self._on_theme_switch(value)

    # ------------------------------------------------------------------
    # 自动加入页
    # ------------------------------------------------------------------
    def _build_auto_join_page(self) -> QWidget:
        """自动加入:开关/热键/窗口匹配/校准入口。

        这里只改 self._automation_cfg(主窗口持有的同一对象)并通知回调;
        持久化由 JoinConfig.save 在回调里统一做(单点落盘)。
        """
        cfg = self._automation_cfg
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        title = QLabel("自动加入")
        title.setObjectName("setTitle")
        col.addWidget(title)

        self._aj_enabled = QCheckBox("启用:识别到房间码后按热键自动加入")
        self._aj_enabled.setChecked(bool(cfg.enabled))
        self._aj_enabled.toggled.connect(
            lambda v: self._aj_apply("enabled", v))
        col.addWidget(self._aj_enabled)

        # 热键
        row1 = QHBoxLayout()
        row1.addWidget(self._label("加入热键"))
        self._aj_hotkey = QComboBox()
        for name in VK_NAMES:
            self._aj_hotkey.addItem(name)
        self._aj_hotkey.setCurrentText(cfg.hotkey)
        self._aj_hotkey.currentTextChanged.connect(
            lambda t: self._aj_apply("hotkey", t))
        row1.addWidget(self._aj_hotkey)
        row1.addStretch(1)
        col.addLayout(row1)

        # 窗口匹配
        row2 = QHBoxLayout()
        row2.addWidget(self._label("窗口标题"))
        self._aj_title = QLineEdit(cfg.window_title)
        self._aj_title.setFixedWidth(200)
        self._aj_title.textChanged.connect(
            lambda t: self._aj_apply("window_title", t))
        row2.addWidget(self._aj_title)
        row2.addStretch(1)
        col.addLayout(row2)

        self._aj_confirm = QCheckBox("粘贴后自动点击「加入房间」")
        self._aj_confirm.setChecked(bool(cfg.auto_confirm))
        self._aj_confirm.toggled.connect(
            lambda v: self._aj_apply("auto_confirm", v))
        col.addWidget(self._aj_confirm)

        # 校准
        col.addWidget(self._label("点击位置校准"))
        cal_row = QHBoxLayout()
        self._aj_cal_status = QLabel(cfg.points_summary())
        self._aj_cal_status.setObjectName("setValue")
        cal_row.addWidget(self._aj_cal_status, 1)
        self._aj_cal_btn = QPushButton("开始校准…")
        self._aj_cal_btn.setObjectName("fbCopyBtn")
        self._aj_cal_btn.setCursor(Qt.PointingHandCursor)
        self._aj_cal_btn.setToolTip(
            "在游戏「自定义房间」页面依次点击三处,记录相对窗口位置")
        self._aj_cal_btn.clicked.connect(self._on_start_calibration)
        cal_row.addWidget(self._aj_cal_btn)
        col.addLayout(cal_row)

        hint = QLabel(
            "使用前提:游戏以窗口模式运行,并停留在「加入房间」页面。"
            "识别到房间码会自动复制;按热键即自动切换到游戏,依次完成"
            "「输入框 → 粘贴 → 加入」三步点击。\n"
            "注意:自动操作游戏存在被反作弊判定的风险,请自行评估后使用。")
        hint.setObjectName("setHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        col.addStretch(1)
        return page

    def _aj_apply(self, key: str, value) -> None:
        """把设置页的改动写回共享配置并通知主窗口(热键重注册/落盘)。"""
        if self._automation_cfg is None:
            return
        if getattr(self._automation_cfg, key) == value:
            return
        setattr(self._automation_cfg, key, value)
        if key in ("enabled", "hotkey"):
            self._aj_refresh_status()
        if self._on_automation_changed is not None:
            self._on_automation_changed()

    def _aj_refresh_status(self) -> None:
        if self._automation_cfg is not None and hasattr(self, "_aj_cal_status"):
            self._aj_cal_status.setText(self._automation_cfg.points_summary())

    def _on_start_calibration(self) -> None:
        """开始校准:关闭设置弹窗(腾出屏幕),由主窗口接管引导条。"""
        if self._start_calibration is not None:
            self._start_calibration()
        self.close()

    # ------------------------------------------------------------------
    # 意见反馈页
    # ------------------------------------------------------------------
    def _copy_text(self, text: str, button: QPushButton, done: str = "已复制 ✓") -> None:
        """复制到剪贴板，按钮短暂显示「已复制」作为就地反馈。"""
        QApplication.clipboard().setText(text)
        original = button.text()
        button.setText(done)
        button.setEnabled(False)
        QTimer.singleShot(1600, lambda: (
            button.setText(original), button.setEnabled(True)))

    def _feedback_card(self, icon_kind: str, title: str, subtitle: str,
                       copy_value: str, action_text: str = "复制") -> QFrame:
        """反馈渠道卡片：图标 + 标题/副标 + 复制动作（参照 LightC 样式）。"""
        card = QFrame()
        card.setObjectName("fbCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(icon_pixmap(icon_kind, 18, Palette.TEXT))
        icon.setFixedSize(36, 36)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setObjectName("fbIcon")
        row.addWidget(icon)

        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("fbTitle")
        col.addWidget(t)
        sub = QLabel(subtitle)
        sub.setObjectName("fbSub")
        col.addWidget(sub)
        row.addLayout(col, 1)

        btn = QPushButton(action_text)
        btn.setObjectName("fbCopyBtn")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(f"复制 {copy_value} 到剪贴板")
        btn.clicked.connect(
            lambda _=False, v=copy_value, b=btn: self._copy_text(v, b))
        row.addWidget(btn)
        return card

    def _build_feedback_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        title = QLabel("意见反馈")
        title.setObjectName("setTitle")
        col.addWidget(title)

        col.addWidget(self._label("问题反馈"))
        hint = QLabel("使用中遇到问题或有改进建议，欢迎通过以下方式联系我：")
        hint.setObjectName("setHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        col.addWidget(self._feedback_card(
            IconButton.CONTACT, "邮件反馈", CONTACT_EMAIL, CONTACT_EMAIL))

        col.addSpacing(6)
        col.addWidget(self._label("交流"))
        col.addWidget(self._feedback_card(
            IconButton.CONTACT, "QQ 群", CONTACT_QQ_GROUP, CONTACT_QQ_GROUP))

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
            col.addLayout(self._site_row(site))

        # 联系方式已迁到主窗口标题栏的「联系作者」按钮，这里不再重复一份
        col.addStretch(1)
        return page

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("setLabel")
        return label

    def _site_row(self, url: str) -> QHBoxLayout:
        """关于页官网行：URL + 「打开」按钮，点击走系统默认浏览器。

        历史注记：这一行原本是「复制」按钮，且调用时把 URL 误当行标签
        传入（_copy_row(site, site)），左侧出现一截截断的 URL —— 2026-10-24
        用户指认。现标签固定「官网」，动作为浏览器打开（open_external_url
        失败时会弹窗给出 URL 原文，不静默假报成功）；URL 仍可鼠标选中复制。
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel("官网")
        name.setObjectName("setMono")
        row.addWidget(name)

        text = QLabel(url)
        text.setObjectName("setMono")
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(text, 1)

        btn = QPushButton("打开")
        btn.setFixedHeight(26)
        btn.setMinimumWidth(56)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("用系统默认浏览器打开官网")
        btn.clicked.connect(lambda _=False: open_external_url(url, self))
        row.addWidget(btn)
        row.addStretch(1)
        return row

    def _on_nav_changed(self, row: int):
        if row >= 0:
            self._stack.setCurrentIndex(row)
