# DESIGN_SYSTEM_REPORT — Phase 1 设计系统与交互方案

> **阶段**：Phase 1
> **日期**：2026-10-14
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`）
> **前置**：Phase 0 门禁通过（见 `SKILL_USAGE_AUDIT.md`）
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 1. 阶段目标与达成情况

| 目标 | 状态 |
|---|---|
| 真实调用 UI/UX Pro Max Skill 并留存证据 | ✅ 完成（10 条命令，全部 `exit=0`） |
| 生成 LiveLens 设计系统并落盘 | ✅ `design-system/livelens/MASTER.md` |
| 生成页面级规范 | ✅ `pages/main-window.md`、`pages/toolbar.md`、`pages/preview-watermark.md` |
| 组件规格 | ✅ `COMPONENT_SPEC.md` |
| 工具栏规格 | ✅ `TOOLBAR_SPEC.md` |
| 水印规格 | ✅ `WATERMARK_SPEC.md` |
| PySide6 适配方案明确 | ⚠️ 部分明确，**有 6 项 UNKNOWN 待 Phase 2** |
| 设计决策可追溯 | ✅ 每条决策标注来源 |

---

## 2. 实际执行的 Skill 命令（FACT）

全部使用绝对路径 `C:\Users\ZhuanZ（无密码）\.workbuddy-ai\skills\ui-ux-pro-max\scripts\search.py`，原始输出存于 `docs/ui-redesign/evidence/`。

| # | 命令 | 输出文件 | 退出码 |
|---|---|---|---|
| 1 | `--help` | （终端） | 0 |
| 2 | `"desktop streaming monitor liquid glass dark premium" --design-system -p LiveLens -f markdown` | `01_design_system.txt` | 0 |
| 3 | `"glassmorphism dark translucent desktop panels" --domain style` | `02_style_glass.txt` | 0 |
| 4 | `"keyboard focus button tooltip contrast" --domain ux` | `03_ux_focus.txt` | 0 |
| 5 | `"motion animation performance reduced motion" --domain ux` | `04_ux_motion.txt` | 0 |
| 6 | `"settings gear navigation toolbar icon consistency" --domain icons` | `05_icons_settings.txt` | 0 |
| 7 | `"desktop window controls focus performance" --stack winui` | `06_stack_winui.txt` | 0 |
| 8 | `"desktop window controls focus performance" --stack wpf` | `07_stack_wpf.txt` | 0 |
| 9 | `--design-system --persist -p LiveLens --output-dir <worktree>` | `08_skill_generated_MASTER_raw.md` | 0 |
| 10 | 深色变体探测（2 条） | `09_…`、`10_…` | 0 |

**FACT — 无任何命令失败。无编造结果。**

---

## 3. 关键决策记录

### D1 — 采纳 `Liquid Glass` 作为风格身份

- **来源**：Skill `--design-system` 实测返回 `Style Name: Liquid Glass`，关键词含 *dynamic material / optical glass / translucency / lensing / refraction / fluid morphing*。
- **判定**：与任务目标 A 精确吻合，**采纳**。

### D2 — 拒绝 Skill 生成的配色，改用深色黑曜石令牌

- **Skill 原值**：`#FAFAF9` 底 / `#0C0A09` 字 / `#A16207` 金强调（"Premium black + gold"，面向奢侈品/时尚电商）。
- **拒绝理由**：**浅色系，与任务要求的黑曜石深色底直接冲突。**
- **替代**：任务书初始令牌 + Skill 的深色 `Glassmorphism` 变体（`#0F172A` / `#1B2336` / `#F8FAFC` / `#94A3B8`）作为交叉验证 —— 两者高度一致，互为佐证。
- **验证**：WCAG 对比度实测，**10/10 组合通过 AA，9/10 通过 AAA**。

### D3 — 拒绝 Skill 的字体，改用系统字体栈

