# LiveLens_UI_OPTIMIZATION_REPORT.md

日期：2026-09-30
范围：直播历史记录重构 / 极速模式显示修复 / 默认播放模式调整
原则：高内聚低耦合；核心链路（连接、播放、OCR、授权）零改动；audio 模块不动结构

---

## 1. 修改文件

新增：
- `stream/history.py` — 历史记录三件套：`LiveRoomHistoryItem`（模型）/ `HistoryRepository`（JSON 持久化 + 旧格式迁移）/ `HistoryService`（用例层，GUI 唯一接触点）
- `gui/history_panel.py` — `HistoryButton`（地址栏入口按钮）+ `HistoryPopup`（卡片式弹窗）+ `HistoryCard`（单条卡片，独立 widget 为扩展留位）
- `tests/history/test_history.py` — 15 项单测（模型/仓库/迁移/服务）

修改：
- `gui/main_window.py` — 接线改造（详见 §2）：QComboBox 历史下拉 → HistoryButton；删除死代码 `_save_stream_history`；`_connect` 移除连接前预保存；`_handle_connect_success` 记录成功；`_sync_address_columns` 退化为兼容 no-op（历史钩子）
- `gui/audio_panel.py` — 模式下拉：极速模式排第一（默认）；`setFixedWidth(92)` → `AdjustToContents + setMinimumContentsLength(5)`
- `audio/models.py` — `AudioConfig` 默认 mode = `LOW_LATENCY`；枚举注释更新
- `audio/service.py` — `AudioService` 默认 mode = `LOW_LATENCY`
- `tests/audio/test_audio_panel.py` — 默认模式/自适应宽度断言；`test_audio_buffer_strategy.py`、`test_audio_service_lifecycle.py` — 泵保序类测试显式固定 SYNC 模式（测试本意与默认模式无关）

未动：`stream/ffmpeg_reader.py`、视频/OCR/授权链路、`utils/stream_history.py`（保留：其它模块引用其 `default_data_dir()`；主用法已切换到新服务）。

## 2. 架构设计

### 需求一：直播历史

```
main_window（接线，无业务逻辑）
   │ record_success / set_note / clear / items
   ▼
HistoryService（stream/history.py）      ← 用例层：成功才记录、LRU、排序
   ▼
HistoryRepository                        ← 持久化：JSON、线程安全、旧格式一次性迁移
   ▼
LiveRoomHistoryItem（dataclass）         ← 模型：id/url/room_name/room_id/
                                            created_at/last_used_at/success_count
                                            （+seq 单调序号保 LRU 稳定）

HistoryButton ──点击──▶ HistoryPopup ──HistoryCard×N──▶ [进入] → enterRequested(url)
                                                            → main_window: 填入地址框 + 直接连接
```

关键规则变化：
- **成功才记录**：旧代码在 `_connect` 发起解析**前**就写入历史；现在 `_connect` 只记住 `_pending_history_url`，`_handle_connect_success`（解析成功、GUI 线程）才调用 `record_success(url, room_id)`。room_id 来自 StreamInfo（平台解析结果），失败/断线不产生记录。
- **URL 不出数据层**：URL 仅作存储键；卡片展示 = 备注名（无备注 → "未命名直播间"）+ 房间号（优先 room_id，兜底 URL 提取）+ 最近使用日期。集成测试断言 UI 文本中不含 "https://"。
- **备注机制**：沿用地址栏 "⋯" 菜单的「编辑备注…」（跟随输入框内容），写入 `service.set_note`；卡片主标题随之切换为备注名。
- **兼容**：首载时把旧 `stream_history.json`（{"streams":[…]}）无损迁移为新格式（nickname→room_name，URL 提取 room_id，迁移即落盘）。
- **扩展留位**：加收藏/删除/排序 = 在 HistoryCard 加按钮或 HistoryService 加用例，存储结构（version 字段）与 main_window 均不动。

### 需求二：极速模式显示

模式下拉去掉 `setFixedWidth(92)`，改 `setSizeAdjustPolicy(AdjustToContents)` + `setMinimumContentsLength(5)`：宽度由内容 sizeHint 决定（实测 121px > 旧 92px），"同步模式/极速模式"完整显示；未来新增更长名称的模式自动加宽，不会截断也不会撑破布局（工具栏 row 对 stretch 不敏感，其余控件宽度均独立）。

### 需求三：默认极速模式

