# -*- coding: utf-8 -*-
"""Phase 7 · R2 —— 水印开/关的进程级 CPU 对照（在 release02-2.2.0 新基线上）。

背景
----
Phase 5 §7.4 只在同一进程内先后取了两段 3 秒的粗略采样（0.82% vs 0.64%），
差值落在噪声内，不足以支撑"隐藏即零开销"的结论；Phase 5 §7.1 也明确记录过
"整进程 CPU 采样在本机噪声过大"。R2 要求在**新基线**上重做一次开/关对照。

计量方式（可归因、可复现）
--------------------------
1. **CPU 时间差法（主指标）**：``psutil.Process.cpu_times()`` 取窗口前后的
   user+system 累计 CPU 时间，除以墙钟时间 → 平均 CPU 百分比。不受 100ms
   采样粒度与采样相位影响。
2. **逐帧计数（有效性校验）**：统计 ``_on_tick`` 与 ``paintEvent`` 的真实调用
   次数。若窗口被遮挡/未 expose，Windows 不下发 WM_PAINT，水印就不画，
   CPU 差值会失真 —— 因此必须校验 paints≈ticks，否则本次数据作废。
3. **预热 + 交替多轮**：先预热丢弃，再 ON/OFF 交替 5 轮（奇偶轮交换先后顺序），
   取中位数，抵消启动缓存预热与单向漂移。

测量对象：未激活状态下的 ``MainWindow``。两种状态下**未激活遮罩都在**，
因此 ON−OFF 的差值只归因于水印覆盖层。

用法（worktree 根目录）：
    <venv-python> tests/ui_redesign/watermark_perf_ab.py \
        --json docs/ui-redesign/evidence/phase7_watermark_ab.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time

# --- 隔离用户数据：不读真实许可证，也不写真实配置 -------------------------
_TEMP = tempfile.mkdtemp(prefix="uilg_wm_ab_")
os.environ["APPDATA"] = os.path.join(_TEMP, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TEMP, "AppData", "Local")
os.environ["USERPROFILE"] = _TEMP
for _p in (os.environ["APPDATA"], os.environ["LOCALAPPDATA"]):
    os.makedirs(_p, exist_ok=True)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psutil  # noqa: E402

# PySide6 / gui 的导入必须推迟到确定 QPA 平台之后：
# 一旦 Qt 被导入，平台插件就固定了。


def _pump(app, seconds: float) -> None:
    """在给定墙钟时间内持续处理事件（不阻塞 Qt 事件循环）。"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.01)


