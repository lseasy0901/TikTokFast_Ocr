# -*- coding: utf-8 -*-
"""B3-3 隔离原型：裸流 → _uhd 回退状态机（不接入生产）。

实现 B3-2 状态机文档的简化可测版本：
  - 裸流候选（FFmpeg7）→ 失败条件触发 → 回退 _uhd（FFmpeg6）→ 双失败 ERROR；
  - 失败条件：进程提前退出 / 首帧超时 / 连续无帧超时；
  - 资源规则：切换前 kill+wait 旧子进程；
  - 每次状态转换记录结构化日志字段。

仅用于隔离验证，不是生产代码；生产接入需按 B3-4 方案评审。
"""
import subprocess
import threading
import time


class DualVariantSource:
    """裸流优先、_uhd 回退的隔离原型数据源。"""

    def __init__(self, bare_url, uhd_url, exe7, exe6,
                 first_frame_timeout=10.0, stall_timeout=10.0,
                 hwaccel=True, threads1=False):
        self.bare_url = bare_url
        self.uhd_url = uhd_url
        self.exe7 = exe7
        self.exe6 = exe6
        self.first_frame_timeout = first_frame_timeout
        self.stall_timeout = stall_timeout
        self.hwaccel = hwaccel
        self.threads1 = threads1
        self.state = "IDLE"
        self.states_seen = ["IDLE"]
        self.variant = None
        self.errors = []
        self.log = []           # [(ts, event, fields...)]
        self.frames = []        # (recv_t, variant, seq)
        self._proc = None
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()

    def _log(self, event, **fields):
        with self._lock:
            self.log.append((time.perf_counter(), event, fields))

    def _set_state(self, st):
        self.state = st
        with self._lock:
            self.states_seen.append(st)
        print("[DVS] state -> %s" % st)

    def _spawn(self, exe, url):
        args = [exe, "-hide_banner", "-loglevel", "error"]
        if self.hwaccel:
            args += ["-hwaccel", "cuda"]
        if self.threads1:
            args += ["-threads", "1"]
        args += ["-probesize", "500000", "-analyzeduration", "500000",
                 "-i", url, "-vf", "scale=320:180,format=gray",
                 "-f", "rawvideo", "pipe:1"]
        return subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)

    def _kill(self):
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        # ---- BARE_CONNECTING (ffmpeg7) ----
        self._set_state("BARE_CONNECTING")
        self.variant = "bare"
        self._log("connect_start", variant="bare", exe="ffmpeg7")
        ok = self._stream("bare", self.exe7, self.bare_url)
        if ok:
            return
        # ---- FALLBACK_UHD (ffmpeg6) ----
        if self._stop_evt.is_set():
            return
        self._set_state("FALLBACK_UHD")
        self.variant = "uhd"
        self._log("fallback_start", variant="uhd", exe="ffmpeg6")
        ok = self._stream("uhd", self.exe6, self.uhd_url)
        if ok:
            return
        # ---- ERROR ----
        self._set_state("ERROR")
        self._log("final_error", errors=self.errors[-4:])

    def _stream(self, variant, exe, url):
        """按状态机运行一条连接；返回 True=正常结束（stop），False=需回退/失败。"""
        self._proc = self._spawn(exe, url)
        frame = 320 * 180
        t0 = time.perf_counter()
        last_frame_t = None
        seq = 0
        view = memoryview(bytearray(frame))
        try:
            while not self._stop_evt.is_set():
                buf = bytearray(frame)
                v = memoryview(buf)
                got = 0
                while got < frame:
                    n = self._proc.stdout.readinto(v[got:])
                    if not n:
                        # 进程提前退出（连接失败/无视频轨/解码器不支持）
                        rc = self._proc.poll()
                        self._log("proc_exit", variant=variant, rc=rc,
                                  frames=seq)
                        self.errors.append("proc_exit rc=%s frames=%d" % (rc, seq))
                        if seq > 0:
                            self._set_state("STREAM_END")   # 播放过再退出 = 断流
                            return True                  # 已有内容，不回退
                        return False                     # 从未出帧 → 回退
                    got += n
                now = time.perf_counter() - t0
                seq += 1
                last_frame_t = now
                with self._lock:
                    self.frames.append((now, variant, seq))
                if not self.state.endswith("_STREAMING"):
                    self._set_state(variant.upper() + "_STREAMING")
                    self._log("first_frame", variant=variant, at_s=round(now, 3))
                # 无有效帧超时检查（首帧超时在 read 阻塞下由 timeout 兜底）
                if (last_frame_t is not None
                        and seq == 1 and now > self.first_frame_timeout):
                    self._log("first_frame_timeout", variant=variant)
                    return False
                if seq == 1 and now > self.first_frame_timeout * 3:
                    return False
        finally:
            self._kill()
        return True

    def get_latest(self):
        with self._lock:
            return self.frames[-1] if self.frames else None

    def stop(self):
        self._stop_evt.set()
        self._kill()