`AudioMode` 是播放模式的**单一事实来源**（策略模式已存在于 `audio/strategy.py`：`create_strategy` → `LowLatencyStrategy/SyncStrategy`），未新建重复的 PlaybackMode 枚举——双枚举会造成两处 if-else 漂移，违背"播放模式判断不散落"。默认值在三处统一翻转：`AudioConfig`（数据）、`AudioService` 构造默认（门面）、`AudioPanel._MODE_ITEMS` 顺序（UI 第一项=默认）。用户切换即时生效（worker 下次迭代换策略，沿用既有机制）。

## 3. 为什么这样设计

- **HistoryService 单入口**：main_window 不再直接摸 JSON/文件，历史规则（成功才记、LRU 上限）集中在 service 一处，可单测、可被未来自动重连等功能复用。
- **卡片独立 widget**：`HistoryCard` 自带自己的信号与布局，新增操作按钮不改动列表容器和弹窗结构——这是 Qt 复合控件的标准做法。
- **弹窗打开时实时读取**：省掉所有"数据变了要刷新 UI"的同步代码（旧 `_load_recent_streams` 的 5 处调用点全部删除），数据源唯一。
- **固定宽度按钮替代动态宽度下拉**：删除了"按面板宽度钉死下拉宽度"的整套联动逻辑（`_sync_address_columns` 保留为兼容 no-op 使调用点不失效），地址栏全部固定宽度控件，布局更简单可预期。
- **不建 PlaybackMode 新枚举**：播放模式与音频缓冲模式在本产品是同一个概念（极速=丢旧缓冲追最新），已有策略模式承载；需求中的"策略模式管理"已满足。

## 4. 测试结果

| 检查项 | 结果 |
|---|---|
| tests/history（模型/仓库/迁移/服务 15 项） | 15/15 通过 |
| tests/audio（37 项，含默认模式/面板宽度新断言） | 37/37 通过 |
| 既有回归（stream_resolver / frame_ready / stream_history_paths 共 65 项） | 65/65 通过 |
| 1. 软件正常启动 | MainWindow 离屏实例化 OK |
| 2. URL 输入正常 | setText/读取 OK |
| 3. 历史正常保存 | record_success 成功才写入；失败/空 URL 不记录；LRU 上限 10；同秒排序稳定 |
| 4. 历史展示正常 | 卡片显示"今晚比赛直播 / 房间号：96670111738 / 最近使用：2026-09-30"；**断言 UI 无 URL**；旧格式迁移（含中文备注）无损 |
| 5. 极速模式默认生效 | AudioService().mode == LOW_LATENCY；下拉第一项"极速模式" |
| 6. 同步模式可切换 | 下拉切换 → service.set_mode 即时生效（双向验证） |
| 7. 视频播放链路无影响 | ffmpeg_reader 零改动；回归 65 项通过；main_window 导入通过 |
| 模式下拉宽度 | sizeHint 121px（旧固定 92px 会截断） |

## 6. 补丁：HistoryPopup 生命周期修复（2026-09-30）

**现象**：开→点外关闭→再开即 `RuntimeError: Internal C++ object (HistoryPopup) already deleted`。

**根因**：popup 设了 `WA_DeleteOnClose`，关闭时 C++ 对象销毁，但 `HistoryButton._popup` 仍持有失效的 Python wrapper；再次点击对其调用 `isVisible()` 触发 RuntimeError。

**修复**（`gui/history_panel.py`，生命周期逻辑收敛在按钮内，不散落 main_window）：
- `popup.destroyed` 信号 → `_on_popup_destroyed` 清理 `self._popup = None`；
- `_on_clicked` 因此可保证"`_popup` 非 None ⇒ wrapper 有效"，toggle 分支安全 close；
- 无 try/except 掩盖，无弱引用技巧。

**测试**（`tests/history/test_history_panel.py`，3 项）：
- 打开→关闭→重开 ×20 无 RuntimeError；
- 按钮 toggle 语义 ×20（每次断言引用被清理）；
- [进入] 关闭后可重开且信号正常转发 ×5。
结果：tests/history 18/18、tests/audio 37/37 全部通过。

## 7. 补丁：首次进入直播间备注询问（2026-09-30）

**流程**：连接成功 → `HistoryService.record_success` 判定首次访问（新条目=首次）→ 记录先落盘 → 触发 `remark_request` 回调 → `HistoryButton.on_remark_request` 弹 `RemarkDialog` → 保存（非空才 set_note）/ 跳过（历史已在、remark 空）。

