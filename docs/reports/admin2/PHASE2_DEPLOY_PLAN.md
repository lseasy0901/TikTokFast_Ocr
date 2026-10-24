# LiveLens Admin 2.0 — 生产上线部署方案（v3 · Phase 4 演练修订版）

> 状态：**v3**。全部步骤已在隔离环境完整演练通过（42/42，见 PHASE4_REPORT.md），生产未执行任何一步。
> 【P0】批准本方案 ≠ 批准执行；每个批准点单独确认（见 §8）。
> 运维脚本（逐条对应本方案）：`ops/prod_backup.sh`、`ops/prod_migrate.sh`、`ops/prod_smoke.sh`、`ops/prod_rollback.sh`。

## 0. 目标环境与部署基线

| 项 | 值 |
| --- | --- |
| 主机 | `118.31.113.99`（SSH 免密，root 执行；服务用户 `licsrv`） |
| 服务 | systemd `livelens-license.service` |
| 运行 | `/opt/livelens/venv/bin/python -m uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000` |
| 代码 | git 仓库 `/opt/livelens`（服务 cwd=`/opt/livelens/license_server`） |
| 数据库 | `sqlite:////var/lib/livelens/license_server.db`（WAL） |
| 访问 | SSH 隧道 `localhost:8001 → 127.0.0.1:8000`；`/admin`（经典，保留）+ `/admin2`（新版） |
| **部署 commit** | `DEPLOY_COMMIT = ffbabc07b6abefff2da63fdf244b7e9c48a27c78`（**P2 前收敛版**：父=生产基线 8fe4ba1，license_server 树与 bb18c37 一致，其余路径零变更；已推送 `deploy/admin2` 并 ls-remote 核实。旧候选 bb18c37 作废——其父 396e9ff 领先生产基线，会夹带 15 个无关客户端提交） |
| **回滚 commit** | `ROLLBACK_COMMIT = 8fe4ba118e7daad6ea2460d0a56074b00c64dc41`（**P1 实测生产 HEAD**；R1=checkout 此 commit 的 license_server，即回到生产当前运行状态） |
| **P0 状态** | ✅ 已完成（2026-09-29）：pytest 55/55、隔离演练 42/42、提交 28 文件、远端可获取性已核实 |
| **P1 状态** | ✅ 已完成（2026-09-29）：BACKUP_ID `livelens-20260929-033751`，integrity/恢复验证通过，生产 HEAD 与 remote 已实测，`deploy/admin2` 从服务器可获取（ls-remote 已更新为 ffbabc0） |
| **P2 前收敛** | ✅ 已完成（2026-09-29）：部署范围收敛至生产基线；收敛提交上 pytest 55/55、隔离演练 42/42（回滚基线=8fe4ba1）；详见 P2_PRECHECK_REPORT.md |
| **P3 状态** | ✅ 验收通过（2026-09-29 09:45 CST 单次重启）：admin2 六页生效、3-KEY 冒烟全过、对账 37==37、validate ≈4ms、日志零异常零敏感信息；进入 72h 观察期（至 10-02），详见 P3_REPORT.md |
| **P2 状态** | ✅ 已完成（2026-09-29）：生产 HEAD=ffbabc0（checkout 前 dirty 工作目录已备份为 license_server.pre-ffbabc0-20260929-093218）；迁移 35 条回填（20 first + 15 renewal）与 REDEEMED 一致；5 索引+事件表校验 PASS；**服务未重启**（ActiveEnterTimestamp 2026-09-26），运行中的进程仍为旧代码，P3 重启后生效 |

**执行用户与权限**：git/迁移/重启以 root 执行；数据库读写属 `licsrv`；备份目录属 `licsrv` 且 `go-rwx`。所有脚本 `set -euo pipefail`（任一步失败立即中止），不输出 ADMIN_API_KEY、签名私钥或明文 KEY（冒烟脚本经环境变量传入凭据，日志只记指纹前 8 位）。

## 1. 本次上线内容

1. admin2 新后台（`/admin2` 六页 + 认证 API + CSV 导出）。
2. 迁移 `migrate_admin2.py`（幂等）：**新表 `redemption_events` + 5 个统计索引**
   （`ix_licenses_created_at`、`ix_licenses_redeemed_at`、`ix_licenses_state`、
   `ix_licenses_auth_state_redeemed`、`ix_device_daily_active_device_seen`）。
   纯增量；旧代码对新表/新索引零依赖。
3. `redemption_service` 加固：兑换事件同事务写入（防重）+ 行级一次性守卫（消除并发双重兑换窗口）。状态机语义不变。

