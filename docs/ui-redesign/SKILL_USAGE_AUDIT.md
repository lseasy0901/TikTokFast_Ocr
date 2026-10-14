# SKILL_USAGE_AUDIT — UI/UX Pro Max 能力与边界审计

> **阶段**：Phase 0
> **日期**：2026-10-14
> **执行者**：WorkBuddy Agent（本次 UI 改造任务）
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`）
> **证据标签**：本文严格区分 **FACT**（已实测）/ **INFERENCE**（推断）/ **UNKNOWN**（未验证）

---

## 0. 阶段目标

在改动 LiveLens 项目之前，先确认已安装的 UI/UX Pro Max Skill：

1. 真实存在且可执行；
2. 命令行参数与文档一致；
3. 支持哪些设计检索域与技术栈；
4. 设计系统持久化方式；
5. **对本项目（PySide6 桌面应用）而言，哪些能力真正适用、哪些不适用。**

---

## 1. Skill 定位（FACT）

| 项 | 值 |
|---|---|
| 安装路径 | `C:\Users\ZhuanZ（无密码）\.workbuddy-ai\skills\ui-ux-pro-max\` |
| 安装级别 | 用户级（跨项目） |
| 安装时间 | 2026-10-14 15:27 |
| 来源 | `ui-ux-pro-max-skill-main.zip`（GitHub `nextlevelbuilder/ui-ux-pro-max-skill`） |
| 声明版本 | `2.15.0`（来自上游 `skill.json` / `.claude-plugin/plugin.json`） |
| 磁盘占用 | 3.6 MB / 73 个文件 |
| 安全审计 | P2（92/100，可安全使用）；详见安装期审计记录 |

**FACT — 已安装的是主 Skill `ui-ux-pro-max`。** 上游压缩包内的另外 6 个子 Skill（`design`、`design-system`、`ui-styling`、`brand`、`banner-design`、`slides`）**未安装**，因此本任务不得引用它们的能力。

---

## 2. 必需文件读取结果（FACT）

任务书要求读取的 4 个文件全部存在：

| 文件 | 大小 | SHA-256（前 16 位） | 状态 |
|---|---|---|---|
| `SKILL.md` | 16,124 B | `9031eb5ada66c727` | ✅ 存在 |
| `scripts/search.py` | 9,373 B | `d54e648fe0ec2932` | ✅ 存在 |
| `references/quick-reference.md` | 24,526 B | `0609bc7c89dacb40` | ✅ 存在 |
| `references/pro-rules.md` | 10,909 B | `d28442d61c310b49` | ✅ 存在 |

**无虚构内容**：4 个文件均已实际读取/哈希。

### 2.1 SKILL.md 关键内容（FACT）

- 10 级优先级规则表：无障碍(1) > 触控交互(2) > 性能(3) > 风格(4) > 布局响应(5) > 排版色彩(6) > 动画(7) > 表单反馈(8) > 导航(9) > 图表(10)。
- 明确声明：*"This skill handles UI/UX design intelligence and implementation guidance. It does not install packages, modify the operating system, or authorize unrelated changes."*
- 明确声明：**不安装包、不修改操作系统、不授权无关改动**；脚本仅需 Python 3 标准库，无第三方依赖、无网络访问。
- 检索工作流：Step 1 需求分析 → Step 2 设计系统（`--design-system`）→ Step 2b 持久化 → Step 3 领域检索（`--domain`）→ Step 4 技术栈检索（`--stack`）。
- 0 结果时的规定行为：**不得编造输出**，重试一次后明确告知用户"来自内置默认值而非数据库匹配"。

### 2.2 脚本自述约束（FACT）

`SKILL.md` §Running the search tool 明确要求：脚本位于 Skill 自身目录，必须以完整路径调用；`python` 缺失时依次尝试 `python3`、`py -3`；**Python 缺失时不得自行安装**，必须停下来请用户安装。

---

## 3. 命令行参数实测（FACT）

执行：

```
python "C:\Users\ZhuanZ（无密码）\.workbuddy-ai\skills\ui-ux-pro-max\scripts\search.py" --help
```

### 3.1 Python 可用性（FACT）

| 调用方式 | 版本 | 结果 |
|---|---|---|
| `python` | 3.13.14 | ✅ 可用（本任务采用） |
| `py -3` | 3.14.7 | ✅ 可用（备用） |

> 注：项目自身的 venv 为 Python 3.11.9（详见 `BASELINE_REPORT.md`）。Skill 脚本与项目运行时相互独立，纯标准库，版本差异无影响。

### 3.2 实测支持的参数（FACT，摘自 `--help` 真实输出）

| 参数 | 说明 |
|---|---|
| `query`（位置参数） | 检索词 |
| `--domain, -d` | `style` `color` `chart` `landing` `product` `ux` `typography` `icons` `gsap` `react` `web` `google-fonts` |
| `--stack, -s` | 22 个：`react` `nextjs` `vue` `svelte` `astro` `swiftui` `react-native` `flutter` `nuxtjs` `nuxt-ui` `html-tailwind` `shadcn` `jetpack-compose` `threejs` `angular` `laravel` `javafx` `wpf` `winui` `avalonia` `uno` `uwp` |
| `--max-results, -n` | 1–20，默认 3 |
| `--json` | JSON 输出 |
| `--full` | 不截断长字段 |
| `--design-system, -ds` | 生成完整设计系统建议 |
| `--project-name, -p` | 设计系统项目名 |
| `--format, -f` | `ascii`（默认）/ `markdown` |
| `--persist` | 持久化到 `design-system/<project-slug>/MASTER.md` |
| `--page` | 生成页面级覆盖文件 `pages/<page>.md` |
| `--output-dir, -o` | 持久化输出目录（默认当前目录） |
| `--force` | 覆盖已存在的 MASTER.md（默认跳过） |
| `--variance` / `--motion` / `--density` | 1–10 调节旋钮，仅配合 `--design-system` |

**FACT — 任务书 Phase 1 中假设的参数全部真实存在**，包括 `--persist`、`--output-dir`、`-f markdown`、`--domain`、`--stack`。

---

## 4. 能力边界（本任务最关键的一节）

### 4.1 ❌ 不存在 PySide6 / Qt 技术栈（FACT）

`--stack` 的 22 个选项中**没有 PySide6、PyQt、Qt Widgets、QML**。

最接近的桌面栈为：`winui`、`wpf`、`avalonia`、`uwp`、`javafx`。

**约束确认**：任务书明确要求 —— 不得将 `winui`/`wpf` 的检索结果冒充 PySide6 专属规范。本次审计**遵守该约束**，`winui`/`wpf` 结果仅作为"Windows 桌面通用设计原则"参考，其代码示例（`ContentDialog`、`AppWindow`）**完全不可用于 PySide6**。

### 4.2 ✅ 真正可用的能力

| 能力 | 可用性 | 本任务用途 |
|---|---|---|
| `--design-system` 设计系统生成 | ✅ 实测可用 | 生成 LiveLens 设计基线 |
| `--domain style` | ✅ 实测可用 | Liquid Glass / Glassmorphism 风格规范 |
| `--domain ux` | ✅ 实测可用 | 焦点、对比度、动画性能约束 |
| `--domain icons` | ✅ 实测可用 | 图标语义与一致性规则 |
| `--domain typography` / `color` | ✅ 可用（本次未取） | 备用 |
| `--stack winui/wpf` | ⚠️ 可用但**代码不可迁移** | 仅取桌面设计原则 |
| `references/quick-reference.md` | ✅ 存在（24.5 KB） | 119 条 UX 规范全文，按需查阅 |
| `references/pro-rules.md` | ✅ 存在（10.9 KB） | 原生/移动 App 交付前检查表 |

### 4.3 ❌ 明确不适用的能力（FACT）

- 所有 `--stack` 结果中的代码片段（React/Vue/WinUI/WPF/Flutter/SwiftUI 等）——项目是 Python + PySide6。
- `--design-system` 输出的 **CSS 变量表**、**Google Fonts 导入**、**Tailwind 集成建议**。
- 预交付检查表中的 Web 专属条目（`cursor-pointer`、`375px/768px` 断点、`prefers-reduced-motion` 媒体查询）。

### 4.4 设计系统持久化（FACT）

`--persist` 将写入 `design-system/<project-slug>/MASTER.md`，配合 `--page` 生成 `pages/<page>.md` 覆盖文件。

**FACT**：项目内 `design-system/` 目录**当前不存在**，`docs/` 目录也**不存在**。因此不存在覆盖既有设计系统的风险。

**UNKNOWN**：`-f markdown` 与 `--persist` 能否在同一次调用中同时生效，`--help` 未说明（`--format` 的帮助文本只提到"`--json` 时忽略"）。Phase 1 将实测，并按实测结果决定分两次调用还是合并调用。

---

## 5. 实际执行记录（FACT）

Phase 1 共执行 7 条真实命令，原始输出已完整保存于：

```
docs/ui-redesign/evidence/
├── 01_design_system.txt      (68 行)
├── 02_style_glass.txt        (30 行)
├── 03_ux_focus.txt           (40 行)
├── 04_ux_motion.txt          (40 行)
├── 05_icons_settings.txt     (43 行)
├── 06_stack_winui.txt        (49 行)
└── 07_stack_wpf.txt          (49 行)
```

全部 `exit=0`，无异常、无报错。

### 5.1 ⚠️ 关键发现：`--design-system` 输出只有约 1/3 可用

执行：

```
python search.py "desktop streaming monitor liquid glass dark premium" --design-system -p "LiveLens" -f markdown
```

**实测输出与适用性判定：**

| 输出部分 | 实测内容 | 是否适用于 LiveLens | 判定依据 |
|---|---|---|---|
| **Style** | **`Liquid Glass`** — keywords: dynamic material, optical glass, translucency, lensing, refraction, fluid morphing, system navigation | ✅ **完全适用，正是目标风格** | 与任务目标 A 精确吻合 |
| Style 性能提示 | `cost:moderate \| drivers:animation,blur` | ✅ 适用 | 直接约束了 Phase 6 的性能预算 |
| Style 无障碍要求 | `requires: contrast-text-4.5, keyboard, visible-focus, reduced-motion` | ✅ 适用 | 可直接转为验收项 |
| Style 反模式 | Cheap visuals / Fast animations | ✅ 适用 | 转为设计红线 |
| **Pattern** | `Scroll-Triggered Storytelling`（Intro hook > Chapter 1 > … > Climax CTA） | ❌ **不适用** | 这是 **Web 落地页滚动叙事**模式，桌面视频监控工具无"章节/滚动/CTA"概念 |
| **Colors** | 浅色系 `Premium black + gold`：Background `#FAFAF9`、Foreground `#0C0A09`、Accent `#A16207` | ❌ **不适用，且与需求冲突** | 输出为**浅色底 + 金色强调**；任务要求**黑曜石深色底 + 蓝调强调**（`#090B10` / `#80BFFF`）。该配色面向"奢侈品/时尚电商" |
| **Typography** | Cormorant + Montserrat（Google Fonts） | ❌ **不适用** | ① 装饰性衬线字体不适合中文 UI；② PySide6 不使用 CSS `@import`；③ 项目需渲染中文，必须走系统字体栈 |
| **Key Effects** | Lensing and refraction, adaptive translucency, fluid morph transitions | ⚠️ **概念适用，实现不可迁移** | 概念是目标；但 Qt 无 CSS `backdrop-filter`，须用 QPainter 自绘 |
| **Pre-Delivery Checklist** | cursor-pointer / 375px 断点 / prefers-reduced-motion 媒体查询 | ❌ **大部分不适用** | Web 专属；桌面 Qt 无 `cursor-pointer` 与响应式断点概念 |

