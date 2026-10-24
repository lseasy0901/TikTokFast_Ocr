# -*- coding: utf-8 -*-
"""
Phase B-2 验证：帧消费从「16ms QTimer 轮询」切换为「frameReady 通知驱动」

覆盖：
    1. 单帧到达能触发 GUI 消费
    2. 连续通知可合并，不产生无限 pending signal
    3. 多帧快速到达时 GUI 最终消费"当前最新帧"
    4. 中间帧被覆盖/丢弃（单槽语义不变）
    5. FrameBridge 不直接操作 QWidget；消费发生在 GUI 线程
    6. reader 停止/断开后不再消费
    7. 重连不产生重复的信号连接
    8. 窗口关闭后无后台 GUI 更新
    9. 既有播放链不回归（LatestFrameBuffer 语义、Timer 不再消费帧）

运行： python test_phase_b2_frame_ready.py
"""

import inspect
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow, _FrameBridge, _FrameNotifier
from stream.frame_buffer import LatestFrameBuffer
from utils.performance import PerformanceMonitor

app = QApplication.instance() or QApplication(sys.argv)

_PASS = 0
_FAIL = 0


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


def pump(seconds=1.0, until=None):
    """处理 GUI 事件直到条件成立或超时"""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if until is not None and until():
            app.processEvents()
            return True
        time.sleep(0.005)
    app.processEvents()
    return until() if until else True


class FakeReader:
    """只实现 _FrameBridge.读侧用到的接口"""

    def __init__(self, w=16, h=16):
        self._w, self._h = w, h
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self.is_running = True
        self.connected = True
        self.error = None
        self.frames_read = 0
        self.frames_skipped = 0
        self.first_frame_ms = 1.0

    def push(self, value):
        """产出一帧：像素统一填充 value，便于断言"拿到的是哪一帧" """
        frame = np.full((self._h, self._w, 3), value, dtype=np.uint8)
        with self._lock:
            self._frame = frame
            self._frame_id += 1
            self.frames_read += 1
        return self._frame_id

    @property
    def frame_id(self):
        with self._lock:
            return self._frame_id

    def read_frame(self):
        with self._lock:
            return self._frame

    def stop(self):
        self.is_running = False


def build_window():
    window = MainWindow()
    window.show()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    return window


def wire(window):
    """按生产接线把一套 reader/buffer/perf/bridge 装到窗口上"""
    reader = FakeReader()
    buffer = LatestFrameBuffer()
    perf = PerformanceMonitor()
    perf.start()
    bridge = _FrameBridge(reader, buffer, perf)
    window._reader = reader
    window._frame_buffer = buffer
    window._perf = perf
    window._frame_bridge = bridge
    window._frame_notifier = bridge.notifier
    bridge.notifier.frameReady.connect(window._on_frame_ready)
    return reader, buffer, perf, bridge


def spy_display(window):
    """记录每次真正上屏的帧内容与所在线程"""
    shown = []
    original = window._video_widget.set_frame

    def wrapped(frame):
        shown.append((threading.current_thread(), int(frame[0, 0, 0])))
        original(frame)

    window._video_widget.set_frame = wrapped
    return shown


# ======================================================================
# [1] 单帧到达能触发 GUI 消费（走真实 FrameBridge 线程 + 真实通知器）
# ======================================================================
def test_single_frame_triggers_consume():
    section("[1] 单帧到达触发 GUI 消费（真实 _FrameBridge + frameReady）")
    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    shown = spy_display(window)

    reader.push(11)
    bridge.start()

    ok = pump(3.0, lambda: len(shown) >= 1)
    check(ok, "帧到达后 GUI 被唤醒并显示了帧")
    check(shown and shown[-1][1] == 11, "显示的是那一帧的内容 (期望 11)")
    check(window._last_displayed_frame_id == buffer.frame_id,
          "消费位置追平 buffer 当前 frame_id")
    check(bridge.notifier.notify_count >= 1, "bridge 确实发出了 frameReady 通知")

    bridge.stop()
    window.close()