**改动**（均为历史模块内，main_window 仅 1 行接线，stream/audio/ocr/license 零改动）：
- `stream/history.py`：`set_remark_callback(cb)` + `record_success` 内 `is_first_visit` 判定 + `_notify_remark_request`（回调异常吞掉，不影响记录）；回调是纯 Python callback——本模块不依赖 Qt。
- `gui/history_panel.py`：`RemarkDialog`（房间号提示 + 输入框 + 保存/跳过，Enter=保存、Esc=跳过）；`HistoryButton.on_remark_request` 槽。
- `gui/main_window.py`：1 行 `self._history.set_remark_callback(self._history_btn.on_remark_request)`。

**测试**（tests/history/test_history_remark.py，8 项）：① 新直播间首次进入触发一次且带 room_id；② 已存在备注/再次进入不触发；③ 跳过后历史仍保存、remark 为空；④ 失败连接（不产生 record_success 调用）不触发、不产生记录；⑤ 回调抛异常不影响记录；⑥ 未注册回调静默；⑦⑧ Dialog 保存/空白两条出口。结果：tests/history 26/26、tests/audio 37/37、既有回归 65/65、真实 MainWindow 接线集成验证通过。

## 8. 补丁：备注状态机 remark_status（2026-09-30）

**问题**：备注询问的触发条件是"条目是否首次创建"，与"用户是否处理过备注"混为一个状态——首次进入跳过后，从历史再次进入永远不会再提示。

**修复**：`LiveRoomHistoryItem` 增加显式状态字段 `remark_status`，询问条件改为 `status == UNSET`（与访问次数正交）。

**状态流转**：

```
record_success（只在成功进入时调用）
        │
        ├─ 新条目 ──────────→ UNSET ──弹窗──┬─ 保存非空备注 → SET    （不再弹）
        │                                   └─ 跳过/确认空   → SKIPPED（不再弹）
        └─ 已有条目：
            remark_status == UNSET   ──────→ 再次弹窗（未处理过）
            remark_status == SKIPPED ──────→ 不弹（可手动改备注）
            remark_status == SET     ──────→ 不弹
            remark_status == 非法值   ─────→ 回落 UNSET（宁可多问不丢提示）
```

**修改文件**：
- `stream/history.py`：`RemarkStatus` 枚举；模型字段+容错反序列化；`record_success` 触发条件改查状态；新增 `mark_remark_skipped`；`set_note` 同步状态（非空→SET，清空→SKIPPED）；旧格式迁移映射（有备注→SET，无备注→UNSET 补一次询问）。
- `gui/history_panel.py`：`on_remark_request` 两个出口都写回状态（跳过/空确认 → `mark_remark_skipped`）。
- `gui/main_window.py`：零改动（接线不变）。
- `tests/history/test_remark_status.py`（新增 11 项）。

**测试**：tests/history 37/37（状态流转 6 场景 + 持久化 roundtrip + 非法值回落 + Dialog 出口写回 ×3）、tests/audio 37/37、既有回归 65/65、main_window 导入通过。

## 9. 潜在风险（原文第 5 节）

1. **旧数据迁移**：首次启动时旧 stream_history.json 会迁移为 version 2 格式；迁移是单向的（旧文件被覆盖为新格式）。旧版本程序再读取会走"加载失败→空历史"路径（旧代码容错已存在），可接受。
2. **room_id 兜底提取**：迁移条目的 room_id 从 URL 第一段路径提取，非抖音域名的路径段可能与平台房间号不一致——仅影响展示，连接始终用完整 URL。
3. **`_pending_history_url` 生命周期**：连接失败后该值保留到下次 `_connect` 被覆盖；由于 `_handle_connect_success` 只可能由最近一次 `_connect` 的 worker 触发（attempt_id 失效机制），不会错记旧 URL。
4. **「编辑备注…」仅对输入框当前地址生效**：与改造前行为一致；对历史里"其它"条目改备注需先在历史卡片进入或粘贴其地址（未来可在卡片上直接加"改备注"按钮，结构已留位）。
5. **HistoryPopup 使用 Qt.Popup 窗口标志**：点外部自动关闭；在极小屏幕上最小宽 280px 可能超出窗口——已限制最大 360px 并按按钮右缘对齐，极端窄窗口（<360px）下可能贴屏幕左缘。
6. **默认极速模式的声音延迟特性**：极速模式缓冲上限 120ms，网络抖动大时可能比同步模式更易出现瞬间静音（策略固有取舍，用户可手动切回同步模式）。
