#!/usr/bin/env bash
# ============================================================
# LiveLens Admin 2.0 — 生产迁移与校验（P2 批准后执行）
# 前置：P1 已批准且 prod_backup.sh 的 verify.txt 人工核对通过。
# 幂等：重复执行结果一致；校验含索引数量（1 表 + 5 索引）。
# 不输出任何密钥/明文 KEY。
# 用法：DEPLOY_COMMIT=<commit> bash prod_migrate.sh
# ============================================================
set -euo pipefail

DEPLOY_COMMIT="${DEPLOY_COMMIT:?需要 DEPLOY_COMMIT=部署目标 commit（P0 批准时记录）}"
REPO=/opt/livelens
LS=$REPO/license_server
PY=$REPO/venv/bin/python

cd "$REPO"

# 1. 代码固定到已测试 commit（禁止盲 pull：先 fetch，再校验 commit 存在，再 checkout）
git fetch --all --prune
git cat-file -e "$DEPLOY_COMMIT"^{commit} || { echo "FATAL: commit $DEPLOY_COMMIT 不存在"; exit 1; }
echo "部署前 commit: $(git rev-parse HEAD)"
git checkout --detach "$DEPLOY_COMMIT"
git log -1 --oneline

# 2. 依赖检查（幂等；admin2 不新增依赖）
"$PY" -m pip install -q "sqlalchemy[asyncio]" || { echo "FATAL: 依赖安装失败"; exit 1; }
"$PY" -c "import greenlet" || { echo "FATAL: greenlet 缺失"; exit 1; }

cd "$LS"

# 3. 迁移前校验（只读）。
# 注意：P2 实测发现脚本缺陷——迁移前库尚未回填，--check 按"缺失"返回退出码 1，
# 在 set -e 下会中止整个脚本。预期行为：退出 0（已一致，迁移可跳过回填）或 1
# （缺失，正是需要迁移的状态）都放行；其他退出码（如 2 = 异常）才中止。
set +e
"$PY" migrate_admin2.py --check
CHECK_RC=$?
set -e
if [ "$CHECK_RC" -gt 1 ]; then
  echo "FATAL: 迁移前校验异常退出（rc=$CHECK_RC）"
  exit 1
fi

# 4. 执行迁移（幂等）
"$PY" migrate_admin2.py

# 5. 迁移后校验：索引与表数量必须精确匹配（1 表 + 5 索引），事件数 == REDEEMED 数
"$PY" - <<'EOF'
import sqlite3, sys
con = sqlite3.connect("file:/var/lib/livelens/license_server.db?mode=ro", uri=True)
idx = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%' AND tbl_name='licenses'")]
extra = [i for i in idx if i.split('ix_licenses_')[-1] not in
         ("id", "key_hash", "authorization_id", "created_at", "redeemed_at", "state", "auth_state_redeemed")]
dau_idx = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='device_daily_active'")]
q = lambda s: con.execute(s).fetchone()[0]
checks = {
    "redemption_events 表存在": q("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='redemption_events'") == 1,
    "新索引数量=5": q("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='licenses' AND name IN"
      " ('ix_licenses_created_at','ix_licenses_redeemed_at','ix_licenses_state','ix_licenses_auth_state_redeemed')") == 4
      and q("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ix_device_daily_active_device_seen'") == 1,
    "无计划外 licenses 索引": not extra,
    "事件数==REDEEMED": q("SELECT COUNT(*) FROM redemption_events")
        == q("SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND redeemed_at IS NOT NULL"),
}
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("PASS " if v else "FAIL ") + k)
sys.exit(1 if bad else 0)
EOF

echo "迁移完成且校验通过。等待 P3 批准后执行重启（prod_smoke.sh 内含重启步骤）。"
