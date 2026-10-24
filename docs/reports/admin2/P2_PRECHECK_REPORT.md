# LiveLens Admin 2.0 — P2 前部署范围收敛报告（P2_PRECHECK_REPORT）

日期：2026-09-29 · 本轮未 SSH 生产（P1 之前的只读核查除外）、未迁移、未重启。
最终状态：**收敛部署提交 `ffbabc0` 已构建并推送；其上 pytest 55/55、隔离演练 42/42（回滚基线=生产实测 HEAD）**。

---

## 1. 基线事实（P1 实测）

- 生产 git HEAD：`8fe4ba1`（"Fix remote client device binding"）——是本地 master 历史的**祖先节点**，非分叉。
- 生产工作目录 **dirty**：7 个 tracked 文件手动修改（+518 行）+ 4 个 untracked 新服务端文件——即 Phase 7.2-5…7.4 的服务端演进以 out-of-band 方式手动部署，git HEAD 未跟进。
- **关键验证**：服务器工作目录 `license_server/` 与 `396e9ff:license_server` 做全目录 diff（`--strip-trailing-cr`）→ **零内容差异**（仅 CRLF 行尾噪音、服务器专属 `.env`、6 个未部署的开发辅助文件）。即：**服务器当前运行的服务端代码 ≡ 396e9ff 的 license_server**。

## 2. admin2 对中间提交服务端变更的依赖（逐项说明）

bb18c37 的父是 396e9ff，中间提交确有服务端演进；admin2 无法脱离它们运行，**均包含在收敛提交中**：

| 依赖 | 来源阶段 | admin2 依赖点 |
| --- | --- | --- |
| `models.DeviceDailyActive` + `heartbeat_service.py` + heartbeat 端点 | 7.4 | DAU 统计、设备「最近活跃」、总览/分析页 |
| `models`：`licenses.features` 列、`authorization_id` 关联（7.2-5 重构） | 7.2-5 | 卡密/设备/兑换全部查询与功能标签 |
| `admin/auth.py` 的 `SESSION_KEY`/`ADMIN_USERNAME` | 7.2-7 | admin2 认证复用（会话互通） |
| `config.BUSINESS_TZ_NAME` + tz 约定 | 7.2-7e/7.4 | 北京时间日切与展示 |
| `schemas` 校验（min_length 等） | 7.2-5/6 | 测试断言与 API 行为一致性 |
| `release_service.py`/`feijipan_resolver.py` | Phase 2 更新分发 | bb18c37 的 `main.py` 引用（admin2 不直接用，但同文件部署必需） |
| 授权修复（`8fe4ba1` 设备绑定修复本身） | 生产已在运行 | **redemption_service 在服务器工作目录相对 8fe4ba1 零修改** → bb18c37 的 redemption_service 已含该修复，无遗漏 |

**结论**：不存在"机械目录覆盖"——收敛提交的 license_server 树 = bb18c37 版（已验证与 bb18c37 逐字节一致），即「服务器当前运行代码 + Admin 2.0」，必要的授权修复与依赖全部在内。

## 3. 相对生产基线（8fe4ba1）的全部变更

`git diff 8fe4ba1..ffbabc0`：**45 文件，+8911 / −165，全部位于 `license_server/`，其余路径零变更**。构成：

1. 服务器已在运行的 out-of-band 演进被**正式入库**（7.2-5…7.4：models/schemas/main/config/database/admin 视图、heartbeat/release/feijipan 服务、dau.html、requirements）——部署后 `git status` 将变干净；
2. Admin 2.0 全部内容（admin2/、stats_service、admin2 模板与静态资源、migrate_admin2.py）；
3. redemption_service 的 Phase 3 加固（事件同事务 + 一次性守卫）；
4. 测试与工具（conftest、test_admin2、test_server/test_simple 更新、pytest.ini、migrate_db.py、add_features_column.py 等 6 个开发辅助文件）；
5. 服务器运行文件的行尾将从 CRLF 规范化为仓库 LF（内容级等价，已验证）。

## 4. 验证结果（均在收敛提交 ffbabc0 上）

```
pytest tests/        → 55 passed / 0 failed（worktree 补齐 out-of-band 组件后）
ops/rehearsal.py     → 42 passed / 0 failed
  （REHEARSAL_CODE_DIR=ffbabc0 工作副本；REHEARSAL_ROLLBACK_REF=8fe4ba1 —— 回滚基线即生产实测 HEAD）
```

演练含：备份/恢复校验、迁移幂等、3-KEY 写操作全链路、三类故障注入、R1 回滚（8fe4ba1 代码 + 已迁移库 → 旧后台与授权 API 正常、/admin2 消失 404）、R2 表回退。

**发现并修复**：
1. 迁移脚本校验在「先实时后迁移」顺序下误报不一致（P2-PRECHECK 前置分析发现）→ 已改为「事件总数（回填+live）== REDEEMED」，两种顺序均正确；
2. `prod_backup.sh` verify.txt 属主问题 → 改为 `sudo -u licsrv tee` 创建（P1 实测问题）；
3. 演练脚本 R1 回滚基线参数化（`REHEARSAL_ROLLBACK_REF`）——此前用主仓库 HEAD 会误把"回滚代码含 admin2"当通过。

## 5. 新的提交坐标

| 项 | 值 |
| --- | --- |
| **DEPLOY_COMMIT** | `ffbabc07b6abefff2da63fdf244b7e9c48a27c78` |
| **ROLLBACK_COMMIT** | `8fe4ba118e7daad6ea2460d0a56074b00c64dc41`（P1 实测生产 HEAD） |
| 远端 | `origin/deploy/admin2` 已 **force-with-lease** 更新为 ffbabc0（lease 锚定旧值 bb18c37，防竞态）；ls-remote 复核一致 |
| 旧候选 bb18c37 | 作废（父 396e9ff 领先生产基线，会夹带 15 个无关客户端提交） |

## 6. 迁移风险

1. 库当前为迁移前 schema（P1 备份证实 3 个 licenses 索引、无事件表）；迁移幂等，`--check`/执行/复检三步校验；
2. `create_all` 会静默重建被意外删除的表（空表）——靠 P1 备份行数核对兜底；
3. 事件回填按 `(redeemed_at, id)` 确定性分类，生产 35 条 REDEEMED 预期回填 35 条（first/renewal 比例由数据决定）；
4. 部署后服务器工作目录将变干净（行尾规范化为 LF），`git status` 应仅剩 `.env`、`.bak-*` 与 untracked 的 runtime 组件——已列入 P3 冒烟观察项。

## 7. 最终部署命令（P2，批准后执行）

```bash
cd D:\TIikTok_Ocr\LiveLens_Admin_2.0\ops
ssh root@118.31.113.99 'DEPLOY_COMMIT=ffbabc07b6abefff2da63fdf244b7e9c48a27c78 bash -s' < prod_migrate.sh
```

（内含：fetch → commit 校验 → checkout 固定 commit → sqlalchemy[asyncio]/greenlet 依赖 → 迁移前 --check → 迁移 → 迁移后 5 索引+对账校验；**不含重启**。）

## 停机点

P2 前收敛完成。**生产 checkout/依赖安装/迁移未执行，等待你批准 P2**（随后 P3 为单次重启+冒烟）。
