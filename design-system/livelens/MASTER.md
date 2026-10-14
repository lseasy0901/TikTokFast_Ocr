# LiveLens Design System — MASTER

> **LOGIC:** 构建具体页面时，先检查 `design-system/livelens/pages/[page-name].md`。
> 若存在，其规则**覆盖**本文件；不存在则严格遵循本文件。
> 页面级文件**不得**自行发明颜色、字体或玻璃材质。

> **项目**：LiveLens / DouyinLowLatencyViewer
> **风格**：Apple Liquid Glass（液态玻璃）
> **技术栈**：Python 3.11 + PySide6 6.11.2 / Qt 6.11.2
> **版本**：1.0（2026-10-14）
> **状态**：本文件是 LiveLens 全部 UI 的**唯一视觉真相来源（Source of Truth）**

> ⚠️ **注意**：本文件已由人工改写。UI/UX Pro Max 原始生成结果（浅色 + 金色 "Luxury/Premium Brand" 配色）
> 完整保存在 `docs/ui-redesign/evidence/08_skill_generated_MASTER_raw.md`。
> 改写原因见 §0。

---

## 0. 溯源与人工适配声明

本设计系统由 **UI/UX Pro Max Skill（v2.15.0）** 的真实输出 + 有证据的人工适配共同产生。
原始命令与完整输出保存在 `docs/ui-redesign/evidence/`。

### 0.1 ✅ 采纳自 Skill 的部分

| 来源 | 采纳内容 |
|---|---|
| `--design-system "desktop streaming monitor liquid glass dark premium"` | **Style 识别 = `Liquid Glass`**；关键词 dynamic material / optical glass / translucency / lensing / refraction / fluid morphing；性能约束 `cost:moderate \| drivers:animation,blur`；无障碍要求 `contrast-text-4.5, keyboard, visible-focus, reduced-motion`；反模式 Cheap visuals / Fast animations |
| `--domain style "glassmorphism dark translucent desktop panels"` | 玻璃材质参数：白色 15–30% 透明、1px 浅色边框、Z-depth 分层、light reflection；性能 `cost:low` |
| `--domain ux`（焦点 / 动画两组） | 可见焦点环（Severity High）、减少动态（High）、每屏动画 ≤1–2 个关键元素（High）、共享 motion token 且不把 150–300ms 当普适要求 |
| `--domain icons` | `gear` 图标语义 = settings；**"Keep one visual family per surface"**（每层单一图标家族）；纯图标控件必须有可访问名称 |
| `--stack winui` | 仅概念：模态应走标准对话框模式，避免自制 overlay + 手工焦点陷阱 |

### 0.2 ❌ 明确拒绝的 Skill 输出及理由

| 被拒内容 | Skill 原值 | 拒绝理由 |
|---|---|---|
| **Pattern** | `Scroll-Triggered Storytelling`（Intro hook > Chapter > Climax CTA） | 这是 **Web 落地页滚动叙事**模式。LiveLens 是桌面视频监控工具，无"章节/滚动/CTA"概念。**不适用** |
| **Colors** | `#FAFAF9` 底 / `#0C0A09` 字 / `#A16207` 金强调（"Premium black + gold"） | 面向奢侈品/时尚电商的**浅色系**。任务明确要求**黑曜石深色底 + 蓝调强调**。**与需求直接冲突** |
| **Typography** | Cormorant + Montserrat（Google Fonts `@import`） | ① 装饰性衬线字体不适合中文 UI；② PySide6 无 CSS `@import`；③ 必须渲染中文，需走系统字体栈。**不适用** |
| **CSS 变量表 / Tailwind 集成** | `--color-primary` 等 CSS 变量 | PySide6 用 QSS，非 CSS。**不适用** |
| **Pre-Delivery Checklist（Web 部分）** | `cursor-pointer`、375/768/1024/1440px 断点、`prefers-reduced-motion` 媒体查询 | 桌面 Qt 无对应概念。**不适用**（其无障碍意图已转为 Qt 等价项，见 §7） |
| **`--stack winui/wpf` 代码** | `ContentDialog`、`AppWindow` API | WinUI 3 / WPF 专有 API，PySide6 无对应物。**代码不可迁移** |

### 0.3 ⚠️ 关键技术限制（已实测确认）

**FACT**：Skill 的 **Style 身份与配色方案是绑定的**。实测：

| 查询 | 返回 Style | 返回配色 |
|---|---|---|
| `desktop streaming monitor liquid glass dark premium` | **Liquid Glass** ✅ | 浅色 + 金 ❌ |
| `dark obsidian glass premium desktop tool control panel` | **Liquid Glass** ✅ | 浅色 + 金 ❌ |
| `video surveillance dashboard dark glass realtime monitoring` | Glassmorphism ⚠️ | **深色石板** ✅ |
| `dark mode video streaming monitor desktop dashboard glass` | Dark Mode (OLED) ⚠️ | **纯黑** `#000000` ✅ |

