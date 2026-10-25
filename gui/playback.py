# -*- coding: utf-8 -*-
"""播放状态机协调器(Phase 3 拆分,自 gui/main_window.py 外迁)。

一条连接会话的完整生命周期:连接发起 → 裸流解析 → 候选 failover →
启动播放 → 帧消费 → 健康检查 → 断开/teardown。会话字段(_reader/
_frame_buffer/_failover/…)由协调器代管、落窗口实例字典;方法经
MainWindow.__getattr__ 转发(兼容架构见 gui/coordinators.py 与
docs/refactor/main_window-split-plan.md)。

可打桩 worker(_ConnectWorker/_BareResolveWorker/_FrameBridge/
_FrameNotifier)的构造点经 sys.modules['gui.main_window'] 运行时解析,
mw.X = _Stub 打桩继续生效。
"""

from __future__ import annotations

import logging
import threading
import time

from stream.bare_source import (REASON_DISABLED, bare_stream_applicable,
                                reason_message)
from stream.failover import CandidateFailover
from stream.models import StreamCandidate, StreamInfo
from utils import ffmpeg7_runtime
from utils.performance import PerformanceMonitor
from utils.startup_timing import STAGE_CANDIDATE_SELECTED, STAGE_FIRST_DISPLAY, StartupTiming
from gui.widgets import set_property


def _mw():
    import sys

    return sys.modules["gui.main_window"]


logger = logging.getLogger("DouyinLowLatencyViewer.gui.playback")


