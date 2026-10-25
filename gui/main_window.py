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
from datetime import datetime

from PySide6.QtCore import (QEvent, QPoint,
                             QStandardPaths, QTimer, Qt, Signal)
from PySide6.QtGui import QAction, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QGridLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QMenu,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.theme import Metrics, Palette
from gui.video_widget import VideoWidget
from gui.watermark import WatermarkOverlay
# Phase 1 拆分:模块级私有类外迁至专职模块,此处按**原名** re-export——
# tests 直接 import 这些名字并 monkeypatch(mw._ConnectWorker 等),
# 保持 main_window 命名空间不变 = 既有测试零改动。
from gui.playback_workers import (  # noqa: F401
    _BareResolveResult,
    _BareResolveWorker,
    _ConnectWorker,
)
from gui.frame_bridge import _FrameBridge, _FrameNotifier  # noqa: F401
from gui.activation_overlay import _ActivationOverlay  # noqa: F401
# 测试打桩面(Phase 2 拆分约定):coordinators 在调用点经 gui.main_window
# 模块命名空间解析这些 UI 类,使 mw.OnboardingDialog = _Stub 等打桩生效;
# 本模块自身不再直接使用它们,导入必须保留(noqa)。
from gui.expired_dialog import ExpiredDialog  # noqa: F401
from gui.onboarding import GuideSpotlight  # noqa: F401
from gui.update_dialog import UpdateDialog  # noqa: F401

from gui.widgets import (
    DockPanel,
    FramelessController,
    IconButton,
    InfoPopup,
    OverlayHost,
    TitleBar,
    WindowButton,
    chip_html,
    enable_mouse_tracking,
    h_line,
    open_external_url,
    set_property,
    set_tone,
)
from gui.settings_dialog import (
    CONTACT_QQ,
    PURCHASE_URL,
    SettingsDialog,
)
from gui.activation_dialog import ActivationDialog
from gui.onboarding import (
    OnboardingDialog,
    RoiHelpDialog,
)
from utils.ui_prefs import PERF_PINNED, UI_THEME, get_pref, set_pref
from gui.toast import Toast
from gui.join_guide import JoinGuideBar
from automation import (
    STEP_PROMPTS,
    CalibrationOverlay,
    GlobalHotkey,
    JoinConfig,
    JoinController,
)
from stream.models import StreamInfo
from stream.resolver import StreamResolver
from stream.failover import CandidateFailover
from stream.ffmpeg_reader import FFmpegReader
from stream.frame_buffer import LatestFrameBuffer
from audio import AudioService
from gui.audio_panel import AudioPanel
from gui.history_panel import HistoryButton
from ocr.roi_manager import ROI, ROIManager
from ocr.roi_selector import ROISelector
from ocr.worker import OCRWorker
# GUI 需要知道当前后端是否使用「会话」模型（只有 ONNX 有），才能决定用哪套
# 芯片时序。直接复用 worker 的选择器，而不是在 GUI 里再写一份规则 ——
# ocr/worker.py 是这条规则的唯一权威，两处各写一份必然漂移。
from ocr.worker import _selected_engine_kind
from ocr.clipboard import ClipboardManager
from ocr.copy_log import CopyLog
from ocr.diagnostics import OCRDiagnostics
from ocr.sample_capture import SampleCapture
# 数据集采集总开关。这里只 import 一个布尔量；真正的 Recorder / 队列 / writer
# 线程都在开关为 True 时才（惰性）导入并构造，见 __init__ 里那一段。
# 开关本体定义在 ocr/dataset_capture.py，全项目只有那一处。
from ocr.dataset_capture import DATASET_CAPTURE_ENABLED, TARGET_FORMATS
from ocr.profiles import game_profiles
from stream.history import HistoryService

logger = logging.getLogger("DouyinLowLatencyViewer.gui.main_window")


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
SESSION_EMPTY_HINT = "暂无记录 —— 复制成功的房间号会出现在这里"

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


