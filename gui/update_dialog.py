# -*- coding: utf-8 -*-
"""
更新对话框 —— Velopack 前端的薄 UI 层 (Phase 3)。

范围：
    本文件只负责**界面与交互状态**。检查、下载、校验、解压、替换安装目录、
    重启，全部由 Velopack 完成（经 utils.update_service.UpdateService 转调）。
    Phase 1/2 时期这里还自己实现流式下载、Content-Length 解析、SHA-256 校验、
    ZIP 完整性检查 —— 那些已经全部删掉，改由 Velopack 承担，本文件不再
    复制一份。

状态机：
    有新版本(说明/确认) → 下载中(真实百分比) → 已就绪(提示重启) → 重启
                                          ↘ 失败(可重试)

关于「取消下载」：
    Velopack 不提供中断下载的接口，因此这里**不摆一个假的取消按钮**。
    下载中唯一可做的是「后台继续」：窗口收起，下载线程照常跑完 —— 下完之后
    包已经落在 Velopack 的 packages 目录里，即使程序没重启，下次启动时
    Velopack 也会自动应用它（App 的 auto-apply 为默认开启）。按钮文案如实
    描述这一点，而不是承诺一个做不到的取消。

关于进度：
    Velopack 的 download_updates 会真实上报 0..100 的百分比，所以直接用它，
    不伪造、不做假的平滑动画。总量取自发布清单（TargetFullRelease.Size）。

配色全部取自 gui/theme.Palette；QDialog 的应用级样式表由 theme.apply_theme()
统一安装，这里不重复设背景。
"""

import logging
import threading
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import GlassDialog
from gui.theme import Palette
from utils.update_service import UpdateService, UpdateError

logger = logging.getLogger("DouyinLowLatencyViewer.gui.update")


def format_size(num_bytes: int) -> str:
    """字节数 → 人类可读。"""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes} B"


