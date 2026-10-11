# -*- coding: utf-8 -*-
"""FFmpeg stderr 有界采集与独立落盘（O-1）

设计目标（不改任何直播读取/显示业务行为）：
    - FFmpeg stderr 的消费端是 ``FFmpegReader._drain_stderr`` 独立线程，
      本模块只作为它的**旁路**：消费线程每读一行调用一次 :meth:`feed`，
      feed 用 ``put_nowait`` —— **任何情况下都不阻塞消费线程**。
    - 磁盘写入由独立 writer 线程完成，与消费线程解耦。

降级策略（按失效模式逐一说明）：
    1. 队列满（stderr 量异常增大）：``put_nowait`` 失败 → 丢弃**本行**并
       ``dropped += 1``。选"丢新"而非"挤旧"：实现最小、无锁、且只会在
       病态量级下发生，此时保活比保序重要。
    2. 磁盘写入失败（IOError/权限/磁盘满）：writer 记 ``write_errors``，
       **继续出队并丢弃行**——消费端照常 feed，内存不增长，恢复后自动续写。
    3. writer 线程意外退出：循环体整体 try/except 包裹，除 stop 外唯一
       出口是不可恢复异常；即便真退出，消费端仍只做 put_nowait（队列满
       后降级为丢弃），**不会失去后继消费者导致 FFmpeg 阻塞**——因为
       FFmpeg 阻塞风险在消费线程，而消费线程永不依赖 writer。
    4. 大小上限：单文件超过 ``max_bytes`` 轮转为 ``.1``/``.2``…，保留
       最近 ``keep`` 个，最旧的直接删除。

生命周期：``start()`` → ``feed()``×N → ``stop()``。
``stop`` 先置事件再 join（带超时），返回是否确认退出；重复 stop 幂等。

开关：环境变量 ``DLV_STDERR_LOG=1`` 启用，**默认关闭**（与 DLV_DIAG_TRACE
同风格）。关闭时 feed 是一次布尔判断，模块不建线程不建文件。
"""

import logging
import os
import queue
import threading

logger = logging.getLogger("DouyinLowLatencyViewer.utils.stderr_recorder")

__all__ = ["StderrRecorder", "STDERR_RECORDER", "shutdown_stderr_recorder"]

_TRUTHY = ("1", "true", "yes", "on")

#: 行队列容量（行）。FFmpeg -loglevel info -nostats 的稳态 stderr 量极低
#: （头部若干行 + 罕见告警），2048 行 ≈ 数 MB 上限，远超任何实测场景。
DEFAULT_QUEUE_CAPACITY = 2048

#: 单文件字节数上限（默认 20MB），超过后轮转
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

#: 轮转保留个数（含当前文件名在内的历史份数）
DEFAULT_KEEP = 3

#: stop 时等待 writer 退出的秒数
DEFAULT_STOP_TIMEOUT = 2.0


