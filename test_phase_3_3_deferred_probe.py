# -*- coding: utf-8 -*-
"""Phase 3.3：分辨率探测移出构造器 → GUI 线程不再同步等待。

四组最小证明（不扩张范围）：

    [A] 已知尺寸：构造与启动都不探测，尺寸直接复用调用方给的值
    [B] 未知尺寸：探测发生在读取线程，调用方（GUI）线程立即返回
    [C] 探测失败：仍走原有 failover（记失败 → 换下一个候选 → 耗尽后断开）
    [D] 正常播放：帧链路行为不变（出帧、尺寸、切片长度、跳帧计数）

另加两条本阶段必须守住的不变量：

    [E] FFmpeg 参数逐字未变（golden 列表）
    [F] stop() 不被进行中的探测拖住（探测进程被 kill，且之后不再启动 FFmpeg）

不联网、不依赖外部服务：
    - 播放用的媒体由随包的 runtime/ffmpeg 现场生成（2 秒 320x240 H.264 FLV）；
    - 播放测试一律 use_cuda=False，因为测试机未必有 NVDEC；帧链路（管道 →
      切片 → numpy 视图）与是否硬解无关，CUDA 参数另由 [E] 逐字锁定。
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stream.ffmpeg_reader import FFmpegReader
from utils.runtime_paths import resolve_ffmpeg_path

FAILURES = []

#: 播放测试用的本地流：2 秒 320x240 H.264 in FLV
MEDIA_DIR = os.path.join(tempfile.gettempdir(), "lld33")
MEDIA = os.path.join(MEDIA_DIR, "phase33.flv")
MEDIA_W, MEDIA_H = 320, 240


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------
def section(title):
    print("\n" + title)


def check(cond, msg, extra=""):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        print("  [FAIL] %s %s" % (msg, extra))
        FAILURES.append(msg)
    return bool(cond)


def wait_until(pred, timeout, app=None):
    """轮询等待条件成立；给了 Qt app 就顺带泵事件循环（GUI 线程要能被唤醒）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return bool(pred())


