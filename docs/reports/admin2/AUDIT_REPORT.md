# LiveLens Admin 2.0 — 第一阶段：现有系统审计报告

- 日期：2026-09-29
- 审计方式：只读（未修改任何现有文件、未连接生产服务器、未触碰生产数据库）
- 代码基线：`D:\TIikTok_Ocr\DouyinLowLatencyViewer`（git HEAD `396e9ff`）

---

## 1. 实际部署结构（已核实）

| 事实 | 依据 |
| --- | --- |
| 后台代码在 `license_server/`，是独立部署的 FastAPI 应用，与客户端仓库同源但单独部署 | `README.md`、`license_server/` 目录结构 |
| 生产实例运行在远程 Linux 主机 `118.31.113.99`，服务绑定 `127.0.0.1:8000`；本机通过 SSH 隧道 `ssh -N -L 8001:127.0.0.1:8000` 访问 `http://127.0.0.1:8001/admin/` | `LiveLens_Admin.bat` |
| 管理后台使用 **内置 SQLAdmin 0.31.1 快照**，位于 `admin_web/sqladmin/`；该目录被 `.gitignore` 排除（PROJECT_STATE.md 记录为 line 47），随部署单独投放，**不修改快照内任何文件** | `app/admin/__init__.py`（启动时自检：来源路径前缀 + 版本号 0.31.1） |
| 快照缺席时 `setup_admin()` 返回 None 并打 WARNING，`/admin` 不可用但许可 API 照常启动（7.2-7d 修复） | `app/admin/__init__.py:setup_admin` |
| **生产数据库不在本地**。本地两份 SQLite 均不可作为数据口径依据：根目录 `license_server.db`（2026-09-10，旧 schema——licenses 表还有 `device_id`/`max_devices`/`expires_at` 列，早于 7.2-5 重构，0 行）；`license_server/test_license_server.db`（测试库，0 行） | 本地实查 `sqlite_master` |
| 生产库 schema 应与 `app/models.py` 一致（4 张表），但**行数、`duration_days` 取值分布等真实数据只能在服务器上核实**（方法见 §5.4） | `models.py` + `database.py`（`DATABASE_URL=sqlite:///./license_server.db`，相对进程 CWD） |
| 没有发现许可服务器的自动化部署脚本；`scripts/` 下的发布脚本全部服务于客户端 Velopack 更新链路 | `scripts/` 目录实查 |

> 结论：本地代码可信，本地**数据不可信**。涉及真实数据的判断一律标注「待服务器核实」，未伪造任何统计。

---

## 2. 现有后台架构与功能清单

### 2.1 应用骨架

- 入口 `license_server/app/main.py`：FastAPI 应用；`DEBUG=false` 时关闭 Swagger/ReDoc；`setup_admin(app)` 把 SQLAdmin 挂载到 `/admin`。
- 业务时区 `BUSINESS_TZ_NAME="Asia/Shanghai"`（`config.py`）：仅用于 DAU 日切与后台展示，**存储与业务计算全部 UTC（naive）**。
- SQLite：WAL + 30s busy timeout（`database.py`）。

### 2.2 认证（`app/admin/auth.py`）

- 无用户表。登录名固定 `admin`，密码 = `settings.ADMIN_API_KEY`（与保护 `POST /api/v1/admin/licenses` 的运维凭据同源），`secrets.compare_digest` 比较。
- 会话 cookie 由 `SECRET_KEY` 签名；未配置 ADMIN_API_KEY 时 fail closed。
- 每个视图独立实现 `is_accessible()`，覆盖内置页 / `@action` / `@expose` 三条路径。

### 2.3 后台页面与操作（全部可正常工作，需保留的能力）