## 2. 【P1】备份与恢复演练 — `ops/prod_backup.sh`

修正点：备份目录脚本内幂等创建（旧方案 `cp` 到不存在目录会失败）；**优先 SQLite Backup API**（WAL 一致性快照）；文件级副本（含 -wal/-shm）仅作 R3 灾难恢复素材。

```bash
# 本机执行（脚本内部 SSH 到生产）：
ssh root@118.31.113.99 'bash -s' < ops/prod_backup.sh
# 产出：BACKUP_ID=livelens-YYYYmmdd-HHMMSS
#   /var/lib/livelens/backups/$BACKUP_ID/license_server.db   ← Backup API 快照
#   /var/lib/livelens/backups/$BACKUP_ID/files/…             ← 文件级副本
#   /var/lib/livelens/backups/$BACKUP_ID/verify.txt          ← 校验记录
```

**P1 验收标准**（verify.txt 人工核对）：
1. `integrity_check: ok`；
2. `schema_expected_present: True`（licenses / authorizations / device_daily_active / update_releases 齐全）；
3. 行数与 Phase 2 基线吻合（licenses 195 / authorizations 20 / DAU 19，允许业务自然增长，不得为 0）；
4. 备份目录权限已收紧（仅 licsrv 可读）。

## 3. 【P2】迁移 — `ops/prod_migrate.sh DEPLOY_COMMIT`

```bash
ssh root@118.31.113.99 'DEPLOY_COMMIT=<commit> bash -s' < ops/prod_migrate.sh
```

脚本内部顺序（任一步失败即中止，且**不重启服务**——旧服务继续用旧代码 + 新库，完全兼容）：
1. `git fetch` → 校验 `DEPLOY_COMMIT` 存在 → `git checkout --detach`（**固定 commit，禁止盲 pull**；记录部署前 commit 供 R1）；
2. 依赖：`pip install "sqlalchemy[asyncio]"` + `import greenlet` 自检；
3. 迁移前 `migrate_admin2.py --check`（只读）；
4. 执行迁移（幂等：`CREATE TABLE/INDEX IF NOT EXISTS` + 按 license_id 去重回填）；
5. 迁移后精确校验：事件表存在、**5 个新索引齐备且无计划外索引**、`事件总数 == REDEEMED 卡密总数`（兼容「先实时后迁移」顺序，Phase 4 修正）。

**注意**：迁移脚本开头 `create_all` 会补齐缺失表（与服务器启动行为一致）——若发现表被意外删除，迁移会静默重建**空表**而非报错；表级异常应以 §2 的备份行数核对为准。

## 4. 【P3】单次重启 + 冒烟 — `ops/prod_smoke.sh`

```bash
ssh root@118.31.113.99 'ADMIN_PASSWORD=<ADMIN_API_KEY> bash -s' < ops/prod_smoke.sh
```

**第一组 · 只读**（失败即回滚评估）：服务 active；`/`、`/admin/login`、`/admin2/login` = 200；未认证 `/admin2/api/*` = 401；日志无 Traceback。
**第二组 · 写操作（3 枚测试月卡）**：
- K1 在测试设备**首次激活** → 兑换记录页出现「首次激活 / 新建授权」；
- K2 同设备**续费** → 出现「续费 / 有效期累加」（实时事件）；
- K1 重复兑换 → 400 `already_redeemed`；
- KEY 反查：K2 命中「已兑换+绑定设备」、K3 命中「未使用」、乱串「未找到」；
- K3 **UNUSED 吊销** → 200；重复吊销 → 409；
- 导出一次 CSV。
**第三组 · 对账**：`回填数 + live 数 == REDEEMED 总数`；冒烟兑换已入账。
收尾：测试卡密状态为 2 已兑换 + 1 已吊销（无遗留未使用卡），全程日志无明文 KEY。

## 5. 72 小时观察期

| 指标 | 方法 | 阈值/动作 |
| --- | --- | --- |
| 错误日志 | `journalctl -u livelens-license -g Traceback --since -24h` 每日 | 出现 Traceback → 评估回滚 |
| 授权 API 延迟 | 每日 50 次 validate 计时 p95 | 较基线（≈10ms）增幅 >50% → 评估回滚 |
| 授权成功率 | 客户端激活反馈抽查 | 非预期 500 → 立即回滚评估 |
| SQLite 锁 | 日志 grep `database is locked`；`-wal` 增长 | 出现锁等待 → 回滚评估 |
| 统计对账 | 每日 §4 第三组 SQL | 不一致 → 回滚评估 |
| 磁盘 | `df -h /var/lib/livelens` | 剩余 <20% → 扩容 |

