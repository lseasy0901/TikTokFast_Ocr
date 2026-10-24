#!/usr/bin/env bash
# ============================================================
# LiveLens Admin 2.0 — 生产回滚（P3/P4 观察期触发时执行）
# 用法：MODE=code|table|disaster [BACKUP_ID=...] bash prod_rollback.sh MODE
#   code     R1 代码回退到 ROLLBACK_COMMIT（首选；无需动库）
#   table    R2 仅删除 redemption_events 新表（保留统计索引——对旧代码无害）
#   disaster R3 灾难恢复：必须显式指定唯一 BACKUP_ID，禁止通配符
# ============================================================
set -euo pipefail

MODE="${1:?用法: prod_rollback.sh code|table|disaster  [ROLLBACK_COMMIT 对 code 必填] [BACKUP_ID 对 disaster 必填]}"
DB=/var/lib/livelens/license_server.db
PY=/opt/livelens/venv/bin/python

case "$MODE" in
  code)
    ROLLBACK_COMMIT="${2:?R1 需要目标 commit（=部署前记录的 commit）}"
    echo "回退前 commit: $(cd /opt/livelens && git rev-parse HEAD)"
    cd /opt/livelens
    git checkout --detach "$ROLLBACK_COMMIT"
    git log -1 --oneline
    systemctl restart livelens-license
    sleep 2
    systemctl is-active livelens-license
    curl -s -o /dev/null -w "root %{http_code}\n" http://127.0.0.1:8000/
    curl -s -o /dev/null -w "admin/login %{http_code}\n" http://127.0.0.1:8000/admin/login
    echo "R1 完成：新表/新索引对旧代码无影响，保留在库中。"
    ;;
  table)
    "$PY" - <<'EOF'
import sqlite3
con = sqlite3.connect("/var/lib/livelens/license_server.db")
con.execute("DROP TABLE IF EXISTS redemption_events")
con.commit()
print("R2 完成：redemption_events 已删除（统计索引保留，对旧代码无害）")
EOF
    ;;
  disaster)
    BACKUP_ID="${2:?R3 必须指定唯一 BACKUP_ID（禁止通配符/最新备份推断）}"
    SRC=/var/lib/livelens/backups/$BACKUP_ID
    [ -d "$SRC" ] || { echo "FATAL: 备份 $BACKUP_ID 不存在：$SRC"; exit 1; }
    [ -f "$SRC/files/license_server.db" ] || { echo "FATAL: 该备份缺文件级副本"; exit 1; }
    # 恢复前先校验该备份自身的完整性
    "$PY" - "$SRC/license_server.db" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
ok = con.execute("PRAGMA integrity_check").fetchone()[0]
assert ok == "ok", f"备份完整性校验失败: {ok}"
print("备份 integrity_check ok")
EOF
    systemctl stop livelens-license
    ts=$(date +%Y%m%d-%H%M%S)
    mkdir -p /var/lib/livelens/pre-restore-$ts
    cp -a "$DB" "$DB-wal" "$DB-shm" /var/lib/livelens/pre-restore-$ts/ 2>/dev/null || cp -a "$DB" /var/lib/livelens/pre-restore-$ts/
    # 使用该备份的文件级副本整体恢复（db+wal+shm 同源，不混用其他备份）
    rm -f "$DB" "$DB-wal" "$DB-shm"
    cp "$SRC/files/license_server.db" "$DB"
    for ext in wal shm; do
      [ -f "$SRC/files/license_server.db-$ext" ] && cp "$SRC/files/license_server.db-$ext" "$DB-$ext"
    done
    # 权限恢复：数据库属 licsrv:licsrv，组内可读写（WAL 需要写权限）
    chown licsrv:licsrv "$DB" "$DB-wal" "$DB-shm" 2>/dev/null || chown licsrv:licsrv "$DB"
    chmod 664 "$DB" && chmod 664 "$DB-wal" "$DB-shm" 2>/dev/null || true
    systemctl start livelens-license
    sleep 2
    systemctl is-active livelens-license
    curl -s -o /dev/null -w "root %{http_code}\n" http://127.0.0.1:8000/
    echo "R3 完成：已从 $BACKUP_ID 恢复；损坏库留存在 /var/lib/livelens/pre-restore-$ts/"
    ;;
  *) echo "未知 MODE: $MODE"; exit 1;;
esac
