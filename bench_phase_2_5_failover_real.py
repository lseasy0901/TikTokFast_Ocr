# -*- coding: utf-8 -*-
"""
Phase 2.5 真实验证：真实 Huya 解析 + 真实 FFmpegReader + 真实 MainWindow failover

与单测不同，这里**不替换 FFmpegReader**，跑的是真 ffmpeg 子进程与真网络。
只对两个类做"记录用"的子类化（不改变行为），用来统计对象数量。

场景：
    1. 自然场景：按平台给出的真实候选顺序连接，观察是否自然发生 failover
    2. 注入场景：把候选顺序改造成 [一个确定不可用的候选, 真实候选]，
       在真实 reader / 真实网络下验证 "A 失败 → 清理 A → B 成功"

同时用 5ms 心跳定时器测量 GUI 线程的最大停顿（failover 的同步代价）。

用法：
    python bench_phase_2_5_failover_real.py <huya房间URL> [成功后保持秒数]

    QT_QPA_PLATFORM=offscreen python bench_phase_2_5_failover_real.py \
        https://www.huya.com/dank1ng 60
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import gui.main_window as mw
from gui.main_window import MainWindow
from stream.models import StreamCandidate, StreamInfo
from stream.resolver import StreamResolver

app = QApplication.instance() or QApplication(sys.argv)

#: 真实 FFmpegReader 的子类，只为统计创建数量
_READERS = []


class _CountingReader(mw.FFmpegReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _READERS.append(self)


_BRIDGES = []


class _CountingBridge(mw._FrameBridge):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _BRIDGES.append(self)


mw.FFmpegReader = _CountingReader
mw._FrameBridge = _CountingBridge


class StallProbe:
    """5ms 心跳：测 GUI 线程被同步操作挡住的时长"""

    def __init__(self):
        self.last = time.perf_counter()
        self.max_gap_ms = 0.0
        self.gaps = []
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(5)

    def _tick(self):
        now = time.perf_counter()
        gap = (now - self.last) * 1000.0
        self.last = now
        # 5ms 周期下正常抖动约 5ms；只记录明显超过周期的停顿
        if gap > 15.0:
            self.gaps.append(gap)
            self.max_gap_ms = max(self.max_gap_ms, gap)

    def reset(self):
        self.last = time.perf_counter()
        self.max_gap_ms = 0.0
        self.gaps.clear()


def pump(seconds, until=None):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if until is not None and until():
            app.processEvents()
            return True
        time.sleep(0.003)
    app.processEvents()
    return until() if until else True


def build_window():
    window = MainWindow()
    window.show()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.005)
    return window


def snapshot(window):
    r = window._reader
    return {
        "reader_url": (r.stream_url[:60] if r else None),
        "connected_field": (r.connected if r else None),
        "is_running": (r.is_running if r else None),
        "frames_read": (r.frames_read if r else None),
        "skipped": (r.frames_skipped if r else None),
        "first_frame_ms": (r.first_frame_ms if r else None),
        "error": (r.error if r else None),
        "pipe": (r.pipe_backlog_stats() if r else None),
        "session_connected": window._connected,
        "attempt": (window._failover.attempt_number if window._failover else None),
        "total": (window._failover.total if window._failover else None),
    }


def describe(failover):
    if failover is None:
        return "无 failover 上下文"
    return "attempt %d/%d, failed=%d" % (
        failover.attempt_number, failover.total, failover.failed_count)


def report_objects(window, label):
    alive_readers = [r for r in _READERS if not _stopped(r)]
    print("  [%s] reader 对象: 累计 %d, 存活 %d" %
          (label, len(_READERS), len(alive_readers)))
    print("  [%s] bridge 对象: 累计 %d, 运行中 %d" %
          (label, len(_BRIDGES), sum(1 for b in _BRIDGES if b._running)))
    print("  [%s] notifier 对象: 累计 %d（每个 bridge 一个，不复用）" %
          (label, len({id(b.notifier) for b in _BRIDGES})))
    print("  [%s] 窗口当前持有: reader=%s bridge=%s notifier=%s" % (
        label,
        window._reader is (_READERS[-1] if _READERS else None),
        window._frame_bridge is (_BRIDGES[-1] if _BRIDGES else None),
        window._frame_notifier is (_BRIDGES[-1].notifier if _BRIDGES else None),
    ))


def _stopped(reader):
    """真实 reader 的存活判定：读取线程是否还在跑"""
    return not reader.is_running and not reader.connected


# ======================================================================
# 场景 1：自然候选顺序
# ======================================================================
def scenario_natural(url, hold_seconds):
    print("\n" + "=" * 72)
    print("场景 1：真实候选顺序（观察是否自然发生 failover）")
    print("=" * 72)

    info = StreamResolver().resolve(url)
    print("解析结果: platform=%s room=%s 候选=%d 选中尺寸=%s" % (
        info.platform, info.room_id, len(info.candidates),
        f"{info.width}x{info.height}" if info.width else "未探测"))

    for i, c in enumerate(info.candidates):
        cdn = c.metadata.get("cdn")
        mark = " <- selected" if c is info.selected else ""
        print("  [%d] cdn=%-10s proto=%s%s" % (i, cdn, c.protocol, mark))

    del _READERS[:]
    del _BRIDGES[:]
    window = build_window()
    probe = StallProbe()
    probe.reset()

    window._handle_connect_success(info)

    # 让真实 16ms 健康检查定时器自己驱动 failover
    deadline = time.perf_counter() + 45.0
    switched = 0
    last_attempt = window._failover.attempt_number if window._failover else 1
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)
        if window._failover and window._failover.attempt_number != last_attempt:
            last_attempt = window._failover.attempt_number
            switched += 1
            print("  >> attempt %d/%d 切换完成，窗口 _last_failover_ms=%.0f ms"
                  % (last_attempt, window._failover.total,
                     window._last_failover_ms))
        if window._reader is not None and window._reader.connected:
            break
        if window._reader is None and window._failover is None and switched >= 0 \
                and window._connected is False:
            break

    ok = window._reader is not None and window._reader.connected
    print("\n真实 failover 次数: %d" % switched)
    print("是否成功播放: %s" % ok)
    print("首候选启动 + failover 阶段最大 GUI 停顿: %.1f ms" % probe.max_gap_ms)
    print("该阶段的停顿分布: %s"
          % (["%.0f" % g for g in probe.gaps[:12]] or "无"))
    print("_last_failover_ms（淘汰旧候选 → 启动新候选）: %.0f ms"
          % window._last_failover_ms)

    if ok:
        print("\n保持播放 %d 秒，期间采集读数..." % hold_seconds)
        base_frames = window._reader.frames_read
        probe.reset()
        held = pump(hold_seconds,
                    lambda: window._reader is None
                    or not window._reader.connected)
        r = window._reader
        if r is not None:
            print("  持续播放:                   %s" % (not held))
            print("  frames_read:                %d (净增 %d)"
                  % (r.frames_read, r.frames_read - base_frames))
            print("  connected / is_running:     %s / %s" % (r.connected, r.is_running))
            print("  frames_skipped:             %d" % r.frames_skipped)
            print("  first_frame_ms:             %s"
                  % (f"{r.first_frame_ms:.0f}" if r.first_frame_ms else None))
            print("  pipe backlog (p50/p95/max): %s / %s / %s" % (
                r.pipe_backlog_stats()["p50"], r.pipe_backlog_stats()["p95"],
                r.pipe_backlog_stats()["max"]))
            print("  error:                      %s" % r.error)
            print("  frameReady 通知:            %d 次 (合并 %d 次)" % (
                window._frame_notifier.notify_count,
                window._frame_notifier.coalesced_count))
            print("  frameReady 消费:            %d 帧上屏" % (
                window._perf.snapshot()["display_frames"]
                if window._perf else -1))
            print("  GUI 消费位置:               %d" % window._last_displayed_frame_id)
    else:
        print("  错误信息: %s" % (window._reader.error if window._reader else "无"))

    print("\n  GUI 线程最大停顿: %.1f ms" % probe.max_gap_ms)
    print("  超过 15ms 的停顿: %s" % (
        ["%.0f" % g for g in probe.gaps[:12]] or "无"))
    report_objects(window, "场景1")
    print("  会话状态: session_connected=%s, failover=%s"
          % (window._connected, describe(window._failover)))

    snapshot(window)
    window.close()
    probe.timer.stop()
    return switched, ok


# ======================================================================
# 场景 2：注入一个确定不可用的首候选
# ======================================================================
def scenario_injected(url, hold_seconds):
    print("\n" + "=" * 72)
    print("场景 2：注入不可用的首候选 → 真实 A 失败 → B 成功")
    print("=" * 72)

    real_info = StreamResolver().resolve(url)
    real = real_info.selected

    # 一个确定起不来的候选（本机 1 号端口：连接被拒，ffmpeg 立刻失败）
    bad = StreamCandidate(
        url="https://127.0.0.1:1/nonexistent.flv",
        protocol="flv",
        quality=None,
        metadata={"cdn": "INJECTED-BAD"},
        http_headers=dict(getattr(real, "http_headers", None) or {}),
    )

    info = StreamInfo(
        platform=real_info.platform,
        room_id=real_info.room_id,
        candidates=[bad, real],
        selected=bad,          # 首候选 = 注入的坏候选
        width=None, height=None,
    )
    print("候选顺序: [0] INJECTED-BAD (selected)  [1] %s (真实 cdn=%s)" % (
        real.metadata.get("cdn"), real.metadata.get("cdn")))
    print("注意: 真实候选不是 selected → 分辨率传 None，由 FFmpegReader 自己探测")

    del _READERS[:]
    del _BRIDGES[:]
    window = build_window()
    probe = StallProbe()

    window._handle_connect_success(info)
    print("\n首候选已启动: reader#%d url=%s"
          % (len(_READERS), _READERS[0].stream_url[:60]))

    # 真实 16ms 健康检查定时器驱动 failover；同时测 GUI 停顿
    probe.reset()
    switched = False
    deadline = time.perf_counter() + 45.0
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)
        if window._failover and window._failover.attempt_number == 2 and not switched:
            switched = True
            print("  >> 已切到候选 2/2，_last_failover_ms=%.0f ms"
                  % window._last_failover_ms)
        if window._reader is window._reader and len(_READERS) >= 2 \
                and _READERS[1].connected:
            break

    print("\n是否发生 failover: %s" % switched)
    print("创建的真实 reader:")
    for i, r in enumerate(_READERS):
        print("  #%d url=%-50s cdn=%-12s connected=%s frames=%d error=%s"
              % (i, r.stream_url[:50],
                 "INJECTED" if i == 0 else "REAL",
                 r.connected, r.frames_read,
                 (r.error or "")[:40]))

    ok = len(_READERS) >= 2 and _READERS[1].connected
    print("\nB 是否成功播放: %s" % ok)
    if _READERS:
        print("A 是否已清理: stop 后被置为 not running = %s"
              % (not _READERS[0].is_running))
    print("failover 记账: %s" % describe(window._failover))

    if ok:
        print("\n保持播放 %d 秒..." % hold_seconds)
        base = _READERS[1].frames_read
        held = pump(hold_seconds,
                    lambda: window._reader is None
                    or not window._reader.connected)
        r = window._reader
        print("  持续播放:                   %s" % (not held))
        print("  frames_read:                %d (净增 %d)"
              % (r.frames_read, r.frames_read - base))
        print("  frames_skipped:             %d" % r.frames_skipped)
        print("  探测器探测到的尺寸:         %dx%d" % (r.width, r.height))
        print("  pipe backlog (p50/p95/max): %s / %s / %s" % (
            r.pipe_backlog_stats()["p50"], r.pipe_backlog_stats()["p95"],
            r.pipe_backlog_stats()["max"]))
        print("  error:                      %s" % r.error)
        print("  frameReady 通知:            %d 次 (合并 %d 次)" % (
            window._frame_notifier.notify_count,
            window._frame_notifier.coalesced_count))

    print("\n  GUI 线程最大停顿: %.1f ms" % probe.max_gap_ms)
    print("  超过 15ms 的停顿: %s" % (
        ["%.0f" % g for g in probe.gaps[:12]] or "无"))
    print("  _last_failover_ms: %.0f ms" % window._last_failover_ms)
    report_objects(window, "场景2")
    print("  会话状态: session_connected=%s" % window._connected)

    window.close()
    probe.timer.stop()
    return ok


# ======================================================================
# 场景 3：failover 时新候选的同步 ffprobe 超时（最坏情况耗时）
# ======================================================================
def scenario_probe_timeout():
    print("\n" + "=" * 72)
    print("场景 3：新候选触发 FFmpegReader 同步分辨率探测且探测超时的耗时")
    print("（这是纯输入构造，用来量出最坏情况的 GUI 阻塞，不是产品行为）")
    print("=" * 72)

    def cand(name, url):
        return StreamCandidate(url=url, protocol="flv",
                               metadata={"cdn": name}, http_headers={})

    # 候选 1：本机 1 号端口，连接被拒 → 立刻失败
    # 候选 2：黑洞地址（不可路由）→ ffprobe 必须等到 5s 超时才返回
    #         且它**不是 selected** → 走 width=None → 触发同步探测
    first = cand("FAST-FAIL", "https://127.0.0.1:1/nonexistent.flv")
    second = cand("PROBE-HANG", "http://10.255.255.1/nonexistent.flv")
    info = StreamInfo(platform="huya", room_id="bench",
                      candidates=[first, second], selected=first,
                      width=1920, height=1080)

    del _READERS[:]
    del _BRIDGES[:]
    window = build_window()
    probe = StallProbe()
    probe.reset()

    window._handle_connect_success(info)

    t0 = time.perf_counter()
    deadline = t0 + 60.0
    transitions = []            # [(t_ms, attempt, _last_failover_ms)]
    last_attempt = 1
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.003)
        fo = window._failover
        if fo is not None and fo.attempt_number != last_attempt:
            last_attempt = fo.attempt_number
            transitions.append(((time.perf_counter() - t0) * 1000.0,
                                last_attempt, window._last_failover_ms))
        if fo is None:                          # 候选耗尽 → 已结束连接
            break

    elapsed = (time.perf_counter() - t0) * 1000.0
    print("\n  两个候选都被判失败并结束连接: %s" % (window._failover is None))
    print("  转换时间线 (t=ms, attempt, 该次淘汰→启动耗时 ms):")
    for t_ms, att, cost in transitions:
        print("    t=%-8.0f attempt=%d  _last_failover_ms=%.0f" % (t_ms, att, cost))
    costs = [c for _, _, c in transitions]
    print("  单次 failover 最大耗时:              %.0f ms"
          % (max(costs) if costs else 0))
    print("  从首候选启动到全部判定失败:          %.0f ms" % elapsed)
    print("  GUI 线程最大停顿:                    %.1f ms" % probe.max_gap_ms)
    print("  超过 15ms 的停顿（前 12 个）:        %s"
          % (["%.0f" % g for g in probe.gaps[:12]] or "无"))

    window.close()
    probe.timer.stop()
    return max(costs) if costs else 0.0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    print("=" * 72)
    print("Phase 2.5 真实验证（真实网络 + 真实 ffmpeg + 真实 MainWindow）")
    print("房间: %s   成功后保持: %ds" % (url, hold))
    print("=" * 72)

    scenario_natural(url, hold)
    scenario_injected(url, hold)
    scenario_probe_timeout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
