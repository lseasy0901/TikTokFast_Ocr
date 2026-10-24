# -*- coding: utf-8 -*-
"""修复「stale-ACTIVE 错误累加」造成的历史坏授权（2026-10-24 事故）。

事故：授权状态从不惰性翻转，设备过期后 state 仍是 ACTIVE。兑换走累加
分支 → 新有效期 = 旧过期日 + duration，仍然在过去。签出的凭据一到手就
过期。代码修复见 redemption_service.py（stale-ACTIVE 改为从当下重启）。

本脚本修**历史数据**：找出所有 `result='extend'` 且
`auth_expires_after <= redeemed_at` 的兑换事件（签发当下就已过期的授权
只可能是这个 bug 造成的），把对应授权修回「兑换时刻 + duration」。

安全约束：
    - 默认 **DRY-RUN**：只打印将修改的行，不写库。加 ``--apply`` 才落盘。
    - 仅当授权当前值仍等于坏值（auth_expires_after）时才修——若之后又有
      兑换/其它变更，只报告不自动改（留人工判断，避免覆盖叠加时长）。
    - 只 UPDATE authorizations 的 expires_at / activated_at / state；
      不动 licenses、不动事件流水（流水是审计事实，不改写）。

用法（生产主机上）：
    python3 repair_stale_active_renewals.py /var/lib/livelens/license_server.db
    python3 repair_stale_active_renewals.py /var/lib/livelens/license_server.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

FIND_SQL = """
SELECT e.id            AS event_id,
       e.license_id,
       e.authorization_id,
       e.device_id,
       e.duration_days,
       e.redeemed_at,
       e.auth_expires_before,
       e.auth_expires_after,
       a.state         AS current_state,
       a.expires_at    AS current_expires_at
  FROM redemption_events e
  JOIN authorizations a ON a.id = e.authorization_id
 WHERE e.result = 'extend'
   AND e.auth_expires_after <= e.redeemed_at
"""


def _parse(value: str) -> datetime:
    """SQLite 里的 UTC 时间字符串 → aware datetime（兼容带/不带 Z 后缀）。"""
    text = (value or "").strip().replace("Z", "+00:00").replace(" ", "T")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="license_server.db 路径")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库（默认只打印不修改）")
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=rw" if args.apply
                          else f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(FIND_SQL).fetchall()
        if not rows:
            print("未发现受影响的兑换事件（result=extend 且签发即过期）。")
            return 0

        print(f"发现 {len(rows)} 条受影响事件：\n")
        repaired = skipped = 0
        for r in rows:
            should = _parse(r["redeemed_at"]) + timedelta(
                days=int(r["duration_days"]))
            current = r["current_expires_at"]
            exact = (current is not None
                     and _parse(current) == _parse(r["auth_expires_after"]))
            print(f"event#{r['event_id']} license#{r['license_id']} "
                  f"device={r['device_id'][:16]}… "
                  f"坏值={r['auth_expires_after']} 应为={should.isoformat()} "
                  f"当前={current}")
            if not exact:
                print("  ↳ 当前值已被后续变更，跳过（需人工核对）")
                skipped += 1
                continue
            if not args.apply:
                continue
            con.execute(
                "UPDATE authorizations SET expires_at=?, activated_at=?, "
                "state='ACTIVE' WHERE id=?",
                (should.isoformat(), r["redeemed_at"], r["authorization_id"]),
            )
            repaired += 1

        if args.apply:
            con.commit()
            print(f"\n完成：修复 {repaired} 条，跳过 {skipped} 条（需人工核对）。")
        else:
            print(f"\nDRY-RUN：将修复 {len(rows) - skipped} 条，"
                  f"跳过 {skipped} 条。确认无误后加 --apply 执行。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