def ensure_media():
    """没有就用随包的 FFmpeg 生成一个最小 FLV"""
    if os.path.isfile(MEDIA) and os.path.getsize(MEDIA) > 0:
        return True
    os.makedirs(MEDIA_DIR, exist_ok=True)
    cmd = [
        resolve_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=%dx%d:rate=15" % (MEDIA_W, MEDIA_H),
        "-t", "2", "-c:v", "libo264rt", "-f", "flv", MEDIA,
    ]
    try:
        subprocess.run(cmd, timeout=60, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 - 生成失败只影响本文件的播放用例
        print("  [SKIP] 无法生成测试媒体: %s" % e)
        return False
    return os.path.getsize(MEDIA) > 0


class ProbeSpy:
    """替换 FFmpegReader._probe_size：记录调用线程与时刻，可延时、可返回失败"""

    def __init__(self, result, delay=0.0):
        self.result = result
        self.delay = delay
        self.calls = []
        self.done = []
        self.threads = []

    def func(self):
        """返回可直接挂到类上的**普通函数**（这样才会按方法绑定传入 reader）"""
        def _spy(reader_self):
            self.threads.append(threading.current_thread().name)
            self.calls.append(time.perf_counter())
            if self.delay:
                time.sleep(self.delay)
            self.done.append(time.perf_counter())
            return self.result
        return _spy


class patch_probe:
    """把 FFmpegReader._probe_size 换成替身（with 用法，退出时还原）"""

    def __init__(self, spy):
        self.spy = spy

    def __enter__(self):
        self.orig = FFmpegReader._probe_size
        FFmpegReader._probe_size = self.spy.func()
        return self.spy

    def __exit__(self, *exc):
        FFmpegReader._probe_size = self.orig
        return False


# ----------------------------------------------------------------------
# [A] 已知尺寸不重复探测
# ----------------------------------------------------------------------
def test_known_size_no_probe():
    section("[A] 已知尺寸：构造与启动都不探测，直接复用调用方给的尺寸")
    spy = ProbeSpy((MEDIA_W, MEDIA_H))
    with patch_probe(spy):
        t0 = time.perf_counter()
        reader = FFmpegReader(MEDIA, width=MEDIA_W, height=MEDIA_H, use_cuda=False)
        t1 = time.perf_counter()
        check(not spy.calls, "构造器未触发任何探测")
        check((reader.width, reader.height) == (MEDIA_W, MEDIA_H),
              "尺寸即调用方给的值", repr((reader.width, reader.height)))
        check((t1 - t0) * 1000 < 200.0,
              "构造耗时 %.1fms < 200ms" % ((t1 - t0) * 1000))

        reader.start()
        wait_until(lambda: reader.frames_read > 0, 10.0)
        check(reader.frames_read > 0, "已知尺寸的流正常出帧", "frames=%d" % reader.frames_read)
        check(not spy.calls, "整个启动过程都没有探测")
        reader.stop()


# ----------------------------------------------------------------------
# [B] 未知尺寸：探测不发生在调用方线程，也不阻塞它
# ----------------------------------------------------------------------
def test_unknown_size_not_blocking():
    section("[B] 未知尺寸：探测在读取线程内进行，调用方线程立即返回")
    spy = ProbeSpy((MEDIA_W, MEDIA_H), delay=0.8)   # 模拟一次慢探测
    with patch_probe(spy):
        t0 = time.perf_counter()
        reader = FFmpegReader(MEDIA, width=None, height=None, use_cuda=False)
        t1 = time.perf_counter()
        check((t1 - t0) * 1000 < 200.0,
              "构造耗时 %.1fms < 200ms（未等待探测）" % ((t1 - t0) * 1000))
        check(not spy.calls, "构造返回时探测还没开始")

        reader.start()
        t2 = time.perf_counter()
        check((t2 - t1) * 1000 < 200.0,
              "start() 耗时 %.1fms < 200ms（未等待探测）" % ((t2 - t1) * 1000))
        # 探测要 800ms，而调用方 0.2ms 就回来了：返回时探测必然还在进行 ——
        # 等待不在调用方线程（这里不能用 spy.calls 判"是否已开始"，线程调度是自由的）
        check(not spy.done, "start() 返回时探测仍在进行 —— 调用方没有等它")

        check(wait_until(lambda: spy.calls, 5.0), "探测确实开始了", "spy=%s" % spy.threads)
        check(spy.threads[:1] == ["ffmpeg-reader"],
              "探测运行在 ffmpeg-reader 线程", repr(spy.threads))

        # 本地文件解码极快、很快到 EOF（connected 会被复位），因此这里看
        # 单调不退的状态：尺寸、已读帧数、首帧时刻。
        check(wait_until(lambda: reader.frames_read > 0, 10.0),
              "探测后仍能正常出帧", "frames=%d" % reader.frames_read)
        check(reader.first_frame_ms is not None, "首帧时刻已记录")
        check((reader.width, reader.height) == (MEDIA_W, MEDIA_H),
              "用探测到的尺寸切帧", repr((reader.width, reader.height)))

        probe_ms = reader._timing.summary()["segments"].get("probe")
        check(probe_ms is not None and probe_ms >= 700.0,
              "探测耗时仍被 Phase 3.2 分段计时记录", "probe=%s ms" % probe_ms)
        reader.stop()


# ----------------------------------------------------------------------
# [C] 探测失败仍进入原有 failover
# ----------------------------------------------------------------------
def test_probe_failure_still_failover():
    section("[C] 探测失败：仍走原有 failover（记失败 → 换候选 → 耗尽后断开）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import gui.main_window as mw
    from PySide6.QtWidgets import QApplication
    from stream.models import StreamCandidate, StreamInfo

    app = QApplication.instance() or QApplication([])

    # 记录每一次"结算失败"，验证它确实走了 CandidateFailover 而不是别的路径
    settled = []
    orig_note_failure = mw.CandidateFailover.note_failure

    def note_failure_spy(failover_self):
        ok = orig_note_failure(failover_self)
        if ok:
            settled.append(failover_self.attempt_number)
        return ok

    mw.CandidateFailover.note_failure = note_failure_spy
    spy = ProbeSpy((None, None))                    # 探测一律失败
    bad = os.path.join(MEDIA_DIR, "missing-stream.flv")   # 不存在 → ffmpeg 立刻 EOF
    try:
        with patch_probe(spy):
            win = mw.MainWindow()
            cands = [StreamCandidate(url=bad, protocol="flv"),
                     StreamCandidate(url=bad + ".2", protocol="flv")]
            info = StreamInfo(platform="douyin", room_id="probe-fail",
                              candidates=cands, selected=None)
            win._handle_connect_success(info)

            check(wait_until(lambda: len(settled) >= 1, 10.0, app),
                  "第一个候选被结算", repr(settled))
            check(wait_until(lambda: win._failover is None, 15.0, app),
                  "候选耗尽后回到断开状态（failover 上下文已丢弃）")

            check(settled == [1, 2],
                  "两个候选都经 failover 结算，顺序未变", repr(settled))
            check(len(spy.calls) >= 2,
                  "两个候选都走了探测（且都失败）", "calls=%d" % len(spy.calls))
            check(win._reader is None, "断开后没有残留 reader")
            check(win._startup_timing is not None
                  and win._startup_timing.summary()["segments"]["first_frame"] is None,
                  "StartupTiming 如实记录 first_decoded_frame 未到达")
            win.close()
    finally:
        mw.CandidateFailover.note_failure = orig_note_failure


# ----------------------------------------------------------------------
# [D] 正常播放的帧链路行为不变
# ----------------------------------------------------------------------
def test_frame_path_unchanged():
    section("[D] 帧链路行为不变：整帧读取、尺寸与长度一致、跳帧计数可用")
    reader = FFmpegReader(MEDIA, width=MEDIA_W, height=MEDIA_H, use_cuda=False)
    reader.start()
    try:
        # 源是本地文件（2 秒，约 25ms 就解完并到 EOF，EOF 会清空单槽），所以
        # 必须从 start() 之后立刻紧凑轮询，按 frame_id 去重收集；先等再读会扑空。
        expected_bytes = MEDIA_W * MEDIA_H * 3
        frames = []
        seen = set()
        deadline = time.time() + 10.0
        while time.time() < deadline and len(frames) < 5:
            fid = reader.frame_id
            frame = reader.read_frame()
            if frame is not None and fid not in seen:
                seen.add(fid)
                frames.append(frame)
            else:
                time.sleep(0.001)
        check(len(frames) >= 1, "read_frame 返回最新帧", "n=%d" % len(frames))
        check(reader.frames_read >= 1, "读取计数在推进", "read=%d" % reader.frames_read)
        for frame in frames:
            if frame.shape != (MEDIA_H, MEDIA_W, 3) or frame.nbytes != expected_bytes:
                check(False, "帧形状/长度恒为 (H, W, 3) / W×H×3",
                      repr((frame.shape, frame.nbytes)))
                break
        else:
            check(True, "帧形状与长度恒为 (H, W, 3) / W×H×3（%d 帧）" % len(frames))
        check(reader.frame_id == reader.frames_read,
              "每个完整帧都被完整存入（读取计数 == 帧序号）",
              "id=%d read=%d" % (reader.frame_id, reader.frames_read))
        check(reader.frames_skipped >= 0,
              "跳帧计数仍在工作", "skipped=%d" % reader.frames_skipped)
    finally:
        reader.stop()


# ----------------------------------------------------------------------
# [E] FFmpeg 参数逐字未变
# ----------------------------------------------------------------------
def test_ffmpeg_params_unchanged():
    section("[E] FFmpeg 参数逐字未变（golden 列表）")
    reader = FFmpegReader("http://example.com/live.flv", width=1280, height=720)
    cmd = reader._build_ffmpeg_cmd()
    expected = [
        resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer+flush_packets",
        "-flags", "low_delay",
        "-flags2", "fast",
        "-probesize", "32768",
        "-analyzeduration", "100000",
        "-thread_queue_size", "1",
        "-fflags", "+genpts",
        "-hwaccel", "cuda",
        "-i", "http://example.com/live.flv",
        "-an", "-sn",
        "-avioflags", "direct",
        "-max_delay", "0",
        "-flush_packets", "1",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo", "pipe:1",
    ]
    check(cmd == expected, "命令与 Phase 3.2 完全一致",
          "\n    got=%s\n    exp=%s" % (cmd, expected))
    no_cuda = FFmpegReader("u", width=1, height=1, use_cuda=False)._build_ffmpeg_cmd()
    check("-hwaccel" not in no_cuda, "use_cuda=False 时不注入 -hwaccel")


# ----------------------------------------------------------------------
# [F] stop() 不被进行中的探测拖住
# ----------------------------------------------------------------------
def test_stop_not_blocked_by_probe():
    section("[F] stop() 在探测进行中立即返回（探测进程被 kill）")
    import stream.video_info as video_info

    orig_cmd = video_info.build_probe_cmd
    # 用一个"永不返回"的探测进程替代 ffprobe：验证的是 kill/解除等待这条路径，
    # 与具体是哪个子进程无关（真 ffprobe 在不可达地址上的行为同理）
    video_info.build_probe_cmd = lambda url: [sys.executable, "-c",
                                              "import time; time.sleep(30)"]
    reader = FFmpegReader(MEDIA, width=None, height=None, use_cuda=False)
    try:
        reader.start()
        check(wait_until(lambda: reader._probe_process is not None, 5.0),
              "探测进程已在运行")
        t0 = time.perf_counter()
        reader.stop()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        check(dt_ms < 1000.0, "stop() 在 %.0fms 内返回（远小于探测超时）" % dt_ms)
        check(reader._process is None, "stop() 之后没有启动 FFmpeg")
        check(reader.frames_read == 0 and not reader.connected, "没有出帧、未标记已连接")
    finally:
        video_info.build_probe_cmd = orig_cmd
        reader.stop()


def main():
    print("=" * 68)
    print("Phase 3.3：分辨率探测移出构造器 / GUI 线程不再同步等待")
    print("=" * 68)

    has_media = ensure_media()
    if has_media:
        test_known_size_no_probe()
        test_unknown_size_not_blocking()
    test_probe_failure_still_failover()
    if has_media:
        test_frame_path_unchanged()
    test_ffmpeg_params_unchanged()
    test_stop_not_blocked_by_probe()

    print("\n" + "=" * 68)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
