# -*- coding: utf-8 -*-
"""
LiveLens Admin 2.0 — 生产数据库只读核查脚本（阶段 A）

用途：在生产主机上对 license_server SQLite 数据库做只读体检。
约束：仅 SELECT / PRAGMA（只读类），不输出明文 KEY、哈希全值或任何 .env 凭证；
     不写入、不迁移、不重启服务。WAL 模式下读者不阻塞写者。

用法（生产主机上，DB 路径按实际部署调整）：
    python3 prod_check_readonly.py /path/to/license_server.db
    python3 prod_check_readonly.py            # 自动按常见路径探测
"""
import json
import os
import sqlite3
import sys

BUSY_TIMEOUT = 5.0
CANDIDATE_PATHS = [
    "license_server.db",
    "/root/license_server/license_server.db",
    "/opt/license_server/license_server.db",
    "/srv/license_server/license_server.db",
]


def find_db():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for p in CANDIDATE_PATHS:
        if os.path.isfile(p):
            return p
    return None


def q(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def main():
    db_path = find_db()
    if not db_path:
        print(json.dumps({"error": "未找到 license_server.db，请以参数形式传入路径"}, ensure_ascii=False))
        return 2

    out = {"db_path": db_path, "db_size_bytes": os.path.getsize(db_path)}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
    cur = con.cursor()

    # ---- schema 与迁移状态 ----
    out["journal_mode"] = q(cur, "PRAGMA journal_mode")[0][0]
    tables = [r[0] for r in q(cur, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    out["tables"] = tables
    out["schema"] = {}
    for t in ("licenses", "authorizations", "device_daily_active", "update_releases", "redemption_events"):
        if t in tables:
            cols = q(cur, f"PRAGMA table_info({t})")
            out["schema"][t] = [{"name": c[1], "type": c[2], "notnull": c[3], "dflt": c[4]} for c in cols]
            out["schema"][t + "__indexes"] = [
                {"name": r[1], "unique": bool(r[2])}
                for r in q(cur, f"PRAGMA index_list({t})")
            ]

    def count(t):
        return q(cur, f"SELECT COUNT(*) FROM {t}")[0][0] if t in tables else None

    out["row_counts"] = {t: count(t) for t in (
        "licenses", "authorizations", "device_daily_active", "update_releases")}

    # ---- duration_days 分布（套餐口径核实）----
    if "licenses" in tables:
        out["duration_days_distribution"] = [
            {"duration_days": r[0], "state": r[1], "cnt": r[2]}
            for r in q(cur, "SELECT duration_days, state, COUNT(*) FROM licenses GROUP BY duration_days, state ORDER BY duration_days")
        ]
        # 关联完整性
        out["integrity"] = {
            "redeemed_without_authorization_id": q(cur,
                "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND authorization_id IS NULL")[0][0],
            "redeemed_without_redeemed_at": q(cur,
                "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND redeemed_at IS NULL")[0][0],
            "orphan_authorization_id": q(cur,
                "SELECT COUNT(*) FROM licenses l LEFT JOIN authorizations a ON l.authorization_id=a.id "
                "WHERE l.authorization_id IS NOT NULL AND a.id IS NULL")[0][0],
            "duplicate_device_rows": q(cur,
                "SELECT COUNT(*) FROM (SELECT device_id FROM authorizations GROUP BY device_id HAVING COUNT(*)>1)")[0][0],
            "authorizations_total": count("authorizations"),
            "distinct_devices_in_dau": q(cur, "SELECT COUNT(DISTINCT device_id) FROM device_daily_active")[0][0]
                if "device_daily_active" in tables else None,
        }
        # 时间覆盖范围（UTC 存储 → 北京时间 = UTC+8）
        out["time_coverage"] = {
            "license_created": q(cur, "SELECT MIN(created_at), MAX(created_at) FROM licenses")[0],
            "license_redeemed": q(cur, "SELECT MIN(redeemed_at), MAX(redeemed_at) FROM licenses WHERE redeemed_at IS NOT NULL")[0],
            "auth_created": q(cur, "SELECT MIN(created_at), MAX(created_at) FROM authorizations")[0]
                if "authorizations" in tables else None,
            "auth_activated": q(cur, "SELECT MIN(activated_at), MAX(activated_at) FROM authorizations WHERE activated_at IS NOT NULL")[0]
                if "authorizations" in tables else None,
        }
        # 每日首次激活是否可按北京时间计算：authorizations.created_at 与
        # 最早 redeemed_at 的日期（北京时区）差异统计
        if "authorizations" in tables:
            out["first_activation_check"] = {
                "auth_created_same_utc_day_as_first_redeem": q(cur,
                    "SELECT COUNT(*) FROM authorizations a WHERE a.created_at = ("
                    "  SELECT MIN(l.redeemed_at) FROM licenses l WHERE l.authorization_id = a.id)")[0][0],
                "auth_created_null": q(cur, "SELECT COUNT(*) FROM authorizations WHERE created_at IS NULL")[0][0],
                "activated_at_null": q(cur, "SELECT COUNT(*) FROM authorizations WHERE activated_at IS NULL")[0][0],
            }
        # 异常日期：redeemed_at 早于 created_at
        out["anomalies"] = {
            "redeemed_before_created": q(cur,
                "SELECT COUNT(*) FROM licenses WHERE redeemed_at IS NOT NULL AND redeemed_at < created_at")[0][0],
            "expires_before_created": q(cur,
                "SELECT COUNT(*) FROM authorizations WHERE expires_at < created_at")[0][0]
                if "authorizations" in tables else None,
        }

    # ---- DAU 覆盖（active_date 已按北京时间日切）----
    if "device_daily_active" in tables:
        out["dau_coverage"] = {
            "range": q(cur, "SELECT MIN(active_date), MAX(active_date) FROM device_daily_active")[0],
            "days_with_rows": q(cur, "SELECT COUNT(DISTINCT active_date) FROM device_daily_active")[0][0],
            "future_dates": q(cur, "SELECT COUNT(*) FROM device_daily_active WHERE active_date > date('now', '+8 hours')")[0][0],
            "unknown_devices": q(cur,
                "SELECT COUNT(DISTINCT d.device_id) FROM device_daily_active d "
                "LEFT JOIN authorizations a ON d.device_id = a.device_id WHERE a.id IS NULL")[0][0],
        }

    # ---- 抽样（脱敏：哈希仅前 8 位）----
    if "licenses" in tables:
        out["sample"] = [
            {"id": r[0], "hash8": (r[1] or "")[:8], "days": r[2], "state": r[3],
             "created_at": r[4], "redeemed_at": r[5], "auth_id": r[6]}
            for r in q(cur, "SELECT id, key_hash, duration_days, state, created_at, redeemed_at, authorization_id "
                            "FROM licenses ORDER BY id DESC LIMIT 5")
        ]

    con.close()
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
