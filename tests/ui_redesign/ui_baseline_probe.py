# -*- coding: utf-8 -*-
"""UI 改造基线探针（Phase 3 / Phase 7 复用）。

用途：在**真实运行**的程序上采集可比较的基线数据，并抓取真实截图。

设计要点：
    - 只读：不修改任何业务状态，不触发网络（心跳/更新检查由 main.py 调用，
      本脚本刻意不调用它们）。
    - 确定性：固定等待时长、固定采样窗口，输出 JSON 便于前后对比。
    - 可复用：改造后用同样命令跑一次，即可得到可比数据。

用法（在 worktree 根目录下）：
    <venv-python> tests/ui_redesign/ui_baseline_probe.py \
        --screenshot <out.png> --json <out.json> [--settle 4] [--cpu-window 3]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 保证以任意 cwd 启动都能导入项目包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _rss_mb() -> float:
    import psutil
    return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--settle", type=float, default=4.0,
                        help="窗口显示后等待许可状态落地的秒数")
    parser.add_argument("--cpu-window", type=float, default=3.0,
                        help="空闲 CPU 采样窗口秒数")
    args = parser.parse_args()

    result: dict = {"ok": False}

    t_import0 = time.perf_counter()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    import gui.theme as theme

    theme.configure_high_dpi()
    app = QApplication(sys.argv)
    app.setApplicationName("Douyin Low Latency Stream Viewer")
    result["import_ms"] = round((time.perf_counter() - t_import0) * 1000, 1)

    from gui.main_window import MainWindow

    t_build0 = time.perf_counter()
    window = MainWindow()
    result["window_construct_ms"] = round((time.perf_counter() - t_build0) * 1000, 1)

    t_show0 = time.perf_counter()
    window.show()
    # 处理事件直到首帧绘制完成
    for _ in range(50):
        app.processEvents()
        time.sleep(0.01)
    result["first_paint_ms"] = round((time.perf_counter() - t_show0) * 1000, 1)

    # --- 等待许可状态落地（离线本地验证，异步） ---
    t_lic0 = time.perf_counter()
    deadline = t_lic0 + args.settle
    status = None
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.05)
        status = getattr(window, "_access_status", None)
        if status is not None and status.get_state() is not None:
            break
    result["license_settle_ms"] = round((time.perf_counter() - t_lic0) * 1000, 1)

    if status is not None:
        result["license_state"] = str(status.get_state())
        result["license_status_text"] = status.get_status_text()
        result["license_is_expired"] = bool(status.is_expired())

    result["rss_after_show_mb"] = _rss_mb()

    # --- 空闲 CPU 采样 ---
    import psutil
    proc = psutil.Process()
    proc.cpu_percent(None)  # 重置基准
    cpu_samples = []
    t_cpu0 = time.perf_counter()
    while time.perf_counter() - t_cpu0 < args.cpu_window:
        app.processEvents()
        time.sleep(0.1)
        cpu_samples.append(proc.cpu_percent(None))
    result["idle_cpu_samples"] = cpu_samples
    result["idle_cpu_mean_pct"] = round(sum(cpu_samples) / len(cpu_samples), 2) if cpu_samples else None
    result["idle_cpu_max_pct"] = round(max(cpu_samples), 2) if cpu_samples else None
    result["cpu_window_s"] = args.cpu_window
    result["rss_idle_mb"] = _rss_mb()

    # --- 截图（真实运行窗口） ---
    if args.screenshot:
        os.makedirs(os.path.dirname(os.path.abspath(args.screenshot)) or ".", exist_ok=True)
        pix = window.grab()
        saved = pix.save(args.screenshot, "PNG")
        result["screenshot"] = os.path.abspath(args.screenshot)
        result["screenshot_saved"] = bool(saved)
        result["screenshot_size"] = [pix.width(), pix.height()]

    # --- 定时器清点（Phase 5 会用到，先建立基线） ---
    try:
        timers = window.findChildren(QTimer)
        result["qtimer_count"] = len(timers)
        result["qtimer_active"] = sum(1 for t in timers if t.isActive())
    except Exception as exc:  # noqa: BLE001
        result["qtimer_error"] = str(exc)

    result["window_size"] = [window.width(), window.height()]
    result["ok"] = True

    # --- 干净退出：验证无残留 ---
    window.close()
    app.processEvents()
    QTimer.singleShot(200, app.quit)
    app.exec()
    result["exited_cleanly"] = True

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)) or ".", exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
