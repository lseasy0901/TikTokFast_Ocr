# -*- coding: utf-8 -*-
"""回归：stale-ACTIVE 授权的兑换必须从当下重启（hotfix/stale-active-renewal）。

事故（2026-10-24）：授权状态从不惰性翻转，设备过期后 state 仍是 ACTIVE。
兑换时走 ACTIVE 累加分支 → 新有效期 = 旧过期日 + duration，仍然在过去，
签出的 SignedLicense 一到手就是过期的（客户端报「许可证已过期」）。

本文件三个用例：
    1. stale-ACTIVE（state=ACTIVE 但 expires_at 已过）兑换 → 从当下重启
    2. 真正 ACTIVE（未过期）兑换 → 照旧从现有到期日累加（不改变原语义）
    3. 事件流水：stale-ACTIVE 记为 renewal/reset，正常累加记为 renewal/extend
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "app"))

from database import Base, SessionLocal  # noqa: E402
from models import (  # noqa: E402
    Authorization,
    AuthorizationState,
    LicenseState,
    RedemptionEvent,
)

# 服务级测试不经过 app 启动流程，这里显式建表（conftest 已把 DATABASE_URL
# 指向一次性临时库，建表不会碰生产/开发库）
from database import engine as _engine  # noqa: E402
Base.metadata.create_all(bind=_engine)
from schemas import LicenseActivate, LicenseCreate  # noqa: E402
from services.license_service import LicenseService  # noqa: E402
from services.susi_security_service import SusiSecurityService  # noqa: E402
from services.redemption_service import RedemptionService  # noqa: E402
from config import settings  # noqa: E402

# 与 main.py 相同的构造方式（开发者默认签名密钥 test_rsa_key.pem）
_susi = SusiSecurityService(settings)

TOLERANCE = 120  # 秒：与 "server_time + duration" 的允许偏差


def _aware(dt):
    """SQLite 读回的 datetime 丢时区（库里统一 UTC）：断言前补 UTC。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _make_key(db, days: int):
    return LicenseService(db).create_license(LicenseCreate(duration_days=days))


def _redeem(db, license_row, device_id: str):
    service = RedemptionService(db, _susi)
    license_model, response = service.redeem_license(LicenseActivate(
        license_key=license_row.license_key
        if hasattr(license_row, "license_key") else license_row,
        device_id=device_id,
    ))
    return license_model, response


def _event(db, license_id) -> RedemptionEvent:
    return db.query(RedemptionEvent).filter_by(license_id=license_id).one()


def test_stale_active_restarts_from_now():
    """state=ACTIVE 但 expires_at 已过：兑换后有效期 = 当下 + duration。"""
    db = SessionLocal()
    try:
        lic = _make_key(db, 1)
        stale = datetime.now(timezone.utc) - timedelta(days=7)
        db.add(Authorization(
            device_id="stale-dev", state=AuthorizationState.ACTIVE,
            expires_at=stale))
        db.commit()

        license_model, _resp = _redeem(db, lic, "stale-dev")

        auth = db.query(Authorization).filter_by(device_id="stale-dev").one()
        auth.expires_at = _aware(auth.expires_at)
        now = datetime.now(timezone.utc)
        expected = now + timedelta(days=1)
        assert auth.expires_at is not None, "兑换后必须有有效期"
        assert auth.expires_at > now, (
            f"stale-ACTIVE 兑换后的有效期必须在未来，实际 {auth.expires_at}")
        assert abs((auth.expires_at - expected).total_seconds()) < TOLERANCE, (
            f"应从当下重启（≈{expected}），实际 {auth.expires_at}")
        assert auth.activated_at is not None

        ev = _event(db, license_model.id)
        assert ev.event_type == "renewal"
        assert ev.result == "reset", (
            f"stale-ACTIVE 应记为 reset，实际 {ev.result}")
        assert _aware(ev.auth_expires_before) == stale
    finally:
        db.rollback()
        db.close()


def test_active_accumulation_unchanged():
    """未过期的 ACTIVE 兑换：保持原语义，从现有到期日累加。"""
    db = SessionLocal()
    try:
        lic = _make_key(db, 30)
        future = datetime.now(timezone.utc) + timedelta(days=10)
        db.add(Authorization(
            device_id="live-dev", state=AuthorizationState.ACTIVE,
            expires_at=future))
        db.commit()

        license_model, _resp = _redeem(db, lic, "live-dev")

        auth = db.query(Authorization).filter_by(device_id="live-dev").one()
        auth.expires_at = _aware(auth.expires_at)
        assert abs((auth.expires_at - (future + timedelta(days=30)))
                   .total_seconds()) < TOLERANCE, (
            f"正常累加语义不得改变：{future} + 30d，实际 {auth.expires_at}")
        ev = _event(db, license_model.id)
        assert ev.result == "extend"
    finally:
        db.rollback()
        db.close()


def test_lookup_reports_time_based_device_state():
    """卡密反查的设备状态与设备页同口径：过期设备显示 expired 而非 ACTIVE。"""
    db = SessionLocal()
    try:
        from services.stats_service import lookup_license

        lic = _make_key(db, 1)
        past = datetime.now(timezone.utc) - timedelta(days=3)
        auth = Authorization(
            device_id="lookup-dev", state=AuthorizationState.ACTIVE,
            expires_at=past)
        db.add(auth)
        db.commit()
        lic.authorization_id = auth.id
        lic.state = LicenseState.REDEEMED
        db.commit()

        result = lookup_license(db, lic.license_key)
        assert result["status"] == "redeemed"
        assert result["device"]["state"] == "expired", (
            f"过期设备反查应显示 expired，实际 {result['device']['state']}")
    finally:
        db.rollback()
        db.close()
