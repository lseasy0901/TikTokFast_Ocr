# -*- coding: utf-8 -*-
"""
FFmpeg 低延迟读取模块 (Phase 1 - Step 3, 延迟优化)

流程：
    直播流URL -> subprocess启动FFmpeg -> rawvideo(bgr24) -> stdout逐帧读取 -> numpy.ndarray

延迟优化要点：
    - 跳帧读取：每次 read 后用 PeekNamedPipe/select 检查管道中是否还有完整帧，
      有则读取并丢弃旧帧，始终只保留最新帧 → 消除管道积压延迟
    - FFmpeg 参数极致低延迟：nobuffer + flush_packets + low_delay + 最小探测
    - 独立子线程读取，绝不阻塞主线程
    - 单槽"最新帧"缓冲：读取跟不上时直接覆盖旧帧，宁可丢帧不增加延迟
    - np.frombuffer 零拷贝视图，不产生 Python 层像素复制

使用方法：
    reader = FFmpegReader(stream_url, width=1920, height=1080, use_cuda=True)
    reader.start()
    frame = reader.read_frame()   # numpy.ndarray (H, W, 3) BGR；无新帧返回 None
    reader.stop()
"""

import collections
import logging
import os
import subprocess
import sys
import threading
import time

import numpy as np

from utils.runtime_paths import resolve_ffmpeg_path

logger = logging.getLogger("DouyinLowLatencyViewer.stream.ffmpeg_reader")