# ======================================================================
# [2] 连续通知可合并，不产生无限 pending signal
# ======================================================================
def test_notifications_coalesce():
    section("[2] 连续通知合并，不无限积压")
    notifier = _FrameNotifier()
    for _ in range(100):
        notifier.notify()
    check(notifier.notify_count == 1, "100 次连续 notify 只发出 1 个信号")
    check(notifier.coalesced_count == 99, "其余 99 次被合并计数")

    notifier.clear_pending()
    notifier.notify()
    check(notifier.notify_count == 2, "消费后下一帧可以重新唤醒 (notify_count=2)")

    fired = []
    notifier.frameReady.connect(lambda: fired.append(1))
    notifier.clear_pending()
    for _ in range(50):
        notifier.notify()
    app.processEvents()
    check(len(fired) == 1, "事件队列里始终只有一个待处理通知 (实际 %d)" % len(fired))


# ======================================================================
# [3][4] 多帧快速到达：只消费最新帧，中间帧被覆盖丢弃
# ======================================================================
def test_latest_frame_wins():
    section("[3][4] 多帧快速到达只消费最新帧，中间帧被覆盖")
    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    shown = spy_display(window)

    for value in (1, 2, 3, 4, 5):
        frame = np.full((16, 16, 3), value, dtype=np.uint8)
        buffer.update(frame)          # 单槽覆盖
    bridge.notifier.notify()          # 模拟"期间只攒下一次通知"

    pump(1.0, lambda: len(shown) >= 1)
    check(len(shown) == 1, "只消费了一次 (实际 %d)" % len(shown))
    check(shown[0][1] == 5, "消费到的是最新帧内容 5（不是 1）")
    check(int(buffer.get()[0, 0, 0]) == 5, "buffer 当前持有第 5 帧")
    check(buffer.frame_id == 5, "buffer frame_id 单调推进到 5")

    window.close()


# ======================================================================
# [5] FrameBridge 不碰 QWidget，消费发生在 GUI 线程
# ======================================================================
def test_thread_discipline():
    section("[5] 线程纪律：bridge 不碰 QWidget，消费在 GUI 线程")
    source = inspect.getsource(_FrameBridge)
    check("QWidget" not in source, "_FrameBridge 源码不出现 QWidget")
    check("_video_widget" not in source, "_FrameBridge 源码不出现 _video_widget")
    check("set_frame" not in source, "_FrameBridge 源码不出现 set_frame")
    check("notifier.notify()" in source, "_FrameBridge 在写 buffer 后调用 notifier.notify()")
    check("clear_pending" not in source, "bridge 侧不参与 pending 清理（由 GUI 负责）")

    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    check(isinstance(bridge.notifier, _FrameNotifier), "bridge 自身持有一个 _FrameNotifier")
    check(bridge.notifier.thread() is app.thread(),
          "通知器归属 GUI 线程（跨线程投递才会走队列连接）")
    shown = spy_display(window)
    reader.push(7)
    bridge.start()
    pump(3.0, lambda: len(shown) >= 1)
    check(shown and shown[-1][0] is threading.main_thread(),
          "set_frame 在主线程执行（实际 %s）" % (shown[-1][0].name if shown else "无"))
    bridge.stop()
    window.close()


# ======================================================================
# [6] reader 停止/断开后不再消费
# ======================================================================
def test_no_consume_after_reader_gone():
    section("[6] reader 停止后不再消费")
    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    shown = spy_display(window)

    buffer.update(np.full((16, 16, 3), 42, dtype=np.uint8))
    window._on_frame_ready()                    # 正常路径：应消费
    check(len(shown) == 1, "reader 在位时正常消费")

    window._reader = None                       # 模拟 reader 已拆除
    buffer.update(np.full((16, 16, 3), 43, dtype=np.uint8))
    window._on_frame_ready()
    check(len(shown) == 1, "reader 为 None 时不再消费新帧")

    window.close()


# ======================================================================
# [7] 重连不产生重复的信号连接
# ======================================================================
def test_reconnect_no_duplicate_connection():
    section("[7] 重连不产生重复信号连接")
    window = build_window()
    r1, b1, p1, bridge1 = wire(window)
    n1 = bridge1.notifier

    # 模拟断开 → 重连：新 bridge 会带一个全新通知器
    window._frame_bridge = None
    window._frame_notifier = None
    r2, b2, p2, bridge2 = wire(window)
    n2 = bridge2.notifier

    check(n1 is not n2, "重连后是全新的通知器对象（不会叠加旧连接）")

    shown = spy_display(window)
    b2.update(np.full((16, 16, 3), 9, dtype=np.uint8))
    n2.notify()
    pump(1.0, lambda: len(shown) >= 1)
    check(len(shown) == 1, "一次通知只消费一次 (实际 %d)" % len(shown))

    # 旧通知器若还有残留唤醒，也不应影响新连接
    n1.clear_pending()
    n1.notify()
    pump(0.5)
    check(len(shown) == 1, "旧通知器的残留唤醒未造成额外消费")

    window.close()