| 页面 | 类型 | 能力 | 2.0 保留策略 |
| --- | --- | --- | --- |
| License Keys 列表/详情 | SQLAdmin ModelView | 只读 + CSV 导出；按状态过滤（未使用/已兑换/已吊销）；按 key_hash 检索；datetime 列 naive UTC → 北京时间展示（`_datetime_formatter`） | 迁移为「卡密管理」，交互重做、口径不变 |
| 吊销所选 Key | `@action` | 仅 UNUSED 可吊销；REDEEMED/REVOKED 分级提示；303 回跳；Flash 消息（含 `_persist_flashes` 兼容补丁） | 保留（业务规则原样） |
| Authorization 列表/详情 | ModelView | 只读；设备标识、状态、到期/创建/激活时间；详情页展示该设备兑换过的 License（关系） | 迁移为「设备授权」 |
| 生成 License Key | BaseView + `generate_keys.html` | 批量生成：预设 1/30/180/365 天、单批 ≤1000、功能标签；整批单事务（`create_license(commit=False)`）；**明文仅显示一次**（只存 SHA-256），带 no-store 头；生成数与写入数一致性断言 | 迁移为「批量生成」弹窗，逻辑等价 |
| 兑换历史 | BaseView | 仅一个重定向入口 → License 列表 `?state=REDEEMED`（**没有独立事件日志表**，历史由 REDEEMED 行派生） | 升级为真正的「兑换记录」页 |
| DAU | BaseView + `dau.html` | 最近 30 天按业务日聚合（活跃设备数 + 心跳次数）+ 累计去重设备数；只读 | 迁移为「数据总览/数据分析」 |

### 2.4 后台之外的关联 API（原型中体现但不动协议）

- `POST /api/v1/licenses/activate`：兑换（状态机：UNUSED→REDEEMED；授权 新建/ACTIVE 累加/EXPIRED 重置/REVOKED 拒绝）。
- `POST /api/v1/licenses/validate`、`POST /api/v1/licenses/heartbeat`（DAU 写入，幂等 upsert）。
- `POST /api/v1/updates/releases` + `/updates/{channel}/{filename}`：更新分发（与授权系统解耦）。
- 管理员 KEY 生成走后台页面；API 侧 `POST /api/v1/admin/licenses`（X-API-Key）只支持单个创建。

### 2.5 依赖

`fastapi, uvicorn, sqlalchemy, pydantic/pydantic-settings, python-multipart, wtforms, jinja2, itsdangerous, tzdata`（Windows）+ 内置 sqladmin 0.31.1 快照。前端资源（Tabler/Bootstrap/Awesomeicons）全部来自 SQLAdmin 快照内置静态文件。

---

## 3. UI 架构存在的问题（2.0 要解决的）

1. **无总览**：登录后落地的是 License 表格页，没有任何 KPI、趋势图或待办提示；DAU 与授权数据割裂在两个页面。
2. **信息架构以「数据库表」为中心**，而不是以运维任务为中心：生成、查询、吊销、看数分散在 SQLAdmin 通用 CRUD 页里，没有工作流串联（如从兑换记录一键跳到设备详情）。
3. **SQLAdmin 框架天花板**：聚合页只能用 BaseView + 自写模板绕行（DAU 页已走到这条路），每加一个统计页都要复制一遍 Bootstrap 结构；列表页无法做日期区间过滤、多条件组合筛选。
4. **视觉不可品牌化**：默认 Tabler 浅色风格，与 LiveLens 客户端「Studio Minimal」深色风格不一致；月份显示为英文（`%d %B %Y`，如 "26 September 2026"），信息密度低（时间戳用大号 badge 渲染）。
5. **移动端不可用**：表格无横向滚动优化，SQLAdmin 布局以桌面为主，手机上基本不能运维。
6. **兑换记录是伪页面**：只是跳转到带过滤参数的 License 列表；看不到「首次激活 vs 续费」，看不到授权变更（累加/重置）。
7. **KEY 只能按指纹检索**：这是安全模型（只存 SHA-256）决定的，但当前没有提供「明文 Key → SHA-256 → 指纹查询」的反查入口，运营拿到用户发来的明文 Key 时无法定位记录。
8. **无空状态/加载态/批量操作反馈的设计规范**：Flash 消息是唯一反馈通道（且历史上还有 session 不回写的坑，已打补丁）。
9. **登录页为 SQLAdmin 默认英文页**，无品牌、无错误引导。
10. **可审计性缺失**：无操作日志（谁在何时生成/吊销了什么），依赖数据库状态反推。

