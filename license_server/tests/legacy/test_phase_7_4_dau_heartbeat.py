# -*- coding: utf-8 -*-
"""
Phase 7.4 — DAU 心跳端点与 device_daily_active 表。

背景
----
DAU 要求「同一设备同一业务日只算一次」。这个去重契约只有数据库能证明，
因此本测试同时做 HTTP 级断言和**直接查库**断言（沿用 ServerFixture.sql()）。

本测试钉住的契约：

    1. 心跳写入 device_daily_active，同一设备同一日**恒为 1 行**，
       launch_count 递增，first_seen_at 保持首次值。
    2. active_date 由服务器按业务时区（UTC+8）派生，跨日边界正确。
    3. 心跳**不依赖授权状态** —— 没有 Authorization 的设备同样 200。
    4. 参数校验：device_id 缺失/空白/超长一律 422（FastAPI 层拦截）。
    5. 表上确实存在 (device_id, active_date) 复合唯一索引。

运行：python test_phase_7_4_dau_heartbeat.py
（需在 license_server/ 目录下执行）
"""

import datetime
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent
SERVER_APP_DIR = HERE / "app"
API_PREFIX = "/api/v1"

# 必须在导入任何 app 模块之前设置：database.py 在 import 时就会读它建 engine。
# 用临时目录，避免碰到开发库。
TMPDIR = pathlib.Path(tempfile.mkdtemp(prefix="dlv-dau-test-"))
os.environ["DATABASE_URL"] = "sqlite:///" + (TMPDIR / "parent.db").as_posix()

sys.path.insert(0, str(SERVER_APP_DIR))

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(cond, msg):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  [ok]   " + msg, flush=True)
    else:
        FAILED += 1
        print("  [FAIL] " + msg, flush=True)