**结论（INFERENCE）**：`--design-system` 的 **Style 识别是准确且有价值的**（准确命中 `Liquid Glass`），但其 **Pattern / Colors / Typography 三个模块是 Web 落地页导向的通用默认值**，对桌面视频工具既无参考价值、甚至与需求冲突。

**因此本任务的处理方式**：采纳 Skill 的 **Style 身份与约束条件**，拒绝其 Pattern/Colors/Typography，颜色与字体改为依据任务书指定令牌 + Skill 的对比度要求自行定稿。此决策已在 Phase 1 设计文档中留痕，**不伪造 Skill 能力**。

### 5.2 风格检索（FACT）

`--domain style` 检索 `glassmorphism dark translucent desktop panels` 命中 **1 条**（`glassmorphism`）：

- 关键词：Frosted glass, transparent, blurred background, layered, vibrant background, light source, depth, multi-layer
- 主要色彩：`rgba(255,255,255,0.1–0.3)`
- 效果：Backdrop blur 10–20px、1px 边框 `rgba(255,255,255,0.2)`、light reflection、Z-depth
- 性能：`cost:low | drivers:none` ← **FACT：Skill 认为该风格性能成本低**
- 无障碍：`requires: contrast-text-4.5, keyboard, visible-focus, reduced-motion`
- 框架兼容：`tailwind|mui|chakra` ← ❌ 全部不适用 PySide6
- 实现检查表：模糊 10–20px / 白色 15–30% 透明 / 1px 浅色边框 / 背景活力验证 / 文字对比度 4.5:1