---

## 4. 数据口径设计（2.0 采用，全部可在现有 schema 上落地）

### 4.1 四个核心指标的定义与来源

| 指标 | 定义 | 现有 schema 上的计算方式 | 状态 |
| --- | --- | --- | --- |
| **首次激活设备（新增设备）** | 第一次出现 Authorization 的设备数，按日归档 | `Authorization.created_at`（创建即首次兑换，`find_or_create_authorization` 保证）；按日归档 = UTC→北京时间换算后取日 | ✅ 可准确计算 |
| **兑换次数** | 成功的兑换动作总数（每张 REDEEMED 卡一次） | `COUNT(License WHERE state='REDEEMED')`，按 `redeemed_at` 归档 | ✅ 可准确计算 |
| **老设备续费** | 兑换发生时设备已有 Authorization（含 ACTIVE 累加与 EXPIRED 重置两种） | `License.redeemed_at` 与其 `authorization_id → Authorization.created_at` 比较：晚于创建日即续费；细分「累加/重置」需对比兑换前后 `expires_at`，**当前无字段记录兑换前到期时间，历史数据无法细分** | ⚠️ 大类可算，细分不可回溯 |
| **DAU** | 当日启动过客户端的设备数（含已过期设备，设计如此） | `device_daily_active` 按 `(device_id, active_date)` 写入时去重，`active_date` 已按 `Asia/Shanghai` 日切 | ✅ 已按北京时间归档 |

### 4.2 套餐（天卡/月卡/年卡）分类

- 现状：**没有套餐维度**，唯一的信号是 `License.duration_days`。生成端白名单为 `1/30/180/365`（`views.py:DURATION_PRESETS`），API 校验 `gt=0, le=365`（`schemas.py`）。
- 2.0 口径映射：`1 → 天卡`、`30 → 月卡`、`180 → 半年卡`、`365 → 年卡`；**180 天卡不在用户要求的「天/月/年」三分法内，映射表必须包含半年卡**（否则历史 180 天卡会被错误归入月卡或年卡）。
- 2.0 界面按映射显示套餐名；后端保留 `duration_days` 原值，映射规则集中一处，未来新增套餐（如 7 天卡、90 天卡）只改映射 + 预设白名单。
- 风险：API `gt=0, le=365` 允许任意天数（例如 45 天），若历史上通过 API 发过非预设时长的卡，映射会出现「其他」分类。**需在服务器上核实**（见 §5.4 SQL-3）。

### 4.3 北京时间归档

- DAU 已在**写入时**按北京时间日切（`heartbeat_service.bucket_date`），无需换算。
- `licenses.created_at / redeemed_at`、`authorizations.*` 均为 naive UTC，**按北京时间分日统计必须在查询层换算**（UTC+8 取日）。SQLite 无时区函数，实施阶段建议：在 Python 聚合，或新增 `active_date_beijing` 生成列/冗余列；**原型阶段只定口径，不动库**。
- 展示层统一「YYYY-MM-DD HH:mm」北京时间标注，不再用英文月份。

### 4.4 历史数据缺口（诚实声明，不伪造）