**结论**：Skill **无法**在一次调用中同时给出"Liquid Glass 风格 + 深色黑曜石配色"。

**本设计系统的处理方式**：
1. **风格身份**取自查询 1/2（正确的 `Liquid Glass`）；
2. **深色基线**参考查询 3 的 `Glassmorphism` 深色石板配色（`#0F172A` 底 / `#1B2336` 卡片 / `#F8FAFC` 字 / `#94A3B8` 次级字），该配色与任务书初始令牌**高度一致**；
3. **最终令牌**以任务书初始值为起点，经 **WCAG 对比度实测**后定稿（见 §2.3）。

此决策链路完整留痕，**未伪造 Skill 能力**。

---

## 1. 设计原则

1. **玻璃是材质，不是装饰。** 半透明表面必须能看出背后有内容；不允许"画一个灰色圆角矩形假装是玻璃"。
2. **深色优先，画面优先。** 界面永远不与被监看的直播画面争夺注意力。
3. **克制的动效。** 动效只用于表达状态变化与空间关系；遵循 Skill 的"每屏 ≤1–2 个关键元素"与"不使用单一固定时长"。
4. **可读性高于通透感。** 任何玻璃表面上的文字都必须满足 WCAG AA 4.5:1。
5. **一套图标，一个家族。** 每层界面只用一个图标体系（Skill 原文：Keep one visual family per surface）。
6. **不在视频帧路径上做任何视觉处理。** 模糊、折射、额外拷贝一律禁止进入帧刷新链路。

---

## 2. 颜色令牌

### 2.1 基础调色板

| 角色 | Token | Hex | 用途 |
|---|---|---|---|
| 主背景 | `--ll-bg-base` | `#090B10` | 窗口最底层（黑曜石） |
| 内容面板 | `--ll-surface-panel` | `#171B24` | 面板/卡片实心底色 |
| 玻璃基色 | `--ll-glass-base` | `#252B36` | 玻璃表面基色（配合透明度） |
| 主文字 | `--ll-text-primary` | `#E9F0FF` | 标题、主要信息 |
| 次级文字 | `--ll-text-secondary` | `#A8B4C7` | 说明、标签、次要信息 |
| 强调色 | `--ll-accent` | `#80BFFF` | 主操作、选中、焦点 |
| 成功色 | `--ll-success` | `#64D9A0` | 连接正常、识别成功 |

### 2.2 玻璃材质派生令牌

| Token | 值 | 说明 |
|---|---|---|
| `--ll-glass-fill` | `rgba(37,43,54,0.62)` | 玻璃填充（基色 + 透明度） |
| `--ll-glass-fill-raised` | `rgba(37,43,54,0.78)` | 悬浮/置顶玻璃 |
| `--ll-glass-border` | `rgba(255,255,255,0.14)` | 1px 边缘描边 |
| `--ll-glass-highlight` | `rgba(255,255,255,0.22)` | 顶部高光（线性渐变起点） |
| `--ll-glass-highlight-end` | `rgba(255,255,255,0.00)` | 顶部高光渐变终点 |
| `--ll-glass-shadow` | `rgba(0,0,0,0.45)` | 悬浮投影 |

> **来源**：`--domain style` 实测建议"白色 15–30% 透明 + 1px 浅色边框"。`--ll-glass-border` 取 0.14，略低于建议下界，因为深色底上 0.2 的白边会显得刺眼（人工适配，需 Phase 6 实机复核）。

### 2.3 ✅ WCAG 对比度实测结果（FACT）

实测脚本：`docs/ui-redesign/evidence/contrast_check.py`（WCAG 2.1 相对亮度公式，sRGB 线性化）。

| 前景 | 背景 | 对比度 | AA 正文 4.5:1 | AAA 7.0:1 |
|---|---|---|---|---|
| 主文字 | 主背景 | **17.22** | ✅ PASS | ✅ PASS |
| 主文字 | 内容面板 | **15.08** | ✅ PASS | ✅ PASS |
| 主文字 | 玻璃基色 | **12.44** | ✅ PASS | ✅ PASS |
| 次级文字 | 主背景 | **9.39** | ✅ PASS | ✅ PASS |
| 次级文字 | 内容面板 | **8.22** | ✅ PASS | ✅ PASS |
| 次级文字 | 玻璃基色 | **6.78** | ✅ PASS | ❌ 未达 AAA |
| 强调色 | 主背景 | **10.14** | ✅ PASS | ✅ PASS |
| 强调色 | 玻璃基色 | **7.32** | ✅ PASS | ✅ PASS |
| 成功色 | 主背景 | **11.22** | ✅ PASS | ✅ PASS |
| 成功色 | 玻璃基色 | **8.10** | ✅ PASS | ✅ PASS |