**关键限制（INFERENCE）**：Skill 返回的是 **Web Glassmorphism**，依赖 CSS `backdrop-filter`。任务书已明确警告"不得把普通网页 Glassmorphism 直接等同于 Apple Liquid Glass"。Qt 的 `QGraphicsBlurEffect` **不是**背景模糊方案，不可直接套用。此项将作为 Phase 6 的技术风险登记。

### 5.3 UX 检索（FACT）

`--domain ux` 两次检索，共 6 条规范：

**焦点/对比度类：**
1. Focus Appearance（WCAG 2.2 AAA，Web）— 焦点指示器 ≥2 CSS px、状态对比 3:1
2. **Focus States（Platform: All）— 每个可交互控件需可见焦点环，含模态框内控件；Severity: High** ← ✅ 适用
3. Focus Not Obscured（Web）— 焦点不被常驻 UI 遮挡

**动画类：**
1. **Reduced Motion（Platform: All）— Severity: High** ← ✅ 概念适用（Qt 需自行实现"减少动态"开关，无媒体查询）
2. **Excessive Motion（Platform: All）— 每屏最多动画 1–2 个关键元素；Severity: High** ← ✅ 适用
3. Duration Timing（All）— 使用共享 motion token，**不把 150–300ms 当普适要求** ← ✅ 适用

