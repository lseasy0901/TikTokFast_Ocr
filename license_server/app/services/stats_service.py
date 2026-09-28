# -*- coding: utf-8 -*-
"""
admin2 统计服务 — 全部统计口径的唯一定义（Phase 2 阶段 B/C）。

口径约定（与 AUDIT_REPORT.md §4、PHASE2 阶段 C 要求一致）：

* 存储侧全部为 naive UTC（见 models.py 约定）；本模块只在**查询层**换算
  北京时间日切：SQLite ``date(col, '+8 hours')``。
* 首次激活设备数：按 ``authorizations.created_at``（创建即首次成功兑换，
  由 redemption_service.find_or_create_authorization 保证；device_id 唯一约束
  保证同一设备只计一次）。
* 兑换次数：``state='REDEEMED'`` 的 License 行数，按 ``redeemed_at`` 归档。
* 续费次数：兑换发生时该授权已存在 —— 确定性判定规则：
  对 License L（authorization_id=A），若存在同授权下更早的
  ``(redeemed_at, id)`` 兑换记录，则 L 为续费；最早的一条（含平局取更小 id）
  为首次激活。该规则不依赖时钟精度，可在历史数据上稳定重放。
* 授权变更语义（累加 vs 重置）：历史记录不含兑换前 expires_at，**无法回溯**，
  一律标记 ``unknown``，不按剩余时长推算、不伪造。
* 套餐分类：以该条兑换记录自身 ``duration_days`` 映射
  1→天卡 30→月卡 180→半年卡 365→年卡，其余 →「其他套餐」（不得静默归类）。
  设备的最新套餐取其最新一条兑换记录的 duration_days。
* DAU：``device_daily_active`` 已在写入时按 Asia/Shanghai 日切，直接计数。
* 有效授权设备：``state='ACTIVE'`` 且 ``expires_at > now``。
"""

import json
from datetime import datetime, timedelta, timezone, date

from sqlalchemy import text

# ----------------------------------------------------------------------
# 常量与小型工具
# ----------------------------------------------------------------------

#: naive UTC → 北京时间的 SQLite 修饰符。
BJ_OFFSET_MODIFIER = "+8 hours"

PLAN_BY_DAYS = {1: "d1", 30: "m30", 180: "h180", 365: "y365"}
PLAN_NAME = {
    "d1": "天卡",
    "m30": "月卡",
    "h180": "半年卡",
    "y365": "年卡",
    "other": "其他套餐",
}
PLAN_ORDER = ["d1", "m30", "h180", "y365", "other"]

EXPIRING_SOON_DAYS = 7


def plan_key_of(duration_days) -> str:
    try:
        return PLAN_BY_DAYS.get(int(duration_days), "other")
    except (TypeError, ValueError):
        return "other"


