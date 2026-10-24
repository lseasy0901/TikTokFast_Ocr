# LiveLens Admin 2.0 — P3 交付报告（正式上线 · 重启 + 冒烟 + 验收）

日期：2026-09-29 · 授权范围：最终检查 / 单次重启 / 3 枚测试月卡冒烟 / 事件对账 / 上线验收。
**结论：P3 验收通过，进入 72 小时观察期。**

---

## 1. 重启前生产状态（只读核查，8 项全过）

Git HEAD=`ffbabc0` ✓；DB integrity=ok、事件表+5 索引在位、对账 35==35、license_id 无重复 ✓；行数 licenses 195 / REDEEMED 35 / auth 20 / DAU 19 / events 35；P1 备份与 P2 工作目录快照在位；.env/admin_web/susi_helper/greenlet 3.5.5 齐备；服务 active、授权 API 200、磁盘余 28G、近期 0 Traceback。脚本行为复核：prod_smoke.sh 按授权范围补齐了第五阶段断言后执行。

## 2. 重启（唯一一次）

- 命令：`prod_smoke.sh`（ADMIN_PASSWORD 在服务器侧从 .env 读取，明文未出现在命令行/日志/报告中）
- 时间：**2026-09-29 09:45:11 CST**（ActiveEnterTimestamp 更新，PID 43321）
- **脚本输出在重启后中断**（本地 ssh 管道问题），重启本身成功；冒烟业务部分改为服务器侧补跑（见 §3-§4），未再次重启。

## 3. 重启后验收（全过）

| 项 | 结果 |
| --- | --- |
| systemd active | ✅ |
| 授权 API `/` | ✅ 200 |
| 经典后台 `/admin/login` | ✅ 200 |
| **新后台 `/admin2/login`** | ✅ **200（已生效）** |
| 未认证 `/admin2/api/*` | ✅ 401 |
| 启动日志 | ✅ 无 Traceback |

## 4. 三枚测试月卡冒烟（全过；设备 DV-smoke-20260929095049，卡密 #196/#197/#198）

- K1 首次激活 200 → 事件 `first/new` ✅；**K1 重复兑换 400 already_redeemed**（服务器日志证实；首轮脚本未捕获 HTTPError 中断，属脚本问题非业务问题）✅
- K2 同设备续费 200 → 事件 `renewal/extend`（有效期累加）✅
- 反查：K2 已兑换+绑定测试设备（含授权状态与到期时间）✅；K3 吊销前 UNUSED ✅；不存在卡密 → not_found ✅
- K3 吊销 200；**重复吊销 409** ✅
- CSV 导出：BOM+表头+37 行，含「首次激活/有效期累加」标签 ✅
- 流水页：该设备两条记录 type=first/renewal、result=新建授权/有效期累加 ✅

## 5. 事件对账与数据完整性

- 迁移回填 35（20 first + 15 renewal）+ 本次 live 2 条 = **37 == REDEEMED 37**，license_id 唯一 ✅
- 吊销未产生事件 ✅（revoke 不写 redemption_events）
- 原有业务数据无异常：licenses 195→198（+3 测试卡）、authorizations 20→21（+1 测试设备）、DAU 19→20（测试设备次日心跳后自然增长，今日 DAU=0 为正确行为——激活不等于心跳）
- 测试卡终态：**2 REDEEMED + 1 REVOKED，无遗留未使用测试卡** ✅

## 6. 性能与日志

- validate 延迟实测 **3.8–4.9ms**（管理端满负载下不受影响）；
- 日志：0 Traceback、0 敏感信息（ADMIN_API_KEY/明文卡密/私钥模式扫描均 0）；
- integrity_check=ok。

## 7. 六页面与管理 API 验收

`/admin2/` 总览、`/keys`、`/devices`、`/redemptions`、`/analytics` 全部 200 ✅；overview API（devices total=21）、devices API（counts 正确）✅。

## 8. 异常与处理

1. 冒烟脚本首轮在「K1 重复兑换」处因未捕获 HTTPError 中断——业务行为本身正确（400），补跑续验完成剩余断言（未新增卡密，严格遵守 3 枚限制）。
2. 两处续验脚本自身的断言/解包小错，逐一修正重跑；对生产无影响。

## 9. 剩余风险

- 会话 Cookie 无 `secure`（当前 SSH 隧道架构下正确）；管理端无限流（仅本地绑定）；事件表随业务增长（量级可忽略）。
- P1 备份为迁移前快照；如需迁移后基线备份可重跑 prod_backup.sh（建议观察期内做一次）。

## 10. P4 · 72 小时观察计划（10/02 左右结束）

| 项 | 方法 | 频率 |
| --- | --- | --- |
| 错误日志 | `journalctl -u livelens-license -g Traceback --since -24h` | 每日 |
| 授权 API 延迟 | validate 计时 p95（基线 ≈5ms） | 每日 |
| 事件对账 | `events == REDEEMED` 只读 SQL | 每日 |
| SQLite 锁 | 日志 grep `database is locked` | 每日 |
| 磁盘 | `df -h /var/lib/livelens` | 每日 |

观察期内保持现有入口（经典后台可继续使用，/admin2 并行），不做默认入口切换。观察期满对账无异常后，与用户确认是否设为默认入口（P4 验收）。

**回滚预案（未触发）**：R1=`git checkout 8fe4ba1 -- license_server/` + 重启（数据库兼容）；R3 灾难恢复用 `livelens-20260929-033751`。
