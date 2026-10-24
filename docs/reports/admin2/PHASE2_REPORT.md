# LiveLens Admin 2.0 — 第二阶段交付报告

日期：2026-09-29 · 本阶段全部工作仅涉及本地开发/测试与生产**只读**核查，未做任何生产变更。

## 阶段 A：审计复核与生产只读核查 ✅

- 审计报告结论全部复核成立（代码路径、表结构、哈希算法 SHA-256、认证 fail-closed、admin_web 快照机制、部署方式）。
- SSH 免密可达生产主机，经批准后以**纯 SELECT/PRAGMA** 完成只读核查（脚本存档：`prod_check_readonly.py`，可重复使用）。
- 生产事实：systemd `livelens-license.service`（用户 licsrv，`/opt/livelens`，uvicorn --app-dir app）；库在 `/var/lib/livelens/license_server.db`（WAL）。
- 核查结果：schema 与 `models.py` 逐列一致；licenses 195（UNUSED 146 / REDEEMED 35 / REVOKED 14）、authorizations 20、DAU 19 行 15 台、update_releases 0；`duration_days` **仅有 1/30/180/365，无「其他」**；关联完整性零异常（无孤儿、无重复设备、无倒挂时间）；`activated_at` 19/20 为 NULL（预期，仅过期重置时写入）；DAU 覆盖 09-17 起，授权数据覆盖 09-11 起。
- **结论：与审计报告一致，按约定继续开发。**

## 阶段 B/C：admin2 后端与统计口径 ✅（本地完成）

**新增文件**（均在 `license_server/`）：

| 文件 | 作用 |
| --- | --- |
| `app/services/stats_service.py` | 统计口径唯一定义：北京时间日切（SQLite `+8 hours`）、首次激活/续费/兑换/DAU/有效授权、套餐映射（1/30/180/365→四类，其余→「其他套餐」）、KEY 分页/反查/设备详情、CSV 公式注入防护 |
| `app/admin2/{__init__,auth,routes}.py` | 独立后台模块：页面路由、认证 JSON API、CSV 导出、静态挂载 |
| `app/templates/admin2/*.html`（7 个） | base 外壳 + 登录 + 五个业务页（服务端渲染外壳，数据全部经 API） |
| `app/static/admin2/`（css/js） | 原型同款设计系统与图表库 + 前端公共层（API 封装/401 跳转/CSRF 头/XSS 转义/加载·空·错误态）+ 5 个页面脚本 |
| `migrate_admin2.py` | `redemption_events` 事件日志表迁移（幂等、可重复、--check 校验、不改既有表；回填续费 result=unknown 不伪造） |
| `tests/conftest.py`、`tests/test_admin2.py`、`pytest.ini` | 一次性临时库测试环境 + 31 项测试 |

**修改文件**：`app/main.py`（仅追加 `setup_admin2(app)`）；`tests/test_server.py`/`test_simple.py`（仅把 `from app.models import …` 等改为与生产 `--app-dir app` 一致的顶层导入，修复收集期「表重复定义」崩溃，未动任何断言）。

**关键设计取舍**：
1. 认证完全复用：同一 ADMIN_API_KEY（fail-closed）、同一会话键 → 经典后台与新后台登录态互通；vendored SQLAdmin 的 SessionMiddleware 只挂在它自己的 /admin 子应用上，admin2 在主应用显式补挂同密钥 SessionMiddleware（对既有 API 无影响）。
2. 首次激活/续费采用确定性判定：同一授权下 `(redeemed_at, id)` 最小者为首次——不依赖时钟精度，可稳定重放历史。
3. 历史续费的「累加 vs 重置」标记为 `unknown`（兑换前 expires_at 未记录，不可回溯），不推算不伪造。
4. 批量生成复用 `admin.views._create_license_batch`（单一事实来源：预设/上限/事务语义与经典后台同源）；吊销先校验 UNUSED 再调 `LicenseService.revoke_license`。
5. KEY 反查走服务端既有 SHA-256，输入不落库不写日志，响应 no-store；按 status 区分 未使用/已兑换/已吊销/未找到/无关联设备。
6. CSRF：会话 token（表单字段 + X-CSRF-Token 头双通道）；API 未认证一律 401 JSON，前端统一跳登录。

## 阶段 D：真实前端接入 ✅

- 五个页面 + 登录页全部接入真实 API；MOCK 文件不进入 `app/static`，无任何静默回退路径。
- 统一处理：加载中/空数据/网络错误/HTTP 错误/401 会话失效；所有服务端字符串经 `esc()` 转义；静态资源带 `?v=` 版本号防缓存。
- 浏览器端到端验证通过：登录 → 总览（真实 KPI/趋势/环形图）→ 卡密（真实 SHA-256 指纹、状态计数、生成弹窗一次性明文 → 服务端反查命中「未使用」）→ 设备（详情抽屉、时间线、剩余 364 天、激活时间为 NULL 的诚实展示）→ 吊销按钮仅对 UNUSED 渲染。

## 阶段 E：测试结果 ✅（1 项遗留已如实记录）

```
python -m pytest tests/   （隔离临时库，不触生产/本地开发库）
31 passed   —— 新增 admin2 套件（31/31）
6 passed / 9 failed —— 既有套件
```

9 个失败全部为**先前已存在**的陈旧测试，根因与本次改动无关（git 可证：本次未改 schemas/services）：
1. `app.container` 注入容器在 Phase 7.2 重构中已移除（test_create_license 等 5 项 AttributeError/KeyError）；
2. `LicenseValidate.license_key` 后续加固加了 `min_length=16`，旧测试仍提交 11 字符短 Key（422 ≠ 200）；
3. `max_devices` 响应字段在 7.2-5 重构中移除（test_duration_handling）。

按「不得修改预期结果掩盖缺陷」要求，未改这些断言；建议下阶段专项清理陈旧测试。

**安全验证已覆盖**：fail-closed、伪造/过期会话 401、无 CSRF 403、伪造 CSRF 403、Swagger 生产关闭、CSV 公式注入、SQL 全参数绑定、日志无明文 Key（设计层面：反查与生成路径零日志）。

## 阶段 F：灰度部署计划 ✅（见 `PHASE2_DEPLOY_PLAN.md`）

备份/恢复演练 → 依赖检查 → 迁移（幂等）→ 单次重启 → 10 步只读冒烟 → 回滚命令（R1 代码回退不需要动库 / R2 删新表 / R3 灾难恢复）→ 72 小时观察期（对账 SQL + 延迟基线 + 锁等待监控）。

## 风险与待确认事项

1. **生产迁移与部署未执行**——等待你按 PHASE2_DEPLOY_PLAN.md 逐步批准。
2. 旧套件 9 个陈旧测试建议另行清理（避免掩盖未来真实回归）。
3. admin2 当前无操作审计日志（生成/吊销者身份），事件日志表已就绪，实时写入需要在未来对 `redemption_service` 做一处加性 hook——待你批准后实施。
4. 管理端无速率限制（登录/反查），当前仅经 SSH 隧道暴露，风险可控；若未来公网暴露需加限流。

## 停机点

本地开发与测试已全部完成。**生产部署（代码上线、迁移执行、服务重启）全部未执行，等待你明确批准。**