class MainWindow(QMainWindow):
    """Douyin Low Latency Stream Viewer 主窗口"""

    # 跨线程安全 Signal：后台线程 → GUI 线程
    _connect_success_sig = Signal(object)   # StreamInfo
    _connect_error_sig = Signal(str)
    _license_loaded_sig = Signal(object)  # 启动时许可证校验结果
    _heartbeat_done_sig = Signal(object)  # 心跳上报结果
    _update_check_done_sig = Signal(object)  # 更新检查结果（Velopack UpdateInfo 或 None）
    _bare_resolved_sig = Signal(int, object)  # 裸流解析结果 (request_id, _BareResolveResult)

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
        # ---- RELEASE-02-B5-CLEANUP：唯一取流入口的会话状态 ----
        # 裸流 URL 与 FFmpeg7 运行时在**会话开始时**于后台线程解析一次。
        # 抖音会话的唯一候选就是该裸流 URL（旧 `_uhd` 候选已整体移除）。
        self._bare_url: str | None = None
        self._bare_exe: str | None = None
        #: 取流请求代号。每次开始新解析 / 断开 / 关闭都递增，迟到的回调
        #: 凭此判定自己属于旧请求并放弃——旧结果绝不覆盖新一轮会话。
        self._bare_req_id: int = 0
        self._bare_worker: _BareResolveWorker | None = None
        #: 窗口正在关闭：此后不再向 GUI 投递任何更新
        self._closing: bool = False
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
        #: Phase 3.2：当前这一次尝试的启动分段计时（candidate → ffprobe → FFmpeg
        #: → 首帧 → 首次显示）。每次 _start_playback 换新对象，尝试结束后只读不改。
        #: 最近一次 failover 切换的实际耗时（ms），无切换为 0.0。仅用于观测。
        # 音频服务：与视频/OCR 平行的独立功能域；main_window 只做生命周期
        # 接线（_start_playback start / _teardown_playback stop），不碰音频内部。
        self._audio = AudioService()
        self._audio_panel: AudioPanel | None = None
        self._roi_manager: ROIManager = ROIManager()
        self._roi_selector: ROISelector | None = None
        self._ocr_worker: OCRWorker | None = None
        # 本次 OCR 会话的后端是否使用「会话」模型（ONNX 专属）。只有它会启用
        # 加载中 → 等待 ROI → 运行中 三态；Paddle/Tesseract 保持原有芯片时序
        # （启动即「运行中」）。在 _start_ocr 里于线程启动**之前**定下来。
        self._ocr_session_backend: bool = False
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

        # ---- 数据集采集旁路（见 ocr/dataset_capture.py）----
        # 关闭（DATASET_CAPTURE_ENABLED = False）时：连 CaptureFanout 都不构造，
        # `_capture_sink` 就是上面那个原封不动的 SampleCapture —— 队列、writer
        # 线程、目录、文件一律不存在，worker 侧也完全不知道有这么个功能。
        # 打开时用一层转发对象顶上 worker 的同一个 sample_capture 槽位，
        # 因此 ocr/worker.py 不需要任何改动。
        self._dataset_recorder = None
        self._dataset_timer = None
        self._diag_enabled_before_dataset = False
        self._capture_sink = self._sample_capture
        if DATASET_CAPTURE_ENABLED:
            from ocr.dataset_capture import CaptureFanout, DatasetRecorder
            self._dataset_recorder = DatasetRecorder()
            self._capture_sink = CaptureFanout(self._sample_capture,
                                               self._dataset_recorder)

        # 直播间历史（Model/Repository/Service 在 stream/history.py）。
        # 只有成功进入直播间才记录；GUI 只经本服务读写。
        self._history = HistoryService()
        #: 本次连接尝试的目标 URL（成功后据此记入历史）
        self._pending_history_url: str | None = None

        # 首次启动状态。构造只读一个本地文件（容错，失败按默认值处理），
        # 不联网、不弹窗 —— 真正的弹窗由 main.py 经 maybe_show_first_run_guide()
        # 触发，理由与 start_heartbeat 相同：测试会直接构造 MainWindow。
        # 同一时刻最多一个引导向导（嵌套事件循环里可能重入）
        self._onboarding_dialog: OnboardingDialog | None = None
        # 「本次会话已经自动弹过引导」：授权成功、启动恢复、窗口显示几条路径
        # 都可能触发首次引导，只靠 prefs 标记挡不住（用户可能在最后一页取消
        # 勾选「不再自动显示」，那样 prefs 不会写入，同一会话里就会被弹第二次）。
        # 主界面聚光灯（非模态），前两步各一个实例。它存在期间不打开模态向导。
        # 第二步需要游戏下拉框可见：面板收起时由引导临时展开。
        # 记下原状态，引导结束时还回去；用户中途自己动过面板就不还。
        #: 引导完成的轻量提示（独立顶层窗口，2 秒后自动消失）
        #: 「联系作者」弹出卡片（Qt.Popup，点外面自动关）
        self._contact_popup = None
        #: 当前这轮聚光灯是不是"标题栏 ? 的回顾模式"。
        #: 回顾模式不写任何首次引导状态、不弹退出确认、不显示完成提示。
        self._roi_help_dialog: RoiHelpDialog | None = None

        # 当前选中的URL（用于备注编辑）
        self._selected_url_for_note: str | None = None

        # 访问状态管理器

        # 许可证管理器（Phase 7.2-6.5）：激活 / 本地验证 / 启动恢复

        # 启动心跳（Phase 7.4，DAU 统计）
        # 与授权状态完全解耦：只上报「本机启动过」，任何失败都静默。
        # 刻意新建独立定时器，不复用 _access_timer —— 后者承担的是许可证到期
        # 重评估，语义不同（理由与 :274-276 对 _status_timer 的说明一致）。
        # 调度器是纯本地状态机（不联网、无副作用），构造时建立是安全的；
        # 真正发起网络请求的是 start_heartbeat()，由应用入口 main.py 调用。
        # 单次触发：每次到点后重新计算下一个业务日边界，因此不会累积 QTimer 漂移。

        # 更新检查（Phase 3：Velopack）
        # 与授权、遥测同样解耦：构造时不联网（UpdateService 惰性构造
        # UpdateManager），真正的检查由应用入口 main.py 经 start_update_check()
        # 触发（理由与心跳完全一致 —— 见 start_heartbeat 的说明：测试会大量
        # 直接构造 MainWindow）。
        # 对话框与后台下载线程的持有者：下载可以在窗口收起后继续跑完，
        # 因此不能让它随 exec() 返回被回收。

        # Phase 2 拆分:授权/引导/更新三类领域状态与流程迁至 gui/coordinators.py。
        # __getattr__ 把对这些字段/方法的访问转发到协调器——测试对
        # window._license_manager / window._apply_license_result 等的直引零改动。
        from gui.coordinators import (GuideCoordinator, LicenseCoordinator,
                                      UpdateCoordinator)
        self._license_coord = LicenseCoordinator(self)
        self._guide_coord = GuideCoordinator(self)
        self._update_coord = UpdateCoordinator(self)
        from gui.playback import PlaybackCoordinator
        self._playback_coord = PlaybackCoordinator(self)
        # 信号(类级,留在窗口)在协调器就绪后接线;目标是 __getattr__
        # 转发出的协调器 bound 方法。
        self._license_loaded_sig.connect(self._on_license_loaded)
        self._heartbeat_done_sig.connect(self._on_heartbeat_done)
        self._update_check_done_sig.connect(self._on_update_check_done)
        # 单次触发：启动后查一次即可，本次不做周期性复查。

        # 上一次显示的 buffer frame_id
        self._last_displayed_frame_id: int = 0

        # 连接跨线程 Signal（后台线程 emit → GUI 线程 slot）
        self._connect_success_sig.connect(self._handle_connect_success)
        self._connect_error_sig.connect(self._handle_connect_error)
        self._bare_resolved_sig.connect(self._on_bare_resolved)

        self._init_ui()

        # 更新初始访问状态
        self._on_access_status_changed()

        # 启动时恢复已保存的许可证（读盘 + 本地验签在后台线程完成）
        self._start_license_load()

        # RELEASE-02-B5：启动时后台探测一次 FFmpeg7 运行时可用性 —— 只记日志、
        # 不阻塞启动、不弹 UI；同时预热缓存，使首次连接的裸流解析不必再等探测。
        self._start_runtime_probe()

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

    def __getattr__(self, name):
        """Phase 2 兼容层:已迁往协调器的字段/方法在此转发。

        仅在常规查找失败时触发;协调器尚未构造(__init__ 早期/拷贝)时
        直接抛 AttributeError,不掩盖真实拼写错误。
        """
        for coord_name in ("_playback_coord", "_license_coord", "_guide_coord",
                           "_update_coord"):
            coord = self.__dict__.get(coord_name)
            if coord is not None:
                try:
                    return getattr(coord, name)
                except AttributeError:
                    continue
        raise AttributeError(
            f"{type(self).__name__!s} object has no attribute {name!r}")

    def _on_frame_ready(self):
        """帧到达通知的**窗口侧槽**(Phase 3 拆分后保留在窗口)。

        实现在 PlaybackCoordinator._on_frame_ready;这里必须是窗口自己的
        方法而不是 __getattr__ 转发:该槽依赖 QObject.sender() 识别信号
        来源以丢弃旧 notifier 的迟到唤醒,receiver 上下文(窗口)一旦
        丢失,Qt 无法归属 sender,隔离语义失效(实测踩过)。
        """
        return self._playback_coord._on_frame_ready()

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
        theme.set_theme(str(get_pref(UI_THEME, "dark")))
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
        self._title_bar = TitleBar("LiveLens", "低延迟播放工作台")

        # ---- 常驻层：授权 / 设置 ------------------------------------
        # 「授权」保持唯一被强调的入口（图标按钮上的 variant=primary）
        self._license_btn = self._title_bar.add_icon_button(
            IconButton.LICENSE, "激活许可证：输入密钥为本机授权",
            accessible_name="激活许可证")
        set_property(self._license_btn, "variant", "primary")
        self._license_btn.clicked.connect(self._on_license_clicked)

        self._settings_btn = self._title_bar.add_icon_button(
            IconButton.SETTINGS, "设置：授权状态、版本与偏好",
            accessible_name="设置")
        self._settings_btn.clicked.connect(self._on_settings_clicked)

        # ---- 分组分隔线 ----------------------------------------------
        self._title_bar.add_action_separator()

        # ---- 溢出层：低频入口收进「⋯」 -------------------------------
        self._more_btn = self._title_bar.add_icon_button(
            IconButton.MORE, "更多：全屏工作台 / 使用引导 / 联系作者",
            accessible_name="更多")
        self._more_menu = QMenu(self._more_btn)
        self._fullscreen_action = QAction("全屏工作台", self._more_menu)
        self._fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self._more_menu.addAction(self._fullscreen_action)
        self._more_menu.addSeparator()
        help_action = QAction("使用引导", self._more_menu)
        help_action.triggered.connect(self._on_help_clicked)
        self._more_menu.addAction(help_action)
        contact_action = QAction("联系作者", self._more_menu)
        contact_action.triggered.connect(self._on_contact_clicked)
        self._more_menu.addAction(contact_action)
        self._more_btn.clicked.connect(self._show_more_menu)

        return self._title_bar

    def _on_app_state_changed(self, state) -> None:
        """应用失活/恢复:联动未激活遮罩的呼吸动画(见 _init_ui 注释)。"""
        if getattr(self, "_activation_overlay", None) is None:
            return
        from PySide6.QtCore import Qt as _Qt
        self._activation_overlay.set_suspended(
            state != _Qt.ApplicationState.ApplicationActive)

    def _show_more_menu(self) -> None:
        """「⋯」溢出菜单：贴着按钮下沿弹出（与联系方式卡片同一锚定习惯）。"""
        self._more_menu.exec(self._more_btn.mapToGlobal(
            QPoint(0, self._more_btn.height() + 4)))

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

        # 历史入口组：「历史」主入口 +「⋯」二级操作（备注/清除）装进同一个
        # 胶囊容器 —— 两个按钮读作一个整体，而不是两颗来历不明的小方块。
        group = QFrame()
        group.setObjectName("historyGroup")
        group_lay = QHBoxLayout(group)
        group_lay.setContentsMargins(0, 0, 0, 0)
        group_lay.setSpacing(0)

        self._history_btn = HistoryButton(self._history)
        self._history_btn.setFixedWidth(64)
        self._history_btn.enterRequested.connect(self._on_history_enter)
        # 首次进入直播间 → 询问备注（判定在 HistoryService，弹窗在 history_panel）
        self._history.set_remark_callback(self._history_btn.on_remark_request)
        group_lay.addWidget(self._history_btn)

        # 历史二级入口：备注 / 清除（原先各自占一个按钮，地址栏太挤）
        self._history_menu_btn = QToolButton()
        self._history_menu_btn.setObjectName("historyMenuBtn")
        self._history_menu_btn.setText("⋯")
        self._history_menu_btn.setFixedSize(30, 32)
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
        group_lay.addWidget(self._history_menu_btn)
        row.addWidget(group)

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
        self._activation_overlay.purchase_clicked.connect(
            self._on_purchase_clicked)
        # interactive=True：遮罩上有「立即激活」按钮，必须收得到点击
        # （默认的鼠标穿透会让按钮连同浮层一起不可点）。
        self._video_container.add_overlay(self._activation_overlay, interactive=True)

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
        # 来源仍是 self._access_status，由 _on_access_status_changed 推送。
        self._watermark = WatermarkOverlay(self._video_widget)
        self._video_container.add_overlay(self._watermark)
        self._watermark.hide()

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
        # 延迟芯片：低延迟是本产品的核心卖点，读数（含 ok/warn/error 变色）
        # 由 _on_stats 更新。历史上它在 _build_perf_bar 里创建却从未被加入
        # 任何布局（死对象）—— 本次改版把它放到它该在的位置：FPS 旁边。
        self._lbl_latency = self._make_chip()
        self._lbl_latency.setText("延迟 --")
        for chip in (self._lbl_status, self._lbl_resolution,
                     self._lbl_fps, self._lbl_latency):
            row.addWidget(chip)

        # 左读数、右控制：状态（未连接/分辨率/FPS/延迟）贴左，
        # 音频控制（音量/模式/状态）贴右，中间留白断开两类语义。
        row.addStretch(1)

        # 音频控制条：音量 / 模式 / 状态（转发给 AudioService，无音频逻辑在此）
        self._audio_panel = AudioPanel()
        self._audio_panel.volumeChanged.connect(self._audio.set_volume)
        self._audio_panel.modeChanged.connect(self._audio.set_mode)
        self._audio.set_state_callback(self._audio_panel.on_service_state)
        row.addWidget(self._audio_panel)
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

        # ---- 数据集采集入口（测试用，可一键关闭）----
        # 与上面那三个"内部诊断"入口分开受控：它由 ocr/dataset_capture.py 的
        # 总开关 DATASET_CAPTURE_ENABLED 决定，**不受** SHOW_INTERNAL_DIAGNOSTICS
        # 影响（后者是生产 UI 收口，默认关；本入口按需求默认开）。
        # 关闭时这个 QWidget 连创建都不创建，界面上不留任何痕迹。
        if DATASET_CAPTURE_ENABLED:
            ds_row = QWidget()
            ds_row_layout = QHBoxLayout(ds_row)
            ds_row_layout.setContentsMargins(0, 0, 0, 0)
            ds_row_layout.setSpacing(6)

            self._dataset_btn = QPushButton("数据集采集", ds_row)
            self._dataset_btn.setObjectName("sectionAction")
            self._dataset_btn.setCursor(Qt.PointingHandCursor)
            self._dataset_btn.setFocusPolicy(Qt.NoFocus)
            self._dataset_btn.setToolTip(
                "把识别器看到的那一帧 ROI 原图 + PP-OCRv6 原始读法存成数据集。\n"
                "复用当前直播流与当前 OCR 结果：不重开流、不重跑识别、不改识别与复制。\n"
                "只收集符合 AAA123 / BBB123 / AAAA1234 三种格式的读法，\n"
                "其余（空结果/普通文本/长度或字符不符）进 rejected.csv，不做任何纠正。")
            self._dataset_btn.clicked.connect(self._on_dataset_capture_toggled)
            ds_row_layout.addWidget(self._dataset_btn)
            ds_row_layout.addStretch(1)
            col.addWidget(ds_row)

            self._dataset_status = QLabel("", ds_row)
            self._dataset_status.setObjectName("captionLabel")
            self._dataset_status.setWordWrap(True)
            col.addWidget(self._dataset_status)

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
        """底部状态栏：常驻用户状态，性能指标默认收进「诊断」弹层。

        常驻：OCR 运行状态芯片、剪贴板反馈芯片、授权状态、
              「诊断」入口（点开才是完整指标）。
        诊断：链路 / 解码 / 渲染 / OCR 四组 10 项指标（set_frame p50/p95、
              paint、管道积压等是开发者数据），默认装在弹层里，
              设置「偏好」里可改回常驻（utils.ui_prefs，键 perf_pinned）。

        指标容器（_perf_bar_widget）只有一份，常驻/弹层两种模式之间
        用 addWidget 换父级搬运，数据照常采集、_set_perf 一如既往写标签。
        """
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)

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
        # 四个组容器保存引用：常驻(单行+分隔线)与弹层(2×2 网格)两种
        # 布局共用同一批组控件，切换时由 _relayout_perf 重排。
        self._perf_groups = [self._perf_group(name, items)
                             for name, items in groups]
        self._perf_bar_widget = QWidget()
        self._perf_bar_widget.hide()

        # 诊断弹层（Qt.Popup：点击外部自动关闭）；指标容器平时住在这里
        self._perf_popup = QFrame(self, Qt.Popup | Qt.FramelessWindowHint)
        self._perf_popup.setAttribute(Qt.WA_TranslucentBackground)
        self._perf_popup.setObjectName("perfPopup")
        popup_lay = QVBoxLayout(self._perf_popup)
        popup_lay.setContentsMargins(1, 1, 1, 1)
        popup_lay.setSpacing(2)
        title = QLabel("诊断 · 性能指标")
        title.setObjectName("perfPopupTitle")
        popup_lay.addWidget(title)
        popup_lay.addWidget(self._perf_bar_widget)
        self._perf_popup.hide()
        self._apply_perf_popup_qss()

        # 「诊断」入口：一枚低调的状态栏按钮（不与授权/OCR 状态抢注意力）
        self._perf_toggle_btn = QPushButton("诊断")
        self._perf_toggle_btn.setObjectName("statusButton")
        self._perf_toggle_btn.setCursor(Qt.PointingHandCursor)
        self._perf_toggle_btn.setToolTip("性能指标：延迟 / 帧率 / 渲染耗时 / OCR")
        self._perf_toggle_btn.clicked.connect(self._toggle_perf_popup)
        bar.addPermanentWidget(self._lbl_ocr)
        bar.addPermanentWidget(self._lbl_clipboard)
        self._access_status_label = QLabel()
        self._access_status_label.setObjectName("accessLabel")
        self._access_status_label.setProperty("tone", "neutral")
        bar.addPermanentWidget(self._access_status_label)
        bar.addPermanentWidget(self._perf_toggle_btn)

        # 剪贴板芯片的 2 秒回落定时器（由 _on_copy_requested 启动）
        self._clipboard_chip_timer = QTimer(self)
        self._clipboard_chip_timer.setSingleShot(True)
        self._clipboard_chip_timer.timeout.connect(self._reset_clipboard_chip)
        self._reset_clipboard_chip()

        # 统一 toast 通知（右下角浮出；连接成败 / 复制 / 导出等共用）
        self._toast = Toast(self)
        self._set_perf_pinned(bool(get_pref(PERF_PINNED, False)), persist=False)

        # ---- 自动加入(automation):配置/热键/执行器/校准 ----------------
        self._pending_join_code: str | None = None
        self._automation_cfg = JoinConfig.load()
        self._join_hotkey = GlobalHotkey(self)
        self._join_hotkey.activated.connect(self._on_join_hotkey)
        self._join_controller = JoinController(self)
        self._join_controller.finished.connect(self._on_join_finished)
        self._calibration = CalibrationOverlay(self)
        self._calibration.stepChanged.connect(self._on_cal_step)
        self._calibration.invalidClick.connect(self._on_cal_invalid)
        self._calibration.finished.connect(self._on_cal_finished)
        self._join_guide = JoinGuideBar()
        self._join_guide.close()
        self._sync_join_hotkey()
        # 应用整体失活(用户切到别的软件)也挂起呼吸动画:窗口还在但没人在
        # 看,60fps 透明度重绘纯烧 CPU;配合最小化暂停,把常驻占用压到零。
        QGuiApplication.instance().applicationStateChanged.connect(
            self._on_app_state_changed)
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
            self._pending_join_code = _fake
            logger.info("[JOIN] 测试钩子:预置待加入码 %r", _fake)

    def _apply_perf_popup_qss(self) -> None:
        """诊断弹层背景用 solid_color 合成的不透明色。

        POPUP_BG 0.97 的 rgba 在视频工具条这种内容复杂的区域会透底，
        指标和工具栏控件糊在一起不可读 —— 弹层底下必须是实底。
        主题切换时重设一次。
        """
        solid = theme.solid_color(Palette.POPUP_BG, Palette.APP_BG)
        self._perf_popup.setStyleSheet(
            f"#perfPopup {{ background-color: {solid};"
            f" border: 1px solid {Palette.BORDER_STRONG};"
            f" border-radius: {Metrics.CARD_RADIUS}px; }}")

    def _relayout_perf(self, pinned: bool) -> None:
        """指标容器重排：常驻=状态栏单行(带分隔线)；弹层=2×2 网格卡。

        一条 900px 的横行在弹层里既难读又会压到工具栏控件；
        网格卡约 420×130，一眼可读。
        """
        widget = self._perf_bar_widget
        old = widget.layout()
        if old is not None:
            # 1) 逐项取出组控件（保活；分隔线随旧布局一起丢弃）
            while old.count():
                item = old.takeAt(0)
                child = item.widget()
                if child is not None:
                    child.setParent(None)
            # 2) 旧布局本体必须从 widget 上卸掉，否则新布局装不进去
            #    （"already has a layout"，组会被加进孤儿布局——实测踩过）。
            #    Qt 没有卸载 API：转移到临时容器再 deleteLater。
            tmp = QWidget()
            tmp.setLayout(old)
            tmp.deleteLater()
        if pinned:
            lay = QHBoxLayout(widget)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            for index, group in enumerate(self._perf_groups):
                if index:
                    divider = QFrame()
                    divider.setObjectName("perfDivider")
                    divider.setFixedWidth(1)
                    lay.addWidget(divider)
                lay.addWidget(group)
        else:
            lay = QGridLayout(widget)
            lay.setContentsMargins(12, 4, 12, 8)
            lay.setHorizontalSpacing(20)
            lay.setVerticalSpacing(2)
            for index, group in enumerate(self._perf_groups):
                lay.addWidget(group, index // 2, index % 2)

    def _toggle_perf_popup(self) -> None:
        """「诊断」按钮：在按钮上方弹出 / 收起指标弹层。

        必须先 show 再取尺寸定位：隐藏状态下指标容器尚未布局，
        height()≈0，按它算出的 y 会把弹层定位到按钮正下方 —— 内容
        撑开后整个垂到窗口外（2026-10-24 用户实测）。
        """
        popup = self._perf_popup
        if popup.isVisible():
            popup.close()
            return
        # 弹层模式：容器上次可能随"常驻→弹层"切换被 hide 过，弹出前拉起
        self._perf_bar_widget.setVisible(True)
        btn = self._perf_toggle_btn
        popup.show()               # 先显示拿到真实布局
        popup.adjustSize()
        anchor = btn.mapToGlobal(QPoint(0, 0))
        screen = btn.screen().availableGeometry()
        # 右缘对齐按钮右缘；整体夹进按钮所在屏幕的可用区（避开任务栏）
        x = max(screen.left() + 8,
                min(anchor.x() + btn.width() - popup.width(),
                    screen.right() - popup.width() - 8))
        y = max(screen.top() + 8, anchor.y() - popup.height() - 8)
        popup.move(x, y)
        popup.raise_()

    def _set_perf_pinned(self, pinned: bool, persist: bool = True) -> None:
        """常驻显示指标（True）或收进诊断弹层（False，默认）。

        两种模式共用同一份指标容器：addWidget 换父级即可搬运，
        PerformanceMonitor 的采集与 _set_perf 的写入完全不受影响。
        """
        widget = self._perf_bar_widget
        if pinned:
            self._perf_popup.hide()
            self._relayout_perf(True)
            self.statusBar().insertPermanentWidget(
                0, widget)  # 指标放最左：与"常驻读数"的旧位置一致
            widget.show()
            self._perf_toggle_btn.setToolTip(
                "性能指标已常驻（在设置 · 偏好中可改回弹层）")
        else:
            if widget.parent() is not self._perf_popup:
                self._perf_popup.layout().insertWidget(1, widget)
            self._relayout_perf(False)
            widget.setVisible(self._perf_popup.isVisible())
            self._perf_toggle_btn.setToolTip(
                "性能指标：延迟 / 帧率 / 渲染耗时 / OCR")
        if persist:
            set_pref(PERF_PINNED, bool(pinned))

    def _apply_theme_switch(self, name: str) -> None:
        """设置中心切主题：重建应用样式 + 重设系统背景明暗调。

        自绘控件（DockPanel 高光 / 水印 / 图标）在下一帧 paintEvent 直接
        读到新 Palette 值；已打开的对话框下次打开时取新令牌。
        """
        theme.set_theme(name)
        theme.apply_theme()
        if getattr(self, "_perf_popup", None) is not None:
            self._apply_perf_popup_qss()
        if getattr(self, "_backdrop", None) == "acrylic":
            try:
                theme.enable_backdrop(int(self.winId()), "acrylic")
            except Exception:  # noqa: BLE001 - 明暗重设失败不影响主题切换
                pass
        self.show_toast("已切换到浅色主题" if name == "light" else "已切换到深色主题",
                        "ok")

    # ------------------------------------------------------------------
    # 自动加入(automation)
    # ------------------------------------------------------------------
    def _sync_join_hotkey(self) -> None:
        """按配置注册/注销全局热键(启用才注册;改键后重注册)。"""
        cfg = self._automation_cfg
        if not cfg.enabled:
            self._join_hotkey.unregister()
            return
        if not self._join_hotkey.register(cfg.hotkey):
            self.show_toast(self._join_hotkey.last_error or "热键注册失败",
                            "error")

    def _on_automation_config_changed(self) -> None:
        """设置页·自动加入有改动:落盘 + 热键重注册。"""
        self._automation_cfg.save()
        self._sync_join_hotkey()

    def _on_join_hotkey(self) -> None:
        """热键触发:预检 → 刷新剪贴板 → 启动三步点击序列。"""
        cfg = self._automation_cfg
        if not cfg.enabled:
            return
        if not cfg.is_calibrated:
            self.show_toast("自动加入尚未校准:设置 → 自动加入 → 开始校准",
                            "warn")
            return
        code = getattr(self, "_pending_join_code", None)
        if not code:
            self.show_toast("还没有可加入的房间码(等 OCR 识别到新码)", "warn")
            return
        # 重写剪贴板,确保粘贴的是目标码(用户可能中途复制过别的;
        # force=True:与上次相同也必须写成功——重复过滤曾把第二次按 F8
        # 误判成"无法写入剪贴板"而中止)
        if not self._clipboard_manager.copy(code, force=True):
            self.show_toast("无法写入剪贴板,自动加入中止", "error")
            return
        self._join_controller.join(cfg)   # 结果统一走 _on_join_finished

    def _on_join_finished(self, result) -> None:
        """序列结束:toast 反馈(成功含比例漂移警告,失败给原因)。"""
        if result.ok:
            self.show_toast(
                f"自动加入已执行:{result.reason}"
                + (f"(注意:{result.warning})" if result.warning else ""),
                "ok" if not result.warning else "warn")
        else:
            self.show_toast(f"自动加入失败:{result.reason}", "error")

    def _start_join_calibration(self) -> None:
        """设置页「开始校准」入口(设置弹窗已自行关闭腾出屏幕)。"""
        self._calibration.start(self._automation_cfg)

    def _on_cal_step(self, index: int, prompt: str) -> None:
        self._join_guide.show_step(index, prompt)

    def _on_cal_invalid(self, message: str) -> None:
        self._join_guide.show_message(message)
        QTimer.singleShot(1500, lambda: (
            self._join_guide.show_step(self._calibration._step,
                                       STEP_PROMPTS[self._calibration._step])
            if self._calibration.active else None))

    def _on_cal_finished(self, ok: bool, message: str, cfg) -> None:
        self._join_guide.close()
        self.show_toast(message, "ok" if ok else "error")
        if ok and cfg is not None:
            cfg.save()
            self._sync_join_hotkey()

    def show_toast(self, text: str, tone: str = "neutral") -> None:
        """右下角 toast：不抢焦点、2.6s 自动消退（gui/toast.py）。"""
        if getattr(self, "_toast", None) is not None:
            self._toast.popup(text, tone)

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
        """地址栏宽度对齐（历史遗留钩子）。

        早期这里按 OCR 面板宽度动态钉住历史下拉的宽度；历史入口改为
        固定宽度按钮（gui/history_panel.HistoryButton）后，地址栏已全部
        是固定宽度控件，无需再做对齐。保留本方法使既有调用点不失效。
        """

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
    # ------------------------------------------------------------------
    # 数据集采集（测试旁路；总开关见 ocr/dataset_capture.py）
    # ------------------------------------------------------------------
    def _on_dataset_capture_toggled(self) -> None:
        """开始 / 停止数据集采集。

        只做两件事：起停 Recorder、必要时打开诊断记录。识别规则、识别线程、
        调度间隔、ROI、剪贴板一律不碰 —— 采集全程是旁路。
        """
        recorder = self._dataset_recorder
        if recorder is None:
            return

        if recorder.running:
            root = recorder.stop()
            if self._dataset_timer is not None:
                self._dataset_timer.stop()
            # 恢复采集前的诊断状态：我们可能为了拿到原始读法把诊断打开过。
            if not self._diag_enabled_before_dataset:
                self._diagnostics.set_enabled(False)
                self._update_diag_controls()
            self._dataset_btn.setText("数据集采集")
            self._refresh_dataset_status()
            if root:
                self.statusBar().showMessage(
                    "数据集已保存到 %s（有效 %d 张）"
                    % (root, recorder.stats.total))
            logger.info("[DATASET] 采集已停止 root=%s valid=%d",
                        root, recorder.stats.total)
            return

        # 开始前先记下诊断状态。原始读法只有在诊断开启时才由 worker 传出
        # （readings 在 trace 为 None 时是 None）；而本阶段要求 ground_truth
        # 取"PP-OCRv6 原始读法"，不是"纠正后的候选"，所以这里必须保证诊断是开的。
        # 诊断只做内存记录、不增加任何 OCR 调用，不会拖慢识别。
        self._diag_enabled_before_dataset = self._diagnostics.enabled
        if not self._diagnostics.enabled:
            self._diagnostics.set_enabled(True)
            self._update_diag_controls()

        root = recorder.start()
        self._dataset_btn.setText("停止采集")
        if self._dataset_timer is None:
            self._dataset_timer = QTimer(self)
            self._dataset_timer.setInterval(500)
            self._dataset_timer.timeout.connect(self._refresh_dataset_status)
        self._dataset_timer.start()
        self._refresh_dataset_status()
        self.statusBar().showMessage("数据集采集已开启：%s" % root)
        logger.info("[DATASET] 采集已开始 root=%s", root)

    def _refresh_dataset_status(self) -> None:
        """刷新三种格式的实时计数（§9）。只读计数，不改变任何行为。"""
        recorder = self._dataset_recorder
        if recorder is None or not hasattr(self, "_dataset_status"):
            return
        snap = recorder.stats.snapshot(recorder.targets)
        if not recorder.running and snap["total"] == 0 and snap["rejected"] == 0:
            self._dataset_status.setText("")
            return
        rows = []
        for fmt_id, _pattern in TARGET_FORMATS:
            n = snap["per_format"].get(fmt_id, 0)
            target = snap["targets"].get(fmt_id)
            rows.append("%-9s %6d%s" % (fmt_id.upper(), n,
                                        " / %d" % target if target else ""))
        rows.append("")
        rows.append("Total     %6d" % snap["total"])
        rows.append("valid %d    rejected %d    duplicate %d"
                    % (snap["total"], snap["rejected"], snap["duplicate"]))
        if snap["complete"]:
            rows.append("数据集目标已完成")
        self._dataset_status.setText("\n".join(rows))

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

    def _set_ocr_state(self, running: bool, error: bool = False,
                       phase: str = "") -> None:
        """OCR 状态是同一份状态的两处显示：右栏指示 + 状态栏芯片。

        phase 只在 running 时生效，用来区分「进程还在跑、但此刻还不能 OCR」
        的两个中间态（仅影响这两个芯片的文案与语气，不影响按钮与任何流程）：

            ""             引擎已就绪，且（如需要）会话也已建好 —— 真能 OCR
            "loading"      引擎正在加载（run() 里的运行时/模型/字典尚未就绪）
            "waiting_roi"  引擎已就绪，但会话还没建：ONNX 的输入 shape 要等
                           ROI 才能定下来，所以此刻还不能 OCR

        三态流转：加载中 → 等待 ROI → 运行中。
        不需要会话的后端（Paddle/Tesseract）没有「等待 ROI」态，直接从
        加载中进入运行中。
        """
        if error:
            self._set_chip(self._ocr_state_chip, "异常", "error", dot=True)
            self._set_chip(self._lbl_ocr, "OCR 异常", "error", dot=True)
        elif running and phase == "loading":
            self._set_chip(self._ocr_state_chip, "加载中", "warn", dot=True)
            self._set_chip(self._lbl_ocr, "OCR 加载中", "warn", dot=True)
        elif running and phase == "waiting_roi":
            self._set_chip(self._ocr_state_chip, "等待 ROI", "warn", dot=True)
            self._set_chip(self._lbl_ocr, "OCR 等待 ROI", "warn", dot=True)
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
            if getattr(self, "_fullscreen_action", None) is not None:
                self._fullscreen_action.setText(
                    "退出全屏工作台" if self.isFullScreen() else "全屏工作台")
            # 最小化时挂起未激活遮罩的呼吸动画（60fps 常驻重绘白烧 CPU）
            if getattr(self, "_activation_overlay", None) is not None:
                self._activation_overlay.set_suspended(self.isMinimized())

    # ------------------------------------------------------------------
    # 直播间历史（数据层见 stream/history.py，UI 见 gui/history_panel.py）
    # ------------------------------------------------------------------
    def _sync_note_action(self, *_args):
        """备注动作跟随【地址输入框】的内容。

        地址输入框是唯一事实来源：只要里面有地址（无论手输还是历史进入），
        「编辑备注…」就可对它生效。
        """
        if getattr(self, "_edit_note_action", None) is None:
            return
        url = self._url_input.text().strip()
        self._selected_url_for_note = url or None
        self._edit_note_action.setEnabled(bool(url))

    def _on_history_enter(self, url: str):
        """历史卡片 [进入]：填入地址并直接发起连接。

        URL 只作为存储键在数据层流转，不在界面上展示。
        """
        if not url:
            return
        self._url_input.setText(url)
        self._on_connect_clicked()

    def _on_edit_note_clicked(self):
        """编辑备注"""
        if not self._selected_url_for_note:
            return

        # 获取当前备注
        current_note = self._history.get_note(self._selected_url_for_note)

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
            self._history.set_note(self._selected_url_for_note, new_note)

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
            self._history.clear()
            self._sync_note_action()
            self.statusBar().showMessage("已清除历史记录")




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
            # 数据集采集接在**同一个**旁路出口上：关闭时它就是原来那个
            # SampleCapture，打开时是一层转发（SampleCapture + DatasetRecorder）。
            # worker 侧看到的始终是"一个带 capture() 的对象"，实现无需改变。
            sample_capture=self._capture_sink,
        )

        # 连接信号
        self._ocr_worker.text_updated.connect(self._on_ocr_text_updated)
        self._ocr_worker.copy_requested.connect(self._on_copy_requested)
        self._ocr_worker.game_mismatch.connect(self._on_ocr_game_mismatch)
        self._ocr_worker.error_occurred.connect(self._on_ocr_error)
        self._ocr_worker.started.connect(self._on_ocr_started)
        # 引擎就绪 / 会话就绪：只驱动状态芯片的三态显示，不参与 OCR 启停流程。
        self._ocr_worker.engine_ready.connect(self._on_ocr_engine_ready)
        self._ocr_worker.session_ready.connect(self._on_ocr_session_ready)
        self._ocr_worker.finished.connect(self._on_ocr_finished)

        # 后端是否走「会话」三态：必须在启动线程**之前**定下来 ——
        # _on_ocr_started 触发时引擎还没构造，那时无法探测。
        # 选择器报错时按「非会话后端」处理、退回原有芯片时序；真正的配置错误
        # 由 worker 的初始化路径权威上报（会走 error_occurred），GUI 不抢这个职责。
        try:
            self._ocr_session_backend = (_selected_engine_kind() == "onnx")
        except Exception:  # noqa: BLE001 - 配置错误交给 worker 报
            self._ocr_session_backend = False

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
        # ONNX 后端：线程已起、引擎还在工作线程里加载 → 芯片显示「加载中」，
        # 之后由 engine_ready / session_ready 推进到「等待 ROI」→「运行中」。
        # Paddle/Tesseract 保持原有行为：启动即「运行中」，不引入加载中态。
        # 按钮文案仍由 _update_ocr_button_state 独占（本方法里的 setText 一字未动）。
        self._set_ocr_state(
            True, phase="loading" if self._ocr_session_backend else ""
        )
        self._sync_ocr_button_variant()
        self._update_diag_controls()          # 运行期间锁定「全 PSM 观测」
        self.statusBar().showMessage("OCR 识别已启动")

        # 第四步聚光灯：用户真的把 OCR 跑起来了，引导随之完成。
        # 只认第四步 —— 其它入口（快捷键 / 之后的日常使用）不该动引导。
        spotlight = self._guide_spotlight
        if spotlight is not None and spotlight.step_index == 3:
            spotlight.notify_completed()

    def _on_ocr_engine_ready(self):
        """引擎加载完成（运行时 + 模型 + 字典已就绪，引擎对象可用）。

        与帧、ROI 无关，由 worker 在初始化成功后无条件发出一次。
        这是三态的中间一步：
          * 需要会话的后端（ONNX/DirectML）此刻**还没建 session** —— 它的输入
            shape 要等 ROI 才能定，所以停在「等待 ROI」；
          * 不需要会话的后端（Paddle/Tesseract）此刻已经能 OCR，直接进「运行中」，
            与新增该信号之前的表现一致。
        """
        if self._ocr_worker is None:
            return
        # 只有「会话」后端（ONNX）在会话建好之前还不能真正 OCR；
        # 其它后端此刻已经能跑，维持原有「运行中」，不引入新状态。
        self._set_ocr_state(
            True, phase="waiting_roi" if self._ocr_session_backend else ""
        )

    def _on_ocr_session_ready(self):
        """引擎的会话已建立并完成 warmup —— 此刻才真的可以 OCR。

        ROI 变化会让 worker 重建会话并再次发出该信号，因此这里也会再次确认
        「运行中」。只有提供会话能力的后端（ONNX）会发，其它后端从不触发。
        """
        # 迟到的排队信号可能落在停止之后：那时 worker 已被丢弃，
        # 不能让它把芯片从「未运行」重新点亮成「运行中」。
        if self._ocr_worker is None or not self._ocr_running:
            return
        self._set_ocr_state(True)

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
            # 自动加入:复制成功的码就是"待加入码"(主播换码自然覆盖;
            # 按热键时还会重写一次剪贴板,防用户中途复制了别的东西)
            self._pending_join_code = text
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







    def _on_help_clicked(self):
        """标题栏「使用引导」：随时重新打开引导（回顾模式）。"""
        if self._onboarding_dialog is not None:
            return
        # 首次引导的聚光灯正在屏幕上时，用户其实已经在被引导了，
        # 再叠一个模态向导只会把界面搞乱。
        if self._guide_spotlight is not None:
            return
        self._open_onboarding(first_run=False)

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
        """「⋯」菜单里的「联系作者」：在溢出按钮下方弹出联系方式卡片。"""
        self._open_contact_popup(self._more_btn)

    def _on_purchase_clicked(self):
        """未激活遮罩里的「购买授权」：用系统默认浏览器打开购买链接。

        刻意**不**经过 _guard_access：没激活的人正是要买授权的人；
        打开失败由 open_external_url 明确提示，绝不假报成功。
        """
        open_external_url(PURCHASE_URL, self)

    def _open_contact_popup(self, anchor) -> None:
        """在 anchor 下方弹出联系方式卡片（已打开则关闭）。

        非模态 —— 不改动直播、OCR 或授权状态，也不发任何网络请求；
        只是把作者联系方式摆出来，方便复制。
        """
        if self._contact_popup is not None and self._contact_popup.isVisible():
            self._contact_popup.close()
            return
        popup = InfoPopup(
            "联系作者",
            "问题反馈与功能建议",
            (
                ("QQ", CONTACT_QQ, "已复制 QQ 号"),
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
            popup.show_below(self._more_btn)

    def _on_settings_clicked(self):
        """设置中心：只读展示 + 追加授权入口，绝不停流 / 停 OCR / 动 ROI。"""
        dialog = SettingsDialog(
            manager=self._license_manager,
            version=self.current_version(),
            access_status=self._access_status,
            append_license_callback=self._on_license_appended,
            perf_pinned_callback=self._set_perf_pinned,
            theme_callback=self._apply_theme_switch,
            automation_callback=self._on_automation_config_changed,
            automation_config=self._automation_cfg,
            start_calibration_callback=self._start_join_calibration,
            parent=self,
        )
        dialog.exec()



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
        # 字段已迁 GuideCoordinator,但"用户动过面板"的语义源于这两个入口,
        # 写点跟着入口留在窗口(经 __dict__ 落窗口,协调器转发读到同一份)。
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
        """首次显示后启用系统背景 Acrylic（失败即保持不透明深色，不做视觉欺骗）。

        Acrylic（DWMSBT_TRANSIENTWINDOW）是真正的桌面高斯模糊 + 噪点，
        玻璃感远强于 Mica 的"取色墙纸"；代价是常驻窗口用它超出微软的
        推荐场景 —— 这里是产品视觉的明确选择，且 DWM 拒绝时整体回退。
        """
        super().showEvent(event)
        # 构建期的同步发生在窗口显示之前，那时 isVisible() 一律为 False，
        # 顶部操作区会被当成"面板已收起"。显示之后再同步一次拿到真实状态。
        self._sync_address_columns()
        if getattr(self, "_backdrop", None) is None:
            ok = theme.enable_backdrop(int(self.winId()), "acrylic")
            self._backdrop = "acrylic" if ok else "opaque"
            if ok:
                # 窗口本体与 central widget 都要标记并重刷样式，
                # QSS 才会把两者的不透明底色换成透明、露出系统模糊。
                for widget in (self, self._root):
                    set_property(widget, "mica", "true")
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
                # 个别系统在窗口完成合成前设置会退回浅色，显示后再补一次
                QTimer.singleShot(150, lambda: theme.enable_backdrop(
                    int(self.winId()), "acrylic"))
            logger.info("[UI] backdrop=%s", self._backdrop)

    def closeEvent(self, event):  # noqa: N802
        """窗口关闭时确保资源释放"""
        # RELEASE-02-B5：先置关闭标志 —— 此后到达的裸流解析结果一律丢弃，
        # 不再向正在销毁的 Qt 对象投递任何更新。
        self._closing = True
        # 数据集采集还在跑就先收尾：把队列排空、写好 dataset.json / manifest，
        # 否则最后一帧和 manifest 会丢。关闭时（未在采集）是空操作。
        if self._dataset_recorder is not None and self._dataset_recorder.running:
            try:
                self._dataset_recorder.stop()
            except Exception:  # noqa: BLE001 - 收尾失败也不能挡住关窗
                logger.debug("数据集收尾失败", exc_info=True)
        self._disconnect()
        super().closeEvent(event)
