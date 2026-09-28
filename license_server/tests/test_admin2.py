# -*- coding: utf-8 -*-
"""
LiveLens Admin 2.0 — 自动化测试套件（Phase 2 阶段 E）。

覆盖：认证与 fail-closed、CSRF、批量生成与事务、仅 UNUSED 可吊销、
KEY 反查（规范化/哈希一致性/未命中/已兑换关联）、北京时间日切（跨日/跨月/
跨年）、首次激活去重与续费判定、半年卡与其他时长、设备状态口径、空库、
分页、CSV 公式注入、旧后台与既有授权 API 回归。
不依赖生产数据库（conftest.py 将 DATABASE_URL 指向一次性临时库）。

运行（license_server/ 目录）：
    .venv/Scripts/python.exe -m pytest tests/ -v
"""

import os
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import config
from services.stats_service import parse_utc
from app.main import app  # noqa: E402  （conftest 已先设置测试数据库）
from database import SessionLocal
from models import Authorization, DeviceDailyActive, License, LicenseState, UpdateRelease

ADMIN_KEY = "test-admin-key-phase2"
client = TestClient(app)


# ======================================================================
# 工具
# ======================================================================

def wipe_tables():
    """清空业务表（测试库，非生产）。"""
    db = SessionLocal()
    try:
        from models import RedemptionEvent
        for t in (RedemptionEvent, DeviceDailyActive, License, Authorization, UpdateRelease):
            db.query(t).delete()
        db.commit()
    finally:
        db.close()


def add_auth(device_id, created_at, expires_at, state="ACTIVE", activated_at=None):
    db = SessionLocal()
    try:
        a = Authorization(device_id=device_id, created_at=created_at,
                          expires_at=expires_at, state=state, activated_at=activated_at)
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def add_license(auth_id, redeemed_at, duration_days=30, state=LicenseState.REDEEMED,
                created_at=None, features=None):
    import hashlib
    import os
    db = SessionLocal()
    try:
        key_hash = hashlib.sha256(os.urandom(32)).hexdigest()
        lic = License(key_hash=key_hash, authorization_id=auth_id,
                      duration_days=duration_days, state=state,
                      created_at=created_at or redeemed_at,
                      redeemed_at=redeemed_at if state == LicenseState.REDEEMED else None,
                      features=features)
        db.add(lic)
        db.commit()
        return lic.id, key_hash
    finally:
        db.close()


def login(client_obj, username="admin", password=ADMIN_KEY):
    """走真实登录流程；返回 (client, csrf_token)。"""
    r = client_obj.get("/admin2/login")
    assert r.status_code == 200
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, "登录页应包含 CSRF token"
    r2 = client_obj.post("/admin2/login", data={"username": username, "password": password,
                                                "csrf_token": m.group(1)},
                          follow_redirects=False)
    return r2