**结论**：任务书给出的初始令牌**全部满足 WCAG AA**，10/10 组合通过 AA，9/10 通过 AAA。

**⚠️ 重要限定（INFERENCE → 必须 Phase 6 复验）**：上表计算的是**不透明色**对比度。真实玻璃表面是半透明的，**实际渲染像素**取决于其背后内容。因此：
- 玻璃表面上的文字**必须**额外做局部不透明衬底（或提高玻璃不透明度），保证合成后仍 ≥4.5:1；
- Phase 6 必须在**真实截图**上重新采样像素验证，不得仅凭本表宣称达标。

**唯一弱项**：次级文字在玻璃基色上为 6.78:1（AA 通过、AAA 未达）。处置：次级文字在玻璃表面上**不用于小于 14px 的字号**，或提升该处玻璃不透明度。

---

## 3. 玻璃材质规范

### 3.1 材质分层（Z 轴）

| 层 | 名称 | 填充 | 描边 | 投影 | 典型用途 |
|---|---|---|---|---|---|
| L0 | 窗口底 | `#090B10` 不透明 | 无 | 无 | 窗口背景 |
| L1 | 内容面板 | `#171B24` 不透明 | 无 | 无 | 直播预览区外框、内容区 |
| L2 | 玻璃面板 | `--ll-glass-fill` | 1px `--ll-glass-border` | 无 | 工具栏、侧栏、分组容器 |
| L3 | 悬浮玻璃 | `--ll-glass-fill-raised` | 1px `--ll-glass-border` | `--ll-glass-shadow` | 弹出菜单、对话框、悬浮卡片 |
| L4 | 覆盖层 | 透明 | 无 | 无 | 水印覆盖层 |

### 3.2 边缘高光

每个玻璃表面顶部有一条**垂直线性渐变**高光：

- 起点 `--ll-glass-highlight`（0.22 白）
- 终点 `--ll-glass-highlight-end`（0 白）
- 高度约为表面的 **1px–8%** 高度，取较小者
- 用 `QLinearGradient` + `QPainterPath` 裁剪到圆角矩形内

### 3.3 ❌ 技术红线

- **不得**把 `QGraphicsBlurEffect` 当作背景玻璃模糊方案。它是**控件自身**的模糊特效，不是"模糊背后内容"。
- **不得**声称 QSS 能实现真实背景折射。QSS 无 `backdrop-filter`。
- **不得**在视频帧刷新路径上做模糊 / 折射 / 额外图像拷贝 / 格式转换。
- 真实背景模糊（blur-behind）需要平台能力或离屏合成；**是否采用必须在 Phase 6 单独立原型实测后决定**，且必须先确认 Windows 版本、窗口模式与 Qt 兼容性。

---

## 4. 字体规范

### 4.1 字体族

**❌ 不采用 Skill 的 Cormorant / Montserrat。** 理由见 §0.2。

**✅ 采用系统字体栈（中文优先）：**

| 用途 | 字体族（按优先级） |
|---|---|
| 中文 | `Microsoft YaHei UI` → `Microsoft YaHei` → `Segoe UI` → `sans-serif` |
| 数字/等宽（帧率、坐标、时间戳） | `Cascadia Mono` → `Consolas` → `monospace` |

**理由（FACT + INFERENCE）**：LiveLens 是 Windows 中文桌面应用，使用系统字体可保证：零依赖、无字体文件分发、中文渲染质量最优、启动无网络请求。

### 4.2 字号与行高

| Token | 字号 | 行高 | 用途 |
|---|---|---|---|
| `--ll-font-xs` | 11px | 16px | 状态角标、时间戳 |
| `--ll-font-sm` | 12px | 18px | 辅助说明 |
| `--ll-font-base` | 13px | 20px | 正文、按钮、菜单 |
| `--ll-font-md` | 14px | 22px | 面板标题 |
| `--ll-font-lg` | 16px | 24px | 区块标题 |
| `--ll-font-xl` | 20px | 28px | 窗口标题 |

> **约束（来自 Skill）**：正文不得小于 12px。故 `--ll-font-xs`(11px) **仅限**角标/时间戳等非阅读性文字。

---

## 5. 间距 / 圆角 / 动效

### 5.1 间距（4px 基准）

`--ll-space-1` 4px · `-2` 8px · `-3` 12px · `-4` 16px · `-5` 20px · `-6` 24px · `-8` 32px

### 5.2 圆角

