# -*- coding: utf-8 -*-
"""
Phase 7.4 — MainWindow 心跳接线（启动上报 / 构造无副作用 / 失败隔离）。

被钉住的契约
------------
    1. 构造 MainWindow **不产生任何心跳请求**（否则每次跑 GUI 测试都会
       向生产服务器发一次，污染真实 DAU）
    2. start_heartbeat() 立即上报一次
    3. 定时器是**单次触发**，且延时不沿用固定周期
    4. 上报成功 -> 记入业务日 -> 定时器重新对齐
    5. 上报失败 -> 不记入业务日 -> 保持"需要上报"（会重试）
    6. LicenseWorker 的兜底 LicenseResult 不能被误判成"上报成功"
    7. 心跳无论如何失败，都不改变客户端的授权状态显示

范围
----
只测接线，不重复测调度算法（那在 test_phase_7_4_dau_heartbeat_schedule.py）
与上报契约（在 test_phase_7_4_dau_client_heartbeat.py）。
不访问网络：requests.post 被替换。不触碰真实 %APPDATA%：LicenseManager
被换成一个指向临时目录的实例。

以 offscreen 平台运行，无需显示器：
    QT_QPA_PLATFORM=offscreen python test_phase_7_4_dau_heartbeat_wiring.py
"""

import datetime
import json
import os
import pathlib
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requests  # noqa: E402

from utils.license_manager import (  # noqa: E402
    LicenseManager,
    LicenseResult,
    LicenseStatus,
)
from utils.license_store import LicenseStore  # noqa: E402

DEVICE_ID = "0123456789abcdef" * 4

PASSED = 0
FAILED = 0


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


#: 一个与"运行测试的当天"无关的固定业务日，用于确定性地验证
#: "客户端只认服务端返回的 active_date"。
FAR_SERVER_DAY = datetime.date(2026, 1, 2)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _PostRecorder:
    def __init__(self):
        self.calls = []
        self.raise_exc = None
        self.status_code = 200
        self.payload = None          # 由调用方设为服务端应答体

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResponse(self.status_code, self.payload)


def _hermetic_manager(tmpdir):
    """一个指向临时目录的 LicenseManager，避免读到开发机的真实 license.json。"""
    tmpdir = pathlib.Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    store = LicenseStore(data_dir=str(tmpdir))
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": 1,
                "signed_license": '{"license_data": "{}", "signature": "x"}',
                "device_id": DEVICE_ID,
                "server_url": "https://wiring-test.invalid/api/v1",
                "activated_at": "2026-09-17T04:30:00+00:00",
            },
            f,
        )
    # 显式指向一个不存在的测试地址：即使 requests.post 的替换将来失效，
    # 也绝不会真的打到生产许可证服务器。
    return LicenseManager(store=store, server_url="https://wiring-test.invalid/api/v1")


