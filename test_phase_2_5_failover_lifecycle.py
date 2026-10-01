# -*- coding: utf-8 -*-
"""
Phase 2.5 验证：MainWindow 候选流 failover 生命周期

覆盖：
    1. A 启动失败 → B 成功
    2. A 失败 → B 失败 → C 成功
    3. A/B/C 全失败 → 回到 disconnected
    4. A 已经出首帧 → 后续断流不能触发 B
    5. disconnect 发生在 failover 前 → 不得启动 B
    6. closeEvent 发生在 failover 前 → 不得启动 B
    7. 旧 notifier 延迟 signal 不得消费旧 attempt
    8. failover 后只存在一个 reader / bridge / notifier
    9. 同步启动异常也走同一条 failover 路径
   10. teardown 不修改 _connected、不重启 OCR

全程不触碰真实网络 / FFmpeg：FFmpegReader 被替换为脚本化的替身。

运行： python test_phase_2_5_failover_lifecycle.py
"""

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PySide6.QtWidgets import QApplication

import gui.main_window as mw
from gui.main_window import MainWindow
from stream.models import StreamCandidate, StreamInfo

app = QApplication.instance() or QApplication(sys.argv)

_PASS = 0
_FAIL = 0

#: url → "ok" / "fail" / "raise"，控制每次尝试的替身行为
PLAN = {}
#: 所有被创建过的替身 reader（按创建顺序）
CREATED = []
#: 所有被创建过的 bridge（按创建顺序）
BRIDGES = []

A = "https://a.example/stream.flv"
B = "https://b.example/stream.flv"
C = "https://c.example/stream.flv"


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  ✓ %s" % msg)
    else:
        _FAIL += 1
        print("  ✗ %s" % msg)


def section(title):
    print("\n%s" % title)


def pump(seconds=0.6, until=None):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if until is not None and until():
            app.processEvents()
            return True
        time.sleep(0.005)
    app.processEvents()
    return until() if until else True


# ======================================================================
# 替身
# ======================================================================
class FakeReader:
    """FFmpegReader 的脚本化替身（只实现调用方真正用到的接口）

    签名必须与 _start_playback 的真实构造调用一致：音频第二路（audio_*）
    参数是后期加入构造调用的，替身不接住的话所有构造都会 TypeError →
    全部尝试同步失败 → 整个文件退化成"全失败"假象（2026-10-01 修复）。
    """

    def __init__(self, url, width=None, height=None, use_cuda=True,
                 http_headers=None, timing=None,
                 audio_enabled=False, audio_pcm_callback=None,
                 audio_sample_rate=None, audio_channels=None,
                 protocol=None):
        self.url = url
        # 记录**调用方传入**的尺寸，用来验证"未探测候选不得硬编码 1920x1080"
        self.requested_width = width
        self.requested_height = height
        self.http_headers = http_headers
        self.use_cuda = use_cuda
        # Phase 3.2：启动分段计时对象由调用方传入（真实 reader 同签名）
        self.startup_timing = timing

        # 模拟 FFmpegReader 自身在 width/height 为 None 时的自动探测结果
        self.width = width if width is not None else 1280
        self.height = height if height is not None else 720

        self.connected = False
        self.is_running = False
        self.error = None
        self.frames_read = 0
        self.first_frame_ms = None
        self.frames_skipped = 0
        self.stopped = False
        self._frame_id = 0
        self._frame = None
        CREATED.append(self)

    # ---- 生命周期 ----
    def start(self):
        plan = PLAN.get(self.url, "ok")
        if plan == "raise":
            raise RuntimeError("模拟 FFmpeg 启动异常")
        self.is_running = True
        if plan == "fail":
            # 模拟"进程立刻退出、从未出帧"：error 非空 + 已停止 + 从未 connected
            self.error = "连接失败：FFmpeg未返回任何数据"
            self.is_running = False
            return
        # ok：首帧已到达
        self.connected = True
        self.first_frame_ms = 12.0
        self.frames_read = 5
        self._frame_id = 5
        self._frame = np.full((self.height, self.width, 3), 7, dtype=np.uint8)

    def stop(self):
        self.stopped = True
        self.is_running = False
        self.connected = False

    # ---- 读侧 ----
    @property
    def frame_id(self):
        return self._frame_id

    def read_frame(self):
        return self._frame

    def pipe_backlog_stats(self):
        return {"samples": 0, "last": 0, "max": 0, "p50": 0, "p95": 0}