## 6. 回滚 — `ops/prod_rollback.sh`

**触发**：冒烟任一组失败；对账不一致；观察期出现影响授权 API 的 Traceback；延迟/错误率超阈值。

```bash
# R1 代码回退（首选）：固定到部署前 commit；新表/索引对旧代码无影响，无需动库
ssh root@118.31.113.99 'ROLLBACK_COMMIT=<部署前commit> bash -s' < ops/prod_rollback.sh code
# 前提核查（演练证实）：git checkout 不会移除 gitignored 的 out-of-band 组件
#   —— admin_web/、susi_helper/、license_server/.env 必须仍在位（生产本就在位）

# R2 表回退（仅当确需移除新表；统计索引保留无害）
ssh root@118.31.113.99 'bash -s' < ops/prod_rollback.sh table

# R3 灾难恢复：必须显式指定唯一 BACKUP_ID（禁止通配符/“最新备份”推断），
#    恢复前先对备份做 integrity_check；恢复后恢复 licsrv 属主与 664 权限
ssh root@118.31.113.99 'bash -s' < ops/prod_rollback.sh disaster livelens-YYYYmmdd-HHMMSS
```

R3 细节（脚本已实现）：stop → 当前损坏库存档至 `/var/lib/livelens/pre-restore-<ts>/` → 从 `$BACKUP_ID/files/` 整套恢复 db+wal+shm（同源，不混用）→ `chown licsrv:licsrv` + `chmod 664` → start → 健康检查。

## 7. 性能与安全要点（Phase 4 复核结论）

- 管理端无全表扫描（EXPLAIN 全部命中索引，含 5 个新索引）、无无界日期查询（自定义区间 ≤366 天强制校验）、无长事务持写锁（导出纯 SELECT，WAL 读者不阻塞写者）；
- 5000 卡密量级实测：keys 8ms / devices 53ms / redemptions 16ms / analytics 24ms / 全量 CSV 385ms；授权 API validate ≈10ms 不受管理负载影响；
- 备份（Backup API 只读快照）与迁移（秒级）远短于任何客户端超时，不会阻塞授权；
- 脚本零敏感输出；会话 Cookie HttpOnly+SameSite=Lax；`secure` 待 HTTPS 化时开启；
- 网络暴露范围不变：仍为 SSH 隧道 + 127.0.0.1 绑定，经典后台与全部既有 API 原样保留。

## 8. 审批点（批准边界：批准备份 ≠ 批准迁移/重启）

| 编号 | 内容 | 批准后允许的动作 |
| --- | --- | --- |
| P0 | 批准本方案 + 创建/记录 `DEPLOY_COMMIT` 与 `ROLLBACK_COMMIT` | 仅允许提交代码与准备脚本 |
| P1 | 备份与恢复演练（§2） | 仅允许运行 `prod_backup.sh` 与只读校验 |
| P2 | 迁移（§3） | 仅允许 checkout 固定 commit + 依赖安装 + 迁移（**不含重启**） |
| P3 | 重启与冒烟（§4） | 仅允许单次重启 + 冒烟（含测试卡密操作） |
| P4 | 观察期结束 | 仅允许讨论将 /admin2 设为默认入口 |

**P1 所需最终命令**（批准后逐条执行）：

```bash
cd D:\TIikTok_Ocr\LiveLens_Admin_2.0\ops
# 1) 生成备份 + 校验记录（脚本经 stdin 推送，不在服务器放置任何文件）
ssh root@118.31.113.99 'bash -s' < prod_backup.sh
# 2) ROLLBACK_COMMIT 只读核实（P0 前提确认，一条只读命令）：
ssh root@118.31.113.99 'cd /opt/livelens && git rev-parse HEAD && git remote -v'
#    期望 HEAD = 396e9ff44d1e592d1ec1669fac98f5b476c1ca5f；
#    同时确认 origin 指向的仓库能提供 deploy/admin2 分支（bb18c37）——
#    若 origin 不同源，需在 P2 前另行同步该 commit（本机 push 已就绪）。
# 3) 人工核对 BACKUP_ID 与 verify.txt 四项验收标准（§2）后，回报 P1 通过。
```

**P1 验收标准**（较 v3 增补第 5 项）：
1. `integrity_check: ok`；2. 基线 schema 齐全；3. 行数与基线吻合（195/20/19 ±自然增长）；
4. 备份目录权限收紧；5. **服务器 HEAD 确为 396e9ff 且 remote 已确认可获取 `deploy/admin2` 分支**。
