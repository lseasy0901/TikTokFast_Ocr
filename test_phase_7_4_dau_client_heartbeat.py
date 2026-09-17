# -*- coding: utf-8 -*-
"""
Phase 7.4 — 客户端登录心跳（DAU）单元验证。

背景
----
DAU 需要客户端在启动后上报一次「本机启动过」。这条链路必须与授权流程
严格隔离：心跳失败（网络不可达、超时、5xx）绝不能影响用户的授权状态。

本测试钉住三个契约：

    1. 上报内容与目标地址正确，且设备标识取自本地已保存的许可证
       （不再调用 susi_helper —— 零额外开销）。
    2. 未激活 / 记录缺 device_id 时**完全不发请求**。
    3. 任何失败都被吞掉：LicenseManager.heartbeat() 与
       LicenseServerClient.heartbeat() 都永不抛异常。

范围
----
只测 utils 层，不启动 GUI（这也是把 heartbeat() 放在 LicenseManager 而不是
MainWindow 的原因）。不访问网络：requests.post 被替换为记录用的假实现。
不触碰真实的 %APPDATA%。

运行：python test_phase_7_4_dau_client_heartbeat.py
"""

import datetime
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requests  # noqa: E402

from utils.license_client import LicenseServerClient, LicenseServerError  # noqa: E402
from utils.license_manager import LicenseManager  # noqa: E402
from utils.license_store import LicenseStore  # noqa: E402

BASE_URL = "https://heartbeat-test.invalid/api/v1"
DEVICE_ID = "0123456789abcdef" * 4  # 真实机器码恰为 64 位十六进制

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


#: 假服务端返回的记账业务日。刻意取一个固定的过去日期，与运行测试的当天无关，
#: 这样"客户端是否照抄服务端日期"就能被确定性地区分出来。
SERVER_DAY = datetime.date(2026, 9, 17)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = (
            payload
            if payload is not None
            else {"ok": True, "active_date": SERVER_DAY.isoformat()}
        )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _PostRecorder:
    """替换 requests.post：记录调用，按脚本返回响应或抛异常。"""

    def __init__(self):
        self.calls = []
        self.raise_exc = None
        self.status_code = 200
        self.payload = None          # None -> 默认的 {"ok":True,"active_date":SERVER_DAY}

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResponse(self.status_code, self.payload)


def _make_store(tmpdir):
    return LicenseStore(data_dir=str(tmpdir))


def _write_license(store, **overrides):
    """写入一份最小可用的 license.json。

    LicenseStore.load() 只要求 signed_license 为真值，device_id 是可选的 ——
    这正是「缺失 device_id」用例能构造出来的原因。
    """
    record = {
        "version": 1,
        "signed_license": '{"license_data": "{}", "signature": "x"}',
        "device_id": DEVICE_ID,
        "server_url": BASE_URL,
        "activated_at": "2026-09-17T04:30:00+00:00",
    }
    record.update(overrides)
    store.path = str(pathlib.Path(store.data_dir) / "license.json")
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(record, f)
    return record


