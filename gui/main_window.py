# -*- coding: utf-8 -*-
"""
主窗口 (Phase 1 - Step 6)

功能：
    - 抖音直播间 URL 输入
    - 连接 / 断开 按钮
    - 视频显示区域（VideoWidget）
    - 性能信息面板（FPS / Latency / Resolution / Status）
    - 状态栏信息
    - QTimer 轮询 LatestFrameBuffer 刷新显示

数据流：
    FFmpegReader 读取线程
          │
          ↓  reader.read_frame()
    _FrameBridge 线程
          │
          ↓  buffer.update(frame)  +  perf.on_capture()
    LatestFrameBuffer
          │
          ↓  buffer.get()
    MainWindow QTimer (30ms)
          │
          ↓  video_widget.set_frame(frame)  +  perf.on_display()
    VideoWidget

约束：
    - GUI 线程只负责界面，网络/读取均在后台线程
    - 不使用 while True 阻塞 GUI
    - 错误显示在状态栏，不直接崩溃
    - LatestFrameBuffer 为 FFmpeg 与 GUI 之间的唯一数据通道
"""

import logging
import threading
import time
from datetime import date, datetime, timezone

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.theme import Metrics
from gui.video_widget import VideoWidget
from gui.widgets import (
    DockPanel,
    FramelessController,
    OverlayHost,
    TitleBar,
    WindowButton,
    chip_html,
    enable_mouse_tracking,
    h_line,
    set_property,
    set_tone,
)
from gui.expired_dialog import ExpiredDialog
from gui.activation_dialog import ActivationDialog, LicenseWorker
from utils.heartbeat_schedule import HeartbeatScheduler
from utils.license_manager import LicenseManager, LicenseResult, LicenseStatus
from stream.douyin import DouyinStream, DouyinStreamError
from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor
from utils.access_status import AccessStatus
from ocr.roi_manager import ROI, ROIManager
from ocr.roi_selector import ROISelector
from ocr.worker import OCRWorker
from ocr.clipboard import ClipboardManager
from ocr.profiles import DELTA_FORCE, VALORANT, get_profile
from utils.stream_history import StreamHistory

logger = logging.getLogger("DouyinLowLatencyViewer.gui.main_window")


# ======================================================================
# 后台连接线程：执行 DouyinStream 网络请求，不阻塞 GUI
# ======================================================================
class _ConnectWorker:
    """后台线程：解析直播间 URL 获取真实流地址"""

    def __init__(self, live_url: str, on_success, on_error):
        """
        :param live_url:   直播间 URL
        :param on_success: 成功回调，签名为 callback(result: dict)
        :param on_error:   失败回调，签名为 callback(error_msg: str)
        """
        self._live_url = live_url
        self._on_success = on_success
        self._on_error = on_error
        self._thread = threading.Thread(
            target=self._run,
            name="connect-worker",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self):
        try:
            ds = DouyinStream(self._live_url)
            result = ds.get_stream_url()
            logger.info("[GUI] stream_url received: type=%s url=%s",
                        result.get("stream_type"), result.get("stream_url", "")[:80])
            self._on_success(result)
        except DouyinStreamError as e:
            logger.error("[GUI] DouyinStream failed: %s", e)
            self._on_error(str(e))
        except Exception as e:
            logger.exception("连接过程未知异常")
            self._on_error(f"连接异常: {e}")


