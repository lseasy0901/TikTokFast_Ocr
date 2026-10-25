# -*- coding: utf-8 -*-
"""MainWindow 的 UI 构建函数(Phase 4 拆分,自 gui/main_window.py 外迁)。

模块级函数 build_xxx(w):w 是 MainWindow。窗口保留同名薄委托方法,
测试对 window._title_bar/window._build_perf_bar 等的引用不受影响;
构建期引用窗口方法/字段(w._xxx)经 __getattr__ 转发同样有效。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QMenu,
                               QPushButton, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from automation import (CalibrationOverlay, GlobalHotkey, JoinConfig,
                        JoinController)
from gui.activation_overlay import _ActivationOverlay
from gui.audio_panel import AudioPanel
from gui.history_panel import HistoryButton
from gui.join_guide import JoinGuideBar
from gui.theme import Metrics
from gui.toast import Toast
from gui.video_widget import VideoWidget
from gui.watermark import WatermarkOverlay
from gui.widgets import IconButton, OverlayHost, TitleBar, h_line, set_property
from utils.ui_prefs import PERF_PINNED, get_pref

logger = logging.getLogger("DouyinLowLatencyViewer.gui.builders")


def _mw():
    """运行时取 gui.main_window(常量在此解析;避免循环导入)。"""
    import sys

    return sys.modules["gui.main_window"]

    # ------------------------------------------------------------------
    # 标题栏 / 顶部地址栏
    # ------------------------------------------------------------------
def build_title_bar(w) -> TitleBar:
    """标题栏：品牌 + 图标操作区 + 窗口控制。

    操作区按使用频率分两层（Phase UI 玻璃 2.0 改版，用户确认方案）：

        [激活许可证] [设置] │ [⋯]

    - **常驻层**：授权 / 设置。授权是全应用唯一被强调的入口
      （variant=primary），设置是次高频入口，两者都有理由常驻。
    - **溢出层**：全屏工作台 / 使用引导 / 联系作者都是低频入口，
      收进「⋯」菜单 —— 右上角从 5 个图标按钮降到 3 个，
      窗口按钮不再挤在一排陌生图标里。
    - 窗口按钮（最小化 / 最大化 / 关闭）仍由 TitleBar 自己贴角管理。

    历史注记：UI 液态玻璃改造时曾以「既有测试断言帮助入口在标题栏
    操作区」为由拒绝溢出菜单；本次是用户明确拍板的交互重构，
    相关断言已随本次改动同步更新（test_first_run_onboarding /
    test_phase_ui3_1_layout），不再是"为改版而破坏既有约定"。
    """
    w._title_bar = TitleBar("LiveLens", "低延迟播放工作台")

    # ---- 常驻层：授权 / 设置 ------------------------------------
    # 「授权」保持唯一被强调的入口（图标按钮上的 variant=primary）
    w._license_btn = w._title_bar.add_icon_button(
        IconButton.LICENSE, "激活许可证：输入密钥为本机授权",
        accessible_name="激活许可证")
    set_property(w._license_btn, "variant", "primary")
    w._license_btn.clicked.connect(w._on_license_clicked)

    w._settings_btn = w._title_bar.add_icon_button(
        IconButton.SETTINGS, "设置：授权状态、版本与偏好",
        accessible_name="设置")
    w._settings_btn.clicked.connect(w._on_settings_clicked)

    # ---- 分组分隔线 ----------------------------------------------
    w._title_bar.add_action_separator()

    # ---- 溢出层：低频入口收进「⋯」 -------------------------------
    w._more_btn = w._title_bar.add_icon_button(
        IconButton.MORE, "更多：全屏工作台 / 使用引导 / 联系作者",
        accessible_name="更多")
    w._more_menu = QMenu(w._more_btn)
    w._fullscreen_action = QAction("全屏工作台", w._more_menu)
    w._fullscreen_action.triggered.connect(w._toggle_fullscreen)
    w._more_menu.addAction(w._fullscreen_action)
    w._more_menu.addSeparator()
    help_action = QAction("使用引导", w._more_menu)
    help_action.triggered.connect(w._on_help_clicked)
    w._more_menu.addAction(help_action)
    contact_action = QAction("联系作者", w._more_menu)
    contact_action.triggered.connect(w._on_contact_clicked)
    w._more_menu.addAction(contact_action)
    w._more_btn.clicked.connect(w._show_more_menu)

    return w._title_bar


def build_address_bar(w) -> QFrame:
    """顶部地址栏：唯一地址输入 + 历史下拉 + 历史操作菜单 + 连接/断开。

    地址只在这一个输入框里出现（历史下拉只是把选中项写回它）。
    备注与清除是低频道的历史管理动作，收进历史下拉右侧的溢出菜单，
    让顶部右侧只剩「连接 / 断开」这一个主操作（Phase UI-3.1）。
    """
    bar = QFrame()
    bar.setObjectName("addressBar")
    bar.setFixedHeight(52)
    row = QHBoxLayout(bar)
    # 右边距 14 = 下方 OCR 面板自己的右内边距：让操作区里的控件与
    # 面板**内容**的右边缘对齐（面板本体贴窗口右缘，见 _sync_address_columns）。
    row.setContentsMargins(12, 8, 14, 8)
    # 与下方 QSplitter 的分隔条同宽 —— 上下两行用同一套列间距
    row.setSpacing(Metrics.SPLITTER_HANDLE)

    w._url_input = QLineEdit()
    w._url_input.setPlaceholderText("粘贴抖音直播间地址，例如 https://live.douyin.com/…")
    w._url_input.setClearButtonEnabled(True)
    w._url_input.returnPressed.connect(w._on_connect_clicked)
    # 地址输入框是直播地址的唯一事实来源：备注动作也跟随它的内容
    w._url_input.textChanged.connect(w._sync_note_action)
    row.addWidget(w._url_input, 1)

    # 历史入口组：「历史」主入口 +「⋯」二级操作（备注/清除）装进同一个
    # 胶囊容器 —— 两个按钮读作一个整体，而不是两颗来历不明的小方块。
    group = QFrame()
    group.setObjectName("historyGroup")
    group_lay = QHBoxLayout(group)
    group_lay.setContentsMargins(0, 0, 0, 0)
    group_lay.setSpacing(0)

    w._history_btn = HistoryButton(w._history)
    w._history_btn.setFixedWidth(64)
    w._history_btn.enterRequested.connect(w._on_history_enter)
    # 首次进入直播间 → 询问备注（判定在 HistoryService，弹窗在 history_panel）
    w._history.set_remark_callback(w._history_btn.on_remark_request)
    group_lay.addWidget(w._history_btn)

    # 历史二级入口：备注 / 清除（原先各自占一个按钮，地址栏太挤）
    w._history_menu_btn = QToolButton()
    w._history_menu_btn.setObjectName("historyMenuBtn")
    w._history_menu_btn.setText("⋯")
    w._history_menu_btn.setFixedSize(30, 32)
    w._history_menu_btn.setToolTip("历史记录操作：编辑备注 / 清除历史")
    w._history_menu_btn.setCursor(Qt.PointingHandCursor)
    w._history_menu_btn.setPopupMode(QToolButton.InstantPopup)

    menu = QMenu(w._history_menu_btn)
    w._edit_note_action = QAction("编辑备注…", w)
    w._edit_note_action.setEnabled(False)
    w._edit_note_action.triggered.connect(w._on_edit_note_clicked)
    menu.addAction(w._edit_note_action)

    menu.addSeparator()
    w._clear_recent_action = QAction("清除历史记录…", w)
    w._clear_recent_action.triggered.connect(w._on_clear_recent_clicked)
    menu.addAction(w._clear_recent_action)
    w._history_menu_btn.setMenu(menu)
    group_lay.addWidget(w._history_menu_btn)
    row.addWidget(group)

    w._connect_btn = QPushButton("连接")
    set_property(w._connect_btn, "variant", "primary")
    w._connect_btn.setToolTip("解析直播间地址并开始拉流")
    # 固定宽度：连接是主操作，宽度不随窗口变化被压缩
    w._connect_btn.setFixedWidth(96)
    w._connect_btn.clicked.connect(w._on_connect_clicked)
    row.addWidget(w._connect_btn)

    w._address_bar = bar
    return bar


def build_body(w) -> QWidget:
    """主体：预览（占满剩余空间）+ 右侧 OCR 面板（可收起）。"""
    # 面板收起状态：用户主动选择优先于窄窗口自动收起（见 _sync_narrow_layout）
    w._panel_user_choice = False
    w._panel_auto_hidden = False

    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)

    w._body_splitter = QSplitter(Qt.Horizontal)
    w._body_splitter.setObjectName("mainSplitter")
    w._body_splitter.setChildrenCollapsible(False)
    w._body_splitter.setHandleWidth(Metrics.SPLITTER_HANDLE)
    w._body_splitter.addWidget(w._build_preview())
    w._body_splitter.addWidget(w._build_ocr_panel())
    w._body_splitter.setStretchFactor(0, 1)
    w._body_splitter.setStretchFactor(1, 0)
    w._body_splitter.setSizes([900, Metrics.OCR_PANEL_WIDTH])
    row.addWidget(w._body_splitter, 1)

    # 收起后的展开入口：贴右边缘的细条（比"没有入口"诚实）
    w._expand_btn = QPushButton("«")
    w._expand_btn.setObjectName("expandBtn")
    w._expand_btn.setFixedWidth(18)
    w._expand_btn.setToolTip("展开右侧 OCR 面板")
    w._expand_btn.clicked.connect(w._on_expand_clicked)
    w._expand_btn.hide()
    row.addWidget(w._expand_btn)

    body = QWidget()
    body.setLayout(row)
    return body

    # ------------------------------------------------------------------
    # 中央：直播预览（视觉中心）
    # ------------------------------------------------------------------
def build_preview(w) -> QWidget:
    column = QWidget()
    layout = QVBoxLayout(column)
    layout.setContentsMargins(12, 12, 12, 0)
    layout.setSpacing(10)

    w._video_container = OverlayHost()
    w._video_container.setObjectName("previewFrame")
    w._video_container.setAttribute(Qt.WA_StyledBackground, True)
    video_layout = QVBoxLayout(w._video_container)
    video_layout.setContentsMargins(0, 0, 0, 0)
    video_layout.setSpacing(0)

    w._video_widget = VideoWidget()
    video_layout.addWidget(w._video_widget, 1)

    # 空状态提示：真正的浮层，不参与布局
    w._connection_status_label = QLabel("尚未连接直播")
    w._connection_status_label.setObjectName("previewOverlay")
    w._connection_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w._video_container.add_overlay(w._connection_status_label)
    w._connection_status_label.show()

    # OCR 复制成功浮层（尺寸自适应，位置在 _show_copy_feedback 里算）
    w._copy_feedback_label = QLabel("✓ 已复制")
    w._copy_feedback_label.setObjectName("copyToast")
    w._copy_feedback_label.setParent(w._video_container)
    w._copy_feedback_label.adjustSize()
    w._copy_feedback_label.hide()
    w._copy_feedback_timer = QTimer(w)
    w._copy_feedback_timer.setSingleShot(True)
    w._copy_feedback_timer.timeout.connect(w._hide_copy_feedback)

    # 未激活遮罩：与连接提示同为浮层，默认按当前授权状态决定是否显示
    w._activation_overlay = _ActivationOverlay()
    w._activation_overlay.activate_clicked.connect(w._on_license_clicked)
    w._activation_overlay.purchase_clicked.connect(
        w._on_purchase_clicked)
    # interactive=True：遮罩上有「立即激活」按钮，必须收得到点击
    # （默认的鼠标穿透会让按钮连同浮层一起不可点）。
    w._video_container.add_overlay(w._activation_overlay, interactive=True)

    # 未激活水印（Phase 5）：**注册在未激活遮罩之后** → 层级在其之上。
    #
    # 为什么必须在遮罩之上：遮罩用 rgba(11,12,14,0.88) 的 scrim 压暗整个
    # 预览区，水印若在其下就基本看不见了；而两者由**同一个** denied 布尔量
    # 驱动（遮罩可见 ⟺ 水印可见），因此"在遮罩之下"等价于"永远不可见"，
    # 水印会彻底失去提示作用。
    #
    # 会不会挡住「立即激活」按钮？不会：
    #   - 本浮层 mouse-transparent（interactive=False），点击一律穿透，
    #     按钮的可操作性完全不受影响；
    #   - 视觉上水印是半透明的（底 ~35%、描边 ~20%、文字 ~36%），
    #     且持续移动，只在经过时短暂叠压。
    # 这正是 DVD 屏保式水印在未授权软件上的常规表现。
    #
    # 水印**只消费一个布尔量**，不导入任何许可模块 —— 授权状态的权威
    # 来源仍是 w._access_status，由 _on_access_status_changed 推送。
    w._watermark = WatermarkOverlay(w._video_widget)
    w._video_container.add_overlay(w._watermark)
    w._watermark.hide()

    layout.addWidget(w._video_container, 1)
    layout.addWidget(w._build_video_toolbar())
    return column


def build_video_toolbar(w) -> QFrame:
    """视频下方工具栏：只放直播状态 / 分辨率 / FPS 三项读数。

    ROI 的两个入口（选择 / 说明）已迁到右侧 OCR 面板的「识别区域」一节：
    它们操作的是识别区域的设置，与播放读数不是同一类东西，混在视频工具条
    里既挤占视频宽度，也让"改哪个区域"离它自己的状态文字很远。
    """
    bar = QFrame()
    bar.setObjectName("toolStrip")
    bar.setFixedHeight(44)
    row = QHBoxLayout(bar)
    row.setContentsMargins(12, 0, 12, 0)
    row.setSpacing(14)

    w._lbl_status = w._make_chip()
    w._lbl_status.setText("未连接")
    w._lbl_resolution = w._make_chip()
    w._lbl_resolution.setText("分辨率 --")
    w._lbl_fps = w._make_chip()
    w._lbl_fps.setText("FPS --")
    # 延迟芯片：低延迟是本产品的核心卖点，读数（含 ok/warn/error 变色）
    # 由 _on_stats 更新。历史上它在 _build_perf_bar 里创建却从未被加入
    # 任何布局（死对象）—— 本次改版把它放到它该在的位置：FPS 旁边。
    w._lbl_latency = w._make_chip()
    w._lbl_latency.setText("延迟 --")
    for chip in (w._lbl_status, w._lbl_resolution,
                 w._lbl_fps, w._lbl_latency):
        row.addWidget(chip)

    # 左读数、右控制：状态（未连接/分辨率/FPS/延迟）贴左，
    # 音频控制（音量/模式/状态）贴右，中间留白断开两类语义。
    row.addStretch(1)

    # 音频控制条：音量 / 模式 / 状态（转发给 AudioService，无音频逻辑在此）
    w._audio_panel = AudioPanel()
    w._audio_panel.volumeChanged.connect(w._audio.set_volume)
    w._audio_panel.modeChanged.connect(w._audio.set_mode)
    w._audio.set_state_callback(w._audio_panel.on_service_state)
    row.addWidget(w._audio_panel)
    return bar

    # ------------------------------------------------------------------
    # 右侧面板：当前房间码 → 本次会话 → 游戏 / ROI → OCR 按钮（固定底部）
    # ------------------------------------------------------------------
def build_ocr_panel(w) -> QFrame:
    panel = QFrame()
    panel.setObjectName("ocrPanel")
    panel.setMinimumWidth(Metrics.OCR_PANEL_WIDTH_MIN)
    panel.setMaximumWidth(Metrics.OCR_PANEL_WIDTH_MAX)
    w._ocr_panel = panel

    col = QVBoxLayout(panel)
    col.setContentsMargins(14, 12, 14, 12)
    col.setSpacing(8)

    head = QHBoxLayout()
    head.addWidget(w._section_label("OCR 控制"))
    head.addStretch(1)
    w._collapse_btn = QPushButton("»")
    w._collapse_btn.setObjectName("collapseBtn")
    w._collapse_btn.setFixedWidth(26)
    w._collapse_btn.setToolTip("收起右侧面板（窄窗口优先保留视频）")
    w._collapse_btn.clicked.connect(w._on_collapse_clicked)
    head.addWidget(w._collapse_btn)
    col.addLayout(head)

    # ---- 1) 当前房间码（视觉优先级最高）----
    col.addWidget(w._section_label("当前房间码"))
    w._lbl_ocr_text = QLabel(_mw().OCR_RESULT_PLACEHOLDER)
    w._lbl_ocr_text.setObjectName("resultBox")
    w._lbl_ocr_text.setProperty("filled", "false")
    w._lbl_ocr_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w._lbl_ocr_text.setMinimumHeight(72)
    w._lbl_ocr_text.setWordWrap(True)
    col.addWidget(w._lbl_ocr_text)

    state_row = QHBoxLayout()
    state_row.setSpacing(12)
    w._ocr_state_chip = w._make_chip()
    w._lbl_clipboard = w._make_chip()
    state_row.addWidget(w._ocr_state_chip)
    state_row.addWidget(w._lbl_clipboard)
    state_row.addStretch(1)
    col.addLayout(state_row)

    col.addWidget(h_line())

    # ---- 2) 本次会话识别历史 ----
    # 列表是可伸缩的那一块：面板里真正需要"余量"的只有历史，
    # 让它吃掉剩余高度，中段就不会再出现一条死的空白带（Phase UI-3.1）。
    #
    # 标题行用 QWidget 容器承载（与下方「识别区域」同样的做法）：
    # 一个只被 Python 局部变量持有的无父 QHBoxLayout 会让解释器在 GC
    # 阶段踩坏堆，详见「识别区域」那段的说明。
    session_head = QWidget()
    session_head_row = QHBoxLayout(session_head)
    session_head_row.setContentsMargins(0, 0, 0, 0)
    session_head_row.setSpacing(6)
    session_head_row.addWidget(w._section_label("本次会话"))
    session_head_row.addStretch(1)

    # 「导出 CSV」：导出的是上面这份显示历史**背后的**流水 ——
    # 每一次成功复制各一条（含重复复制的同一房间码），不受去重与
    # 50 行上限影响，因此 tooltip 必须说清楚，别让人以为导出的就是所见列表。
    w._export_csv_btn = QPushButton("导出 CSV", session_head)
    w._export_csv_btn.setObjectName("sectionAction")
    w._export_csv_btn.setCursor(Qt.PointingHandCursor)
    w._export_csv_btn.setFocusPolicy(Qt.NoFocus)
    w._export_csv_btn.setToolTip(
        "导出本次运行中每一次成功复制的房间码（同一房间码重复复制也各记一条）")
    w._export_csv_btn.clicked.connect(w._on_export_csv_clicked)
    session_head_row.addWidget(w._export_csv_btn)
    col.addWidget(session_head)

    # 诊断开关与导出（默认关闭）。与上面的「导出 CSV」同一套 #sectionAction
    # 小按钮样式，不再引入新的控件样式；两者都只动日志，不动识别。
    diag_row = QWidget()
    diag_row_layout = QHBoxLayout(diag_row)
    diag_row_layout.setContentsMargins(0, 0, 0, 0)
    diag_row_layout.setSpacing(6)

    w._diag_btn = QPushButton("开始诊断", diag_row)
    w._diag_btn.setObjectName("sectionAction")
    w._diag_btn.setCursor(Qt.PointingHandCursor)
    w._diag_btn.setFocusPolicy(Qt.NoFocus)
    w._diag_btn.setToolTip(
        "开启后记录每一次 OCR 执行的诊断信息（各 PSM 原始读法、候选、裁决、"
        "确认与复制结果）；只记录，不改变识别行为。默认关闭，记录只留在内存里。")
    w._diag_btn.clicked.connect(w._on_diag_toggled)
    diag_row_layout.addWidget(w._diag_btn)

    w._diag_export_btn = QPushButton("导出诊断 CSV", diag_row)
    w._diag_export_btn.setObjectName("sectionAction")
    w._diag_export_btn.setCursor(Qt.PointingHandCursor)
    w._diag_export_btn.setFocusPolicy(Qt.NoFocus)
    w._diag_export_btn.setToolTip("把诊断记录导出为 CSV（与房间码记录 CSV 分开的两份文件）")
    w._diag_export_btn.clicked.connect(w._on_export_diagnostics_clicked)
    diag_row_layout.addWidget(w._diag_export_btn)
    diag_row_layout.addStretch(1)
    col.addWidget(diag_row)

    # 「全 PSM 观测」（默认不勾选）：只有诊断开启时才可勾选，识别运行期间
    # 锁定。勾上后，本来会提前停止的那一帧会继续跑完 PSM 7/11，把它们的原始
    # 读法也写进诊断 CSV；补跑结果【只进诊断】，不参与仲裁 / 游戏冲突判定 /
    # 时序确认 / 剪贴板，因此复制结果与不勾选时完全一致。
    obs_row = QWidget()
    obs_row_layout = QHBoxLayout(obs_row)
    obs_row_layout.setContentsMargins(0, 0, 0, 0)
    obs_row_layout.setSpacing(6)

    w._diag_obs_check = QCheckBox("全 PSM 观测", obs_row)
    w._diag_obs_check.setCursor(Qt.PointingHandCursor)
    w._diag_obs_check.setFocusPolicy(Qt.NoFocus)
    w._diag_obs_check.setToolTip(
        "连同 PSM 7/11 的读法一起记进诊断 CSV（默认不勾选）。\n"
        "需要先点「开始诊断」；识别运行期间不可更改，停止后恢复。\n"
        "补跑的读法只用于诊断，不参与仲裁、游戏冲突判定、连续确认与复制。")
    w._diag_obs_check.toggled.connect(w._on_diag_observe_toggled)
    obs_row_layout.addWidget(w._diag_obs_check)
    obs_row_layout.addStretch(1)
    col.addWidget(obs_row)

    # 「采集 ROI 样本」（默认不勾选）：把识别器真正看到的那一帧像素存到
    # 文档目录，供离线基准用真实引擎重跑。与诊断开关相互独立 ——
    # 采集只落盘、不判定，也不依赖诊断。
    cap_row = QWidget()
    cap_row_layout = QHBoxLayout(cap_row)
    cap_row_layout.setContentsMargins(0, 0, 0, 0)
    cap_row_layout.setSpacing(6)

    w._diag_capture_check = QCheckBox("采集 ROI 样本", cap_row)
    w._diag_capture_check.setCursor(Qt.PointingHandCursor)
    w._diag_capture_check.setFocusPolicy(Qt.NoFocus)
    w._diag_capture_check.setToolTip(
        "把识别器看到的那一帧 ROI 原图存到「文档/LiveLens-OCR样本」（默认不勾选）。\n"
        "供离线准确率基准用真实 OCR 引擎重跑；有数量与磁盘上限，采满即停。\n"
        "只落盘，不参与识别判定，也不影响复制结果。")
    w._diag_capture_check.toggled.connect(w._on_sample_capture_toggled)
    cap_row_layout.addWidget(w._diag_capture_check)
    cap_row_layout.addStretch(1)
    col.addWidget(cap_row)

    # 内部诊断入口的可见性：由模块顶部的单一开关决定，默认不显示。
    # 三个行控件已经 addWidget 进 col 并被 col 持有，这里只改可见性 ——
    # 控件对象、信号、实现全部保留（开关置 True 即完整恢复）。
    # 隐藏控件在 QBoxLayout 里是空项：不占高度、不产生行间距，所以关闭后
    # 不会留下空白带，也不会出现"不可见但仍占位"的控件。
    for row in (diag_row, obs_row, cap_row):
        row.setVisible(_mw().SHOW_INTERNAL_DIAGNOSTICS)

    # ---- 数据集采集入口（测试用，可一键关闭）----
    # 与上面那三个"内部诊断"入口分开受控：它由 ocr/dataset_capture.py 的
    # 总开关 _mw().DATASET_CAPTURE_ENABLED 决定，**不受** _mw().SHOW_INTERNAL_DIAGNOSTICS
    # 影响（后者是生产 UI 收口，默认关；本入口按需求默认开）。
    # 关闭时这个 QWidget 连创建都不创建，界面上不留任何痕迹。
    if _mw().DATASET_CAPTURE_ENABLED:
        ds_row = QWidget()
        ds_row_layout = QHBoxLayout(ds_row)
        ds_row_layout.setContentsMargins(0, 0, 0, 0)
        ds_row_layout.setSpacing(6)

        w._dataset_btn = QPushButton("数据集采集", ds_row)
        w._dataset_btn.setObjectName("sectionAction")
        w._dataset_btn.setCursor(Qt.PointingHandCursor)
        w._dataset_btn.setFocusPolicy(Qt.NoFocus)
        w._dataset_btn.setToolTip(
            "把识别器看到的那一帧 ROI 原图 + PP-OCRv6 原始读法存成数据集。\n"
            "复用当前直播流与当前 OCR 结果：不重开流、不重跑识别、不改识别与复制。\n"
            "只收集符合 AAA123 / BBB123 / AAAA1234 三种格式的读法，\n"
            "其余（空结果/普通文本/长度或字符不符）进 rejected.csv，不做任何纠正。")
        w._dataset_btn.clicked.connect(w._on_dataset_capture_toggled)
        ds_row_layout.addWidget(w._dataset_btn)
        ds_row_layout.addStretch(1)
        col.addWidget(ds_row)

        w._dataset_status = QLabel("", ds_row)
        w._dataset_status.setObjectName("captionLabel")
        w._dataset_status.setWordWrap(True)
        col.addWidget(w._dataset_status)

    # 初始状态：诊断默认关闭 -> 观测不可勾选
    w._update_diag_controls()

    w._session_list = QListWidget()
    w._session_list.setObjectName("sessionList")
    w._session_list.setFrameShape(QFrame.NoFrame)
    w._session_list.setFocusPolicy(Qt.NoFocus)
    w._session_list.setMinimumHeight(64)
    w._session_list.setToolTip("本次会话中【复制成功】的房间号与时间（最新在上）")
    w._session_list.setSelectionMode(QListWidget.NoSelection)
    col.addWidget(w._session_list, 1)
    w._session_placeholder = False
    w._show_session_placeholder()

    col.addWidget(h_line())

    # ---- 3) 游戏与 ROI ----
    col.addWidget(w._section_label("游戏"))
    w._ocr_game_combo = QComboBox()
    for profile_id, label in _mw().OCR_GAME_CHOICES:
        w._ocr_game_combo.addItem(label, profile_id)
    w._ocr_game_combo.setCurrentIndex(
        w._ocr_game_combo.findData(w._ocr_profile_id)
    )
    w._ocr_game_combo.setToolTip("决定房间号按哪款游戏的格式识别")
    w._ocr_game_combo.currentIndexChanged.connect(w._on_ocr_game_changed)
    col.addWidget(w._ocr_game_combo)


    # 识别区域：标题行右侧是窄「说明」入口，状态文字下方是「选择识别区域」。
    # 两个控件都是**原实例**（同一个 _roi_help_btn / _roi_btn），
    # 只是从视频工具条搬到了这里 —— 信号与行为完全没变。
    #
    # 标题行用 QWidget 容器承载，而不是把裸的 QHBoxLayout 直接 addLayout
    # 进 col：一个只被 Python 局部变量持有的无父 QHBoxLayout，在
    # test_phase_b2_frame_ready.py 这种「反复建/弃主窗口」的用例里会让
    # 解释器在 GC 阶段踩坏堆（0xC0000374）。容器 QWidget 自带布局，
    # 布局从构造起就有 C++ 父对象，生命周期不再依赖 Python 引用。
    header = QWidget()
    header_row = QHBoxLayout(header)
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(6)
    header_row.addWidget(w._section_label("识别区域"))
    header_row.addStretch(1)

    w._roi_help_btn = QPushButton("说明", header)
    w._roi_help_btn.setObjectName("sectionAction")
    # 高度由 #sectionAction 的 QSS 钉死（QSS 优先级高于 setFixedHeight）
    w._roi_help_btn.setCursor(Qt.PointingHandCursor)
    w._roi_help_btn.setFocusPolicy(Qt.NoFocus)
    w._roi_help_btn.setToolTip("怎样框选识别区域（含正确 / 错误对比示例）")
    w._roi_help_btn.clicked.connect(w._on_roi_help_clicked)
    header_row.addWidget(w._roi_help_btn)
    col.addWidget(header)

    w._lbl_roi = QLabel("未选择")
    w._lbl_roi.setObjectName("valueLabel")
    w._lbl_roi.setWordWrap(True)
    col.addWidget(w._lbl_roi)

    w._roi_btn = QPushButton("选择识别区域")
    w._roi_btn.setEnabled(False)
    w._roi_btn.setToolTip("在直播画面上框选房间号所在区域")
    w._roi_btn.clicked.connect(w._on_roi_select_clicked)
    col.addWidget(w._roi_btn)

    # 这里只留最短的一句操作键序；完整说明在标题右侧的「说明」浮层里，
    # 避免同一段提示在面板、tooltip、帮助浮层三处各说一遍。
    roi_row = QHBoxLayout()
    roi_row.setSpacing(6)
    w._roi_note = QLabel("拖动框选 → Enter 确认 / Esc 取消")
    w._roi_note.setObjectName("captionLabel")
    w._roi_note.setWordWrap(True)
    roi_row.addWidget(w._roi_note, 1)
    col.addLayout(roi_row)

    # ---- 4) OCR 启停固定在面板底部 ----
    w._ocr_btn = QPushButton("开始OCR")
    w._ocr_btn.setEnabled(False)
    set_property(w._ocr_btn, "variant", "primary")
    w._ocr_btn.setToolTip("开始 / 停止识别所选区域")
    w._ocr_btn.setFixedHeight(36)
    w._ocr_btn.clicked.connect(w._on_ocr_clicked)
    col.addWidget(w._ocr_btn)
    return panel

    # ------------------------------------------------------------------
    # 底部：分组性能栏（挂在状态栏永久区，左侧保留瞬时消息）
    # ------------------------------------------------------------------
def build_perf_bar(w) -> None:
    """底部状态栏：常驻用户状态，性能指标默认收进「诊断」弹层。

    常驻：OCR 运行状态芯片、剪贴板反馈芯片、授权状态、
          「诊断」入口（点开才是完整指标）。
    诊断：链路 / 解码 / 渲染 / OCR 四组 10 项指标（set_frame p50/p95、
          paint、管道积压等是开发者数据），默认装在弹层里，
          设置「偏好」里可改回常驻（utils.ui_prefs，键 perf_pinned）。

    指标容器（_perf_bar_widget）只有一份，常驻/弹层两种模式之间
    用 addWidget 换父级搬运，数据照常采集、_set_perf 一如既往写标签。
    """
    bar = w.statusBar()
    bar.setSizeGripEnabled(False)

    w._lbl_ocr = w._make_chip()
    w._perf_values = {}
    w._perf_defaults = {}

    # 分组：(组名, [(键, 初始文本, tooltip)])；"已有指标"才在这里出现
    groups = (
        ("链路", (("latency", "延迟 —", "帧到达缓冲 → GUI 取走的等待"),
                  ("first", "首帧 —", "从 start() 到首帧"),
                  ("skip", "跳帧 —", "累计跳帧 / 当前速率"),
                  ("pipe", "管道 —", "FFmpeg rawvideo 管道积压"))),
        # 组名已经是「解码」，条目名不再重复写成「解码」——
        # 否则读起来是「解码 解码 —」，像同一件事写了两遍。
        ("解码", (("decode_fps", "输出 —", "FFmpeg 输出帧率"),
                  ("input_fps", "输入 —", "最新帧交给 GUI 的帧率"))),
        ("渲染", (("set_frame", "set_frame —", "set_frame p50/p95"),
                  ("paint", "paint —", "paintEvent p50/p95"))),
        # OCR 耗时当前没有测量源（E2E 探针已按计划移除）→ 如实显示 "—"
        ("OCR", (("ocr_cost", "耗时 —", "OCR 单次耗时：当前无测量源"),
                 ("ocr_frames", "帧数 —", "本次会话识别次数"))),
    )
    # 四个组容器保存引用：常驻(单行+分隔线)与弹层(2×2 网格)两种
    # 布局共用同一批组控件，切换时由 _relayout_perf 重排。
    w._perf_groups = [w._perf_group(name, items)
                         for name, items in groups]
    w._perf_bar_widget = QWidget()
    w._perf_bar_widget.hide()

    # 诊断弹层（Qt.Popup：点击外部自动关闭）；指标容器平时住在这里
    w._perf_popup = QFrame(w, Qt.Popup | Qt.FramelessWindowHint)
    w._perf_popup.setAttribute(Qt.WA_TranslucentBackground)
    w._perf_popup.setObjectName("perfPopup")
    popup_lay = QVBoxLayout(w._perf_popup)
    popup_lay.setContentsMargins(1, 1, 1, 1)
    popup_lay.setSpacing(2)
    title = QLabel("诊断 · 性能指标")
    title.setObjectName("perfPopupTitle")
    popup_lay.addWidget(title)
    popup_lay.addWidget(w._perf_bar_widget)
    w._perf_popup.hide()
    w._apply_perf_popup_qss()

    # 「诊断」入口：一枚低调的状态栏按钮（不与授权/OCR 状态抢注意力）
    w._perf_toggle_btn = QPushButton("诊断")
    w._perf_toggle_btn.setObjectName("statusButton")
    w._perf_toggle_btn.setCursor(Qt.PointingHandCursor)
    w._perf_toggle_btn.setToolTip("性能指标：延迟 / 帧率 / 渲染耗时 / OCR")
    w._perf_toggle_btn.clicked.connect(w._toggle_perf_popup)
    bar.addPermanentWidget(w._lbl_ocr)
    bar.addPermanentWidget(w._lbl_clipboard)
    w._access_status_label = QLabel()
    w._access_status_label.setObjectName("accessLabel")
    w._access_status_label.setProperty("tone", "neutral")
    bar.addPermanentWidget(w._access_status_label)
    bar.addPermanentWidget(w._perf_toggle_btn)

    # 剪贴板芯片的 2 秒回落定时器（由 _on_copy_requested 启动）
    w._clipboard_chip_timer = QTimer(w)
    w._clipboard_chip_timer.setSingleShot(True)
    w._clipboard_chip_timer.timeout.connect(w._reset_clipboard_chip)
    w._reset_clipboard_chip()

    # 统一 toast 通知（右下角浮出；连接成败 / 复制 / 导出等共用）
    w._toast = Toast(w)
    w._set_perf_pinned(bool(get_pref(PERF_PINNED, False)), persist=False)

    # ---- 自动加入(automation):配置/热键/执行器/校准 ----------------
    w._pending_join_code: str | None = None
    w._automation_cfg = JoinConfig.load()
    w._join_hotkey = GlobalHotkey(w)
    w._join_hotkey.activated.connect(w._on_join_hotkey)
    w._join_controller = JoinController(w)
    w._join_controller.finished.connect(w._on_join_finished)
    w._calibration = CalibrationOverlay(w)
    w._calibration.stepChanged.connect(w._on_cal_step)
    w._calibration.invalidClick.connect(w._on_cal_invalid)
    w._calibration.finished.connect(w._on_cal_finished)
    w._join_guide = JoinGuideBar()
    w._join_guide.close()
    w._sync_join_hotkey()
    # 应用整体失活(用户切到别的软件)也挂起呼吸动画:窗口还在但没人在
    # 看,60fps 透明度重绘纯烧 CPU;配合最小化暂停,把常驻占用压到零。
    QGuiApplication.instance().applicationStateChanged.connect(
        w._on_app_state_changed)
    # 测试钩子(与 LIVELENS_E2E_UPDATE 同一先例):显式设置环境变量
    # 或项目根下存在 _test_pending_code.txt,即预置一个待加入码,
    # 便于不连直播验证 F8 序列。仅测试用途。
    _fake = os.environ.get("DLV_TEST_PENDING_CODE", "").strip()
    _fake_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_test_pending_code.txt")
    if not _fake and os.path.isfile(_fake_file):
        try:
            with open(_fake_file, encoding="utf-8-sig") as fh:
                _fake = fh.read().strip()
        except OSError:
            pass
    if _fake:
        w._pending_join_code = _fake
        logger.info("[JOIN] 测试钩子:预置待加入码 %r", _fake)


def build_title_hotzone(w, parent: QWidget) -> None:
    """全屏时用于"鼠标到顶部滑出标题栏"的细热区（非全屏不启用）。"""
    w._title_hot = QFrame(parent)
    w._title_hot.setFixedHeight(4)
    w._title_hot.setStyleSheet("background: transparent; border: none;")
    w._title_hot.hide()
    w._title_hide_timer = QTimer(w)
    w._title_hide_timer.setSingleShot(True)
    w._title_hide_timer.setInterval(1000)
    w._title_hide_timer.timeout.connect(w._maybe_autohide_title)