def logged_in_client():
    """返回已登录的 (client, csrf_token)。"""
    c = TestClient(app)
    r = login(c)
    assert r.status_code == 303, f"登录应成功，实际 {r.status_code}"
    page = c.get("/admin2/keys")
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, "页面应包含 CSRF meta"
    return c, m.group(1)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个测试：干净库 + 已配置的管理员凭据。"""
    wipe_tables()
    monkeypatch.setattr(config.settings, "ADMIN_API_KEY", ADMIN_KEY)
    yield
    wipe_tables()


# ======================================================================
# 1. 认证与安全
# ======================================================================

class TestAuth:
    def test_unauthenticated_page_redirects(self):
        r = client.get("/admin2/", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin2/login" in r.headers["location"]

    def test_unauthenticated_api_401(self):
        r = client.get("/admin2/api/overview")
        assert r.status_code == 401

    def test_all_api_endpoints_require_auth(self):
        for path in ["/admin2/api/keys", "/admin2/api/devices",
                     "/admin2/api/redemptions", "/admin2/api/analytics",
                     "/admin2/api/export/csv?scope=daily",
                     "/admin2/api/devices/DV-X"]:
            assert client.get(path).status_code == 401, path
        for path in ["/admin2/api/keys/generate", "/admin2/api/keys/revoke",
                     "/admin2/api/keys/lookup"]:
            assert client.post(path, json={}).status_code == 401, path

    def test_login_success_shares_session_with_classic_admin(self):
        c, _ = logged_in_client()
        # 会话键与经典后台一致：已登录的 client 访问 /admin 不再跳登录
        r = c.get("/admin/", follow_redirects=False)
        assert r.status_code in (200, 307), "与 SQLAdmin 共享登录态"
        assert r.status_code == 200

    def test_login_wrong_password(self):
        c = TestClient(app)
        r = login(c, password="wrong")
        assert r.status_code == 401
        assert "用户名或密码错误" in r.text

    def test_login_fail_closed_without_admin_key(self, monkeypatch):
        monkeypatch.setattr(config.settings, "ADMIN_API_KEY", None)
        c = TestClient(app)
        r = login(c, password=ADMIN_KEY)
        assert r.status_code == 401, "未配置凭据时必须 fail closed"

    def test_logout_clears_session(self):
        c, _ = logged_in_client()
        c.get("/admin2/logout")
        assert client.get("/admin2/", follow_redirects=False).status_code in (200, 303)
        r = c.get("/admin2/", follow_redirects=False)
        assert r.status_code == 303

    def test_post_without_csrf_403(self):
        c, _ = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1})
        assert r.status_code == 403
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                   headers={"X-CSRF-Token": "forged"})
        assert r.status_code == 403

    def test_expired_or_forged_session_cookie_rejected(self):
        # 伪造会话 cookie：不携带合法签名 → 视为未登录
        bad = TestClient(app)
        bad.cookies.set("session", "forged-cookie-value")
        assert bad.get("/admin2/api/overview").status_code == 401


# ======================================================================
# 2. 批量生成 / 吊销 / 反查
# ======================================================================

class TestKeys:
    def test_generate_batch_once_and_not_stored_plaintext(self):
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate",
                   json={"duration_days": 30, "quantity": 5, "features": "ocr,hd"},
                   headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 5 and len(data["plaintext_keys"]) == 5
        assert len(set(data["plaintext_keys"])) == 5, "明文 Key 必须唯一"
        assert r.headers.get("cache-control") == "no-store"
        # 明文绝不入库：库里只能查到哈希
        import hashlib
        db = SessionLocal()
        try:
            for k in data["plaintext_keys"]:
                h = hashlib.sha256(k.encode()).hexdigest()
                row = db.execute(text("SELECT state, features FROM licenses WHERE key_hash=:h"),
                                 {"h": h}).first()
                assert row is not None
                assert row.state == "UNUSED"
                import json as _json
                assert sorted(_json.loads(row.features)) == ["hd", "ocr"]
            assert db.execute(text("SELECT COUNT(*) FROM licenses")).scalar() == 5
        finally:
            db.close()

    def test_generate_rejects_non_preset_duration(self):
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 7, "quantity": 1},
                   headers={"X-CSRF-Token": csrf})
        assert r.status_code == 400

    def test_generate_rejects_out_of_range_quantity(self):
        c, csrf = logged_in_client()
        assert c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 0},
                      headers={"X-CSRF-Token": csrf}).status_code == 422
        assert c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1001},
                      headers={"X-CSRF-Token": csrf}).status_code == 422

    def test_generate_rollback_on_failure(self, monkeypatch):
        """批内任一失败 → 整批回滚，不写半批数据。"""
        from services.license_service import LicenseService
        c, csrf = logged_in_client()
        original = LicenseService.create_license
        calls = {"n": 0}

        def boom(self, license_data, features=None, commit=True):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated mid-batch failure")
            return original(self, license_data, features=features, commit=commit)

        monkeypatch.setattr(LicenseService, "create_license", boom)
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 1, "quantity": 5},
                   headers={"X-CSRF-Token": csrf})
        assert r.status_code == 500
        db = SessionLocal()
        try:
            assert db.execute(text("SELECT COUNT(*) FROM licenses")).scalar() == 0, \
                "第 3 个失败时前 2 个也必须回滚"
        finally:
            db.close()

    def test_revoke_only_unused(self):
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 2},
                   headers={"X-CSRF-Token": csrf})
        key = r.json()["plaintext_keys"][0]
        import hashlib
        h = hashlib.sha256(key.encode()).hexdigest()
        db = SessionLocal()
        try:
            lid = db.execute(text("SELECT id FROM licenses WHERE key_hash=:h"), {"h": h}).scalar()
        finally:
            db.close()
        # UNUSED → 吊销成功
        assert c.post("/admin2/api/keys/revoke", json={"license_id": lid},
                      headers={"X-CSRF-Token": csrf}).status_code == 200
        # 已吊销 → 409，不可重复处理
        assert c.post("/admin2/api/keys/revoke", json={"license_id": lid},
                      headers={"X-CSRF-Token": csrf}).status_code == 409
        # 不存在的 ID → 404
        assert c.post("/admin2/api/keys/revoke", json={"license_id": 999999},
                      headers={"X-CSRF-Token": csrf}).status_code == 404

    def test_keys_list_filters_and_pagination(self):
        c, csrf = logged_in_client()
        c.post("/admin2/api/keys/generate", json={"duration_days": 1, "quantity": 3},
               headers={"X-CSRF-Token": csrf})
        c.post("/admin2/api/keys/generate", json={"duration_days": 365, "quantity": 2},
               headers={"X-CSRF-Token": csrf})
        r = c.get("/admin2/api/keys?page=1&page_size=2")
        data = r.json()
        assert data["total"] == 5 and len(data["items"]) == 2 and data["page"] == 1
        assert data["counts"] == {"total": 5, "unused": 5, "redeemed": 0, "revoked": 0}
        # 状态过滤
        r = c.get("/admin2/api/keys?state=UNUSED&page_size=100")
        assert r.json()["total"] == 5
        # 套餐过滤
        r = c.get("/admin2/api/keys?plan=y365&page_size=100")
        assert r.json()["total"] == 2
        assert all(i["plan_key"] == "y365" for i in r.json()["items"])
        # 指纹前缀检索
        prefix = r.json()["items"][0]["hash"][:8]
        r = c.get(f"/admin2/api/keys?q={prefix}")
        assert r.json()["total"] >= 1
        # 第 2 页
        r = c.get("/admin2/api/keys?page=3&page_size=2")
        assert r.json()["page"] == 3 and len(r.json()["items"]) == 1

    def test_lookup_lifecycle(self):
        """反查：规范化（去空白）、哈希一致、未使用 → 兑换后关联设备。"""
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                   headers={"X-CSRF-Token": csrf})
        key = r.json()["plaintext_keys"][0]
        import hashlib
        expected_hash = hashlib.sha256(key.encode()).hexdigest()

        # 未使用
        r = c.post("/admin2/api/keys/lookup", json={"license_key": f"  {key}  "},
                   headers={"X-CSRF-Token": csrf})
        data = r.json()
        assert data["status"] == "unused"
        assert data["hash8"] == expected_hash[:8] + "…", "必须使用与业务层一致的 SHA-256"
        assert data["device"] is None
        assert data["license"]["plan_key"] == "m30"

        # 未找到
        r = c.post("/admin2/api/keys/lookup", json={"license_key": "not-a-real-key"},
                   headers={"X-CSRF-Token": csrf})
        assert r.json()["status"] == "not_found"

        # 走真实兑换 API（回归 Business Layer），再反查应带设备信息
        device = "DV-LOOKUP-TEST"
        act = c.post("/api/v1/licenses/activate",
                     json={"license_key": key, "device_id": device})
        assert act.status_code == 200, act.text
        r = c.post("/admin2/api/keys/lookup", json={"license_key": key},
                   headers={"X-CSRF-Token": csrf})
        data = r.json()
        assert data["status"] == "redeemed"
        assert data["device"] is not None
        assert data["device"]["device_id"] == device
        assert data["device"]["state"] == "active"
        assert data["license"]["authorization_id"] is not None


# ======================================================================
# 3. 统计口径：北京时间日切 / 首次激活 / 续费 / 套餐
# ======================================================================

class TestStatsCaliber:
    def seed(self):
        """确定性场景（时间为 naive UTC）：
        - DV-A 首次激活 2026-01-15 15:59 UTC → 北京 01-15 23:59（日切前）
        - DV-B 首次激活 2026-01-15 16:00 UTC → 北京 01-16 00:00（日切后）
        - DV-A 续费 2026-01-16 18:00 UTC（半年卡 180）
        - DV-A 同日再续 2026-01-16 19:00 UTC（90 天 → 其他套餐）
        - DV-C 首次激活 2025-12-31 16:30 UTC → 北京 2026-01-01（跨年）
        - DV-B 续费 2026-02-01 10:00 UTC（365 天卡）
        """
        a1 = add_auth("DV-A", datetime(2026, 1, 15, 15, 59, 0),
                      datetime(2026, 2, 14, 15, 59, 0))
        add_license(a1, datetime(2026, 1, 15, 15, 59, 30), 30)
        b1 = add_auth("DV-B", datetime(2026, 1, 15, 16, 0, 0),
                      datetime(2026, 2, 14, 16, 0, 0))
        add_license(b1, datetime(2026, 1, 15, 16, 0, 30), 30)
        add_license(a1, datetime(2026, 1, 16, 18, 0, 0), 180)
        add_license(a1, datetime(2026, 1, 16, 19, 0, 0), 90)
        c1 = add_auth("DV-C", datetime(2025, 12, 31, 16, 30, 0),
                      datetime(2026, 1, 30, 16, 30, 0))
        add_license(c1, datetime(2025, 12, 31, 16, 30, 30), 30)
        add_license(b1, datetime(2026, 2, 1, 10, 0, 0), 365)

    def test_beijing_day_boundary_and_cross_year(self):
        self.seed()
        c, _ = logged_in_client()
        data = c.get("/admin2/api/analytics?date_from=2025-12-25&date_to=2026-02-10").json()
        rows = {r["date"]: r for r in data["rows"]}

        assert rows["2025-12-31"]["new_devices"] == 0, "UTC 12-31 16:30 已是北京 01-01"
        assert rows["2026-01-01"]["new_devices"] == 1, "跨年归档必须落在北京时间 01-01"
        assert rows["2026-01-15"]["new_devices"] == 1, "15:59 UTC 属北京 01-15"
        assert rows["2026-01-16"]["new_devices"] == 1, "16:00 UTC 属北京 01-16"

        assert rows["2026-01-17"]["renew"] == 2, "同日两笔续费都要计数（01-16 18/19 时 UTC = 北京 01-17）"
        assert rows["2026-01-17"]["h180"] == 1, "半年卡"
        assert rows["2026-01-17"]["other"] == 1, "90 天 → 其他套餐，不得静默归类"

        assert rows["2026-01-15"]["first"] == 1 and rows["2026-01-15"]["m30"] == 1
        assert rows["2026-01-16"]["first"] == 1 and rows["2026-01-16"]["m30"] == 1
        assert rows["2026-02-01"]["renew"] == 1 and rows["2026-02-01"]["y365"] == 1

        t = data["totals"]
        assert t["new_devices"] == 3 and t["first"] == 3 and t["renew"] == 3
        assert t["redeem_total"] == 6
        assert t["m30"] == 3 and t["h180"] == 1 and t["y365"] == 1 and t["other"] == 1

    def test_first_vs_renewal_tiebreak_and_consistency(self):
        self.seed()
        db = SessionLocal()
        try:
            counts = __import__("services.stats_service", fromlist=["redemption_counts"]) \
                .redemption_counts(db)
            assert counts["first"] + counts["renew"] == counts["total"] == 6
            assert counts["first"] == 3 and counts["renew"] == 3
        finally:
            db.close()

    def test_renewal_result_marked_unknown_not_fabricated(self):
        self.seed()
        c, _ = logged_in_client()
        data = c.get("/admin2/api/redemptions?type_=renewal&page_size=50").json()
        assert data["total"] == 3
        for item in data["items"]:
            assert "不可回溯" in item["result"], "历史续费语义必须标记为未知"
        first = c.get("/admin2/api/redemptions?type_=first&page_size=50").json()
        assert first["total"] == 3
        for item in first["items"]:
            assert item["result"] == "新建授权"

    def test_empty_database_zeros(self):
        c, _ = logged_in_client()
        data = c.get("/admin2/api/analytics?days=7").json()
        assert all(r["new_devices"] == 0 and r["dau"] == 0 and r["first"] == 0 for r in data["rows"])
        assert data["totals"]["redeem_total"] == 0
        ov = c.get("/admin2/api/overview").json()
        assert ov["devices"]["total"] == 0
        assert ov["today"]["new_devices"] == 0

    def test_device_states_and_detail(self):
        now = datetime.utcnow()
        add_auth("DV-OK", now - timedelta(days=10), now + timedelta(days=30))
        add_auth("DV-SOON", now - timedelta(days=10), now + timedelta(days=3))
        add_auth("DV-EXP", now - timedelta(days=40), now - timedelta(days=1))
        c, _ = logged_in_client()
        data = c.get("/admin2/api/devices?sort=remain_asc&page_size=50").json()
        states = {d["device_id"]: d["state"] for d in data["items"]}
        assert states == {"DV-OK": "active", "DV-SOON": "expiring", "DV-EXP": "expired"}
        assert data["counts"]["total"] == 3

        detail = c.get("/admin2/api/devices/DV-EXP").json()
        assert detail["state"] == "expired"
        assert detail["redemptions"] == 0

    def test_device_detail_history_order_and_type(self):
        t0 = datetime(2026, 3, 1, 2, 0, 0)
        a = add_auth("DV-H", t0, t0 + timedelta(days=30))
        add_license(a, t0 + timedelta(seconds=30), 30)
        add_license(a, t0 + timedelta(days=5), 180)
        add_license(a, t0 + timedelta(days=9), 365)
        c, _ = logged_in_client()
        d = c.get("/admin2/api/devices/DV-H").json()
        assert d["redemptions"] == 3
        types = [h["type"] for h in d["history"]]  # 升序
        assert types == ["first", "renewal", "renewal"]
        assert d["history"][0]["result"] == "新建授权"
        assert "不可回溯" in d["history"][1]["result"]
        plans = {h["plan_name"] for h in d["history"]}
        assert plans == {"月卡", "半年卡", "年卡"}

    def test_pagination_large_page_size_cap(self):
        c, csrf = logged_in_client()
        c.post("/admin2/api/keys/generate", json={"duration_days": 1, "quantity": 25},
               headers={"X-CSRF-Token": csrf})
        r = c.get("/admin2/api/keys?page=1&page_size=10").json()
        assert r["total"] == 25 and len(r["items"]) == 10
        r2 = c.get("/admin2/api/keys?page=3&page_size=10").json()
        assert len(r2["items"]) == 5

    def test_analytics_invalid_range(self):
        c, _ = logged_in_client()
        assert c.get("/admin2/api/analytics?date_from=2026-01-02&date_to=2026-01-01").status_code == 400
        assert c.get("/admin2/api/analytics?date_from=bad-date").status_code == 400


# ======================================================================
# 4. CSV 导出与防公式注入
# ======================================================================

class TestCsvExport:
    def test_csv_sanitizes_formula_injection(self):
        # 恶意 device_id / features 不应被 Excel 当公式执行
        now = datetime.utcnow()
        a = add_auth("=HYPERLINK(\"http://evil\")", now - timedelta(days=1),
                     now + timedelta(days=10))
        add_license(a, now - timedelta(hours=5), 30, features='["=cmd|\' /c calc!\'"]')
        c, _ = logged_in_client()
        r = c.get("/admin2/api/export/csv?scope=devices")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        body = r.content.decode("utf-8-sig")
        for line in body.splitlines():
            for cell in line.split(","):
                assert not cell.startswith(("=", "+", "-", "@")), f"公式注入未防护: {cell}"

    def test_csv_daily_export_with_totals(self):
        c, _ = logged_in_client()
        r = c.get("/admin2/api/export/csv?scope=daily&days=7")
        assert r.status_code == 200
        lines = r.content.decode("utf-8-sig").strip().splitlines()
        assert len(lines) == 9  # 表头 + 7 天 + 合计
        assert lines[0].startswith("日期(北京时间)")
        assert lines[-1].startswith("合计")

    def test_csv_requires_auth(self):
        assert client.get("/admin2/api/export/csv?scope=keys").status_code == 401


# ======================================================================
# 5. 旧后台与既有授权 API 回归
# ======================================================================

class TestLegacyRegression:
    def test_root_and_activate_validate_flow(self):
        c = TestClient(app)
        assert c.get("/").status_code == 200
        # 走既有 admin API 生成（X-API-Key），再激活/校验
        r = c.post("/api/v1/admin/licenses", json={"duration_days": 30},
                   headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200, "admin2 上线不得影响既有管理 API"
        key = r.json()["license_key"]
        device = "DV-REGRESSION"
        act = c.post("/api/v1/licenses/activate", json={"license_key": key, "device_id": device})
        assert act.status_code == 200 and act.json().get("signed_license")
        val = c.post("/api/v1/licenses/validate", json={"license_key": key, "device_id": device})
        assert val.status_code == 200 and val.json()["valid"] is True

    def test_heartbeat_dau_and_admin2_consistency(self):
        c = TestClient(app)
        for d in ("DV-HB-1", "DV-HB-2", "DV-HB-1"):  # 第三次是同设备重复心跳
            r = c.post("/api/v1/licenses/heartbeat", json={"device_id": d})
            assert r.status_code == 200
        c2, _ = logged_in_client()
        ov = c2.get("/admin2/api/overview").json()
        assert ov["today"]["dau"] == 2, "DAU 去重口径：同设备同日只计 1"
        dv = c2.get("/admin2/api/devices?page_size=50").json()
        assert dv["counts"]["total"] == 0, "心跳不创建授权（与既有语义一致）"

    def test_classic_admin_still_mounted(self):
        c = TestClient(app)
        # 未登录访问经典后台：最终落在 SQLAdmin 登录页（允许中间 30x）
        r = c.get("/admin/login")
        assert r.status_code == 200 and "password" in r.text.lower(),             "SQLAdmin 登录页必须仍然可用"
        r2 = c.get("/admin/", follow_redirects=False)
        assert r2.status_code in (200, 302, 307)

    def test_swagger_hidden_in_production_debug_off(self):
        if config.settings.DEBUG:
            pytest.skip("本地 .env 开启 DEBUG，/docs 仅生产断言（生产 DEBUG=false）")
        c = TestClient(app)
        assert c.get("/docs").status_code == 404  # DEBUG=false


# ======================================================================
# 6. 实时兑换事件日志（Phase 3 任务二）
# ======================================================================

from sqlalchemy.orm import Session as _OrmSession  # noqa: E402


def _events(db):
    from sqlalchemy import text as _t
    return [dict(r._mapping) for r in db.execute(_t(
        "SELECT license_id, event_type, result, duration_days, recorded_via, "
        "auth_expires_before, auth_expires_after FROM redemption_events"))]


class TestRedemptionEvents:
    def _activate(self, csrf_client, key, device):
        return csrf_client.post("/api/v1/licenses/activate",
                                json={"license_key": key, "device_id": device})

    def test_first_activation_event(self):
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                   headers={"X-CSRF-Token": csrf})
        key = r.json()["plaintext_keys"][0]
        assert self._activate(c, key, "DV-EVT-1").status_code == 200
        db = SessionLocal()
        try:
            ev = _events(db)
            assert len(ev) == 1
            e = ev[0]
            assert e["event_type"] == "first"
            assert e["result"] == "new"
            assert e["duration_days"] == 30
            assert e["recorded_via"] == "live"
            assert e["auth_expires_before"] is None  # 新建授权无“之前”
            assert e["auth_expires_after"] is not None
        finally:
            db.close()

    def test_renewal_extend_captures_before_and_after(self):
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 2},
                   headers={"X-CSRF-Token": csrf})
        k1, k2 = r.json()["plaintext_keys"]
        assert self._activate(c, k1, "DV-EVT-2").status_code == 200
        assert self._activate(c, k2, "DV-EVT-2").status_code == 200  # ACTIVE 累加
        db = SessionLocal()
        try:
            ev = sorted(_events(db), key=lambda x: x["license_id"])
            assert len(ev) == 2
            renewal = ev[1]
            assert renewal["event_type"] == "renewal"
            assert renewal["result"] == "extend"
            assert renewal["auth_expires_before"] is not None
            # 累加：after = before + 30 天
            delta = (parse_utc(renewal["auth_expires_after"])
                     - parse_utc(renewal["auth_expires_before"])).total_seconds()
            assert 29 * 86400 < delta <= 31 * 86400
        finally:
            db.close()

    def test_renewal_reset_after_expiry(self):
        """过期后续费 → result=reset，before=旧到期时间（<now），after=新到期。"""
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 1, "quantity": 2},
                   headers={"X-CSRF-Token": csrf})
        k1, k2 = r.json()["plaintext_keys"]
        assert self._activate(c, k1, "DV-EVT-3").status_code == 200
        # 手动把授权改过期（测试库操作）
        db = SessionLocal()
        try:
            from models import Authorization
            a = db.query(Authorization).filter_by(device_id="DV-EVT-3").first()
            a.expires_at = datetime.utcnow() - timedelta(days=1)
            a.state = "EXPIRED"
            db.commit()
        finally:
            db.close()
        assert self._activate(c, k2, "DV-EVT-3").status_code == 200
        db = SessionLocal()
        try:
            renewal = [e for e in _events(db) if e["event_type"] == "renewal"][0]
            assert renewal["result"] == "reset"
            assert parse_utc(renewal["auth_expires_before"]) < datetime.utcnow()
        finally:
            db.close()

    def test_duplicate_redemption_no_second_event(self):
        """重复兑换被业务层拒绝 → 绝不产生第二条事件。"""
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                   headers={"X-CSRF-Token": csrf})
        key = r.json()["plaintext_keys"][0]
        assert self._activate(c, key, "DV-EVT-4").status_code == 200
        assert self._activate(c, key, "DV-EVT-4").status_code == 400
        db = SessionLocal()
        try:
            assert len(_events(db)) == 1
        finally:
            db.close()

    def test_event_rolls_back_with_transaction(self, monkeypatch):
        """最终 commit 失败 → 卡密/授权/事件整体回滚（同一事务）。

        用续费路径精确命中兑换的最终 commit（续费时无内部 commit）：
        第一次 commit 调用模拟失败，之后恢复正常以便夹具清理。
        """
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 2},
                   headers={"X-CSRF-Token": csrf})
        k1, k2 = r.json()["plaintext_keys"]
        assert self._activate(c, k1, "DV-EVT-5").status_code == 200
        db = SessionLocal()
        try:
            before = _events(db)
            assert len(before) == 1
            auth_before = db.execute(text(
                "SELECT expires_at FROM authorizations WHERE device_id='DV-EVT-5'"
            )).scalar()
        finally:
            db.close()

        calls = {"n": 0}
        orig_commit = _OrmSession.commit

        def failing_commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated commit failure")
            return orig_commit(self)

        monkeypatch.setattr(_OrmSession, "commit", failing_commit)
        c2 = TestClient(app, raise_server_exceptions=False)
        resp = c2.post("/api/v1/licenses/activate",
                       json={"license_key": k2, "device_id": "DV-EVT-5"})
        assert resp.status_code == 500
        monkeypatch.undo()

        db = SessionLocal()
        try:
            after = _events(db)
            assert len(after) == 1, "事件必须随事务回滚"
            # 直接按授权状态断言更稳：授权未累加、卡密仍为 UNUSED
            state = db.execute(text(
                "SELECT state FROM authorizations WHERE device_id='DV-EVT-5'")).scalar()
            assert state == "ACTIVE"
            exp = db.execute(text(
                "SELECT expires_at FROM authorizations WHERE device_id='DV-EVT-5'")).scalar()
            assert exp == auth_before, "有效期不得被部分累加"
            unused = db.execute(text(
                "SELECT COUNT(*) FROM licenses WHERE state='UNUSED'")).scalar()
            assert unused == 1, "k2 必须仍是未使用（可重试兑换）"
        finally:
            db.close()

    def test_concurrent_redemption_exactly_one_event(self):
        """并发兑换同一 Key：恰好一人成功、恰好一条事件（UNIQUE 兜底）。"""
        import concurrent.futures
        c, csrf = logged_in_client()
        r = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                   headers={"X-CSRF-Token": csrf})
        key = r.json()["plaintext_keys"][0]

        def worker(i):
            cl = TestClient(app)
            return cl.post("/api/v1/licenses/activate",
                           json={"license_key": key, "device_id": f"DV-CC-{i}"}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            codes = list(ex.map(worker, range(4)))
        assert codes.count(200) == 1, f"应恰好一个成功，实际 {codes}"
        assert codes.count(400) == 3
        db = SessionLocal()
        try:
            assert len(_events(db)) == 1
        finally:
            db.close()

    def test_backfill_and_live_coexist_no_duplicates(self):
        """先造历史数据并回填，再实时兑换新卡：两类事件共存、重复回填不产生重复。"""
        import subprocess
        import sys
        # 造 2 条历史兑换（auth A：1 首充 + 1 续费）
        t0 = datetime(2026, 3, 1, 2, 0, 0)
        a = add_auth("DV-BF", t0, t0 + timedelta(days=30))
        add_license(a, t0 + timedelta(seconds=30), 30)
        add_license(a, t0 + timedelta(days=5), 180)
        # 回填（子进程执行迁移脚本，验证其可独立运行）
        env = dict(os.environ)
        r = subprocess.run(
            [sys.executable, "migrate_admin2.py"], env=env, cwd=".",
            capture_output=True, text=True, timeout=60)
        assert "一致 ✓" in r.stdout, r.stdout + r.stderr
        # 实时兑换一张新卡
        c, csrf = logged_in_client()
        r2 = c.post("/admin2/api/keys/generate", json={"duration_days": 30, "quantity": 1},
                    headers={"X-CSRF-Token": csrf})
        key = r2.json()["plaintext_keys"][0]
        assert self._activate(c, key, "DV-BF-LIVE").status_code == 200
        db = SessionLocal()
        try:
            ev = _events(db)
            assert len(ev) == 3
            backfilled = [e for e in ev if e["recorded_via"] == "backfill"]
            live = [e for e in ev if e["recorded_via"] == "live"]
            assert len(backfilled) == 2 and len(live) == 1
            assert backfilled[1]["result"] == "unknown", "回填续费必须 unknown"
            assert live[0]["result"] == "new"
        finally:
            db.close()
        # 重复回填不产生重复
      