| Token | 值 | 用途 |
|---|---|---|
| `--ll-radius-sm` | 6px | 按钮、输入框 |
| `--ll-radius-md` | 10px | 玻璃面板、分组 |
| `--ll-radius-lg` | 14px | 对话框、弹出菜单 |
| `--ll-radius-pill` | 999px | 状态胶囊 |

### 5.3 动效令牌

| Token | 值 | 用途 |
|---|---|---|
| `--ll-duration-fast` | 120ms | 悬停、按下 |
| `--ll-duration-base` | 180ms | 状态切换、淡入淡出 |
| `--ll-duration-slow` | 260ms | 面板展开、对话框 |
| `--ll-easing-standard` | `OutCubic` | 通用 |
| `--ll-easing-enter` | `OutCubic` | 进入 |
| `--ll-easing-exit` | `InCubic` | 退出（比进入快） |

> **来源**：Skill `--domain ux` 明确"使用共享 motion token，**不把 150–300ms 当普适要求**"。上表是按用途分档的共享令牌，而非一刀切。
> **Skill 约束**：每屏最多 1–2 个关键元素做动画；必须支持"减少动态"。

### 5.4 减少动态

Qt 无 `prefers-reduced-motion` 媒体查询。**等价实现**：提供应用内开关（读系统设置或应用配置），关闭时：

- 所有过渡时长置 0；
- 水印动画**仍然运行**（它是功能性的防盗用标识，非装饰）—— 但此项需在 Phase 5 明确决策并记录。

---

## 6. 图标系统

### 6.1 规则（来自 Skill `--domain icons`）

1. **每层界面只用一个图标家族**（Keep one visual family per surface）。
2. 纯图标按钮**必须**有可访问名称（Qt：`setAccessibleName()`）+ 中文工具提示（`setToolTip()`）。
3. 设置入口使用语义明确的 **`gear`（齿轮）** 图标。
4. 图标语义优先精确，不用含义模糊的自绘图形。

### 6.2 规格

| Token | 值 |
|---|---|
| 工具栏图标尺寸 | 20×20 px |
| 菜单图标尺寸 | 16×16 px |
| 图标线宽 | 1.5px（Outline 风格） |
| 图标与文字间距 | 6px |
| 图标按钮最小点击区 | 32×32 px（工具栏）/ 44×44 px（触控场景） |

### 6.3 ⚠️ 未决项（转 Phase 2）

**UNKNOWN**：Skill 推荐 Phosphor（主）+ Heroicons（兜底），但**两者均为 React 图标库，PySide6 无法直接使用**。

Phase 2 必须审计项目**现有**图标来源（QIcon / SVG 文件 / Qt 标准图标 / 自绘 QPainter），再决定统一方案。

**硬约束**：**不得为统一图标而安装任何新依赖。**

---

## 7. 无障碍要求（验收项）

源自 Skill 的三组检索结果，转为 LiveLens 可执行验收项：

| # | 要求 | 来源 | Qt 等价实现 |
|---|---|---|---|
| A1 | 每个可交互控件有可见焦点环 | ux / Focus States (High) | QSS `:focus` + 2px `--ll-accent` 描边 |
| A2 | 焦点不被常驻 UI 遮挡 | ux / Focus Not Obscured | 焦点进入时收起遮挡面板 |
| A3 | 玻璃表面文字对比度 ≥4.5:1 | ux + style | §2.3 + 局部衬底 |
| A4 | 支持"减少动态" | ux / Reduced Motion (High) | 应用内开关，见 §5.4 |
| A5 | 每屏动画 ≤1–2 个关键元素 | ux / Excessive Motion (High) | 设计评审项 |
| A6 | 纯图标控件有可访问名称 | icons | `setAccessibleName()` + `setToolTip()` |
| A7 | 每层单一图标家族 | icons | 设计评审项 |

---

## 8. 反模式（禁止）

来自 Skill 的 `Avoid` 与 `--domain style` 实现检查表：

- ❌ Cheap visuals（廉价感：生硬描边、无层次的纯色块、默认 Qt 控件外观裸奔）
- ❌ Fast animations（过快动效：<100ms 的状态切换会显得闪烁）
- ❌ 用 Emoji 当图标
- ❌ 移除焦点环而不提供替代
- ❌ 只靠颜色传达状态（需同时有文字/形状）
- ❌ 混合两种图标家族
- ❌ 在视频帧上叠加任何模糊/折射处理
- ❌ 大面积遮挡直播画面

---

## 9. 文件索引

| 文件 | 内容 |
|---|---|
| `MASTER.md`（本文件） | 全局令牌、原则、验收项 |
| `pages/main-window.md` | 主窗口与背景层规范 |
| `pages/toolbar.md` | 顶部工具栏规范 |
| `pages/preview-watermark.md` | 直播预览与水印覆盖层规范 |

**规则**：页面文件只描述该页面的**差异**；未提及处一律遵循 MASTER。
