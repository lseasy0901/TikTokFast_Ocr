# -*- coding: utf-8 -*-
"""
FFmpeg 低延迟读取模块 (Phase 1 - Step 3, 延迟优化)

流程：
    直播流URL -> [尺寸未知时在读取线程内探测] -> subprocess启动FFmpeg
              -> rawvideo(bgr24) -> stdout逐帧读取 -> numpy.ndarray

延迟优化要点：
    - 跳帧读取：每次 read 后用 PeekNamedPipe/select 检查管道中是否还有完整帧，
      有则读取并丢弃旧帧，始终只保留最新帧 → 消除管道积压延迟
    - FFmpeg 参数极致低延迟：nobuffer + flush_packets + low_delay + 最小探测
    - 独立子线程读取，绝不阻塞主线程
    - 单槽"最新帧"缓冲：读取跟不上时直接覆盖旧帧，宁可丢帧不增加延迟
    - np.frombuffer 零拷贝视图，不产生 Python 层像素复制

启动阻塞（Phase 3.3）：
    构造器不执行任何网络探测。尺寸要么由调用方给定，要么是 None —— 后者留到
    读取线程里用 ffprobe 解析，因此"尺寸未知"不会让 GUI 线程同步等待；
    探测进程句柄由 stop() 负责 kill，断开/切换候选时不会等满超时。

使用方法：
    reader = FFmpegReader(stream_url, width=1920, height=1080, use_cuda=True)
    reader.start()
    frame = reader.read_frame()   # numpy.ndarray (H, W, 3) BGR；无新帧返回 None
    reader.stop()
"""

import collections
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time

import numpy as np

from utils.runtime_paths import resolve_ffmpeg_path
from utils.startup_timing import (
    STAGE_FFMPEG_SPAWN,
    STAGE_FFPROBE_END,
    STAGE_FFPROBE_START,
    STAGE_FIRST_FRAME,
    StartupTiming,
)

logger = logging.getLogger("DouyinLowLatencyViewer.stream.ffmpeg_reader")

__all__ = ["FFmpegReader"]

#: 「本次尝试没有 ffprobe 阶段」的说明文案（Phase 3.2 分段计时用）
_PROBE_SKIP_REASON = "分辨率已知，无需探测"

#: 读取线程内尺寸探测的超时（秒）与探测失败时的回退尺寸。
#: 回退值保持与 Phase 3.3 之前一致：超时/失败不影响"这次尝试能不能起来"，
#: 只是按 1920x1080 切片；尺寸不对会在后续的帧长度校验里暴露出来。
_PROBE_TIMEOUT_S = 5
_FALLBACK_WIDTH = 1920
_FALLBACK_HEIGHT = 1080

# ---- 输入停滞看门狗（迁移审计第 1.5 步）----
# 实测（房间 263970540685，pull-q5 节点）：CDN 存在"TCP 存活但静默断供"的
# 停滞——21 秒零帧且无任何 IO 错误，IO 层 reconnect（只对可检出的断流生效）
# 覆盖不到。看门狗在帧计数冻结超过 _STALL_TIMEOUT_S 后击杀子进程，读取线程
# 以"同 URL 重启"响应（新连接即直播边缘），把源端断供造成的冻屏从 20s+ 压到
# 约 4-5s。连续 _STALL_MAX_RESTARTS 次仍无帧则按真断流上报（防死流死循环）。
_STALL_TIMEOUT_S = 4.0
_STALL_MAX_RESTARTS = 5

# ---- 音频第二路输出（AudioService，默认关闭） ----
# 传输通道选型说明（Windows 实测，2026-09）：
#   pipe:3 自定义句柄 —— 本项目自带的 ffmpeg 5.x 在 fd 3 未映射 CRT 时直接
#     fastfail 崩溃（0xC0000409），不可用；
#   Windows 命名管道 —— CreateNamedPipeW 在部分用户机器上被安全软件 hook 拦截，
#     不可依赖；
#   127.0.0.1 回环 TCP —— 零依赖、跨平台、延迟 <1ms，ffmpeg 作为客户端主动
#     连出，本层作为监听方。故选此通道。
#: 音频 PCM 输出参数：48kHz / 16bit / 双声道（固定，消费端 audio/ 按此解析）
_AUDIO_SAMPLE_RATE = 48000
_AUDIO_CHANNELS = 2
#: 音频监听 accept 轮询间隔（秒），同时保证 stop() 能及时让 accept 退出
_AUDIO_ACCEPT_TIMEOUT_S = 0.5

# ---- 管道数据可用性检查（跨平台） ----
_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    import ctypes
    import msvcrt

    _kernel32 = ctypes.windll.kernel32

    def _pipe_bytes_available(pipe_obj) -> int:
        """Windows: 用 PeekNamedPipe 检查管道中可读字节数（非阻塞）"""
        try:
            handle = msvcrt.get_osfhandle(pipe_obj.fileno())
            avail = ctypes.c_ulong(0)
            success = _kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(avail), None
            )
            return avail.value if success else 0
        except Exception:
            return 0
else:
    import select

    def _pipe_bytes_available(pipe_obj) -> int:
        """Unix: 用 select 检查管道是否可读（非阻塞）"""
        try:
            fd = pipe_obj.fileno()
            ready, _, _ = select.select([fd], [], [], 0)
            if ready:
                # 无法精确获取字节数，返回一个足够大的值表示有数据
                import fcntl
                return fcntl.ioctl(fd, fcntl.FIONREAD, 0)
            return 0
        except Exception:
            return 0