def plan_name_of(duration_days) -> str:
    return PLAN_NAME[plan_key_of(duration_days)]


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_utc(value) -> datetime | None:
    """解析数据库中的 naive UTC 时间字符串（兼容秒/微秒、date 对象）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # SQLite 可能输出 "YYYY-MM-DD  HH:MM:SS"（双空格）等历史脏格式
    s = s.replace("  ", " ")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def to_bj(value, with_seconds: bool = False) -> str | None:
    """naive UTC 字符串 → 北京时间显示串（仅展示层换算，不改存储）。"""
    dt = parse_utc(value)
    if dt is None:
        return None
    local = dt + timedelta(hours=8)
    return local.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def bj_date_str(value) -> str | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def empty_plan_map() -> dict:
    return {k: 0 for k in PLAN_ORDER}


# ----------------------------------------------------------------------
# 数据总览
# ----------------------------------------------------------------------

def overview(db, days: int = 30) -> dict:
    """总览页数据：今日 KPI、近期趋势、最近兑换、即将到期。"""
    days = max(1, min(int(days), 60))
    rows = _daily_rows(db, days, None, None)
    today = rows[-1] if rows else None
    yesterday = rows[-2] if len(rows) >= 2 else None

    active, expiring, expired, total = _device_state_counts(db)
    recent = recent_redemptions(db, limit=8)
    expiring_soon = expiring_devices(db, limit=5)
    plan30 = _plan_totals(db, days)

    return {
        "generated_at_bj": to_bj(now_utc(), with_seconds=True),
        "today": today,
        "yesterday": yesterday,
        "devices": {
            "active": active,
            "expiring": expiring,
            "expired": expired,
            "total": total,
        },
        "trend": rows,
        "plan_totals_window": plan30,
        "recent_redemptions": recent,
        "expiring_soon": expiring_soon,
    }


def _plan_totals(db, days: int) -> dict:
    """近 N 天（北京时间）各套餐兑换量。"""
    sql = text(f"""
        SELECT {plan_case_sql('l.duration_days')} AS plan_key, COUNT(*) AS cnt
        FROM licenses l
        WHERE l.state = 'REDEEMED' AND l.redeemed_at IS NOT NULL
          AND {bj_date_sql('l.redeemed_at')} >= date('now', '{BJ_OFFSET_MODIFIER}', :offset_days)
        GROUP BY plan_key
    """)
    params = {f"offset_days": f"-{days - 1} days"}
    result = empty_plan_map()
    for key, cnt in db.execute(sql, params):
        result[key] = int(cnt)
    return result


def recent_redemptions(db, limit: int = 8) -> list[dict]:
    sql = text(f"""
        SELECT l.id, l.key_hash, l.duration_days, l.redeemed_at, l.authorization_id,
               a.device_id, e.result AS event_result
        FROM licenses l
        LEFT JOIN authorizations a ON a.id = l.authorization_id
        LEFT JOIN redemption_events e ON e.license_id = l.id
        WHERE l.state = 'REDEEMED' AND l.redeemed_at IS NOT NULL
        ORDER BY l.redeemed_at DESC, l.id DESC
        LIMIT :limit
    """)
    items = []
    for r in db.execute(sql, {"limit": limit}):
        items.append({
            "license_id": r.id,
            "hash8": (r.key_hash or "")[:8] + "…",
            "plan_key": plan_key_of(r.duration_days),
            "plan_name": plan_name_of(r.duration_days),
            "redeemed_at_bj": to_bj(r.redeemed_at),
            "device_id": r.device_id,
            "type": "first" if is_first_redemption(db, r.id) else "renewal",
            "result": _result_label("first" if is_first_redemption(db, r.id) else "renewal", r.event_result),
        })
    return items


def expiring_devices(db, limit: int = 5) -> list[dict]:
    now = now_utc()
    horizon = now + timedelta(days=EXPIRING_SOON_DAYS)
    sql = text("""
        SELECT a.id, a.device_id, a.expires_at, a.state,
               (SELECT MAX(d.last_seen_at) FROM device_daily_active d
                 WHERE d.device_id = a.device_id) AS last_seen
        FROM authorizations a
        WHERE a.state = 'ACTIVE' AND a.expires_at > :now AND a.expires_at <= :horizon
        ORDER BY a.expires_at ASC
        LIMIT :limit
    """)
    items = []
    for r in db.execute(sql, {"now": now, "horizon": horizon, "limit": limit}):
        remain = int((parse_utc(r.expires_at) - now).total_seconds() // 86400)
        items.append({
            "device_id": r.device_id,
            "expires_at_bj": to_bj(r.expires_at),
            "remain_days": max(remain, 0),
            "last_seen_bj": to_bj(r.last_seen),
        })
    return items


# ----------------------------------------------------------------------
# 按日统计（数据分析 / 总览趋势共用）
# ----------------------------------------------------------------------

def _daily_rows(db, days: int | None, date_from: str | None, date_to: str | None) -> list[dict]:
    """按北京时间日历日聚合的统计行（含空日补零，不含未来日）。

    date_from/date_to（北京时间 YYYY-MM-DD）优先于 days。
    """
    today_bj = (now_utc() + timedelta(hours=8)).date()
    if date_from or date_to:
        if not (date_from and date_to):
            raise ValueError("自定义区间必须同时提供开始与结束日期")
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("日期格式无效，应为 YYYY-MM-DD")
        if start > end:
            raise ValueError("开始日期晚于结束日期")
        if (end - start).days > 366:
            raise ValueError("自定义区间最长 366 天")
    else:
        n = max(1, min(int(days or 30), 60))
        start = today_bj - timedelta(days=n - 1)
        end = today_bj

    new_rows = _query_new_devices_by_day(db, start, end)
    redeems = _query_redemptions_by_day(db, start, end)
    dau_rows = _query_dau_by_day(db, start, end)

    out = []
    d = start
    while d <= end:
        key = d.strftime("%Y-%m-%d")
        rd = redeems.get(key, {})
        out.append({
            "date": key,
            "new_devices": int(new_rows.get(key, 0)),
            "first": rd.get("first", 0),
            "renew": rd.get("renew", 0),
            "d1": rd.get("plans", {}).get("d1", 0),
            "m30": rd.get("plans", {}).get("m30", 0),
            "h180": rd.get("plans", {}).get("h180", 0),
            "y365": rd.get("plans", {}).get("y365", 0),
            "other": rd.get("plans", {}).get("other", 0),
            "dau": dau_rows.get(key, 0),
        })
        d += timedelta(days=1)
    return out


def analytics(db, days: int | None = 30, date_from: str | None = None, date_to: str | None = None) -> dict:
    rows = _daily_rows(db, days, date_from, date_to)
    totals = {
        "new_devices": sum(r["new_devices"] for r in rows),
        "first": sum(r["first"] for r in rows),
        "renew": sum(r["renew"] for r in rows),
        "d1": sum(r["d1"] for r in rows),
        "m30": sum(r["m30"] for r in rows),
        "h180": sum(r["h180"] for r in rows),
        "y365": sum(r["y365"] for r in rows),
        "other": sum(r["other"] for r in rows),
    }
    totals["redeem_total"] = totals["first"] + totals["renew"]
    dau_values = [r["dau"] for r in rows]
    totals["dau_avg"] = round(sum(dau_values) / len(dau_values)) if dau_values else 0
    totals["dau_peak"] = max(dau_values) if dau_values else 0
    totals["days"] = len(rows)
    return {"rows": rows, "totals": totals}


def daily_csv(db, days, date_from, date_to) -> list[list]:
    data = analytics(db, days, date_from, date_to)
    header = ["日期(北京时间)", "新增设备", "首次激活", "续费",
              "天卡", "月卡", "半年卡", "年卡", "其他套餐", "DAU"]
    rows = [[r["date"], r["new_devices"], r["first"], r["renew"],
             r["d1"], r["m30"], r["h180"], r["y365"], r["other"], r["dau"]]
            for r in data["rows"]]
    t = data["totals"]
    rows.append(["合计", t["new_devices"], t["first"], t["renew"],
                 t["d1"], t["m30"], t["h180"], t["y365"], t["other"], t["dau_avg"]])
    return [header] + rows


def _query_new_devices_by_day(db, start: date, end: date) -> dict:
    """每日首次激活设备数：authorizations.created_at 按北京时间归档。"""
    sql = text(f"""
        SELECT {bj_date_sql('a.created_at')} AS d, COUNT(*) AS cnt
        FROM authorizations a
        WHERE {bj_date_sql('a.created_at')} >= :start AND {bj_date_sql('a.created_at')} <= :end
        GROUP BY d
    """)
    return {r.d: int(r.cnt) for r in db.execute(sql, {"start": start.isoformat(), "end": end.isoformat()})}


def _query_redemptions_by_day(db, start: date, end: date) -> dict:
    """每日兑换：first / renew / 各套餐，均按北京时间归档。"""
    sql = text(f"""
        SELECT {bj_date_sql('l.redeemed_at')} AS d,
               {plan_case_sql('l.duration_days')} AS plan_key,
               CASE WHEN EXISTS (
                   SELECT 1 FROM licenses f
                   WHERE f.authorization_id = l.authorization_id
                     AND f.state = 'REDEEMED'
                     AND (f.redeemed_at < l.redeemed_at
                          OR (f.redeemed_at = l.redeemed_at AND f.id < l.id))
               ) THEN 1 ELSE 0 END AS is_renewal,
               COUNT(*) AS cnt
        FROM licenses l
        WHERE l.state = 'REDEEMED' AND l.redeemed_at IS NOT NULL
          AND {bj_date_sql('l.redeemed_at')} >= :start AND {bj_date_sql('l.redeemed_at')} <= :end
        GROUP BY d, plan_key, is_renewal
    """)
    out: dict[str, dict] = {}
    for r in db.execute(sql, {"start": start.isoformat(), "end": end.isoformat()}):
        slot = out.setdefault(r.d, {"first": 0, "renew": 0, "plans": empty_plan_map()})
        if r.is_renewal:
            slot["renew"] += int(r.cnt)
        else:
            slot["first"] += int(r.cnt)
        slot["plans"][r.plan_key] += int(r.cnt)
    return out


def _query_dau_by_day(db, start: date, end: date) -> dict:
    sql = text("""
        SELECT active_date AS d, COUNT(*) AS cnt
        FROM device_daily_active
        WHERE active_date >= :start AND active_date <= :end
        GROUP BY active_date
    """)
    return {str(r.d): int(r.cnt) for r in db.execute(sql, {"start": start.isoformat(), "end": end.isoformat()})}


# ----------------------------------------------------------------------
# 卡密
# ----------------------------------------------------------------------

def key_counts(db) -> dict:
    sql = text("SELECT state, COUNT(*) AS cnt FROM licenses GROUP BY state")
    by_state = {r.state: int(r.cnt) for r in db.execute(sql)}
    return {
        "total": sum(by_state.values()),
        "unused": by_state.get("UNUSED", 0),
        "redeemed": by_state.get("REDEEMED", 0),
        "revoked": by_state.get("REVOKED", 0),
    }


def keys_page(db, state: str | None, plan: str | None, q: str | None,
              page: int, page_size: int = 10) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    where, params = ["1=1"], {}
    if state in ("UNUSED", "REDEEMED", "REVOKED"):
        where.append("l.state = :state")
        params["state"] = state
    if plan:
        # plan 来自白名单字典，内联安全；其余任何 plan 值都归入「其他」桶
        valid = {"d1": "(1)", "m30": "(30)", "h180": "(180)", "y365": "(365)"}
        if plan in valid:
            where.append(f"l.duration_days IN {valid[plan]}")
        else:
            where.append("l.duration_days NOT IN (1, 30, 180, 365)")
    if q:
        where.append("(l.key_hash LIKE :like OR l.id = :qid OR l.id IN "
                     "(SELECT a2.id FROM authorizations a2 WHERE a2.device_id LIKE :like))")
        params["like"] = f"{q}%"
        try:
            params["qid"] = int(q.lstrip("#"))
        except ValueError:
            params["qid"] = -1

    where_sql = " AND ".join(where)
    total = db.execute(text(f"SELECT COUNT(*) FROM licenses l WHERE {where_sql}"), params).scalar() or 0
    sql = text(f"""
        SELECT l.id, l.key_hash, l.duration_days, l.state, l.features,
               l.created_at, l.redeemed_at, l.authorization_id, a.device_id
        FROM licenses l
        LEFT JOIN authorizations a ON a.id = l.authorization_id
        WHERE {where_sql}
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT :limit OFFSET :offset
    """)
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    items = []
    for r in db.execute(sql, params):
        items.append({
            "id": r.id,
            "hash": r.key_hash,
            "duration_days": r.duration_days,
            "plan_key": plan_key_of(r.duration_days),
            "plan_name": plan_name_of(r.duration_days),
            "state": r.state.lower(),  # unused / redeemed / revoked（前端徽章口径）
            "features": parse_features(r.features),
            "created_at_bj": to_bj(r.created_at),
            "redeemed_at_bj": to_bj(r.redeemed_at),
            "authorization_id": r.authorization_id,
            "device_id": r.device_id,
        })
    return {"total": int(total), "page": page, "page_size": page_size, "items": items}


def parse_features(features_json) -> list[str]:
    if not features_json:
        return []
    try:
        data = json.loads(features_json)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


# ----------------------------------------------------------------------
# KEY 反向查询（明文 → 现有哈希算法 → 指纹检索）
# ----------------------------------------------------------------------

def lookup_license(db, license_key: str) -> dict:
    """使用与 Business Layer 完全一致的 SHA-256 哈希检索。

    不落库、不写日志、不缓存输入。仅去除首尾空白（不改变字符内容——
    Key 为 Base64URL 大小写敏感，做 lower/upper 会改变哈希导致查不到）。
    """
    from services.license_service import generate_key_hash

    cleaned = (license_key or "").strip()
    if not cleaned:
        return {"status": "invalid_input"}
    key_hash = generate_key_hash(cleaned)
    row = db.execute(text(
        "SELECT id, key_hash, duration_days, state, features, created_at, redeemed_at, authorization_id "
        "FROM licenses WHERE key_hash = :h"
    ), {"h": key_hash}).first()
    if row is None:
        return {"status": "not_found", "hash8": key_hash[:8] + "…"}

    device = None
    if row.authorization_id:
        arow = db.execute(text(
            "SELECT device_id, state, expires_at, created_at, activated_at "
            "FROM authorizations WHERE id = :i"
        ), {"i": row.authorization_id}).first()
        if arow:
            device = {
                "device_id": arow.device_id,
                "state": arow.state.lower(),
                "expires_at_bj": to_bj(arow.expires_at),
                "activated_at_bj": to_bj(arow.activated_at),
            }
    return {
        "status": row.state.lower(),  # unused / redeemed / revoked
        "hash8": key_hash[:8] + "…",
        "license": {
            "id": row.id,
            "duration_days": row.duration_days,
            "plan_key": plan_key_of(row.duration_days),
            "plan_name": plan_name_of(row.duration_days),
            "state": row.state.lower(),
            "features": parse_features(row.features),
            "created_at_bj": to_bj(row.created_at),
            "redeemed_at_bj": to_bj(row.redeemed_at),
            "authorization_id": row.authorization_id,
        },
        "device": device,
    }


# ----------------------------------------------------------------------
# 设备授权
# ----------------------------------------------------------------------

def _device_state(expires_at, now) -> str:
    exp = parse_utc(expires_at)
    if exp is None or exp <= now:
        return "expired"
    if exp <= now + timedelta(days=EXPIRING_SOON_DAYS):
        return "expiring"
    return "active"


def _device_state_counts(db) -> tuple[int, int, int, int]:
    now = now_utc()
    horizon = now + timedelta(days=EXPIRING_SOON_DAYS)
    row = db.execute(text(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN state='ACTIVE' AND expires_at > :h THEN 1 ELSE 0 END) AS active,"
        " SUM(CASE WHEN state='ACTIVE' AND expires_at > :now AND expires_at <= :h THEN 1 ELSE 0 END) AS expiring"
        " FROM authorizations"
    ), {"now": now, "h": horizon}).first()
    total = int(row.total or 0)
    active = int(row.active or 0)
    expiring = int(row.expiring or 0)
    expired = total - active
    return active, expiring, expired, total


def devices_page(db, state: str | None, q: str | None, sort: str | None,
                 page: int, page_size: int = 10) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    now = now_utc()
    horizon = now + timedelta(days=EXPIRING_SOON_DAYS)
    sql = """
        SELECT a.id, a.device_id, a.state AS auth_state, a.expires_at, a.created_at, a.activated_at,
          (SELECT MAX(d.last_seen_at) FROM device_daily_active d WHERE d.device_id = a.device_id) AS last_seen,
          (SELECT COUNT(*) FROM licenses l WHERE l.authorization_id = a.id AND l.state='REDEEMED') AS redeem_cnt,
          (SELECT l2.duration_days FROM licenses l2
            WHERE l2.authorization_id = a.id AND l2.state='REDEEMED'
            ORDER BY l2.redeemed_at DESC, l2.id DESC LIMIT 1) AS latest_days,
          (SELECT l3.features FROM licenses l3
            WHERE l3.authorization_id = a.id AND l3.state='REDEEMED'
            ORDER BY l3.redeemed_at DESC, l3.id DESC LIMIT 1) AS latest_features
        FROM authorizations a
    """
    filters, params = [], {}
    if q:
        filters.append("a.device_id LIKE :q")
        params["q"] = f"{q}%"
    sql_str = str(sql)
    if filters:
        sql_str += " WHERE " + " AND ".join(filters)
    rows = db.execute(text(sql_str), params).fetchall()

    items = []
    for r in rows:
        st = _device_state(r.expires_at, now)
        exp = parse_utc(r.expires_at)
        remain = int((exp - now).total_seconds() // 86400) if exp else None
        items.append({
            "device_id": r.device_id,
            "state": st,
            "expires_at_bj": to_bj(r.expires_at),
            "first_activation_bj": to_bj(r.created_at),
            "activated_at_bj": to_bj(r.activated_at),
            "last_seen_bj": to_bj(r.last_seen),
            "remain_days": remain,
            "redemptions": int(r.redeem_cnt or 0),
            "latest_plan_key": plan_key_of(r.latest_days),
            "latest_plan_name": plan_name_of(r.latest_days),
            "features": parse_features(r.latest_features),
        })

    if state in ("active", "expiring", "expired"):
        items = [x for x in items if x["state"] == state]
    sorters = {
        "remain_asc": lambda x: (x["remain_days"] is None, x["remain_days"] or 0),
        "remain_desc": lambda x: (x["remain_days"] is None, -(x["remain_days"] or 0)),
        "recent": lambda x: x["last_seen_bj"] or "",
    }
    items.sort(key=sorters.get(sort, sorters["remain_asc"]))

    total = len(items)
    start = (page - 1) * page_size
    counts = {"active": 0, "expiring": 0, "expired": 0}
    for x in items:
        counts[x["state"]] += 1
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": items[start:start + page_size],
        "counts": {**counts, "total": total},
    }


def device_detail(db, device_id: str) -> dict | None:
    row = db.execute(text(
        "SELECT a.* FROM authorizations a WHERE a.device_id = :d"
    ), {"d": device_id}).first()
    if row is None:
        return None
    now = now_utc()
    exp = parse_utc(row.expires_at)
    remain = int((exp - now).total_seconds() // 86400) if exp else None
    lic_rows = db.execute(text("""
        SELECT l.id, l.key_hash, l.duration_days, l.features, l.redeemed_at,
               e.result AS event_result
        FROM licenses l
        LEFT JOIN redemption_events e ON e.license_id = l.id
        WHERE l.authorization_id = :i AND l.state='REDEEMED'
        ORDER BY l.redeemed_at ASC, l.id ASC
    """), {"i": row.id}).fetchall()

    features: set[str] = set()
    history = []
    for i, lr in enumerate(lic_rows):
        features.update(parse_features(lr.features))
        history.append({
            "license_id": lr.id,
            "hash8": (lr.key_hash or "")[:8] + "…",
            "plan_name": plan_name_of(lr.duration_days),
            "time_bj": to_bj(lr.redeemed_at, with_seconds=True),
            # 首条（按 (redeemed_at, id) 最小）为首次激活；其余续费。
            # 实时事件可给出准确结果；历史回填/缺失行保持「不可回溯」。
            "type": "first" if i == 0 else "renewal",
            "result": _result_label("first" if i == 0 else "renewal", lr.event_result),
        })
    return {
        "device_id": row.device_id,
        "auth_state": row.state.lower(),
        "state": _device_state(row.expires_at, now),
        "expires_at_bj": to_bj(row.expires_at),
        "first_activation_bj": to_bj(row.created_at),
        "activated_at_bj": to_bj(row.activated_at),
        "remain_days": remain,
        "redemptions": len(lic_rows),
        "features": sorted(features),
        "history": history,
    }


# ----------------------------------------------------------------------
# 兑换记录
# ----------------------------------------------------------------------

def _result_label(event_type: str, event_result) -> str:
    """兑换结果展示标签：优先采用实时事件（extend/reset 可区分），
    回填或缺失（历史续费）标记为不可回溯，不推算。"""
    if event_type == "first":
        return "新建授权"
    if event_result == "extend":
        return "有效期累加"
    if event_result == "reset":
        return "过期重置"
    return "累加或重置（历史不可回溯）"


def redemption_counts(db) -> dict:
    total = db.execute(text(
        "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND redeemed_at IS NOT NULL"
    )).scalar() or 0
    renew = db.execute(text("""
        SELECT COUNT(*) FROM licenses l
        WHERE l.state='REDEEMED' AND l.redeemed_at IS NOT NULL AND l.authorization_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM licenses f
                      WHERE f.authorization_id = l.authorization_id AND f.state='REDEEMED'
                        AND (f.redeemed_at < l.redeemed_at
                             OR (f.redeemed_at = l.redeemed_at AND f.id < l.id)))
    """)).scalar() or 0
    return {"total": int(total), "renew": int(renew), "first": int(total) - int(renew)}


def redemptions_page(db, type_: str | None, plan: str | None, q: str | None,
                     date_from: str | None, date_to: str | None,
                     page: int, page_size: int = 12) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    want_renewal: bool | None = None
    if type_ == "first":
        want_renewal = False
    elif type_ == "renewal":
        want_renewal = True

    where, params = ["l.state='REDEEMED'", "l.redeemed_at IS NOT NULL"], {}
    if want_renewal is True:
        where.append(_RENEWAL_EXISTS)
    elif want_renewal is False:
        where.append("NOT " + _RENEWAL_EXISTS)
    if plan:
        # plan 来自白名单字典，内联安全；其余任何 plan 值都归入「其他」桶
        valid = {"d1": "(1)", "m30": "(30)", "h180": "(180)", "y365": "(365)"}
        if plan in valid:
            where.append(f"l.duration_days IN {valid[plan]}")
        else:
            where.append("l.duration_days NOT IN (1, 30, 180, 365)")
    if q:
        where.append("(l.key_hash LIKE :like OR a.device_id LIKE :like)")
        params["like"] = f"{q}%"
    if date_from:
        where.append(f"{bj_date_sql('l.redeemed_at')} >= :df")
        params["df"] = date_from
    if date_to:
        where.append(f"{bj_date_sql('l.redeemed_at')} <= :dt")
        params["dt"] = date_to

    where_sql = " AND ".join(where)
    base_from = (" FROM licenses l"
                 " LEFT JOIN authorizations a ON a.id = l.authorization_id"
                 " LEFT JOIN redemption_events e ON e.license_id = l.id"
                 " WHERE " + where_sql)
    total = db.execute(text("SELECT COUNT(*)" + base_from), params).scalar() or 0
    sql = text(
        "SELECT l.id, l.key_hash, l.duration_days, l.redeemed_at, l.authorization_id,"
        " a.device_id, e.result AS event_result"
        + base_from + " ORDER BY l.redeemed_at DESC, l.id DESC LIMIT :limit OFFSET :offset")
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    items = []
    for r in db.execute(sql, params):
        is_renewal = is_first_redemption(db, r.id) is False
        items.append({
            "license_id": r.id,
            "hash8": (r.key_hash or "")[:8] + "…",
            "plan_key": plan_key_of(r.duration_days),
            "plan_name": plan_name_of(r.duration_days),
            "redeemed_at_bj": to_bj(r.redeemed_at, with_seconds=True),
            "device_id": r.device_id,
            "type": "renewal" if is_renewal else "first",
            "result": _result_label("renewal" if is_renewal else "first", r.event_result),
        })
    counts = redemption_counts(db)
    return {"total": int(total), "page": page, "page_size": page_size,
            "items": items, "counts": counts}


_RENEWAL_EXISTS = """EXISTS (SELECT 1 FROM licenses f
    WHERE f.authorization_id = l.authorization_id AND f.state='REDEEMED'
      AND (f.redeemed_at < l.redeemed_at
           OR (f.redeemed_at = l.redeemed_at AND f.id < l.id)))"""


def is_first_redemption(db, license_id: int) -> bool:
    """该 License 是否为其授权下的首次兑换（确定性平局规则：更小 id 优先）。"""
    row = db.execute(text(f"""
        SELECT
          EXISTS (SELECT 1 FROM licenses f
                  WHERE f.authorization_id = l.authorization_id AND f.state='REDEEMED'
                    AND (f.redeemed_at < l.redeemed_at
                         OR (f.redeemed_at = l.redeemed_at AND f.id < l.id))) AS is_renewal
        FROM licenses l WHERE l.id = :i
    """), {"i": license_id}).first()
    return bool(row) and not row.is_renewal


def redemptions_csv(db, **filters) -> list[list]:
    data = redemptions_page(db, page=1, page_size=200, **filters)
    header = ["兑换时间(北京时间)", "类型", "套餐", "KEY指纹(前8位)", "设备ID", "授权变更"]
    rows = [[r["redeemed_at_bj"], "首次激活" if r["type"] == "first" else "续费",
             r["plan_name"], r["hash8"], r["device_id"] or "—", r["result"]]
            for r in data["items"]]
    return [header] + rows


# ----------------------------------------------------------------------
# SQL 片段
# ----------------------------------------------------------------------

def bj_date_sql(col: str) -> str:
    return f"date({col}, '{BJ_OFFSET_MODIFIER}')"


def plan_case_sql(col: str) -> str:
    return (f"CASE {col} WHEN 1 THEN 'd1' WHEN 30 THEN 'm30' "
            f"WHEN 180 THEN 'h180' WHEN 365 THEN 'y365' ELSE 'other' END")


# ----------------------------------------------------------------------
# CSV（防公式注入）
# ----------------------------------------------------------------------

def csv_safe_cell(value) -> str:
    """CSV 公式注入防护：以公式前导字符开头的单元格前置单引号。"""
    s = "" if value is None else str(value)
    if s.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + s
    return s