def _cpu_times_s(proc: psutil.Process) -> float:
    t = proc.cpu_times()
    return float(t.user + t.system)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default="")
    ap.add_argument("--rounds", type=int, default=5, help="OFF/ON 各测几轮（预热不计）")
    ap.add_argument("--window", type=float, default=5.0, help="每轮测量窗口秒数")
    ap.add_argument("--settle", type=float, default=0.8, help="切换状态后的稳定时间")
    ap.add_argument("--warmup", type=float, default=6.0, help="预热秒数（丢弃）")
    ap.add_argument("--offscreen", action="store_true",
                    help="用 offscreen 平台测量（窗口不出现、不会被最小化/遮挡，"
                         "但也不含 DWM 合成开销）")
    ap.add_argument("--pause-timers", action="store_true",
                    help="测量期间暂停应用自身的周期定时器（帧轮询/状态刷新等），"
                         "使唯一变量是水印 —— 隔离变量，不是隐瞒")
    args = ap.parse_args()

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from gui import theme  # noqa: PLC0415

    result: dict = {
        "ok": False,
        "method": "process_cpu_times_delta",
        "platform": "offscreen" if args.offscreen else "native",
        "rounds": args.rounds,
        "window_s": args.window,
        "settle_s": args.settle,
        "warmup_s": args.warmup,
        "note": "未激活遮罩在两种状态下均存在；ON-OFF 差值仅归因于水印覆盖层",
    }

    theme.configure_high_dpi()
    app = QApplication(sys.argv)
    app.setApplicationName("Douyin Low Latency Stream Viewer")

    import gui.main_window as mw
    import gui.watermark as wmmod
    from gui.watermark import WatermarkOverlay

    # --- 计数版水印：统计真实 tick / paint 次数 --------------------------
    class CountingWatermark(WatermarkOverlay):
        tick_count = 0
        paint_count = 0
        step_us = 0.0
        paint_us = 0.0

        def _on_tick(self):  # noqa: D102
            type(self).tick_count += 1
            t = time.perf_counter()
            super()._on_tick()
            type(self).step_us += (time.perf_counter() - t) * 1e6

        def paintEvent(self, ev):  # noqa: D102
            type(self).paint_count += 1
            t = time.perf_counter()
            super().paintEvent(ev)
            type(self).paint_us += (time.perf_counter() - t) * 1e6

    # main_window 里是 `from gui.watermark import WatermarkOverlay`，
    # 名字在导入时已绑定，因此两处都要替换。
    wmmod.WatermarkOverlay = CountingWatermark
    mw.WatermarkOverlay = CountingWatermark

    # --- 屏蔽会阻塞的模态对话框 -----------------------------------------
    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return 0

        def show(self):
            pass

        def close(self):
            pass

    mw.SettingsDialog = _Stub          # type: ignore[assignment]
    mw.ActivationDialog = _Stub        # type: ignore[assignment]
    mw.ExpiredDialog = _Stub           # type: ignore[assignment]

    window = mw.MainWindow()
    window.maybe_show_first_run_guide = lambda *a, **kw: None
    window.show()
    window.raise_()
    window.activateWindow()
    _pump(app, 1.5)

    overlay = window._watermark
    result["overlay_class"] = type(overlay).__name__
    result["tick_ms"] = getattr(overlay, "TICK_MS", None)

    # 强制进入"未激活"显示态（与 _sync_watermark 的入参语义一致）
    window._sync_watermark(True)
    _pump(app, 0.5)
    result["watermark_visible_when_on"] = bool(overlay.isVisible())
    result["watermark_animating_when_on"] = bool(overlay.is_animating())
    result["watermark_size"] = list(overlay.watermark_size())
    try:
        handle = window.windowHandle()
        result["window_exposed"] = bool(handle.isExposed()) if handle else None
    except Exception:  # noqa: BLE001
        result["window_exposed"] = None

    # --- 预热（丢弃）：让字体/背板缓存/许可 worker 的收尾工作先跑完 -------
    window._sync_watermark(True)
    _pump(app, args.warmup * 0.6)
    window._sync_watermark(False)
    _pump(app, args.warmup * 0.2)
    window._sync_watermark(True)
    _pump(app, args.warmup * 0.2)
    result["warmup_paints"] = CountingWatermark.paint_count
    result["warmup_ticks"] = CountingWatermark.tick_count

    # --- 隔离变量：暂停应用自身的周期定时器 -----------------------------
    # 应用空闲时本来就有 16ms 帧轮询等常驻负载（实测 3%~6%，且会随时间漂移），
    # 其幅度比水印本身还大。不隔离就无法把水印的增量成本从噪声里分出来。
    # 这里只停"应用自身"的定时器，水印的计时器不动。
    if args.pause_timers:
        paused = []
        for name in ("_frame_timer", "_status_timer", "_access_timer", "_update_timer",
                     "_heartbeat_timer", "_copy_feedback_timer", "_clipboard_chip_timer",
                     "_title_hide_timer", "_dataset_timer"):
            timer = getattr(window, name, None)
            try:
                if timer is not None and timer.isActive():
                    paused.append({"name": name, "interval_ms": timer.interval()})
                    timer.stop()
            except RuntimeError:
                continue
        result["paused_timers"] = paused
        _pump(app, 0.5)

    proc = psutil.Process()
    proc.cpu_percent(None)  # 初始化采样基准

    def _restore_window() -> None:
        """窗口被最小化时 Qt 不派发 paintEvent（实测），必须恢复后再测。"""
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
        _pump(app, 0.4)

    def _row_is_valid(row: dict) -> bool:
        if row["minimized"] or not row["exposed"]:
            return False
        if row["state"] == "off":
            return row["paints"] == 0 and row["ticks"] == 0
        return row["paints"] > 0 and row["paints"] >= 0.6 * row["ticks"]

    def _measure(state: str, round_no: int, attempt: int) -> dict:
        window._sync_watermark(state == "on")
        _pump(app, args.settle)
        if window.isMinimized():
            _restore_window()

        t0 = time.perf_counter()
        c0 = _cpu_times_s(proc)
        p0 = CountingWatermark.paint_count
        k0 = CountingWatermark.tick_count
        su0 = CountingWatermark.step_us
        pu0 = CountingWatermark.paint_us
        rss0 = proc.memory_info().rss

        samples: list[float] = []
        end = t0 + args.window
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(0.05)
            samples.append(proc.cpu_percent(None))

        c1 = _cpu_times_s(proc)
        wall = time.perf_counter() - t0
        rss1 = proc.memory_info().rss

        return {
            "round": round_no,
            "attempt": attempt,
            "state": state,
            "wall_s": round(wall, 3),
            "cpu_time_ms": round((c1 - c0) * 1000.0, 2),
            "cpu_mean_pct": round((c1 - c0) / wall * 100.0, 3),
            "cpu_samples_mean_pct": round(statistics.fmean(samples), 3) if samples else None,
            "cpu_samples_max_pct": round(max(samples), 2) if samples else None,
            "paints": CountingWatermark.paint_count - p0,
            "ticks": CountingWatermark.tick_count - k0,
            "step_us_total": round(CountingWatermark.step_us - su0, 1),
            "paint_us_total": round(CountingWatermark.paint_us - pu0, 1),
            "step_us_per_tick": (round((CountingWatermark.step_us - su0) / max(1, CountingWatermark.tick_count - k0), 1)
                                 if CountingWatermark.tick_count - k0 else None),
            "paint_us_per_paint": (round((CountingWatermark.paint_us - pu0) / max(1, CountingWatermark.paint_count - p0), 1)
                                   if CountingWatermark.paint_count - p0 else None),
            "expected_ticks": round(wall * 1000.0 / (getattr(overlay, "TICK_MS", 40) or 40)),
            "rss_mb": round(rss1 / (1024 * 1024), 1),
            "rss_delta_kb": round((rss1 - rss0) / 1024.0, 1),
            "minimized": window.isMinimized(),
            "exposed": bool(window.windowHandle().isExposed()) if window.windowHandle() else None,
        }

    rounds: list[dict] = []
    discarded: list[dict] = []
    for i in range(args.rounds):
        # 奇偶轮交换顺序：抵消"上一状态残留"与单向漂移
        order = ("off", "on") if i % 2 == 0 else ("on", "off")
        for state in order:
            row = None
            for attempt in range(1, 4):
                candidate = _measure(state, i + 1, attempt)
                if _row_is_valid(candidate):
                    row = candidate
                    break
                discarded.append(candidate)
                _restore_window()
            if row is None:
                row = candidate  # 三次都不合格：保留最后一条并如实标记
                row["invalid"] = True
            rounds.append(row)

    result["rounds_data"] = rounds
    result["discarded_rounds"] = discarded

    off = [r for r in rounds if r["state"] == "off"]
    on = [r for r in rounds if r["state"] == "on"]

    def med(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(statistics.median(vals), 3) if vals else None

    summary = {
        "off_cpu_mean_pct_median": med(off, "cpu_mean_pct"),
        "on_cpu_mean_pct_median": med(on, "cpu_mean_pct"),
        "off_cpu_samples_median": med(off, "cpu_samples_mean_pct"),
        "on_cpu_samples_median": med(on, "cpu_samples_mean_pct"),
        "off_paints_total": sum(r["paints"] for r in off),
        "on_paints_total": sum(r["paints"] for r in on),
        "off_ticks_total": sum(r["ticks"] for r in off),
        "on_ticks_total": sum(r["ticks"] for r in on),
        "on_expected_ticks_total": sum(r["expected_ticks"] for r in on),
        "off_cpu_time_ms_total": round(sum(r["cpu_time_ms"] for r in off), 2),
        "on_cpu_time_ms_total": round(sum(r["cpu_time_ms"] for r in on), 2),
    }
    if summary["off_cpu_mean_pct_median"] is not None and summary["on_cpu_mean_pct_median"] is not None:
        summary["delta_pp"] = round(
            summary["on_cpu_mean_pct_median"] - summary["off_cpu_mean_pct_median"], 3)
    # 单帧成本 = 增量 CPU 时间 / 实际帧数（µs）
    if summary["on_paints_total"] > 0:
        summary["per_paint_us"] = round(
            (summary["on_cpu_time_ms_total"] - summary["off_cpu_time_ms_total"])
            / summary["on_paints_total"] * 1000.0, 1)

    # --- 有效性校验：OFF 必须一帧不画；ON 必须真的在画 -------------------
    checks = {
        "off_paints_zero": summary["off_paints_total"] == 0,
        "off_ticks_zero": summary["off_ticks_total"] == 0,
        "on_paints_positive": summary["on_paints_total"] > 0,
        "on_paints_per_tick_ge_0_8": (
            summary["on_ticks_total"] > 0
            and summary["on_paints_total"] >= 0.8 * summary["on_ticks_total"]
        ),
        "on_ticks_near_expected": (
            summary["on_expected_ticks_total"] > 0
            and summary["on_ticks_total"] >= 0.5 * summary["on_expected_ticks_total"]
        ),
        "all_rounds_exposed": all(
            bool(r["exposed"]) and not r["minimized"] for r in rounds),
        "no_invalid_rounds": not any(r.get("invalid") for r in rounds),
    }
    result["checks"] = checks
    result["valid"] = all(checks.values())
    result["summary"] = summary
    result["ok"] = True

    window.close()
    _pump(app, 0.3)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)) or ".", exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