| 缺口 | 影响 | 补救 |
| --- | --- | --- |
| 兑换前的 `expires_at` 未记录 | 无法回溯每次续费是「累加」还是「重置」，授权变更流水不完整 | 2.0 起新增兑换事件日志表（含 before/after），历史数据只到「是续费」粒度 |
| 无套餐维度 | 天/月/年分类只能由 `duration_days` 派生；非预设时长无法归类 | 先跑 §5.4 SQL-3 核实分布；必要时补「其他」分类 |
| 无操作审计 | 谁/何时生成、吊销无法追溯 | 2.0 实施阶段加 admin_audit_log（本次不改） |
| 生产库在远程服务器，本地无法统计真实行数 | 总览数字的真实基线未知 | 用 §5.4 的三条 SQL 在服务器核实后填入实施阶段验收标准 |
| DAU 数据从 7.4 版本客户端上线才开始累积 | 上线前的「DAU」不存在 | 图表从有数据的第一天画起，空数据显式标注「该时段无采集」 |

---

## 5. 附录

### 5.1 需保留的既有行为清单（实施阶段验收用）

- 明文 Key 只显示一次、只存 SHA-256、no-store；批量整批单事务，失败全回滚。
- 仅 UNUSED 可吊销；REDEEMED 不可吊销且不收回既有授权（UI 文案不得暗示相反）。
- 授权状态机：新建 ACTIVE、ACTIVE 累加、EXPIRED 重置、REVOKED 拒绝。
- fail-closed 登录；会话判定覆盖 action/expose。
- 存储保持 naive UTC；仅展示与日切用北京时间。
- `/admin`（SQLAdmin）继续可用：2.0 作为新控制台并列上线，SQLAdmin 作为「经典后台」保留入口，二者共存、互不影响（本阶段边界）。

### 5.2 现有页面路由一览

`/admin`（重定向列表）、`/admin/login`、`/admin/license/list|detail`、`/admin/authorization/list|detail`、`/admin/generate-keys`、`/admin/redeemed-history`、`/admin/dau`，以及 SQLAdmin 内置 api/schema/exports 路由。

### 5.3 已知运维问题（顺带记录，不属于 UI 范围）

- `SUSI_DEVELOPMENT_PRIVATE_KEY` 未配置时 `POST /licenses/activate` 会挂起 90s 后 500（PROJECT_STATE.md 记录，生产已配置则无碍）。
- 数据库路径 `sqlite:///./license_server.db` 相对进程 CWD，换目录启动会静默新建空库（配置审计项）。

### 5.4 待在服务器执行的核实 SQL（只读）

```sql
-- SQL-1：真实数据规模
SELECT (SELECT COUNT(*) FROM licenses) AS licenses,
       (SELECT COUNT(*) FROM authorizations) AS devices,
       (SELECT COUNT(*) FROM device_daily_active) AS dau_rows;

-- SQL-2：duration_days 实际分布（决定套餐映射是否出现「其他」）
SELECT duration_days, state, COUNT(*) FROM licenses GROUP BY duration_days, state;

-- SQL-3：DAU 覆盖区间（确定趋势图可用起点）
SELECT MIN(active_date), MAX(active_date) FROM device_daily_active;
```

---

## 6. 下一阶段（实施）预计修改的文件

**新增：**

- `license_server/app/admin2/`（新控制台路由 + 模板，与现有 `admin/` 并列；SQLAdmin 快照零改动）
- `license_server/app/services/stats_service.py`（聚合查询：总览/按日统计/兑换流水，全部北京日切）
- `license_server/app/templates/admin2/*`（Jinja2 页面，对应本原型 6 页）
- `license_server/migrate_admin2.py`（可选：兑换事件日志表 + 北京日冗余列的迁移脚本，先评审再执行）

**修改：**

- `license_server/app/main.py`：仅追加 `setup_admin2(app)` 一行（与 `setup_admin` 并列，不动既有路由）。
- `license_server/app/admin/views.py`：仅在 `GenerateKeysView` 生成成功路径上追加事件日志写入（行为不变）；其余不动。

**不修改：** `admin_web/sqladmin/**`（快照逐字节一致）、`models.py` 既有表结构（新增走迁移脚本）、`redemption_service.py` 状态机、`schemas.py`、客户端代码、生产数据库（上线前单独评审迁移脚本）。