class RecordingBridge(mw._FrameBridge):
    """记录所有被创建过的 bridge，用来验证"同时只有一个" """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        BRIDGES.append(self)


_real_reader = mw.FFmpegReader
_real_bridge = mw._FrameBridge


def install_fakes():
    PLAN.clear()
    del CREATED[:]
    del BRIDGES[:]
    mw.FFmpegReader = FakeReader
    mw._FrameBridge = RecordingBridge


def restore_fakes():
    mw.FFmpegReader = _real_reader
    mw._FrameBridge = _real_bridge


def build_window():
    window = MainWindow()
    window.show()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    return window


def make_info(urls, width=1920, height=1080):
    candidates = [
        StreamCandidate(url=u, protocol="flv", http_headers={"User-Agent": "UA"})
        for u in urls
    ]
    return StreamInfo(
        platform="fake",
        room_id="room",
        candidates=candidates,
        selected=candidates[0] if candidates else None,
        width=width,
        height=height,
    )


def drive_failure(window):
    """模拟 16ms 健康检查发现失败（并抑制真实定时器，保证测试确定性）"""
    window._frame_timer.stop()
    window._on_frame_timeout()
    window._frame_timer.stop()


def connect(window, urls):
    window._handle_connect_success(make_info(urls))
    window._frame_timer.stop()      # 后续一律手动驱动，避免真实定时器插入


# ======================================================================
# [1] A 启动失败 → B 成功
# ======================================================================
def test_a_fail_b_ok():
    section("[1] A 启动失败 → B 成功")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    check(len(CREATED) == 1, "首次只创建了 1 个 reader")
    ra = CREATED[0]
    check(ra.url == A, "先试候选 A")
    check(window._connected is True, "会话立即进入 connected（沿用既有语义）")
    check(window._failover.total == 2 and window._failover.attempt_number == 1,
          "failover 记账：第 1/2 次尝试")

    drive_failure(window)

    check(len(CREATED) == 2, "A 失败后创建了第 2 个 reader")
    rb = CREATED[1]
    check(rb.url == B, "换到候选 B")
    check(ra.stopped is True, "A 的 reader 已被清理（stop() 被调用）")
    check(window._reader is rb, "当前 reader 是 B")
    check(window._connected is True, "failover 后会话仍是 connected")
    check(window._failover.attempt_number == 2, "failover 记账：第 2/2 次尝试")
    check(window._failover.failed_count == 1, "只记了 A 一次失败")

    # 分辨率规则：selected（A）用已探测值；未探测的 B 必须传 None
    check((ra.requested_width, ra.requested_height) == (1920, 1080),
          "A 是 selected，使用 info 里已探测的 1920x1080")
    check((rb.requested_width, rb.requested_height) is (None, None)
          or (rb.requested_width is None and rb.requested_height is None),
          "B 未被探测过 → 传 None 交给 FFmpegReader 自探测，而不是 1920x1080")
    check(rb.width == 1280, "B 使用了自己探测到的分辨率")

    window.close()
    restore_fakes()


# ======================================================================
# [2] A 失败 → B 失败 → C 成功
# ======================================================================
def test_a_b_fail_c_ok():
    section("[2] A 失败 → B 失败 → C 成功")
    install_fakes()
    PLAN.update({A: "fail", B: "fail", C: "ok"})
    window = build_window()

    connect(window, [A, B, C])
    drive_failure(window)                       # A → B
    check(len(CREATED) == 2 and CREATED[1].url == B, "第一次切换到了 B")
    check(CREATED[0].stopped is True, "A 已清理")

    drive_failure(window)                       # B → C
    check(len(CREATED) == 3 and CREATED[2].url == C, "第二次切换到了 C")
    check(CREATED[1].stopped is True, "B 已清理")
    check(window._reader is CREATED[2], "当前 reader 是 C")
    check(window._connected is True, "仍然 connected")
    check(window._failover.failed_count == 2, "记录了 A、B 两次失败")

    window.close()
    restore_fakes()


