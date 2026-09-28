# -*- coding: utf-8 -*-
"""
LiveLens Admin 2.0 — 兑换事件日志表迁移（阶段 C，可重复执行）。

目的：
    现有 schema 没有独立的兑换事件日志，「兑换历史」只能由 REDEEMED 的
    License 行派生，且历史续费的「累加 vs 重置」语义无法回溯（兑换前的
    expires_at 未记录）。本迁移创建 ``redemption_events`` 表并从既有数据
    **确定性回填**，为未来的审计需求打底。

安全边界：
    * 只新增表、只插入缺失行；**不修改** licenses / authorizations 的任何
      数据，不改变任何授权业务语义；
    * 可重复执行：CREATE TABLE IF NOT EXISTS + 按 license_id 去重回填，
      重复运行结果一致；
    * 回填行 ``result='unknown'``（历史不可回溯，不按剩余时长推算）；
      ``recorded_via='backfill'`` 与未来的实时写入区分。
    * **生产执行需单独批准**：批准后先备份，再运行本脚本，再跑校验。

用法（在 license_server/ 目录）：
    python migrate_admin2.py            # 执行迁移 + 校验
    python migrate_admin2.py --check    # 只校验，不写入
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import config  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS redemption_events (
    id INTEGER PRIMARY KEY,
    license_id INTEGER NOT NULL UNIQUE,
    authorization_id INTEGER,
    device_id VARCHAR(64),
    event_type VARCHAR(16) NOT NULL,          -- 'first' | 'renewal'
    duration_days INTEGER NOT NULL,
    redeemed_at DATETIME NOT NULL,            -- naive UTC（与 licenses.redeemed_at 一致）
    auth_created_at DATETIME,                 -- 兑换时授权的创建时间
    auth_expires_before DATETIME,             -- 兑换前到期时间；回填行为 NULL（不可回溯）
    auth_expires_after DATETIME,              -- 兑换后到期时间；回填行为 NULL
    result VARCHAR(16),                       -- 'new' | 'extend' | 'reset' | 'unknown'
    recorded_via VARCHAR(16) NOT NULL,        -- 'backfill' | 'live'
    recorded_at DATETIME NOT NULL
)
"""

BACKFILL = """
INSERT OR IGNORE INTO redemption_events
    (license_id, authorization_id, device_id, event_type, duration_days,
     redeemed_at, auth_created_at, auth_expires_before, auth_expires_after,
     result, recorded_via, recorded_at)
SELECT
    l.id,
    l.authorization_id,
    a.device_id,
    CASE WHEN EXISTS (
        SELECT 1 FROM licenses f
        WHERE f.authorization_id = l.authorization_id AND f.state = 'REDEEMED'
          AND (f.redeemed_at < l.redeemed_at
               OR (f.redeemed_at = l.redeemed_at AND f.id < l.id))
    ) THEN 'renewal' ELSE 'first' END,
    l.duration_days,
    l.redeemed_at,
    a.created_at,
    NULL,
    NULL,
    CASE WHEN EXISTS (
        SELECT 1 FROM licenses f
        WHERE f.authorization_id = l.authorization_id AND f.state = 'REDEEMED'
          AND (f.redeemed_at < l.redeemed_at
               OR (f.redeemed_at = l.redeemed_at AND f.id < l.id))
    ) THEN 'unknown' ELSE 'new' END,
    'backfill',
    CURRENT_TIMESTAMP
FROM licenses l
LEFT JOIN authorizations a ON a.id = l.authorization_id
WHERE l.state = 'REDEEMED' AND l.redeemed_at IS NOT NULL
"""

# Phase 3 追加：统计查询性能索引（幂等；均为纯增量，不影响既有写入语义）
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS ix_licenses_created_at ON licenses (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_licenses_redeemed_at ON licenses (redeemed_at)",
    "CREATE INDEX IF NOT EXISTS ix_licenses_state ON licenses (state)",
    "CREATE INDEX IF NOT EXISTS ix_licenses_auth_state_redeemed "
    "ON licenses (authorization_id, state, redeemed_at)",
    # 设备列表的“最近活跃”相关子查询：按设备取 MAX(last_seen_at)
    "CREATE INDEX IF NOT EXISTS ix_device_daily_active_device_seen "
    "ON device_daily_active (device_id, last_seen_at)",
]

VERIFY_TOTAL = "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND redeemed_at IS NOT NULL"
VERIFY_EVENTS_ALL = "SELECT COUNT(*) FROM redemption_events"
VERIFY_EVENTS_BACKFILL = "SELECT COUNT(*) FROM redemption_events WHERE recorded_via='backfill'"
VERIFY_TYPES = "SELECT event_type, COUNT(*) FROM redemption_events WHERE recorded_via='backfill' GROUP BY event_type"


def main(check_only: bool = False) -> int:
    print(f"数据库：{engine.url}")
    # 与服务器启动行为一致（app/main.py:29）：补齐缺失表。纯增量、幂等，
    # 已存在的表与数据不受影响；全新空库也能直接执行本脚本。
    from database import Base
    import models  # noqa: F401  确保 4 张表均已注册
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if check_only:
            total = db.execute(text(VERIFY_TOTAL)).scalar()
            events = db.execute(text(VERIFY_EVENTS_ALL)).scalar()
            backfill = db.execute(text(VERIFY_EVENTS_BACKFILL)).scalar()
            ok = events >= total
            print(f"[check] REDEEMED 卡密 {total} 条，事件总数 {events} 条"
                  f"（回填 {backfill} + 实时 {events - backfill}）→ "
                  + ("一致" if ok else "缺失（需要执行迁移）"))
            for t, c in db.execute(text(VERIFY_TYPES)):
                print(f"  - {t}: {c}")
            return 0 if ok else 1

        db.execute(text(DDL))
        for ddl in INDEX_DDL:
            db.execute(text(ddl))
        db.commit()
        print("[migrate] redemption_events 表与统计索引已就绪（IF NOT EXISTS，幂等）")

        db.execute(text(BACKFILL))
        db.commit()
        print("[migrate] 历史回填完成（INSERT OR IGNORE，可重复执行）")

        total = db.execute(text(VERIFY_TOTAL)).scalar()
        events = db.execute(text(VERIFY_EVENTS_ALL)).scalar()
        backfill = db.execute(text(VERIFY_EVENTS_BACKFILL)).scalar()
        print(f"[verify] REDEEMED 卡密 {total} 条，事件总数 {events} 条"
              f"（回填 {backfill} + 实时 {events - backfill}）→ "
              + ("一致 ✓" if events >= total else "不一致 ✗（请勿部署，先排查）"))
        for t, c in db.execute(text(VERIFY_TYPES)):
            print(f"  - {t}: {c}")
        print("[done] 迁移完成。如需回滚：DROP TABLE redemption_events;")
        return 0 if events >= total else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(check_only="--check" in sys.argv))