class FFmpegReader:
    """FFmpeg 低延迟视频帧读取器"""

    def __init__(self, stream_url, width=None, height=None, use_cuda=True,
                 http_headers=None, timing=None,
                 audio_enabled=False, audio_pcm_callback=None,
                 audio_sample_rate=None, audio_channels=None,
                 protocol=None):
        """
        构造**不做任何网络探测**：尺寸要么是调用方给的确定值，要么是显式
        None（= 尺寸未知，留到读取线程里解析，见 :meth:`_resolve_pending_size`）。

        :param stream_url: 直播流URL，例如 https://xxx.flv
        :param width:      输出帧宽度，必须与 FFmpeg 输出一致；
                           None = 未知（本层在读取线程内探测，不阻塞调用方线程）
        :param height:     输出帧高度，语义同 width
        :param use_cuda:   True 时启用 CUDA 硬件解码
        :param http_headers: 平台提供的媒体请求头（如 {"User-Agent": ...}）。
                          由平台适配器决定，本层不判断平台、不区分字段含义，
                          只负责把它注入到 FFmpeg 的**输入**选项里。
                          None / 空字典 = 沿用 FFmpeg 默认请求头（抖音即此路径）。
        :param timing:     Phase 3.2 启动分段计时对象。调用方传入时由调用方负责
                          输出汇总（它还要记录 GUI 侧的首帧时刻）；未传入时本层
                          自己持有，并在首帧到达后输出一条汇总，供命令行自测使用。
                          计时对象不改变任何播放行为。
        :param audio_enabled: True 时同一 FFmpeg 实例增加第二路输出：音频解码为
                          48kHz/16bit/双声道 PCM 写入 127.0.0.1 回环 TCP（本层
                          监听）。视频 stdout 的命令与字节流完全不变。
                          流无音频轨时：ffmpeg 在配置输出阶段即报错退出（不会
                          打开网络），本层据此**去掉音频输出重启一次**（纯进程
                          重启，无网络等待），并通过 audio_pcm_callback(None)
                          通知调用方"无音频轨"。
        :param audio_pcm_callback: 音频 PCM 回调，签名 callback(data)：
                          data 为非空 bytes = PCM 块；b"" = 音频流结束（EOF）；
                          None = 流无音频轨。只读语义，本层不关心消费方是谁，
                          异常由调用方自理（本层会在回调抛异常时记录并继续读）。
        """
        self.stream_url = stream_url
        self._http_headers = dict(http_headers) if http_headers else {}
        self._audio_enabled = bool(audio_enabled)
        self._audio_pcm_callback = audio_pcm_callback
        # 音频输出格式（None = 模块默认 48000/2）。音质对比测试 A/B/C 用。
        self._audio_sample_rate = int(audio_sample_rate or _AUDIO_SAMPLE_RATE)
        self._audio_channels = int(audio_channels or _AUDIO_CHANNELS)

        self._owns_timing = timing is None
        self._timing = timing if timing is not None else StartupTiming()

        # 尺寸是否留待读取线程解析（Phase 3.3）。默认 None 而不是 1920x1080：
        # "没给尺寸"必须是"未知"，绝不能悄悄当成 1080p —— 那会让切片逻辑按错误
        # 尺寸工作，而且是在没有任何探测依据的情况下。
        self._size_pending = (width is None or height is None)

        if self._size_pending:
            # 尺寸未知时不在这里探测：探测被推迟到 _reader_loop（读取线程）里，
            # 这样构造/启动不会阻塞调用方线程（GUI 线程）。
            self.width = None
            self.height = None
            self._frame_size = None      # 尺寸确定后计算（见 _resolve_pending_size）
        else:
            self.width = int(width)
            self.height = int(height)
            self._frame_size = self.width * self.height * 3
            # 尺寸已知的候选不跑 ffprobe：显式记下"本次尝试没有这个阶段"，
            # 让日志区分"跳过"与"没测到"
            self._timing.skip(STAGE_FFPROBE_START, _PROBE_SKIP_REASON)
            self._timing.skip(STAGE_FFPROBE_END, _PROBE_SKIP_REASON)
            self._timing.note(f"size={self.width}x{self.height}(已知)")

        self.use_cuda = bool(use_cuda)

        self._process = None
        # 尺寸探测进程（读取线程内运行）。stop() 会 kill 它，好让探测立即结束。
        self._probe_process = None
        self._thread = None
        self._running = False
        self._error = None

        # 单槽"最新帧"：只保留最近一帧，旧帧直接丢弃
        self._latest_frame = None
        self._frame_id = 0
        self._frame_lock = threading.Lock()

        # 外部可读状态
        self.connected = False      # 是否已成功收到首帧
        self.frames_read = 0        # 已读取帧总数

        # 延迟诊断
        self._first_frame_time = None   # 首帧到达时间（相对start时间）
        self._start_time = None         # start()调用时间
        self._frames_skipped = 0        # 跳帧总数

        # ---- 输入停滞看门狗状态 ----
        #: 最近一次完整帧入槽的时刻（monotonic 秒）；None = 尚未有帧
        self._last_frame_mono: float | None = None
        #: True = 看门狗已击杀当前子进程、等待读取线程处理重启
        self._stall_killed = False

        # ---- 协议 / 分辨率自报（迁移审计刀1/刀2）----
        #: 候选协议映射的强制 demuxer：仅 "flv" → live_flv（跳过格式探测 +
        #: flv_ignore_prevtag 容错跳读）；hls/rtmp/None → 自动探测
        self._input_format = "live_flv" if (protocol or "").lower() == "flv" else None
        #: 子进程 stderr 自报的分辨率 (w, h) 与就绪事件（_size_pending 时使用，
        #: 替代独立的 ffprobe 二次连流——迁移审计刀1）
        self._stderr_size: tuple[int, int] | None = None
        self._stderr_size_event = threading.Event()

        # FFmpeg stderr 尾部日志（最多保留20行，用于诊断连接失败）
        self._stderr_tail = collections.deque(maxlen=20)

        # 管道积压观测（Phase B-1）：每读完一帧做一次非阻塞 Peek，
        # 只把字节数存进环形窗口，不改变 stdout 的读取方式。
        self._pipe_samples = collections.deque(maxlen=300)
        self._pipe_bytes_last = 0
        self._pipe_bytes_max = 0

        # ---- 音频第二路输出状态 ----
        self._audio_server: socket.socket | None = None   # 回环监听 socket
        self._audio_conn = None                            # ffmpeg 连入后的会话
        self._audio_port = 0                               # 监听端口（0=未就绪）
        self._audio_accepted = False                       # ffmpeg 是否已连入
        self._audio_thread = None                          # 音频读取线程
        self._audio_thread_started = False

    def _resolve_pending_size(self) -> bool:
        """尺寸未知时，在**读取线程内**解析它（构造器不再探测）。

        这是 Phase 3.3 的核心：探测从前移到这里的唯一变化是"谁在等"。
        探测期间 GUI 线程依然在跑事件循环，界面不会卡住；探测耗时照旧由
        Phase 3.2 的 StartupTiming 分段记录（ffprobe_start → ffprobe_end）。

        :return: True = 尺寸已确定，可以启动 FFmpeg；
                 False = 期间被 :meth:`stop` 取消，调用方必须放弃启动
        """
        if not self._size_pending:
            return True
        if not self._running:
            # stop() 在探测开始前就落下了：一个进程都不该再起
            return False

        logger.info("[FFMPEG] 未提供分辨率，在读取线程内探测...")
        self._timing.mark(STAGE_FFPROBE_START)
        try:
            width, height = self._probe_size()
        finally:
            # 无论成功、失败还是被 kill，探测都到此结束（finally 保证不留空洞）
            self._timing.mark(STAGE_FFPROBE_END)

        if not self._running:
            # 探测期间被 stop()：绝不能再启动 FFmpeg（否则会留下无人回收的进程）
            logger.info("[FFMPEG] 尺寸探测期间已被停止，放弃启动")
            return False

        if width and height:
            self._timing.note(f"size={width}x{height}(探测)")
        else:
            # 与 Phase 3.3 之前完全一致的回退：探测失败沿用 1920x1080 并告警，
            # 由后续的"帧数据长度不符"路径去暴露尺寸不对。
            width, height = _FALLBACK_WIDTH, _FALLBACK_HEIGHT
            logger.warning("[FFMPEG] 分辨率探测失败，使用: %dx%d", width, height)
            self._timing.note(f"size={width}x{height}(探测失败→默认)")

        self.width = int(width)
        self.height = int(height)
        self._frame_size = self.width * self.height * 3
        self._size_pending = False
        # 探测耗时不属于"FFmpeg 启动到首帧"：把计时起点挪到探测结束，
        # 让 first_frame_ms（状态栏"首帧"，也是历史 benchmark 的口径）保持原义。
        # 探测本身由 Phase 3.2 的 StartupTiming 单独记录。
        self._start_time = time.perf_counter()
        return True

    def _probe_size(self):
        """用 ffprobe 探测流尺寸；返回 (width, height)，失败返回 (None, None)。

        用 Popen 而不是 subprocess.run，是为了让 :meth:`stop` 能 kill 掉这个
        进程：读取线程会立刻从 communicate() 返回，join 不会把断开/切换候选的
        调用方（GUI 线程）拖满超时。
        """
        proc = None
        try:
            from .video_info import build_probe_cmd, parse_probe_output

            creationflags = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0
            proc = subprocess.Popen(
                build_probe_cmd(self.stream_url),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._probe_process = proc
            if not self._running:
                # stop() 可能刚好在这两行之间跑过、没看到这个句柄：自己补一刀，
                # 否则下面的 communicate() 会把读取线程（进而 join）拖满超时
                proc.kill()
            stdout, _ = proc.communicate(timeout=_PROBE_TIMEOUT_S)

            if proc.returncode != 0:
                if self._running:   # 被 stop() kill 掉的不算探测失败，不必告警
                    logger.warning("[FFMPEG] 分辨率探测返回错误 (code=%d)", proc.returncode)
                return (None, None)

            video_info = parse_probe_output(stdout.decode("utf-8", errors="replace"))
            if video_info.width and video_info.height:
                logger.info(
                    "[VIDEO INFO] width=%d, height=%d, pix_fmt=%s, fps=%s, codec=%s",
                    video_info.width,
                    video_info.height,
                    video_info.pix_fmt or "unknown",
                    f"{video_info.fps:.2f}" if video_info.fps else "unknown",
                    video_info.codec_name or "unknown",
                )
                return (video_info.width, video_info.height)

            logger.warning("[FFMPEG] 分辨率探测未返回有效尺寸")
            return (None, None)

        except subprocess.TimeoutExpired:
            logger.warning("[FFMPEG] 分辨率探测超时 (%.0fs)", _PROBE_TIMEOUT_S)
            return (None, None)
        except Exception as e:
            logger.warning("[FFMPEG] 自动分辨率检测失败: %s", e)
            return (None, None)
        finally:
            self._probe_process = None
            if proc is not None and proc.poll() is None:
                proc.kill()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def start(self):
        """启动 FFmpeg 读取线程（非阻塞，立即返回）"""
        if self._thread is not None and self._thread.is_alive():
            logger.info("FFmpegReader 已在运行，忽略重复 start()")
            return self

        # 重置状态
        self._running = True
        self._error = None
        self.connected = False
        self._latest_frame = None
        self._frame_id = 0
        self.frames_read = 0
        self._frames_skipped = 0
        self._start_time = time.perf_counter()
        self._first_frame_time = None
        self._last_frame_mono = None
        self._stall_killed = False
        self._stderr_size = None
        self._stderr_size_event.clear()

        # 音频监听 socket：bind/listen 在调用方线程（快，微秒级），ffmpeg 在
        # 转码开始前作为客户端连入。失败只禁用音频，绝不影响视频。
        if self._audio_enabled:
            try:
                self._audio_server = socket.socket()
                self._audio_server.bind(("127.0.0.1", 0))
                self._audio_server.listen(1)
                self._audio_server.settimeout(_AUDIO_ACCEPT_TIMEOUT_S)
                self._audio_port = self._audio_server.getsockname()[1]
            except Exception as e:  # noqa: BLE001 - 音频监听失败不影响视频
                logger.warning("[FFMPEG] 音频监听创建失败，本次禁用音频: %s", e)
                self._close_audio_server()
                self._audio_enabled = False

        logger.info(
            "视频尺寸: %s",
            "待读取线程探测" if self._size_pending else f"{self.width}x{self.height}",
        )
        logger.info("CUDA状态: %s", "启用" if self.use_cuda else "关闭")

        self._thread = threading.Thread(
            target=self._reader_loop,
            name="ffmpeg-reader",
            daemon=True,
        )
        self._thread.start()
        # 停滞看门狗：与读取线程平行的独立哨兵（daemon，stop() 后随 _running 退出）
        threading.Thread(
            target=self._stall_watchdog,
            name="ffmpeg-stall-watchdog",
            daemon=True,
        ).start()
        return self

    def read_frame(self):
        """
        返回最新一帧视频帧。

        :return: numpy.ndarray (H, W, 3) 格式 BGR；
                 尚未读到帧 / 已停止 / 出错时返回 None
        """
        with self._frame_lock:
            return self._latest_frame

    def stop(self):
        """停止 FFmpeg 并释放资源（可重复调用，线程安全）"""
        self._running = False

        # 正在探测尺寸的话，kill 掉 ffprobe：读取线程会立刻从 communicate()
        # 返回，下面的 join 不会把调用方（GUI 线程）拖满探测超时。
        probe = self._probe_process
        if probe is not None and probe.poll() is None:
            try:
                probe.kill()
            except Exception:  # noqa: BLE001 - 已在退出的进程不值得再报错
                pass

        proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

        # 音频通道：先关 socket（accept/recv 会立刻失败退出线程），再等线程
        self._close_audio_server()
        thread = self._audio_thread
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._audio_thread = None

        self._cleanup_process(proc)

        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

        with self._frame_lock:
            self._latest_frame = None

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------
    @property
    def is_running(self):
        """读取线程是否在运行"""
        return self._running

    @property
    def error(self):
        """最近一次错误信息；无错误为 None"""
        return self._error

    @property
    def frame_id(self):
        """最新帧序号（单调递增，可用于判断是否有新帧）"""
        with self._frame_lock:
            return self._frame_id

    @property
    def frame_shape(self):
        """输出帧形状 (H, W, 3)"""
        return (self.height, self.width, 3)

    @property
    def frames_skipped(self):
        """跳帧总数（延迟诊断）"""
        return self._frames_skipped

    @property
    def first_frame_ms(self):
        """首帧到达耗时（ms）；未收到首帧返回 None"""
        if self._first_frame_time is None or self._start_time is None:
            return None
        return (self._first_frame_time - self._start_time) * 1000.0

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _reader_loop(self):
        """子线程入口：启动FFmpeg -> stderr自报分辨率 -> 逐帧读取 -> 跳帧到最新 -> 覆盖单槽"""
        # 0. 尺寸解析已并入下方 spawn 之后（迁移审计刀1）：分辨率来自同一条
        #    连接的 stderr dump 行，不再有独立的 ffprobe 二次连流；解析失败的
        #    回退路径仍是可中断的 ffprobe（见 _wait_size_from_stderr）。

        # 1+2. 启动 FFmpeg 并确认连接（含"无音频轨重启一次"重试）
        # 音频输出用非可选 -map 0:a：流无音频轨时 ffmpeg 在配置输出阶段（尚未
        # 打开网络）立即退出、stdout EOF、音频端口从未被连入 —— 这三个事实
        # 合起来就是确定性的"无音频轨"判据，此时去掉音频输出重启一次即可。
        proc = None
        frame_bytes = None
        try:
            for _ in range(2):
                proc = self._spawn_ffmpeg() if proc is None else self._spawn_ffmpeg(False)

                self._process = proc
                # 独立线程排空 stderr，防止管道写满阻塞FFmpeg，同时保留错误日志尾部
                threading.Thread(
                    target=self._drain_stderr,
                    args=(proc,),
                    name="ffmpeg-stderr",
                    daemon=True,
                ).start()

                # 分辨率自报（刀1）：dump 行在 open 阶段即出现、先于首帧，来自
                # 同一条连接；失败回退到可中断的 ffprobe（旧行为，stop() 会 kill）
                if self._size_pending:
                    if not self._wait_size_from_stderr(proc):
                        if not self._running:
                            self._cleanup_process(proc)
                            return
                        try:
                            if not self._resolve_pending_size():
                                # 期间被 stop()：不留下任何待清理的资源
                                self._cleanup_process(proc)
                                self._running = False
                                return
                        except Exception as e:  # noqa: BLE001 - 回退探测异常必须转 reader 错误
                            self._set_error(f"尺寸探测异常: {e}")
                            self._running = False
                            self._cleanup_process(proc)
                            return

                if self._audio_enabled and not self._audio_thread_started:
                    self._start_audio_thread()

                # 阻塞读取第一帧，确认连接是否成功
                try:
                    frame_bytes = proc.stdout.read(self._frame_size)
                except Exception as e:
                    if self._running:
                        self._set_error(f"读取首帧异常: {e}")
                    self._cleanup_process(proc)
                    self._running = False
                    return

                if not self._running:
                    self._cleanup_process(proc)
                    return

                if frame_bytes:
                    break  # 正常拿到首帧，进入持续读取

                # stdout EOF：进程已退出。若音频输出还没被连入过，存在两种
                # 可能，必须用 stderr 证据区分（2026-10-01，"声音没了"修复）：
                #   a) 真无音频轨：ffmpeg 在配置输出阶段报
                #      "Stream map '0:a' matches no streams" → 去掉音频重启；
                #   b) 瞬时连接失败（CDN 抖动/首次 403 等）：此时去掉音频重启
                #      会造成"视频正常但声音悄悄消失"——必须带音频原样重启。
                # 注意 stdout 管道关闭与进程对象 signaled 之间有竞态，必须用
                # 带超时的 wait() 而不是 poll()。
                if self._audio_enabled and not self._audio_accepted:
                    try:
                        proc.wait(timeout=1.5)
                        exited = True
                    except subprocess.TimeoutExpired:
                        exited = False
                    if exited:
                        no_audio_track = any(
                            "matches no streams" in line
                            for line in self._stderr_tail
                        )
                        if no_audio_track:
                            logger.info("[FFMPEG] stderr 证实流无音频轨"
                                        "（matches no streams），去掉音频输出重启")
                            self._audio_enabled = False
                            self._close_audio_server()
                            self._notify_audio(None)   # None = 无音频轨
                        else:
                            logger.warning(
                                "[FFMPEG] 首次启动失败但 stderr 无'无音频轨'证据"
                                "（瞬时失败），保留音频输出原样重启")
                        self._cleanup_process(proc)
                        continue

                # 与改动前一致的失败路径
                self._cleanup_process(proc)
                self._running = False
                self._set_error(
                    "连接失败：FFmpeg未返回任何数据（请检查直播流地址/网络，见下方FFmpeg输出）"
                )
                return

            if not frame_bytes or len(frame_bytes) < self._frame_size:
                self._cleanup_process(proc)
                self._running = False
                self._set_error(
                    "数据长度错误：首帧数据不完整（实际视频尺寸与配置 width/height 不符？）"
                )
                return
        except FileNotFoundError:
            self._set_error("[FFMPEG] ffmpeg not found: please install FFmpeg and add to PATH")
            self._running = False
            return
        except Exception as e:  # noqa: BLE001 - 启动路径异常必须转为 reader 错误
            self._set_error(f"启动FFmpeg失败: {e}")
            self._running = False
            return

        # 首帧成功 -> 连接状态确认
        self.connected = True
        self._first_frame_time = time.perf_counter()
        self._timing.mark(STAGE_FIRST_FRAME)
        first_frame_ms = (self._first_frame_time - self._start_time) * 1000.0
        logger.info(
            "[FFMPEG] first frame received! connected=True, first_frame=%.0fms",
            first_frame_ms,
        )
        self._store_frame(frame_bytes)
        if self._owns_timing:
            # 计时对象由本层自己持有（命令行自测 / bench 直连）：首帧就是终点。
            # 调用方传入计时对象时不由本层输出 —— 它还要记录 GUI 侧的首帧时刻。
            self._timing.log()

        # 3. 持续逐帧读取（跳帧到最新；带停滞看门狗的同 URL 自动重启）
        # 外层 while 的每一次迭代 = 一个子进程生命周期；看门狗击杀后在原地
        # 重新 spawn（新连接即直播边缘），GUI 与帧缓冲完全无感。
        error = None
        stall_restarts = 0
        while self._running:
            try:
                while self._running:
                    frame_bytes = proc.stdout.read(self._frame_size)
                    if not frame_bytes:
                        if self._stall_killed:
                            # 看门狗击杀（源端静默断供）：走下方重启路径
                            break
                        if not self._running:
                            break
                        error = "视频断流：FFmpeg已停止输出（直播结束或网络中断）"
                        break
                    if len(frame_bytes) < self._frame_size:
                        if self._stall_killed:
                            # 击杀时管道残量不足一帧：半帧属于旧流，丢弃后走重启
                            break
                        if not self._running:
                            break
                        error = (
                            "数据长度错误：帧数据不完整"
                            "（实际视频尺寸与配置 width/height 不符，或视频断流）"
                        )
                        break

                    # Phase B-1：记录"刚读到的这帧之后、管道里还排着多少字节"。
                    # 必须在跳帧之前采样，否则测得的是排空后的残量，反映不出积压。
                    # 这是一次非阻塞 Peek，不读取、不改变读取逻辑。
                    self._sample_pipe_backlog(proc.stdout)

                    # ★ 延迟优化核心：跳帧读取
                    # 检查管道中是否还有完整帧可读，有则丢弃当前帧、读取更新的帧
                    # 这样确保我们始终持有管道中最新的帧，而非积压的旧帧
                    skipped = self._skip_to_latest(proc.stdout, frame_bytes)
                    if skipped is not None:
                        # 有更新的帧，用最新的
                        frame_bytes = skipped

                    self._store_frame(frame_bytes)
                    stall_restarts = 0   # 有帧产出：停滞重启预算重置
            except Exception as e:
                # 击杀瞬间的读异常（管道断裂等）同样属于停滞路径，不置错误
                if self._running and not self._stall_killed:
                    error = f"读取异常: {e}（FFmpeg进程已退出）"

            # ---- 阶段结算：真错误/用户停止 → 收尾；看门狗击杀 → 重启 ----
            if error is not None or not self._running:
                break

            if not self._stall_killed:
                break   # 无停滞标记的 EOF：直播正常结束（error 已在上面置位）

            self._stall_killed = False
            stall_restarts += 1
            if stall_restarts > _STALL_MAX_RESTARTS:
                error = (f"输入停滞：连续 {stall_restarts - 1} 次自动重启后仍无数据"
                         "（源端或网络持续不可用）")
                break
            logger.warning(
                "[FFMPEG] 输入停滞，重启子进程回到直播边缘（第 %d/%d 次）",
                stall_restarts, _STALL_MAX_RESTARTS,
            )
            self._cleanup_process(proc)
            proc = None
            if self._audio_enabled:
                # 旧音频线程随连接断开已退出：重新武装，等待新子进程连入同一端口
                self._audio_accepted = False
                self._audio_thread_started = False
            try:
                proc = self._spawn_ffmpeg()
            except Exception as e:
                error = f"停滞重启失败: {e}"
                break
            self._process = proc
            threading.Thread(target=self._drain_stderr, args=(proc,),
                             name="ffmpeg-stderr", daemon=True).start()
            if self._audio_enabled and not self._audio_thread_started:
                self._start_audio_thread()
            # 回到读帧循环：下一次 read 自然拿到重启后的首帧

        # 4. 收尾
        self.connected = False
        self._running = False
        self._cleanup_process(proc)
        with self._frame_lock:
            self._latest_frame = None

        if error:
            self._set_error(error)

    def _skip_to_latest(self, pipe, current_frame: bytes):
        """
        延迟优化：检查管道中是否有更新的完整帧，有则跳过当前帧读取最新的。

        使用 PeekNamedPipe(Windows)/select(Unix) 非阻塞检查管道可读字节数。
        如果可读字节数 >= 一帧大小，说明 FFmpeg 已输出更新的帧到管道，
        读取并丢弃当前帧，返回最新的帧。

        :param pipe:           FFmpeg 的 stdout 管道
        :param current_frame:  当前已读到的帧（可能过时）
        :return: 最新的帧 bytes；如果管道中没有更新帧则返回 None（使用 current_frame）
        """
        latest = None
        total_skipped = 0

        while self._running:
            avail = _pipe_bytes_available(pipe)
            if avail < self._frame_size:
                # 管道中不足一帧 → 当前帧就是最新的
                break

            # 管道中有至少一帧更新数据 → 读取它
            try:
                next_frame = pipe.read(self._frame_size)
            except Exception:
                break

            if not next_frame or len(next_frame) < self._frame_size:
                # 不完整帧 → 保留之前读到的帧
                break

            # 丢弃旧帧，保留新帧
            latest = next_frame
            total_skipped += 1

            # 安全阀：单次最多跳30帧（避免极端情况下无限循环）
            if total_skipped >= 30:
                break

        if total_skipped > 0:
            self._frames_skipped += total_skipped
            if total_skipped >= 5 or self._frames_skipped % 100 < total_skipped:
                logger.debug(
                    "[Latency] skipped %d frames (total_skipped=%d) -> pipeline catching up",
                    total_skipped, self._frames_skipped,
                )

        return latest

    # ------------------------------------------------------------------
    # Phase B-1：管道积压观测（纯只读，不影响读取逻辑）
    # ------------------------------------------------------------------
    def _sample_pipe_backlog(self, pipe):
        """记录一次管道可读字节数（复用已有的非阻塞 Peek，不新增阻塞 read）"""
        try:
            avail = _pipe_bytes_available(pipe)
        except Exception:
            return
        if avail < 0:
            return
        self._pipe_bytes_last = avail
        if avail > self._pipe_bytes_max:
            self._pipe_bytes_max = avail
        self._pipe_samples.append(avail)

    def pipe_backlog_stats(self) -> dict:
        """管道积压样本的分位数汇总（由调用方在既有刷新周期中调用）

        :return: {"samples", "last", "max", "p50", "p95"}，单位为字节；
                 无样本时数值全为 0
        """
        samples = list(self._pipe_samples)
        n = len(samples)
        if n == 0:
            return {"samples": 0, "last": 0, "max": 0, "p50": 0, "p95": 0}
        ordered = sorted(samples)

        def _pct(p: float) -> int:
            return ordered[int((len(ordered) - 1) * p)]

        return {
            "samples": n,
            "last": samples[-1],
            "max": self._pipe_bytes_max,
            "p50": _pct(0.50),
            "p95": _pct(0.95),
        }

    @property
    def pipe_bytes_last(self) -> int:
        """最近一次采样到的管道可读字节数"""
        return self._pipe_bytes_last

    @property
    def pipe_bytes_max(self) -> int:
        """管道可读字节数的历史峰值"""
        return self._pipe_bytes_max

    def _store_frame(self, frame_bytes):
        """将一帧字节串转为 numpy 视图并写入单槽（覆盖旧帧 = 主动丢帧）"""
        # np.frombuffer + reshape 为零拷贝视图：不产生 Python 层像素复制
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            (self.height, self.width, 3)
        )
        with self._frame_lock:
            self._latest_frame = frame
            self._frame_id += 1
        self.frames_read += 1
        self._last_frame_mono = time.monotonic()

    def _stall_watchdog(self):
        """输入停滞看门狗（独立线程，只做一件事：发现停滞 → 击杀子进程）。

        连接建立后帧时间戳冻结超过 _STALL_TIMEOUT_S → 击杀当前子进程。读取
        线程阻塞中的 stdout.read() 会立即以 EOF 返回，据此走"同 URL 重启"
        路径（新连接即直播边缘）。这覆盖 IO 层 reconnect 触发不到的场景：
        TCP 存活但 CDN 静默断供/慢滴（实测 21s 零帧无任何 IO 错误）。
        一次只允许一个在途击杀（_stall_killed 标志），重启预算的计数与上限
        在读取线程的结算段处理。
        """
        while self._running:
            time.sleep(0.5)
            if not self._running or not self.connected:
                continue
            last = self._last_frame_mono
            if last is None or self._stall_killed:
                continue
            if time.monotonic() - last <= _STALL_TIMEOUT_S:
                continue
            proc = self._process
            if proc is None or proc.poll() is not None:
                continue
            self._stall_killed = True
            logger.warning(
                "[FFMPEG] 输入停滞超过 %ds（源端静默断供），击杀子进程以重启回直播边缘",
                int(_STALL_TIMEOUT_S),
            )
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - 已在退出的进程不值得再报错
                pass

    def _build_ffmpeg_cmd(self, with_audio: bool | None = None):
        """构造 FFmpeg 极低延迟参数（CUDA 按需追加）

        :param with_audio: 是否追加音频第二路输出；None = 跟随 self._audio_enabled
        """
        if with_audio is None:
            with_audio = self._audio_enabled
        # loglevel info + -nostats：stderr 里会出现一次性的 "Stream #0:0 ...
        # 1280x720" dump 行，读取线程据此自报分辨率（替代独立的 ffprobe 二次
        # 连流）；-nostats 压掉周期性进度行，保 stderr_tail 可用于错误诊断。
        cmd = [resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-nostats"]

        # ---- 输入侧：极致低延迟参数 ----
        # 注意：-fflags 只能出现一次。ffmpeg CLI 把 per-file 选项存进
        # AVDictionary（av_dict_set 默认替换语义），重复的 -fflags 是后者覆盖
        # 前者——历史上分写的两次导致 nobuffer/flush_packets 被 +genpts 丢掉
        #（迁移审计 P0-1 修复）。
        cmd += ["-fflags", "nobuffer+flush_packets+genpts"]  # 关输入缓冲+立即刷包+缺PTS补全
        cmd += ["-flags", "low_delay"]                 # 低延迟解码
        cmd += ["-flags2", "fast"]                     # 快速解码（跳过不重要帧）
        cmd += ["-probesize", "32768"]                 # 32KB探测：快且足够检测FLV+h264
        cmd += ["-analyzeduration", "100000"]          # 0.1s分析：足够检测参数，快速启动
        cmd += ["-thread_queue_size", "1"]             # 输入线程队列只留1包

        # ---- 断流自愈（仅 http(s) 输入；迁移审计 P0-2，参数取自 live-player 逆向报告）----
        # 这四项是 http 协议私有选项，而 ffmpeg CLI 对"该输入不认识的选项"
        # 会直接报错退出（lavfi/file 实测复现 "Option reconnect not found."），
        # 因此必须按 URL 协议条件注入。
        # rw_timeout：网络读/写超时，卡死的连接 15s 内必然报错退出，而不是让
        #   stdout.read() 永久阻塞、界面无休止停在"连接中"。
        # reconnect 三项：http(s) 层自动重拉同一 URL（FLV 重挂即回直播边缘），
        #   把秒级网络抖动的恢复成本从"子进程退出→整条重连"降为 IO 层 1~5s 自愈。
        if str(self.stream_url).lower().startswith(("http://", "https://")):
            cmd += ["-rw_timeout", "15000000"]
            cmd += ["-reconnect", "1"]
            cmd += ["-reconnect_streamed", "1"]
            cmd += ["-reconnect_delay_max", "5"]

        # ---- CUDA 硬件解码 ----
        if self.use_cuda:
            cmd += ["-hwaccel", "cuda"]  # GPU解码，帧自动回传CPU（rawvideo需要CPU内存）

        # ---- 强制 demuxer（迁移审计刀2，live-player 实证参数）----
        # flv 候选跳过格式探测（省探测开销且不依赖探测收敛），
        # flv_ignore_prevtag 容错跳读坏 tag；hls/rtmp/未知协议不注入。
        if self._input_format == "live_flv":
            cmd += ["-f", "live_flv", "-flv_ignore_prevtag", "1"]

        # ---- 平台请求上下文（Phase 2.4）----
        # 必须放在 -i **之前**：这些都是 input option，放到 -i 之后会被当成
        # 输出选项，FFmpeg 要么报错要么静默忽略。
        # 本层不判断平台：headers 为空时命令与改动前逐字相同（抖音路径）。
        if self._http_headers:
            user_agent = None
            extra_headers = []
            for key, value in self._http_headers.items():
                # User-Agent 走 FFmpeg 原生选项，其余走 -headers 机制
                if key.lower() == "user-agent":
                    user_agent = value
                else:
                    extra_headers.append((key, value))
            if user_agent:
                cmd += ["-user_agent", user_agent]
            if extra_headers:
                blob = "".join(f"{k}: {v}\r\n" for k, v in extra_headers)
                cmd += ["-headers", blob]

        cmd += ["-i", self.stream_url]

        # ---- 输出侧：仅视频 rawvideo ----
        cmd += ["-an", "-sn"]                     # 丢弃音频/字幕，减少处理
        cmd += ["-avioflags", "direct"]           # 直写，跳过AVIO内部缓冲
        cmd += ["-max_delay", "0"]                # 关闭复用器FIFO延迟
        cmd += ["-flush_packets", "1"]            # 立即刷出输出包
        cmd += ["-pix_fmt", "bgr24"]              # 输出 BGR 24bit（与numpy匹配）
        cmd += ["-f", "rawvideo", "pipe:1"]       # 原始视频写 stdout

        # ---- 音频第二路输出（同一实例，stdout 选项不受影响）----
        # -map 0:a 刻意不用 0:a?（可选映射）：流无音频轨时"可选映射"会让该输出
        # 零流而使整个 ffmpeg 报错退出（连累视频）；非可选映射则是在**配置输出
        # 阶段**（尚未打开网络）就报 "matches no streams" 退出，读取线程据此
        # 去掉音频输出重启一次。回环 TCP 的连接发生在输入打开之后，因此
        # "进程秒退 + 从未连入音频端口" 是可靠的"无音频轨"判据。
        if with_audio and self._audio_port:
            cmd += ["-map", "0:a", "-vn", "-sn",
                    "-f", "s16le",
                    "-ar", str(self._audio_sample_rate),
                    "-ac", str(self._audio_channels),
                    f"tcp://127.0.0.1:{self._audio_port}"]

        return cmd

    def _spawn_ffmpeg(self, with_audio: bool | None = None):
        """启动 FFmpeg 子进程（ffmpeg 不存在时抛出 FileNotFoundError）"""
        cmd = self._build_ffmpeg_cmd(with_audio)
        logger.info("FFmpeg启动参数: %s", " ".join(cmd))

        # Windows 下不弹出黑色控制台窗口（后续由 GUI 调用时需要）
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        # Popen 返回 = 进程已创建。此刻之后 FFmpeg 自己的连接/探测/首个关键帧
        # 等待都算在 "ffmpeg_spawn → first_decoded_frame" 这一段里。
        self._timing.mark(STAGE_FFMPEG_SPAWN)
        return proc

    #: ffmpeg 对输入的 dump 行：`Stream #0:0...: Video: h264 ..., 1280x720, ...`
    _STREAM_SIZE_RE = re.compile(r"Stream #\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})[,\s]")

    def _drain_stderr(self, proc):
        """排空 FFmpeg stderr，保留最近若干行用于诊断；
        尺寸未知时同步从 dump 行自报分辨率（迁移审计刀1）。"""
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if (self._size_pending
                            and self._stderr_size is None
                            and "Video:" in text):
                        m = self._STREAM_SIZE_RE.search(text)
                        if m:
                            self._stderr_size = (int(m.group(1)), int(m.group(2)))
                            self._stderr_size_event.set()
        except Exception:
            pass

    def _wait_size_from_stderr(self, proc, timeout_s: float = 10.0) -> bool:
        """等待子进程 stderr 自报分辨率并落到 self.width/height。

        这是刀1 的核心：分辨率来自**同一条流连接**的 dump 行（open 阶段就有，
        先于首帧），替代独立的 ffprobe 二次连流。

        等待是 stop 感知与子进程死亡感知的：
            - stop() 置 _running=False → 立即返回 False（不拖住 join）；
            - 子进程已退出（坏 URL 等）→ dump 永远不会来 → 立即回退，
              不把 failover 拖满超时。

        :return: True = 尺寸已就绪；False = 超时/停止/子进程已死（调用方回退）
        """
        if not self._size_pending:
            return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._running:
                return False
            if self._stderr_size_event.wait(0.2):
                break
            if proc is not None and proc.poll() is not None:
                break
        if self._stderr_size_event.is_set() and self._stderr_size:
            w, h = self._stderr_size
            self.width = int(w)
            self.height = int(h)
            self._frame_size = self.width * self.height * 3
            self._size_pending = False
            self._timing.note(f"size={self.width}x{self.height}(stderr)")
            # 探测耗时不属于"FFmpeg 启动到首帧"：把计时起点挪到此刻，
            # 让 first_frame_ms 保持原义（与 _resolve_pending_size 的处理一致）。
            self._start_time = time.perf_counter()
            return True
        if self._running:
            logger.warning(
                "[FFMPEG] %.0fs 内未从 stderr 解析到分辨率（子进程存活=%s），"
                "回退 ffprobe 探测",
                timeout_s,
                proc is not None and proc.poll() is None,
            )
        return False

    # ------------------------------------------------------------------
    # 音频第二路输出（回环 TCP）：监听 -> ffmpeg 连入 -> 逐块回调
    # ------------------------------------------------------------------
    def _start_audio_thread(self):
        """启动音频读取线程（仅读取线程内调用一次）"""
        self._audio_thread_started = True
        self._audio_thread = threading.Thread(
            target=self._audio_loop,
            name="ffmpeg-audio",
            daemon=True,
        )
        self._audio_thread.start()

    def _audio_loop(self):
        """音频线程入口：accept ffmpeg 的连接，把 PCM 逐块交给回调。

        消费端停滞时 TCP 发送缓冲会反压到 ffmpeg 的音频输出，进而可能拖住
        整个转码进程 —— 所以本线程绝不缓冲，读到就交，把背压控制交给
        消费方（AudioService 用不阻塞的队列 + 丢块策略）。
        """
        conn = None
        try:
            # 等待 ffmpeg 连入（配置失败时永远不会来，随 stop() 退出）
            while self._running:
                try:
                    conn, _ = self._audio_server.accept()
                    break
                except socket.timeout:
                    continue
                except OSError:
                    return
            if not self._running:
                return
            self._audio_conn = conn
            self._audio_accepted = True
            conn.settimeout(None)
            logger.info("[FFMPEG] 音频通道已连入 (127.0.0.1:%d)", self._audio_port)

            while self._running:
                try:
                    chunk = conn.recv(8192)
                except (OSError, ConnectionResetError):
                    chunk = b""
                if not chunk:
                    break
                self._notify_audio(chunk)
        finally:
            self._audio_accepted = False
            conn = self._audio_conn
            self._audio_conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._running:
                # ffmpeg 侧断开（正常退出 / 崩溃）：通知消费方流已结束
                self._notify_audio(b"")

    def _notify_audio(self, data):
        """把音频数据交给回调；回调异常绝不外溢（不得影响视频读取）"""
        cb = self._audio_pcm_callback
        if cb is None:
            return
        try:
            cb(data)
        except Exception:  # noqa: BLE001 - 消费方异常与视频链路隔离
            logger.warning("[FFMPEG] 音频回调异常（已忽略）", exc_info=True)

    def _close_audio_server(self):
        """关闭音频监听 socket（幂等）"""
        server = self._audio_server
        self._audio_server = None
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass

    def _set_error(self, message):
        """记录错误并输出 FFmpeg 诊断日志"""
        self._error = message
        logger.error(message)
        if self._stderr_tail:
            logger.error("FFmpeg输出:\n%s", "\n".join(self._stderr_tail))

    def _cleanup_process(self, proc=None):
        """确保子进程及其管道被释放"""
        proc = proc or self._process
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass
        self._process = None


if __name__ == "__main__":
    # ==================================================================
    # 命令行自测入口：python stream/ffmpeg_reader.py [stream_url]
    # 持续输出 FPS / 帧尺寸 / 读取状态，Ctrl+C 停止
    # ==================================================================
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="FFmpegReader 低延迟读取自测")
    parser.add_argument(
        "stream_url", nargs="?",
        help="直播流URL（如 https://xxx.flv），不传则手动输入",
    )
    parser.add_argument("--width", type=int, default=1920, help="输出宽度（默认1920）")
    parser.add_argument("--height", type=int, default=1080, help="输出高度（默认1080）")
    parser.add_argument("--no-cuda", action="store_true", help="禁用CUDA硬件解码")
    args = parser.parse_args()

    # 从 config.yaml 读取 ffmpeg.cuda 配置（显式 --no-cuda 时以命令行优先）
    use_cuda = not args.no_cuda
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"
    )
    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg_cuda = (cfg.get("ffmpeg") or {}).get("cuda")
        if cfg_cuda is not None and not args.no_cuda:
            use_cuda = bool(cfg_cuda)
    except Exception:
        pass  # 配置读取失败时采用命令行默认值

    stream_url = args.stream_url
    if not stream_url:
        stream_url = input("请输入直播流URL（例如 https://xxx.flv）: ").strip()
    if not stream_url:
        print("[退出] 未提供直播流URL")
        sys.exit(1)

    reader = FFmpegReader(
        stream_url,
        width=args.width,
        height=args.height,
        use_cuda=use_cuda,
    )
    print(f"[测试] URL={stream_url}")
    print(f"[测试] 目标尺寸={args.width}x{args.height}  CUDA={use_cuda}")
    reader.start()

    consumed = 0
    last_report = time.perf_counter()
    last_consumed = 0
    try:
        while True:
            frame = reader.read_frame()
            if frame is not None:
                consumed += 1
                now = time.perf_counter()
                if now - last_report >= 1.0:
                    fps = (consumed - last_consumed) / (now - last_report)
                    print(
                        f"[状态] 消费FPS={fps:6.2f} 累计帧={consumed} "
                        f"帧ID={reader.frame_id} 尺寸={frame.shape[1]}x{frame.shape[0]} "
                        f"类型={frame.dtype} 连接={reader.connected} 运行={reader.is_running} "
                        f"跳帧={reader.frames_skipped}"
                    )
                    last_report = now
                    last_consumed = consumed
            elif reader.error:
                print(f"[失败] {reader.error}")
                break
            else:
                time.sleep(0.002)  # 尚未收到首帧，等待连接
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C，正在停止...")
    finally:
        reader.stop()
        print("[完成] FFmpegReader 已停止")