- **Skill 原值**：Cormorant + Montserrat（Google Fonts）。
- **拒绝理由**：装饰性衬线字体不适合中文 UI；PySide6 无 CSS `@import`；必须渲染中文。
- **替代**：`Microsoft YaHei UI` → `Microsoft YaHei` → `Segoe UI`（中文）+ `Cascadia Mono` → `Consolas`（数字）。零依赖、无网络请求。

### D4 — 拒绝 Skill 的 Pattern / Web 检查表

- **Skill 原值**：`Scroll-Triggered Storytelling`（Web 落地页滚动叙事）+ `cursor-pointer` / 375px 断点 / `prefers-reduced-motion` 媒体查询。
- **拒绝理由**：桌面视频工具无"滚动章节/CTA/响应式断点"概念。
- **替代**：其**无障碍意图**转为 Qt 等价项（MASTER §7 的 A1–A7）。

### D5 — 明确"Skill 无法一次给出 Liquid Glass + 深色配色"

- **FACT**：4 组查询实测证明 Style 身份与配色在 Skill 中**绑定**。
- **处置**：拆分为"风格取 A 查询、配色取 B 查询 + 人工定稿"，全链路留痕。
- **意义**：这是本次审计最有价值的发现，避免了"假装 Skill 给了完整方案"。

### D6 — 玻璃材质实现路径

- **采纳**：QSS（填充/圆角/描边）+ `QPainter`/`QPainterPath`（高光/边缘/渐变）。
- **明确红线**：
  - ❌ 不得把 `QGraphicsBlurEffect` 当作背景模糊方案；
  - ❌ 不得声称 QSS 能实现真实背景折射；
  - ❌ 不得在视频帧路径上做模糊/折射/拷贝。
- **Windows DWM/Acrylic**：**UNKNOWN**，Phase 6 需独立最小原型实测后再决定。**默认不依赖原生材质。**

### D7 — 图标方案

- **Skill 建议**：Phosphor（主）+ Heroicons（兜底），`gear` 为设置图标。
- **限制**：两者均为 React 图标库，**PySide6 无法直接使用**。
- **采纳**：`gear` 的**语义与视觉风格**（Outline、1.5px、单一家族）。
- **UNKNOWN**：项目现有图标来源 → Phase 2 审计。**不得为此安装新依赖。**

---

## 4. 产出文件清单

```
design-system/livelens/
├── MASTER.md                       # 唯一视觉真相来源（已人工改写）
└── pages/
    ├── main-window.md              # 主窗口与背景层
    ├── toolbar.md                  # 工具栏视觉规范
    └── preview-watermark.md        # 预览与水印覆盖层

docs/ui-redesign/
├── SKILL_USAGE_AUDIT.md            # Phase 0 产出
├── DESIGN_SYSTEM_REPORT.md         # 本文件
├── COMPONENT_SPEC.md               # 组件规格
├── TOOLBAR_SPEC.md                 # 工具栏规格
├── WATERMARK_SPEC.md               # 水印规格（含运动算法与测试矩阵）
└── evidence/
    ├── 01_design_system.txt ~ 10_dark_oled_variant.txt
    ├── 08_skill_generated_MASTER_raw.md   # Skill 原始生成结果（未改写）
    └── contrast_check.py                  # WCAG 对比度实测脚本
```

---

## 5. 阶段门禁判定

| 门禁条件 | 结果 | 说明 |
|---|---|---|
| 设计系统已落盘 | ✅ 通过 | `design-system/livelens/MASTER.md` + 3 个 page 文件 |
| 关键设计决策可追溯 | ✅ 通过 | 每条决策标注 Skill 来源或拒绝理由（§3 D1–D7） |
| 设计规范 / 组件边界 / 实现方案 / 风险相互核对 | ✅ 通过 | 见 §6 交叉核对表 |
| PySide6 适配方案明确 | ⚠️ **部分** | 材质与令牌方案明确；但**依赖 6 项 Phase 2 审计结果**才能定稿 |