**INFERENCE**：这 6 条中，标注 `Platform: All` 的 4 条可直接转为 LiveLens 验收项；标注 `Platform: Web` 的 2 条（Focus Appearance / Focus Not Obscured）其 WCAG 原则仍适用，但 CSS 示例需改写为 Qt 实现。

### 5.4 图标检索（FACT）

`--domain icons` 检索 `settings gear navigation toolbar icon consistency` 命中 3 条：

1. **`gear`（Phosphor，Outline）** — keywords: gear cog preferences config；Best For: Settings page configuration；Semantic Role: meaningful ← ✅ **正是任务要求的设置齿轮图标**
2. `icon-context-accessibility` — 图标语义与无障碍规则：装饰性图标 aria-hidden；纯图标按钮必须有可访问名称；**"Prefer the most semantically precise icon... Keep one visual family per surface"**（每层保持单一图标家族）← ✅ 适用
3. `list`（Phosphor）— 汉堡菜单/导航抽屉

**FACT**：Skill 推荐 **Phosphor（主）+ Heroicons（兜底）** 图标体系，但二者的 `import` 代码均为 React 语法，**不可用于 PySide6**。

**UNKNOWN（转 Phase 2 解决）**：项目当前实际使用什么图标来源（QIcon / SVG 文件 / Qt 标准图标 / 自绘 QPainter），尚未审计。这决定了"统一图标体系"能否落地为 Phosphor 风格。

