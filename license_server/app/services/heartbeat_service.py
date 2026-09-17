# -*- coding: utf-8 -*-
"""
Heartbeat / DAU service - Phase 7.4

Records that a client launched. Exactly one row per ``(device_id, active_date)``,
so counting daily active devices is a plain ``COUNT(*)`` per day rather than a
``GROUP BY`` over an event log.

Design notes
------------
* **De-duplication happens at write time**, via ``UNIQUE(device_id, active_date)``
  plus a SQLite upsert. A retried, duplicated or concurrent heartbeat for the same
  device and day can therefore never produce a second row.
* **The client's clock is never trusted.** It sends a device id and nothing else;
  ``active_date`` is derived from the *server's* UTC clock.
* **No authorization check.** A heartbeat means "the app started", not "the
  license is valid". An expired or revoked device still launched and is still
  counted -- that is the intended DAU definition.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

import config
from models import DeviceDailyActive
from schemas import HeartbeatResponse, LicenseHeartbeat

logger = logging.getLogger(__name__)

#: Business time zone, resolved once at import.
#
# This is *only* the calendar-day boundary for DAU buckets. Storage and every
# business calculation stay UTC; see config.settings.BUSINESS_TZ_NAME.
#
# On failure this stays None and record_heartbeat() raises, so the endpoint
# returns 5xx. That is deliberate: DAU is a non-critical path, and reporting a
# failure is strictly better than silently filing devices under the wrong day.
# On Linux the system tz database is used; on Windows the `tzdata` package
# provides it (license_server/requirements.txt).
try:
    from zoneinfo import ZoneInfo

    BUSINESS_TZ = ZoneInfo(config.settings.BUSINESS_TZ_NAME)
except Exception as exc:  # noqa: BLE001 - 缺时区库时要在调用点明确失败
    BUSINESS_TZ = None
    logger.error(
        "无法加载业务时区 %s，DAU 心跳端点将不可用（原因：%s）。"
        "请在运行环境安装 tzdata。",
        config.settings.BUSINESS_TZ_NAME,
        exc,
    )


def bucket_date(now_utc: datetime) -> date:
    """Map a UTC instant to the business-zone calendar day it belongs to.

    Naive input is interpreted as UTC, matching the convention used everywhere
    else in this codebase (``susi_security_service._to_rfc3339`` and
    ``RedemptionService`` both do the same).

    Args:
        now_utc: The instant to bucket. Should come from the server clock.

    Returns:
        The calendar date in ``config.settings.BUSINESS_TZ_NAME``.

    Raises:
        RuntimeError: The business time zone could not be resolved.
    """
    if BUSINESS_TZ is None:
        raise RuntimeError(
            f"业务时区 {config.settings.BUSINESS_TZ_NAME} 不可用（缺少时区数据库）"
        )
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(BUSINESS_TZ).date()


class HeartbeatService:
    """Records device activity for DAU. One instance per request."""

    def __init__(self, db: Session):
        self.db = db

    def record_heartbeat(self, heartbeat_data: LicenseHeartbeat) -> HeartbeatResponse:
        """Record one launch for ``heartbeat_data.device_id``.

        Idempotent per (device, business day): the first call of the day inserts
        the row, every later call only bumps ``last_seen_at`` and
        ``launch_count``. ``first_seen_at`` keeps the day's first observation.

        Returns:
            HeartbeatResponse carrying the business-zone day the call landed in,
            so the client (and tests) can see which bucket was credited.
        """
        now_utc = datetime.now(timezone.utc)
        active_date = bucket_date(now_utc)
        # Columns follow the project-wide naive-UTC convention (see models.py).
        now_naive = now_utc.replace(tzinfo=None)

        device_id = heartbeat_data.device_id

        # Single atomic statement: SQLite serialises writers, and database.py
        # already configures WAL + a 30s busy timeout. Two concurrent heartbeats
        # for the same device and day therefore resolve to exactly one row.
        #
        # The dialect-specific insert is used because this project's DATABASE_URL
        # is SQLite (app/database.py). `launch_count` in the SET clause refers to
        # the *existing* row's value (SQLite: unqualified column = current row,
        # `excluded.` = the proposed row), which is what we want for an increment.
        statement = (
            sqlite_insert(DeviceDailyActive)
            .values(
                device_id=device_id,
                active_date=active_date,
                first_seen_at=now_naive,
                last_seen_at=now_naive,
                launch_count=1,
            )
            .on_conflict_do_update(
                index_elements=["device_id", "active_date"],
                set_={
                    "last_seen_at": now_naive,
                    "launch_count": DeviceDailyActive.launch_count + 1,
                },
            )
        )

        self.db.execute(statement)
        self.db.commit()

        return HeartbeatResponse(ok=True, active_date=active_date)