def skip(msg):
    global SKIPPED
    SKIPPED += 1
    print("  [skip] " + msg, flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerFixture:
    """Runs the real license server (uvicorn) against a temporary database."""

    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="dlv_dau_srv_")
        self.db_path = os.path.join(self.tmp_dir, "license.db").replace("\\", "/")
        self.log_path = os.path.join(self.tmp_dir, "server.log")
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}{API_PREFIX}"
        self.proc = None
        self._log = None

    def start(self):
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{self.db_path}",
                "ADMIN_API_KEY": "test-admin-key",
                "API_PREFIX": API_PREFIX,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "main:app",
                "--host", "127.0.0.1", "--port", str(self.port),
            ],
            cwd=str(SERVER_APP_DIR),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                if requests.get(
                    f"http://127.0.0.1:{self.port}/", timeout=1
                ).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.4)
        raise RuntimeError(f"license server did not start; see {self.log_path}")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log is not None:
            self._log.close()

    def cleanup(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def sql(self, statement, params=()):
        """直查数据库 —— 用于断言 API 不返回的服务端状态。"""
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(statement, params)
            rows = cur.fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def heartbeat(self, payload, expect=200):
        r = requests.post(
            f"{self.base_url}/licenses/heartbeat", json=payload, timeout=20
        )
        if r.status_code != expect:
            raise AssertionError(
                f"expected HTTP {expect}, got {r.status_code}: {r.text[:200]}"
            )
        return r

    def read_log(self) -> str:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""


DEVICE_A = "0123456789abcdef" * 4           # 真实机器码恰为 64 位十六进制
DEVICE_B = "fedcba9876543210" * 4


def test_bucket_date_unit():
    """active_date 的业务时区边界 —— 纯函数，固定输入，不依赖当前时间。"""
    print("\n[业务时区日历日（单元）]", flush=True)

    try:
        # database 必须先于 models 导入：models.py 顶部 `import database`，
        # 而 database.py 底部又 `from models import ...` —— 先导入 models 会
        # 撞上循环导入。应用的每个入口（main.py:11）都是先 import database，
        # 这里保持同样的顺序。
        import database  # noqa: F401
        from services.heartbeat_service import bucket_date
    except Exception as exc:  # noqa: BLE001
        skip(f"无法导入 heartbeat_service（{type(exc).__name__}: {exc}）")
        return

    utc = datetime.timezone.utc
    cases = [
        # (UTC 瞬时,                 期望的业务日,   说明)
        ((2026, 9, 17, 15, 59), (2026, 9, 17), "UTC+8 当天 23:59"),
        ((2026, 9, 17, 16, 0), (2026, 9, 18), "UTC+8 次日 00:00 —— 跨日边界"),
        ((2026, 9, 17, 20, 0), (2026, 9, 18), "UTC+8 次日 04:00"),
        ((2026, 9, 17, 0, 0), (2026, 9, 17), "UTC+8 当天 08:00"),
        ((2026, 9, 17, 23, 59), (2026, 9, 18), "UTC+8 次日 07:59"),
    ]
    for (y, mo, d, h, mi), (ey, emo, ed), label in cases:
        got = bucket_date(datetime.datetime(y, mo, d, h, mi, tzinfo=utc))
        check(
            got == datetime.date(ey, emo, ed),
            f"{label}: {y}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}Z -> {got}",
        )

    # naive 输入按 UTC 解释（与全仓库约定一致）
    naive = datetime.datetime(2026, 9, 17, 20, 0)
    check(
        bucket_date(naive) == datetime.date(2026, 9, 18),
        "naive 输入按 UTC 解释，得到同样的业务日",
    )


def main():
    global PASSED, FAILED, SKIPPED

    print("Phase 7.4 — DAU heartbeat endpoint", flush=True)
    print("-" * 64, flush=True)

    test_bucket_date_unit()

    server = ServerFixture()
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [skip] 服务端未能启动，跳过 HTTP 断言：{exc}", flush=True)
        SKIPPED += 1
        server.stop()
        server.cleanup()
        return 0

    try:
        # ------------------------------------------------------------------
        # 表结构：复合唯一约束必须真实存在（去重的唯一保证）
        # ------------------------------------------------------------------
        print("\n[表结构]", flush=True)
        cols = [r[1] for r in server.sql("PRAGMA table_info(device_daily_active)")]
        check(
            cols == ["id", "device_id", "active_date", "first_seen_at",
                     "last_seen_at", "launch_count"],
            f"device_daily_active 列符合预期：{cols}",
        )

        unique_indexes = [
            r for r in server.sql("PRAGMA index_list(device_daily_active)")
            if r[2] == 1
        ]
        check(len(unique_indexes) == 1, "存在且仅存在 1 个唯一索引")
        if unique_indexes:
            index_name = unique_indexes[0][1]
            indexed_cols = [
                r[2] for r in server.sql(f"PRAGMA index_info({index_name})")
            ]
            check(
                sorted(indexed_cols) == ["active_date", "device_id"],
                f"唯一索引覆盖 (device_id, active_date)：{indexed_cols}",
            )

        # ------------------------------------------------------------------
        # 首次心跳
        # ------------------------------------------------------------------
        print("\n[首次心跳]", flush=True)
        body = server.heartbeat({"device_id": DEVICE_A}).json()
        check(body.get("ok") is True, "响应 ok=True")
        check("active_date" in body, "响应带有 active_date")

        rows = server.sql(
            "SELECT device_id, launch_count FROM device_daily_active WHERE device_id=?",
            (DEVICE_A,),
        )
        check(len(rows) == 1, "首次心跳写入 1 行")
        check(rows and rows[0][1] == 1, "launch_count 初始为 1")

        # 响应里的 active_date 必须与库里存的一致
        stored_date = server.sql(
            "SELECT active_date FROM device_daily_active WHERE device_id=?",
            (DEVICE_A,),
        )[0][0]
        check(
            str(body["active_date"]) == stored_date,
            f"响应 active_date 与库中一致：{body['active_date']} == {stored_date}",
        )

        # ------------------------------------------------------------------
        # 核心契约：同设备同日重复心跳 -> 恒为 1 行
        # ------------------------------------------------------------------
        print("\n[去重契约]", flush=True)
        first_seen_before = server.sql(
            "SELECT first_seen_at FROM device_daily_active WHERE device_id=?",
            (DEVICE_A,),
        )[0][0]

        for _ in range(4):
            server.heartbeat({"device_id": DEVICE_A})

        count = server.sql(
            "SELECT COUNT(*) FROM device_daily_active WHERE device_id=?", (DEVICE_A,)
        )[0][0]
        check(count == 1, f"同设备同日上报 5 次后仍恒为 1 行（实际 {count}）")

        launch_count, first_seen_after, last_seen = server.sql(
            "SELECT launch_count, first_seen_at, last_seen_at FROM "
            "device_daily_active WHERE device_id=?"
        , (DEVICE_A,))[0]
        check(launch_count == 5, f"launch_count 累计为 5（实际 {launch_count}）")
        check(
            first_seen_after == first_seen_before,
            "first_seen_at 保持首次观测值，未被后续心跳改写",
        )
        check(
            last_seen >= first_seen_after,
            f"last_seen_at 不早于 first_seen_at（{last_seen} >= {first_seen_after}）",
        )

        # ------------------------------------------------------------------
        # 不同设备各自成行
        # ------------------------------------------------------------------
        print("\n[多设备]", flush=True)
        server.heartbeat({"device_id": DEVICE_B})
        total = server.sql("SELECT COUNT(*) FROM device_daily_active")[0][0]
        check(total == 2, f"两台不同设备共 2 行（实际 {total}）")

        distinct_devices = server.sql(
            "SELECT COUNT(DISTINCT device_id) FROM device_daily_active"
        )[0][0]
        check(distinct_devices == 2, "COUNT(DISTINCT device_id) 为 2")

        # ------------------------------------------------------------------
        # 心跳不依赖授权：该设备在 authorizations 里根本没有记录
        # ------------------------------------------------------------------
        print("\n[与授权解耦]", flush=True)
        auth_rows = server.sql(
            "SELECT COUNT(*) FROM authorizations WHERE device_id=?", (DEVICE_A,)
        )[0][0]
        check(auth_rows == 0, "该设备确实没有任何 Authorization 记录")
        check(
            total == 2,
            "无授权的设备同样被计入 DAU（心跳度量的是启动，不是可用性）",
        )

        # ------------------------------------------------------------------
        # 参数校验
        # ------------------------------------------------------------------
        print("\n[参数校验]", flush=True)
        invalid_payloads = (
            ({}, "缺少 device_id"),
            ({"device_id": ""}, "device_id 为空串"),
            ({"device_id": "f" * 65}, "device_id 65 字符超长"),
            ({"device_id": 12345}, "device_id 非字符串"),
        )
        for payload, label in invalid_payloads:
            r = requests.post(
                f"{server.base_url}/licenses/heartbeat", json=payload, timeout=20
            )
            check(
                r.status_code == 422,
                f"{label} -> 422（实际 {r.status_code}）",
            )

        # 64 字符边界必须被接受（真实机器码就是 64 位）
        server.heartbeat({"device_id": "a" * 64})
        check(True, "64 字符 device_id 被接受（真实机器码长度）")

        # ------------------------------------------------------------------
        # DAU 查询：与链路图中的 COUNT(*) 一致
        # ------------------------------------------------------------------
        print("\n[DAU 查询]", flush=True)
        dau_rows = server.sql(
            "SELECT active_date, COUNT(*) FROM device_daily_active "
            "GROUP BY active_date ORDER BY active_date DESC"
        )
        check(len(dau_rows) >= 1, "按 active_date 聚合返回至少一天")
        if dau_rows:
            today_date, today_count = dau_rows[0]
            check(today_count == 3, f"今日活跃设备数为 3（实际 {today_count}）")
            check(
                isinstance(today_date, str) and len(today_date) == 10,
                f"active_date 以 ISO 日期存储：{today_date}",
            )

        # ------------------------------------------------------------------
        # 服务端日志无异常
        # ------------------------------------------------------------------
        print("\n[服务端日志]", flush=True)
        log = server.read_log()
        check(
            "Traceback" not in log,
            "心跳请求未在服务端产生任何 traceback",
        )

    finally:
        server.stop()
        server.cleanup()

    print("\n" + "-" * 64, flush=True)
    print("PASSED=%d  FAILED=%d  SKIPPED=%d" % (PASSED, FAILED, SKIPPED), flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