def _drain(app, seconds=1.0):
    """让 Qt 事件循环跑一会儿（把后台线程的结果信号送进槽）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _wait_worker(app, window, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        worker = window._heartbeat_worker
        if worker is not None and not worker.is_running():
            return True
        time.sleep(0.02)
    return False


def main():
    global PASSED, FAILED

    print("Phase 7.4 — MainWindow heartbeat wiring", flush=True)
    print("-" * 64, flush=True)

    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="dlv-dau-wiring-"))
    original_post = requests.post

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from gui.main_window import MainWindow

    try:
        # ------------------------------------------------------------------
        # 1) 构造 MainWindow 不产生心跳
        # ------------------------------------------------------------------
        print("\n[构造无副作用]", flush=True)
        rec = _PostRecorder()
        requests.post = rec

        window = MainWindow()
        _drain(app, 1.5)          # 给启动期的后台线程充分机会去发请求

        check(
            rec.calls == [],
            f"构造 MainWindow 未发起任何网络请求（实际 {len(rec.calls)} 次）",
        )

        # 等启动期的许可证加载结束，后续断言才稳定
        deadline = time.time() + 15
        while time.time() < deadline:
            app.processEvents()
            worker = window._license_load_worker
            if worker is None or not worker.is_running():
                break
            time.sleep(0.05)

        # 换成指向临时目录的管理器 + 确认定时器尚未启动
        window._license_manager = _hermetic_manager(tmp_root / "mgr")
        check(
            not window._heartbeat_timer.isActive(),
            "构造后心跳定时器未启动",
        )
        check(
            window._heartbeat_scheduler.reported_day is None,
            "构造后尚未记录任何业务日",
        )

        # ------------------------------------------------------------------
        # 2) start_heartbeat() 立即上报一次
        # ------------------------------------------------------------------
        print("\n[启动立即上报]", flush=True)
        local_day = window._heartbeat_scheduler.current_day(
            datetime.datetime.now(datetime.timezone.utc)
        )
        rec = _PostRecorder()
        rec.payload = {"ok": True, "active_date": local_day.isoformat()}
        requests.post = rec

        window.start_heartbeat()
        finished = _wait_worker(app, window)
        _drain(app, 0.5)

        check(finished, "心跳 worker 正常结束")
        check(len(rec.calls) == 1, f"start_heartbeat() 立即上报 1 次（实际 {len(rec.calls)} 次）")
        if rec.calls:
            check(
                rec.calls[0]["url"].endswith("/licenses/heartbeat"),
                f"上报到 heartbeat 端点：{rec.calls[0]['url']}",
            )
            check(
                rec.calls[0]["json"] == {"device_id": DEVICE_ID},
                "请求体只含 device_id",
            )

        # ------------------------------------------------------------------
        # 3) 上报成功 -> 记入业务日 + 定时器重新对齐
        # ------------------------------------------------------------------
        print("\n[成功后的调度状态]", flush=True)
        check(
            window._heartbeat_scheduler.reported_day == local_day,
            f"记入服务端返回的业务日 {window._heartbeat_scheduler.reported_day}"
            f"（本地业务日 {local_day}）",
        )
        check(window._heartbeat_timer.isActive(), "定时器已重新装上")
        check(
            window._heartbeat_timer.isSingleShot(),
            "定时器为单次触发（每次到点后重新计算，不累积漂移）",
        )
        remaining = window._heartbeat_timer.remainingTime()
        check(
            0 < remaining <= 15 * 60 * 1000 + 1000,
            f"延时被 watchdog 封顶，不超过 15 分钟（实际 {remaining}ms）",
        )
        check(
            not window._heartbeat_scheduler.should_report(
                datetime.datetime.now(datetime.timezone.utc)
            ),
            "当天已上报 -> 不再判定为需要上报",
        )

        # ------------------------------------------------------------------
        # 4) 上报失败 -> 不记入业务日 -> 保持需要上报（会重试）
        # ------------------------------------------------------------------
        print("\n[失败隔离与重试]", flush=True)
        tmp2 = tmp_root / "mgr2"
        window._license_manager = _hermetic_manager(tmp2)

        rec = _PostRecorder()
        rec.raise_exc = requests.exceptions.ConnectionError("boom")
        requests.post = rec

        window._heartbeat_scheduler.mark_reported_day(None)
        window._start_heartbeat()
        _wait_worker(app, window)
        _drain(app, 0.5)

        check(len(rec.calls) == 1, "失败时确实尝试过上报")
        check(
            window._heartbeat_scheduler.reported_day is None,
            "上报失败 -> 不记入业务日（当天会继续重试）",
        )
        check(
            window._heartbeat_scheduler.should_report(
                datetime.datetime.now(datetime.timezone.utc)
            ),
            "上报失败 -> 仍判定为需要上报",
        )
        check(window._heartbeat_timer.isActive(), "失败后定时器仍被重新装上")

        # ------------------------------------------------------------------
        # 5) 失败不影响客户端授权状态
        # ------------------------------------------------------------------
        print("\n[授权状态不受影响]", flush=True)
        state_before = window._access_status.get_state()
        text_before = window._access_status_label.text()
        enabled_before = window._connect_btn.isEnabled()

        rec = _PostRecorder()
        rec.status_code = 500
        requests.post = rec
        window._start_heartbeat()
        _wait_worker(app, window)
        _drain(app, 0.5)

        check(
            window._access_status.get_state() is state_before,
            "HTTP 500 后授权状态未变",
        )
        check(
            window._access_status_label.text() == text_before,
            "HTTP 500 后顶部状态文案未变",
        )
        check(
            window._connect_btn.isEnabled() == enabled_before,
            "HTTP 500 后控件可用性未变",
        )

        issued = None
        try:
            window._on_heartbeat_done(
                LicenseResult(LicenseStatus.ACTIVATION_FAILED, "兜底")
            )
        except Exception as exc:  # noqa: BLE001 - 正是要证明不会发生
            issued = exc
        check(issued is None, "兜底 LicenseResult 不会让心跳回调抛异常")
        check(
            window._access_status.get_state() is state_before,
            "兜底 LicenseResult 也未改变授权状态",
        )

        # ------------------------------------------------------------------
        # 6) 兜底对象不能被误判为"上报成功"
        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        # 7) 服务端说是哪天就记哪天 —— 即使客户端此刻已经是另一天
        #    （这就是 F1：请求贴着业务日边界发出、响应跨过边界。
        #     旧实现用本地 datetime.now() 推断，会把下一天误记为已上报，
        #     导致那一天永远不会被上报。现在必须以服务端 active_date 为准。）
        # ------------------------------------------------------------------
        print("\n[记账权威在服务端]", flush=True)
        now_day = window._heartbeat_scheduler.current_day(
            datetime.datetime.now(datetime.timezone.utc)
        )
        check(
            FAR_SERVER_DAY != now_day,
            f"测试构造的服务端业务日 {FAR_SERVER_DAY} 与本地此刻 {now_day} 不同",
        )

        window._heartbeat_scheduler.mark_reported_day(None)
        rec = _PostRecorder()
        rec.payload = {"ok": True, "active_date": FAR_SERVER_DAY.isoformat()}
        requests.post = rec
        window._start_heartbeat()
        _wait_worker(app, window)
        _drain(app, 0.5)

        check(len(rec.calls) == 1, "确实发起了一次上报")
        check(
            window._heartbeat_scheduler.reported_day == FAR_SERVER_DAY,
            f"_reported_day == 服务端的 {FAR_SERVER_DAY}"
            f"（实际 {window._heartbeat_scheduler.reported_day}）",
        )
        check(
            window._heartbeat_scheduler.reported_day != now_day,
            f"没有被本地此刻的 {now_day} 覆盖（这正是 F1 的失效模式）",
        )

        # 服务端返回不可用时，宁可保持"未上报"去重试，也不猜一个本地日期
        window._heartbeat_scheduler.mark_reported_day(None)
        rec = _PostRecorder()
        rec.payload = {"ok": True}                 # 缺少 active_date
        requests.post = rec
        window._start_heartbeat()
        _wait_worker(app, window)
        _drain(app, 0.5)
        check(
            window._heartbeat_scheduler.reported_day is None,
            "响应缺 active_date -> 不记账（保持重试，而不是退回本地日期）",
        )

        # ------------------------------------------------------------------
        # 8) 正常跨 UTC+8 午夜：服务端返回新业务日，客户端记入新的一天
        # ------------------------------------------------------------------
        print("\n[跨午夜成功上报]", flush=True)
        today = window._heartbeat_scheduler.current_day(
            datetime.datetime.now(datetime.timezone.utc)
        )
        yesterday = today - datetime.timedelta(days=1)

        window._heartbeat_scheduler.mark_reported_day(yesterday)
        check(
            window._heartbeat_scheduler.should_report(
                datetime.datetime.now(datetime.timezone.utc)
            ),
            f"上一业务日 {yesterday} 已记账 -> 跨天后判定为需要上报",
        )

        rec = _PostRecorder()
        rec.payload = {"ok": True, "active_date": today.isoformat()}
        requests.post = rec
        window._on_heartbeat_timer_timeout()       # 走真实的定时器到点路径
        _wait_worker(app, window)
        _drain(app, 0.5)

        check(len(rec.calls) == 1, "定时器到点发起了一次上报")
        check(
            window._heartbeat_scheduler.reported_day == today,
            f"记入服务端返回的新业务日 {today}",
        )
        check(
            not window._heartbeat_scheduler.should_report(
                datetime.datetime.now(datetime.timezone.utc)
            ),
            "新业务日当天不再重复上报",
        )
        check(window._heartbeat_timer.isActive(), "定时器已按新边界重新装上")

        # ------------------------------------------------------------------
        print("\n[非日期一律不记账]", flush=True)
        check(
            bool(LicenseResult(LicenseStatus.ACTIVATION_FAILED, "兜底")) is True,
            "LicenseResult 是真值（因此不能用真值判断）",
        )

        non_days = [
            (LicenseResult(LicenseStatus.ACTIVATION_FAILED, "兜底"),
             "LicenseWorker 的兜底 LicenseResult"),
            (None, "None（上报失败/未激活）"),
            (False, "布尔 False"),
            ("2026-09-17", "字符串（未解析）"),
            (datetime.datetime(2026, 9, 17, 12, 0), "datetime 而非 date"),
        ]
        for value, label in non_days:
            window._heartbeat_scheduler.mark_reported_day(None)
            window._on_heartbeat_done(value)
            check(
                window._heartbeat_scheduler.reported_day is None,
                f"{label} 不会被记成已上报的业务日",
            )

    finally:
        requests.post = original_post
        try:
            window.close()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "-" * 64, flush=True)
    print("PASSED=%d  FAILED=%d" % (PASSED, FAILED), flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