class PlaybackCoordinator:
    """连接会话状态机(无 QObject 基类:定时器/信号都挂在窗口上)。"""

    def __init__(self, window):
        self._w = window
        w = window
        w._attempt_id = 0
        w._startup_timing = None
        w._last_failover_ms = 0.0
        w._reader = None
        w._frame_buffer = None
        w._frame_notifier = None
        w._frame_bridge = None
        w._playback_info = None
        w._failover = None
        w._connect_worker = None
        w._bare_worker = None
        w._bare_req_id = 0
        w._last_displayed_frame_id = -1
        w._perf = None            # PerformanceMonitor 在会话启动时构造
        w._connected = False
        # 会话定时器(parent=窗口;接线在协调器,窗口不再触碰)
        from PySide6.QtCore import QTimer

        w._frame_timer = QTimer(w)
        w._frame_timer.timeout.connect(self._on_frame_timeout)
        w._status_timer = QTimer(w)
        w._status_timer.timeout.connect(self._on_status_timeout)

    def __getattr__(self, name):
        # 与 gui.coordinators._CoordinatorBase 相同的受限转发(跨域引用:
        # 播放读授权守门/写状态芯片等),object.__getattribute__ 防回环。
        w = self.__dict__.get("_w")
        if w is None:
            raise AttributeError(name)
        try:
            return object.__getattribute__(w, name)
        except AttributeError:
            pass
        for coord_name in ("_playback_coord", "_license_coord",
                           "_guide_coord", "_update_coord"):
            coord = w.__dict__.get(coord_name)
            if coord is None or coord is self:
                continue
            try:
                return object.__getattribute__(coord, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"PlaybackCoordinator has no attribute {name!r}")


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

        url = self._w._url_input.text().strip()
        if not url:
            self._w.statusBar().showMessage("错误：请输入直播间URL")
            return

        self._w._connect_btn.setEnabled(False)
        self._w._connect_btn.setText("连接中...")
        self._w._url_input.setEnabled(False)
        self._w.statusBar().showMessage(f"正在解析直播间: {url}")

        # 历史记录在**成功进入直播间**后才写入（_handle_connect_success），
        # 失败的链接不进历史 —— 这里只记住本次目标 URL。
        self._w._pending_history_url = url

        self._w._connect_worker = _mw()._ConnectWorker(
            live_url=url,
            on_success=self._on_connect_success,
            on_error=self._on_connect_error,
            resolver=self._resolver,
        )
        self._connect_worker.start()


    def _on_connect_success(self, info: StreamInfo):
        """连接成功回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_success called (bg thread), emitting _connect_success_sig")
        self._w._connect_success_sig.emit(info)


    def _handle_connect_success(self, info: StreamInfo):
        """在 GUI 线程中处理连接成功：建立候选迭代器，启动第一个候选"""
        self._w.statusBar().showMessage(
            f"已获取流地址 (room={info.room_id}, type={info.protocol})，"
            f"正在连接视频流..."
        )
        logger.info(
            "[GUI] _handle_connect_success: room_id=%s, type=%s, candidates=%d, url=%s",
            info.room_id, info.protocol, len(info.candidates or ()),
            (info.url or "")[:80],
        )

        # 成功进入直播间：写入历史（需求：失败链接不进历史）。
        # 记录的是用户输入的 URL（可重新进入），room_id 用解析结果。
        if self._w._pending_history_url:
            self._w._history.record_success(self._w._pending_history_url,
                                         info.room_id or "")
            self._w._pending_history_url = None

        # Phase 2.5：本次会话的候选迭代器。候选顺序**完全由平台适配器给出**
        # （适配器里已按平台自己的偏好排好序），这里只负责"按顺序逐个试"，
        # 不重排、不评分、不测速，也不认识任何具体平台。
        # 抖音会话的候选集由取流结果决定（唯一候选 = 裸流本身），见
        # _on_bare_resolved()；虎牙 / B 站直接使用适配器给出的候选。
        self._w._failover = CandidateFailover(info)
        self._w._playback_info = info
        self._w.show_toast("已解析直播间，正在连接视频流…", "ok")

        # RELEASE-02-B5-CLEANUP：唯一取流入口。运行时校验与裸流页面直取都在
        # 后台线程完成（不阻塞 GUI），拿到结果后才开始播放 —— 全程只有一条
        # 播放管线，不存在第二路解码，也不存在任何旧路径回退。
        self._begin_bare_resolution(info)

    # ------------------------------------------------------------------
    # RELEASE-02-B5-CLEANUP：唯一取流入口（后台）+ 会话启动
    # ------------------------------------------------------------------
    def _begin_bare_resolution(self, info: StreamInfo) -> None:
        """开始本次会话的取流解析（后台线程），完成后才启动播放。

        - 非抖音（虎牙 / B 站）/ 总开关显式关闭 → 立即用适配器给的候选启动，
          不请求页面、不等运行时探测；
        - 抖音 → 后台 worker：运行时校验（带缓存）→ 页面直取裸流 URL。
          取不到或运行时不可用 → **明确提示并结束会话**（不切换任何旧路径）。
        """
        self._w._bare_url = None
        self._w._bare_exe = None
        self._w._bare_req_id += 1
        req_id = self._w._bare_req_id

        # 所有平台都要过同一个 FFmpeg 7 运行时校验（内置 FFmpeg 6 已移除，
        # 不再存在任何内置解码器）。抖音额外要页面直取裸流 URL。
        self._w._bare_worker = _mw()._BareResolveWorker(
            request_id=req_id,
            platform=info.platform,
            room_id=info.room_id or "",
            want_bare=bare_stream_applicable(info.platform),
            on_done=self._on_bare_done,
        )
        self._bare_worker.start()
        logger.info("[BARE] 已启动后台取流准备 (req=%d platform=%s room=%s)",
                    req_id, info.platform, info.room_id)


    def _on_bare_done(self, request_id: int, result) -> None:
        """worker 回调（**后台线程**）→ 经 Signal 回到 GUI 线程。"""
        self._w._bare_resolved_sig.emit(request_id, result)


    def _on_bare_resolved(self, request_id: int, result) -> None:
        """在 GUI 线程处理取流结果（含迟到回调保护）。

        RELEASE-02-B5-CLEANUP：这里**没有**"失败就降级到旧路径"的分支 ——
        取不到流或运行时不可用都只有一条出路：给用户一句明确的话并结束会话。
        """
        # 会话已切换（新解析已开始）/ 已断开 / 正在关闭 → 丢弃旧结果
        if request_id != self._w._bare_req_id:
            logger.info("[BARE] 丢弃过期结果 (req=%d 当前=%d)",
                        request_id, self._w._bare_req_id)
            return
        if self._w._closing or self._playback_info is None:
            logger.info("[BARE] 会话已结束，丢弃结果 (req=%d)", request_id)
            return

        self._w._bare_worker = None
        self._w._bare_exe = result.exe

        if not result.runtime_ok:
            self._abort_session(
                f"{ffmpeg7_runtime.reason_message(result.reason or '')}，无法播放直播流",
                result.reason, result.detail)
            return

        if not result.url:
            if (self._playback_info.platform or "").lower() == "douyin":
                # 抖音只能走裸流（旧 `_uhd` 转码档候选已整体移除）：开关被显式
                # 关闭或取流失败，都意味着本会话没有可播放来源。
                self._abort_session(reason_message(result.reason
                                                   or REASON_DISABLED),
                                    result.reason, result.detail)
                return
            # 虎牙 / B 站：候选由适配器给出，运行时已就绪
            logger.info("[FFMPEG7] 运行时就绪，使用平台候选 (exe=%s)", result.exe)
            self._start_session_failover()
            return

        self._w._bare_url = result.url
        logger.info("[BARE] 本次会话使用裸流 (exe=%s)", result.exe)

        # 唯一候选 = 裸流本身。旧 `_uhd` 转码档候选已整体移除，因此抖音会话
        # 的候选集恒为 1 个；`CandidateFailover` 退化为"单候选迭代"，
        # 但虎牙 / B 站的多候选能力不受影响（它们不走这里）。
        info = self._playback_info
        cand = StreamCandidate(url=result.url, protocol="flv")
        self._w._failover = CandidateFailover(StreamInfo(
            platform=info.platform, room_id=info.room_id,
            candidates=[cand], selected=cand))
        self._start_session_failover()


    def _start_session_failover(self) -> None:
        """启动本次会话的候选尝试（取流解析完成后的唯一入口）。"""
        if self._w._closing or self._playback_info is None:
            return
        if not self._failover_next():
            self._failover_exhausted()
            return
        self._apply_connected_ui()


    def _abort_session(self, message: str, reason: str | None,
                       detail: str = "") -> None:
        """本会话无法建立播放：给用户一句明确的话并结束。

        刻意**不**把路径 / 环境变量 / 底层错误码放进 UI —— 那些只进日志。
        也**不**静默切换到任何旧路径（唯一拉流架构）。
        """
        logger.warning("[BARE] 会话终止: reason=%s detail=%s", reason, detail)
        self._disconnect()
        # 写在 _disconnect() 之后：否则会被它结尾的"已断开"覆盖
        self._w.statusBar().showMessage(message)


    def _start_runtime_probe(self) -> None:
        """启动时在后台探测一次 FFmpeg7 运行时（仅日志 + 预热缓存）。

        刻意不阻塞启动、不弹 UI：可用性对**用户**的呈现发生在连接时（状态栏
        提示）；这里保证"安装后能从日志明确判断运行时是否可用"，并让首次连接
        的裸流解析不必再等一次进程探测。
        """

        def _run() -> None:
            try:
                st = ffmpeg7_runtime.status()
                if st.ok:
                    logger.info("[FFMPEG7] 启动探测：可用 %s", st.as_log_line())
                else:
                    logger.warning(
                        "[FFMPEG7] 启动探测：不可用，裸流将回退标准路径 %s",
                        st.as_log_line(),
                    )
            except Exception:  # noqa: BLE001 - 探测失败绝不影响启动
                logger.exception("[FFMPEG7] 启动探测异常")

        threading.Thread(target=_run, name="ffmpeg7-probe", daemon=True).start()

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
        self._w._attempt_id += 1

        # Phase 3.2：本次尝试的启动分段计时。t0 = 决定试这个候选的那一刻；
        # 覆盖构造器内的 ffprobe、FFmpeg 启动、首帧到达，直到 GUI 首次取到帧。
        # 计时只记录事实，不改变下面任何一步的行为。
        timing = StartupTiming()
        timing.note(f"attempt={self._attempt_id}")
        if self._failover is not None:
            timing.note(f"candidate={self._failover.attempt_number}/{self._failover.total}")
        timing.note(f"proto={candidate.protocol}")
        timing.mark(STAGE_CANDIDATE_SELECTED)
        self._w._startup_timing = timing

        # 分辨率规则（迁移审计刀1后）：解析层不再串行探测分辨率——FFmpegReader
        # 会从子进程 stderr 的 dump 行自报（同一连接，零额外开销）。info.width
        # 理论上恒为 None；保留传参通道仅为向后兼容（未来若某平台有免费的
        # 免连流分辨率来源，可以直接填进来）。
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

        # 创建并启动 FFmpegReader（protocol 供 flv 候选强制 live_flv demuxer）
        reader = None
        try:
            # RELEASE-02-B5-CLEANUP：**所有平台**都用外部 FFmpeg 7 运行时
            # （内置 FFmpeg 6 已随本任务移除，不再存在任何内置解码器）。
            # 运行时路径在会话开始时已在后台线程定位并校验（self._w._bare_exe）；
            # 失败不会再走到这里（会话已在此之前明确终止）。
            # 抖音会话的唯一候选就是裸流本身（旧 `_uhd` 候选已整体移除）；
            # 虎牙 / B 站的候选由各自适配器给出。
            is_bare = bool(self._w._bare_url and candidate.url == self._w._bare_url)
            logger.info("[FFMPEG] %s播放（ffmpeg7 运行时 %s），url=%s",
                        "裸流" if is_bare else "平台候选",
                        self._w._bare_exe, (candidate.url or "")[:60])
            reader = _mw().FFmpegReader(
                candidate.url,
                width=video_width,
                height=video_height,
                use_cuda=True,
                http_headers=media_headers,
                timing=timing,
                audio_enabled=True,
                audio_pcm_callback=self._audio.feed,
                audio_sample_rate=self._audio.sample_rate,
                audio_channels=self._audio.channels,
                protocol=candidate.protocol,
                exe_path=self._w._bare_exe,
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

        self._w._reader = reader

        # 音频随视频一起启动（同一 FFmpeg 实例的第二路输出）。
        # 音频任何失败都在 service 内部消化，不会影响之后的视频初始化。
        self._audio.start()
        logger.info("[AUDIO] service.start() called (with reader attempt=%d)",
                    self._attempt_id)

        # 创建 LatestFrameBuffer
        self._w._frame_buffer = _mw().LatestFrameBuffer()
        logger.info("[BUFFER] LatestFrameBuffer created")

        # 创建 PerformanceMonitor 并启动
        self._w._perf = PerformanceMonitor()
        self._perf.start()

        # Phase B-1：把 set_frame / paintEvent 的耗时观测挂到视频控件上。
        # 只汇总统计，不改变任何渲染行为。
        self._video_widget.attach_monitor(self._perf)

        # 创建并启动 FrameBridge: reader → buffer (+ perf.on_capture)
        self._w._frame_bridge = _mw()._FrameBridge(self._reader, self._frame_buffer, self._perf)
        # 先接线再启动：确保 bridge 一开始发通知时消费者已经就位。
        # 通知器随 bridge 新建，重连/换候选即换新对象，不存在重复连接。
        self._w._frame_notifier = self._frame_bridge.notifier
        self._frame_notifier.frameReady.connect(self._w._on_frame_ready)
        self._frame_bridge.start()
        logger.info("[BRIDGE] FrameBridge.start() called (frameReady 通知驱动)")

        # 消费位置复位：buffer 是新建的，frame_id 从 0 重新起算
        self._w._last_displayed_frame_id = 0

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
        self._w._connected = True
        self._w._connect_btn.setEnabled(True)
        self._w._connect_btn.setText("断开")
        # 连接后主按钮语义从「执行」变为「中断」，用危险色把这一点说清楚
        set_property(self._w._connect_btn, "variant", "danger")
        self._w._url_input.setEnabled(False)
        # 连接后启用 ROI 选择（访问被拒绝时保持禁用）
        self._w._roi_btn.setEnabled(not self._w._access_status.is_expired())

        # 隐藏连接状态消息
        self._w._connection_status_label.hide()

        # 更新性能面板（分辨率随候选更新，尽量反映当前实际播放的那一路）。
        # 尺寸未知的候选（Phase 3.3）此刻还没探测完：显示"--"而不是 "None × None"，
        # 真正的数值会在首帧到达后由每秒一次的状态刷新补上（见 _on_status_timeout）。
        if self._reader is not None:
            if self._reader.width and self._reader.height:
                self._w._set_chip(
                    self._w._lbl_resolution,
                    f"{self._reader.width} × {self._reader.height}",
                )
            else:
                self._w._set_chip(self._w._lbl_resolution, "分辨率 --")
        self._w._set_chip(self._w._lbl_status, "连接中", "warn", dot=True)

        # 更新 OCR 按钮状态
        self._w._update_ocr_button_state()

        self._w._sync_note_action()

        # 第一步聚光灯：连接成功了才自动往下走。
        # 放在方法末尾，让"已连接"这版界面先落定再谈引导。
        # 只在第一步这样推进 —— 连接成功与第二步（选游戏）无关。
        spotlight = self._w._guide_spotlight
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
        self._w.statusBar().showMessage("连接失败：所有候选流均不可用")
        self._w.show_toast("连接失败：所有候选流均不可用", "error")


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
            self._w.statusBar().showMessage(status_message)
            self._disconnect()
            return

        # 从未成功 → 该候选不可用：结算它、清理它，再试下一个
        self._failover.note_failure()

        # RELEASE-02-B5-CLEANUP：这里原为"裸流已失败 → 丢弃裸流、后续候选走
        # 旧 _uhd 路径"的会话级抑制。旧路径整体移除后已无意义：
        # 抖音会话只有 1 个候选（裸流本身），它失败即候选耗尽，由下方
        # _failover_next() 返回 False 走到明确的失败提示。

        t0 = time.perf_counter()
        self._teardown_playback()

        # _teardown_playback() 本身不改变 attempt_id（也不修改 _connected）；
        # 这里显式校验一次，保证将来任何异步清理实现都不会让旧 attempt
        # 在清理之后启动新 reader。
        if self._attempt_id != attempt:
            logger.warning("[FAILOVER] attempt %d 已失效，放弃切换候选", attempt)
            return

        if self._failover_next():
            self._w._last_failover_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "[FAILOVER] attempt %d 失败 → 切换到候选 %d/%d，耗时 %.0f ms",
                attempt, self._failover.attempt_number, self._failover.total,
                self._last_failover_ms,
            )
            # 会话仍是 connected：只刷新 UI，不进入断开流程
            self._apply_connected_ui()
            self._w.statusBar().showMessage(
                f"已切换到备用线路 ({self._failover.attempt_number}/"
                f"{self._failover.total})，正在连接视频流..."
            )
            return

        self._w._last_failover_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning("[FAILOVER] attempt %d 失败，且已无更多候选可用", attempt)
        self._disconnect()
        # 同上：写在 _disconnect() 之后才不会被"已断开"覆盖
        self._w.statusBar().showMessage("连接失败：所有候选流均不可用")
        self._w.show_toast("连接失败：所有候选流均不可用", "error")


    def _on_connect_error(self, error_msg: str):
        """连接失败回调（从后台线程调用，通过 Signal 安全回到 GUI 线程）"""
        logger.info("[GUI] _on_connect_error called (bg thread), emitting _connect_error_sig")
        self._w._connect_error_sig.emit(error_msg)


    def _handle_connect_error(self, error_msg: str):
        """在 GUI 线程中处理连接失败"""
        self._reset_connect_ui()
        self._w.statusBar().showMessage(f"连接失败: {error_msg}")
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
            self._w._frame_bridge = None

        notifier = self._frame_notifier
        if notifier is not None:
            try:
                notifier.frameReady.disconnect(self._on_frame_ready)
            except (RuntimeError, TypeError):
                pass        # 从未连接 / 已断开：无需处理
            notifier.clear_pending()
            self._w._frame_notifier = None

        # 停止 FFmpegReader
        if self._reader is not None:
            self._reader.stop()
            self._w._reader = None

        # 停止音频（跟随播放生命周期；failover 换候选时同样拆除重建）
        self._audio.stop()

        # 清空 buffer
        if self._frame_buffer is not None:
            self._frame_buffer.clear()
            self._w._frame_buffer = None

        # 重置性能监控
        if self._perf is not None:
            # 先解挂，避免控件继续写入已废弃的监控对象
            self._video_widget.attach_monitor(None)
            self._perf.reset()
            self._w._perf = None


    def _disconnect(self):
        """断开连接，停止一切"""
        # 先让所有在途 attempt 失效：任何排队中的回调都不会再启动 reader
        self._w._attempt_id += 1

        # RELEASE-02-B5：让在途的裸流解析请求失效并取消（旧结果不得覆盖
        # 下一轮会话），同时清掉本会话已解析出的裸流结果与运行时路径。
        self._w._bare_req_id += 1
        if self._bare_worker is not None:
            self._bare_worker.cancel()
            self._w._bare_worker = None
        self._w._bare_url = None
        self._w._bare_exe = None

        # 取消并丢弃候选迭代器：本会话不再尝试任何候选
        if self._failover is not None:
            self._failover.cancel()
            self._w._failover = None
        self._w._playback_info = None

        # 停止 OCR
        # 注意：这一步只在整条会话结束时做。failover 切换候选不走这里 ——
        # OCRWorker 通过 lambda 晚绑定读取 _frame_buffer，换 buffer 无需重启，
        # 用户的 ROI 与游戏档也应当保留。
        self._w._stop_ocr()

        # 释放播放资源（reader / buffer / perf / bridge / notifier / 定时器）
        self._teardown_playback()

        # 清除画面
        self._video_widget.clear_frame()
        # 隐藏ROI框
        self._video_widget.clear_roi()

        self._w._sync_note_action()

        # 重置 UI
        self._reset_connect_ui()
        self._reset_perf_ui()
        self._w._roi_btn.setEnabled(False)
        self._w._connection_status_label.show()
        self._w.statusBar().showMessage("已断开")

        # 断开后重新套用访问限制：访问已到期时连接入口应保持关闭
        self._w._on_access_status_changed()


    def _reset_connect_ui(self):
        """重置连接按钮和输入框为初始状态"""
        self._w._connect_btn.setEnabled(True)
        self._w._connect_btn.setText("连接")
        set_property(self._w._connect_btn, "variant", "primary")
        self._w._url_input.setEnabled(True)
        self._w._connected = False
        self._w._sync_note_action()


    def _reset_perf_ui(self):
        """重置性能面板为初始状态"""
        self._w._set_chip(self._w._lbl_fps, "FPS --")
        self._w._set_chip(self._w._lbl_latency, "延迟 --")
        self._w._set_chip(self._w._lbl_resolution, "分辨率 --")
        self._w._set_chip(self._w._lbl_status, "就绪", dot=True)
        self._w._lbl_roi.setText("未选择")
        self._w._set_ocr_state(False)
        self._w._set_result_text("")

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
                self._w._connection_status_label.hide()
                self._w._last_displayed_frame_id = buffer_frame_id
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
        self._w._set_chip(self._w._lbl_fps, f"FPS {fps:.0f}" if fps > 0 else "FPS --")

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
            self._w._set_chip(self._w._lbl_latency, f"延迟 {cur_lat / 1000:.2f}s", tone)
        else:
            self._w._set_chip(self._w._lbl_latency, "延迟 --")

        # Resolution
        if self._reader.connected:
            w, h = self._reader.width, self._reader.height
            self._w._set_chip(self._w._lbl_resolution, f"{w} × {h}")

        # Status
        if self._reader.connected:
            self._w._set_chip(self._w._lbl_status, "直播中", "ok", dot=True)
        elif self._reader.is_running:
            self._w._set_chip(self._w._lbl_status, "连接中", "warn", dot=True)
        else:
            self._w._set_chip(self._w._lbl_status, "已停止", "error", dot=True)

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
            self._w._set_perf("latency",
                           f"延迟 {cur_lat:.0f}ms" if cur_lat > 0 else "延迟 —")
            self._w._set_perf("first", f"首帧 {first_str}")
            self._w._set_perf("skip", f"跳帧 {skipped}")
            self._w._set_perf("pipe", f"管道 {pipe.get('last', 0) / 1024.0:.0f}KB")
            self._w._set_perf("decode_fps",
                           f"解码 {read_frames / runtime:.0f}fps" if runtime > 0
                           else "解码 —")
            self._w._set_perf("input_fps", f"输入 {snap['produced_fps']:.0f}fps")
            self._w._set_perf("set_frame",
                           f"set_frame {snap['set_frame_p50']:.1f}/"
                           f"{snap['set_frame_p95']:.1f}ms")
            self._w._set_perf("paint",
                           f"paint {snap['paint_p50']:.1f}/"
                           f"{snap['paint_p95']:.1f}ms")
            worker = self._w._ocr_worker
            if worker is not None:
                self._w._set_perf("ocr_frames", f"帧数 {worker.recognition_count}")
            self._w._set_perf("ocr_cost", "耗时 —")   # 当前没有 OCR 耗时测量源
        else:
            for key in ("latency", "first", "skip", "pipe", "decode_fps",
                        "input_fps", "set_frame", "paint", "ocr_frames"):
                self._w._perf_values[key].setText(self._w._perf_defaults.get(key, "—"))

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
        if not self._w._access_status.is_expired():
            return True

        self._w.statusBar().showMessage(
            f"{self._w._access_status.get_status_text()}，无法{action}"
        )
        logger.info("访问被拒绝: %s", action)
        return False

