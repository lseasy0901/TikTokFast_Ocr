# -*- coding: utf-8 -*-
"""
主窗口 (Phase 1 - Step 6)

功能：
    - 抖音直播间 URL 输入
    - 连接 / 断开 按钮
    - 视频显示区域（VideoWidget）
    - 性能信息面板（FPS / Latency / Resolution / Status）
    - 状态栏信息
    - frameReady 通知驱动刷新显示（不再由固定周期 QTimer 消费帧）

数据流：
    FFmpegReader 读取线程
          │
          ↓  reader.read_frame()
    _FrameBridge 线程
          │
          ↓  buffer.update(frame)  +  perf.on_capture()  +  notifier.notify()
    LatestFrameBuffer（单槽，只保留最新帧）
          │
          ↓  frameReady 信号（可合并，队列投递到 GUI 线程）
          ↓  clear_pending()
          ↓  buffer.get() 取当前最新帧
    MainWindow._on_frame_ready
          │
          ↓  video_widget.set_frame(frame)  +  perf.on_display()
    VideoWidget

    注：QTimer(16ms) 仍在运行，但只承担连接健康检查（断流/结束检测），
    不再负责帧消费。

约束：
    - GUI 线程只负责界面，网络/读取均在后台线程
    - 不使用 while True 阻塞 GUI
    - 错误显示在状态栏，不直接崩溃
    - LatestFrameBuffer 为 FFmpeg 与 GUI 之间的唯一数据通道
"""

import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from PySide6.QtCore import (QEvent, QEasingCurve, QObject,
                             QPropertyAnimation, QStandardPaths, QTimer, Qt, Signal)
