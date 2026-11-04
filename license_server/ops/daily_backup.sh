#!/usr/bin/env bash
# LiveLens 每日自动备份(2026-11-05 P0 整改):SQLite Backup API 快照
# + integrity 校验 + 14 天保留策略。由 root crontab 每日 04:30 触发。
set -euo pipefail
DB=/var/lib/livelens/license_server.db
PY=/opt/livelens/venv/bin/python
BACKUP_ID="livelens-$(date +%Y%m%d-%H%M%S)"
DEST=/var/lib/livelens/backups/$BACKUP_ID
KEEP_DAYS=14

sudo -u licsrv mkdir -p "$DEST/files"
sudo -u licsrv "$PY" - "$DB" "$DEST/license_server.db" <<'PYEOF1'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
print("backup-api ok")
PYEOF1

sudo -u licsrv "$PY" - "$DEST/license_server.db" "$BACKUP_ID" <<'PYEOF2' | tee "$DEST/verify.txt" >/dev/null
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("backup_id:", sys.argv[2])
print("integrity_check:", con.execute("PRAGMA integrity_check").fetchone()[0])
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("licenses", "authorizations", "device_daily_active", "redemption_events"):
    if t in tables:
        print(f"rows:{t}:", con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
PYEOF2

# 保留策略:删除 14 天前的备份目录
find /var/lib/livelens/backups -maxdepth 1 -name "livelens-*" -mtime +$KEEP_DAYS -exec rm -rf {} \; 2>/dev/null || true
chmod -R go-rwx "$DEST"
echo "DONE $BACKUP_ID"
