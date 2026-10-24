#!/usr/bin/env bash
# ============================================================
# LiveLens Admin 2.0 — 生产备份与恢复校验（P1 批准后执行）
# 产物：BACKUP_ID = livelens-YYYYmmdd-HHMMSS
#   /var/lib/livelens/backups/$BACKUP_ID/license_server.db      (SQLite Backup API 快照)
#   /var/lib/livelens/backups/$BACKUP_ID/files/license_server.db* (文件级，含 -wal/-shm)
#   /var/lib/livelens/backups/$BACKUP_ID/verify.txt             (校验输出)
# 全程只读（mode=ro / cp），不锁库、不输出任何密钥或明文 KEY。
# SSH 执行方式（P1，本机执行；脚本经 stdin 推送，无需在服务器放置任何文件）：
#   ssh root@118.31.113.99 'bash -s' < prod_backup.sh
# 输出 BACKUP_ID 与 verify.txt；人工核对通过后方可批准 P2。
# （若脚本已上传服务器，也可直接以 root 执行：bash prod_backup.sh）
# ============================================================
set -euo pipefail

DB=/var/lib/livelens/license_server.db
PY=/opt/livelens/venv/bin/python
BACKUP_ID="livelens-$(date +%Y%m%d-%H%M%S)"
DEST=/var/lib/livelens/backups/$BACKUP_ID

# 0. 备份根目录幂等创建（修正旧方案中目录不存在导致 cp 失败的问题）
sudo -u licsrv mkdir -p "$DEST/files"

# 1. 首选：SQLite Backup API（WAL 一致性快照，只读连接）
sudo -u licsrv "$PY" - "$DB" "$DEST/license_server.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
print("backup-api ok")
EOF

# 2. 文件级备份（含 WAL/SHM，供灾难恢复 R3 使用）
sudo -u licsrv cp -a "$DB" "$DB-wal" "$DB-shm" "$DEST/files/" 2>/dev/null \
  || sudo -u licsrv cp -a "$DB" "$DEST/files/"

# 3. 恢复演练校验：integrity_check + schema + 行数（写入 verify.txt，作为 P1 验收材料）
# verify.txt 由 licsrv 身份创建（tee），避免 root 重定向导致属主不一致、
# 使末尾 licsrv 的 chmod 报 Operation not permitted（P1 实测问题）。
sudo -u licsrv "$PY" - "$DEST/license_server.db" "$BACKUP_ID" <<'EOF' | sudo -u licsrv tee "$DEST/verify.txt" >/dev/null
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("backup_id:", sys.argv[2])
print("integrity_check:", con.execute("PRAGMA integrity_check").fetchone()[0])
tables = {r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
expected = {"licenses", "authorizations", "device_daily_active", "update_releases"}
print("schema_expected_present:", expected <= tables)
print("schema_extra_tables:", sorted(tables - expected - {"redemption_events",
      "sqlite_sequence", "sqlite_stat1"}) or "none")
for t in sorted(expected | {"redemption_events"}):
    if t in tables:
        print(f"rows:{t}:", con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
EOF
cat "$DEST/verify.txt"

# 4. 权限收紧：备份目录仅 licsrv 可读
sudo -u licsrv chmod -R go-rwx "$DEST"

echo "BACKUP_ID=$BACKUP_ID"
echo "请人工核对 verify.txt：integrity_check=ok、四张基线表齐全、行数与业务预期一致后，方可批准 P2。"
