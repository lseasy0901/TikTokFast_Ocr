# LiveLens Admin 2.0 — Phase 4 交付报告（部署方案修正与隔离环境演练）

日期：2026-09-29 · 本轮**未 SSH 生产、未动生产库、未发布代码、未重启服务**。
最终状态：**隔离演练 42/42 通过（连续两轮，可重复）· pytest 55/55 通过**。

---

## 一、部署方案修正（PHASE2_DEPLOY_PLAN.md → v3）

| # | 问题 | 修正 |
| --- | --- | --- |
| 1 | 文件级备份目标目录不存在，`cp` 直接失败 | `prod_backup.sh` 内 `mkdir -p` 幂等创建 `/var/lib/livelens/backups/$BACKUP_ID/files/` |
| 2 | 备份方式 | 优先 SQLite Backup API（`mode=ro` 源 + `.backup()`，WAL 一致性快照）；文件级副本仅作 R3 素材 |
| 3 | 恢复演练无深度校验 | verify.txt 记录 `PRAGMA integrity_check`、基线 schema 齐全性、四表行数（对照 Phase 2 基线 195/20/19） |
| 4 | 冒烟测试过于笼统 | 固定 **3 枚测试月卡**：K1 首次激活、K2 同设备续费、K3 UNUSED 吊销，另含重复兑换 400、反查三态、CSV、对账 |
| 5 | 灾难恢复用通配符（`backup-files-*`）可能混用多份备份 | R3 必须显式 `BACKUP_ID`；恢复前对该备份 `integrity_check`；整套恢复 db+wal+shm（同源）；恢复后 `chown licsrv` + `chmod 664`；当前损坏库存档至 `pre-restore-<ts>/` |
| 6 | 索引数量口径不一致 | 统一为 **1 表 + 5 索引**（`migrate_admin2.py` 实际 DDL）；`prod_migrate.sh` 校验精确到索引名单并检查无计划外索引 |
| 7 | `git pull` 盲更新 | `DEPLOY_COMMIT` 固定部署 commit（P0 创建并记录），`git fetch` + commit 存在性校验 + `checkout --detach`；同时记录 `ROLLBACK_COMMIT` |
| 8 | 命令健壮性 | 全部脚本 `set -euo pipefail`；`greenlet` 显式自检；执行用户/文件权限明确（root 执行、库属 licsrv、备份 go-rwx） |
| 9 | 审批边界模糊 | P0–P4 逐点定义「批准后允许的动作」：批准备份（P1）≠ 批准迁移（P2）≠ 批准重启（P3）；P2 明确**不含重启** |

**过程中发现并修正的迁移脚本真缺陷**：`migrate_admin2.py` 校验逻辑只统计回填行数与全部 REDEEMED 比较——若按「先上线新代码、后补迁移」顺序执行（live 事件已存在）会**误报不一致**阻断部署。已修正为「事件总数（回填+live）== REDEEMED 总数」，两种上线顺序均正确。

## 二、隔离环境演练（`ops/rehearsal.py`，可重复）

**隔离保证**：`tempfile` 独立临时库 × 2（演练库/故障库）+ 独立端口 8010/8011/8013 + git worktree 隔离回滚代码 + `finally` 自动清理进程与文件；零 SSH、零生产接触。脚本内置端口占用快速失败与「种子必须写入目标库」断言（防止残留进程导致的静默错库）。

**14 个阶段全部通过（42/42，连续两轮）**：

| 阶段 | 结果 |
| --- | --- |
| [1] 备份（Backup API） | ✓ 快照生成 |
| [2] 恢复校验 | ✓ integrity=ok / schema 完整 / 行数核对 |
| [3-5] 迁移 + 索引对账 + 幂等 | ✓ 5 索引齐备；重复迁移零重复回填 |
| [6-7] 启动 + 只读冒烟 | ✓ 三页 200、未认证 401 |
| [8] 写操作 | ✓ K1 首充 / K2 续费 / K1 重复兑换 400 / 反查三态 / K3 吊销 + 重复吊销 409 / CSV |
| [9] 事件对账 | ✓ 事件数==REDEEMED；live 语义 new+extend |
| [10] 故障注入 A（不兼容半成品事件表） | ✓ 迁移退出非零、licenses 原样、无半批写入 |
| [11] 故障注入 B（坏配置启动） | ✓ 进程退出且报错可定位；不影响既有实例 |
| [12] 故障注入 C（篡改事件表） | ✓ 对账检出不一致 |
| [13] R1 代码回滚（worktree @ 部署前 commit） | ✓ 旧后台登录页 200、validate/管理 API 正常、/admin2 消失(404) |
| [14] R2 表回退后旧代码 | ✓ 授权校验正常 |