def main():
    global PASSED, FAILED

    print("Phase 7.4 — client heartbeat (DAU)", flush=True)
    print("-" * 64, flush=True)

    original_post = requests.post
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="dlv-dau-client-"))

    try:
        # ------------------------------------------------------------------
        # 1) 正常路径：有 device_id → 发出正确请求
        # ------------------------------------------------------------------
        print("\n[上报内容]", flush=True)
        tmp = tmp_root / "ok"
        tmp.mkdir()
        store = _make_store(tmp)
        _write_license(store)

        rec = _PostRecorder()
        requests.post = rec
        manager = LicenseManager(server_url=BASE_URL, store=store)
        result = manager.heartbeat()

        check(
            result == SERVER_DAY,
            f"上报成功返回服务端记账的业务日 {SERVER_DAY}（而非布尔值）",
        )
        check(len(rec.calls) == 1, "恰好发出 1 次请求")
        if rec.calls:
            call = rec.calls[0]
            check(
                call["url"] == BASE_URL + "/licenses/heartbeat",
                "上报到 %s" % (BASE_URL + "/licenses/heartbeat"),
            )
            check(
                call["json"] == {"device_id": DEVICE_ID},
                "请求体只含 device_id，不含任何时间/IP/密钥字段",
            )
            check(call["timeout"] == 5, "超时为独立的 5s（不沿用 activate 的 15s）")

        # ------------------------------------------------------------------
        # 2) 未激活：完全没有本地记录 → 不发请求
        # ------------------------------------------------------------------
        print("\n[未激活不上报]", flush=True)
        tmp = tmp_root / "empty"
        tmp.mkdir()
        store = _make_store(tmp)
        store.clear()  # 确保没有残留

        rec = _PostRecorder()
        requests.post = rec
        manager = LicenseManager(server_url=BASE_URL, store=store)
        check(manager.heartbeat() is None, "无 license.json 时返回 None")
        check(rec.calls == [], "无 license.json 时不发任何请求")

        # ------------------------------------------------------------------
        # 3) 记录存在但缺 device_id → 不发请求，且不抛异常
        # ------------------------------------------------------------------
        tmp = tmp_root / "nodevice"
        tmp.mkdir()
        store = _make_store(tmp)
        _write_license(store, device_id=None)

        rec = _PostRecorder()
        requests.post = rec
        manager = LicenseManager(server_url=BASE_URL, store=store)
        raised = None
        returned = None
        try:
            returned = manager.heartbeat()
        except Exception as e:  # noqa: BLE001 - 正是要证明不会发生
            raised = e
        check(raised is None, "缺 device_id 时不抛异常")
        check(returned is None, "缺 device_id 时返回 None")
        check(rec.calls == [], "缺 device_id 时不发请求")

        # ------------------------------------------------------------------
        # 4) 各种失败都必须被吞掉
        # ------------------------------------------------------------------
        print("\n[失败静默]", flush=True)
        tmp = tmp_root / "fail"
        tmp.mkdir()
        store = _make_store(tmp)
        _write_license(store)
        manager = LicenseManager(server_url=BASE_URL, store=store)

        failures = {
            "连接失败": requests.exceptions.ConnectionError("boom"),
            "超时": requests.exceptions.Timeout("slow"),
            "其它请求异常": requests.exceptions.RequestException("odd"),
        }
        for label, exc in failures.items():
            rec = _PostRecorder()
            rec.raise_exc = exc
            requests.post = rec
            raised = None
            returned = None
            try:
                returned = manager.heartbeat()
            except Exception as e:  # noqa: BLE001
                raised = e
            check(raised is None, "%s 被吞掉，不冒泡" % label)
            check(returned is None, "%s 时返回 None（调度器据此重试）" % label)
            check(len(rec.calls) == 1, "%s 时确实尝试过上报" % label)

        for status in (400, 404, 422, 500, 503):
            rec = _PostRecorder()
            rec.status_code = status
            requests.post = rec
            raised = None
            returned = None
            try:
                returned = manager.heartbeat()
            except Exception as e:  # noqa: BLE001
                raised = e
            check(raised is None, "HTTP %d 被吞掉，不冒泡" % status)
            check(returned is None, "HTTP %d 返回 None" % status)

        # 读盘失败（data_dir 指向一个文件而非目录）
        bad_path = tmp_root / "not-a-dir"
        bad_path.write_text("x", encoding="utf-8")
        rec = _PostRecorder()
        requests.post = rec
        manager = LicenseManager(
            server_url=BASE_URL, store=LicenseStore(data_dir=str(bad_path))
        )
        raised = None
        try:
            manager.heartbeat()
        except Exception as e:  # noqa: BLE001
            raised = e
        check(raised is None, "本地存储不可读时也不抛异常")

        # ------------------------------------------------------------------
        # 5) 客户端侧参数校验：非法 device_id 直接跳过
        # ------------------------------------------------------------------
        print("\n[客户端保护]", flush=True)
        rec = _PostRecorder()
        requests.post = rec
        client = LicenseServerClient(base_url=BASE_URL)
        rejected = [
            client.heartbeat(""),
            client.heartbeat("   "),
            client.heartbeat(None),
            client.heartbeat("f" * 65),
        ]
        check(rec.calls == [], "空/超长 device_id 一律不发请求，也不需要调用方 try")
        check(
            all(r is None for r in rejected),
            "非法标识一律返回 None（而不是抛异常）",
        )

        check(
            client.heartbeat(DEVICE_ID) == SERVER_DAY,
            "合法 device_id 返回服务端记账的业务日",
        )
        check(len(rec.calls) == 1, "合法 device_id 正常上报")

        # ------------------------------------------------------------------
        # 5b) 200 但响应不可用时 -> 一律 None（保持"未上报"，让 watchdog 重试）
        # ------------------------------------------------------------------
        print("\n[响应解析兜底]", flush=True)
        bad_payloads = [
            ({}, "缺少 active_date"),
            ({"ok": True}, "只有 ok"),
            ({"ok": True, "active_date": None}, "active_date 为 None"),
            ({"ok": True, "active_date": ""}, "active_date 为空串"),
            ({"ok": True, "active_date": "not-a-date"}, "active_date 非法"),
            ({"ok": True, "active_date": 20260917}, "active_date 是数字"),
            ([], "顶层不是对象"),
            (ValueError("bad json"), "响应不是合法 JSON"),
        ]
        for payload, label in bad_payloads:
            rec = _PostRecorder()
            rec.payload = payload
            requests.post = rec
            got = client.heartbeat(DEVICE_ID)
            check(
                got is None,
                f"{label} -> None（宁可重试，也不凭空相信一个本地推断的日期）",
            )

        # 合法的 active_date 必须被原样接受
        rec = _PostRecorder()
        rec.payload = {"ok": True, "active_date": "2026-12-31"}
        requests.post = rec
        check(
            client.heartbeat(DEVICE_ID) == datetime.date(2026, 12, 31),
            "合法的 active_date 被原样解析为 date",
        )

        # ------------------------------------------------------------------
        # 6) 轻量回归：activate() 的行为未被本次改动影响
        # ------------------------------------------------------------------
        print("\n[授权链路回归]", flush=True)
        raised = None
        try:
            LicenseServerClient(base_url=BASE_URL).activate("short", DEVICE_ID)
        except LicenseServerError as e:
            raised = e
        check(
            raised is not None and raised.reason == "invalid_key",
            "activate() 的密钥长度校验仍然生效（未被心跳改动影响）",
        )
        check(
            hasattr(LicenseServerClient, "activate")
            and hasattr(LicenseServerClient, "heartbeat"),
            "activate 与 heartbeat 是两个独立方法",
        )

    finally:
        requests.post = original_post

    print("\n" + "-" * 64, flush=True)
    print("PASSED=%d  FAILED=%d" % (PASSED, FAILED), flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
