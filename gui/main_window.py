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

from PySide6.QtCore import QTimer, Qt, Signal, QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QVBoxLayout,
    QWidget,
)

from gui.video_widget import VideoWidget
from gui.expired_dialog import ExpiredDialog
from stream.douyin import DouyinStream, DouyinStreamError
from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor
from utils.access_status import AccessStatus, AccessState
from ocr.roi_manager import ROI, ROIManager
from ocr.roi_selector import ROISelector
from ocr.worker import OCRWorker
from ocr.clipboard import ClipboardManager
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
class MainWindow(QMainWindow):
    """Douyin Low Latency Stream Viewer 主窗口"""

    # 跨线程安全 Signal：后台线程 → GUI 线程
    _connect_success_sig = Signal(dict)
    _connect_error_sig = Signal(str)

    def __init__(self):
        super().__init__()

        self.setWindowTitle("抖音低延迟播放器")
        self.resize(1280, 800)

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
        self._access_status.set_update_timer(self._status_timer)
        self._access_status.set_callbacks(self._on_access_status_changed, self._on_access_expired)

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
        # ---- Compact control panel ----
        control_panel = QWidget()
        control_panel.setObjectName("control_panel")
        control_panel.setStyleSheet("""
            QWidget#control_panel {
                background-color: rgba(45, 55, 72, 0.9);
                border-radius: 6px;
                border: 1px solid rgba(74, 85, 104, 0.5);
                margin-bottom: 0;
            }
        """)

        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(10, 8, 10, 8)
        control_layout.setSpacing(4)

        # Section 1: Recent Streams
        recent_layout = QHBoxLayout()
        recent_layout.setSpacing(6)

        recent_label = QLabel("最近连接:")
        recent_label.setStyleSheet("font-size: 11px; color: #a0aec0;")

        self._recent_combo = QComboBox()
        self._recent_combo.setFixedHeight(24)
        self._recent_combo.setMaxVisibleItems(5)
        self._recent_combo.currentTextChanged.connect(self._on_recent_selected)
        self._recent_combo.setMinimumWidth(180)

        # Group buttons with dropdown
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        self._edit_note_btn = QPushButton("备注")
        self._edit_note_btn.setFixedHeight(24)
        self._edit_note_btn.setFixedWidth(75)
        self._edit_note_btn.setFont(QFont("Arial", 11))
        self._edit_note_btn.clicked.connect(self._on_edit_note_clicked)
        self._edit_note_btn.setEnabled(False)

        self._clear_recent_btn = QPushButton("清除")
        self._clear_recent_btn.setFixedHeight(24)
        self._clear_recent_btn.setFixedWidth(75)
        self._clear_recent_btn.setFont(QFont("Arial", 11))
        self._clear_recent_btn.clicked.connect(self._on_clear_recent_clicked)

        button_layout.setSpacing(8)

        button_layout.addWidget(self._edit_note_btn)
        button_layout.addWidget(self._clear_recent_btn)

        recent_layout.addWidget(recent_label)
        recent_layout.addWidget(self._recent_combo, stretch=1)
        recent_layout.addLayout(button_layout)
        control_layout.addLayout(recent_layout)

        # Section 2: URL Input & Connect
        input_layout = QHBoxLayout()
        input_layout.setSpacing(8)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("输入抖音直播间URL")
        self._url_input.setFixedHeight(24)
        self._url_input.setFont(QFont("Arial", 11))
        self._url_input.returnPressed.connect(self._on_connect_clicked)

        self._connect_btn = QPushButton("连接")
        self._connect_btn.setFixedHeight(24)
        self._connect_btn.setFixedWidth(65)
        self._connect_btn.setFont(QFont("Arial", 11))
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        self._roi_btn = QPushButton("选择识别区域")
        self._roi_btn.setFixedHeight(24)
        self._roi_btn.setFixedWidth(105)
        self._roi_btn.setFont(QFont("Arial", 11))
        self._roi_btn.setEnabled(False)
        self._roi_btn.clicked.connect(self._on_roi_select_clicked)

        self._ocr_btn = QPushButton("开始OCR")
        self._ocr_btn.setFixedHeight(24)
        self._ocr_btn.setFixedWidth(75)
        self._ocr_btn.setFont(QFont("Arial", 11))
        self._ocr_btn.setEnabled(False)
        self._ocr_btn.clicked.connect(self._on_ocr_clicked)

        input_layout.addWidget(self._url_input, stretch=1)
        input_layout.addWidget(self._connect_btn)
        input_layout.addWidget(self._roi_btn)
        input_layout.addWidget(self._ocr_btn)
        control_layout.addLayout(input_layout)

        # ---- 中部：视频显示区域 ----
        self._video_widget = VideoWidget()

        # ---- OCR 复制成功反馈标签（浮动显示）----
        self._copy_feedback_label = QLabel("✓ 已复制")
        self._copy_feedback_label.setStyleSheet("""
            QLabel {
                background-color: rgba(72, 187, 120, 0.9);
                color: white;
                padding: 10px 20px;
                border-radius: 8px;
                font-weight: 600;
                font-size: 14px;
                border: none;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        self._copy_feedback_label.setParent(self._video_widget)
        self._copy_feedback_label.hide()
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.timeout.connect(self._hide_copy_feedback)

        # ---- 底部：性能信息面板 ----
        perf_layout = QHBoxLayout()
        perf_layout.setContentsMargins(4, 2, 4, 2)

        self._lbl_fps = QLabel("FPS: --")
        self._lbl_latency = QLabel("Latency: -- ms")
        self._lbl_resolution = QLabel("Resolution: --")
        self._lbl_status = QLabel("Status: 就绪")
        self._lbl_roi = QLabel("区域: 未选择")
        self._lbl_ocr = QLabel("OCR: 未运行")
        self._lbl_ocr_text = QLabel("识别: --")

        # 等宽字体，对齐更整齐
        mono_font = self._lbl_fps.font()
        mono_font.setFamily("Consolas")
        for lbl in (self._lbl_fps, self._lbl_latency, self._lbl_resolution, self._lbl_status, self._lbl_roi, self._lbl_ocr, self._lbl_ocr_text):
            lbl.setFont(mono_font)

        perf_layout.addWidget(self._lbl_fps)
        perf_layout.addWidget(self._lbl_latency)
        perf_layout.addWidget(self._lbl_resolution)
        perf_layout.addWidget(self._lbl_roi)
        perf_layout.addWidget(self._lbl_ocr)
        perf_layout.addWidget(self._lbl_ocr_text)
        perf_layout.addStretch()  # 状态标签靠右
        perf_layout.addWidget(self._lbl_status)

        # ---- Video Section (prominent, expands to fill space) ----
        video_container = QWidget()
        video_container.setObjectName("video_container")
        video_container.setStyleSheet("""
            QWidget#video_container {
                background-color: rgba(26, 32, 44, 0.6);
                border-radius: 6px;
                border: 1px solid rgba(74, 85, 104, 0.3);
                min-height: 0;
            }
        """)

        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        # Video widget takes all space
        video_layout.addWidget(self._video_widget, 1)

        # Connection status overlay (true overlay, not in layout)
        self._connection_status_label = QLabel("尚未连接直播")
        self._connection_status_label.setStyleSheet("""
            QLabel {
                color: rgba(255, 255, 255, 0.3);
                font-size: 16px;
                font-weight: 300;
                background: none;
                padding: 20px;
            }
        """)
        self._connection_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._connection_status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Set parent to video container and position overlay
        self._connection_status_label.setParent(video_container)
        self._connection_status_label.setGeometry(0, 0, video_container.width(), video_container.height())

        # Initially show the message
        self._connection_status_label.raise_()  # Bring to front
        self._connection_status_label.show()

        # ---- 底部：性能信息面板 ----
        perf_layout = QHBoxLayout()
        perf_layout.setContentsMargins(8, 4, 8, 8)

        # 创建分组框架
        perf_group = QWidget()
        perf_group.setObjectName("perf_group")
        perf_group.setStyleSheet("""
            QWidget#perf_group {
                background-color: rgba(240, 240, 240, 180);
                border-radius: 6px;
                border: 1px solid rgba(200, 200, 200, 150);
            }
        """)

        perf_inner_layout = QHBoxLayout(perf_group)
        perf_inner_layout.setContentsMargins(12, 8, 12, 8)
        perf_inner_layout.setSpacing(16)

        self._lbl_fps = QLabel("FPS: --")
        self._lbl_latency = QLabel("Latency: -- ms")
        self._lbl_resolution = QLabel("Resolution: --")
        self._lbl_roi = QLabel("区域: 未选择")
        self._lbl_ocr = QLabel("OCR: 未运行")
        self._lbl_ocr_text = QLabel("识别: --")

        # 等宽字体，对齐更整齐
        mono_font = self._lbl_fps.font()
        mono_font.setFamily("Consolas")
        mono_font.setPointSize(10)
        mono_font.setWeight(QFont.Weight.Medium)  # Use enum value instead of int

        # Configure all performance labels
        for lbl in (self._lbl_fps, self._lbl_latency, self._lbl_resolution, self._lbl_roi, self._lbl_ocr, self._lbl_ocr_text, self._lbl_status):
            lbl.setFont(mono_font)
            lbl.setStyleSheet("color: #63b3ed;")  # Light blue for performance metrics

        # OCR text label more compact
        self._lbl_ocr_text.setStyleSheet("color: #48bb78;")  # Green for OCR text

        perf_inner_layout.addWidget(self._lbl_fps)
        perf_inner_layout.addWidget(self._lbl_latency)
        perf_inner_layout.addWidget(self._lbl_resolution)
        perf_inner_layout.addWidget(self._lbl_roi)
        perf_inner_layout.addWidget(self._lbl_ocr)
        perf_inner_layout.addWidget(self._lbl_ocr_text)
        perf_inner_layout.addStretch()  # 状态标签靠右
        perf_inner_layout.addWidget(self._lbl_status)

        perf_layout.addWidget(perf_group)

        # ---- 组合布局 ----
        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)  # No spacing for maximum compactness

        # Header section (minimal height)
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        # App title
        title_label = QLabel("抖音低延迟播放器")
        title_label.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: 600;
                color: #ffffff;
                background: none;
                padding: 6px 0;
            }
        """)

        header_layout.addWidget(title_label)
        header_layout.addStretch()

        # Access status label
        self._access_status_label = QLabel()
        self._access_status_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #a0aec0;
                background: none;
                padding: 4px 0;
            }
        """)

        # License button (prominent)
        self._license_btn = QPushButton("激活许可证")
        self._license_btn.setFixedHeight(32)
        self._license_btn.setFixedWidth(120)
        self._license_btn.setStatusTip("激活许可证")
        self._license_btn.setStyleSheet("""
            QPushButton {
                padding: 6px 16px;
                border: 1px solid #4a90e2;
                border-radius: 6px;
                background-color: #4a90e2;
                color: white;
                font-size: 12px;
                font-weight: 500;
                min-height: 32px;
            }
            QPushButton:hover {
                background-color: #357abd;
            }
            QPushButton:pressed {
                background-color: #2968a3;
            }
            QPushButton:disabled {
                background-color: #2c5282;
                color: #a0aec0;
                border-color: #4a5568;
            }
        """)
        self._license_btn.clicked.connect(self._on_license_clicked)

        header_layout.addWidget(self._access_status_label)
        header_layout.addWidget(self._license_btn)
        main_layout.addLayout(header_layout)

        # Main content area
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Control panel (minimal margins)
        content_layout.addWidget(control_panel)

        # Video section (takes ALL remaining space)
        content_layout.addWidget(video_container, stretch=1)

        # Compact status bar
        status_bar = QWidget()
        status_bar.setObjectName("status_bar")
        status_bar.setStyleSheet("""
            QWidget#status_bar {
                background-color: rgba(45, 55, 72, 0.9);
                border-top: 1px solid rgba(74, 85, 104, 0.5);
            }
        """)

        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(8, 4, 8, 4)
        status_layout.setSpacing(12)

        # Performance metrics
        status_layout.addWidget(self._lbl_fps)
        status_layout.addWidget(self._lbl_latency)
        status_layout.addWidget(self._lbl_resolution)
        status_layout.addWidget(self._lbl_roi)
        status_layout.addWidget(self._lbl_ocr)
        status_layout.addWidget(self._lbl_ocr_text)
        status_layout.addStretch()
        status_layout.addWidget(self._lbl_status)

        content_layout.addWidget(status_bar)
        main_layout.addLayout(content_layout)

        self.setCentralWidget(central)

        # 应用样式 - Dark Navy/Blue Theme (Compact)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a202c;
                color: #e2e8f0;
            }

            /* Input Fields */
            QLineEdit {
                padding: 6px 12px;
                border: 1px solid #4a5568;
                border-radius: 6px;
                background-color: #2d3748;
                color: #e2e8f0;
                font-size: 12px;
                selection-background-color: #4a90e2;
            }

            QLineEdit:focus {
                border: 1px solid #4a90e2;
                background-color: #374151;
                outline: none;
            }

            /* Buttons */
            QPushButton {
                padding: 4px 12px;
                border: 1px solid #4a90e2;
                border-radius: 6px;
                background-color: #4a90e2;
                color: white;
                font-size: 12px;
                font-weight: 500;
                min-height: 28px;
            }

            QPushButton:hover {
                background-color: #357abd;
                border-color: #357abd;
            }

            QPushButton:pressed {
                background-color: #2968a3;
                border-color: #2968a3;
            }

            QPushButton:disabled {
                background-color: #2c5282;
                color: #a0aec0;
                border-color: #4a5568;
            }

            /* Combo Box */
            QComboBox {
                padding: 6px 10px;
                border: 1px solid #4a5568;
                border-radius: 6px;
                background-color: #2d3748;
                color: #e2e8f0;
                font-size: 12px;
                min-height: 28px;
            }

            QComboBox::drop-down {
                border: none;
                padding-right: 6px;
            }

            QComboBox::down-arrow {
                image: none;
                border: none;
                width: 0;
                height: 0;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 4px solid #a0aec0;
            }

            QComboBox QAbstractItemView {
                background-color: #2d3748;
                border: 1px solid #4a5568;
                border-radius: 6px;
                selection-background-color: #4a90e2;
                selection-color: white;
            }

            QComboBox QAbstractItemView::item {
                padding: 6px 10px;
            }

            /* Labels */
            QLabel {
                font-size: 13px;
                color: #e2e8f0;
            }

            /* Status Bar */
            QStatusBar {
                background-color: #2d3748;
                color: #e2e8f0;
                border-top: 1px solid #4a5568;
            }

            QStatusBar::item {
                border: none;
            }

            /* Menu Bar */
            QMenuBar {
                background-color: #2d3748;
                color: #e2e8f0;
                border-bottom: 1px solid #4a5568;
            }

            QMenuBar::item {
                background: transparent;
                padding: 6px 12px;
                border-radius: 4px;
            }

            QMenuBar::item:selected {
                background-color: #4a90e2;
            }

            QMenu {
                background-color: #2d3748;
                color: #e2e8f0;
                border: 1px solid #4a5568;
                border-radius: 6px;
            }

            QMenu::item {
                padding: 8px 20px;
            }

            QMenu::item:selected {
                background-color: #4a90e2;
            }

            /* Dialog */
            QDialog {
                background-color: #2d3748;
                color: #e2e8f0;
            }

            QDialog QLabel {
                color: #e2e8f0;
            }
        """)

        # ---- 状态栏 ----
        self.statusBar().showMessage("就绪")

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
        self._url_input.setEnabled(False)
        self._roi_btn.setEnabled(True)  # 连接后启用 ROI 选择

        # 隐藏连接状态消息
        self._connection_status_label.hide()

        # 更新性能面板
        self._lbl_resolution.setText(f"Resolution: {self._reader.width}x{self._reader.height}")
        self._lbl_status.setText("Status: 连接中")

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

    def _reset_connect_ui(self):
        """重置连接按钮和输入框为初始状态"""
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("连接")
        self._url_input.setEnabled(True)
        self._connected = False
        self._selected_url_for_note = None
        self._edit_note_btn.setEnabled(False)

    def _reset_perf_ui(self):
        """重置性能面板为初始状态"""
        self._lbl_fps.setText("FPS: --")
        self._lbl_latency.setText("Latency: -- ms")
        self._lbl_resolution.setText("Resolution: --")
        self._lbl_status.setText("Status: 就绪")
        self._lbl_roi.setText("区域: 未选择")
        self._lbl_ocr.setText("OCR: 未运行")
        self._lbl_ocr_text.setText("识别: --")

    # ------------------------------------------------------------------
    # ROI 区域选择
    # ------------------------------------------------------------------
    def _on_roi_select_clicked(self):
        """点击"选择识别区域"按钮：从 buffer 取最新帧，弹出 ROI 选择窗口"""
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
            f"区域: {roi.width}x{roi.height} ({roi.x},{roi.y})"
        )
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
    def _on_ocr_clicked(self):
        """OCR 按钮点击事件"""
        if self._ocr_running:
            self._stop_ocr()
        else:
            self._start_ocr()

    def _start_ocr(self):
        """启动 OCR 识别"""
        if not self._connected or self._frame_buffer is None:
            self.statusBar().showMessage("请先连接直播流")
            return

        if not self._roi_manager.has_roi:
            self.statusBar().showMessage("请先选择识别区域")
            return

        logger.info("[OCR] Starting OCR recognition")

        # 创建 OCR 工作线程
        self._ocr_worker = OCRWorker(
            frame_buffer_getter=lambda: self._frame_buffer,
            roi_manager=self._roi_manager,
            interval_ms=300
        )

        # 连接信号
        self._ocr_worker.text_updated.connect(self._on_ocr_text_updated)
        self._ocr_worker.copy_requested.connect(self._on_copy_requested)
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
        self._lbl_ocr.setText("OCR: 未运行")
        self.statusBar().showMessage("OCR 识别已停止")

    def _on_ocr_started(self):
        """OCR 线程启动回调"""
        self._ocr_running = True
        self._ocr_btn.setText("识别中")
        self._lbl_ocr.setText("OCR: 运行中")
        self.statusBar().showMessage("OCR 识别已启动")

    def _on_ocr_finished(self):
        """OCR 线程结束回调"""
        self._ocr_running = False
        self._ocr_btn.setText("开始OCR")
        self._lbl_ocr.setText("OCR: 已停止")

    def _on_ocr_text_updated(self, text: str):
        """OCR 文本更新回调"""
        # 更新识别结果显示
        display_text = text if text else "(空)"
        self._lbl_ocr_text.setText(f"识别: {display_text}")

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
        self._lbl_ocr.setText("OCR: 错误")

    def _on_copy_requested(self, text: str):
        """OCR 请求复制到剪切板（在GUI线程中执行）"""
        logger.info("[CLIPBOARD] copy request received in GUI thread")

        # 在GUI线程中执行剪切板复制
        copied = self._clipboard_manager.copy(text)

        if copied:
            logger.info("[CLIPBOARD] copy success")
            # 显示复制成功反馈
            self._show_copy_feedback(text)
        else:
            logger.error("[CLIPBOARD] copy failed reason: %s", self._clipboard_manager.last_error or "unknown")
            self.statusBar().showMessage(f"复制失败: {self._clipboard_manager.last_error or '未知错误'}")

    def _show_copy_feedback(self, text: str):
        """显示复制成功反馈"""
        # 更新反馈文本
        display_text = f"✓ 已复制 {text}"
        self._copy_feedback_label.setText(display_text)

        # 计算显示位置（视频区域右下角附近）
        video_widget = self._video_widget
        label_width = self._copy_feedback_label.sizeHint().width()
        label_height = self._copy_feedback_label.sizeHint().height()

        # 定位到视频区域右下角，稍微偏上
        x = video_widget.width() - label_width - 20
        y = video_widget.height() - label_height - 20

        self._copy_feedback_label.move(x, y)
        self._copy_feedback_label.show()

        # 500ms 后隐藏
        self._copy_feedback_timer.start(500)

        logger.debug("[FEEDBACK] 显示复制反馈: '%s'", display_text)

    def _hide_copy_feedback(self):
        """隐藏复制成功反馈"""
        self._copy_feedback_label.hide()
        logger.debug("[FEEDBACK] 隐藏复制反馈")

    def _update_ocr_button_state(self):
        """更新 OCR 按钮状态"""
        # 只有在已连接且已选择 ROI 时才启用 OCR 按钮
        enabled = self._connected and self._roi_manager.has_roi
        self._ocr_btn.setEnabled(enabled)

        if enabled and not self._ocr_running:
            self._ocr_btn.setText("开始OCR")
        elif enabled and self._ocr_running:
            self._ocr_btn.setText("识别中")
        else:
            self._ocr_btn.setText("开始OCR")

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
        self._lbl_fps.setText(f"FPS: {fps:.1f}" if fps > 0 else "FPS: --")

        # Latency（当前延迟 / 平均延迟）
        cur_lat = snap["current_latency_ms"]
        avg_lat = snap["avg_latency_ms"]
        if cur_lat > 0:
            self._lbl_latency.setText(f"Latency: {cur_lat:.1f}/{avg_lat:.1f} ms")
        else:
            self._lbl_latency.setText("Latency: -- ms")

        # Resolution
        if self._reader.connected:
            w, h = self._reader.width, self._reader.height
            self._lbl_resolution.setText(f"Resolution: {w}x{h}")

        # Status
        if self._reader.connected:
            status_text = "Playing"
        elif self._reader.is_running:
            status_text = "连接中"
        else:
            status_text = "Stopped"
        self._lbl_status.setText(f"Status: {status_text}")

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

    def _on_access_status_changed(self):
        """访问状态变化回调"""
        status_text = self._access_status.get_status_text()
        self._access_status_label.setText(status_text)

        # 更新按钮状态
        if self._access_status.get_state() == AccessState.EXPIRED:
            self._license_btn.setEnabled(True)
        else:
            self._license_btn.setEnabled(False)

    def _on_access_expired(self):
        """访问过期回调"""
        # 显示过期对话框
        dialog = ExpiredDialog(self)
        dialog.exec()

    def _on_license_clicked(self):
        """激活许可证按钮点击事件（占位）"""
        # 演示功能：重置试用期
        if self._access_status.get_state() == AccessState.EXPIRED:
            self._access_status.reset_trial()
            QMessageBox.information(
                self,
                "提示",
                "已重置7天试用期限。"
            )

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