**演练额外发现（已写入方案 §6）**：R1 回退的前提是 gitignored 的 out-of-band 组件（`admin_web/`、`susi_helper/`、`.env`）在位——全新 checkout 缺它们时旧代码无法启动（`SusiSecurityService` 硬失败）；生产上这些文件本就在 `/opt/livelens` 且 `git checkout` 不会移除 untracked/ignored 文件，因此生产 R1 安全，但已在方案中列为显式前提核查项。

**修复的演练脚本自身问题**：WAL 热日志导致只读打开失败（加 checkpoint）、函数名遮蔽 `http` 模块、urllib 跟随重定向的断言、僵尸服务进程占用端口（8010/8013 已清理并加入防护）、worktree 工作目录错传。

## 三、性能与安全复核

- 无全表扫描（EXPLAIN 全命中索引）、无无界日期查询（自定义区间 ≤366 天）、无长事务持写锁；
- 备份（只读快照）与迁移（秒级）不会阻塞客户端授权：WAL 读者不阻塞写者，且迁移不重启、不持锁超过秒级；
- 三个运维脚本 + 演练脚本通读核查：无 ADMIN_API_KEY/私钥/明文 KEY 输出（凭据仅经环境变量传入，日志只出指纹前 8 位与状态码）；
- 经典后台与既有授权 API 全保留，网络暴露范围不变（SSH 隧道 + 127.0.0.1）。

## 四、测试结果

```
pytest tests/                → 55 passed / 0 failed / 0 skipped（隔离临时库）
ops/rehearsal.py             → 42 passed / 0 failed（连续两轮）
```

## 五、交付物

| 文件 | 说明 |
| --- | --- |
| `PHASE2_DEPLOY_PLAN.md` | v3 修订版部署方案（逐条可执行） |
| `ops/prod_backup.sh` | P1 备份 + 恢复校验（BACKUP_ID / verify.txt） |
| `ops/prod_migrate.sh` | P2 固定 commit + 依赖 + 迁移 + 精确校验 |
| `ops/prod_smoke.sh` | P3 单次重启 + 3-KEY 冒烟 + 对账 |
| `ops/prod_rollback.sh` | R1 代码回退 / R2 表回退 / R3 灾难恢复（唯一 BACKUP_ID） |
| `ops/rehearsal.py` | 可重复隔离演练（14 阶段 42 断言） |
| `PHASE4_REPORT.md` | 本报告 |

## 六、剩余风险

1. 生产 `admin_web/`、`susi_helper/`、`.env` 的在位性以 Phase 2 核查为准（当时存在）；P2 执行前建议再做一次在位检查（已列入 R1 前提）。
2. 迁移脚本 `create_all` 会静默重建被意外删除的表（空表）——无法靠迁移本身发现表级丢失，依赖 §2 备份行数核对兜底。
3. 冒烟需要 `ADMIN_PASSWORD`（=ADMIN_API_KEY）经环境变量传入生产脚本——由你本人执行时填写，脚本不回显不落盘。
4. 事件表随兑换量线性增长（当前量级可忽略）；长期可加归档策略。

## 停机点

本地修正与演练全部完成。**P1（生产备份）未执行——等待你单独批准。**
P1 批准后执行：`cd D:\TIikTok_Ocr\LiveLens_Admin_2.0\ops && ssh root@118.31.113.99 'bash -s' < prod_backup.sh`，核对 BACKUP_ID 与 verify.txt 后回报。
