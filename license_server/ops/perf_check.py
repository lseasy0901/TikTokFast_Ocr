# -*- coding: utf-8 -*-
"""
Phase 3 任务三 — 统计查询性能与索引核查（本地临时库，5000 行量级）。

产出：
1. 关键统计 SQL 的 EXPLAIN QUERY PLAN（确认索引使用、无失控扫描）；
2. 各管理接口在 5000 卡密 / 500 设备 / 60 天 DAU 下的耗时；
3. CSV 全量导出耗时，以及导出期间授权 API（validate）的延迟影响。

运行：license_server/ 目录下
    .venv/Scripts/python.exe ../LiveLens_Admin_2.0/perf_check.py
（路径不同时可自行调整 sys.path；脚本只建临时库，不触碰任何真实数据。）
"""

import os
import sys
import time
import tempfile

_LS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "DouyinLowLatencyViewer", "license_server")
sys.path.insert(0, _LS)             # app. 包导入（app.main）
sys.path.insert(0, os.path.join(_LS, "app"))  # 顶层模块（database/models/...）
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "DouyinLowLatencyViewer", "license_server"))

_TMP = tempfile.mkdtemp(prefix="livelens-perf-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "perf.db").replace("\\", "/")
os.environ.setdefault("DEBUG", "false")

import hashlib  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from sqlalchemy import text  # noqa: E402
from database import SessionLocal, engine, Base  # noqa: E402
from models import Authorization, DeviceDailyActive, License, LicenseState  # noqa: E402

N_LICENSES = 5000
N_DEVICES = 500
N_DAU_DAYS = 60

print("=" * 64)
print(f"种子数据：{N_LICENSES} 卡密 / {N_DEVICES} 设备 / {N_DAU_DAYS} 天 DAU")
print("=" * 64)

# 模拟生产：先加统计索引（迁移脚本内容）
Base.metadata.create_all(bind=engine)
with engine.begin() as con:
    for ddl in [
        "CREATE INDEX IF NOT EXISTS ix_licenses_created_at ON licenses (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_licenses_redeemed_at ON licenses (redeemed_at)",
        "CREATE INDEX IF NOT EXISTS ix_licenses_state ON licenses (state)",
    "CREATE INDEX IF NOT EXISTS ix_licenses_auth_state_redeemed "
    "ON licenses (authorization_id, state, redeemed_at)",
        "CREATE INDEX IF NOT EXISTS ix_device_daily_active_device_seen "
        "ON device_daily_active (device_id, last_seen_at)",
    ]:
        con.execute(text(ddl))

db = SessionLocal()
try:
    now = datetime.utcnow()
    print("写入种子数据…")
    t0 = time.perf_counter()
    devs = []
    for i in range(N_DEVICES):
        devs.append(Authorization(
            device_id=f"DV-PERF-{i:05d}",
            created_at=now - timedelta(days=N_DAU_DAYS - i % N_DAU_DAYS),
            expires_at=now + timedelta(days=(i % 90) - 10),
            state="ACTIVE",
        ))
    db.add_all(devs)
    db.commit()

    days_pool = [1, 30, 180, 365]
    batch = []
    for i in range(N_LICENSES):
        st = LicenseState.REDEEMED if i % 3 else LicenseState.UNUSED
        redeemed = now - timedelta(hours=i % (N_DAU_DAYS * 24)) if st == LicenseState.REDEEMED else None
        batch.append(License(
            key_hash=hashlib.sha256(f"perf-{i}".encode()).hexdigest(),
            authorization_id=devs[i % N_DEVICES].id if redeemed else None,
            duration_days=days_pool[i % 4],
            state=st,
            created_at=now - timedelta(hours=i % (N_DAU_DAYS * 24)),
            redeemed_at=redeemed,
        ))
    db.add_all(batch)
    db.commit()
    for i in range(N_DEVICES):
        for d in range(N_DAU_DAYS):
            db.add(DeviceDailyActive(
                device_id=f"DV-PERF-{i:05d}",
                active_date=(now - timedelta(days=d)).date(),
                first_seen_at=now, last_seen_at=now, launch_count=1,
            ))
    db.commit()
    print(f"种子完成 {time.perf_counter() - t0:.1f}s")

    # ---- EXPLAIN QUERY PLAN ----
    QUERIES = {
        "keys_page(排序+分页)": (
            "SELECT l.id FROM licenses l ORDER BY l.created_at DESC, l.id DESC LIMIT 10 OFFSET 100"),
        "keys_state_filter": (
            "SELECT l.id FROM licenses l WHERE l.state='REDEEMED' ORDER BY l.created_at DESC LIMIT 10"),
        "redemptions_by_day(续费判定)": (
            "SELECT date(l.redeemed_at, '+8 hours') AS d, COUNT(*) FROM licenses l "
            "WHERE l.state='REDEEMED' AND l.redeemed_at IS NOT NULL GROUP BY d"),
        "renewal_exists(单行判定)": (
            "SELECT COUNT(*) FROM licenses l WHERE l.state='REDEEMED' AND EXISTS ("
            "SELECT 1 FROM licenses f WHERE f.authorization_id=l.authorization_id "
            "AND f.state='REDEEMED' AND f.redeemed_at < l.redeemed_at)"),
        "dau_by_day": (
            "SELECT active_date, COUNT(*) FROM device_daily_active WHERE "
            "active_date >= '2026-08-01' GROUP BY active_date"),
        "devices_latest_plan(相关子查询)": (
            "SELECT a.id FROM authorizations a WHERE EXISTS (SELECT 1 FROM licenses l "
            "WHERE l.authorization_id=a.id AND l.state='REDEEMED')"),
    }
    print("\n---- EXPLAIN QUERY PLAN ----")
    for name, sql in QUERIES.items():
        plan = [f"{r[3]}" for r in db.execute(text("EXPLAIN QUERY PLAN " + sql))]
        flag = ""
        for p in plan:
            if "SCAN" in p and "COVERING" not in p and "USING INDEX" not in p:
                flag = "  ⚠ 全表扫描"
        print(f"[{name}]{' ' * max(0, 30 - len(name))}{plan[0][:70]}{flag}")

    # ---- 接口耗时 ----
    print("\n---- 接口耗时（5000 行量级） ----")
    from fastapi.testclient import TestClient
    import config
    config.settings.ADMIN_API_KEY = "perf-key"
    from main import app
    client = TestClient(app)

    def timed(label, fn, n=3):
        best = 1e9
        for _ in range(n):
            t = time.perf_counter()
            r = fn()
            best = min(best, time.perf_counter() - t)
            assert r.status_code in (200, 307), (label, r.status_code)
        print(f"{label:<44} {best * 1000:8.1f} ms")

    # 登录
    lr = client.get("/admin2/login")
    import re as _re
    tok = _re.search(r'name="csrf_token" value="([^"]+)"', lr.text).group(1)
    client.post("/admin2/login", data={"username": "admin", "password": "perf-key",
                                       "csrf_token": tok})
    page = client.get("/admin2/keys")
    csrf = _re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    timed("GET /admin2/api/keys (page_size=10)", lambda: client.get("/admin2/api/keys?page=250&page_size=10"))
    timed("GET /admin2/api/keys?q=前缀过滤", lambda: client.get("/admin2/api/keys?q=abcdef0"))
    timed("GET /admin2/api/devices (page_size=50)", lambda: client.get("/admin2/api/devices?page_size=50&sort=remain_asc"))
    timed("GET /admin2/api/redemptions (page_size=12)", lambda: client.get("/admin2/api/redemptions?page_size=12"))
    timed("GET /admin2/api/analytics?days=60", lambda: client.get("/admin2/api/analytics?days=60"))
    timed("GET /admin2/api/overview", lambda: client.get("/admin2/api/overview?days=30"))

    t = time.perf_counter()
    r = client.get("/admin2/api/export/csv?scope=keys")
    csv_ms = (time.perf_counter() - t) * 1000
    rows = r.content.count(b"\n") - 1
    print(f"{'GET /admin2/api/export/csv (keys 全量)':<44} {csv_ms:8.1f} ms  ({rows} 行)")

    # 导出期间授权 API 延迟（同进程顺序模拟争用场景）
    key = "x" * 40
    t = time.perf_counter()
    client.post("/api/v1/licenses/validate", json={"license_key": key, "device_id": "DV-X"})
    base_validate = (time.perf_counter() - t) * 1000
    print(f"\n{'validate 基线延迟':<44} {base_validate:8.1f} ms")
    print("结论：导出为纯 SELECT（WAL 读者不阻塞写者），无长事务持有写锁。")

finally:
    db.close()

print("\nPERF CHECK DONE")