class StderrRecorder:
    """有界、非阻塞、独立落盘的 stderr 旁路记录器（默认关闭）。"""

    def __init__(self, queue_capacity: int = DEFAULT_QUEUE_CAPACITY):
        self._on = (os.environ.get("DLV_STDERR_LOG") or "").strip().lower() in _TRUTHY
        self._q: queue.Queue | None = None
        self._queue_capacity = queue_capacity
        self._writer: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._path: str | None = None
        self.max_bytes = int(os.environ.get("DLV_STDERR_LOG_MAX_BYTES")
                             or DEFAULT_MAX_BYTES)
        self.keep = max(1, int(os.environ.get("DLV_STDERR_LOG_KEEP") or DEFAULT_KEEP))
        # ---- 可观察计数器 ----
        self.fed = 0            # 消费线程成功入队的行数
        self.dropped = 0        # 队列满被丢弃的行数
        self.written = 0        # writer 实际写盘的行数
        self.write_errors = 0   # 写盘失败次数
        self.last_error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._on

    @property
    def output_path(self) -> str | None:
        return self._path

    @property
    def writer_alive(self) -> bool:
        th = self._writer
        return bool(th is not None and th.is_alive())

    @property
    def queue_depth(self) -> int:
        return 0 if self._q is None else self._q.qsize()

    # ------------------------------------------------------------------
    def start(self, path: str | None = None) -> str | None:
        """启动落盘。成功（文件可写且 writer 已启动）返回路径，否则 None。"""
        if not self._on:
            return None
        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                return self._path
            if path is None:
                path = os.environ.get("DLV_STDERR_LOG_FILE") or os.path.join(
                    os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
                    "livelens_stderr_%d.log" % os.getpid())
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
            except OSError as e:
                self.last_error = "open failed: %s" % e
                logger.error("[STDERR-LOG] 无法写入 %s: %s", path, e)
                return None
            self._q = queue.Queue(maxsize=self._queue_capacity)
            self._path = path
            self._stop = threading.Event()
            th = threading.Thread(target=self._writer_loop, name="stderr-log-writer",
                                  daemon=True)
            self._writer = th
            th.start()
            logger.info("[STDERR-LOG] stderr 落盘已启用: %s", path)
            return path

    def feed(self, text: str) -> None:
        """消费线程调用：入队一行（**绝不阻塞**；满则丢弃计数）。"""
        if not self._on or self._q is None:
            return
        try:
            self._q.put_nowait(text)
            self.fed += 1
        except queue.Full:
            self.dropped += 1

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
        """停止 writer。返回 True 表示已确认线程退出；超时返回 False。"""
        with self._lock:
            th = self._writer
            stop = self._stop
            self._writer = None
            self._stop = None
        if stop is not None:
            stop.set()
        if th is None:
            return True
        th.join(timeout=timeout)
        if th.is_alive():
            logger.error("[STDERR-LOG] writer 线程 %.1fs 未退出（泄漏风险）", timeout)
            return False
        return True

    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        """独立落盘线程。任何单行失败都不退出；只有 stop 事件结束循环。"""
        stop = self._stop
        q = self._q
        assert stop is not None and q is not None
        try:
            f = open(self._path, "a", encoding="utf-8")
        except OSError as e:
            self.write_errors += 1
            self.last_error = "open failed: %s" % e
            # 打不开文件 → 退化为"只出队丢弃"模式，保证队列不积压、
            # 消费端不受影响；直到 stop。
            self._drain_and_discard(q, stop)
            return
        try:
            while not stop.wait(0.2):
                got = self._write_batch(f, q)
                if got:
                    try:
                        f.flush()
                    except OSError as e:
                        self.write_errors += 1
                        self.last_error = "flush failed: %s" % e
                    # 轮转：超过上限 → 关闭当前、滚动历史、重开新文件；
                    # 重开失败则进入"出队丢弃"模式直至成功或 stop。
                    if self._over_limit(f):
                        f = self._rotate(f, q, stop)
                        if f is None:
                            return  # stop 已置位
            # 收尾：把剩余行尽量写完（有界，最多队列现有量）
            self._write_batch(f, q)
            try:
                f.flush()
            except OSError:
                pass
        finally:
            try:
                f.close()
            except OSError:
                pass

    def _over_limit(self, f) -> bool:
        try:
            return f.tell() >= self.max_bytes
        except OSError:
            return False

    def _rotate(self, f, q, stop):
        """执行轮转并返回新文件对象；失败/停止返回 None。"""
        path = self._path
        try:
            size = f.tell()
        except OSError:
            size = -1
        try:
            f.close()
        except OSError:
            pass
        try:
            # 轮转语义：文件集为 path, path.1, …, path.(keep-1)（共 keep 份）。
            # 1) 删除最旧 path.(keep-1)；2) path.i → path.(i+1) 逐个后移；
            # 3) 当前 path → path.1；4) 重开新的 path。
            oldest = "%s.%d" % (path, self.keep - 1)
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(self.keep - 2, 0, -1):
                src = "%s.%d" % (path, i)
                dst = "%s.%d" % (path, i + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(path):
                os.replace(path, "%s.1" % path)
            logger.info("[STDERR-LOG] 轮转: %s (size=%s)", path, size)
        except OSError as e:
            self.write_errors += 1
            self.last_error = "rotate failed: %s" % e
        # 以追加模式重新打开；期间队列继续由 feed 填充（有界，满则丢）
        while not stop.is_set():
            try:
                return open(path, "a", encoding="utf-8")
            except OSError as e:
                self.write_errors += 1
                self.last_error = "reopen failed: %s" % e
                self._drain_some(q)
                stop.wait(0.2)
        return None

    def _drain_some(self, q) -> None:
        """磁盘不可用期间出队丢弃，防止内存积压（计入 dropped）。"""
        try:
            while True:
                q.get_nowait()
                self.dropped += 1
        except queue.Empty:
            pass

    def _write_batch(self, f, q) -> int:
        """把队列中当前所有行写盘；写失败时出队丢弃（保消费端活性）。"""
        n = 0
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            try:
                f.write(line + "\n")
                self.written += 1
            except OSError as e:
                self.write_errors += 1
                self.last_error = "write failed: %s" % e
                # 磁盘故障期间：继续出队丢弃，避免内存积压
            n += 1
        return n

    def _drain_and_discard(self, q, stop) -> None:
        while not stop.wait(0.2):
            self._drain_some(q)


#: 全局单例。关闭状态下 feed 是一次布尔判断。
STDERR_RECORDER = StderrRecorder()


def shutdown_stderr_recorder(timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
    """进程收尾时调用（幂等）；未启用时安全。"""
    return STDERR_RECORDER.stop(timeout=timeout)
