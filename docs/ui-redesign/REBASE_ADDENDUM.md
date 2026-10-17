# REBASE_ADDENDUM — 基线变更与合并结果（Phase 7 期间）

> **日期**：2026-10-14
> **背景**：Phase 7 性能 A/B 期间，项目负责人指出"未激活页面的联系作者已改成购买授权、微信已换成 QQ"，
> 而当时看到的界面仍是旧版。经核实，该改动存在于发布分支，我的 UI 分支基线不含它。
>
> 本文档记录**基线变更、冲突解决与合并后验证**。Phase 0–6 各阶段报告的实质内容不变，
> 但其中标注的"基线 HEAD `f0418ea` / `dbd4ea0`"均已被本次 rebase 取代，以本文档为准。

---

## 1. 问题（FACT）

| 项 | 值 |
|---|---|
| 我 Phase 0 时的基线 | `f0418ea`（`feature/bare-stream-integration` 顶端） |
| 我据此导入的在途工作 | commit `18476a6`（patch 方式导入主工作区未提交改动） |
| 发布线实际状态 | `feature/release02-2.2.0` = `d3b2c78`，**领先我 14 个提交** |
| 落后的 14 个提交包含 | `31baf58` **购买授权 + QQ**、`2a2724f` B站适配器、`83bd8ed` **移除 _uhd 旧拉流链路**、`112f6f3`/`a3cff00`/`0e9d9d5` 裸流接入、`d3b2c78`/`b96aa0f` FFmpeg7 运行时、`1fcf4f6` 版本 2.2.0 |

**结论**：我的 UI 分支基于一个**落后发布线 14 个提交**的基线，因此看不到「购买授权」与 QQ。

> **为什么 Phase 0 没发现**：Phase 0 审计的是**主工作区**所在的 `feature/bare-stream-integration`
> 分支，并按其未提交改动建立基线。当时未审计其它分支，也未把"项目最前线是哪个分支"作为审计项。
> 这是本次任务的**审计缺口**，如实记录。

## 2. 决策（项目负责人 2026-10-14 确认）

**rebase 到 `feature/release02-2.2.0`（`d3b2c78`）**。

理由：它是发布线顶端（版本 2.2.0 + 发布脚本），包含全部在飞工作；仅 rebase 到
`31baf58` 仍会落后 13 个提交。

## 3. 执行（FACT）

### 3.1 安全措施

- 回退网：`git branch -f feature/ui-liquid-glass-pre-rebase 6b7cf0b`
  —— 旧基线上的全部 8 个提交（含 `18476a6`）**完整保留，未删除**。
- 命令：`git rebase --onto d3b2c78 18476a6 feature/ui-liquid-glass`
  —— **重放 `18476a6..6b7cf0b` 的 7 个提交**，并**丢弃 `18476a6`**。
- `18476a6` 被丢弃的理由：它导入的是 `feature/bare-stream-integration` 上的未提交在途工作；
  同一份功能在 `release02-2.2.0` 上已由 `0e9d9d5`/`98e6394`/`112f6f3`/`a3cff00` 等提交
  **正式落地**。保留它只会与已提交的版本在同一批文件上互相覆盖。
  **该在途工作本身未丢失**：主工作区仍有未提交副本，旧基线分支亦留有提交。

### 3.2 冲突与解决

两处冲突，**都在 `gui/widgets.py` 的 import 行**（无内容冲突）：

| # | 提交 | 冲突原因 | 解法 |
|---|---|---|---|
| 1 | `00f17fe` Phase 4 | 发布线加 `QUrl`/`QDesktopServices`；我加 `QPainterPath` | 合并：同时导入四者 |
| 2 | `6b7cf0b` Phase 6 | 发布线加 `QUrl`/`QDesktopServices`；我加 `QLinearGradient` | 合并：同时导入三组 |

`gui/main_window.py` **自动合并成功**（发布线改了 +327 行，主要是裸流/FFmpeg7/B站接入，
与我改的工具栏结构、水印挂载、Palette 互不重叠）。

### 3.3 合并后语义核对（关键）

**`main_window.py` 自动合并后逐项确认（FACT）**：

