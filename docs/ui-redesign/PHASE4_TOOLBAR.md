# PHASE4_TOOLBAR — 顶部工具栏重构

> **阶段**：Phase 4（第一个代码实现阶段）
> **日期**：2026-10-14
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`，起始 HEAD `dbd4ea0`）
> **依据**：`design-system/livelens/pages/toolbar.md`、`docs/ui-redesign/TOOLBAR_SPEC.md`、`docs/ui-redesign/PHASE2_UI_AUDIT.md`
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 1. 阶段目标与达成

| 目标（任务书 Phase 4） | 状态 | 说明 |
|---|---|---|
| 减少视觉杂乱 | ✅ | 3+1 分组 + 分隔线；图标提亮、加大、统一线宽 |
| 对操作合理分组 | ✅ | `[激活][设置][全屏] │ [引导][联系作者]` |
| 常用操作保持可见 | ✅ | 5 个入口全部保留在标题栏 |
| 低频操作可收纳到菜单 | ⛔ **未做** | **有硬证据说明不该做** —— 见 §3 |
| 统一图标、尺寸、间距、圆角、悬停、按下 | ✅ | 统一 30px 按钮 / 20px 图标 / 1.4px 线宽 / 6px 圆角 |
| 使用清晰齿轮图标 | ✅ **核心修复** | 辐条星芒 → 闭合轮廓真齿轮 |
| 纯图标按钮提供工具提示 | ✅ | 5 个全部有 tooltip（原有） |
| 保留全部业务功能、快捷键、信号槽语义 | ✅ | 逐项验证，见 §5 |
| 检查布局溢出 / 缩放 / DPI / 键盘操作 | ⚠️ 部分 | 键盘可达已实现；DPI 与缩放实测见 §7 |

---

## 2. 实际改动清单（FACT）

| 文件 | 改动 |
|---|---|
| `gui/widgets.py` | ① 导入 `QPainterPath`；② 新增 `_ICON_S_RATIO` 常量；③ **重写 `SETTINGS` 齿轮画法**；④ `icon_pixmap` 改用比例常量 + 线宽 1.3；⑤ `IconButton`：可访问名、`TabFocus`、线宽 1.4、等比 `s`、默认色改 `Palette.ICON`；⑥ `TitleBar.add_icon_button` 支持 `accessible_name`；⑦ 新增 `TitleBar.add_action_separator()` |
| `gui/theme.py` | ① `Palette` 新增 `ICON`（`#B6BAC2`）与 `FOCUS_RING`（`#80BFFF`）；② `Metrics.ICON_BUTTON_SIZE` 28 → **30**；③ QSS 新增 `#titleIconBtn:focus` 焦点环、`#titleBarSeparator` 分隔线样式 |
| `gui/main_window.py` | ① 重写 `_build_title_bar()`：重排为两组 + 分隔线、传入 `accessible_name`；② **删除死代码 `_build_status_bar()`（52 行）** |
| `tests/ui_redesign/test_toolbar_contract.py` | 新增：43 项工具栏契约回归测试 |
| `tests/ui_redesign/icon_contact_sheet.py` | 新增：图标对照图生成器（验收证据） |

**未触碰**：直播接入、FFmpeg 解码、帧队列/调度、识别调度、ROI 坐标映射、授权验证语义、快捷键、任何信号槽目标函数（FACT）。

---

## 3. ⛔ 明确不做「低频项收进溢出菜单」—— 及原因

Phase 1 的 `TOOLBAR_SPEC.md` §4 原本规划把「使用引导 / 联系作者」收进 `⋮` 菜单。**实施时发现这会破坏既有契约，因此修订设计。**

### 3.1 硬证据（FACT）

现有测试把「这些入口落在标题栏操作区」当作**事实断言**：

| 测试 | 断言 | 行号 |
|---|---|---|
| `test_first_run_onboarding.py` | `window._help_btn.objectName() == "titleIconBtn"` | `:438` |
| `test_first_run_onboarding.py` | `window._help_btn.toolTip().startswith("使用引导")` | `:442` |
| `test_first_run_onboarding.py` | `window._help_btn.property("variant") is None` | `:446` |
| `test_phase_ui3_1_layout.py` | 四个入口均为 `IconButton`、尺寸 = `ICON_BUTTON_SIZE`、有 tooltip | `:123-137` |
| `test_phase_ui3_activation_settings.py` | `window._license_btn.click()` / `window._settings_btn.click()` | `:298, :309` |