# ======================================================================
# [3] A/B/C 全失败 → 回到 disconnected
# ======================================================================
def test_all_fail():
    section("[3] A/B/C 全失败 → 回到 disconnected")
    install_fakes()
    PLAN.update({A: "fail", B: "fail", C: "fail"})
    window = build_window()

    connect(window, [A, B, C])
    drive_failure(window)
    drive_failure(window)
    drive_failure(window)                       # C 失败 → 候选耗尽

    check(len(CREATED) == 3, "三个候选各尝试了一次")
    check(all(r.stopped for r in CREATED), "三个 reader 都已清理")
    check(window._reader is None, "reader 已释放")
    check(window._frame_buffer is None, "buffer 已释放")
    check(window._frame_bridge is None, "bridge 已释放")
    check(window._frame_notifier is None, "notifier 已废弃")
    check(window._perf is None, "性能监控已释放")
    check(window._connected is False, "回到 disconnected")
    check(window._failover is None, "failover 上下文已丢弃")
    check(window._playback_info is None, "播放上下文已丢弃")
    check(window._frame_timer.isActive() is False, "健康检查定时器已停止")
    check(window._status_timer.isActive() is False, "状态刷新定时器已停止")
    check(window._connect_btn.text() == "连接", "按钮回到「连接」")

    # 再补一次迟到的健康检查：不得复活任何东西
    window._on_frame_timeout()
    check(len(CREATED) == 3, "断开后迟到的健康检查不会创建新 reader")

    window.close()
    restore_fakes()