class _DownloadWorker:
    """后台线程：跑一次 Velopack 下载，把进度与结果送回 GUI 线程。

    存在理由：Velopack 的 download_updates 是**阻塞**调用（内部 I/O 线程在跑，
    但它自己在调用线程上循环等进度回调），直接放在 GUI 线程会冻住界面。
    因此照搬本项目既有的模式（gui.activation_dialog.LicenseWorker /
    已验证过的更新下载线程）：线程内调用回调，回调再 emit Qt Signal，
    由 Qt 排队投递回 GUI 线程。不引入 asyncio，也不引入 QThread 体系。

    进度回调来自 Velopack，其入参是 0..100 的整数百分比。
    """

    def __init__(self, service: UpdateService, info, on_progress, on_done):
        self._service = service
        self._info = info
        self._on_progress = on_progress
        self._on_done = on_done                   # callback(worker, error)
        self._thread = threading.Thread(
            target=self._run, name="velopack-download", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._service.download(self._info, progress=self._on_progress)
        except UpdateError as e:
            logger.warning("更新下载失败: %s", e)
            self._on_done(self, str(e))
        except Exception as e:  # noqa: BLE001 - 兜底：线程不能静默死掉
            logger.exception("更新下载异常")
            self._on_done(self, f"更新下载异常: {e}")
        else:
            self._on_done(self, None)


class UpdateDialog(GlassDialog):
    """「发现新版本」→「下载」→「已就绪，重启生效」。"""

    #: 更新包已下载完成（Velopack 已落盘并校验），可以应用了。
    update_ready = Signal(object)          # (UpdateInfo)

    #: 用户点击「立即重启并更新」。由主窗口接手：先停流收尾，再交给 Velopack。
    apply_requested = Signal(object)       # (UpdateInfo)

    # 后台线程 → GUI 线程
    _progress_sig = Signal(int)            # 百分比 0..100
    _done_sig = Signal(object, object)     # (worker, error|None)

    _STATE_IDLE = 0
    _STATE_DOWNLOADING = 1
    _STATE_READY = 2
    _STATE_FAILED = 3

    def __init__(self, service: UpdateService, info, parent=None):
        """
        :param service: utils.update_service.UpdateService
        :param info: velopack.UpdateInfo（check() 的返回值）
        """
        super().__init__("发现新版本", parent)
        self._service = service
        self._info = info
        self._asset = info.TargetFullRelease
        self._worker: Optional[_DownloadWorker] = None
        self._downloaded = False

        self.setFixedSize(460, 380)

        self._progress_sig.connect(self._on_progress)
        self._done_sig.connect(self._on_done)
        self._setup_ui()
        self._set_state(self._STATE_IDLE)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def version(self) -> str:
        """可更新到的版本号。"""
        return self._asset.Version

    def is_downloading(self) -> bool:
        """下载是否仍在后台进行（窗口可能是收起状态）。"""
        return self._worker is not None and self._worker.is_running()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        layout = self.card_layout
        layout.setContentsMargins(20, 4, 20, 4)

        self._version_label = QLabel(
            f"当前版本 {self._service.current_version()}　→　最新版本 {self._asset.Version}"
        )
        versions.setStyleSheet(
            f"QLabel {{ font-size: 13px; color: {Palette.TEXT}; background: none; }}"
        )
        self._version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._version_label)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_notes_page())        # 0
        self._stack.addWidget(self._build_progress_page())     # 1
        self._stack.addWidget(self._build_result_page())       # 2
        layout.addWidget(self._stack, 1)

        layout.addLayout(self._build_buttons())

    def _build_notes_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        size = self._asset.Size
        caption = QLabel(
            f"更新说明　（下载大小 {format_size(size)}）" if size > 0 else "更新说明"
        )
        caption.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM}; background: none; }}"
        )
        page_layout.addWidget(caption)

        # 更新说明的长度由发布清单决定，可能很长。放进滚动区，避免长文本把
        # 固定尺寸的对话框撑破。
        notes = QLabel(self._asset.NotesMarkdown.strip() or "（本次更新未提供说明）")
        notes.setWordWrap(True)
        notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        notes.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        notes.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM};"
            f" background: {Palette.INPUT_BG}; border: 1px solid {Palette.BORDER_SOFT};"
            f" border-radius: 4px; padding: 8px; }}"
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(notes)
        container_layout.addStretch(1)
        scroll.setWidget(container)
        page_layout.addWidget(scroll, 1)

        return page

    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)
        page_layout.addStretch(1)

        self._progress_label = QLabel("正在连接更新源…")
        self._progress_label.setStyleSheet(
            f"QLabel {{ font-size: 13px; color: {Palette.TEXT}; background: none; }}"
        )
        page_layout.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(10)
        # 进度条样式写在控件上：gui/theme.py 不在本阶段允许修改的范围内
        self._progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {Palette.INPUT_BG};"
            f" border: 1px solid {Palette.BORDER}; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {Palette.ACCENT};"
            f" border-radius: 3px; }}"
        )
        page_layout.addWidget(self._progress_bar)

        self._progress_note = QLabel("下载完成后由 Velopack 校验并安装，无需手动操作。")
        self._progress_note.setWordWrap(True)
        self._progress_note.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_MUTED}; background: none; }}"
        )
        page_layout.addWidget(self._progress_note)
        page_layout.addStretch(1)

        return page

    def _build_result_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)
        page_layout.addStretch(1)

        self._result_title = QLabel()
        self._result_title.setWordWrap(True)
        page_layout.addWidget(self._result_title)

        self._result_detail = QLabel()
        self._result_detail.setWordWrap(True)
        self._result_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._result_detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        page_layout.addWidget(self._result_detail)
        page_layout.addStretch(1)

        return page

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self._later_btn = QPushButton("稍后", self)
        self._later_btn.setFixedHeight(36)
        self._later_btn.setMinimumWidth(96)
        self._later_btn.clicked.connect(self.reject)

        self._start_btn = QPushButton("立即更新", self)
        self._start_btn.setFixedHeight(36)
        self._start_btn.setMinimumWidth(110)
        self._start_btn.setProperty("variant", "primary")
        self._start_btn.clicked.connect(self._start_download)

        # 下载中：不提供「取消」（Velopack 无法中断下载），提供「后台继续」。
        self._background_btn = QPushButton("后台继续", self)
        self._background_btn.setFixedHeight(36)
        self._background_btn.setMinimumWidth(110)
        self._background_btn.clicked.connect(self._go_background)

        self._close_btn = QPushButton("关闭", self)
        self._close_btn.setFixedHeight(36)
        self._close_btn.setMinimumWidth(110)
        self._close_btn.clicked.connect(self.accept)

        self._retry_btn = QPushButton("重试", self)
        self._retry_btn.setFixedHeight(36)
        self._retry_btn.setMinimumWidth(110)
        self._retry_btn.setProperty("variant", "primary")
        self._retry_btn.clicked.connect(self._start_download)

        self._restart_btn = QPushButton("立即重启并更新", self)
        self._restart_btn.setFixedHeight(36)
        self._restart_btn.setMinimumWidth(140)
        self._restart_btn.setProperty("variant", "primary")
        self._restart_btn.clicked.connect(self._request_apply)

        row.addWidget(self._later_btn)
        row.addWidget(self._background_btn)
        row.addWidget(self._close_btn)
        row.addStretch(1)
        row.addWidget(self._retry_btn)
        row.addWidget(self._restart_btn)
        row.addWidget(self._start_btn)
        return row

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_state(self, state: int) -> None:
        """切换界面状态。按钮用 show/hide 互斥，避免重建布局。"""
        self._later_btn.setVisible(state == self._STATE_IDLE)
        self._start_btn.setVisible(state == self._STATE_IDLE)
        self._background_btn.setVisible(state == self._STATE_DOWNLOADING)
        self._close_btn.setVisible(state in (self._STATE_READY, self._STATE_FAILED))
        self._retry_btn.setVisible(state == self._STATE_FAILED)
        self._restart_btn.setVisible(state == self._STATE_READY)

        if state == self._STATE_IDLE:
            self._stack.setCurrentIndex(0)
        elif state == self._STATE_DOWNLOADING:
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(2)

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def _start_download(self) -> None:
        if self._worker is not None:
            return

        self._progress_bar.setValue(0)
        self._progress_label.setText("正在连接更新源…")
        self._set_state(self._STATE_DOWNLOADING)

        worker = _DownloadWorker(
            self._service,
            self._info,
            on_progress=self._progress_sig.emit,
            on_done=self._done_sig.emit,
        )
        self._worker = worker
        worker.start()

    def _go_background(self) -> None:
        """收起窗口，下载继续在后台跑完。

        不销毁对话框、不打断线程：下载完成后 Velopack 已经把包放在 packages
        目录里，即使这次没重启，下次启动也会被自动应用。
        """
        logger.info("更新下载转入后台继续")
        self.hide()

    def _request_apply(self) -> None:
        """用户选择立即重启。真正的应用动作由主窗口执行（它要先停流收尾）。"""
        logger.info("用户请求重启并应用更新 %s", self._asset.Version)
        self.accept()
        self.apply_requested.emit(self._info)

    # ------------------------------------------------------------------
    # 后台回调（都在 GUI 线程执行，经 Qt 排队投递）
    # ------------------------------------------------------------------
    def _on_progress(self, percent: int) -> None:
        self._progress_bar.setValue(max(0, min(100, int(percent))))
        size = self._asset.Size
        if size > 0:
            self._progress_label.setText(
                f"正在下载 {int(percent)}%（共 {format_size(size)}）"
            )
        else:
            self._progress_label.setText(f"正在下载 {int(percent)}%")

    def _on_done(self, worker, error) -> None:
        if worker is not self._worker:
            return      # 过期回调
        self._worker = None

        if error is None:
            self._downloaded = True
            self._show_ready()
            self.update_ready.emit(self._info)
        else:
            self._show_error(error)

    # ------------------------------------------------------------------
    # 结果展示
    # ------------------------------------------------------------------
    def _show_ready(self) -> None:
        self._result_title.setText("更新已下载完成")
        self._result_title.setStyleSheet(
            f"QLabel {{ font-size: 14px; font-weight: 600;"
            f" color: {Palette.OK_TEXT}; background: none; }}"
        )
        self._result_detail.setText(
            f"新版本 {self._asset.Version} 已下载完成，重启后生效。\n\n"
            "选择「稍后」也可以：更新包已保存，下次启动程序时会自动完成更新。"
        )
        self._result_detail.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM};"
            f" background: {Palette.INPUT_BG}; border: 1px solid {Palette.BORDER_SOFT};"
            f" border-radius: 4px; padding: 8px; }}"
        )
        self._set_state(self._STATE_READY)

    def _show_error(self, message: str) -> None:
        self._result_title.setText("更新下载失败")
        self._result_title.setStyleSheet(
            f"QLabel {{ font-size: 14px; font-weight: 600;"
            f" color: {Palette.DANGER_TEXT}; background: none; }}"
        )
        self._result_detail.setText(
            f"{message}\n\n可以稍后重试；本次失败不影响当前版本的正常使用。"
        )
        self._result_detail.setStyleSheet(
            f"QLabel {{ font-size: 12px; color: {Palette.TEXT_DIM};"
            f" background: {Palette.DANGER_BG}; border: 1px solid {Palette.DANGER_BORDER};"
            f" border-radius: 4px; padding: 8px; }}"
        )
        self._set_state(self._STATE_FAILED)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def reject(self) -> None:
        """Esc / 「稍后」/ 关闭：不打断已在后台进行的下载。

        下载线程是 daemon 且在后台照常跑完，进度回调照常更新这个（已隐藏的）
        对话框；它不会因为窗口关掉而失败，也不需要额外交接。
        """
        if self.is_downloading():
            logger.info("下载进行中，窗口关闭但下载继续")
        super().reject()