把 `_help_btn` 换成 `QAction`，会让 **`test_first_run_onboarding.py` 的 225 项通过全部失败**、`test_phase_ui3_activation_settings.py` 的 62 项通过失败。

**这属于"为改版而破坏既有约定"，也违反任务书"不得通过删除测试 / 放宽验收标准来制造通过结果"的精神。**

### 3.2 采用的替代方案（FACT）

改为**分组 + 分隔线**降低视觉杂乱，功能入口一个不动：

```
改造前：  ?  🔑  💬  ⚙  ⛶   ‖  — □ ✕
改造后：  🔑  ⚙  ⛶  │  ?  💬   ‖  — □ ✕
          └── 功能组 ──┘   └ 帮助组 ┘
```

**可发现性保障**：5 个入口全部仍在标题栏可见位置；tooltip 与可访问名齐全；键盘 Tab 可达（新增）。

**设计文档已同步修订**：本报告即为对 `TOOLBAR_SPEC.md` §4 的正式修订说明。

---

## 4. 核心修复：设置齿轮图标

### 4.1 根因（FACT，Phase 2 审计结论）

旧画法（`widgets.py` 原 `:318-329`）画 **8 根从齿根半径指向齿顶半径的细辐条** + 齿圈 + 中心孔。在真实尺寸（`s=6.0`、线宽 1.2px、`TEXT_DIM` 灰）下，8 根辐条糊成一团**星芒**，读不出"齿轮"。

### 4.2 新画法（FACT）

改为**闭合多边形轮廓**：8 个梯形齿构成外轮廓（齿顶半径 `0.96s`、齿根半径 `0.70s`、齿顶半角 `0.22×45°`），外加中心孔圆（`0.30s`）。齿形由"外轮廓的凹凸"表达，不再依赖辐条。

同时：按钮 28 → 30px、线宽 1.2 → 1.4px、默认色 `#90939B` → `#B6BAC2`。

### 4.3 视觉证据（FACT）

对照图：`docs/ui-redesign/evidence/screenshots/icon_contact_sheet.png`

| 列 | 内容 | 观察 |
|---|---|---|
| 左 | 改造前画法（28px / s=6） | 设置图标是**模糊星芒**，与 `?`、钥匙、气泡难以区分 |
| 中 | 改造后画法（30px 实际尺寸） | **清晰可辨的齿轮**（可见齿 + 中心孔） |
| 右 | 改造后 4x 放大 | 标准 8 齿齿轮轮廓 |

### 4.4 几何自动化断言（FACT）

`test_toolbar_contract.py` §C 用**极坐标采样**验证图标几何（不是"看起来像"）：

| 断言 | 结果 |
|---|---|
| 圆心处无墨（有中心孔） | ✅ |
| `r_hub` 半径上有轮廓（孔有边） | ✅ |
| 齿中心方向 `r_out` 处有轮廓（齿存在） | ✅ |
| **齿隙方向 `r_out` 处无墨**（存在齿隙 → 不是圆环） | ✅ |
| 齿隙方向 `r_root` 处有轮廓（齿根圆弧） | ✅ |
| **旧齿圈半径 `0.52s` 处无墨**（确认辐条已移除） | ✅ |
| 5 个图标两两渲染结果不同 | ✅ |

> **为什么用极坐标而不是方形窗口**：方形窗口的对角线半径大于边长，会把相邻齿的上升沿误判成"该半径上有墨"。首轮实现即因此误报 2 项，已修正 —— 记录于此以保证可复现。

---

## 5. 功能保全回归（任务书核心要求）

### 5.1 原有 15 个交互项逐一核对（FACT）

`test_toolbar_contract.py` §B 用 `QObject.receivers("2clicked()")` / `("2triggered()")` **实测连接数**：