| 项 | 期望（来自发布线） | 合并后实际 |
|---|---|---|
| `settings_dialog.py` 联系方式 | `CONTACT_QQ = "1752924057"`，无微信/邮箱 | ✅ `CONTACT_QQ = "1752924057"`；`CONTACT_WECHAT`/`CONTACT_EMAIL` **已不存在** |
| 购买链接 | `PURCHASE_URL`（淘宝） | ✅ 存在且被 `_on_purchase_clicked` 使用 |
| 未激活遮罩按钮 | 「**购买授权**」→ 系统浏览器打开购买链接 | ✅ `QPushButton("购买授权")` + `purchase_clicked` → `_on_purchase_clicked` → `open_external_url(PURCHASE_URL, self)` |
| 标题栏按钮 | 「联系作者」→ 弹出 QQ 卡片（发布线同样是「联系作者」） | ✅ `IconButton.CONTACT, "联系作者"` → `_on_contact_clicked` → `_open_contact_popup` |
| 联系方式弹卡 | 仅 QQ，无微信/邮箱 | ✅ `("QQ", CONTACT_QQ, "已复制 QQ 号")` 单条 |
| `open_external_url()` 助手 | 系统浏览器 + 失败明确提示 | ✅ 来自发布线，完好 |

**重要澄清**：发布线 `d3b2c78` 的**标题栏按钮本身就叫「联系作者」**（`31baf58` 只改了
未激活遮罩里的按钮）。因此我的标题栏「联系作者」**与发布线一致，不是回退**。

## 4. 合并后验证（FACT）

| 验证 | 结果 |
|---|---|
| `python -m compileall gui/ tests/ui_redesign/` | ✅ 通过 |
| 导入 `gui.main_window` / `gui.watermark` / `gui.widgets.open_external_url` | ✅ 通过 |
| **真实启动**（`ui_baseline_probe.py`） | ✅ `ok: true`、`exited_cleanly: true` |
| 截图 `phase7_rebase_screenshot.png` | ✅ 遮罩显示「**购买授权**」；「立即激活」为强调色；**水印 `LiveLens` 可见**；游戏下拉显示「三角洲行动」 |
| 12 项既有回归测试 | ✅ **11 项与 rebase 前完全一致**；1 项（`test_roi_persistence_gui.py`）**文件已不存在** —— 它是 `18476a6` 导入的在途测试，随之被 rebase 丢弃，功能已由发布线提交正式覆盖 |
| 新增 `test_toolbar_contract.py` | ✅ **72 项通过 / 0 失败** |
| 新增 `test_watermark.py` | ✅ **60 项通过 / 0 失败** |

**FACT — 零回归、零新增失败。** 我的 132 条新断言在新基线上全部通过。

## 5. 新历史（FACT）

```
05a5ad6 perf(ui): Phase 7 水印刷新间隔 33ms→40ms + 尺寸缓存
8fbe710 feat(ui): Phase 6 深色液态玻璃主题落地
e51302e feat(ui): Phase 5 未激活动态水印（DVD 屏保式，独立覆盖层）
ca83484 feat(ui): Phase 4 顶部工具栏重构 + 设置齿轮图标修复
eed51fc test(ui-redesign): Phase 3 隔离工作区运行环境与基线
0573bb2 docs(ui-redesign): Phase 2 现有 UI 与授权状态审计
29ca4bc docs(ui-redesign): Phase 0/Phase 1 Skill 审计 + 设计系统
d3b2c78 release(2.2.0): bundle FFmpeg 7 playback runtime   <-- 新基线
...
```

## 6. 设计文档的连带修正

`design-system/livelens/pages/preview-watermark.md` §5 与
`docs/ui-redesign/TOOLBAR_SPEC.md` 中提到的「联系作者」语义不变；但需补充：
**未激活遮罩内的入口已是「购买授权」（打开系统浏览器），不再是联系方式弹卡。**
水印规格 §2 的"授权状态来源"不受影响。

## 7. 遗留项

| # | 项 | 类型 |
|---|---|---|
| R1 | 性能 A/B 的基线 worktree `_wt_uilg_base` 基于旧基线 `dbd4ea0`，**其数据仅对旧基线的"无购买授权改动"有效** | FACT |
| R2 | 需在 `release02-2.2.0` 基线上重做一次水印开/关的 CPU 对照 | 待办（Phase 7） |
| R3 | 主工作区的未提交在途工作是否与发布线重复/冲突，属项目负责人决策 | 超出本次 UI 任务范围 |
