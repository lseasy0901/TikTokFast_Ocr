# LiveLens Admin 2.0 — 进度存档（2026-09-29 收工时点）

> 明日恢复：从「待执行 P2」继续。P2 命令今晚被取消，**经只读核实生产零影响**（未 checkout、未迁移、服务正常）。

## 当前进度

| 阶段 | 状态 |
| --- | --- |
| Phase 1-4（审计/原型/admin2 开发/演练） | ✅ 完成（报告见目录内各 MD） |
| P0 版本锁定 | ✅ DEPLOY_COMMIT 已锁定推送（bb18c37 后被收敛版取代） |
| P1 生产备份 | ✅ BACKUP_ID `livelens-20260929-033751`（/var/lib/livelens/backups/，integrity ok，行数 195/20/19/0） |
| P2 前范围收敛 | ✅ 新提交 `ffbabc0`（父=生产基线 8fe4ba1，仅 license_server，+8911/−165），pytest 55/55、演练 42/42 |
| **P2 迁移** | ⏸ **已批准、命令已发出但被取消——生产零影响，明日重跑即可** |
| P3 重启+冒烟 / P4 观察期 | 未开始 |

## 关键坐标

- **DEPLOY_COMMIT** = `ffbabc07b6abefff2da63fdf244b7e9c48a27c78`（已推送 `origin/deploy/admin2`，ls-remote 核实）
- **ROLLBACK_COMMIT** = `8fe4ba118e7daad6ea2460d0a56074b00c64dc41`（P1 实测生产 HEAD；生产工作目录 dirty 属预期——out-of-band 手动部署的服务端演进，内容 ≡ 396e9ff:license_server）
- **BACKUP_ID** = `livelens-20260929-033751`（/var/lib/livelens/backups/，独立恢复验证已通过）
- 本地 master = `bb18c37`（P0 提交，父 396e9ff）；生产 = 8fe4ba1 + dirty 工作目录

## 明日恢复步骤

1. 直接重跑 P2（幂等；脚本含 fetch/commit 校验/依赖/迁移前后校验，任一步失败即中止且不重启）：
   ```bash
   cd D:\TIikTok_Ocr\LiveLens_Admin_2.0\ops
   ssh root@118.31.113.99 'DEPLOY_COMMIT=ffbabc07b6abefff2da63fdf244b7e9c48a27c78 bash -s' < prod_migrate.sh
   ```
   通过标准：迁移输出「一致 ✓」、5 索引+事件表校验 PASS、**服务未重启**（旧进程继续服务）。
2. P2 通过后 → 请求批准 P3：`ssh root@118.31.113.99 'ADMIN_PASSWORD=<ADMIN_API_KEY> bash -s' < prod_smoke.sh`（单次重启 + 3-KEY 冒烟 + 对账）。
3. P3 后进入 72 小时观察期（PHASE2_DEPLOY_PLAN.md §5）。

## 注意事项

- 今晚 P2 取消未产生任何效果（已核实：无 checkout、无迁移、服务健康）。
- 服务器工作目录 dirty（7 个 M 文件 + untracked 新文件）是**预期状态**（out-of-band 部署史）；P2 的 checkout 会将其收敛为 ffbabc0 的干净树，冒烟时 `git status` 应仅剩 .env/.bak/runtime 组件。
- 本地 master HEAD=bb18c37 与生产收敛提交 ffbabc0 是**不同 SHA、相同 license_server 树**（ffbabc0 父=8fe4ba1）；本地无需改动。
- git push 需走本机代理：`git -c http.proxy=http://127.0.0.1:7890 push …`。
- 测试环境：`license_server/.venv`（含 pytest/greenlet）；pytest 与演练命令见 PHASE4_REPORT.md。
- 演练脚本支持 `REHEARSAL_CODE_DIR`（被演练代码目录）与 `REHEARSAL_ROLLBACK_REF`（回滚基线）。