# ======================================================================
# [4] A 已经出首帧 → 后续断流不能触发 B
# ======================================================================
def test_played_then_died_no_failover():
    section("[4] A 已经出首帧 → 后续断流不能触发 B（本阶段不自动重连）")
    install_fakes()
    PLAN.update({A: "ok", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    ra = CREATED[0]
    check(ra.frames_read == 5 and ra.first_frame_ms is not None,
          "A 已经出过首帧（frames_read=5）")

    # 模拟中途断流
    ra.error = "视频断流：FFmpeg已停止输出"
    ra.is_running = False
    ra.connected = False
    drive_failure(window)

    check(len(CREATED) == 1, "没有启动候选 B")
    check(window._connected is False, "直接进入断开流程")
    check(window._reader is None, "A 的 reader 已释放")

    # 同一规则的第二条路径：「直播已结束」（无 error，但已停止且未连接）
    connect(window, [A, B])
    ra2 = CREATED[-1]
    check(ra2.frames_read == 5, "第二次连接同样拿到了首帧")
    ra2.error = None
    ra2.is_running = False
    ra2.connected = False
    drive_failure(window)
    check(len(CREATED) == 2, "「直播已结束」路径同样不换候选")
    check(window._connected is False, "进入断开流程")

    window.close()
    restore_fakes()


# ======================================================================
# [5] disconnect 发生在 failover 前 → 不得启动 B
# ======================================================================
def test_disconnect_before_failover():
    section("[5] disconnect 发生在 failover 前 → 不得启动 B")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    check(len(CREATED) == 1, "只有 A 被创建")

    attempt_before = window._attempt_id
    window._disconnect()
    check(window._attempt_id != attempt_before,
          "disconnect 使旧 attempt 失效（attempt_id 已递增）")
    check(window._failover is None, "failover 上下文已丢弃")

    # 迟到的健康检查回调（模拟已排队的调用）
    window._on_frame_timeout()
    check(len(CREATED) == 1, "断开后不得再启动候选 B")
    check(window._reader is None, "依然没有 reader")

    # 直接验证：没有 failover 上下文时 _failover_next 一定拒绝启动
    check(window._failover_next() is False,
          "无 failover 上下文时 _failover_next() 返回 False")

    window.close()
    restore_fakes()


# ======================================================================
# [6] closeEvent 发生在 failover 前 → 不得启动 B
# ======================================================================
def test_close_before_failover():
    section("[6] closeEvent 发生在 failover 前 → 不得启动 B")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    window.close()                       # closeEvent → _disconnect
    app.processEvents()

    check(window._failover is None, "关闭后 failover 上下文已丢弃")
    check(window._reader is None, "关闭后 reader 已释放")

    window._on_frame_timeout()
    app.processEvents()
    check(len(CREATED) == 1, "窗口关闭后不得再启动候选")
    check(window._failover_next() is False, "关闭后 _failover_next() 返回 False")

    restore_fakes()


# ======================================================================
# [7] 旧 notifier 延迟 signal 不得消费旧 attempt
# ======================================================================
def test_stale_notifier():
    section("[7] 旧 notifier 的迟到唤醒不得污染新 attempt")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    old_bridge = window._frame_bridge
    old_notifier = window._frame_notifier

    drive_failure(window)

    new_bridge = window._frame_bridge
    new_notifier = window._frame_notifier
    check(old_notifier is not new_notifier, "换候选后是全新的 notifier 对象")
    check(old_bridge is not new_bridge, "换候选后是全新的 bridge 对象")
    check(old_bridge._running is False, "旧 bridge 线程已停止")

    # 先让新 attempt 的 bridge 停下来，并把它的在途通知排空，
    # 这样接下来能观察到的唯一干扰源就是"旧 notifier 的迟到唤醒"。
    new_bridge.stop()
    new_notifier.clear_pending()
    pump(0.3)
    new_notifier.clear_pending()

    # 模拟"已经排进 Qt 事件队列、来不及被 disconnect 拦下的旧唤醒"：
    # 重新把旧 notifier 接到窗口上，再发一次。
    old_notifier.frameReady.connect(window._on_frame_ready)
    window._frame_buffer.update(
        np.full((16, 16, 3), 99, dtype=np.uint8))
    before = window._last_displayed_frame_id
    pump(0.2)
    before = window._last_displayed_frame_id

    old_notifier.notify()
    pump(0.5)
    check(window._last_displayed_frame_id == before,
          "旧 notifier 的唤醒被丢弃，未消费新 attempt 的 buffer")

    # 新 notifier 必须仍然工作
    new_notifier.clear_pending()
    new_notifier.notify()
    ok = pump(1.5, lambda: window._last_displayed_frame_id != before)
    check(ok, "新 notifier 仍能正常唤醒 GUI 消费")

    window.close()
    restore_fakes()


# ======================================================================
# [8] failover 后只存在一个 reader / bridge / notifier
# ======================================================================
def test_single_owner_after_failover():
    section("[8] failover 后只存在一个 reader / bridge / notifier")
    install_fakes()
    PLAN.update({A: "fail", B: "fail", C: "ok"})
    window = build_window()

    connect(window, [A, B, C])
    drive_failure(window)
    drive_failure(window)

    check(len(CREATED) == 3, "三次尝试各创建了一个 reader")
    check(sum(1 for r in CREATED if not r.stopped) == 1,
          "只有最后那个 reader 是活的（其余均已 stop）")
    check(window._reader is CREATED[-1], "窗口持有的是最后一个 reader")

    check(len(BRIDGES) == 3, "每个候选一个 bridge")
    check(sum(1 for b in BRIDGES if b._running) == 1,
          "同一时刻只有一个 bridge 在运行")
    check(window._frame_bridge is BRIDGES[-1], "窗口持有的是最后一个 bridge")

    notifiers = [b.notifier for b in BRIDGES]
    check(len({id(n) for n in notifiers}) == len(notifiers),
          "每个 bridge 各自持有独立 notifier（未复用）")
    check(window._frame_notifier is notifiers[-1], "窗口持有的是最后一个 notifier")

    notifier_threads = {n.thread() for n in notifiers}
    check(notifier_threads == {app.thread()},
          "所有 notifier 都归属 GUI 线程")

    window.close()
    restore_fakes()


# ======================================================================
# [9] 同步启动异常也走同一条 failover 路径
# ======================================================================
def test_sync_start_failure():
    section("[9] 同步启动异常（构造/起线程）同样换候选")
    install_fakes()
    PLAN.update({A: "raise", B: "ok"})
    window = build_window()

    connect(window, [A, B])

    check(len(CREATED) == 2, "A 同步异常后直接尝试了 B")
    check(CREATED[0].url == A and CREATED[1].url == B, "顺序是 A → B")
    check(window._reader is CREATED[1], "当前 reader 是 B")
    check(window._connected is True, "仍然是 connected")
    check(window._failover.failed_count == 1, "同步失败被记入 failover")

    # 全部同步失败 → 断开
    PLAN.update({A: "raise", B: "raise"})
    window._disconnect()
    connect(window, [A, B])
    check(len(CREATED) == 4, "两个候选都尝试过")
    check(window._connected is False, "全同步失败后回到 disconnected")
    check(window._reader is None, "没有留下半成品 reader")

    window.close()
    restore_fakes()


# ======================================================================
# [10] teardown 不修改 _connected、不重启 OCR
# ======================================================================
def test_teardown_contract():
    section("[10] teardown 契约：不改 _connected、不重启 OCR")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()

    connect(window, [A, B])
    check(window._connected is True, "会话已 connected")

    # _teardown_playback() 单独调用：必须留下 connected 状态
    window._teardown_playback()
    check(window._connected is True, "_teardown_playback() 不修改 _connected")
    check(window._reader is None and window._frame_bridge is None,
          "资源确实已释放")
    check(window._failover is not None,
          "_teardown_playback() 不动 failover 上下文（那是 disconnect 的事）")

    # 重复调用必须安全（幂等）
    window._teardown_playback()
    window._teardown_playback()
    check(True, "_teardown_playback() 可重复调用")

    window.close()
    restore_fakes()

    section("[11] failover 期间不重启 OCR")
    install_fakes()
    PLAN.update({A: "fail", B: "ok"})
    window = build_window()
    stops = []
    original_stop_ocr = window._stop_ocr
    window._stop_ocr = lambda: (stops.append(1), original_stop_ocr())[1]

    connect(window, [A, B])
    drive_failure(window)
    check(stops == [], "failover 切换候选时不调用 _stop_ocr（ROI / 游戏档保留）")

    window._disconnect()
    check(stops == [1], "整条会话结束时才停止 OCR")

    window.close()
    restore_fakes()


# ======================================================================
# [12] 全链路：候选耗尽前的每一次切换都经过同一个 _start_playback
# ======================================================================
def test_single_entry_point():
    section("[12] 所有候选都经过同一个 _start_playback（无第二套初始化）")
    import inspect

    source = inspect.getsource(MainWindow)
    check(source.count("FFmpegReader(") == 1,
          "MainWindow 中只有一处创建 FFmpegReader（%d 处）"
          % source.count("FFmpegReader("))
    check(source.count("_FrameBridge(") == 1,
          "MainWindow 中只有一处创建 _FrameBridge")
    check("def _start_playback" in source, "存在唯一的播放初始化入口")
    check(source.count("frameReady.connect(") == 1,
          "frameReady 只在 _start_playback 里接线一次")

    install_fakes()
    PLAN.update({A: "fail", B: "fail", C: "ok"})
    window = build_window()
    connect(window, [A, B, C])
    check(window._reader is not None, "首个候选已启动")

    window.close()
    restore_fakes()


def main():
    print("=" * 68)
    print("Phase 2.5：MainWindow 候选流 failover 生命周期验证")
    print("=" * 68)
    test_a_fail_b_ok()
    test_a_b_fail_c_ok()
    test_all_fail()
    test_played_then_died_no_failover()
    test_disconnect_before_failover()
    test_close_before_failover()
    test_stale_notifier()
    test_single_owner_after_failover()
    test_sync_start_failure()
    test_teardown_contract()
    test_single_entry_point()

    print("\n" + "=" * 68)
    print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    print("=" * 68)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