### 5.5 Windows 桌面栈检索（FACT）

`--stack winui` 与 `--stack wpf` 各命中 3 条，内容高度相似：

- Use ContentDialog for modal interactions（WinUI API）
- Use WinUI 3 Window management / AppWindow API
- （第 3 条同类）

**判定：❌ 代码层面完全不可用。** 全部是 WinUI 3 / WPF 专有 API，PySide6 无对应物。

**可提取的通用原则（INFERENCE，仅概念）**：
- 模态交互应使用标准对话框模式，而非自制 overlay + 手工焦点陷阱；
- 窗口生命周期应集中管理，避免"一切挂在 MainWindow"。

> 后者恰好命中本项目的真实风险：`gui/main_window.py` 已达 **4,082 行 / 203 KB**（见 §6）。

---

## 6. 与项目实际约束的交叉核对（FACT）

| 项目事实 | 值 | 对 Skill 使用的影响 |
|---|---|---|
| 技术栈 | Python 3.11.9 + PySide6 6.11.2 / Qt 6.11.2 | Skill 无对应 stack，全部代码示例不可用 |
| UI 主文件 | `gui/main_window.py` 4,082 行 / 203 KB | 单文件巨石，改造风险高 |
| 既有主题 | `gui/theme.py` 31 KB | 已有主题系统，需先审计再决定扩展还是替换 |
| 预览控件 | `gui/video_widget.py` 7.7 KB | 水印覆盖层的挂载点候选 |
| `design-system/` 目录 | 不存在 | 持久化无覆盖风险 |
| `docs/` 目录 | 不存在 | 本次将新建 |

---

## 7. 阶段门禁判定

| 门禁条件 | 结果 |
|---|---|
| Skill 可执行 | ✅ 通过（`--help` 与 7 条真实查询全部 `exit=0`） |
| 参数真实有效 | ✅ 通过（任务书假设的 `--persist`/`--output-dir`/`-f markdown`/`--domain`/`--stack` 全部存在） |
| 输出路径安全 | ✅ 通过（`design-system/`、`docs/` 均不存在，无覆盖风险） |
| 边界已识别 | ✅ 通过（无 PySide6 stack；Pattern/Colors/Typography 不适用；Web 代码不可迁移） |

### ✅ **Phase 0 门禁通过，允许进入 Phase 1。**

**遗留 UNKNOWN（带入后续阶段）**：
1. `-f markdown` 与 `--persist` 能否同次调用 —— Phase 1 实测。
2. 项目实际图标来源 —— Phase 2 审计。
3. 项目现有 QSS 主题结构 —— Phase 2 审计。
4. `--domain icons` 返回的 Phosphor 图标能否在 PySide6 中落地（需要图标资源文件）—— Phase 2 审计后决定，**不得为此安装新依赖**。

---

## 附录 A：本阶段未做任何项目代码改动

Phase 0 全程只读。本阶段新增文件仅为审计文档与证据快照，位于 `docs/ui-redesign/`。

## 附录 B：Skill 使用红线（本任务全程遵守）

1. 不安装任何新 Skill、新 Python 包、不调用收费 API。
2. 不把 `winui`/`wpf`/React 代码当作 PySide6 方案。
3. 不伪造 Skill 调用结果；0 结果时如实说明。
4. 每条主要视觉决策必须可追溯到：Skill 输出 / 项目约束 / 有证据的人工适配。
