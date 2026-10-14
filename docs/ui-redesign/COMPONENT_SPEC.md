# COMPONENT_SPEC — 组件规格

> **阶段**：Phase 1（设计）/ 实施于 Phase 6
> **依据**：`design-system/livelens/MASTER.md`
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 0. 通用规则

所有组件必须满足：

1. 颜色/圆角/间距**只使用 MASTER 中的令牌**，不得硬编码新色值。
2. 可交互组件必须有**悬停 / 按下 / 焦点 / 禁用**四态。
3. 纯图标控件必须有 `setToolTip()` + `setAccessibleName()`。
4. 过渡使用 `--ll-duration-*` 令牌，不得逐处自定义时长。
5. 玻璃材质组件遵循 MASTER §3 的分层规范。

---

## 1. 玻璃面板 (GlassPanel)

| 属性 | 规范 |
|---|---|
| 层级 | L2（`--ll-glass-fill`）或 L3（`--ll-glass-fill-raised`） |
| 圆角 | `--ll-radius-md` (10px) |
| 描边 | 1px `--ll-glass-border` |
| 顶部高光 | 垂直渐变，`--ll-glass-highlight` → `--ll-glass-highlight-end`，高度 min(1px, 8% H) |
| 投影 | L3 才有：`--ll-glass-shadow` |
| 实现 | QSS 负责填充/圆角/描边；`QPainter` + `QPainterPath` 负责高光与自定义表面 |

**QSS 可表达的部分**：`background-color`(rgba)、`border`、`border-radius`、`padding`。
**QSS 不可表达、必须自绘的部分**：顶部渐变高光、局部边缘反射、圆角裁剪的渐变。

---

## 2. 按钮

### 2.1 图标按钮 (IconButton)

| 属性 | 值 |
|---|---|
| 尺寸 | 32×32 px |
| 图标 | 20×20 px，单一 Outline 家族 |
| 圆角 | `--ll-radius-sm` (6px) |
| 四态 | 见 `pages/toolbar.md` §3 |
| 必需 | `setToolTip()` + `setAccessibleName()` |

### 2.2 图标+文字按钮 (LabeledButton)

| 属性 | 值 |
|---|---|
| 高度 | 32 px |
| 内边距 | 水平 12px |
| 图标-文字间距 | 6px |
| 字号 | `--ll-font-base` (13px) |
| 用途 | 分区 ② 的中频业务操作（提升可发现性） |

### 2.3 主操作按钮 (PrimaryButton)

| 属性 | 值 |
|---|---|
| 填充 | `rgba(128,191,255,0.18)` |
| 描边 | 1px `--ll-accent` |
| 文字/图标 | `--ll-accent` |
| 悬停 | 填充提至 0.26 |
| 用途 | 分区 ① 的"连接直播"等主操作，**全工具栏唯一** |

> **约束**：同屏主操作按钮**至多一个**（Skill: *Animate 1–2 key elements*、避免视觉噪音）。

---

## 3. 输入框 (LineEdit / ComboBox)

| 属性 | 值 |
|---|---|
| 高度 | 32 px |
| 填充 | `rgba(9,11,16,0.60)`（比玻璃更深，形成"凹陷"感） |
| 描边 | 1px `--ll-glass-border` |
| 圆角 | `--ll-radius-sm` |
| 文字 | `--ll-text-primary` |
| 占位符 | `--ll-text-secondary` |
| 焦点 | 描边 2px `--ll-accent` |
| 错误 | 描边 2px 危险色 + 邻近文字说明（**不得只靠颜色**，Skill 反模式） |

---

## 4. 菜单 / 弹出层

| 属性 | 值 |
|---|---|
| 层级 | L3 悬浮玻璃 |
| 圆角 | `--ll-radius-lg` (14px) |
| 项高 | 28 px |
| 项悬停 | `rgba(255,255,255,0.06)` |
| 图标 | 16×16 px |
| 分隔线 | 1px `--ll-glass-border` |
| 键盘 | 方向键导航、Enter 触发、Esc 关闭 |

---

## 5. 状态指示

| 类型 | 规范 |
|---|---|
| 状态灯 (StatusDot) | 8×8 px 圆形；颜色 + **文字标签**（不得只靠颜色） |
| 连接正常 | `--ll-success` |
| 断开/错误 | 危险色 |
| 未知 | `--ll-text-secondary` |
| 状态胶囊 (StatusPill) | `--ll-radius-pill`，高 20px，`--ll-font-xs` |

> **来源**：Skill 反模式 "Relying on color alone to convey meaning"。

---

## 6. 对话框

| 属性 | 值 |
|---|---|
| 层级 | L3 |
| 圆角 | `--ll-radius-lg` |
| 遮罩 | `rgba(9,11,16,0.60)` |
| 结构 | 标准标题 / 内容 / 操作按钮（右对齐） |

> **来源**：`--stack winui` 的概念性建议 —— "模态交互应使用标准对话框模式，而非自制 overlay + 手工焦点陷阱"。**仅采纳概念，不采纳其 API。**

---

## 7. 水印组件

见 `WATERMARK_SPEC.md` 与 `design-system/livelens/pages/preview-watermark.md`。

---

## 8. ⚠️ 现有组件审计需求（转 Phase 2）

**UNKNOWN**：项目已有 `gui/widgets.py`（34 KB），其中可能已存在自定义组件。Phase 2 必须审计：

| # | 问题 |
|---|---|
| C1 | `gui/widgets.py` 中已有哪些自定义组件？能否复用/扩展？ |
| C2 | `gui/theme.py` 的令牌命名与作用域？本设计的 `--ll-*` 令牌如何与之共存？ |
| C3 | 现有组件是否已被多处引用（改动影响面）？ |

**原则**：**优先扩展现有组件**，避免重复造轮子与样式分裂。若本设计的组件与现有组件冲突，**以现有组件为基础改造**，而不是新增一套并行体系。