# ======================================================================
# [8] 窗口关闭后无后台 GUI 更新
# ======================================================================
def test_close_stops_everything():
    section("[8] 窗口关闭后无后台更新")
    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    shown = spy_display(window)
    window._frame_timer.start(16)

    window.close()
    app.processEvents()

    check(window._reader is None, "关闭后 reader 已释放")
    check(window._frame_buffer is None, "关闭后 frame buffer 已释放")
    check(window._frame_bridge is None, "关闭后 frame bridge 已释放")
    check(window._frame_notifier is None, "关闭后通知器已废弃")
    check(window._frame_timer.isActive() is False, "关闭后健康检查定时器已停止")

    before = len(shown)
    window._on_frame_ready()                    # 已排队的通知 / 迟到回调
    app.processEvents()
    check(len(shown) == before, "关闭后迟到的通知不再触发上屏")


# ======================================================================
# [9] 既有播放链不回归
# ======================================================================
def test_no_regression():
    section("[9] 既有播放链不回归")

    buf = LatestFrameBuffer()
    buf.update(np.full((4, 4, 3), 1, dtype=np.uint8))
    buf.update(np.full((4, 4, 3), 2, dtype=np.uint8))
    check(int(buf.get()[0, 0, 0]) == 2, "LatestFrameBuffer 仍是单槽覆盖（留最新）")
    check(buf.frame_id == 2 and buf.has_frame, "frame_id 单调递增 / has_frame 正常")
    buf.clear()
    check(buf.get() is None and buf.frame_id == 0, "clear() 语义不变")
    check(not hasattr(buf, "queue") and not hasattr(buf, "maxlen"),
          "未引入 FIFO / 多帧缓存")

    window = build_window()
    reader, buffer, perf, bridge = wire(window)
    shown = spy_display(window)

    # 关键回归点：健康检查定时器必须不再消费帧
    buffer.update(np.full((16, 16, 3), 66, dtype=np.uint8))
    for _ in range(5):
        window._on_frame_timeout()
    app.processEvents()
    check(len(shown) == 0, "16ms 健康检查不再消费帧（帧仍待在 buffer 里）")
    check(window._last_displayed_frame_id == 0, "健康检查未推进消费位置")

    # 但健康检查仍能发现 reader 错误
    reader.error = "模拟断流"
    window._on_frame_timeout()
    check(window._reader is None, "健康检查仍能在 reader 报错时触发断开")

    window.close()


# ======================================================================
# [10] 延迟指标口径：只记 frame-arrival → GUI 取走，不含渲染耗时
# ======================================================================
def test_latency_excludes_render_time():
    section("[10] 延迟指标排除渲染耗时")
    window = build_window()
    reader, buffer, perf, bridge = wire(window)

    def slow_set_frame(frame):
        time.sleep(0.06)          # 人为把渲染拉长到 60ms

    window._video_widget.set_frame = slow_set_frame

    perf.on_capture()             # 生产顺序：写 buffer → on_capture → 通知
    buffer.update(np.full((16, 16, 3), 3, dtype=np.uint8))
    window._on_frame_ready()

    check(perf.current_latency_ms < 60.0,
          "延迟读数 %.1fms 未把 60ms 渲染耗时算进来" % perf.current_latency_ms)
    check(perf.current_latency_ms >= 0.0, "延迟读数非负")
    check(window._last_displayed_frame_id == buffer.frame_id, "该帧确实已消费")

    window.close()


def main():
    print("=" * 68)
    print("Phase B-2：frameReady 通知驱动 验证")
    print("=" * 68)
    test_single_frame_triggers_consume()
    test_notifications_coalesce()
    test_latest_frame_wins()
    test_thread_discipline()
    test_no_consume_after_reader_gone()
    test_reconnect_no_duplicate_connection()
    test_close_stops_everything()
    test_no_regression()
    test_latency_excludes_render_time()

    print("\n" + "=" * 68)
    print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    print("=" * 68)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