| ID | 项目 | 连接数 | 结果 |
|---|---|---|---|
| T1 | 使用引导 | 1 | ✅ |
| T2 | 激活许可证 | 1 | ✅ |
| T3 | 联系作者 | 1 | ✅ |
| T4 | 设置 | 1 | ✅ |
| T5 | 全屏工作台 | 1 | ✅ |
| A1 | 历史 | 1 | ✅ |
| A2 | ⋯ 溢出菜单（挂 QMenu，≥2 项） | — | ✅ |
| A2a | 编辑备注… | 2 | ✅ |
| A2b | 清除历史记录… | 2 | ✅ |
| A3 | 连接 / 断开 | 1 | ✅ |
| A4 | 地址输入框（Enter 触发连接） | 1 | ✅ |
| P1 | 收起面板 | 1 | ✅ |
| P2 | 展开面板 | 1 | ✅ |
| W1/W2/W3 | 最小化 / 最大化 / 关闭 | 各 1 | ✅ |

**15 / 15 全部保留，连接目标未变（FACT）。**

### 5.2 行为回归（FACT）

| 用例 | 结果 |
|---|---|
| 点全屏按钮 → 进入全屏 | ✅ |
| 全屏后图标切换 `fullscreen` → `fullscreenExit` | ✅ |
| 再点 → 退出全屏，图标切回 | ✅ |
| 点设置按钮 → 打开设置中心（打桩验证） | ✅ |
| 帮助入口 → 打开使用引导（`first_run=False`） | ✅ |
| 最大化按钮仍是普通最大化，不变成全屏 | ✅ |

### 5.3 快捷键（FACT）

**项目仅有 1 个隐式快捷键**：地址输入框 Enter。改造后仍为 1 个，未新增、未丢失。

---

## 6. 新增无障碍能力（FACT）

| 项 | 改造前 | 改造后 |
|---|---|---|
| 可访问名称 | 无 | 5 个按钮全部有（"设置"/"激活许可证"/…） |
| 键盘可达 | `Qt.NoFocus`（**完全够不到**） | `Qt.TabFocus`（Tab 可达） |
| 焦点可见 | 不适用 | 2px `#80BFFF` 焦点环（QSS `#titleIconBtn:focus`） |

> **依据**：Skill `--domain ux` → *Focus States (Severity: High)*："Use a visible focus ring on every interactive control"；*Focus Appearance* 的 2px 周长要求。
> **INFERENCE**：原实现是 `NoFocus`，键盘用户无法通过键盘到达帮助/设置/授权入口 —— 这是一个真实的无障碍缺口，本次修复。

---

## 7. 测试与性能

### 7.1 回归对比（FACT）—— 零回归

同一组 12 个测试，改造前后**逐项结果完全一致**：

| 测试 | 基线（Phase 3） | Phase 4 | 一致？ |
|---|---|---|---|
| `test_phase_ui3_activation_settings.py` | 通过 62 / 0 | 通过 62 / 0 | ✅ |
| `test_access_status.py` | 18/18 | 18/18 | ✅ |
| `test_offline_activation.py` | 32 / 0 | 32 / 0 | ✅ |
| `test_first_run_onboarding.py` | **225 / 0** | **225 / 0** | ✅ |
| `test_phase_ui3_1_layout.py` | 失败（`_stream_history`） | 失败（**同一点**） | ✅ |
| `test_ui_internal_diagnostics_gate.py` | 失败（`_stream_history`） | 失败（同一点） | ✅ |
| `test_ui_tune_02_address_alignment.py` | 失败（`_recent_combo`） | 失败（同一点） | ✅ |
| `test_ui_tune_01_roi_entry.py` | 24 通过 / 1 失败 | 24 通过 / 1 失败 | ✅ |
| `test_expiration_access_control.py` | 失败（`resolver`） | 失败（同一点） | ✅ |
| `test_roi_verification.py` | 失败（`int` not callable） | 失败（同一点） | ✅ |
| `test_license_priority.py` | 失败（缺测试密钥） | 失败（同一点） | ✅ |
| `test_roi_persistence_gui.py` | 失败（`get_with_seq`） | 失败（同一点） | ✅ |

**新增**：`tests/ui_redesign/test_toolbar_contract.py` → **43 项全部通过**。

**额外证据（FACT）**：`test_phase_ui3_1_layout.py` 在改造后**通过了 A 段（标题栏）全部断言**，才在 B 段（地址栏）撞上既有的 `_stream_history` 问题 —— 说明标题栏契约未被破坏。

### 7.2 性能对比（FACT + 重要限定）