__all__ = ["FFmpegReader"]

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

    def __init__(self, stream_url, width=1920, height=1080, use_cuda=True):
        """
        :param stream_url: 直播流URL，例如 https://xxx.flv
        :param width:      输出帧宽度（必须与FFmpeg输出一致，默认1920）
                          注意：如果不提供，将尝试自动检测流媒体实际分辨率
        :param height:     输出帧高度（默认1080）
        :param use_cuda:   True 时启用 CUDA 硬件解码
        """
        self.stream_url = stream_url

        # 分辨率检测标志（是否使用了自动检测）
        self._auto_detected = (width is None or height is None)

        # 如果没有提供分辨率，先尝试检测
        if self._auto_detected:
            logger.info("[FFMPEG] 未提供完整分辨率，尝试自动检测...")
            detected_width, detected_height = self._detect_stream_resolution()
            if detected_width and detected_height:
                width = width if width is not None else detected_width
                height = height if height is not None else detected_height
                logger.info("[FFMPEG] 使用检测到的分辨率: %dx%d", width, height)
            else:
                # 检测失败，使用默认值或提供的部分值
                width = width if width is not None else 1920
                height = height if height is not None else 1080
                logger.warning("[FFMPEG] 分辨率检测失败，使用: %dx%d", width, height)

        self.width = int(width)
        self.height = int(height)
        self.use_cuda = bool(use_cuda)

        # 一帧 BGR24 的字节数
        self._frame_size = self.width * self.height * 3

        self._process = None
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

        # FFmpeg stderr 尾部日志（最多保留20行，用于诊断连接失败）
        self._stderr_tail = collections.deque(maxlen=20)

    def _detect_stream_resolution(self):
        """
        自动检测流媒体的分辨率

        返回：
            (width, height) 元组，检测失败返回 (None, None)
        """
        try:
            from .video_info import get_video_info

            video_info = get_video_info(self.stream_url, timeout=5)
            if video_info.width and video_info.height:
                logger.info(
                    "[VIDEO INFO] width=%d, height=%d, pix_fmt=%s, fps=%s, codec=%s",
                    video_info.width,
                    video_info.height,
                    video_info.pix_fmt or "unknown",
                    f"{video_info.fps:.2f}" if video_info.fps else "unknown",
                    video_info.codec_name or "unknown"
                )
                return (video_info.width, video_info.height)
            else:
                return (None, None)

        except Exception as e:
            logger.warning("[FFMPEG] 自动分辨率检测失败: %s", e)
            return (None, None)

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

        logger.info("视频尺寸: %dx%d", self.width, self.height)
        logger.info("CUDA状态: %s", "启用" if self.use_cuda else "关闭")

        self._thread = threading.Thread(
            target=self._reader_loop,
            name="ffmpeg-reader",
            daemon=True,
        )
        self._thread.start()
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

        proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

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
        """子线程入口：启动FFmpeg -> 逐帧读取 -> 跳帧到最新 -> 覆盖单槽"""
        # 1. 启动 FFmpeg 进程
        try:
            proc = self._spawn_ffmpeg()
        except FileNotFoundError:
            self._set_error("[FFMPEG] ffmpeg not found: please install FFmpeg and add to PATH")
            self._running = False
            return
        except Exception as e:
            self._set_error(f"启动FFmpeg失败: {e}")
            self._running = False
            return

        self._process = proc
        # 独立线程排空 stderr，防止管道写满阻塞FFmpeg，同时保留错误日志尾部
        threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name="ffmpeg-stderr",
            daemon=True,
        ).start()

        # 2. 阻塞读取第一帧，确认连接是否成功
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

        if not frame_bytes:
            self._cleanup_process(proc)
            self._running = False
            self._set_error(
                "连接失败：FFmpeg未返回任何数据（请检查直播流地址/网络，见下方FFmpeg输出）"
            )
            return
        if len(frame_bytes) < self._frame_size:
            self._cleanup_process(proc)
            self._running = False
            self._set_error(
                "数据长度错误：首帧数据不完整（实际视频尺寸与配置 width/height 不符？）"
            )
            return

        # 首帧成功 -> 连接状态确认
        self.connected = True
        self._first_frame_time = time.perf_counter()
        first_frame_ms = (self._first_frame_time - self._start_time) * 1000.0
        logger.info(
            "[FFMPEG] first frame received! connected=True, first_frame=%.0fms",
            first_frame_ms,
        )
        self._store_frame(frame_bytes)

        # 3. 持续逐帧读取（跳帧到最新）
        error = None
        try:
            while self._running:
                frame_bytes = proc.stdout.read(self._frame_size)
                if not frame_bytes:
                    if not self._running:
                        break
                    error = "视频断流：FFmpeg已停止输出（直播结束或网络中断）"
                    break
                if len(frame_bytes) < self._frame_size:
                    if not self._running:
                        break
                    error = (
                        "数据长度错误：帧数据不完整"
                        "（实际视频尺寸与配置 width/height 不符，或视频断流）"
                    )
                    break

                # ★ 延迟优化核心：跳帧读取
                # 检查管道中是否还有完整帧可读，有则丢弃当前帧、读取更新的帧
                # 这样确保我们始终持有管道中最新的帧，而非积压的旧帧
                skipped = self._skip_to_latest(proc.stdout, frame_bytes)
                if skipped is not None:
                    # 有更新的帧，用最新的
                    frame_bytes = skipped

                self._store_frame(frame_bytes)
        except Exception as e:
            if self._running:
                error = f"读取异常: {e}（FFmpeg进程已退出）"

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

    def _build_ffmpeg_cmd(self):
        """构造 FFmpeg 极低延迟参数（CUDA 按需追加）"""
        cmd = [resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "warning"]

        # ---- 输入侧：极致低延迟参数 ----
        cmd += ["-fflags", "nobuffer+flush_packets"]  # 关闭输入缓冲 + 立即刷出包
        cmd += ["-flags", "low_delay"]                 # 低延迟解码
        cmd += ["-flags2", "fast"]                     # 快速解码（跳过不重要帧）
        cmd += ["-probesize", "32768"]                 # 32KB探测：快且足够检测FLV+h264
        cmd += ["-analyzeduration", "100000"]          # 0.1s分析：足够检测参数，快速启动
        cmd += ["-thread_queue_size", "1"]             # 输入线程队列只留1包
        cmd += ["-fflags", "+genpts"]                  # 生成PTS（缺少时补全，避免等待）

        # ---- CUDA 硬件解码 ----
        if self.use_cuda:
            cmd += ["-hwaccel", "cuda"]  # GPU解码，帧自动回传CPU（rawvideo需要CPU内存）

        cmd += ["-i", self.stream_url]

        # ---- 输出侧：仅视频 rawvideo ----
        cmd += ["-an", "-sn"]                     # 丢弃音频/字幕，减少处理
        cmd += ["-avioflags", "direct"]           # 直写，跳过AVIO内部缓冲
        cmd += ["-max_delay", "0"]                # 关闭复用器FIFO延迟
        cmd += ["-flush_packets", "1"]            # 立即刷出输出包
        cmd += ["-pix_fmt", "bgr24"]              # 输出 BGR 24bit（与numpy匹配）
        cmd += ["-f", "rawvideo", "pipe:1"]       # 原始视频写 stdout

        return cmd

    def _spawn_ffmpeg(self):
        """启动 FFmpeg 子进程（ffmpeg 不存在时抛出 FileNotFoundError）"""
        cmd = self._build_ffmpeg_cmd()
        logger.info("FFmpeg启动参数: %s", " ".join(cmd))

        # Windows 下不弹出黑色控制台窗口（后续由 GUI 调用时需要）
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

    def _drain_stderr(self, proc):
        """排空 FFmpeg stderr，保留最近若干行用于诊断"""
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
        except Exception:
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