### 6. 交叉核对表（任务书要求的"相互核对"）

| 规范项 | 组件边界 | 实现方案 | 风险 | 一致？ |
|---|---|---|---|---|
| 玻璃面板 L2/L3 | 独立 QWidget 或现有容器 | QSS + QPainter 高光 | 真实背景模糊不可用 | ✅ |
| 工具栏三区 | `pages/toolbar.md` | QSS 限定作用域 | 样式污染 `gui/theme.py` | ⚠️ 待 Phase 2 确认 theme 结构 |
| 水印覆盖层 L4 | 独立透明 QWidget | 独立 QTimer + O(1) 算术 | 与视频控件父子关系未知 | ⚠️ 待 Phase 2 确认 `video_widget.py` |
| 水印授权状态 | 纯消费者 | 只读状态/信号 | 权威来源未知 | ⚠️ 待 Phase 2 确认许可模块 |
| 图标系统 | 单一 Outline 家族 | 复用现有资源 | 现有资源未知 | ⚠️ 待 Phase 2 确认 |

**INFERENCE**：4 项规范与项目现实的核对**无法在 Phase 2 之前完成**，因为涉及真实代码结构。这不构成设计缺陷，但**构成明确的后续依赖**。

---

## 7. ⚠️ 阶段门禁判定结论

### 🟡 **Phase 1 有条件通过 —— 允许进入 Phase 2，但 Phase 5/6 编码前必须回填 UNKNOWN**

**理由**：

- 任务书要求 Phase 1 门禁"设计规范、组件边界、实现方案和风险必须相互核对。发现冲突先修订设计文档，再进入 Phase 2。"
- 已核对的 5 项中有 4 项因**信息不足**（非冲突）而挂起，且这些信息**只能通过 Phase 2 的代码审计获得**。这是阶段划分的固有依赖，不是设计冲突。
- 任务书 Phase 1 自身也要求"组件边界"明确 —— 而水印/工具栏的组件边界**取决于真实代码结构**，Phase 2 的 `PREVIEW_OVERLAY_AUDIT.md` / `TOOLBAR_AUDIT.md` 正是为此设计。

**因此**：Phase 2 完成后，**必须回填并复核本报告的 §6**，确认无冲突后方可进入 Phase 3+。

**未发现设计层面的自相冲突**（颜色、字体、材质、间距、动效在各文件间一致）。

---

## 8. UNKNOWN 汇总（跨阶段追踪）

| # | 未知项 | 目标阶段 | 影响 |
|---|---|---|---|
| U1 | 许可状态权威来源与可观察性 | Phase 2 | Phase 5 编码前置 |
| U2 | `video_widget.py` 控件类型与父子结构 | Phase 2 | Phase 5 覆盖层挂载方式 |
| U3 | 预览区边界/坐标系统/缩放模式 | Phase 2 | Phase 5 边界计算 |
| U4 | 顶部按钮完整清单与信号槽 | Phase 2 | Phase 4 回归基线 |
| U5 | 现有图标来源与家族 | Phase 2 | Phase 4 图标统一方案 |
| U6 | `gui/theme.py` 结构与作用域 | Phase 2 | Phase 4/6 样式隔离策略 |
| U7 | 水印定时器最优周期 | Phase 5 实测 | 性能预算 |
| U8 | 水印不透明度最终值 | Phase 5 截图复核 | 视觉质量 |
| U9 | Windows DWM/Acrylic 可行性 | Phase 6 原型 | 是否采用原生材质 |
| U10 | 按钮真实使用频率 | Phase 2（INFERENCE） | 收纳决策依据 |

---

## 9. 本阶段未做的改动（FACT）

Phase 1 **未修改任何项目源码**。新增文件仅为设计文档与证据快照：

- `design-system/`（新建目录）
- `docs/ui-redesign/`（新建目录）

项目源码 `gui/`、`stream/`、`ocr/` 等**零改动**。