同一命令、同一参数（`ui_baseline_probe.py --settle 5 --cpu-window 3`）：

| 指标 | 基线 | Phase 4 | 变化 |
|---|---|---|---|
| import | 251.0 ms | 122.5 ms | −51.2% |
| 窗口构造 | 1007.7 ms | 467.3 ms | −53.6% |
| 首帧绘制 | 753.5 ms | 714.5 ms | −5.2% |
| 空闲 CPU 均值 | 1.43 % | 0.95 % | −33.6% |
| 空闲 CPU 峰值 | 14.3 % | 14.3 % | 0.0% |
| RSS（空闲） | 248.1 MB | 247.5 MB | −0.2% |
| QTimer 总数 / 活动 | 8 / 1 | 8 / 1 | 0 |

**🔴 重要限定（必须声明，不得当作改进成果）**：
import 与窗口构造的 ~50% 提升**极可能是操作系统文件缓存预热**造成的假象 —— 删除 52 行死代码不可能让导入耗时减半。基线是**本机首次运行**，Phase 4 是**第二次**。

**因此本阶段不宣称任何性能改进。** 严谨的对照测量（每项 N=3 取中位数 + 显式预热）留到 **Phase 7**，届时会在基线 commit `dbd4ea0` 上另建 worktree 做真正的 A/B。

**可确认的只有**：**无性能回退**（RSS 与 QTimer 数量持平，首帧绘制差异在噪声范围内）。

### 7.3 真实截图（FACT）

| 文件 | 说明 |
|---|---|
| `evidence/screenshots/baseline_main_window.png` | 改造前 |
| `evidence/screenshots/phase4_toolbar_main_window.png` | 改造后 |
| `evidence/screenshots/icon_contact_sheet.png` | 图标几何对照 |

改造后截图可见：`🔑 ⚙ ⛶ │ ? 💬` 分组 + 分隔线，齿轮清晰可辨。

---

## 8. 布局与 DPI 验证

| 项 | 状态 |
|---|---|
| 1400×860（默认） | ✅ 实测截图，无溢出 |
| 窗口最小宽度 1100 | ⚠️ **UNKNOWN** —— 未单独截图验证；按钮数未变（仅重排），理论风险低 |
| DPI 100% | ✅ 实测（本机） |
| DPI 125/150/200% | ⚠️ **UNKNOWN** —— 需改系统缩放，本阶段未做 |
| 键盘 Tab 遍历 | ✅ 已实现 `TabFocus` + 焦点环（QSS） |
| 溢出裁剪 | ✅ 按钮数量与总宽未增加（28→30px × 5 = +10px，标题栏有 stretch，不溢出） |

**DPI 多档与最小宽度实测 → 顺延 Phase 6**（届时与全局主题一起做，避免重复改系统设置）。

---

## 9. Phase 4 门禁判定

| 门禁条件 | 结果 |
|---|---|
| 所有已清点原有功能入口保留 | ✅ 15/15，`receivers` 实测 |
| 动作映射与快捷键回归 | ✅ 见 §5，Enter 唯一快捷键未变 |
| 实际运行截图 | ✅ 3 张，含改造前后对照 |
| 对应自动化测试 | ✅ 新增 43 项契约测试全部通过 |
| 未删除功能以减少按钮数 | ✅ 功能入口零删除 |
| 未改动受保护区域 | ✅ 直播/解码/帧队列/OCR/ROI/授权零改动 |
| 零回归 | ✅ 12 项测试结果与基线逐项一致 |

### ✅ **Phase 4 门禁通过，允许进入 Phase 5。**

---

## 10. 遗留项

| # | 项 | 类型 | 处置 |
|---|---|---|---|
| R1 | DPI 125/150/200% 与最小宽度布局未实测 | UNKNOWN | Phase 6 一并验证 |
| R2 | 性能改进幅度受缓存预热干扰 | FACT | Phase 7 在基线 commit 另建 worktree 做严谨 A/B |
| R3 | `TOOLBAR_SPEC.md` §4 溢出菜单方案被否 | FACT | 本报告 §3 即为正式修订，原因有测试证据 |
| R4 | 12 项测试中 8 项既有失败 | FACT | 与本次无关（Phase 3 已归因）；建议另立任务修复 |
