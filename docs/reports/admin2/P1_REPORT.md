# LiveLens Admin 2.0 — P1 交付报告（生产备份 + 恢复验证 + 只读 Git 核查）

日期：2026-09-29 · 本轮仅执行：只读记录、SQLite Backup API 备份、独立位置恢复验证、只读 Git 核查。
**未执行**：git checkout、迁移、服务重启、测试卡密生成、任何业务写操作。

---

## 1. 实际执行命令与关键输出

### 1.1 只读记录（生产现状）

```
ssh root@118.31.113.99 'cd /opt/livelens && git rev-parse HEAD && git remote -v
  && systemctl is-active livelens-license && <只读 SQL 行数统计>'
```

| 项 | 实测值 |
| --- | --- |
| 生产 HEAD | `8fe4ba118e7daad6ea2460d0a56074b00c64dc41`（"Fix remote client device binding for license activation"，分支 master） |
| remote | `origin → https://github.com/lseasy0901/TikTokFast_Ocr.git`（fetch/push 同源） |
| 服务 | active |
| 实时行数 | licenses 195 / authorizations 20 / device_daily_active 19 / update_releases 0 / REDEEMED 35 |

⚠️ **HEAD 与 P0 预期（396e9ff）不同**——详见 §4 风险 1。经查证：`8fe4ba1` 是本地 master 历史的祖先节点（生产落后本地），非分叉；本地 `git log 396e9ff..8fe4ba1` 为空、`git branch --contains 8fe4ba1` 含 master。

### 1.2 备份执行

```
ssh root@118.31.113.99 'bash -s' < ops/prod_backup.sh
```

关键输出：

```
backup-api ok
backup_id: livelens-20260929-033751
integrity_check: ok
schema_expected_present: True
schema_extra_tables: none
rows:authorizations: 20
rows:device_daily_active: 19
rows:licenses: 195
rows:update_releases: 0
```

- **BACKUP_ID = `livelens-20260929-033751`**
- 位置：`/var/lib/livelens/backups/livelens-20260929-033751/`
  - `license_server.db`（SQLite Backup API 快照，118,784 字节）
  - `files/`（db+wal+shm 文件级副本，R3 素材）
  - `verify.txt`（校验记录）
- 权限：目录与快照 `drwx------/-rw------- licsrv:licsrv`（go-rwx 已生效）。

### 1.3 独立位置恢复验证

快照复制到独立目录 `/var/lib/livelens/restore-drill/restored.db`（不触碰生产库），以 licsrv 只读打开：

```
integrity: ok
licenses: 195   authorizations: 20   dau: 19   redeemed: 35
lic indices: 3   ← 迁移前 schema（正确：备份摄于迁移之前）
```

与生产实时行数逐项一致。**服务全程健康**（`active`，授权 API HTTP 200）。

### 1.4 只读 Git 核查

- 服务器 `git remote -v`：origin = 同一 GitHub 仓库 ✓
- **`deploy/admin2` 可获取性**：服务器上 `git ls-remote origin refs/heads/deploy/admin2` →
  `bb18c37f66166508ee6f73e842936b4c48b9c8b1` ✓（与 DEPLOY_COMMIT 一致，P2 的 fetch 可用）
- 未执行任何 checkout/fetch 写操作（fetch 会改服务器本地 refs，按「只读」边界未做；P2 脚本自带 `git cat-file -e` 兜底）。

## 2. P1 验收标准核对（5/5）

| # | 标准 | 结果 |
| --- | --- | --- |
| 1 | integrity_check=ok | ✅（快照与恢复副本均 ok） |
| 2 | 基线 schema 齐全 | ✅ 四表 + 无计划外表 |
| 3 | 行数与基线吻合 | ✅ 195/20/19（±0，与 Phase 2 核查完全一致） |
| 4 | 备份目录权限收紧 | ✅（verify.txt 属主小瑕疵见 §4） |
| 5 | HEAD/remote 核实 | ✅ HEAD=8fe4ba1（≠P0 推断，已查证拓扑，见 §4）；remote 同源；deploy/admin2 可获取 |

## 3. 执行偏差记录（不影响交付，均已补救/说明）

1. `prod_backup.sh` 末尾 `chmod -R go-rwx` 对 `verify.txt` 报 "Operation not permitted"（该文件由 root shell 重定向创建、属 root，licsrv 无法改其权限），导致脚本以非零退出。**备份与校验本身不受影响**；已由 root 补做 `chmod 644 + chown root`。→ 建议脚本后续把 verify.txt 创建改为 `sudo -u licsrv tee`（P2 前顺手修，不阻塞）。
2. 恢复验证脚本两次内联 heredoc 失误（chr(47) 误用），重跑成功；不影响任何生产状态。

## 4. 重要发现与风险（P2 批准前必须知情）

1. **⚠️ 生产 HEAD 落后本地**：生产在 `8fe4ba1`（Sep 前后的设备绑定修复），本地 master 已在其上叠加 **15 个提交**（OCR checkpoint、Huya 适配、UI onboarding、流解析重构等，清单已列于执行记录）直至 `396e9ff`，admin2 又在其上。**P2 的 `git checkout bb18c37` 将把 admin2 与这 15 个提交一并部署**——其中含客户端侧的 OCR/流/UI 变更，虽不影响服务端进程（服务只运行 license_server/），但版本跨度远超"仅 admin2"。**P2 批准前需要你确认接受这一部署范围**；可选替代：在服务器上创建只含 license_server 变更的过渡分支（需你批准后本机操作）。
2. ROLLBACK_COMMIT 已修正为实测值 `8fe4ba1`（P0 报告中的 396e9ff 作废）。
3. `verify.txt` 属主 root（licsrv 目录内），读取无碍；P2 前顺手统一属主。
4. 备份摄于 2026-09-29 03:37 UTC（服务器时钟），此后新增业务数据不在备份内；P2 迁移前如需更新快照可重跑 `prod_backup.sh`（幂等，生成新 BACKUP_ID）。

## 5. P1 验收：5/5 通过。

## 停机点

P1 完成。**P2（固定 commit checkout + 依赖 + 幂等迁移，不含重启）未执行，等待你批准**——批准前请先就 §4.1 的部署范围（15 个中间提交随行）给出明确决定。