from PySide6.QtGui import QAction, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QMenu,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.theme import Metrics, Palette
from gui.video_widget import VideoWidget
from gui.widgets import (
    DockPanel,
    FramelessController,
    IconButton,
    InfoPopup,
    icon_pixmap,
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
from gui.settings_dialog import (
    CONTACT_EMAIL,
    CONTACT_WECHAT,
    SettingsDialog,
)
from gui.activation_dialog import ActivationDialog, LicenseWorker
from gui.update_dialog import UpdateDialog
from gui.onboarding import (
    DIALOG_START_FOR,
    GUIDE_ADVANCE,
    GUIDE_ALWAYS,
    GUIDE_REVIEW_CLOSED,
    SPOTLIGHT_STEPS,
    STEPS,
    GuideSpotlight,
    GuideToast,
    OnboardingDialog,
    RoiHelpDialog,
)
from utils import update_state
from utils.heartbeat_schedule import HeartbeatScheduler
from utils.update_service import UpdateError, UpdateService
from utils.license_manager import LicenseManager, LicenseResult, LicenseStatus
from stream.models import StreamInfo, StreamResolveError
from stream.resolver import StreamResolver
from stream.failover import CandidateFailover
from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor
from utils.access_status import AccessStatus
from ocr.roi_manager import ROI, ROIManager
from ocr.roi_selector import ROISelector
from ocr.worker import OCRWorker
from ocr.clipboard import ClipboardManager
from ocr.copy_log import CopyLog
from ocr.diagnostics import OCRDiagnostics
from ocr.sample_capture import SampleCapture
from ocr.profiles import game_profiles, get_profile
from utils.stream_history import StreamHistory
from utils.first_run import FirstRunStore
from utils.startup_timing import (
    STAGE_CANDIDATE_SELECTED,
    STAGE_FIRST_DISPLAY,
    StartupTiming,
)

logger = logging.getLogger("DouyinLowLatencyViewer.gui.main_window")


# ======================================================================
# 后台连接线程：执行流解析（平台无关），不阻塞 GUI
# ======================================================================
class _ConnectWorker:
    """后台线程：把直播间 URL 解析为统一的 StreamInfo"""

    def __init__(self, live_url: str, on_success, on_error, resolver=None):
        """
        :param live_url:   直播间 URL
        :param on_success: 成功回调，签名为 callback(info: StreamInfo)
        :param on_error:   失败回调，签名为 callback(error_msg: str)
        :param resolver:   StreamResolver；不传则新建一个默认的
        """
        self._live_url = live_url
        self._on_success = on_success
        self._on_error = on_error
        self._resolver = resolver if resolver is not None else StreamResolver()
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
            info = self._resolver.resolve(self._live_url)
            logger.info("[GUI] stream resolved: platform=%s room=%s type=%s url=%s",
                        info.platform, info.room_id, info.protocol,
                        (info.url or "")[:80])
            self._on_success(info)
        except StreamResolveError as e:
            logger.error("[GUI] stream resolve failed: %s", e)
            self._on_error(str(e))
        except Exception as e:
            logger.exception("连接过程未知异常")
            self._on_error(f"连接异常: {e}")


# ======================================================================
# 帧桥接线程：FFmpegReader → LatestFrameBuffer
# ======================================================================
class _FrameNotifier(QObject):
    """跨线程"帧已到达"通知器（Phase B-2）

    由 FrameBridge 子线程在写入 LatestFrameBuffer 之后调用 notify()，
    经 Qt 队列连接把 GUI 唤到 GUI 线程消费。设计要点：

    - **可合并**：上一通知未被消费前，后续通知只计数不再发信号，
      因此事件队列里最多只有一个待处理通知，不会无限堆积。
    - **不携带内容**：信号不带帧数据，GUI 被唤醒后自己重读
      LatestFrameBuffer 的当前最新帧，因此永远不会消费到旧帧。
    - 只发信号、不触碰任何 QWidget，子线程不会直接操作 GUI。
    """

    frameReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = False
        self.notify_count = 0      # 实际发出的通知数
        self.coalesced_count = 0   # 因 pending 未清而被合并掉的通知数

    def notify(self) -> None:
        """帧已写入 buffer：请求一次 GUI 唤醒（子线程调用，不阻塞）"""
        with self._lock:
            if self._pending:
                self.coalesced_count += 1
                return
            self._pending = True
            self.notify_count += 1
        self.frameReady.emit()

    def clear_pending(self) -> None:
        """GUI 线程的消费者在处理通知前调用，允许下一帧再次唤醒"""
        with self._lock:
            self._pending = False


class _FrameBridge:
    """
    后台线程：持续从 FFmpegReader 读取最新帧，写入 LatestFrameBuffer

    运行频率 1ms 轮询，保证 buffer 始终持有最新帧，
    即使 GUI 消费间隔内有多个新帧，buffer 只保留最新的，
    天然实现"宁可丢帧，不要累计延迟"。

    每次写入 buffer 后调用 perf.on_capture() 记录采集时间戳，
    用于后续计算 capture → display 延迟；随后通过 notifier 唤醒 GUI
    （不再依赖 GUI 侧的固定周期轮询）。
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
        # 每次连接新建一个：旧 bridge 停止后其通知器一并废弃，
        # 因此重连不会产生重复的信号连接。
        self.notifier = _FrameNotifier()

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
                    # 唤醒 GUI 消费（可合并、不携带帧数据、不阻塞本线程）
                    self.notifier.notify()
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
#:
#: 直接由 ocr/profiles.py 的注册表派生，不在 GUI 里再手写一份——两份清单
#: 各自维护时，新增一款游戏很容易只改一边（注册了档位却选不到，或者反过来）。
#: 界面短名来自 GameProfile.short_name，与游戏配置同处一地。
#:
#: 只暴露具体游戏——"auto" 仅作为内部默认档保留，不出现在 UI 上，
#: 因为用户显式选择某一款游戏才能消除不同位数之间的长度歧义。
OCR_GAME_CHOICES = tuple(
    (profile.profile_id, profile.short_name) for profile in game_profiles()
)

#: profile_id → 界面短名（左栏场景列表与右栏下拉框共用同一份文案）
GAME_LABELS = dict(OCR_GAME_CHOICES)

#: 默认游戏档（= 注册表里的第一款游戏）
DEFAULT_OCR_GAME_PROFILE_ID = OCR_GAME_CHOICES[0][0]

#: OCR 检测到「游戏选错了」时给用户看的固定文案。
#: 只说明需要重新选择，不含任何技术细节（不出现位数、格式名、档位名，
#: 不暗示程序认为应该是哪款游戏），也不附加任何操作入口。
OCR_GAME_MISMATCH_MESSAGE = "检测到选择错误，请重新选择游戏类型后再进行识别。"

# ======================================================================
# 内部诊断入口的可见性开关（生产 UI 收口）
# ======================================================================
#: 生产 UI 是否显示内部诊断入口。默认 False = 不显示。
#:
#: 只管**看得见 / 看不见**，不删除任何实现：被它隐藏的四个入口
#: （「开始诊断」「导出诊断 CSV」「全 PSM 观测」「采集 ROI 样本」）对应的
#: 按钮 / 复选框对象、信号连接、OCRDiagnostics、SampleCapture、
#: observe_all_psm 全链路一律原样保留。置为 True 即完整恢复原样，
#: 不需要改其它任何一行代码。
#:
#: 为什么用隐藏而不是不创建：`_diag_obs_check.isChecked()` 在开始识别时被
#: 读取（见 _start_ocr），控件必须继续存在才能保持行为不变；而"隐藏"同时
#: 天然阻止误触。Qt 的 QBoxLayout 会把隐藏控件当作空项，不占高度、不产生
#: 间距，因此关闭后不会留下空白带（由 test_ui_internal_diagnostics_gate.py
#: 用几何断言钉住）。
#:
#: 隐藏的是内部诊断/采样入口，**不**包括正式产品功能：「导出 CSV」（本次
#: 会话房间码流水）、历史记录、备注、导出路径等都照常显示。
SHOW_INTERNAL_DIAGNOSTICS = False

#: 房间码还没有结果时的占位文案。刻意用文字而不是 "--"：后者在等宽大字下
#: 和真实房间码难以区分，用户会把占位当成识别结果。
OCR_RESULT_PLACEHOLDER = "尚未识别"

#: 本次会话历史为空时的说明（占位项，不参与业务）。
SESSION_EMPTY_HINT = "复制成功的房间号会出现在这里"

#: 窗口宽度低于此值时自动收起右侧 OCR 面板，把宽度让给视频。
#:
#: 取「面板展开时的最小窗口宽度」本身，而不是比它更大的值：1100×660 是
#: 约定要保留的"面板展开的最小尺寸"，阈值一旦大于它，用户在 1100 就会看到
#: 面板被自动收掉，展开态就没有最小尺寸了。
#:
#: 由此带来的实际效果要说清楚：面板展开时窗口本来就被 minimumWidth 卡在
#: 1100，所以这条线只能被"程序把窗口设得更窄"（或将来调小
#: WINDOW_MIN_WIDTH）触发 —— 日常使用中收起仍然是用户主动点的。
#: 它是一条防止两个不变量互相打架的兜底，不是常见路径。
AUTO_COLLAPSE_WIDTH = Metrics.WINDOW_MIN_WIDTH

#: 顶部右侧操作区里「非弹性」部分的总宽度，用来把历史下拉的宽度反推出来：
#:     连接 96 + 间距 8 + ⋯ 34 + 间距 8 + 右边距 14 = 160
#: 于是 下拉宽度 = 面板宽度 - 160，下拉的左边缘正好落在 OCR 面板的左边缘上，
#: 连接按钮的右边缘落在面板内容的右边缘上。
#:
#: 左边缘取面板的**外**边缘（而不是内容边缘）是刻意的：这样顶部那 8px
#: 列间距与下方 QSplitter 的分隔条落在完全相同的像素区间上，
#: 「左右分割线精确对齐」才成立。右侧取内容边缘，是因为面板本体一直贴到
#: 窗口右缘，让按钮也贴到窗口边会失去呼吸空间。
_ADDR_ACTIONS_CHROME = 160

#: 启动后多久发起更新检查（Phase 1）。
#: 刻意不是 0：启动瞬间许可证加载、心跳、首帧解码都在抢资源与带宽，而更新
#: 检查是三者中唯一「晚几秒毫无损失」的一个。延迟也顺带降低了服务端被所有
#: 客户端同时打点的并发压力。
UPDATE_CHECK_DELAY_MS = 5000


class _ActivationOverlay(QWidget):
    """视频区上方的未激活遮罩：品牌 + 轻呼吸 + 「请激活 LiveLens」+ 激活入口。

    纯展示：只发一个 clicked 信号，不调用任何生命周期方法；呼吸动画用 Qt 原生
    属性动画，只在可见时运行，隐藏即停（不引入周期性定时器，也不碰视频线程）。
    """

    activate_clicked = Signal()
    contact_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("activationOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)

        col = QVBoxLayout(self)
        col.setContentsMargins(24, 24, 24, 24)
        col.setSpacing(10)
        col.addStretch(1)

        self._brand = QLabel("LiveLens")
        self._brand.setObjectName("activationBrand")
        self._brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._brand)

        self._breath = QGraphicsOpacityEffect(self._brand)
        self._brand.setGraphicsEffect(self._breath)
        self._anim = QPropertyAnimation(self._breath, b"opacity", self)
        self._anim.setDuration(2400)
        self._anim.setStartValue(0.55)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.setLoopCount(-1)

        self._prompt = QLabel("请激活 LiveLens")
        self._prompt.setObjectName("activationPrompt")
        self._prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._prompt)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self._activate_btn = QPushButton("立即激活")
        self._activate_btn.setFixedHeight(34)
        self._activate_btn.setMinimumWidth(120)
        self._activate_btn.setProperty("variant", "primary")
        self._activate_btn.clicked.connect(self.activate_clicked.emit)
        row.addWidget(self._activate_btn)

        # 次要入口：尚未激活的人同样需要能找到人，因此放在「立即激活」旁边，
        # 但保持次要样式 —— 主操作仍然只有激活一个。
        self._contact_btn = QPushButton("联系作者")
        self._contact_btn.setFixedHeight(34)
        self._contact_btn.setMinimumWidth(120)
        self._contact_btn.setIcon(
            QIcon(icon_pixmap(IconButton.CONTACT, 14, Palette.TEXT_DIM)))
        self._contact_btn.setCursor(Qt.PointingHandCursor)
        self._contact_btn.setToolTip("联系作者")
        self._contact_btn.clicked.connect(self.contact_clicked.emit)
        row.addWidget(self._contact_btn)

        row.addStretch(1)
        col.addLayout(row)

        self._hint = QLabel("激活后即可连接直播并开始 OCR 识别")
        self._hint.setObjectName("activationHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self._hint)
        col.addStretch(1)

    def show_unlocked(self) -> None:
        """解锁过渡：把遮罩切成"已激活"文案（视觉过渡，随后由调用方收起）。"""
        self._anim.stop()
        self._breath.setOpacity(1.0)
        self._brand.setText("LiveLens")
        self._prompt.setText("已激活")
        self._hint.setText("正在解锁工作台…")
        self._activate_btn.hide()

    def reset_prompt(self) -> None:
        """恢复未激活文案（下次显示遮罩时用）。"""
        self._prompt.setText("请激活 LiveLens")
        self._hint.setText("激活后即可连接直播并开始 OCR 识别")
        self._activate_btn.show()

    # -- 呼吸动画生命周期 --------------------------------------------------
    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._anim.start()

    def hideEvent(self, event):  # noqa: N802
        self._anim.stop()
        self._breath.setOpacity(1.0)
        super().hideEvent(event)


class MainWindow(QMainWindow):
    """Douyin Low Latency Stream Viewer 主窗口"""

    # 跨线程安全 Signal：后台线程 → GUI 线程
    _connect_success_sig = Signal(object)   # StreamInfo
    _connect_error_sig = Signal(str)
    _license_loaded_sig = Signal(object)  # 启动时许可证校验结果
    _heartbeat_done_sig = Signal(object)  # 心跳上报结果
    _update_check_done_sig = Signal(object)  # 更新检查结果（Velopack UpdateInfo 或 None）

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
        # 当前连接的帧到达通知器（随 FrameBridge 一同创建/废弃）
        self._frame_notifier: _FrameNotifier | None = None
        self._connect_worker: _ConnectWorker | None = None
        # 平台解析统一入口：MainWindow 只认识 StreamResolver 和 StreamInfo，
        # 不 import 也不判断任何具体平台。
        self._resolver = StreamResolver()

        # ---- Phase 2.5：候选流 failover ----
        # _failover 只在"一条连接会话有候选可试"期间存在；断开/关闭时置 None。
        # _playback_info 是本次会话的解析结果，供切换候选时复用（候选顺序、
        # 已探测分辨率都在里面），不重新解析。
        self._failover: CandidateFailover | None = None
        self._playback_info: StreamInfo | None = None
        #: 尝试代号。每次启动候选 / 断开 / 关闭都递增，用来让迟到的回调失效：
        #: 旧 attempt 不得再启动新 reader。
        self._attempt_id: int = 0
        #: Phase 3.2：当前这一次尝试的启动分段计时（candidate → ffprobe → FFmpeg
        #: → 首帧 → 首次显示）。每次 _start_playback 换新对象，尝试结束后只读不改。
        self._startup_timing: StartupTiming | None = None
        #: 最近一次 failover 切换的实际耗时（ms），无切换为 0.0。仅用于观测。
        self._last_failover_ms: float = 0.0
        self._perf: PerformanceMonitor | None = None
        self._connected: bool = False
        self._roi_manager: ROIManager = ROIManager()
        self._roi_selector: ROISelector | None = None
        self._ocr_worker: OCRWorker | None = None
        self._ocr_running: bool = False
        # 当前选择的游戏档（OCRWorker 构造/切换时使用）
        self._ocr_profile_id: str = DEFAULT_OCR_GAME_PROFILE_ID
        self._clipboard_manager: ClipboardManager = ClipboardManager()
        # 成功复制的房间码流水（内存内、只追加），仅供「导出 CSV」使用。
        # 与右栏的 _session_list 是两回事：那是给人看的、带去重与 50 行上限的
        # 显示历史，会丢事件；这份是事件账，每次成功复制都各记一条。
        self._copy_log: CopyLog = CopyLog()
        # OCR 诊断日志（默认关闭）。同一个实例交给 OCRWorker / 识别器使用：
        # 识别线程往里写执行记录，GUI 线程回填复制结果，内部自带锁。
        # 关闭时它不建记录、不占内存，也不会有任何文件写入。
        self._diagnostics: OCRDiagnostics = OCRDiagnostics()
        # ROI 样本采集（默认关闭）：把识别器真正看到的那一帧像素存下来，
        # 供离线基准重跑真实引擎。落盘目录按进程时间戳分一次，
        # 避免多次运行互相覆盖；目录在第一个样本落盘时才真正创建。
        self._sample_capture: SampleCapture = SampleCapture(
            self._default_sample_dir())
        self._stream_history: StreamHistory = StreamHistory()

        # 首次启动状态。构造只读一个本地文件（容错，失败按默认值处理），
        # 不联网、不弹窗 —— 真正的弹窗由 main.py 经 maybe_show_first_run_guide()
        # 触发，理由与 start_heartbeat 相同：测试会直接构造 MainWindow。
        self._first_run_store: FirstRunStore = FirstRunStore()
        # 同一时刻最多一个引导向导（嵌套事件循环里可能重入）
        self._onboarding_dialog: OnboardingDialog | None = None
        # 「本次会话已经自动弹过引导」：授权成功、启动恢复、窗口显示几条路径
        # 都可能触发首次引导，只靠 prefs 标记挡不住（用户可能在最后一页取消
        # 勾选「不再自动显示」，那样 prefs 不会写入，同一会话里就会被弹第二次）。
        self._first_run_guide_done = False
        # 主界面聚光灯（非模态），前两步各一个实例。它存在期间不打开模态向导。
        self._guide_spotlight: GuideSpotlight | None = None
        # 第二步需要游戏下拉框可见：面板收起时由引导临时展开。
        # 记下原状态，引导结束时还回去；用户中途自己动过面板就不还。
        self._guide_panel_restore_to: bool | None = None
        self._guide_panel_touched = False
        #: 引导完成的轻量提示（独立顶层窗口，2 秒后自动消失）
        self._guide_toast = None
        #: 「联系作者」弹出卡片（Qt.Popup，点外面自动关）
        self._contact_popup = None
        #: 当前这轮聚光灯是不是"标题栏 ? 的回顾模式"。
        #: 回顾模式不写任何首次引导状态、不弹退出确认、不显示完成提示。
        self._guide_review = False
        self._roi_help_dialog: RoiHelpDialog | None = None

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

        # 更新检查（Phase 3：Velopack）
        # 与授权、遥测同样解耦：构造时不联网（UpdateService 惰性构造
        # UpdateManager），真正的检查由应用入口 main.py 经 start_update_check()
        # 触发（理由与心跳完全一致 —— 见 start_heartbeat 的说明：测试会大量
        # 直接构造 MainWindow）。
        self._update_service = UpdateService()
        self._update_worker: LicenseWorker | None = None
        # 对话框与后台下载线程的持有者：下载可以在窗口收起后继续跑完，
        # 因此不能让它随 exec() 返回被回收。
        self._update_dialog: UpdateDialog | None = None
        self._update_check_done_sig.connect(self._on_update_check_done)
        self._update_timer = QTimer(self)
        # 单次触发：启动后查一次即可，本次不做周期性复查。
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._on_update_timer_timeout)

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
        """构建主界面（Studio Minimal 工作台）。

        排版意图：

            ┌────────────────────────────────────────────────────────┐
            │ LiveLens      帮助 授权             —  □  ×            │ 标题栏
            ├────────────────────────────────────────────────────────┤
            │ [直播地址输入]        [历史 ▾] [备注][清除] [连接]      │ 地址栏
            ├──────────────────────────────────┬─────────────────────┤
            │                                  │ 当前房间码           │
            │          直播预览（等比不裁剪）    │ 本次会话             │
            ├──────────────────────────────────┤ 游戏 / ROI          │ OCR 面板
            │ ● LIVE  1920×1080  FPS 60  [ROI] │ [开始OCR]（固定底部） │
            ├──────────────────────────────────┴─────────────────────┤
            │ 链路 … │ 解码 … │ 渲染 … │ OCR …   （分组性能条）        │
            └────────────────────────────────────────────────────────┘

        中央预览吃掉全部剩余空间，右侧面板可收起（收起后靠右边缘细条展开）。
        所有业务信号接线与定时器都在 _start_playback / _start_ocr 等处完成，
        这里只摆放控件 —— 与改动前的职责划分一致。
        """
        theme.apply_theme()

        self._syncing_games = True  # 构建期间不响应列表/下拉框联动

        root = QFrame()
        root.setObjectName("windowRoot")
        self._root = root
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_title_bar())
        root_layout.addWidget(self._build_address_bar())
        root_layout.addWidget(self._build_body(), 1)
        self.setCentralWidget(root)

        # 底部性能栏（挂在 QStatusBar 的永久区：左侧仍是瞬时消息区）
        self._build_perf_bar()

        # 无边框窗口：拖动 + 边角缩放（顶部热区只在全屏时启用，不与其争抢）
        self._frameless = FramelessController(self)
        enable_mouse_tracking(root)

        self._build_title_hotzone(root)
        # 顶部热区与标题栏/地址栏都要走事件过滤器（全屏滑出用）
        self._title_hot.installEventFilter(self)
        self._title_bar.installEventFilter(self)
        self._address_bar.installEventFilter(self)

        self._title_bar.min_button.clicked.connect(self.showMinimized)
        # 最大化按钮回归普通最大化 / 还原（全屏由 _fullscreen_btn 独立承担）
        self._title_bar.max_button.clicked.connect(self._toggle_maximized)
        self._title_bar.close_button.clicked.connect(self.close)

        # 顶部操作区与 OCR 面板列宽联动：以"面板自身被 resize"作为唯一触发点。
        # 用户拖分隔条、窗口缩放、最大化、setSizes 都会让面板 resize，因此
        # 不需要再分别挂 splitterMoved / resizeEvent —— 多个触发点反而容易
        # 出现某条路径漏同步（QSplitter.setSizes 就不发 splitterMoved）。
        self._ocr_panel.installEventFilter(self)
        self._sync_address_columns()

        self._syncing_games = False
        self._sync_game_views(self._ocr_profile_id)

        self.statusBar().showMessage("就绪")

    # ------------------------------------------------------------------
    # 标题栏 / 顶部地址栏
    # ------------------------------------------------------------------
    def _build_title_bar(self) -> TitleBar:
        """标题栏：品牌 + 图标操作区（引导 / 授权 / 设置 / 全屏）+ 窗口控制。

        操作区一律用自绘图标按钮：三个中文标签会把标题栏挤满，且随字号变化
        抖动；图标 + tooltip 在同样的宽度下更好辨认（Phase UI-3.1）。
        """
        self._title_bar = TitleBar("LiveLens", "低延迟播放工作台")

        self._help_btn = self._title_bar.add_icon_button(
            IconButton.HELP, "使用引导：查看操作流程，可随时重新打开")
        self._help_btn.clicked.connect(self._on_help_clicked)

        # 「授权」保持唯一被强调的入口（图标按钮上的 variant=primary）
        self._license_btn = self._title_bar.add_icon_button(
            IconButton.LICENSE, "激活许可证：输入密钥为本机授权")
        set_property(self._license_btn, "variant", "primary")
        self._license_btn.clicked.connect(self._on_license_clicked)

        # 「联系作者」放在授权与设置之间：它和这两个一样是"辅助入口"，
        # 不抢主操作的注意力
        self._contact_btn = self._title_bar.add_icon_button(
            IconButton.CONTACT, "联系作者")
        self._contact_btn.clicked.connect(self._on_contact_clicked)

        self._settings_btn = self._title_bar.add_icon_button(
            IconButton.SETTINGS, "设置")
        self._settings_btn.clicked.connect(self._on_settings_clicked)

        # 全屏工作台是【独立】入口：窗口按钮里的最大化仍然是普通最大化，
        # 不能用全屏取代它（否则用户就没有"占满屏幕但保留任务栏"这条路了）。
        self._fullscreen_btn = self._title_bar.add_icon_button(
            IconButton.FULLSCREEN, "全屏工作台：隐藏标题栏与地址栏")
        self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        return self._title_bar

    def _build_address_bar(self) -> QFrame:
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

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("粘贴抖音直播间地址，例如 https://live.douyin.com/…")
        self._url_input.setClearButtonEnabled(True)
        self._url_input.returnPressed.connect(self._on_connect_clicked)
        # 地址输入框是直播地址的唯一事实来源：备注动作也跟随它的内容
        self._url_input.textChanged.connect(self._sync_note_action)
        row.addWidget(self._url_input, 1)

        self._recent_combo = QComboBox()
        self._recent_combo.setMaxVisibleItems(10)
        self._recent_combo.setMinimumWidth(60)
        # 弹性：宽度由 _sync_address_columns 按 OCR 面板的实际宽度算出来后钉住，
        # 因此它总是吃掉操作区里除固定控件之外的剩余空间。
        self._recent_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # 选中后立刻复位成占位文案，因此这里平时只显示"历史记录"而不是某个地址
        self._recent_combo.setPlaceholderText("历史记录")
        self._recent_combo.setToolTip("最近连接（选中即填入左侧地址框）")
        self._recent_combo.currentIndexChanged.connect(self._on_recent_selected)
        row.addWidget(self._recent_combo)

        # 历史二级入口：备注 / 清除（原先各自占一个按钮，地址栏太挤）
        self._history_menu_btn = QToolButton()
        self._history_menu_btn.setObjectName("historyMenuBtn")
        self._history_menu_btn.setText("⋯")
        self._history_menu_btn.setFixedSize(34, 34)
        self._history_menu_btn.setToolTip("历史记录操作：编辑备注 / 清除历史")
        self._history_menu_btn.setCursor(Qt.PointingHandCursor)
        self._history_menu_btn.setPopupMode(QToolButton.InstantPopup)

        menu = QMenu(self._history_menu_btn)
        self._edit_note_action = QAction("编辑备注…", self)
        self._edit_note_action.setEnabled(False)
        self._edit_note_action.triggered.connect(self._on_edit_note_clicked)
        menu.addAction(self._edit_note_action)

        menu.addSeparator()
        self._clear_recent_action = QAction("清除历史记录…", self)
        self._clear_recent_action.triggered.connect(self._on_clear_recent_clicked)
        menu.addAction(self._clear_recent_action)
        self._history_menu_btn.setMenu(menu)
        row.addWidget(self._history_menu_btn)

        self._connect_btn = QPushButton("连接")
        set_property(self._connect_btn, "variant", "primary")
        self._connect_btn.setToolTip("解析直播间地址并开始拉流")
        # 固定宽度：连接是主操作，宽度不随窗口变化被压缩
        self._connect_btn.setFixedWidth(96)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        row.addWidget(self._connect_btn)

        self._address_bar = bar
        return bar

    def _build_body(self) -> QWidget:
        """主体：预览（占满剩余空间）+ 右侧 OCR 面板（可收起）。"""
        # 面板收起状态：用户主动选择优先于窄窗口自动收起（见 _sync_narrow_layout）
        self._panel_user_choice = False
        self._panel_auto_hidden = False

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.setObjectName("mainSplitter")
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(Metrics.SPLITTER_HANDLE)
        self._body_splitter.addWidget(self._build_preview())
        self._body_splitter.addWidget(self._build_ocr_panel())
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        self._body_splitter.setSizes([900, Metrics.OCR_PANEL_WIDTH])
        row.addWidget(self._body_splitter, 1)

        # 收起后的展开入口：贴右边缘的细条（比"没有入口"诚实）
        self._expand_btn = QPushButton("«")
        self._expand_btn.setObjectName("expandBtn")
        self._expand_btn.setFixedWidth(18)
        self._expand_btn.setToolTip("展开右侧 OCR 面板")
        self._expand_btn.clicked.connect(self._on_expand_clicked)
        self._expand_btn.hide()
        row.addWidget(self._expand_btn)

        body = QWidget()
        body.setLayout(row)
        return body

    # ------------------------------------------------------------------
    # 中央：直播预览（视觉中心）
    # ------------------------------------------------------------------
    def _build_preview(self) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(12, 12, 12, 0)
        layout.setSpacing(10)

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

        # 未激活遮罩：与连接提示同为浮层，默认按当前授权状态决定是否显示
        self._activation_overlay = _ActivationOverlay()
        self._activation_overlay.activate_clicked.connect(self._on_license_clicked)
        self._activation_overlay.contact_clicked.connect(
            self._on_overlay_contact_clicked)
        # interactive=True：遮罩上有「立即激活」按钮，必须收得到点击
        # （默认的鼠标穿透会让按钮连同浮层一起不可点）。
        self._video_container.add_overlay(self._activation_overlay, interactive=True)

        layout.addWidget(self._video_container, 1)
        layout.addWidget(self._build_video_toolbar())
        return column

    def _build_video_toolbar(self) -> QFrame:
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

        self._lbl_status = self._make_chip()
        self._lbl_status.setText("未连接")
        self._lbl_resolution = self._make_chip()
        self._lbl_resolution.setText("分辨率 --")
        self._lbl_fps = self._make_chip()
        self._lbl_fps.setText("FPS --")
        for chip in (self._lbl_status, self._lbl_resolution, self._lbl_fps):
            row.addWidget(chip)
        row.addStretch(1)
        return bar

    # ------------------------------------------------------------------
    # 右侧面板：当前房间码 → 本次会话 → 游戏 / ROI → OCR 按钮（固定底部）
    # ------------------------------------------------------------------
    def _build_ocr_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("ocrPanel")
        panel.setMinimumWidth(Metrics.OCR_PANEL_WIDTH_MIN)
        panel.setMaximumWidth(Metrics.OCR_PANEL_WIDTH_MAX)
        self._ocr_panel = panel

        col = QVBoxLayout(panel)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(self._section_label("OCR 控制"))
        head.addStretch(1)
        self._collapse_btn = QPushButton("»")
        self._collapse_btn.setObjectName("collapseBtn")
        self._collapse_btn.setFixedWidth(26)
        self._collapse_btn.setToolTip("收起右侧面板（窄窗口优先保留视频）")
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        head.addWidget(self._collapse_btn)
        col.addLayout(head)

        # ---- 1) 当前房间码（视觉优先级最高）----
        col.addWidget(self._section_label("当前房间码"))
        self._lbl_ocr_text = QLabel(OCR_RESULT_PLACEHOLDER)
        self._lbl_ocr_text.setObjectName("resultBox")
        self._lbl_ocr_text.setProperty("filled", "false")
        self._lbl_ocr_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_ocr_text.setMinimumHeight(72)
        self._lbl_ocr_text.setWordWrap(True)
        col.addWidget(self._lbl_ocr_text)

        state_row = QHBoxLayout()
        state_row.setSpacing(12)
        self._ocr_state_chip = self._make_chip()
        self._lbl_clipboard = self._make_chip()
        state_row.addWidget(self._ocr_state_chip)
        state_row.addWidget(self._lbl_clipboard)
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
        session_head_row.addWidget(self._section_label("本次会话"))
        session_head_row.addStretch(1)

        # 「导出 CSV」：导出的是上面这份显示历史**背后的**流水 ——
        # 每一次成功复制各一条（含重复复制的同一房间码），不受去重与
        # 50 行上限影响，因此 tooltip 必须说清楚，别让人以为导出的就是所见列表。
        self._export_csv_btn = QPushButton("导出 CSV", session_head)
        self._export_csv_btn.setObjectName("sectionAction")
        self._export_csv_btn.setCursor(Qt.PointingHandCursor)
        self._export_csv_btn.setFocusPolicy(Qt.NoFocus)
        self._export_csv_btn.setToolTip(
            "导出本次运行中每一次成功复制的房间码（同一房间码重复复制也各记一条）")
        self._export_csv_btn.clicked.connect(self._on_export_csv_clicked)
        session_head_row.addWidget(self._export_csv_btn)
        col.addWidget(session_head)

        # 诊断开关与导出（默认关闭）。与上面的「导出 CSV」同一套 #sectionAction
        # 小按钮样式，不再引入新的控件样式；两者都只动日志，不动识别。
        diag_row = QWidget()
        diag_row_layout = QHBoxLayout(diag_row)
        diag_row_layout.setContentsMargins(0, 0, 0, 0)
        diag_row_layout.setSpacing(6)

        self._diag_btn = QPushButton("开始诊断", diag_row)
        self._diag_btn.setObjectName("sectionAction")
        self._diag_btn.setCursor(Qt.PointingHandCursor)
        self._diag_btn.setFocusPolicy(Qt.NoFocus)
        self._diag_btn.setToolTip(
            "开启后记录每一次 OCR 执行的诊断信息（各 PSM 原始读法、候选、裁决、"
            "确认与复制结果）；只记录，不改变识别行为。默认关闭，记录只留在内存里。")
        self._diag_btn.clicked.connect(self._on_diag_toggled)
        diag_row_layout.addWidget(self._diag_btn)

        self._diag_export_btn = QPushButton("导出诊断 CSV", diag_row)
        self._diag_export_btn.setObjectName("sectionAction")
        self._diag_export_btn.setCursor(Qt.PointingHandCursor)
        self._diag_export_btn.setFocusPolicy(Qt.NoFocus)
        self._diag_export_btn.setToolTip("把诊断记录导出为 CSV（与房间码记录 CSV 分开的两份文件）")
        self._diag_export_btn.clicked.connect(self._on_export_diagnostics_clicked)
        diag_row_layout.addWidget(self._diag_export_btn)
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

        self._diag_obs_check = QCheckBox("全 PSM 观测", obs_row)
        self._diag_obs_check.setCursor(Qt.PointingHandCursor)
        self._diag_obs_check.setFocusPolicy(Qt.NoFocus)
        self._diag_obs_check.setToolTip(
            "连同 PSM 7/11 的读法一起记进诊断 CSV（默认不勾选）。\n"
            "需要先点「开始诊断」；识别运行期间不可更改，停止后恢复。\n"
            "补跑的读法只用于诊断，不参与仲裁、游戏冲突判定、连续确认与复制。")
        self._diag_obs_check.toggled.connect(self._on_diag_observe_toggled)
        obs_row_layout.addWidget(self._diag_obs_check)
        obs_row_layout.addStretch(1)
        col.addWidget(obs_row)

        # 「采集 ROI 样本」（默认不勾选）：把识别器真正看到的那一帧像素存到
        # 文档目录，供离线基准用真实引擎重跑。与诊断开关相互独立 ——
        # 采集只落盘、不判定，也不依赖诊断。
        cap_row = QWidget()
        cap_row_layout = QHBoxLayout(cap_row)
        cap_row_layout.setContentsMargins(0, 0, 0, 0)
        cap_row_layout.setSpacing(6)

        self._diag_capture_check = QCheckBox("采集 ROI 样本", cap_row)
        self._diag_capture_check.setCursor(Qt.PointingHandCursor)
        self._diag_capture_check.setFocusPolicy(Qt.NoFocus)
        self._diag_capture_check.setToolTip(
            "把识别器看到的那一帧 ROI 原图存到「文档/LiveLens-OCR样本」（默认不勾选）。\n"
            "供离线准确率基准用真实 OCR 引擎重跑；有数量与磁盘上限，采满即停。\n"
            "只落盘，不参与识别判定，也不影响复制结果。")
        self._diag_capture_check.toggled.connect(self._on_sample_capture_toggled)
        cap_row_layout.addWidget(self._diag_capture_check)
        cap_row_layout.addStretch(1)
        col.addWidget(cap_row)

        # 内部诊断入口的可见性：由模块顶部的单一开关决定，默认不显示。
        # 三个行控件已经 addWidget 进 col 并被 col 持有，这里只改可见性 ——
        # 控件对象、信号、实现全部保留（开关置 True 即完整恢复）。
        # 隐藏控件在 QBoxLayout 里是空项：不占高度、不产生行间距，所以关闭后
        # 不会留下空白带，也不会出现"不可见但仍占位"的控件。
        for row in (diag_row, obs_row, cap_row):
            row.setVisible(SHOW_INTERNAL_DIAGNOSTICS)

        # 初始状态：诊断默认关闭 -> 观测不可勾选
        self._update_diag_controls()

        self._session_list = QListWidget()
        self._session_list.setObjectName("sessionList")
        self._session_list.setFrameShape(QFrame.NoFrame)
        self._session_list.setFocusPolicy(Qt.NoFocus)
        self._session_list.setMinimumHeight(64)
        self._session_list.setToolTip("本次会话中【复制成功】的房间号与时间（最新在上）")
        self._session_list.setSelectionMode(QListWidget.NoSelection)
        col.addWidget(self._session_list, 1)
        self._session_placeholder = False
        self._show_session_placeholder()

        col.addWidget(h_line())

        # ---- 3) 游戏与 ROI ----
        col.addWidget(self._section_label("游戏"))
        self._ocr_game_combo = QComboBox()
        for profile_id, label in OCR_GAME_CHOICES:
            self._ocr_game_combo.addItem(label, profile_id)
        self._ocr_game_combo.setCurrentIndex(
            self._ocr_game_combo.findData(self._ocr_profile_id)
        )
        self._ocr_game_combo.setToolTip("决定房间号按哪款游戏的格式识别")
        self._ocr_game_combo.currentIndexChanged.connect(self._on_ocr_game_changed)
        col.addWidget(self._ocr_game_combo)

        # 场景列表（原左栏）保留实例与同步逻辑，但不再显示：游戏选择入口
        # 已由上面的下拉框承担（两者始终同步，功能没有丢失）。
        # 必须持有引用：无父控件的 QWidget 会被 Python 回收，导致 _game_list
        # 变成悬空包装对象（_sync_game_views 会直接抛 RuntimeError）。
        self._scene_panel = self._build_scene_panel()
        self._scene_panel.hide()

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
        header_row.addWidget(self._section_label("识别区域"))
        header_row.addStretch(1)

        self._roi_help_btn = QPushButton("说明", header)
        self._roi_help_btn.setObjectName("sectionAction")
        # 高度由 #sectionAction 的 QSS 钉死（QSS 优先级高于 setFixedHeight）
        self._roi_help_btn.setCursor(Qt.PointingHandCursor)
        self._roi_help_btn.setFocusPolicy(Qt.NoFocus)
        self._roi_help_btn.setToolTip("怎样框选识别区域（含正确 / 错误对比示例）")
        self._roi_help_btn.clicked.connect(self._on_roi_help_clicked)
        header_row.addWidget(self._roi_help_btn)
        col.addWidget(header)

        self._lbl_roi = QLabel("未选择")
        self._lbl_roi.setObjectName("valueLabel")
        self._lbl_roi.setWordWrap(True)
        col.addWidget(self._lbl_roi)

        self._roi_btn = QPushButton("选择识别区域")
        self._roi_btn.setEnabled(False)
        self._roi_btn.setToolTip("在直播画面上框选房间号所在区域")
        self._roi_btn.clicked.connect(self._on_roi_select_clicked)
        col.addWidget(self._roi_btn)

        # 这里只留最短的一句操作键序；完整说明在标题右侧的「说明」浮层里，
        # 避免同一段提示在面板、tooltip、帮助浮层三处各说一遍。
        roi_row = QHBoxLayout()
        roi_row.setSpacing(6)
        self._roi_note = QLabel("拖动框选 → Enter 确认 / Esc 取消")
        self._roi_note.setObjectName("captionLabel")
        self._roi_note.setWordWrap(True)
        roi_row.addWidget(self._roi_note, 1)
        col.addLayout(roi_row)

        # ---- 4) OCR 启停固定在面板底部 ----
        self._ocr_btn = QPushButton("开始OCR")
        self._ocr_btn.setEnabled(False)
        set_property(self._ocr_btn, "variant", "primary")
        self._ocr_btn.setToolTip("开始 / 停止识别所选区域")
        self._ocr_btn.setFixedHeight(36)
        self._ocr_btn.clicked.connect(self._on_ocr_clicked)
        col.addWidget(self._ocr_btn)
        return panel

    # ------------------------------------------------------------------
    # 底部：分组性能栏（挂在状态栏永久区，左侧保留瞬时消息）
    # ------------------------------------------------------------------
    def _build_perf_bar(self) -> None:
        """底部性能栏：分组展示已有指标；没有数据的一律显示 "—"。"""
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)

        self._lbl_latency = self._make_chip()
        self._lbl_ocr = self._make_chip()
        self._perf_values = {}
        self._perf_defaults = {}

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
        # 组与组之间插一条细分隔线：四组指标在没有分隔时会连成一整行密集字符串，
        # 用户分不清哪些数字属于哪一段（Phase UI-3.1）。
        for index, (name, items) in enumerate(groups):
            if index:
                divider = QFrame()
                divider.setObjectName("perfDivider")
                divider.setFixedWidth(1)
                bar.addPermanentWidget(divider)
            bar.addPermanentWidget(self._perf_group(name, items))
        bar.addPermanentWidget(self._lbl_ocr)
        bar.addPermanentWidget(self._lbl_clipboard)
        self._access_status_label = QLabel()
        self._access_status_label.setObjectName("accessLabel")
        self._access_status_label.setProperty("tone", "neutral")
        bar.addPermanentWidget(self._access_status_label)

        # 剪贴板芯片的 2 秒回落定时器（由 _on_copy_requested 启动）
        self._clipboard_chip_timer = QTimer(self)
        self._clipboard_chip_timer.setSingleShot(True)
        self._clipboard_chip_timer.timeout.connect(self._reset_clipboard_chip)
        self._reset_clipboard_chip()

    def _perf_group(self, name: str, items) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QLabel(f"{name}")
        head.setObjectName("perfGroup")
        lay.addWidget(head)
        for key, text, tip in items:
            lbl = QLabel(text)
            lbl.setObjectName("perfValue")
            lbl.setToolTip(tip)
            self._perf_values[key] = lbl
            self._perf_defaults[key] = text
            lay.addWidget(lbl)
        return box

    # ------------------------------------------------------------------
    # 全屏工作台 / 面板收起
    # ------------------------------------------------------------------
    def _set_perf(self, key: str, text: str) -> None:
        """更新性能栏某一项（键不存在时静默忽略，便于渐进调整）。"""
        label = self._perf_values.get(key)
        if label is not None:
            label.setText(text)

    def _build_title_hotzone(self, parent: QWidget) -> None:
        """全屏时用于"鼠标到顶部滑出标题栏"的细热区（非全屏不启用）。"""
        self._title_hot = QFrame(parent)
        self._title_hot.setFixedHeight(4)
        self._title_hot.setStyleSheet("background: transparent; border: none;")
        self._title_hot.hide()
        self._title_hide_timer = QTimer(self)
        self._title_hide_timer.setSingleShot(True)
        self._title_hide_timer.setInterval(1000)
        self._title_hide_timer.timeout.connect(self._maybe_autohide_title)

    def _toggle_fullscreen(self) -> None:
        """标题栏按钮：全屏工作台 / 还原（刻意不提供键盘快捷键）。

        退出全屏走 showNormal()：从全屏退回的是普通窗口，而不是最大化窗口 ——
        用户点的是"全屏工作台"，退出后就该回到进入前那种可见标题栏的状态。
        """
        if self.isFullScreen():
            self.showNormal()
            self._frameless.set_fullscreen(False)
            self._show_title_chrome(True)
            self._title_hot.hide()
            self._title_hide_timer.stop()
        else:
            self._frameless.set_fullscreen(True)
            # 从最大化进入全屏时先退回普通窗口，避免 DWM 在两者之间保留
            # 最大化的工作区尺寸（会导致全屏后视频区比屏幕小一圈）
            if self.isMaximized():
                self.showNormal()
            self.showFullScreen()
            self._title_hot.show()
            self._title_hot.raise_()
            self._position_title_hotzone()
            # 全屏默认隐藏标题栏与地址栏；鼠标到顶部时滑出（见 eventFilter）
            self._show_title_chrome(False)
            # 全屏给足工作区，但不覆盖用户的主动收起：用户自己收掉的面板，
            # 不该因为切到全屏又自己弹回来。
            if not self._panel_user_choice:
                self._panel_auto_hidden = False
                self._set_ocr_panel_visible(True)

    def _show_title_chrome(self, visible: bool) -> None:
        self._title_bar.setVisible(visible)
        self._address_bar.setVisible(visible)

    def _position_title_hotzone(self) -> None:
        if getattr(self, "_title_hot", None) is not None:
            self._title_hot.setGeometry(0, 0, self.width(), 4)

    def _maybe_autohide_title(self) -> None:
        if not self.isFullScreen() or getattr(self, "_title_hot", None) is None:
            return
        if self._title_hot.underMouse():
            return
        if self._title_bar.underMouse() or self._address_bar.underMouse():
            self._title_hide_timer.start()
            return
        self._show_title_chrome(False)

    def eventFilter(self, obj, event):  # noqa: N802
        """顶部热区 → 滑出标题栏；离开标题栏/地址栏 1 秒后收起。

        另外监听 OCR 面板自身的 resize，用来把顶部操作区的宽度重新对齐到
        面板的实际宽度（见 :meth:`_sync_address_columns`）。
        """
        if obj is getattr(self, "_ocr_panel", None) \
                and event.type() == QEvent.Type.Resize:
            self._sync_address_columns()
            # OCR 面板尺寸变了，第二步高亮的游戏下拉框位置也跟着变
            self._reposition_guide_spotlight()
            return False

        hot = getattr(self, "_title_hot", None)
        if hot is not None and obj is hot and event.type() == QEvent.Type.Enter:
            if self.isFullScreen():
                self._show_title_chrome(True)
                self._title_bar.raise_()
                self._address_bar.raise_()
                self._title_hide_timer.start()
        elif obj in (getattr(self, "_title_bar", None),
                     getattr(self, "_address_bar", None)):
            if not self.isFullScreen():
                return super().eventFilter(obj, event)
            if event.type() == QEvent.Type.Leave:
                self._title_hide_timer.start()
            elif event.type() == QEvent.Type.Enter:
                self._title_hide_timer.stop()
        return super().eventFilter(obj, event)

    def _sync_address_columns(self) -> None:
        """让顶部右侧操作区与下方 OCR 面板的列宽一一对应。

        做法是把「历史下拉」的宽度反推出来，而不是另加一个容器控件去框住
        操作区：面板宽度减去操作区里所有固定开销（见
        :data:`_ADDR_ACTIONS_CHROME`），剩下的就是下拉该占的宽度。这样
        操作区的左边缘落在面板内容左边缘、右边缘落在连接按钮右边缘，两者
        都与面板内容对齐，且顶部**不新增任何控件**。

        宽度取自 OCR 面板的实时几何（不是 310 常量）：用户拖分隔条、缩放
        窗口、最大化、展开/收起面板都会让面板 resize，都会重新走到这里。

        收敛性：只从下往上单向取值（用面板宽度定下拉宽度），反向没有依赖，
        因此不会与 resizeEvent / splitterMoved 互相追着改；并且只在数值真的
        变化时才 setFixedWidth，避免无谓重排。
        """
        combo = getattr(self, "_recent_combo", None)
        panel = getattr(self, "_ocr_panel", None)
        if combo is None or panel is None:
            return

        # 面板收起时没有可对齐的对象，退回设计宽度：控件铺满这一段，
        # 地址输入框吃掉剩下的全部空间，既不留空白也不重叠。
        panel_width = panel.width() if panel.isVisible() else Metrics.OCR_PANEL_WIDTH
        if panel_width <= 0:
            return

        combo_width = panel_width - _ADDR_ACTIONS_CHROME
        if combo_width > 0 and combo.width() != combo_width:
            combo.setFixedWidth(combo_width)

    def _set_ocr_panel_visible(self, visible: bool) -> None:
        """展开 / 收起右侧 OCR 面板（收起时窗口最小宽度收紧，优先保留视频）。"""
        if getattr(self, "_ocr_panel", None) is None:
            return
        self._ocr_panel.setVisible(visible)
        self._expand_btn.setVisible(not visible)
        if visible:
            self._body_splitter.setSizes(
                [max(600, self.width() - Metrics.OCR_PANEL_WIDTH),
                 Metrics.OCR_PANEL_WIDTH])
        self.setMinimumWidth(
            Metrics.WINDOW_MIN_WIDTH if visible else Metrics.WINDOW_MIN_WIDTH_NARROW)
        # 顶部操作区跟随面板的展开 / 收起
        self._sync_address_columns()
        # 第二步高亮的游戏下拉框在面板里：面板一动，聚光灯跟着重新定位
        self._reposition_guide_spotlight()

    # ------------------------------------------------------------------
    # 本次会话识别历史
    # ------------------------------------------------------------------
    def _add_session_code(self, code: str) -> None:
        """把【复制成功】的房间号与时间记入右栏历史（最新在上）。

        只有复制成功才会走到这里 —— 未经确认的 OCR 读数不算"拿到过房间号"，
        记进历史会让用户以为那个号码已经被采用过。写入点在
        :meth:`_on_copy_requested` 的成功分支，不在 OCR 文本回调里。

        这里只是**给人看的**显示历史：连续重复的房间码折叠成一行、最多 50 行，
        因此它不完整。导出（:meth:`_on_export_csv_clicked`）用的不是它，
        而是同一事件另一侧的 :attr:`_copy_log` —— 别把两者合并。
        """
        if not code or getattr(self, "_session_list", None) is None:
            return
        self._drop_session_placeholder()
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"{code}   {stamp}"

        first = self._session_list.item(0)
        if first is not None and first.text().startswith(f"{code}   "):
            # 同一个房间号连续复制多次不重复堆行，只把时间更新到最新
            first.setText(text)
            return

        self._session_list.insertItem(0, text)
        while self._session_list.count() > 50:
            self._session_list.takeItem(self._session_list.count() - 1)

    def _drop_session_placeholder(self) -> None:
        """历史一旦有真实内容，就撤掉"空状态"占位项。"""
        if getattr(self, "_session_placeholder", False) and self._session_list.count():
            self._session_list.takeItem(0)
            self._session_placeholder = False

    def _show_session_placeholder(self) -> None:
        """历史为空时放一条不可选中的说明，让这块区域是"空状态"而不是"空白"。"""
        if getattr(self, "_session_list", None) is None or self._session_list.count():
            return
        item = QListWidgetItem(SESSION_EMPTY_HINT)
        item.setFlags(Qt.NoItemFlags)          # 不可选中、不可点击
        item.setForeground(QColor(Palette.TEXT_MUTED))
        self._session_list.addItem(item)
        self._session_placeholder = True

    def _clear_session_codes(self) -> None:
        if getattr(self, "_session_list", None) is not None:
            self._session_list.clear()
            self._session_placeholder = False
            self._show_session_placeholder()

    # ------------------------------------------------------------------
    # 房间码流水导出（CSV）
    # ------------------------------------------------------------------
    def _on_export_csv_clicked(self) -> None:
        """把本次运行中【每一次成功复制】的房间码导出成 CSV。

        导出的是 :attr:`_copy_log`（事件流水），不是右栏那份去重后的显示历史。
        导出**不清空也不改动**记录：可以反复导出，每次内容只取决于当时的记录。
        无记录时只提示，不打开保存对话框 —— 先让人选路径再告诉人没东西可存，
        顺序是反的。
        """
        if not self._copy_log:
            QMessageBox.information(
                self, "导出 CSV", "本次运行还没有成功复制过房间码，暂无可导出的记录。")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出房间码记录", self._default_export_path("LiveLens-房间码"),
            "CSV 文件 (*.csv)")

        if not path:
            # 用户取消：记录原样保留，状态栏也不改（这不是一次失败）
            logger.info("[COPY-LOG] 用户取消导出")
            return

        if not path.lower().endswith(".csv"):
            # 手输文件名时不一定自动补后缀，缺了后缀 Excel 双击认不出来
            path += ".csv"

        try:
            count = self._copy_log.export_csv(path)
        except OSError as exc:
            logger.error("[COPY-LOG] 导出失败: %s", exc)
            QMessageBox.warning(self, "导出 CSV", f"导出失败：{exc}")
            return

        logger.info("[COPY-LOG] 导出成功: %d 条 -> %s", count, path)
        self.statusBar().showMessage(f"已导出 {count} 条房间码记录：{path}")

    def _default_export_path(self, name_prefix: str) -> str:
        """导出对话框的默认文件名（``<前缀>-<时间戳>.csv``），落在用户的文档目录。

        两份导出（房间码记录 / OCR 诊断）共用这一段，只有前缀不同——
        文件名里带用途前缀，用户一眼能分清哪个文件是哪个。

        不从当前工作目录起手：打包后那可能是安装目录（往往不可写），
        让用户在一个存不进去的地方开始选路径是很差的默认。
        """
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        if not base:
            base = os.path.expanduser("~")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return os.path.join(base, f"{name_prefix}-{stamp}.csv")

    @staticmethod
    def _default_sample_dir() -> str:
        """ROI 样本的落盘目录：``<文档>/LiveLens-OCR样本/<时间戳>``。

        与导出 CSV 用同一个可写位置（打包后仓库目录可能是只读的），
        但**不加文件后缀**：这是一个目录。
        """
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        if not base:
            base = os.path.expanduser("~")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return os.path.join(base, "LiveLens-OCR样本", stamp)

    # ------------------------------------------------------------------
    # OCR 诊断日志（默认关闭）
    # ------------------------------------------------------------------
    def _on_diag_toggled(self) -> None:
        """开启 / 关闭诊断记录。

        只切换记录与否，不动 OCR 的任何规则、间隔或线程：开关本身不重启 OCR，
        开启后**接下来**的执行才被记录，已有记录一律保留。
        """
        enabled = not self._diagnostics.enabled
        self._diagnostics.set_enabled(enabled)
        self._diag_btn.setText("停止诊断" if enabled else "开始诊断")
        if not enabled and self._diag_obs_check.isChecked():
            # 诊断关了就无从「看」起：同步清掉勾选，避免界面显示与实际行为
            # 不一致（识别器那边同样会因为记录器关闭而停止补跑）。
            # 用 blockSignals 抑制重入，勾选状态由这里独占改写。
            self._diag_obs_check.blockSignals(True)
            self._diag_obs_check.setChecked(False)
            self._diag_obs_check.blockSignals(False)
        self._update_diag_controls()
        if enabled:
            self.statusBar().showMessage(
                "OCR 诊断日志已开启（只记录，不影响识别）：可从右栏「导出诊断 CSV」导出")
        else:
            self.statusBar().showMessage("OCR 诊断日志已关闭，已记录的内容仍可导出")
        logger.info("[DIAG] 诊断记录已%s", "开启" if enabled else "关闭")

    def _on_diag_observe_toggled(self, checked: bool) -> None:
        """「全 PSM 观测」勾选变化。

        只决定「下一次开始识别时，识别器要不要补跑其余的 PSM」。识别器在
        构造时取值，所以改动对**正在运行**的那一轮不生效 —— 运行期间锁定该
        复选框（见 _update_diag_controls），改动一律走「停止 → 改 → 重新开始」。
        """
        if checked and not self._diagnostics.enabled:
            # 未开诊断时复选框本就是禁用的，这里只是兜底：不接受该状态。
            self._diag_obs_check.blockSignals(True)
            self._diag_obs_check.setChecked(False)
            self._diag_obs_check.blockSignals(False)
            return
        logger.info("[DIAG] 全 PSM 观测已%s", "开启" if checked else "关闭")
        if checked:
            self.statusBar().showMessage(
                "全 PSM 观测已开启：下次开始识别起生效（补跑的读法只进诊断，"
                "不影响识别与复制）")
        else:
            self.statusBar().showMessage("全 PSM 观测已关闭")

    def _on_sample_capture_toggled(self, checked: bool) -> None:
        """「采集 ROI 样本」勾选变化。

        采集是在识别线程里按帧判断开关的，因此这里改了**立刻生效**，
        不需要重启 OCR，也不会改变任何识别结果与复制行为。
        """
        self._sample_capture.set_enabled(checked)
        logger.info("[CAPTURE] ROI 样本采集已%s", "开启" if checked else "关闭")
        if checked:
            self.statusBar().showMessage(
                f"开始采集 ROI 样本（上限 {self._sample_capture.max_samples} 张）："
                f"{self._sample_capture.directory}")
        else:
            self.statusBar().showMessage(self._sample_capture.summary())

    def _update_diag_controls(self) -> None:
        """诊断相关控件的可用性。

        规则（与产品约定一致）：
            - 只有诊断开启时才允许勾选「全 PSM 观测」；
            - 识别运行期间一律锁定，停止后恢复。
        只改可用性，不动勾选状态本身：勾选状态是用户的意图，由用户自己
        （或在关闭诊断时由 _on_diag_toggled）清除。
        """
        self._diag_obs_check.setEnabled(
            self._diagnostics.enabled and not self._ocr_running
        )

    def _on_export_diagnostics_clicked(self) -> None:
        """导出 OCR 诊断日志 CSV（与房间码记录 CSV 完全分开的两份文件）。

        与房间码导出一样：导出不清空、不改动记录，可反复导出；
        没有记录时只提示，不打开保存对话框。
        """
        if not self._diagnostics:
            QMessageBox.information(
                self, "导出诊断 CSV",
                "本次运行还没有诊断记录。诊断默认关闭，请先点「开始诊断」，"
                "再让 OCR 跑一会儿。")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出 OCR 诊断日志", self._default_export_path("LiveLens-OCR诊断"),
            "CSV 文件 (*.csv)")

        if not path:
            logger.info("[DIAG] 用户取消导出诊断")
            return

        if not path.lower().endswith(".csv"):
            # 手输文件名时不一定自动补后缀，缺了后缀 Excel 双击认不出来
            path += ".csv"

        try:
            count = self._diagnostics.export_csv(path)
        except OSError as exc:
            logger.error("[DIAG] 导出失败: %s", exc)
            QMessageBox.warning(self, "导出诊断 CSV", f"导出失败：{exc}")
            return

        logger.info("[DIAG] 导出成功: %d 条 -> %s", count, path)
        # 记录有上限（默认 30000 条），溢出时丢的是【最早】的记录。
        # 这里如实告知丢了多少 —— 不能让用户以为 CSV 就是完整的一段。
        dropped = self._diagnostics.dropped
        if dropped:
            self.statusBar().showMessage(
                f"已导出 {count} 条诊断记录（因内存上限已丢弃最早的 {dropped} 条）：{path}")
        else:
            self.statusBar().showMessage(f"已导出 {count} 条诊断记录：{path}")


    # ------------------------------------------------------------------
    # 左侧 Dock：配置 / 场景 + 直播源
    # ------------------------------------------------------------------
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

        # 引导入口。刻意用次要样式（不带 variant="primary"）：状态栏上
        # 「激活许可证」应当是唯一被强调的按钮。放在它左边，保持许可证在最右。
        self._help_btn = QPushButton("使用引导")
        self._help_btn.setObjectName("statusButton")
        self._help_btn.setStatusTip("查看使用引导，可随时重新打开")
        self._help_btn.clicked.connect(self._on_help_clicked)
        bar.addPermanentWidget(self._help_btn)

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
        """识别结果框：有结果时点亮，空结果回到明确的占位文案。"""
        self._lbl_ocr_text.setText(text if text else OCR_RESULT_PLACEHOLDER)
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
        """跟随窗口状态切换标题栏按钮字形。

        最大化与全屏现在是两个独立入口，因此两个按钮各自只看自己的那个状态：
        原先把全屏也算进最大化按钮，会让"全屏中再点最大化"出现字形与行为不符。
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            # 全屏时停用无边框的边缘缩放：顶部缩放热区会盖住标题栏滑出热区
            if getattr(self, "_frameless", None) is not None:
                self._frameless.set_fullscreen(self.isFullScreen())
            self._title_bar.max_button.set_kind(
                WindowButton.RESTORE if self.isMaximized() else WindowButton.MAX)
            if getattr(self, "_fullscreen_btn", None) is not None:
                self._fullscreen_btn.set_kind(
                    IconButton.FULLSCREEN_EXIT if self.isFullScreen()
                    else IconButton.FULLSCREEN)

    # ------------------------------------------------------------------
    # 最近连接的流管理
    # ------------------------------------------------------------------
    def _load_recent_streams(self):
        """加载最近连接的流列表。

        填表期间屏蔽信号：QComboBox 会自动选中第一项并发出 currentIndexChanged，
        那会让"刷新历史"顺带把某个历史地址写进输入框、并把下拉显示成一个地址。
        填完复位到 -1，下拉平时只显示「历史记录」占位文案。
        """
        streams = self._stream_history.get_all()
        self._recent_combo.blockSignals(True)
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

        self._recent_combo.setCurrentIndex(-1)
        self._recent_combo.blockSignals(False)
        self._sync_note_action()

    def _sync_note_action(self, *_args):
        """备注动作跟随【地址输入框】的内容，而不是跟随历史下拉。

        原先只在"选中过历史项"时才可用，直接粘贴地址反而不能加备注；
        现在地址输入框是唯一事实来源，历史下拉只是往它里面写字。
        """
        if getattr(self, "_edit_note_action", None) is None:
            return
        url = self._url_input.text().strip()
        self._selected_url_for_note = url or None
        self._edit_note_action.setEnabled(bool(url))

    def _extract_room_id(self, url: str) -> str:
        """从URL提取房间ID（仅用于最近记录列表的显示文本）

        按路径取房间标识，不判断具体平台 —— 直播间 URL 的房间号就在第一段
        路径上（如 https://live.douyin.com/123456789 -> 123456789）。
        """
        try:
            return urlparse(url).path.strip("/").split("/")[0]
        except Exception:
            return ""

    def _on_recent_selected(self, index: int):
        """选择最近连接的流：把 URL 写进地址输入框，随后立刻复位下拉。

        复位是刻意的：地址栏里不允许存在两个同时显示直播地址的区域。
        写入输入框会触发 textChanged → _sync_note_action，备注因此自动可用。
        """
        if index < 0:
            return
        url = self._recent_combo.itemData(index)
        if not url:
            return
        self._url_input.setText(url)
        self._recent_combo.blockSignals(True)
        self._recent_combo.setCurrentIndex(-1)
        self._recent_combo.blockSignals(False)

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
            self._sync_note_action()
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
            resolver=self._resolver,
        )
        self._connect_worker.start()

    def _on_connect_success(self, info: StreamInfo):
        """连接成功回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_success called (bg thread), emitting _connect_success_sig")
        self._connect_success_sig.emit(info)

    def _handle_connect_success(self, info: StreamInfo):
        """在 GUI 线程中处理连接成功：建立候选迭代器，启动第一个候选"""
        self.statusBar().showMessage(
            f"已获取流地址 (room={info.room_id}, type={info.protocol})，"
            f"正在连接视频流..."
        )
        logger.info(
            "[GUI] _handle_connect_success: room_id=%s, type=%s, candidates=%d, url=%s",
            info.room_id, info.protocol, len(info.candidates or ()),
            (info.url or "")[:80],
        )

        # Phase 2.5：本次会话的候选迭代器。候选顺序**完全由平台适配器给出**
        # （适配器里已按平台自己的偏好排好序），这里只负责"按顺序逐个试"，
        # 不重排、不评分、不测速，也不认识任何具体平台。
        self._failover = CandidateFailover(info)
        self._playback_info = info

        if not self._failover_next():
            self._failover_exhausted()
            return

        self._apply_connected_ui()

    # ------------------------------------------------------------------
    # Phase 2.5：候选流 failover
    # ------------------------------------------------------------------
    def _start_playback(self, info: StreamInfo, candidate) -> bool:
        """用给定候选启动一次播放（reader → buffer → perf → bridge → 定时器）。

        这是**唯一**的播放初始化入口：首次连接与 failover 切换都走这里，
        不存在第二套初始化流程。

        刻意不触碰 `_connected` —— failover 期间整条连接会话仍属于
        connected 状态，这里只重建"这一次尝试"的播放资源。

        调用前必须确保上一次尝试已由 `_teardown_playback()` 清理干净。

        :return: True = reader 已启动；False = 同步启动失败（构造/起线程异常）
        """
        # 本次尝试的代号。任何在本函数返回之后才到达的迟到回调，都可以凭
        # attempt_id 的变化判定自己属于旧 attempt 并放弃。
        self._attempt_id += 1

        # Phase 3.2：本次尝试的启动分段计时。t0 = 决定试这个候选的那一刻；
        # 覆盖构造器内的 ffprobe、FFmpeg 启动、首帧到达，直到 GUI 首次取到帧。
        # 计时只记录事实，不改变下面任何一步的行为。
        timing = StartupTiming()
        timing.note(f"attempt={self._attempt_id}")
        if self._failover is not None:
            timing.note(f"candidate={self._failover.attempt_number}/{self._failover.total}")
        timing.note(f"proto={candidate.protocol}")
        timing.mark(STAGE_CANDIDATE_SELECTED)
        self._startup_timing = timing

        # 分辨率规则（平台无关）：只有平台真正探测过的那一个候选
        # （info.selected）才带可信分辨率，其余候选一律交给 FFmpegReader
        # 自己探测。**绝不能**把未知候选硬编码成 1920x1080 —— 那会让
        # FFmpegReader 判定"分辨率已知"从而跳过自动探测，最终按错误尺寸切帧。
        if candidate is info.selected and info.width and info.height:
            video_width, video_height = info.width, info.height
        else:
            video_width, video_height = None, None

        logger.info(
            "[GUI] _start_playback: attempt=%d room=%s type=%s size=%s url=%s",
            self._attempt_id, info.room_id, candidate.protocol,
            f"{video_width}x{video_height}" if video_width else "auto-detect",
            (candidate.url or "")[:80],
        )

        # 平台提供的媒体请求头（如虎牙要求特定 User-Agent），随候选一起由
        # 适配器给出。这里只转交，不判断平台、不解释字段含义。
        media_headers = candidate.http_headers or None

        # 创建并启动 FFmpegReader
        reader = None
        try:
            reader = FFmpegReader(
                candidate.url,
                width=video_width,
                height=video_height,
                use_cuda=True,
                http_headers=media_headers,
                timing=timing,
            )
            reader.start()
            logger.info("[FFMPEG] reader.start() called, use_cuda=True")
        except Exception as e:
            logger.error("[FFMPEG] reader.start() FAILED (attempt=%d): %s",
                         self._attempt_id, e)
            # Phase 3.2：这次尝试到此为止（不会再有首帧/显示），把已有的
            # 分段耗时如实输出，否则失败候选的探测代价会凭空消失
            timing.log()
            # 只清理这个半成品 reader，绝不动其它状态：
            # 是否换候选由调用方（_failover_next）决定。
            if reader is not None:
                try:
                    reader.stop()
                except Exception:  # noqa: BLE001 - 清理失败不应影响换候选
                    pass
            return False

        self._reader = reader

        # 创建 LatestFrameBuffer
        self._frame_buffer = LatestFrameBuffer()
        logger.info("[BUFFER] LatestFrameBuffer created")

        # 创建 PerformanceMonitor 并启动
        self._perf = PerformanceMonitor()
        self._perf.start()

        # Phase B-1：把 set_frame / paintEvent 的耗时观测挂到视频控件上。
        # 只汇总统计，不改变任何渲染行为。
        self._video_widget.attach_monitor(self._perf)

        # 创建并启动 FrameBridge: reader → buffer (+ perf.on_capture)
        self._frame_bridge = _FrameBridge(self._reader, self._frame_buffer, self._perf)
        # 先接线再启动：确保 bridge 一开始发通知时消费者已经就位。
        # 通知器随 bridge 新建，重连/换候选即换新对象，不存在重复连接。
        self._frame_notifier = self._frame_bridge.notifier
        self._frame_notifier.frameReady.connect(self._on_frame_ready)
        self._frame_bridge.start()
        logger.info("[BRIDGE] FrameBridge.start() called (frameReady 通知驱动)")

        # 消费位置复位：buffer 是新建的，frame_id 从 0 重新起算
        self._last_displayed_frame_id = 0

        # 启动连接健康检查定时器（不再消费帧；帧消费由 frameReady 通知驱动）
        self._frame_timer.start(16)
        # 启动状态栏刷新（每秒更新一次）
        self._status_timer.start(1000)
        logger.info(
            "[GUI] health timer started (16ms), status_timer started (1000ms); "
            "frames are frameReady-driven"
        )
        return True

    def _apply_connected_ui(self):
        """把界面切到"已连接"状态（纯 UI，不创建/销毁任何播放资源）。

        因为不碰资源，所以可以在每次 failover 切成功之后重复调用（幂等）。
        """
        self._connected = True
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("断开")
        # 连接后主按钮语义从「执行」变为「中断」，用危险色把这一点说清楚
        set_property(self._connect_btn, "variant", "danger")
        self._url_input.setEnabled(False)
        # 连接后启用 ROI 选择（访问被拒绝时保持禁用）
        self._roi_btn.setEnabled(not self._access_status.is_expired())

        # 隐藏连接状态消息
        self._connection_status_label.hide()

        # 更新性能面板（分辨率随候选更新，尽量反映当前实际播放的那一路）。
        # 尺寸未知的候选（Phase 3.3）此刻还没探测完：显示"--"而不是 "None × None"，
        # 真正的数值会在首帧到达后由每秒一次的状态刷新补上（见 _on_status_timeout）。
        if self._reader is not None:
            if self._reader.width and self._reader.height:
                self._set_chip(
                    self._lbl_resolution,
                    f"{self._reader.width} × {self._reader.height}",
                )
            else:
                self._set_chip(self._lbl_resolution, "分辨率 --")
        self._set_chip(self._lbl_status, "连接中", "warn", dot=True)

        # 更新 OCR 按钮状态
        self._update_ocr_button_state()

        self._sync_note_action()

        # 第一步聚光灯：连接成功了才自动往下走。
        # 放在方法末尾，让"已连接"这版界面先落定再谈引导。
        # 只在第一步这样推进 —— 连接成功与第二步（选游戏）无关。
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 0:
            spotlight.notify_completed()

    def _failover_next(self) -> bool:
        """把 failover 推进到下一个候选并启动它。

        同步启动失败（构造/起线程异常）会就地结算该候选并继续往后试，
        因此一次调用可能连续尝试多个候选，直到有一个真的启动成功、
        或候选全部耗尽。

        :return: True = 已成功启动一个候选；False = 候选耗尽 / 已取消 /
                 没有 failover 上下文（调用方应结束本次连接）
        """
        failover = self._failover
        info = self._playback_info
        if failover is None or info is None:
            return False

        while True:
            candidate = failover.next_candidate()
            if candidate is None:
                return False
            if self._start_playback(info, candidate):
                return True
            # note_failure 是幂等的：连续调用不会让下一次 next_candidate
            # 跳过两个候选，因此这里的循环不会吞掉中间候选。
            failover.note_failure()

    def _failover_exhausted(self):
        """所有候选都试过且都起不来：结束整条连接"""
        logger.warning("[FAILOVER] 全部候选均启动失败，结束连接")
        self._disconnect()
        # 写在 _disconnect() 之后：否则会被它结尾的"已断开"覆盖掉
        self.statusBar().showMessage("连接失败：所有候选流均不可用")

    def _handle_playback_failure(self, status_message: str):
        """一次播放尝试失败：先判断该不该换候选，再决定是否结束连接。

        阶段规则：**只处理"从未成功播放"的候选**。曾经出过首帧
        （frames_read > 0 或 first_frame_ms 非空）说明该候选启动成功了，
        它之后的断流属于直播结束/网络中断 —— 本阶段不自动换候选、
        不自动重连，保持既有行为不变。
        """
        # 迟到回调保护：本函数执行期间若 attempt 已被换掉（断开 / 关闭窗口），
        # 就绝不能再启动任何 reader。
        attempt = self._attempt_id

        reader = self._reader
        ever_played = False
        if reader is not None:
            ever_played = (
                reader.frames_read > 0 or reader.first_frame_ms is not None
            )

        if ever_played or self._failover is None:
            # 曾经成功播放过（或没有 failover 上下文）：保持既有行为
            self.statusBar().showMessage(status_message)
            self._disconnect()
            return

        # 从未成功 → 该候选不可用：结算它、清理它，再试下一个
        self._failover.note_failure()

        t0 = time.perf_counter()
        self._teardown_playback()

        # _teardown_playback() 本身不改变 attempt_id（也不修改 _connected）；
        # 这里显式校验一次，保证将来任何异步清理实现都不会让旧 attempt
        # 在清理之后启动新 reader。
        if self._attempt_id != attempt:
            logger.warning("[FAILOVER] attempt %d 已失效，放弃切换候选", attempt)
            return

        if self._failover_next():
            self._last_failover_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "[FAILOVER] attempt %d 失败 → 切换到候选 %d/%d，耗时 %.0f ms",
                attempt, self._failover.attempt_number, self._failover.total,
                self._last_failover_ms,
            )
            # 会话仍是 connected：只刷新 UI，不进入断开流程
            self._apply_connected_ui()
            self.statusBar().showMessage(
                f"已切换到备用线路 ({self._failover.attempt_number}/"
                f"{self._failover.total})，正在连接视频流..."
            )
            return

        self._last_failover_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning("[FAILOVER] attempt %d 失败，且已无更多候选可用", attempt)
        self._disconnect()
        # 同上：写在 _disconnect() 之后才不会被"已断开"覆盖
        self.statusBar().showMessage("连接失败：所有候选流均不可用")

    def _on_connect_error(self, error_msg: str):
        """连接失败回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_error called (bg thread), emitting _connect_error_sig")
        self._connect_error_sig.emit(error_msg)

    def _handle_connect_error(self, error_msg: str):
        """在 GUI 线程中处理连接失败"""
        self._reset_connect_ui()
        self.statusBar().showMessage(f"连接失败: {error_msg}")
        logger.warning("连接失败: %s", error_msg)

    def _teardown_playback(self):
        """释放"当前这一次播放尝试"的全部资源。

        刻意**不修改 `_connected`**，也不碰 OCR / ROI / 备注选中：
        failover 期间整条连接会话仍然属于 connected 状态，这里只是把这一次
        尝试拆掉，好让下一次尝试复用同一套 UI 状态。

        必须可以重复调用，也必须可以在一次从未成功启动的尝试上调用
        （所有访问都是空值安全的）。
        """
        # Phase 3.2：一次尝试到此结束，兜底输出启动分段汇总（已输出过则是
        # no-op）。覆盖"reader 起来了但从未出帧"的情况 —— 否则这组耗时只会在
        # 内存里等着被下一次尝试覆盖掉。
        if self._startup_timing is not None:
            self._startup_timing.log()

        # 停止定时器（下一次尝试会在 _start_playback() 里重新启动）
        self._frame_timer.stop()
        self._status_timer.stop()

        # 顺序很重要：先停 bridge 线程（不再产生新通知），再断开通知器信号
        # （已经排进事件队列的通知不再投递给 GUI），最后清空引用。
        # 下一次尝试必然是全新 notifier，绝不复用旧对象。
        if self._frame_bridge is not None:
            self._frame_bridge.stop()
            self._frame_bridge = None

        notifier = self._frame_notifier
        if notifier is not None:
            try:
                notifier.frameReady.disconnect(self._on_frame_ready)
            except (RuntimeError, TypeError):
                pass        # 从未连接 / 已断开：无需处理
            notifier.clear_pending()
            self._frame_notifier = None

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
            # 先解挂，避免控件继续写入已废弃的监控对象
            self._video_widget.attach_monitor(None)
            self._perf.reset()
            self._perf = None

    def _disconnect(self):
        """断开连接，停止一切"""
        # 先让所有在途 attempt 失效：任何排队中的回调都不会再启动 reader
        self._attempt_id += 1

        # 取消并丢弃候选迭代器：本会话不再尝试任何候选
        if self._failover is not None:
            self._failover.cancel()
            self._failover = None
        self._playback_info = None

        # 停止 OCR
        # 注意：这一步只在整条会话结束时做。failover 切换候选不走这里 ——
        # OCRWorker 通过 lambda 晚绑定读取 _frame_buffer，换 buffer 无需重启，
        # 用户的 ROI 与游戏档也应当保留。
        self._stop_ocr()

        # 释放播放资源（reader / buffer / perf / bridge / notifier / 定时器）
        self._teardown_playback()

        # 清除画面
        self._video_widget.clear_frame()
        # 隐藏ROI框
        self._video_widget.clear_roi()

        self._sync_note_action()

        # 重置 UI
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
        self._sync_note_action()

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

        # 第三步聚光灯：框选期间收起遮罩与卡片，让画面独占交互
        # （框选窗口是独立的最大化窗口，浮层留在屏幕上只会是干扰）
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 2:
            spotlight.suspend()

        # 创建并显示选择窗口
        self._roi_selector = ROISelector(frame, parent=self)
        self._roi_selector.roi_selected.connect(self._on_roi_selected)
        self._roi_selector.roi_selection_canceled.connect(self._on_roi_canceled)
        self._roi_selector.selection_drawn.connect(self._on_roi_selection_drawn)
        self._roi_selector.show()

    def _on_roi_selection_drawn(self, rect):
        """鼠标松开、画面上出现选区：显示轻量提示，**不**推进任何流程。

        只有按 Enter 才算确认（见 _on_roi_selected）—— 松开鼠标只是框好了，
        用户还可能继续调整或直接 Esc 取消。
        """
        self._hide_roi_hint()
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 2:
            spotlight.show_roi_hint(rect)

    def _hide_roi_hint(self) -> None:
        spotlight = self._guide_spotlight
        if spotlight is not None:
            spotlight.hide_roi_hint()

    def _on_roi_selected(self, roi: ROI):
        """ROI 选择确认回调"""
        self._hide_roi_hint()
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

        # 第三步聚光灯：Enter 确认成功才自动进第四步
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 2:
            spotlight.notify_completed()

    def _on_roi_canceled(self):
        """ROI 选择取消回调（不改变旧 ROI）"""
        self._hide_roi_hint()
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

        # 第三步聚光灯：Esc 取消后留在本步，把遮罩与卡片恢复回来
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 2:
            spotlight.resume()

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

        # 第二步聚光灯：用户真选了一次游戏就自动推进。
        # 只认第二步 —— 构造期与其它路径也会触发本回调，不能无条件推进。
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 1:
            spotlight.notify_completed()

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

        # 创建 OCR 工作线程（带上当前选择的游戏档）。
        # 观测开关每次都从复选框现读：每轮识别都是新建 worker + 新建识别器，
        # 因此「停止 → 取消勾选 → 重新开始」不会残留上一轮的观测状态。
        self._ocr_worker = OCRWorker(
            frame_buffer_getter=lambda: self._frame_buffer,
            roi_manager=self._roi_manager,
            interval_ms=300,
            profile=self._ocr_profile_id,
            diagnostics=self._diagnostics,
            observe_all_psm=self._diag_obs_check.isChecked(),
            sample_capture=self._sample_capture,
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
        # 单独再解锁一次：即便 finished 信号没到（停止路径异常），
        # 「全 PSM 观测」也必须恢复可编辑，不能永久留在锁定态。
        self._update_diag_controls()
        self.statusBar().showMessage("OCR 识别已停止")

    def _on_ocr_started(self):
        """OCR 线程启动回调"""
        self._ocr_running = True
        self._ocr_btn.setText("识别中")
        self._set_ocr_state(True)
        self._sync_ocr_button_variant()
        self._update_diag_controls()          # 运行期间锁定「全 PSM 观测」
        self.statusBar().showMessage("OCR 识别已启动")

        # 第四步聚光灯：用户真的把 OCR 跑起来了，引导随之完成。
        # 只认第四步 —— 其它入口（快捷键 / 之后的日常使用）不该动引导。
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 3:
            spotlight.notify_completed()

    def _on_ocr_finished(self):
        """OCR 线程结束回调"""
        self._ocr_running = False
        self._ocr_btn.setText("开始OCR")
        self._set_ocr_state(False)
        self._sync_ocr_button_variant()
        self._update_diag_controls()          # 停止后恢复可编辑

    def _on_ocr_text_updated(self, text: str):
        """OCR 文本更新回调。

        刻意【不】写本次会话历史：这里只是"这一帧读到了什么"，还没经过复制确认。
        历史记录改由 _on_copy_requested 的成功分支写入（Phase UI-3.1）。
        """
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
            # 只有真正写进剪贴板的房间号才算"本次会话拿到过"，记入右栏历史
            self._add_session_code(text)
            # 同一次事件也记进导出用的流水。这里是**唯一**的写入点：
            # 复制失败、未确认的读数都走不到这一行，所以 CSV 必然只反映
            # 实际发生的复制。两个记录刻意分开 —— 右栏历史会去重、有上限，
            # 用它当导出源会丢事件。
            self._copy_log.record(text)
            # 诊断：把"真的写进剪贴板了"回填给请求它的那次 OCR 执行。
            self._diagnostics.note_copy_success(text)
        else:
            logger.error("[CLIPBOARD] copy failed reason: %s", self._clipboard_manager.last_error or "unknown")
            self._set_chip(self._lbl_clipboard, "剪贴板 ✕", "error", dot=True)
            self._clipboard_chip_timer.start(2000)
            self.statusBar().showMessage(f"复制失败: {self._clipboard_manager.last_error or '未知错误'}")
            # 诊断：区分「写入失败」（有错误信息）与「被剪贴板去重过滤」
            # （没有错误信息）—— 后者并没有写进剪贴板，不能算成功。
            if self._clipboard_manager.last_error:
                self._diagnostics.note_copy_failure(
                    text, self._clipboard_manager.last_error)
            else:
                self._diagnostics.note_copy_skipped(text)

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
    # 帧消费：由 frameReady 通知驱动（Phase B-2）
    # ------------------------------------------------------------------
    def _on_frame_ready(self):
        """帧到达通知（GUI 线程）：消费当前最新帧

        信号不携带帧数据，因此这里重新读取 LatestFrameBuffer 的当前最新帧，
        永远不会消费到旧帧；处理期间到达的帧会被单槽覆盖并合并成一次新通知。

        Phase 2.5：只接受"当前这一次尝试"的通知器。旧 notifier 若还有已经
        排进 Qt 事件队列、来不及被 disconnect 拦下的唤醒，会在这里被丢弃，
        不会去动新尝试的 buffer / pending 状态。
        （直接调用本方法时没有 sender，此时不做该判断，保持既有语义。）
        """
        sender = self.sender()
        if sender is not None and sender is not self._frame_notifier:
            logger.debug("[BRIDGE] 丢弃旧通知器的迟到唤醒: %r", sender)
            return

        notifier = self._frame_notifier
        if notifier is not None:
            # 先放行下一帧的唤醒再消费：消费耗时内到达的帧仍能触发下一轮
            notifier.clear_pending()
        self._consume_latest_frame()

    def _consume_latest_frame(self):
        """取 LatestFrameBuffer 的当前最新帧并显示（仅在出现新帧时）"""
        if self._reader is None or self._frame_buffer is None:
            return

        # 从 buffer 获取最新帧（仅当有新帧时才刷新显示）
        buffer_frame_id = self._frame_buffer.frame_id
        if buffer_frame_id != self._last_displayed_frame_id:
            frame = self._frame_buffer.get()
            if frame is not None:
                # Phase 3.2：本次尝试的首帧到达 GUI。判定依据是"本次尝试
                # 还没显示过任何帧"，与 _start_playback 对计数的复位一致。
                if self._last_displayed_frame_id == 0:
                    self._mark_first_display()
                # 记录「帧到达 buffer → 被 GUI 取走」的等待。
                # 必须放在 set_frame 之前：否则会把本帧的渲染耗时（毫秒级）
                # 也算进延迟指标，使读数变成本地渲染成本而非消费等待。
                # 渲染耗时另有 set_frame_p50/p95 单独统计。
                if self._perf is not None:
                    self._perf.on_display()
                self._video_widget.set_frame(frame)
                # 隐藏连接状态消息（当有帧时）
                self._connection_status_label.hide()
                self._last_displayed_frame_id = buffer_frame_id
                if buffer_frame_id <= 3 or buffer_frame_id % 100 == 0:
                    logger.info(
                        "[DISPLAY] set_frame called: buf_id=%d shape=%s",
                        buffer_frame_id, frame.shape,
                    )

    def _mark_first_display(self):
        """Phase 3.2：记录本次尝试的首帧已被 GUI 取走，并输出启动分段汇总。

        标记点是「取到帧、即将 set_frame」的那一刻，刻意**不含渲染耗时**；
        真正的上屏时刻需要在渲染路径里挂钩子，超出本阶段范围（本阶段不改
        GUI 帧处理逻辑），因此这里只声明到"交付给控件"为止。

        :meth:`StartupTiming.log` 幂等，重复调用只会有一次输出。
        """
        timing = self._startup_timing
        if timing is None:
            return
        timing.mark(STAGE_FIRST_DISPLAY)
        timing.log()

    # ------------------------------------------------------------------
    # 连接健康检查（QTimer 驱动；不再负责帧消费）
    # ------------------------------------------------------------------
    def _on_frame_timeout(self):
        """每 16ms 被 QTimer 调用：只做 reader 健康检查，不再消费帧

        帧消费已改由 frameReady 通知驱动（见 _on_frame_ready）。
        该定时器保留的唯一职责是及时发现断流/直播结束，
        避免画面停住却无人报错。
        """
        if self._reader is None or self._frame_buffer is None:
            logger.warning("[DISPLAY] health timer fired but reader=%s buffer=%s, stopping timer",
                           self._reader is not None, self._frame_buffer is not None)
            self._frame_timer.stop()
            return

        # 检查读取器错误
        if self._reader.error:
            logger.error("[DISPLAY] reader.error detected: %s", self._reader.error)
            # Phase 2.5：失败后是否换候选由 _handle_playback_failure 判定
            # （从未成功播放 → 试下一个候选；成功播放过 → 保持既有断开行为）
            self._handle_playback_failure(f"视频错误: {self._reader.error}")
            return

        # 读取器已停止但无错误（直播正常结束）
        if not self._reader.is_running and not self._reader.connected:
            logger.info("[DISPLAY] reader stopped, not connected -> disconnect")
            self._handle_playback_failure("直播已结束")
            return

    # ------------------------------------------------------------------
    # 状态栏 + 性能面板刷新（低频，1秒一次）
    # ------------------------------------------------------------------
    def _on_status_timeout(self):
        """每秒刷新状态栏和性能面板"""
        if self._reader is None or self._perf is None:
            return

        # Phase B-1：在既有刷新周期内汇总观测样本（不产生逐帧日志）。
        # 三个采样分别对应：输入给 GUI 的帧率、跳帧速率、管道积压。
        if self._frame_buffer is not None:
            self._perf.sample_produced(self._frame_buffer.frame_id)
        self._perf.sample_skipped(self._reader.frames_skipped)
        self._perf.set_pipe_stats(self._reader.pipe_backlog_stats())

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

        # 底部性能栏：分组展示已有指标；没有数据源的一律显示 "—"
        if self._reader.connected:
            read_frames = self._reader.frames_read
            disp_frames = snap["display_frames"]
            runtime = snap["runtime_s"]
            skipped = self._reader.frames_skipped
            first_ms = self._reader.first_frame_ms
            first_str = f"{first_ms:.0f}ms" if first_ms else "--"

            # Phase B-1：本地链路观测读数。注意这些数字只描述
            # FFmpeg 输出 → 管道 → buffer → set_frame → paintEvent，
            # 不是抖音直播端到端延迟。
            pipe = snap["pipe"]
            self._set_perf("latency",
                           f"延迟 {cur_lat:.0f}ms" if cur_lat > 0 else "延迟 —")
            self._set_perf("first", f"首帧 {first_str}")
            self._set_perf("skip", f"跳帧 {skipped}")
            self._set_perf("pipe", f"管道 {pipe.get('last', 0) / 1024.0:.0f}KB")
            self._set_perf("decode_fps",
                           f"解码 {read_frames / runtime:.0f}fps" if runtime > 0
                           else "解码 —")
            self._set_perf("input_fps", f"输入 {snap['produced_fps']:.0f}fps")
            self._set_perf("set_frame",
                           f"set_frame {snap['set_frame_p50']:.1f}/"
                           f"{snap['set_frame_p95']:.1f}ms")
            self._set_perf("paint",
                           f"paint {snap['paint_p50']:.1f}/"
                           f"{snap['paint_p95']:.1f}ms")
            worker = self._ocr_worker
            if worker is not None:
                self._set_perf("ocr_frames", f"帧数 {worker.recognition_count}")
            self._set_perf("ocr_cost", "耗时 —")   # 当前没有 OCR 耗时测量源
        else:
            for key in ("latency", "first", "skip", "pipe", "decode_fps",
                        "input_fps", "set_frame", "paint", "ocr_frames"):
                self._perf_values[key].setText(self._perf_defaults.get(key, "—"))

            # 延迟诊断日志（每5秒输出一次）
            if runtime > 0 and int(runtime) % 5 == 0 and int(runtime) != int(runtime - 1):
                logger.info(
                    "[Latency] pipeline=%s/%sms decode_fps=%.0f display_fps=%.1f "
                    "frames=%d skipped=%d first_frame=%s | "
                    "B1 skip: rate=%.1f/s last=%d max=%d state=%s | "
                    "B1 pipe: last=%.1fKB p95=%.1fKB peak=%.1fKB n=%d | "
                    "B1 render: n=%d p50=%.1f p95=%.1f p99=%.1f max=%.1f ms | "
                    "B1 paint: n=%d drawn=%d p50=%.1f p95=%.1f p99=%.1f max=%.1f ms | "
                    "B1 flow: produced=%.1ffps consumed=%d changes=%d displayed=%.1ffps",
                    f"{cur_lat:.0f}" if cur_lat > 0 else "--",
                    f"{avg_lat:.0f}" if avg_lat > 0 else "--",
                    read_frames / runtime,
                    fps,
                    read_frames, skipped, first_str,
                    snap["skipped_rate"], snap["skipped_delta_last"],
                    snap["skipped_delta_max"], snap["skipped_state"],
                    pipe.get("last", 0) / 1024.0, pipe.get("p95", 0) / 1024.0,
                    pipe.get("max", 0) / 1024.0, pipe.get("samples", 0),
                    snap["set_frame_count"], snap["set_frame_p50"], snap["set_frame_p95"],
                    snap["set_frame_p99"], snap["set_frame_max"],
                    snap["paint_count"], snap["paint_drawn"], snap["paint_p50"],
                    snap["paint_p95"], snap["paint_p99"], snap["paint_max"],
                    snap["produced_fps"], snap["set_frame_count"],
                    snap["input_frame_changes"], fps,
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

        # 未激活遮罩跟随同一份状态（只影响显示）
        self._sync_activation_overlay()

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
        # 恢复出有效授权 → 这才是「已授权用户启动」这条路径的判定点。
        # 早于此刻调用只会读到"尚未读到凭据"，因此引导被推迟到这里。
        self.maybe_show_first_run_guide()

    # ------------------------------------------------------------------
    # 首次启动引导 / 帮助
    # ------------------------------------------------------------------
    def maybe_show_first_run_guide(self) -> bool:
        """在「已获得有效授权且尚未看过引导」时弹出使用引导。

        触发时机（Phase UI 微调 08）：
            - 未授权启动：**不弹**。先让用户完成授权，引导的意义是介绍一个
              他能真正使用的软件，对着未激活界面讲操作流程是无效信息。
            - 启动时已是授权用户：由 main.py 在主窗口显示后调用这里；
              授权状态由后台线程读盘得出，所以先等它落地再判定。
            - 激活 / 追加授权成功：由 _on_license_clicked / _on_license_appended
              在解锁过渡之后再次调用，这是未授权用户真正该看到引导的时刻。

        仍然刻意由入口而不是 __init__ 调用：「构造一个 MainWindow」在测试里很
        常见，构造期弹模态对话框会让无头测试永久阻塞。

        重入与重复防护（三道，缺一不可）：
            1. _onboarding_dialog 非空 → 嵌套事件循环里的重入
            2. _first_run_guide_done → 同一会话里多入口重复触发
            3. should_show_guide() → 用户明确选择过「不再自动显示」
        注意第 3 条挡不住第 2 条描述的情况：用户在最后一页取消勾选时 prefs
        不会写入，只靠它会让同一个会话弹出第二次。

        :return: True 表示本次确实弹出了向导
        """
        if self._onboarding_dialog is not None:
            return False
        if self._first_run_guide_done:
            return False
        if not self._first_run_store.should_show_guide():
            return False

        # 授权状态可能还在后台线程里读盘。等它落地再判定，否则「已授权用户
        # 启动」这条路径会因为此刻还没读到凭据而被误判成未授权。
        self._wait_for_license_load()

        # 等待期间会泵事件，其它入口（_on_license_loaded / 激活成功）完全可能
        # 在这个窗口内把引导弹掉。必须把上面的守卫**重新判一遍**，否则同一会话
        # 里会弹出第二个向导 —— 这正是需求 6 要防的重复。
        if self._onboarding_dialog is not None or self._first_run_guide_done:
            return False
        if self._access_status.is_expired():
            logger.info("[ONBOARDING] 当前无有效授权，暂不显示引导（待授权成功后触发）")
            return False

        self._first_run_guide_done = True
        logger.info("[ONBOARDING] 显示使用引导")
        self._open_onboarding(first_run=True)
        return True

    def _wait_for_license_load(self, timeout_ms: int = 3000) -> None:
        """启动期短暂等待许可证判定落地（本地读盘 + 验签，通常几毫秒）。

        为什么必须有界：入口 main.py 紧接着要武装「5 秒后弹更新对话框」的
        定时器，而引导本身也是模态的 —— 引导必须在那个定时器触发前弹完，
        否则两个模态框会叠在一起。因此这里只做有界等待，超时就按当前状态判定。
        """
        worker = getattr(self, "_license_load_worker", None)
        if worker is None or not worker.is_running():
            return
        deadline = time.monotonic() + timeout_ms / 1000.0
        while worker.is_running() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        logger.info("[ONBOARDING] 等待许可证判定结束（仍在跑=%s）", worker.is_running())

    def _on_help_clicked(self):
        """标题栏「使用引导」：随时重新打开引导（回顾模式）。"""
        if self._onboarding_dialog is not None:
            return
        # 首次引导的聚光灯正在屏幕上时，用户其实已经在被引导了，
        # 再叠一个模态向导只会把界面搞乱。
        if self._guide_spotlight is not None:
            return
        self._open_onboarding(first_run=False)

    def _open_onboarding(self, first_run: bool) -> None:
        """打开引导。

        首次引导的前两步是**主界面上的聚光灯**（非模态，见 GuideSpotlight），
        走完之后才把第 3 步及之后交给原来的模态对话框；回顾模式直接进对话框。
        因此首次这条路径不能在这里 exec() —— 它是异步的，后续阶段在
        _on_guide_spotlight_finished 里推进。
        """
        # 两条路径都用同一套四步聚光灯：首次引导从第 1 步走真实交互，
        # 标题栏 ? 的回顾模式则在 review_mode 下只做只读说明。
        self._start_guide_spotlight(SPOTLIGHT_STEPS[0], review=not first_run)

    def _spotlight_spec(self, step_index: int):
        """某一步聚光灯要说什么、高亮哪些真实控件。

        标题与正文一律取自 STEPS —— 聚光灯与旧版对话框讲的是同一份四步引导，
        各写一份迟早会改歪一份。
        """
        if step_index == 0:
            return STEPS[0][0], STEPS[0][1][0], [self._url_input, self._connect_btn]
        if step_index == 1:
            return STEPS[1][0], STEPS[1][1][0], [self._ocr_game_combo]
        if step_index == 2:
            # 第三步高亮右侧面板里的「选择识别区域」；正文含辅助提示那一段，
            # 因此用换行把两段拼起来。
            return STEPS[2][0], "\n".join(STEPS[2][1]), [self._roi_btn]
        if step_index == 3:
            return (STEPS[3][0], "\n".join(STEPS[3][1]), [self._ocr_btn],
                    self._ocr_blocked_reason())
        return None

    def _start_guide_spotlight(self, step_index: int, review: bool = False) -> None:
        """显示第 step_index 步的聚光灯。

        :param review: True = 标题栏 ? 的回顾模式（只读说明）。它复用同一套
            聚光灯，但被 GuideSpotlight 用 review_mode 隔离掉所有"真实操作
            自动推进"的路径，主窗口这边也不写任何首次引导状态。
        """
        if self._guide_spotlight is not None:
            return
        spec = self._spotlight_spec(step_index)
        if spec is None:
            return
        title, body, targets = spec[0], spec[1], spec[2]
        blocked_reason = spec[3] if len(spec) > 3 else None
        # 后三步高亮的都是右侧 OCR 面板里的控件：卡片必须放到左边的视频区，
        # 否则一张 340px 的卡片会把 310px 的面板整个盖住。
        prefers_left = step_index in (1, 2, 3)
        if prefers_left:
            self._ensure_panel_for_game_step()
        last = step_index == SPOTLIGHT_STEPS[-1]
        logger.info("[ONBOARDING] 聚光灯第 %d 步：%s", step_index + 1, title)
        spotlight = GuideSpotlight(
            self, targets, title, body, step_index, len(STEPS),
            # 第一步没有上一步可回
            show_prev=step_index > SPOTLIGHT_STEPS[0],
            prefers_left=prefers_left,
            # 最后一步的主按钮是「完成」——它只结束引导，不会替用户点 OCR
            next_text="完成" if last else "下一步",
            review_mode=review)
        if blocked_reason:
            spotlight.set_body(f"{body}\n{blocked_reason}")
        spotlight.finished.connect(
            lambda outcome, i=step_index, r=review:
                self._on_guide_spotlight_finished(i, outcome, r))
        spotlight.back_requested.connect(
            lambda i=step_index, r=review: self._on_guide_spotlight_back(i, r))
        self._guide_spotlight = spotlight
        self._guide_review = review
        spotlight.start()

    def _on_guide_spotlight_finished(self, step_index: int, outcome: str,
                                     review: bool = False) -> None:
        """某一步聚光灯结束：继续下一步，或按模式决定怎么收场。"""
        self._guide_spotlight = None

        if review:
            # 回顾模式：只做清理，既不写首次引导状态，也不弹完成提示
            self._guide_review = False
            if outcome == GUIDE_ADVANCE and step_index != SPOTLIGHT_STEPS[-1]:
                QTimer.singleShot(
                    0, lambda: self._start_guide_spotlight(step_index + 1, review=True))
                return
            logger.info("[ONBOARDING] 回顾模式结束（第 %d 步）", step_index + 1)
            self._restore_guide_panel()
            return

        if outcome != GUIDE_ADVANCE:
            # 关闭前已经用确认框问过用户了，这里只负责按答案落盘
            keep = outcome == GUIDE_ALWAYS
            logger.info("[ONBOARDING] 第 %d 步被提前关闭，下次继续提醒=%s",
                        step_index + 1, keep)
            self._first_run_store.set_should_show_guide(keep)
            self._restore_guide_panel()
            return

        if step_index == SPOTLIGHT_STEPS[-1]:
            # 最后一步走完 = 整份引导完成
            logger.info("[ONBOARDING] 引导全部完成")
            self._first_run_store.set_should_show_guide(False)
            self._restore_guide_panel()
            self._show_guide_done_toast()
            return

        next_index = step_index + 1
        # 延到下一个事件循环再切下一步：本回调可能是从 _apply_connected_ui
        # 或游戏档切换中间发出来的，就地换层会让那一版界面停在半途。
        if next_index in SPOTLIGHT_STEPS:
            QTimer.singleShot(
                0, lambda: self._start_guide_spotlight(next_index))
            return
        start_step = DIALOG_START_FOR.get(next_index)
        if start_step is None:
            return
        logger.info("[ONBOARDING] 聚光灯结束，衔接对话框第 %d 步", start_step + 1)
        QTimer.singleShot(
            0, lambda: self._run_onboarding_dialog(
                first_run=True, start_step=start_step))

    def _on_guide_spotlight_back(self, step_index: int, review: bool = False) -> None:
        """「上一步」：退回上一步的聚光灯。

        刻意只做"再显示一次上一步"这一件事 —— 不碰连接、不清地址输入框。
        用户是在读引导，后退一步不该产生任何业务副作用。
        """
        self._guide_spotlight = None
        previous = step_index - 1
        if previous < 0:
            return
        logger.info("[ONBOARDING] 聚光灯回退到第 %d 步", previous + 1)
        QTimer.singleShot(
            0, lambda: self._start_guide_spotlight(previous, review=review))

    def _ocr_blocked_reason(self) -> str | None:
        """OCR 现在为什么点不动；能点则返回 None。

        只**描述**缺少的前置条件，不做任何补救 —— 引导绝不会替用户连接直播
        或框选区域，那既绕过了业务门禁，也把"用户自己完成"这件事抹掉了。
        """
        if self._access_status.is_expired():
            return "当前授权不可用，请先完成激活。"
        if not self._connected:
            return "尚未连接直播：请先完成第 1 步。"
        if not self._roi_manager.has_roi:
            return "尚未框选房间码：请先完成第 3 步。"
        return None

    def _ensure_panel_for_game_step(self) -> None:
        """需要高亮面板内控件时，若面板处于收起状态则临时展开。

        只记"引导把面板从收起改成展开"这一种情形 —— 本来就是展开的就不动它，
        结束时也就没有需要还原的东西。
        """
        if self._ocr_panel.isVisible():
            # 已经可见就什么都不做 —— 尤其**不要**把已记下的原状态覆盖成 None：
            # 第 1 步可能已经把面板从"收起"临时展开了，后面几步看到的是引导
            # 自己展开的面板，覆盖掉原状态就再也还原不回去了。
            return
        logger.info("[ONBOARDING] 面板处于收起状态，为聚光灯临时展开")
        self._guide_panel_restore_to = False
        self._guide_panel_touched = False
        # 走底层 setter，不走 _on_expand_clicked —— 后者会把它记成"用户的选择"
        self._set_ocr_panel_visible(True)

    def _restore_guide_panel(self) -> None:
        """引导结束：把面板还回引导开始前的状态。"""
        target = self._guide_panel_restore_to
        self._guide_panel_restore_to = None
        if target is None:
            return
        if self._guide_panel_touched:
            # 用户在引导期间自己点过收起/展开 —— 他的新选择优先
            logger.info("[ONBOARDING] 用户在引导期间自己动过面板，保持现状")
            return
        logger.info("[ONBOARDING] 引导结束，面板恢复到展开=%s", target)
        self._set_ocr_panel_visible(target)

    def _show_guide_done_toast(self) -> None:
        """引导完成后的轻量提示：约 2 秒后自动消失。"""
        self._guide_toast = GuideToast()
        self._guide_toast.show_over(self, 2000)

    def _reposition_guide_spotlight(self) -> None:
        """让聚光灯跟上真实控件：OCR 面板展开/收起、分隔条拖动都会走到这里。"""
        spotlight = self._guide_spotlight
        if spotlight is not None:
            spotlight.reposition()

    def _run_onboarding_dialog(self, first_run: bool, start_step: int = 0) -> None:
        """构造并模态运行向导；关闭后按用户表态落盘 —— 这里是唯一的写盘点。

        结论在 exec() 返回之后再取，而不是接 accepted/rejected 信号：
        QDialog::done() 先发 finished 再发 accepted/rejected，靠信号回调的先后
        顺序去读状态，容易读到已经被清掉的引用。
        """
        dialog = OnboardingDialog(self, first_run=first_run, start_step=start_step)
        # 必须在 exec() 之前赋值，否则重入保护不起作用
        self._onboarding_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._onboarding_dialog = None

        # 只有首次引导才会写状态：从帮助里重新打开看一眼，不该改掉用户的设置
        if not first_run:
            return
        # 走到这里整份首次引导就结束了（走完或中途关掉），把面板还回去
        self._restore_guide_panel()
        if dialog.completed():
            # 正常走完 → 一律不再自动显示。这是"已完成"的唯一定义，
            # 与中途关闭的三选项（见 ask_guide_exit）互不重叠。
            logger.info("[ONBOARDING] 引导已走完，不再自动显示")
            self._first_run_store.set_should_show_guide(False)
        elif dialog.exit_choice() is not None:
            # 提前关闭：关闭前已经在确认框里问过用户了
            keep = dialog.exit_choice() == GUIDE_ALWAYS
            logger.info("[ONBOARDING] 引导被提前关闭，下次继续提醒=%s", keep)
            self._first_run_store.set_should_show_guide(keep)

    def _on_roi_help_clicked(self):
        """「说明」：ROI 框选示意图。不依赖连接状态，随时可看。"""
        if self._roi_help_dialog is not None:
            return
        dialog = RoiHelpDialog(self)
        self._roi_help_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._roi_help_dialog = None

    def _on_license_clicked(self):
        """激活 / 追加授权入口（标题栏按钮与未激活遮罩共用同一个弹窗）。

        只有真正建立起的本地许可证状态才更新状态；
        激活失败（网络不可达 / 密钥无效等）不会改变本地状态，
        错误已由弹窗就地展示（不再额外弹提示框）。
        """
        dialog = ActivationDialog(self._license_manager, self)
        dialog.exec()

        result = dialog.result_data()
        if result is not None:
            self._apply_license_result(result)
            self._play_unlock_transition()
            # 授权成功、进入可使用状态 —— 未授权用户第一次该看到引导的时刻。
            # 放在解锁过渡之后：先让用户看到"已激活"，再讲怎么用。
            self.maybe_show_first_run_guide()

    def _on_contact_clicked(self):
        """标题栏「联系作者」：在按钮下方弹出联系方式卡片。"""
        self._open_contact_popup(self._contact_btn)

    def _on_overlay_contact_clicked(self):
        """未激活遮罩里的「联系作者」：复用同一个卡片，只是换个锚点。

        刻意**不**经过 _guard_access：没激活的人同样需要能找到联系人。
        """
        self._open_contact_popup(self._activation_overlay._contact_btn)

    def _open_contact_popup(self, anchor) -> None:
        """在 anchor 下方弹出联系方式卡片（已打开则关闭）。

        非模态 —— 不改动直播、OCR 或授权状态，也不发任何网络请求；
        只是把两条已经写死的联系方式摆出来，方便复制。
        """
        if self._contact_popup is not None and self._contact_popup.isVisible():
            self._contact_popup.close()
            return
        popup = InfoPopup(
            "联系作者",
            "问题反馈与功能建议",
            (
                ("微信", CONTACT_WECHAT, "已复制微信号"),
                ("邮箱", CONTACT_EMAIL, "已复制邮箱"),
            ),
            self,
        )
        # 关闭即丢弃引用，下次点击重新构造（内容很少，不值得复用）
        popup.destroyed.connect(lambda *_: setattr(self, "_contact_popup", None))
        self._contact_popup = popup
        popup.show_below(anchor)

    def _close_contact_popup(self) -> None:
        """关掉还开着的联系方式卡片（遮罩消失 / 授权成功时用）。"""
        popup = self._contact_popup
        if popup is not None and popup.isVisible():
            popup.close()

    def _reposition_contact_popup(self) -> None:
        """窗口移动 / 缩放 / 最大化后，把还开着的卡片重新贴回按钮下方。"""
        popup = self._contact_popup
        if popup is not None and popup.isVisible():
            popup.show_below(self._contact_btn)

    def _on_settings_clicked(self):
        """设置中心：只读展示 + 追加授权入口，绝不停流 / 停 OCR / 动 ROI。"""
        dialog = SettingsDialog(
            manager=self._license_manager,
            version=self.current_version(),
            access_status=self._access_status,
            append_license_callback=self._on_license_appended,
            parent=self,
        )
        dialog.exec()

    def _on_license_appended(self, result: LicenseResult):
        """设置中心里追加授权成功后：与主入口同一套状态应用 + 解锁过渡。"""
        self._apply_license_result(result)
        self._play_unlock_transition()
        # 追加授权同样意味着"进入可使用状态"，未授权用户走这条路也该看到引导
        self.maybe_show_first_run_guide()

    # ------------------------------------------------------------------
    # 未激活遮罩 / 解锁过渡
    # ------------------------------------------------------------------
    def _sync_activation_overlay(self) -> None:
        """按**既有授权状态**决定是否显示未激活遮罩（不改变任何状态）。"""
        if getattr(self, "_activation_overlay", None) is None:
            return
        denied = self._access_status.is_expired()
        if denied:
            self._activation_overlay.reset_prompt()
        else:
            # 遮罩要收起了 —— 遮罩入口打开的卡片不能留在屏幕上
            self._close_contact_popup()
        self._activation_overlay.setVisible(denied)
        if denied:
            self._activation_overlay.raise_()

    def _play_unlock_transition(self) -> None:
        """成功激活后的轻量解锁过渡：约 1.4 秒后恢复普通预览。

        刻意**不**自动连接直播、不启动 OCR —— 只做视觉过渡。
        """
        if getattr(self, "_activation_overlay", None) is None:
            return
        # 授权已经成功：卡片若还开着就立刻收掉，别让它浮在解锁过渡之上
        self._close_contact_popup()
        self._activation_overlay.setVisible(True)
        self._activation_overlay.show_unlocked()
        QTimer.singleShot(1400, self._finish_unlock_transition)

    def _finish_unlock_transition(self) -> None:
        self._sync_activation_overlay()

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
    # 更新（Phase 3：Velopack）
    #
    # 与心跳同构的一条后台链路，且同样与授权严格隔离。但更新本身的实现
    # **不在这里**：版本比较、清单解析、下载、校验、解压、替换安装目录、
    # 重启，全部是 Velopack 的职责，经 utils.update_service.UpdateService
    # 转调。这里只负责：
    #   - 起后台线程发起检查（Velopack 的 check 是阻塞调用）；
    #   - 有新版本时弹对话框；
    #   - 用户确认重启时，先停流收尾，再交给 Velopack。
    #
    # 因此「检查更新失败 / 更新源不可用 / 未安装运行」都不会以任何形式影响
    # 启动、视频、OCR 或授权状态。
    #
    # Phase 3.5：旧的自研更新链（utils/update_client + utils/update_download）
    # 已随 Velopack E2E 跑通后删除。更新只有这一条生产路径。
    # ------------------------------------------------------------------
    def current_version(self) -> str:
        """当前版本号。已安装运行时来自 Velopack 的安装清单（它才知道更新后
        自己是什么版本），未安装运行（源码 / 便携 ZIP）时回落 utils.version。"""
        return self._update_service.current_version()

    def start_update_check(self):
        """安排一次启动后的更新检查。由应用入口 main.py 调用。

        为什么不在 __init__ 里自动开始：
            与 start_heartbeat 同因 —— 它会向更新源发起**真实网络请求**，而
            「构造一个 MainWindow」在测试里极常见。若挂在构造流程上，每次跑
            测试都会打一次真实源。

        重复调用是安全的：只会重置那个单次定时器。
        """
        self._update_timer.start(UPDATE_CHECK_DELAY_MS)

    def _on_update_timer_timeout(self):
        """延时到点：在后台线程发起检查。

        Velopack 的 check_for_updates 是阻塞调用（Rust 侧释放 GIL 但不返回
        事件循环），放 GUI 线程会冻住界面，因此借用既有的 LicenseWorker
        线程模式 —— 它只要求「无参任务 + 单参完成回调」，恰好合适。
        """
        # 上一次仍在跑就跳过，避免网络慢时堆叠线程
        if self._update_worker is not None and self._update_worker.is_running():
            return
        self._update_worker = LicenseWorker(
            task=self._update_service.check,
            on_done=self._update_check_done_sig.emit,
            name="update-check",
        )
        self._update_worker.start()

    def _on_update_check_done(self, info):
        """检查结束回调（经 Qt 排队回到 GUI 线程）。

        ``info`` 为 None 表示「没有可用的更新」—— 包括已是最新、更新源不可
        用（正式源尚未部署 / 网络不通）、以及本机不是 Velopack 安装版本
        （源码启动、旧 ZIP 解压运行）。这些一律静默返回，不提示、不改状态：
        后台版本检查的失败不该在用户面前产生动静。

        版本比较由 Velopack 自己做（check_for_updates 已经把「有更新」判定
        完了），我们不再实现第二套比较逻辑。

        结构判断而不是真值判断：LicenseWorker 在任务抛异常时会回调一个
        LicenseResult 兜底对象，它既不是 None 也没有 TargetFullRelease，
        绝不能被当成一份有效更新信息去取版本号。
        """
        if info is None or not hasattr(info, "TargetFullRelease"):
            return
        logger.info(
            "发现新版本 %s（当前 %s）",
            info.TargetFullRelease.Version,
            self._update_service.current_version(),
        )
        # 持有引用：下载可以在窗口收起后继续跑完，对话框不能随 exec() 返回被回收
        self._update_dialog = UpdateDialog(self._update_service, info, self)
        self._update_dialog.apply_requested.connect(self._on_update_apply_requested)
        self._update_dialog.exec()

    def _on_update_apply_requested(self, info):
        """用户确认「立即重启并更新」。

        Velopack 的 apply_updates_and_restart 会启动 Update.exe 等本进程退出、
        然后**直接结束当前进程**（Rust 侧 exit(0)），Qt 的窗口关闭流程不会再
        走一遍。因此 ffmpeg / 播放线程必须在这里先显式停掉，否则会留下孤儿
        进程 —— 这正是先调 _disconnect() 的原因。
        """
        logger.info("用户确认应用更新，停止当前会话后交给 Velopack")
        self._disconnect()
        # 先落盘「正在换到新版本」再 apply（Phase 4B）。顺序不能反：apply 之后
        # 本进程会被 Velopack 直接结束，没有机会再写任何东西。这条记录是新版
        # 启动失败时唯一的线索 —— 下一次启动据此判定要不要回滚。
        update_state.write_state(
            update_state.mark_applying(
                self.current_version(), info.TargetFullRelease.Version
            )
        )
        try:
            self._update_service.apply_and_restart(info)
        except UpdateError as e:
            # 只有「Velopack 没接管、进程还活着」才会走到这里
            logger.warning("应用更新失败: %s", e)
            QMessageBox.warning(self, "更新失败", str(e))

    # ------------------------------------------------------------------
    # 窗口事件处理
    # ------------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        """窗口大小改变时更新覆盖层位置、顶部热区与窄窗口自动收起。"""
        super().resizeEvent(event)
        self._update_connection_overlay_position()
        self._position_title_hotzone()
        self._sync_narrow_layout()
        self._reposition_contact_popup()

    def _sync_narrow_layout(self) -> None:
        """窄窗口自动收起右侧面板：视频优先，放宽后再自动展开。

        只接管「自动」那次收起：用户自己点过的收起/展开永远优先，
        否则窗口一缩小就把用户刚展开的面板再收掉，操作会被吃掉。
        """
        if getattr(self, "_ocr_panel", None) is None:
            return
        if getattr(self, "_panel_user_choice", False):
            return

        narrow = self.width() < AUTO_COLLAPSE_WIDTH
        visible = self._ocr_panel.isVisible()
        if narrow and visible:
            self._panel_auto_hidden = True
            logger.info("[UI] 窗口宽度 %d < %d，自动收起右侧面板",
                        self.width(), AUTO_COLLAPSE_WIDTH)
            self._set_ocr_panel_visible(False)
        elif not narrow and not visible and self._panel_auto_hidden:
            self._panel_auto_hidden = False
            self._set_ocr_panel_visible(True)

    def _on_collapse_clicked(self) -> None:
        """用户主动收起：记住这是用户的选择，不再被自动逻辑覆盖。"""
        self._panel_user_choice = True
        self._panel_auto_hidden = False
        self._guide_panel_touched = True
        self._set_ocr_panel_visible(False)

    def _on_expand_clicked(self) -> None:
        self._panel_user_choice = True
        self._panel_auto_hidden = False
        self._guide_panel_touched = True
        self._set_ocr_panel_visible(True)

    def moveEvent(self, event):  # noqa: N802
        """窗口被拖动时，开着的卡片跟着走（否则会飘在半空）。"""
        super().moveEvent(event)
        self._reposition_contact_popup()

    def showEvent(self, event):  # noqa: N802
        """首次显示后启用 Mica（失败即保持不透明深色，不做视觉欺骗）。"""
        super().showEvent(event)
        # 构建期的同步发生在窗口显示之前，那时 isVisible() 一律为 False，
        # 顶部操作区会被当成"面板已收起"。显示之后再同步一次拿到真实状态。
        self._sync_address_columns()
        if getattr(self, "_backdrop", None) is None:
            ok = theme.enable_backdrop(int(self.winId()), "mica")
            self._backdrop = "mica" if ok else "opaque"
            if ok:
                set_property(self._root, "mica", "true")
                for widget in (self._root,):
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
                # 个别系统在窗口完成合成前设置会退回浅色，显示后再补一次
                QTimer.singleShot(150, lambda: theme.enable_backdrop(
                    int(self.winId()), "mica"))
            logger.info("[UI] backdrop=%s", self._backdrop)

    def closeEvent(self, event):  # noqa: N802
        """窗口关闭时确保资源释放"""
        self._disconnect()
        super().closeEvent(event)