# ======================================================================
# 帧桥接线程：FFmpegReader → LatestFrameBuffer
# ======================================================================
class _FrameBridge:
    """
    后台线程：持续从 FFmpegReader 读取最新帧，写入 LatestFrameBuffer

    运行频率 1ms 轮询，保证 buffer 始终持有最新帧，
    即使 GUI QTimer (30ms) 采样间隔内有多个新帧，buffer 只保留最新的，
    天然实现"宁可丢帧，不要累计延迟"。

    每次写入 buffer 后调用 perf.on_capture() 记录采集时间戳，
    用于后续计算 capture → display 延迟。
    """

    def __init__(
        self,
        reader: FFmpegReader,
        buffer: LatestFrameBuffer,
        perf: PerformanceMonitor,
    ):
        self._reader = reader
        self._buffer = buffer
        self._perf = perf
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_reader_frame_id: int = -1

    def start(self):
        """启动桥接线程"""
        self._running = True
        self._last_reader_frame_id = -1
        self._thread = threading.Thread(
            target=self._run,
            name="frame-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("FrameBridge 已启动")

    def stop(self):
        """停止桥接线程"""
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        logger.info("FrameBridge 已停止")

    def _run(self):
        """桥接循环：reader → buffer + perf.on_capture()"""
        _log_count = 0
        while self._running:
            current_id = self._reader.frame_id
            if current_id != self._last_reader_frame_id and current_id > 0:
                frame = self._reader.read_frame()
                if frame is not None:
                    self._buffer.update(frame)
                    # 记录采集时间戳（在 buffer 写入后立即记录）
                    self._perf.on_capture()
                    self._last_reader_frame_id = current_id
                    _log_count += 1
                    if _log_count <= 3 or _log_count % 100 == 0:
                        logger.info(
                            "[BRIDGE] frame %d -> buffer: shape=%s dtype=%s",
                            _log_count, frame.shape, frame.dtype,
                        )

            time.sleep(0.001)


# ======================================================================
# 主窗口
# ======================================================================
#: 游戏选择器里的可选项：(profile_id, 界面短名)。
#: 只暴露具体游戏——"auto" 仅作为内部默认档保留，不出现在 UI 上，
#: 因为用户显式选择某一款游戏才能消除 6 位/7 位的长度歧义。
OCR_GAME_CHOICES = (
    (DELTA_FORCE.profile_id, "三角洲行动"),
    (VALORANT.profile_id, "无畏契约"),
)

#: profile_id → 界面短名（左栏场景列表与右栏下拉框共用同一份文案）
GAME_LABELS = dict(OCR_GAME_CHOICES)

#: 默认游戏档（与 OCR_GAME_CHOICES[0] 一致）
DEFAULT_OCR_GAME_PROFILE_ID = DELTA_FORCE.profile_id

#: OCR 检测到「游戏选错了」时给用户看的固定文案。
#: 只说明需要重新选择，不含任何技术细节（不出现位数、格式名、档位名，
#: 不暗示程序认为应该是哪款游戏），也不附加任何操作入口。
OCR_GAME_MISMATCH_MESSAGE = "检测到选择错误，请重新选择游戏类型后再进行识别。"


class MainWindow(QMainWindow):
    """Douyin Low Latency Stream Viewer 主窗口"""

    # 跨线程安全 Signal：后台线程 → GUI 线程
    _connect_success_sig = Signal(dict)
    _connect_error_sig = Signal(str)
    _license_loaded_sig = Signal(object)  # 启动时许可证校验结果
    _heartbeat_done_sig = Signal(object)  # 心跳上报结果

    def __init__(self):
        super().__init__()

        # 无边框 + 自定义标题栏（视觉与交互对齐专业工具；边角缩放由
        # FramelessController 在 _init_ui 里补上）
        self.setWindowTitle("LiveLens · 抖音低延迟播放器")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMinimumSize(Metrics.WINDOW_MIN_WIDTH, Metrics.WINDOW_MIN_HEIGHT)
        self.resize(1400, 860)

        # 内部状态
        self._reader: FFmpegReader | None = None
        self._frame_buffer: LatestFrameBuffer | None = None
        self._frame_bridge: _FrameBridge | None = None
        self._connect_worker: _ConnectWorker | None = None
        self._perf: PerformanceMonitor | None = None
        self._connected: bool = False
        self._roi_manager: ROIManager = ROIManager()
        self._roi_selector: ROISelector | None = None
        self._ocr_worker: OCRWorker | None = None
        self._ocr_running: bool = False
        # 当前选择的游戏档（OCRWorker 构造/切换时使用）
        self._ocr_profile_id: str = DEFAULT_OCR_GAME_PROFILE_ID
        self._clipboard_manager: ClipboardManager = ClipboardManager()
        self._stream_history: StreamHistory = StreamHistory()

        # 当前选中的URL（用于备注编辑）
        self._selected_url_for_note: str | None = None

        # 帧轮询定时器（GUI 线程，从 buffer 取帧）
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._on_frame_timeout)

        # 状态刷新定时器（低频更新状态栏 + 性能面板）
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._on_status_timeout)

        # 访问状态管理器
        self._access_status = AccessStatus()

        # 访问状态专用定时器（Phase 7.2-6.6）
        # 低频重新评估访问状态，用于发现「应用运行中到期」。
        # 刻意不复用 _status_timer：后者随连接启停、周期 1s，属于视频/性能面板，
        # 且会在断开连接时被停止，不适合承担许可证到期检测。
        self._access_timer = QTimer(self)
        self._access_timer.timeout.connect(self._on_access_timer_timeout)
        self._access_status.set_update_timer(self._access_timer)
        self._access_status.set_callbacks(self._on_access_status_changed, self._on_access_expired)

        # 许可证管理器（Phase 7.2-6.5）：激活 / 本地验证 / 启动恢复
        self._license_manager = LicenseManager()
        self._license_load_worker: LicenseWorker | None = None
        self._license_loaded_sig.connect(self._on_license_loaded)

        # 启动心跳（Phase 7.4，DAU 统计）
        # 与授权状态完全解耦：只上报「本机启动过」，任何失败都静默。
        # 刻意新建独立定时器，不复用 _access_timer —— 后者承担的是许可证到期
        # 重评估，语义不同（理由与 :274-276 对 _status_timer 的说明一致）。
        self._heartbeat_worker: LicenseWorker | None = None
        self._heartbeat_done_sig.connect(self._on_heartbeat_done)
        # 调度器是纯本地状态机（不联网、无副作用），构造时建立是安全的；
        # 真正发起网络请求的是 start_heartbeat()，由应用入口 main.py 调用。
        self._heartbeat_scheduler = HeartbeatScheduler()
        self._heartbeat_timer = QTimer(self)
        # 单次触发：每次到点后重新计算下一个业务日边界，因此不会累积 QTimer 漂移。
        self._heartbeat_timer.setSingleShot(True)
        self._heartbeat_timer.timeout.connect(self._on_heartbeat_timer_timeout)

        # 上一次显示的 buffer frame_id
        self._last_displayed_frame_id: int = 0

        # 连接跨线程 Signal（后台线程 emit → GUI 线程 slot）
        self._connect_success_sig.connect(self._handle_connect_success)
        self._connect_error_sig.connect(self._handle_connect_error)

        self._init_ui()

        # 加载历史记录
        self._load_recent_streams()

        # 更新初始访问状态
        self._on_access_status_changed()

        # 启动时恢复已保存的许可证（读盘 + 本地验签在后台线程完成）
        self._start_license_load()

        # 注意：启动心跳刻意不在这里发起，由应用入口 main.py 显式调用
        # start_heartbeat()。原因见该方法自己的说明。

        # 显示连接状态消息
        self._connection_status_label.show()

        # 开始访问状态倒计时
        self._access_status.start_countdown()

        # Update overlay position after UI is built
        self._update_connection_overlay_position()

    def _update_connection_overlay_position(self):
        """Update connection status overlay position"""
        if hasattr(self, '_connection_status_label') and self._connection_status_label:
            video_container = self._connection_status_label.parent()
            if video_container:
                self._connection_status_label.setGeometry(0, 0, video_container.width(), video_container.height())

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _init_ui(self):
        """构建主界面（OBS 式三栏 Dock 布局）。

        排版意图：

            ┌────────────────────────────────────────────────┐
            │ LiveLens                           —  □  ×     │  标题栏
            ├────────────┬──────────────────────┬────────────┤
            │ 配置 / 场景 │                      │  OCR 控制  │
            │ 直播源      │      直播预览         │            │
            ├────────────┴──────────────────────┴────────────┤
            │ 状态栏：● LIVE  延迟  FPS  OCR  剪贴板         │
            └────────────────────────────────────────────────┘

        中央预览吃掉全部剩余空间（stretch=1），两侧 Dock 宽度可拖动。
        所有面板都只是既有控件的重新归位，不引入新的产品功能。
        """
        # 主题装在 QApplication 上：QDialog / QMessageBox 是独立顶层窗口，
        # 只有应用级样式表才覆盖得到。
        theme.apply_theme()

        self._syncing_games = True  # 构建期间不响应列表/下拉框联动

        root = QFrame()
        root.setObjectName("windowRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---- 自定义标题栏（无边框窗口）----
        self._title_bar = TitleBar("LiveLens", "抖音低延迟播放器")
        root_layout.addWidget(self._title_bar)

        # ---- 三栏主体 ----
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setObjectName("mainSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(Metrics.SPLITTER_HANDLE)

        left_dock = self._build_config_dock()
        preview = self._build_preview_panel()
        right_dock = self._build_ocr_dock()

        for panel in (left_dock, preview, right_dock):
            self._splitter.addWidget(panel)

        left_dock.setMinimumWidth(Metrics.DOCK_MIN_WIDTH)
        left_dock.setMaximumWidth(Metrics.DOCK_MAX_WIDTH)
        right_dock.setMinimumWidth(Metrics.DOCK_MIN_WIDTH)
        right_dock.setMaximumWidth(Metrics.DOCK_MAX_WIDTH)
        preview.setMinimumWidth(420)

        # 只有中央预览随窗口拉伸，两侧 Dock 保持固定宽度
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._splitter.setSizes(
            [Metrics.DOCK_WIDTH_LEFT, 900, Metrics.DOCK_WIDTH_RIGHT]
        )

        root_layout.addWidget(self._splitter, 1)
        self.setCentralWidget(root)

        # ---- 底部状态栏 ----
        self._build_status_bar()

        # ---- 无边框窗口：拖动 + 边角缩放 ----
        self._frameless = FramelessController(self)
        enable_mouse_tracking(root)

        self._title_bar.min_button.clicked.connect(self.showMinimized)
        self._title_bar.max_button.clicked.connect(self._toggle_maximized)
        self._title_bar.close_button.clicked.connect(self.close)

        # 场景列表与下拉框对齐到同一个当前档
        self._syncing_games = False
        self._sync_game_views(self._ocr_profile_id)

        self.statusBar().showMessage("就绪")

    # ------------------------------------------------------------------
    # 左侧 Dock：配置 / 场景 + 直播源
    # ------------------------------------------------------------------
    def _build_config_dock(self) -> QWidget:
        """左栏 = OBS 的「场景 + 来源」：游戏配置在上，直播源在下。"""
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.SPLITTER_HANDLE)

        layout.addWidget(self._build_scene_panel())
        layout.addWidget(self._build_source_panel(), 1)
        return column

    def _build_scene_panel(self) -> DockPanel:
        """配置 / 场景：两个游戏档的可选列表（与右侧下拉框同一选择）。"""
        panel = DockPanel("配置 / 场景")
        body = panel.body

        marker = QLabel()
        marker.setObjectName("captionLabel")
        marker.setTextFormat(Qt.RichText)
        marker.setText(
            '<span style="color:%s;">●</span>&nbsp;当前配置' % theme.tone_color("ok")
        )
        body.addWidget(marker)

        self._game_list = QListWidget()
        self._game_list.setObjectName("sceneList")
        self._game_list.setFrameShape(QFrame.NoFrame)
        self._game_list.setSelectionMode(QListWidget.SingleSelection)
        self._game_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._game_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._game_list.setFocusPolicy(Qt.NoFocus)
        for profile_id, label in OCR_GAME_CHOICES:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, profile_id)
            self._game_list.addItem(item)
        self._game_list.setFixedHeight(len(OCR_GAME_CHOICES) * 32 + 6)
        self._game_list.currentItemChanged.connect(self._on_game_list_changed)
        body.addWidget(self._game_list)

        body.addStretch(1)
        return panel

    def _build_source_panel(self) -> DockPanel:
        """直播源：最近连接 + 备注 + 地址输入 + 连接/断开。"""
        panel = DockPanel("直播源")
        body = panel.body

        body.addWidget(self._section_label("最近连接"))

        self._recent_combo = QComboBox()
        self._recent_combo.setMaxVisibleItems(8)
        self._recent_combo.setMinimumWidth(120)
        self._recent_combo.currentTextChanged.connect(self._on_recent_selected)
        body.addWidget(self._recent_combo)

        note_row = QHBoxLayout()
        note_row.setSpacing(6)

        self._edit_note_btn = QPushButton("备注")
        self._edit_note_btn.setEnabled(False)
        self._edit_note_btn.setToolTip("为选中的历史记录添加备注")
        self._edit_note_btn.clicked.connect(self._on_edit_note_clicked)

        self._clear_recent_btn = QPushButton("清除")
        self._clear_recent_btn.setToolTip("清除全部最近连接")
        self._clear_recent_btn.clicked.connect(self._on_clear_recent_clicked)

        note_row.addWidget(self._edit_note_btn, 1)
        note_row.addWidget(self._clear_recent_btn, 1)
        body.addLayout(note_row)

        body.addWidget(h_line())
        body.addWidget(self._section_label("直播间地址"))

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("输入抖音直播间 URL")
        self._url_input.returnPressed.connect(self._on_connect_clicked)
        body.addWidget(self._url_input)

        self._connect_btn = QPushButton("连接")
        set_property(self._connect_btn, "variant", "primary")
        self._connect_btn.setToolTip("解析直播间地址并开始拉流")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        body.addWidget(self._connect_btn)

        body.addStretch(1)
        return panel

    # ------------------------------------------------------------------
    # 中央：直播预览（视觉中心）
    # ------------------------------------------------------------------
    def _build_preview_panel(self) -> DockPanel:
        panel = DockPanel("直播预览")
        panel.set_body_margins(0, 0, 0, 0)
        panel.set_body_spacing(0)

        self._lbl_resolution = self._make_chip()
        self._lbl_resolution.setText("分辨率 --")
        panel.add_header_widget(self._lbl_resolution)

        self._video_container = OverlayHost()
        self._video_container.setObjectName("previewFrame")
        self._video_container.setAttribute(Qt.WA_StyledBackground, True)
        video_layout = QVBoxLayout(self._video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        self._video_widget = VideoWidget()
        video_layout.addWidget(self._video_widget, 1)

        # 空状态提示：真正的浮层，不参与布局
        self._connection_status_label = QLabel("尚未连接直播")
        self._connection_status_label.setObjectName("previewOverlay")
        self._connection_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_container.add_overlay(self._connection_status_label)
        self._connection_status_label.show()

        # OCR 复制成功浮层（尺寸自适应，位置在 _show_copy_feedback 里算）
        self._copy_feedback_label = QLabel("✓ 已复制")
        self._copy_feedback_label.setObjectName("copyToast")
        self._copy_feedback_label.setParent(self._video_container)
        self._copy_feedback_label.adjustSize()
        self._copy_feedback_label.hide()
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.timeout.connect(self._hide_copy_feedback)

        panel.body.addWidget(self._video_container, 1)
        return panel

    # ------------------------------------------------------------------
    # 右侧 Dock：OCR 控制
    # ------------------------------------------------------------------
    def _build_ocr_dock(self) -> DockPanel:
        panel = DockPanel("OCR 控制")
        body = panel.body

        # ---- 游戏 ----
        body.addWidget(self._section_label("游戏"))
        self._ocr_game_combo = QComboBox()
        for profile_id, label in OCR_GAME_CHOICES:
            self._ocr_game_combo.addItem(label, profile_id)
        self._ocr_game_combo.setCurrentIndex(
            self._ocr_game_combo.findData(self._ocr_profile_id)
        )
        self._ocr_game_combo.setToolTip("决定房间号按哪款游戏的格式识别")
        self._ocr_game_combo.currentIndexChanged.connect(self._on_ocr_game_changed)
        body.addWidget(self._ocr_game_combo)

        body.addWidget(h_line())

        # ---- OCR 区域 ----
        body.addWidget(self._section_label("OCR 区域"))
        self._lbl_roi = QLabel("未选择")
        self._lbl_roi.setObjectName("valueLabel")
        self._lbl_roi.setWordWrap(True)
        body.addWidget(self._lbl_roi)

        self._roi_btn = QPushButton("选择识别区域")
        self._roi_btn.setEnabled(False)
        self._roi_btn.setToolTip("在直播画面上框选房间号所在区域")
        self._roi_btn.clicked.connect(self._on_roi_select_clicked)
        body.addWidget(self._roi_btn)

        body.addWidget(h_line())

        # ---- OCR 识别 ----
        body.addWidget(self._section_label("OCR 识别"))

        self._ocr_state_chip = self._make_chip()
        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        state_row.addWidget(self._ocr_state_chip)
        state_row.addStretch(1)
        body.addLayout(state_row)

        self._ocr_btn = QPushButton("开始OCR")
        self._ocr_btn.setEnabled(False)
        set_property(self._ocr_btn, "variant", "primary")
        self._ocr_btn.setToolTip("开始 / 停止识别所选区域")
        self._ocr_btn.clicked.connect(self._on_ocr_clicked)
        body.addWidget(self._ocr_btn)

        result_caption = QLabel("识别结果")
        result_caption.setObjectName("fieldLabel")
        body.addWidget(result_caption)

        self._lbl_ocr_text = QLabel("--")
        self._lbl_ocr_text.setObjectName("resultBox")
        self._lbl_ocr_text.setProperty("filled", "false")
        self._lbl_ocr_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_ocr_text.setMinimumHeight(44)
        self._lbl_ocr_text.setWordWrap(True)
        body.addWidget(self._lbl_ocr_text)

        body.addStretch(1)
        # OCR 初始状态由 _reset_perf_ui() 统一设置：那时状态栏芯片也已存在，
        # 右栏指示与状态栏芯片能一次性对齐。
        return panel

    # ------------------------------------------------------------------
    # 底部状态栏
    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        """状态栏：左侧留给瞬时消息，右侧是一排只读状态芯片。

        芯片走 ``addPermanentWidget`` —— ``showMessage`` 显示临时消息时
        Qt 会隐藏普通 widget，状态读数不应该跟着闪。
        """
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)

        self._lbl_status = self._make_chip()
        self._lbl_latency = self._make_chip()
        self._lbl_fps = self._make_chip()
        self._lbl_ocr = self._make_chip()
        self._lbl_clipboard = self._make_chip()

        for chip in (
            self._lbl_status,
            self._lbl_latency,
            self._lbl_fps,
            self._lbl_ocr,
            self._lbl_clipboard,
        ):
            bar.addPermanentWidget(chip)

        self._access_status_label = QLabel()
        self._access_status_label.setObjectName("accessLabel")
        self._access_status_label.setProperty("tone", "neutral")
        bar.addPermanentWidget(self._access_status_label)

        self._license_btn = QPushButton("激活许可证")
        self._license_btn.setObjectName("statusButton")
        set_property(self._license_btn, "variant", "primary")
        self._license_btn.setStatusTip("激活许可证")
        self._license_btn.clicked.connect(self._on_license_clicked)
        bar.addPermanentWidget(self._license_btn)

        # 剪贴板芯片的自动回落（复制成功 → 2 秒后恢复待命）
        self._clipboard_chip_timer = QTimer(self)
        self._clipboard_chip_timer.setSingleShot(True)
        self._clipboard_chip_timer.timeout.connect(self._reset_clipboard_chip)
        self._reset_clipboard_chip()

        self._reset_perf_ui()

    # ------------------------------------------------------------------
    # 小组件构造 / 状态芯片
    # ------------------------------------------------------------------
    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    @staticmethod
    def _make_chip() -> QLabel:
        """状态芯片：等宽字体 + 状态语气（QSS 里的 ``[tone=...]``）。"""
        chip = QLabel()
        chip.setObjectName("metricChip")
        chip.setTextFormat(Qt.RichText)
        chip.setProperty("tone", "neutral")
        return chip

    @staticmethod
    def _set_chip(chip: QLabel, text: str, tone: str = "neutral", dot: bool = False) -> None:
        """更新状态芯片：文字 + 语气色（可选前置状态圆点）。"""
        chip.setText(chip_html(text, tone) if dot else text)
        set_tone(chip, tone)

    def _set_result_text(self, text: str) -> None:
        """识别结果框：有结果时点亮，空结果保持低调。"""
        self._lbl_ocr_text.setText(text if text else "--")
        set_property(self._lbl_ocr_text, "filled", "true" if text else "false")

    def _set_ocr_state(self, running: bool, error: bool = False) -> None:
        """OCR 状态是同一份状态的两处显示：右栏指示 + 状态栏芯片。"""
        if error:
            self._set_chip(self._ocr_state_chip, "异常", "error", dot=True)
            self._set_chip(self._lbl_ocr, "OCR 异常", "error", dot=True)
        elif running:
            self._set_chip(self._ocr_state_chip, "运行中", "ok", dot=True)
            self._set_chip(self._lbl_ocr, "OCR 运行中", "ok", dot=True)
        else:
            self._set_chip(self._ocr_state_chip, "未运行")
            self._set_chip(self._lbl_ocr, "OCR 未运行")

    def _reset_clipboard_chip(self) -> None:
        self._set_chip(self._lbl_clipboard, "剪贴板 —")

    # ------------------------------------------------------------------
    # 游戏选择：列表与下拉框是同一个设置的两个视图
    # ------------------------------------------------------------------
    def _game_row_for(self, profile_id: str) -> int:
        for row in range(self._game_list.count()):
            if self._game_list.item(row).data(Qt.UserRole) == profile_id:
                return row
        return 0

    def _sync_game_views(self, profile_id: str) -> None:
        """把「配置 / 场景」列表与 OCR 下拉框对齐到同一个档。

        只同步显示，不触发任何业务回调（业务链路仍由 _on_ocr_game_changed 独占）。
        """
        was_syncing = self._syncing_games
        self._syncing_games = True
        try:
            for row in range(self._game_list.count()):
                item = self._game_list.item(row)
                active = item.data(Qt.UserRole) == profile_id
                label = GAME_LABELS.get(item.data(Qt.UserRole), item.text().strip())
                item.setText(("● " if active else "   ") + label)
            self._game_list.setCurrentRow(self._game_row_for(profile_id))
            index = self._ocr_game_combo.findData(profile_id)
            if index >= 0 and index != self._ocr_game_combo.currentIndex():
                self._ocr_game_combo.setCurrentIndex(index)
        finally:
            self._syncing_games = was_syncing

    def _on_game_list_changed(self, current, previous=None):  # noqa: N802
        """左栏场景列表选择 → 复用下拉框那条业务链路。"""
        if self._syncing_games or current is None:
            return
        profile_id = current.data(Qt.UserRole)
        index = self._ocr_game_combo.findData(profile_id)
        if index >= 0 and index != self._ocr_game_combo.currentIndex():
            # 改下拉框会触发 _on_ocr_game_changed，由它完成同步与提示
            self._ocr_game_combo.setCurrentIndex(index)
        else:
            self._sync_game_views(profile_id)

    # ------------------------------------------------------------------
    # 无边框窗口
    # ------------------------------------------------------------------
    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):  # noqa: N802
        """跟随窗口状态切换标题栏按钮字形（最大化 / 还原）。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            kind = (
                WindowButton.RESTORE if self.isMaximized() else WindowButton.MAX
            )
            self._title_bar.max_button.set_kind(kind)

    # ------------------------------------------------------------------
    # 最近连接的流管理
    # ------------------------------------------------------------------
    def _load_recent_streams(self):
        """加载最近连接的流列表"""
        streams = self._stream_history.get_all()
        self._recent_combo.clear()

        for stream in streams:
            url = stream["url"]
            nickname = stream.get("nickname", "")

            if nickname:
                # 昵称加粗显示
                display_text = f"{nickname} - {url}"
            else:
                # 从URL提取房间ID作为显示
                room_id = self._extract_room_id(url)
                if room_id:
                    display_text = f"房间 {room_id} - {url}"
                else:
                    display_text = url

            self._recent_combo.addItem(display_text, url)

    def _extract_room_id(self, url: str) -> str:
        """从URL提取房间ID"""
        try:
            # 处理不同格式的URL
            if "live.douyin.com/" in url:
                return url.split("live.douyin.com/")[1].split("/")[0]
            return ""
        except:
            return ""

    def _on_recent_selected(self, text: str):
        """选择最近连接的流"""
        if text:
            # 获取存储的URL
            index = self._recent_combo.currentIndex()
            if index >= 0:
                url = self._recent_combo.itemData(index)
                if url:
                    self._url_input.setText(url)
                    # 启用备注按钮
                    self._edit_note_btn.setEnabled(True)
                    # 保存当前选中的URL
                    self._selected_url_for_note = url

    def _on_edit_note_clicked(self):
        """编辑备注"""
        if not self._selected_url_for_note:
            return

        # 获取当前备注
        current_note = self._stream_history.get_nickname(self._selected_url_for_note)

        # 创建输入对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑备注")
        dialog.setFixedWidth(400)

        layout = QVBoxLayout(dialog)

        # 提示标签
        label = QLabel(f"为以下地址编辑备注:\n{self._selected_url_for_note}")
        layout.addWidget(label)

        # 备注输入框
        note_input = QLineEdit(current_note)
        note_input.setPlaceholderText("输入备注（留空清除备注）")
        layout.addWidget(note_input)

        # 按钮框
        button_box = QHBoxLayout()

        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")

        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)

        button_box.addWidget(ok_btn)
        button_box.addWidget(cancel_btn)
        layout.addLayout(button_box)

        # 显示对话框
        result = dialog.exec()

        if result == QDialog.Accepted:
            # 保存备注
            new_note = note_input.text().strip()
            self._stream_history.set_nickname(self._selected_url_for_note, new_note)

            # 重新加载列表以更新显示
            self._load_recent_streams()

            # 显示反馈
            if new_note:
                self.statusBar().showMessage(f"备注已保存: {new_note}")
            else:
                self.statusBar().showMessage("备注已清除")

    def _on_clear_recent_clicked(self):
        """清除历史记录"""
        reply = QMessageBox.question(
            self,
            "确认清除",
            "确定要清除所有最近连接的历史记录吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self._stream_history.clear()
            self._load_recent_streams()
            self._selected_url_for_note = None
            self._edit_note_btn.setEnabled(False)
            self.statusBar().showMessage("已清除历史记录")

    def _save_stream_history(self, url: str):
        """保存连接历史"""
        nickname = self._nickname_input.text().strip()
        self._stream_history.add_or_update(url, nickname)
        self._load_recent_streams()

    # ------------------------------------------------------------------
    # 连接 / 断开
    # ------------------------------------------------------------------
    def _on_connect_clicked(self):
        """连接按钮点击事件"""
        if self._connected:
            self._disconnect()
        elif self._connect_worker and self._connect_worker.is_running():
            return
        else:
            self._connect()

    def _connect(self):
        """发起连接：解析直播间 → 获取流地址 → 启动 FFmpegReader"""
        # 受保护入口：新建连接需要有效访问权限
        if not self._guard_access("连接直播流"):
            return

        url = self._url_input.text().strip()
        if not url:
            self.statusBar().showMessage("错误：请输入直播间URL")
            return

        self._connect_btn.setEnabled(False)
        self._connect_btn.setText("连接中...")
        self._url_input.setEnabled(False)
        self.statusBar().showMessage(f"正在解析直播间: {url}")

        # 添加到历史记录（不带备注）
        self._stream_history.add_or_update(url, "")
        self._load_recent_streams()

        self._connect_worker = _ConnectWorker(
            live_url=url,
            on_success=self._on_connect_success,
            on_error=self._on_connect_error,
        )
        self._connect_worker.start()

    def _on_connect_success(self, result: dict):
        """连接成功回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_success called (bg thread), emitting _connect_success_sig")
        self._connect_success_sig.emit(result)

    def _handle_connect_success(self, result: dict):
        """在 GUI 线程中处理连接成功"""
        stream_url = result["stream_url"]
        stream_type = result["stream_type"]
        room_id = result["room_id"]

        self.statusBar().showMessage(
            f"已获取流地址 (room={room_id}, type={stream_type})，正在连接视频流..."
        )
        logger.info(
            "[GUI] _handle_connect_success: room_id=%s, stream_type=%s, url=%s",
            room_id, stream_type, stream_url[:80],
        )

        # 创建并启动 FFmpegReader
        try:
            # 从结果中获取检测到的视频分辨率
            video_width = result.get("video_width")
            video_height = result.get("video_height")

            # 如果检测到分辨率，使用实际分辨率
            if video_width and video_height:
                logger.info(
                    "[GUI] 使用检测到的视频分辨率: %dx%d",
                    video_width, video_height
                )
                self._reader = FFmpegReader(
                    stream_url,
                    width=video_width,
                    height=video_height,
                    use_cuda=True,
                )
            else:
                # 使用默认分辨率
                logger.warning("[GUI] 未检测到视频分辨率，使用默认值: 1920x1080")
                self._reader = FFmpegReader(
                    stream_url,
                    width=1920,
                    height=1080,
                    use_cuda=True,
                )

            self._reader.start()
            logger.info("[FFMPEG] reader.start() called, use_cuda=True")
        except Exception as e:
            logger.error("[FFMPEG] reader.start() FAILED: %s", e)
            self.statusBar().showMessage(f"FFmpeg启动失败: {e}")
            self._reset_connect_ui()
            return

        # 创建 LatestFrameBuffer
        self._frame_buffer = LatestFrameBuffer()
        logger.info("[BUFFER] LatestFrameBuffer created")

        # 创建 PerformanceMonitor 并启动
        self._perf = PerformanceMonitor()
        self._perf.start()

        # 创建并启动 FrameBridge: reader → buffer (+ perf.on_capture)
        self._frame_bridge = _FrameBridge(self._reader, self._frame_buffer, self._perf)
        self._frame_bridge.start()
        logger.info("[BRIDGE] FrameBridge.start() called")

        # 切换到观看状态
        self._connected = True
        self._last_displayed_frame_id = 0
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("断开")
        # 连接后主按钮语义从「执行」变为「中断」，用危险色把这一点说清楚
        set_property(self._connect_btn, "variant", "danger")
        self._url_input.setEnabled(False)
        # 连接后启用 ROI 选择（访问被拒绝时保持禁用）
        self._roi_btn.setEnabled(not self._access_status.is_expired())

        # 隐藏连接状态消息
        self._connection_status_label.hide()

        # 更新性能面板
        self._set_chip(self._lbl_resolution, f"{self._reader.width} × {self._reader.height}")
        self._set_chip(self._lbl_status, "连接中", "warn", dot=True)

        # 更新 OCR 按钮状态
        self._update_ocr_button_state()

        # 禁用备注按钮（只有在选择历史项时才启用）
        self._edit_note_btn.setEnabled(False)

        # 启动帧轮询（16ms ≈ 60Hz 采样率，更快响应新帧）
        self._frame_timer.start(16)
        # 启动状态栏刷新（每秒更新一次）
        self._status_timer.start(1000)
        logger.info("[GUI] frame_timer started (16ms), status_timer started (1000ms)")

    def _on_connect_error(self, error_msg: str):
        """连接失败回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_error called (bg thread), emitting _connect_error_sig")
        self._connect_error_sig.emit(error_msg)

    def _handle_connect_error(self, error_msg: str):
        """在 GUI 线程中处理连接失败"""
        self._reset_connect_ui()
        self._selected_url_for_note = None
        self._edit_note_btn.setEnabled(False)
        self.statusBar().showMessage(f"连接失败: {error_msg}")
        logger.warning("连接失败: %s", error_msg)

    def _disconnect(self):
        """断开连接，停止一切"""
        # 停止 OCR
        self._stop_ocr()

        # 停止定时器
        self._frame_timer.stop()
        self._status_timer.stop()

        # 停止 FrameBridge
        if self._frame_bridge is not None:
            self._frame_bridge.stop()
            self._frame_bridge = None

        # 停止 FFmpegReader
        if self._reader is not None:
            self._reader.stop()
            self._reader = None

        # 清空 buffer
        if self._frame_buffer is not None:
            self._frame_buffer.clear()
            self._frame_buffer = None

        # 重置性能监控
        if self._perf is not None:
            self._perf.reset()
            self._perf = None

        # 清除画面
        self._video_widget.clear_frame()
        # 隐藏ROI框
        self._video_widget.clear_roi()

        # 清除选中的URL
        self._selected_url_for_note = None
        self._edit_note_btn.setEnabled(False)

        # 重置 UI
        self._connected = False
        self._reset_connect_ui()
        self._reset_perf_ui()
        self._roi_btn.setEnabled(False)
        self._connection_status_label.show()
        self.statusBar().showMessage("已断开")

        # 断开后重新套用访问限制：访问已到期时连接入口应保持关闭
        self._on_access_status_changed()

    def _reset_connect_ui(self):
        """重置连接按钮和输入框为初始状态"""
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("连接")
        set_property(self._connect_btn, "variant", "primary")
        self._url_input.setEnabled(True)
        self._connected = False
        self._selected_url_for_note = None
        self._edit_note_btn.setEnabled(False)

    def _reset_perf_ui(self):
        """重置性能面板为初始状态"""
        self._set_chip(self._lbl_fps, "FPS --")
        self._set_chip(self._lbl_latency, "延迟 --")
        self._set_chip(self._lbl_resolution, "分辨率 --")
        self._set_chip(self._lbl_status, "就绪", dot=True)
        self._lbl_roi.setText("未选择")
        self._set_ocr_state(False)
        self._set_result_text("")

    # ------------------------------------------------------------------
    # ROI 区域选择
    # ------------------------------------------------------------------
    def _on_roi_select_clicked(self):
        """点击"选择识别区域"按钮：从 buffer 取最新帧，弹出 ROI 选择窗口"""
        # 受保护入口：选择识别区域需要有效访问权限
        if not self._guard_access("选择识别区域"):
            return

        if self._frame_buffer is None:
            self.statusBar().showMessage("请先连接直播流")
            return

        frame = self._frame_buffer.get()
        if frame is None:
            self.statusBar().showMessage("尚未收到视频帧，请稍后再试")
            return

        logger.info("[ROI] Opening ROI selector with frame %dx%d",
                    frame.shape[1], frame.shape[0])

        # 隐藏当前ROI框
        self._video_widget.clear_roi()

        # 创建并显示选择窗口
        self._roi_selector = ROISelector(frame, parent=self)
        self._roi_selector.roi_selected.connect(self._on_roi_selected)
        self._roi_selector.roi_selection_canceled.connect(self._on_roi_canceled)
        self._roi_selector.show()

    def _on_roi_selected(self, roi: ROI):
        """ROI 选择确认回调"""
        self._roi_manager.set_roi(roi)
        self._lbl_roi.setText(
            f"{roi.width} × {roi.height} @ ({roi.x}, {roi.y})"
        )
        # 已有区域之后，这个按钮的语义是「重新框选」
        self._roi_btn.setText("重新框选")
        self.statusBar().showMessage(
            f"OCR 区域已选择: x={roi.x} y={roi.y} w={roi.width} h={roi.height}"
        )
        logger.info("[ROI] ROI set: %s", roi.to_dict())

        # 更新VideoWidget显示ROI框
        self._video_widget.update_roi(roi)

        # 更新 OCR 按钮状态
        self._update_ocr_button_state()

    def _on_roi_canceled(self):
        """ROI 选择取消回调（不改变旧 ROI）"""
        if self._roi_manager.has_roi:
            roi = self._roi_manager.get_roi()
            self.statusBar().showMessage("选择已取消，保留原区域")
            logger.info("[ROI] Selection canceled, keeping existing ROI")
            # 继续显示原ROI框
            self._video_widget.update_roi(roi)
        else:
            self.statusBar().showMessage("选择已取消")
            # 没有ROI时隐藏框
            self._video_widget.clear_roi()
        self._update_ocr_button_state()

    # ------------------------------------------------------------------
    # OCR 识别控制
    # ------------------------------------------------------------------
    def _on_ocr_game_changed(self, index: int):
        """游戏档切换：只改变后续 OCR 使用的房间号格式。

        刻意不重启 OCR 线程、不重启视频流：识别器换档是纯 CPU 的
        规则替换（引擎已存在，不加载 Tesseract），因此不会卡住 GUI。
        """
        profile_id = self._ocr_game_combo.itemData(index)
        if not profile_id:
            return

        self._ocr_profile_id = profile_id
        logger.info("[OCR] 游戏档切换: %s", profile_id)

        if self._ocr_worker is not None:
            self._ocr_worker.set_profile(profile_id)

        # 左栏场景列表跟随同一选择（只同步显示）
        self._sync_game_views(profile_id)

        self.statusBar().showMessage(
            f"OCR 识别游戏: {self._ocr_game_combo.currentText()}"
        )

    def _on_ocr_clicked(self):
        """OCR 按钮点击事件"""
        if self._ocr_running:
            self._stop_ocr()
        else:
            self._start_ocr()

    def _start_ocr(self):
        """启动 OCR 识别"""
        # 受保护入口：OCR 需要有效访问权限
        if not self._guard_access("启动 OCR 识别"):
            return

        if not self._connected or self._frame_buffer is None:
            self.statusBar().showMessage("请先连接直播流")
            return

        if not self._roi_manager.has_roi:
            self.statusBar().showMessage("请先选择识别区域")
            return

        logger.info("[OCR] Starting OCR recognition")

        # 创建 OCR 工作线程（带上当前选择的游戏档）
        self._ocr_worker = OCRWorker(
            frame_buffer_getter=lambda: self._frame_buffer,
            roi_manager=self._roi_manager,
            interval_ms=300,
            profile=self._ocr_profile_id,
        )

        # 连接信号
        self._ocr_worker.text_updated.connect(self._on_ocr_text_updated)
        self._ocr_worker.copy_requested.connect(self._on_copy_requested)
        self._ocr_worker.game_mismatch.connect(self._on_ocr_game_mismatch)
        self._ocr_worker.error_occurred.connect(self._on_ocr_error)
        self._ocr_worker.started.connect(self._on_ocr_started)
        self._ocr_worker.finished.connect(self._on_ocr_finished)

        # 启动识别
        self._ocr_worker.start_recognition()

    def _stop_ocr(self):
        """停止 OCR 识别"""
        if self._ocr_worker is not None:
            logger.info("[OCR] Stopping OCR recognition")
            self._ocr_worker.stop_recognition()
            self._ocr_worker = None

        self._ocr_running = False
        self._ocr_btn.setText("开始OCR")
        self._set_ocr_state(False)
        self._sync_ocr_button_variant()
        self.statusBar().showMessage("OCR 识别已停止")

    def _on_ocr_started(self):
        """OCR 线程启动回调"""
        self._ocr_running = True
        self._ocr_btn.setText("识别中")
        self._set_ocr_state(True)
        self._sync_ocr_button_variant()
        self.statusBar().showMessage("OCR 识别已启动")

    def _on_ocr_finished(self):
        """OCR 线程结束回调"""
        self._ocr_running = False
        self._ocr_btn.setText("开始OCR")
        self._set_ocr_state(False)
        self._sync_ocr_button_variant()

    def _on_ocr_text_updated(self, text: str):
        """OCR 文本更新回调"""
        # 更新识别结果显示
        self._set_result_text(text)

        # 记录日志
        if text:
            logger.info("[OCR] 识别文本更新: '%s'", text)

        # 可选：在状态栏显示
        if text:
            self.statusBar().showMessage(f"OCR 识别: {text}")

    def _on_ocr_error(self, error_msg: str):
        """OCR 错误回调"""
        logger.error("[OCR] 错误: %s", error_msg)
        self.statusBar().showMessage(f"OCR 错误: {error_msg}")
        self._set_ocr_state(self._ocr_running, error=True)

    def _on_ocr_game_mismatch(self):
        """OCR 认为当前选择的游戏与画面内容明显冲突。

        只提醒用户重新选择：
            - 不修改 _ocr_profile_id、不调用 set_profile（绝不替用户选择）
            - 不重启 OCR、不换档、不给「一键切换」入口
        用户关闭提示后自行在游戏选择器里重新选择，下一轮 OCR 按新档执行。
        """
        logger.warning("[OCR] 游戏选择冲突，提示用户重新选择")
        self.statusBar().showMessage(OCR_GAME_MISMATCH_MESSAGE)
        QMessageBox.warning(self, "提示", OCR_GAME_MISMATCH_MESSAGE)

    def _on_copy_requested(self, text: str):
        """OCR 请求复制到剪切板（在GUI线程中执行）"""
        logger.info("[CLIPBOARD] copy request received in GUI thread")

        # 在GUI线程中执行剪切板复制
        copied = self._clipboard_manager.copy(text)

        if copied:
            logger.info("[CLIPBOARD] copy success")
            # 显示复制成功反馈
            self._show_copy_feedback(text)
            # 状态栏芯片同步点亮，2 秒后自动回落到待命
            self._set_chip(self._lbl_clipboard, "剪贴板 ✓", "ok", dot=True)
            self._clipboard_chip_timer.start(2000)
        else:
            logger.error("[CLIPBOARD] copy failed reason: %s", self._clipboard_manager.last_error or "unknown")
            self._set_chip(self._lbl_clipboard, "剪贴板 ✕", "error", dot=True)
            self._clipboard_chip_timer.start(2000)
            self.statusBar().showMessage(f"复制失败: {self._clipboard_manager.last_error or '未知错误'}")

    def _show_copy_feedback(self, text: str):
        """显示复制成功反馈"""
        # 更新反馈文本
        display_text = f"✓ 已复制 {text}"
        self._copy_feedback_label.setText(display_text)

        # 定位到预览区右下角，稍微偏上；坐标以浮层的宿主容器为准，
        # 并夹回容器内，避免窗口刚显示、布局还没落定时飘到界面外。
        label = self._copy_feedback_label
        label.adjustSize()
        container = self._video_container
        x = max(0, container.width() - label.width() - 20)
        y = max(0, container.height() - label.height() - 20)

        label.move(x, y)
        label.show()
        label.raise_()

        # 500ms 后隐藏
        self._copy_feedback_timer.start(500)

        logger.debug("[FEEDBACK] 显示复制反馈: '%s'", display_text)

    def _hide_copy_feedback(self):
        """隐藏复制成功反馈"""
        self._copy_feedback_label.hide()
        logger.debug("[FEEDBACK] 隐藏复制反馈")

    def _update_ocr_button_state(self):
        """更新 OCR 按钮状态"""
        # 只有在已连接、已选择 ROI、且访问未被拒绝时才启用 OCR 按钮
        enabled = (
            self._connected
            and self._roi_manager.has_roi
            and not self._access_status.is_expired()
        )
        self._ocr_btn.setEnabled(enabled)

        if enabled and not self._ocr_running:
            self._ocr_btn.setText("开始OCR")
        elif enabled and self._ocr_running:
            self._ocr_btn.setText("识别中")
        else:
            self._ocr_btn.setText("开始OCR")

        self._sync_ocr_button_variant()

    def _sync_ocr_button_variant(self) -> None:
        """运行中用「停止」配色，空闲时用主操作配色。

        只动样式不动文案：按钮文案由 _update_ocr_button_state 独占，
        外部（含测试）会直接断言它的取值。
        """
        running = self._ocr_running and self._ocr_btn.isEnabled()
        set_property(self._ocr_btn, "variant", "danger" if running else "primary")

    # ------------------------------------------------------------------
    # 帧轮询（QTimer 驱动，从 LatestFrameBuffer 取帧，不阻塞 GUI）
    # ------------------------------------------------------------------
    def _on_frame_timeout(self):
        """每 30ms 被 QTimer 调用：从 LatestFrameBuffer 读取最新帧并显示"""
        if self._reader is None or self._frame_buffer is None:
            logger.warning("[DISPLAY] timer fired but reader=%s buffer=%s, stopping timer",
                           self._reader is not None, self._frame_buffer is not None)
            self._frame_timer.stop()
            return

        # 检查读取器错误
        if self._reader.error:
            logger.error("[DISPLAY] reader.error detected: %s", self._reader.error)
            self.statusBar().showMessage(f"视频错误: {self._reader.error}")
            self._disconnect()
            return

        # 读取器已停止但无错误（直播正常结束）
        if not self._reader.is_running and not self._reader.connected:
            logger.info("[DISPLAY] reader stopped, not connected -> disconnect")
            self.statusBar().showMessage("直播已结束")
            self._disconnect()
            return

        # 从 buffer 获取最新帧（仅当有新帧时才刷新显示）
        buffer_frame_id = self._frame_buffer.frame_id
        if buffer_frame_id != self._last_displayed_frame_id:
            frame = self._frame_buffer.get()
            if frame is not None:
                self._video_widget.set_frame(frame)
                # 隐藏连接状态消息（当有帧时）
                self._connection_status_label.hide()
                # 记录显示事件（计算 capture → display 延迟）
                if self._perf is not None:
                    self._perf.on_display()
                self._last_displayed_frame_id = buffer_frame_id
                if buffer_frame_id <= 3 or buffer_frame_id % 100 == 0:
                    logger.info(
                        "[DISPLAY] set_frame called: buf_id=%d shape=%s",
                        buffer_frame_id, frame.shape,
                    )

    # ------------------------------------------------------------------
    # 状态栏 + 性能面板刷新（低频，1秒一次）
    # ------------------------------------------------------------------
    def _on_status_timeout(self):
        """每秒刷新状态栏和性能面板"""
        if self._reader is None or self._perf is None:
            return

        # 更新性能面板
        snap = self._perf.snapshot()

        # FPS
        fps = snap["fps"]
        self._set_chip(self._lbl_fps, f"FPS {fps:.0f}" if fps > 0 else "FPS --")

        # Latency（当前延迟 / 平均延迟）
        cur_lat = snap["current_latency_ms"]
        avg_lat = snap["avg_latency_ms"]
        if cur_lat > 0:
            # 低延迟是本产品的核心指标：超过 100ms 变黄、超过 300ms 变红，
            # 让「延迟是否正常」一眼可辨，而不是靠用户读数字。
            if cur_lat <= 100:
                tone = "ok"
            elif cur_lat <= 300:
                tone = "warn"
            else:
                tone = "error"
            self._set_chip(self._lbl_latency, f"延迟 {cur_lat / 1000:.2f}s", tone)
        else:
            self._set_chip(self._lbl_latency, "延迟 --")

        # Resolution
        if self._reader.connected:
            w, h = self._reader.width, self._reader.height
            self._set_chip(self._lbl_resolution, f"{w} × {h}")

        # Status
        if self._reader.connected:
            self._set_chip(self._lbl_status, "直播中", "ok", dot=True)
        elif self._reader.is_running:
            self._set_chip(self._lbl_status, "连接中", "warn", dot=True)
        else:
            self._set_chip(self._lbl_status, "已停止", "error", dot=True)

        # 状态栏（详细 + 延迟诊断）
        if self._reader.connected:
            read_frames = self._reader.frames_read
            disp_frames = snap["display_frames"]
            runtime = snap["runtime_s"]
            skipped = self._reader.frames_skipped
            first_ms = self._reader.first_frame_ms
            first_str = f"{first_ms:.0f}ms" if first_ms else "--"
            self.statusBar().showMessage(
                f"帧: {read_frames}(跳{skipped}) | 显: {disp_frames} | "
                f"运行: {runtime:.0f}s | 延迟: {cur_lat:.1f}ms | 首帧: {first_str}"
            )
            # 延迟诊断日志（每5秒输出一次）
            if runtime > 0 and int(runtime) % 5 == 0 and int(runtime) != int(runtime - 1):
                logger.info(
                    "[Latency] pipeline=%s/%sms decode_fps=%.0f display_fps=%.1f "
                    "frames=%d skipped=%d first_frame=%s",
                    f"{cur_lat:.0f}" if cur_lat > 0 else "--",
                    f"{avg_lat:.0f}" if avg_lat > 0 else "--",
                    read_frames / runtime,
                    fps,
                    read_frames, skipped, first_str,
                )

    # ------------------------------------------------------------------
    # 访问限制（Phase 7.2-6.6）
    # ------------------------------------------------------------------
    def _guard_access(self, action: str) -> bool:
        """受保护动作的统一切口检查。

        所有需要有效访问权限的入口都必须先经过这里，
        而不是只在启动时判定一次。

        :param action: 动作名称，用于状态栏提示
        :return: True 允许继续；False 已拒绝（状态栏已说明原因）
        """
        if not self._access_status.is_expired():
            return True

        self.statusBar().showMessage(
            f"{self._access_status.get_status_text()}，无法{action}"
        )
        logger.info("访问被拒绝: %s", action)
        return False

    def _on_access_timer_timeout(self):
        """访问状态专用定时器回调（低频）。

        用于发现「应用运行中到期」：状态到期后无需重启即可生效。
        """
        self._access_status.refresh()

    def _on_access_status_changed(self):
        """访问状态变化回调：更新文案，并把限制反映到控件可用状态。

        只影响「能否发起新动作」，绝不主动断开已建立的视频连接 ——
        到期后仍保留画面与「断开」入口，避免把用户困在播放中。
        """
        status_text = self._access_status.get_status_text()
        self._access_status_label.setText(status_text)

        denied = self._access_status.is_expired()

        # 访问状态是「能不能用」的总开关，语气必须比普通读数更醒目：
        # 已到期用危险色，临近到期用警告色，正常则保持低调。
        if denied:
            tone = "error"
        elif "即将到期" in status_text:
            tone = "warn"
        else:
            tone = "ok"
        set_tone(self._access_status_label, tone)

        # 激活入口始终可用：任何状态下都应允许用户激活许可证
        self._license_btn.setEnabled(True)

        if not self._connected:
            # 未连接：把受限的入口可见地关掉
            self._connect_btn.setEnabled(not denied)
            self._url_input.setEnabled(not denied)
        elif denied and self._ocr_running:
            # 已连接：到期后停止正在运行的 OCR（不主动断开视频流）
            logger.info("访问已到期，停止正在运行的 OCR")
            self._stop_ocr()

        self._roi_btn.setEnabled(self._connected and not denied)
        self._update_ocr_button_state()

    def _on_access_expired(self):
        """访问过期回调"""
        # 显示过期对话框
        dialog = ExpiredDialog(self)
        dialog.exec()

    # ------------------------------------------------------------------
    # 许可证激活（Phase 7.2-6.5）
    # ------------------------------------------------------------------
    def _start_license_load(self):
        """后台加载并本地验证已保存的许可证，不阻塞 GUI。"""
        self._license_load_worker = LicenseWorker(
            task=self._license_manager.load_stored,
            on_done=self._license_loaded_sig.emit,
            name="license-startup",
        )
        self._license_load_worker.start()

    def _on_license_loaded(self, result: LicenseResult):
        """启动恢复结果（经 Qt 排队回到 GUI 线程）。"""
        logger.info("启动许可证校验结果: %s", result.status.value)
        self._apply_license_result(result)

    def _on_license_clicked(self):
        """激活许可证按钮点击事件：打开激活对话框。"""
        dialog = ActivationDialog(self._license_manager, self)
        dialog.exec()

        # 只有真正建立起的本地许可证状态才更新顶部状态；
        # 激活失败（网络不可达 / 密钥无效等）不会改变本地状态，
        # 其错误信息已在对话框中展示。
        result = dialog.result_data()
        if result is not None:
            self._apply_license_result(result)
            QMessageBox.information(
                self, "提示", f"{result.display_text}，许可证已保存到本机。"
            )

    def _apply_license_result(self, result: LicenseResult):
        """把许可证结果映射到顶部状态显示。"""
        if result.status is LicenseStatus.NOT_ACTIVATED:
            # 本地没有许可证：未激活即拒绝使用（免费试用已移除）
            self._access_status.clear_license_state()
        elif result.status is LicenseStatus.ACTIVATED:
            # 有效期直接来自服务器签名的凭据，客户端不做时长计算
            self._access_status.set_license_active(result.expires_at)
        else:
            self._access_status.set_license_inactive(result.display_text)

    # ------------------------------------------------------------------
    # 启动心跳（Phase 7.4，DAU 统计）
    #
    # 这是一条纯遥测链路，与授权流程严格隔离：
    #   - 上报逻辑在 LicenseManager.heartbeat()，它永不抛异常、永不改状态；
    #   - 调度逻辑在 utils.heartbeat_schedule.HeartbeatScheduler（纯本地计算）；
    #   - 这里只负责起后台线程与摆弄定时器，绝不调用 _apply_license_result，
    #     也绝不触碰 AccessStatus。
    # 因此心跳无论如何失败，都不会影响用户看到的授权状态。
    #
    # 节奏：启动立即上报一次；之后对齐**下一个业务日边界**，且每次触发后
    # 重新计算下一次延时。为什么不是固定周期，见 heartbeat_schedule 模块说明。
    # ------------------------------------------------------------------
    def start_heartbeat(self):
        """开启心跳上报。由应用入口 main.py 调用。

        为什么不在 __init__ 里自动开始：
            心跳会向许可证服务器发起**真实网络请求**，而「构造一个 MainWindow」
            在测试里是极常见的动作（多个 GUI 测试直接实例化窗口）。若把它挂在
            构造流程上，每次跑测试都会向生产服务器发一次心跳，污染真实 DAU。
            遥测属于「应用启动了」这件事，不属于「窗口对象被构造了」这件事。

        授权流程不受影响：本方法只依赖 LicenseManager.heartbeat()，它永不抛异常、
        永不改状态；结果槽也只用于推进调度状态。

        重复调用是安全的：上一次上报仍在进行时不会堆叠线程。
        """
        self._start_heartbeat()

    def _start_heartbeat(self):
        """后台上报一次心跳，不阻塞 GUI。"""
        # 上一次仍在跑就跳过，避免网络慢时堆叠线程
        if self._heartbeat_worker is not None and self._heartbeat_worker.is_running():
            return
        self._heartbeat_worker = LicenseWorker(
            task=self._license_manager.heartbeat,
            on_done=self._heartbeat_done_sig.emit,
            name="license-heartbeat",
        )
        self._heartbeat_worker.start()

    def _schedule_next_heartbeat(self):
        """按调度器算出的延时重新装上定时器。

        每次都现算，而不是沿用固定周期 —— 这是"不累积 QTimer 漂移"的落点。
        """
        delay_ms = self._heartbeat_scheduler.next_delay_ms(datetime.now(timezone.utc))
        if delay_ms is None:
            # 业务时区不可用：保留启动那一次上报，但不做周期补发。
            # 刻意不猜一个固定偏移 —— 猜错会把心跳记到服务端的另一天，
            # 比"不补发"更糟。此处只在启动上报结束时走到一次，不会刷屏。
            logger.warning("心跳周期调度不可用（业务时区无法解析），已停止补发")
            return
        self._heartbeat_timer.start(delay_ms)

    def _on_heartbeat_timer_timeout(self):
        """定时器到点：业务日若已推进就补报，然后重新对齐下一个边界。

        watchdog 会周期性地把这里叫醒（见 WATCHDOG_MAX_MS），因此有两种情况：
        当前业务日已上报过 -> 什么都不做，只重新对齐；
        业务日已推进（含休眠跨天后唤醒）-> 补报这一天。
        """
        now = datetime.now(timezone.utc)

        if not self._heartbeat_scheduler.should_report(now):
            self._schedule_next_heartbeat()
            return

        if self._heartbeat_worker is not None and self._heartbeat_worker.is_running():
            # 上一次还没回来：稍后再来，不堆叠线程
            self._schedule_next_heartbeat()
            return

        self._start_heartbeat()

    def _on_heartbeat_done(self, credited_day):
        """心跳结束回调（经 Qt 排队回到 GUI 线程）。

        只做两件事：推进调度状态、重新装定时器。绝不触碰授权状态显示 ——
        心跳是遥测，失败不该有任何界面反馈。

        ``credited_day`` 是**服务端**返回的 ``active_date``（见
        LicenseServerClient.heartbeat），即服务端本次实际记账的业务日。

        刻意**不**用"收到响应时的本地日期"来推断已上报的业务日：请求贴着
        业务日边界发出、响应跨过边界时两端会差一天，那样会把下一天误记为
        已上报，而服务端只记了前一天 —— 下一天于是永远不会被上报。
        记账权威在服务端，这里只原样转交。

        类型判断而不是真值判断：LicenseWorker 在任务抛异常时会回调一个
        LicenseResult 兜底对象，它既不是 None 也不是 date，绝不能被当成
        "某一天已上报"；非 date 一律视为未上报，保持 watchdog 重试。

        显式排除 ``datetime``：它是 ``date`` 的子类，会通过 isinstance 检查，
        但一旦混进 ``_reported_day``，"业务日是否已上报"的比较就永远为不等，
        调度器会退化成每 15 分钟无限重试。只接受纯 date。
        """
        if isinstance(credited_day, date) and not isinstance(credited_day, datetime):
            self._heartbeat_scheduler.mark_reported_day(credited_day)
        self._schedule_next_heartbeat()

    # ------------------------------------------------------------------
    # 窗口事件处理
    # ------------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        """窗口大小改变时更新覆盖层位置"""
        super().resizeEvent(event)
        self._update_connection_overlay_position()

    def closeEvent(self, event):  # noqa: N802
        """窗口关闭时确保资源释放"""
        self._disconnect()
        super().closeEvent(event)
