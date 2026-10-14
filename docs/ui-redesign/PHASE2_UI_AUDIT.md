# PHASE2_UI_AUDIT — LiveLens 现有 UI 与授权状态审计

> **阶段**：Phase 2
> **日期**：2026-10-14
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`，HEAD `cd24c98`）
> **方法**：只读代码审计（4 个并行审计通道），全部结论附 `file:line` 证据
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 0. 审计前 Git 状态核验（FACT）

| 项 | 实测值 |
|---|---|
| 目标 worktree 分支 | `feature/ui-liquid-glass` |
| 目标 worktree HEAD | `cd24c98 docs(ui-redesign): Phase 0 Skill 能力审计 + Phase 1 设计系统` |
| 目标 worktree porcelain | 仅 `?? runtime/ffmpeg/ffmpeg_5x_working.exe`（已知机器资产，有意不提交） |
| 主工作区分支 | `feature/bare-stream-integration` |
| 主工作区 HEAD | `f0418ea feat(stream): 裸流门禁增加 DLV_FFMPEG7_EXE 可执行校验` |
| 主工作区 porcelain | `M gui/main_window.py`、`M stream/adapters/douyin.py`、6 项未跟踪 —— **与 Phase 0 记录完全一致，未被触碰** |
| worktree 总数 | 11（含主工作区），与 Phase 0 记录一致 |
| Phase 0/1 产物 | `design-system/livelens/{MASTER.md,pages/*}`、`docs/ui-redesign/*`、`evidence/`(11 项) 全部存在 |

**结论**：基线核验通过，无差异。

---

## 1. 任务 2.1 — 许可状态审计

### 1.1 权威数据源（FACT）

**UI 层权威对象**：`MainWindow._access_status`，`gui/main_window.py:643`：

```python
self._access_status = AccessStatus()
```

**底层真值**：`LicenseManager`（`main_window.py:655`）的 `LicenseResult`。

数据流：
```
LicenseManager.load_stored()  [后台线程 LicenseWorker]
   → LicenseResult (utils/license_manager.py)
   → _license_loaded_sig (Qt Signal, main_window.py:507)
   → _on_license_loaded (main_window.py:3357)
   → _apply_license_result (main_window.py:3787)
   → AccessStatus.set_license_active/inactive
```

### 1.2 状态结构（FACT）

**两层枚举**：

`AccessState`（`utils/access_status.py:27-31`）—— **UI 消费层**：
| 值 | 含义 |
|---|---|
| `ACTIVE` | 已激活且未临近过期 |
| `EXPIRING` | 已激活但临近过期 |
| `EXPIRED` | **未激活 / 已过期 / 无效** —— 全部塌缩到这一态 |

关键代码 `access_status.py:108-109`：
```python
if not self._license_active:
    return AccessState.EXPIRED
```

`LicenseStatus`（`utils/license_manager.py:63-71`）—— **底层层**：
`NOT_ACTIVATED` / `ACTIVATED` / `ACTIVATION_FAILED` / `EXPIRED` / `INVALID` / `WRONG_MACHINE`

**⚠️ 重要结论（FACT）**：原系统对 UI 呈现**实质是二态**——「已激活(ACTIVE/EXPIRING)」与「未激活(EXPIRED)」。
底层虽有 6 种 `LicenseStatus`，但 UI 层把它们全部塌缩为 `EXPIRED`。

**任务书 §2.1 要求"明确区分授权有效/无效/状态未知"。实测：原系统没有独立的"未知/正在验证"态。** 按任务书"如果原系统只有二态，则如实记录并按其契约实现"，**本任务严格遵循现有二态契约**，不新建平行状态。

### 1.3 可观察性（FACT）

**不是 Qt 信号**，而是 **回调 + 定时器轮询** 组合：

| 机制 | 位置 | 说明 |
|---|---|---|
| 回调注册 | `access_status.py:67-70` `set_callbacks(on_status_changed, on_expired)` | **已被占用**：`main_window.py:652` 注册了 `_on_access_status_changed` / `_on_access_expired` |
| 状态变更触发 | `access_status.py:150-159` `_notify_status_changed()` | 由 `set_license_active/inactive/clear_license_state` 调用 |
| 轮询兜底 | `access_status.py:47` `CHECK_INTERVAL_MS`(60s) → `_access_timer` → `_access_status.refresh()`（`main_window.py:3293-3298`） | 捕获运行期过期 |
| 启动投递 | `main_window.py:507` `_license_loaded_sig = Signal(object)` | 后台线程 → GUI 线程（跨线程安全） |

**⚠️ 关键实现约束（FACT）**：`set_callbacks()` **不可重复调用**——第二次调用会**覆盖**第一次的回调，从而破坏现有 UI 更新逻辑。

**✅ 正确接入点**：在 `_on_access_status_changed`（`main_window.py:3300`）内部、紧邻现有 `self._sync_activation_overlay()` 调用（`main_window.py:3337`）处插入水印刷新。

### 1.4 时序与失败语义（FACT）

- 启动验证是**异步**的（后台线程，`_start_license_load` `main_window.py:3348-3355`）。
- 但验证是**纯本地离线**的（`license_manager.py:352-388` 读文件 + 校验签名/机器码/有效期），**不联网**。联网只发生在 `activate()` 与 `heartbeat()`。
- **验证落地之前**，状态是 `_license_active = False`、`note = "未激活"`（`access_status.py:59-61`）→ **UI 会先看到"未激活"**。
- **失败语义 = fail closed（保守关闭）**：`get_state()` 在 `not self._license_active` 时一律返回 `EXPIRED`。**不存在 fail-open 路径**。
- 激活时网络失败 → `ACTIVATION_FAILED`，且**本地存储完全未被触碰**（`license_manager.py:207-209`）。

**INFERENCE**：由于默认即"未激活"且 fail-closed，水印在启动瞬间会短暂显示，直到离线验证完成。这是**符合契约**的行为，但会造成视觉闪烁。→ 处置见 §5.2。

### 1.5 既有 UI 消费者（FACT）

| 消费者 | 位置 | 订阅方式 |
|---|---|---|
| `_access_status_label`（状态文字） | `main_window.py:1327-1330`, 更新于 `:3300-3319` | 回调 |
| **`_activation_overlay`**（未激活遮罩层） | `_ActivationOverlay` `:405`，实例 `:971`，显隐由 `_sync_activation_overlay()` `:3757-3769` | 回调 |
| 控件禁用 | `_connect_btn`, `_url_input`, `_roi_btn` `:3324-3334`；门禁 `_guard_access()` `:3275-3291` | 回调 |
| `ExpiredDialog` | `_on_access_expired` `:3339-3343` | 回调 |
| `SettingsDialog` | `settings_dialog.py:195-196` **主动拉取** `get_status_text()` | 无订阅 |
| `ActivationDialog` | `activation_dialog.py:267` 读 `manager.last_result` | 无订阅 |

**✅ 可复用模式（FACT）**：`_sync_activation_overlay()`（`main_window.py:3757-3769`）就是"根据 `is_expired()` 显隐一个覆盖层"的现成范例，水印应**镜像**该模式。

### 1.6 线程安全（FACT）

- `AccessStatus` 的写入全部发生在 **GUI 线程**（通过 queued signal `_license_loaded_sig`）。
- `LicenseStore` 用 `threading.RLock`（`license_store.py:53`），`LicenseManager` 用 `_activation_lock`（`license_manager.py:156`）。
- **`AccessStatus` 自身无锁** → 必须视为 **GUI 线程独占**。

---

## 2. 任务 2.2 — 视频预览结构审计

### 2.1 控件类与渲染路径（FACT）

`gui/video_widget.py:28`：
```python
class VideoWidget(QWidget):
```
**普通自定义绘制 `QWidget`**，非 `QLabel` / `QGraphicsView` / `QOpenGLWidget` / `QQuickWidget`。

- 帧投递：**直接方法调用**，无携带帧的信号。`video_widget.py:54` `def set_frame(self, frame, seq=None)`；内部 `QPixmap.fromImage(qimg)` 后 `self.update()`（`video_widget.py:98-99`）。
- 绘制：`paintEvent`（`video_widget.py:138-200`），核心缩放：
```python
# video_widget.py:148-158
if self._pixmap and not self._pixmap.isNull():
    scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    x = (self.width() - scaled.width()) // 2
    y = (self.height() - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
```

**`_FrameBridge`（`main_window.py:240`）不是信号载体**，它是后台读取循环（`:289-318`）；`frameReady` 是空信号，仅作通知，链路：`main_window.py:2288` → `_on_frame_ready`(`:3051`) → `_consume_latest_frame`(`:3073`) → `set_frame`(`:3099`)。

### 2.2 缩放与坐标系（FACT）

- `Qt.KeepAspectRatio` + 居中偏移 → **letterbox / pillarbox**，黑边填充（`video_widget.py:146`）。
- 正向映射（源→显示）在 `video_widget.py:174-177`：
```python
roi_x = offset_x + (self._roi_rect.x() * scaled_w) // source_w
roi_y = offset_y + (self._roi_rect.y() * scaled_h) // source_h
roi_w = (self._roi_rect.width() * scaled_w) // source_w
roi_h = (self._roi_rect.height() * scaled_h) // source_h
```

### 2.3 控件层级（FACT）

构建于 `_build_preview`（`main_window.py:937-981`）：

```
MainWindow
 └ QSplitter(Qt.Horizontal)            main_window.py:910
    └ addWidget(self._build_preview())  main_window.py:914
       └ preview column (QWidget) → QVBoxLayout → layout.addWidget(self._video_container, 1)  main_window.py:979
          └ OverlayHost  self._video_container = OverlayHost()   main_window.py:943
             └ QVBoxLayout → video_layout.addWidget(self._video_widget, 1)   main_window.py:951
                └ VideoWidget  self._video_widget = VideoWidget()            main_window.py:950
```

### 2.4 ✅ 已有覆盖层机制（FACT —— 本任务最重要的发现）

`gui/widgets.py:175` `class OverlayHost(QFrame)`，注册接口 `widgets.py:186-198`：
```python
def add_overlay(self, widget, interactive=False):
    widget.setParent(self)
    widget.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)
    self._overlays.append(widget)
    widget.setGeometry(self.rect())
```
`_sync_overlays` 在 `resizeEvent` 中重设几何（`widgets.py:200-207`）。

**已有使用者**：连接状态标签（`main_window.py:957`）、复制提示 toast（`:963`）、**未激活遮罩 `_activation_overlay`（`:977`）**。

**ROI 框**不走覆盖层，而是**画在 `VideoWidget.paintEvent` 内部**（`video_widget.py:160-183`，红色 `QPen`）。

**✅ 结论（FACT）**：水印应作为 **`OverlayHost` 的注册覆盖层**实现，`interactive=False` 天然获得鼠标穿透（`widgets.py:196`）。**不需要也不允许**改 `VideoWidget.paintEvent`。

### 2.5 ROI 坐标边界（FACT）

ROI 选择是**独立最大化顶层窗口**，不是嵌入预览：
- `ROISelector(frame, parent=self)` `main_window.py:2597`；`setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)` + `WindowMaximized`（`roi_selector.py:82-83`）
- 反向映射（显示→源）`ocr/roi_selector.py:222-237`，比例来自 `_display_scale`（`roi_selector.py:170-173`）
- 产出 `ROI(..., source_width, source_height)`（`roi_selector.py:239-252`）

**INFERENCE**：水印覆盖层是 `OverlayHost` 的子控件，而 ROI 选择在**另一个顶层窗口**里进行 → **两者物理隔离，水印不可能影响 ROI 坐标换算**。这满足任务书红线。

### 2.6 鼠标事件（FACT）

- `VideoWidget` **没有任何鼠标处理器**。
- `widgets.py:643-685` 的处理器属于标题栏（无边框窗口拖拽 `startSystemMove`），与视频无关。
- ROI 的鼠标 press/move/release 在 `ROISelector`（`roi_selector.py:104-125`）。
- 非交互覆盖层自动获得 `WA_TransparentForMouseEvents`（`widgets.py:196`）→ 点击穿透。

---

## 3. 任务 2.3 — 顶部工具栏完整清点

### 3.1 窗口形态（FACT）

**无边框窗口 + 全自绘标题栏**，无原生标题栏。`main_window.py:516-519`：
```python
self.setWindowTitle("LiveLens · 抖音低延迟播放器")
self.setWindowFlag(Qt.FramelessWindowHint, True)
self.setMinimumSize(Metrics.WINDOW_MIN_WIDTH, Metrics.WINDOW_MIN_HEIGHT)
self.resize(1400, 860)
```
"顶部区域" = `_build_title_bar()`（`main_window.py:797`）+ `_build_address_bar()`（`:832`），于 `_init_ui()` `:759-760` 加入。

### 3.2 图标来源（FACT —— 关键）

**全项目没有任何图标资源文件**。所有图标都是**运行时 `QPainter` 矢量绘制**：
- `gui/widgets.py:283` `_draw_icon_glyph(...)`
- `gui/widgets.py:341` `icon_pixmap(kind, size, color)`
- `gui/widgets.py:364` `class IconButton(QPushButton)` —— 自绘按钮（`paintEvent` 在 `widgets.py:406-423`）

**无 `QStyle.StandardPixmap`，无 `.svg`，无 `.png` 图标资源。** 磁盘上唯一的图片在无关的 vendored Rust crate（`susi_source/crates/susi_server/src/assets/*`），**不可用作应用图标**。

### 3.3 完整交互项清单（FACT）

| # | 标签 / 提示 | 图标来源 | 创建位置 | 槽 / 回调 | 快捷键 | 启用条件 |
|---|---|---|---|---|---|---|
| T1 | 使用引导（仅 tooltip） | 自绘 `?` 文字（`widgets.py:290-299`） | `main_window.py:805-807` | `_on_help_clicked`(`:3432`) | 无 | 恒可用 |
| T2 | 激活许可证（仅 tooltip） | 自绘钥匙：椭圆+3线（`widgets.py:300-305`） | `main_window.py:810-813`（`variant=primary`） | `_on_license_clicked`(`:3672`) | 无 | `setEnabled(True)` `:3322` |
| T3 | 联系作者（仅 tooltip） | 自绘对话气泡（`widgets.py:306-317`） | `main_window.py:817-819` | `_on_contact_clicked`(`:3690`) | 无 | 恒可用 |
| **T4** | **设置（仅 tooltip）** | **自绘齿轮：8 辐条+齿圈+中心孔（`widgets.py:318-329`）** | **`main_window.py:821-823`** | `_on_settings_clicked`(`:3736`) | 无 | 恒可用 |
| T5 | 全屏工作台 / 退出全屏（仅 tooltip） | 自绘四角括号（`widgets.py:330-338`） | `main_window.py:827-829` | `_toggle_fullscreen`(`:1375`) | 无（docstring `:1376` 明确省略） | 恒可用；图标由 `set_kind` `:2005-2008` 切换 |
| W1 | 最小化 | 自绘横线（`widgets.py:270-271`） | `WindowButton` `widgets.py:600` | `self.showMinimized`(`:777`) | 无 | 恒可见 |
| W2 | 最大化 / 还原 | 自绘方框/双方框（`widgets.py:272-276`） | `WindowButton` `widgets.py:601` | `_toggle_maximized`(`:779`, 定义 `:1986`) | 无 | 恒可见；`set_kind` `:2003-2004` |
| W3 | 关闭 | 自绘 X（`widgets.py:277-279`） | `WindowButton` `widgets.py:602` | `self.close`(`:780`) | 无 | 恒可见 |
| A1 | 历史 | **纯文字**（`QPushButton("历史")`, `history_panel.py:203`） | `HistoryButton` `main_window.py:860-865` | `HistoryButton._on_clicked`(`history_panel.py:211`) → `HistoryPopup` | 无 | 恒可用；`setFixedWidth(64)` |
| A2 | ⋯ | **文字字形** `"⋯"` | `QToolButton` `main_window.py:868-887` | `InstantPopup` → `QMenu` | 无 | 恒可用 |
| A2a | ↳ 编辑备注…（菜单项） | 无 | `QAction` `main_window.py:877-880` | `_on_edit_note_clicked`(`:2035`) | 无 | `setEnabled(False)` `:878`；运行期按 `bool(url)` 切换 `:2023` |
| A2b | ↳ 清除历史记录…（菜单项） | 无 | `QAction` `main_window.py:883-885` | `_on_clear_recent_clicked`(`:2086`) | 无 | 恒可用 |
| A3 | 连接 / 断开 / 连接中... | **纯文字**（`variant=primary`） | `main_window.py:889-894` | `_on_connect_clicked`(`:2104`) | 无 | `setFixedWidth(96)`；连接中禁用 `:2124`，恢复 `:2311/2549/3326` |
| A4 | 地址输入框（仅 placeholder） | 无 | `QLineEdit` `main_window.py:849-855` | `returnPressed`→`_on_connect_clicked`(`:852`)；`textChanged`→`_sync_note_action`(`:854`) | **Enter** | 连接中禁用 `:2126`，恢复 `:2552` |
| P1 | » 收起面板 | 纯文字 | `main_window.py:1033-1038` | `_on_collapse_clicked`(`:4035`) | 无 | 恒可用 |
| P2 | « 展开面板 | 纯文字，默认隐藏 | `main_window.py:922-928` | `_on_expand_clicked`(`:4042`) | 无 | `.hide()` `:927`；面板收起时显示 `:1466` |

**邻近但非顶部区域**：音量滑块 + 模式下拉框（`audio_panel.py:53,57,66`，位于 `_build_video_toolbar` `:983`）；ROI「说明」「选择识别区域」与「开始OCR」在右侧面板（`:1246, 1261, 1278`）。

### 3.4 隐藏 / 次级入口（FACT）

- **完全没有菜单栏**：`gui/` 下 `menuBar()` / `setMenuBar` / `addMenu` / `QMenuBar` **零匹配**。唯一的 `QMenu` 是历史溢出菜单（`main_window.py:876`）。
- **完全没有右键菜单**：无 `setContextMenuPolicy` / `customContextMenuRequested`。
- **完全没有键盘快捷键**：无 `setShortcut` / `QShortcut` / `QAction.setShortcut`。仅 URL 输入框隐式 Enter（`:852`）。`_toggle_fullscreen` docstring 明确说明故意不设快捷键。
- 次级入口：未激活遮罩上的「立即激活」（`main_window.py:447` → `_on_license_clicked`）与「联系作者」（`:456` → `_on_overlay_contact_clicked` `:3694`）；设置对话框（`settings_dialog.py`）仅能通过设置图标进入，内含「追加授权」（`settings_dialog.py:181`）。
- `gui/onboarding.py` 是独立模态引导对话框，未接入顶部栏。

### 3.5 设置按钮问题根因（FACT —— 任务书要求修复项）

创建于 `main_window.py:821-823`：
```python
self._settings_btn = self._title_bar.add_icon_button(
    IconButton.SETTINGS, "设置")
self._settings_btn.clicked.connect(self._on_settings_clicked)
```

图标绘制于 `widgets.py:318-329`：
```python
elif kind == IconButton.SETTINGS:
    # 标准齿轮：一圈齿 + 齿圈 + 中心孔
    teeth = s * 0.86      # 齿顶半径
    ring = s * 0.52       # 齿根半径（也是齿圈半径）
    for i in range(8):
        angle = math.radians(i * 45.0)
        dx, dy = math.cos(angle), math.sin(angle)
        painter.drawLine(QPointF(cx + dx * ring, cy + dy * ring),
                         QPointF(cx + dx * teeth, cy + dy * teeth))
    painter.drawEllipse(QRectF(cx - ring, cy - ring, ring * 2, ring * 2))
    hole = s * 0.22
    painter.drawEllipse(QRectF(cx - hole, cy - hole, hole * 2, hole * 2))
```

**根因（INFERENCE，高置信）**：
- 绘制半径 `s = 6.0`（`widgets.py:420`）—— **极小**。
- 线宽 1.2px，颜色 `TEXT_DIM`（`widgets.py:411-415, 399-404`）—— **低对比**。
- 8 根 1.2px 细辐条 + 齿圈 + 0.22·s 中心孔，在约 28px 按钮内 → 视觉上呈**密集星芒/星号**，而非可辨识的齿轮。
- 唯一的文字线索是 tooltip「设置」（`:822`），hover 前不可见。

**这精确解释了任务书所述"设置按钮图标难以识别"。**

### 3.6 重复与异常（FACT）

- **死代码：`_build_status_bar`（`main_window.py:1826-1876`）从未被调用。** 它会创建**第二对**「使用引导」(`:1857`) 与「激活许可证」(`:1863`, `variant=primary`) 并**覆盖** `self._help_btn` / `self._license_btn`。因 `_init_ui` 调用的是 `_build_perf_bar()`（`:765`），这两个按钮从未出现。
- **概念重复**：设置图标按钮 vs 激活许可证按钮——设置对话框的「授权」页也能打开 `ActivationDialog`（`settings_dialog.py:216`），即激活有**两条路径**；「联系作者」出现在**三处**（标题栏 `:817`、遮罩 `:456`、设置对话框）。
- **5 个标题栏按钮无可见文字**（HELP / LICENSE / CONTACT / SETTINGS / FULLSCREEN）——纯图标 + tooltip，**可发现性风险高**，尤其 SETTINGS 与 HELP。
- **条件隐藏但仍创建**（`SHOW_INTERNAL_DIAGNOSTICS = False`，`main_window.py:366`）：「开始诊断」`_diag_btn`(`:1096`)、「导出诊断 CSV」`_diag_export_btn`(`:1106`)、「全 PSM 观测」`_diag_obs_check`(`:1125`)、「采集 ROI 样本」`_diag_capture_check`(`:1145`)，均在 `:1162-1163` `setVisible(False)`。
- **从未创建**：「数据集采集」`_dataset_btn`(`:1176`)，被 `DATASET_CAPTURE_ENABLED = False` 挡住（`ocr/dataset_capture.py:72`）。
- **隐藏实例**：`_scene_panel`(`:1227`) 构建后 `.hide()`(`:1228`)，其 `_game_list` 与「游戏」`QComboBox`(`:1213`) 功能重复。

---

## 4. 任务 2.4 — 主题系统审计

### 4.1 `gui/theme.py` 结构（FACT）

| 符号 | 位置 | 说明 |
|---|---|---|
| `class Palette` | `theme.py:51` | **类型化颜色令牌**（约 40 个）：`APP_BG`、`DOCK_BG`、`ELEVATED`、`HOVER`、`ACCENT`、`OK_TEXT`、`DANGER`、`FONT_FAMILY` 等（`theme.py:55-114`） |
| `class Metrics` | `theme.py:120` | 几何常量：`TITLEBAR_HEIGHT`、`RADIUS`、`ICON_BUTTON_SIZE`、面板宽度（`theme.py:123-142`） |
| `_TONE_DOT` | `theme.py:146` | 私有；**硬编码 hex** |
| `tone_color(tone)` | `theme.py:155` | 状态点取色 |
| `_QSS` | `theme.py:163-1025` | **单个约 860 行 QSS 模板**，用 `{TOKEN}` 占位 |
| `build_stylesheet()` | `theme.py:1028` | 按 `cls.__annotations__` 替换 `{TOKEN}` |
| `_build_palette()` | `theme.py:1041` | 私有；构建 `QPalette`（`#FFFFFF`/`#000000` 硬编码于 `:1052-1060`） |
| `configure_high_dpi()` | `theme.py:1070` | 见 §4.4 |
| `apply_theme(app=None)` | `theme.py:1087` | 设 Fusion 风格 + QPalette + **全局样式表** |
| `_win11_22h2_or_newer()` | `theme.py:1106` | 私有 |
| **`enable_backdrop(hwnd, kind)`** | **`theme.py:1119`** | **Win32 DWM Mica/Acrylic —— 已实现！** |
| `apply_dialog_acrylic(dialog)` | `theme.py:1149` | 对话框亚克力 |

**✅ FACT：已有集中式令牌体系。** 颜色集中在 `Palette`，QSS 只用 `{TOKEN}`。例外：`_TONE_DOT`(`:146`) 与 `_build_palette`(`:1052-1060`) 内联硬编码。

**✅ FACT：DWM Mica/Acrylic 已有实现**（`theme.py:1119`），且已有 Win11 版本判断（`:1106`）。这大幅降低了 Phase 6 的风险——**不需要新写平台调用**，只需审计其现有调用点与效果。

### 4.2 作用域（FACT）

**全局（`QApplication`）**：
- `theme.py:1100` `app.setStyleSheet(build_stylesheet())`（在 `apply_theme()` 内）
- `main_window.py:748` `theme.apply_theme()` —— **唯一的正式调用点**（`main.py:181` 只调 `configure_high_dpi()`）
- `ui_prototype.py:733` —— **仅原型，不随包发布**

**控件局部（逐对话框）**：
| 文件 | 行 |
|---|---|
| `gui/activation_dialog.py` | `:165`（局部 `_QSS` 定义于 `:48`） |
| `gui/settings_dialog.py` | `:120`（局部 `_QSS` 定义于 `:64`） |
| `gui/widgets.py` | `:466`（`_POPUP_QSS` 定义于 `:429`） |
| `gui/onboarding.py` | `:621, 718, 764, 825, 1196` |
| `gui/expired_dialog.py` | `:43, 56` |
| `gui/update_dialog.py` | `:163, 172, 195, 206, 216, 235, 246, 256, 417, 425, 434, 441` |
| `gui/main_window.py` | `:1368`（仅标题栏热区，`background: transparent`） |

**✅ 关键结论（FACT）**：全局表**只应用一次**；对话框局部表叠加其上，且**局部表不会泄漏给兄弟控件**。四个对话框局部 `_QSS`（`activation_dialog.py:48`、`settings_dialog.py:64`、`widgets.py:429`、`onboarding.py:660`）**已经引用 `Palette.*` 令牌** → **改 `Palette` 会自动传播**。

### 4.3 是否存在多套样式系统（FACT）

存在：`theme.py` 全局 QSS **+** 散落的内联 `setStyleSheet`。各文件计数：

| 文件 | 内联 `setStyleSheet` 次数 |
|---|---|
| `update_dialog.py` | **12**（原始字符串，**需手工改**） |
| `onboarding.py` | 6 |
| `expired_dialog.py` | 2（原始字符串，需手工改） |
| `main_window.py` | 1 |
| `widgets.py` | 1 |
| `settings_dialog.py` | 1 |
| `activation_dialog.py` | 1 |
| `history_panel.py` / `audio_panel.py` / `video_widget.py` | **0** |

### 4.4 高 DPI（FACT）

`theme.py:1070-1084`：
```python
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
```
在 `QApplication` 构造前调用（`main.py:181`）。**无** `AA_EnableHighDpiScaling` / `AA_UseHighDpiPixmaps` / `devicePixelRatio` 处理（Qt6 默认）。

### 4.5 字体（FACT）

`theme.py:165-168` 全局：
```css
QWidget { font-family: {FONT_FAMILY}; font-size: {FONT_SIZE}; }
```
- `FONT_FAMILY = '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei"'`（`theme.py:111`）
- `MONO_FAMILY = 'Consolas, "Cascadia Mono", monospace'`（`theme.py:112`）
- `FONT_SIZE="12px"`, `FONT_SIZE_SMALL="11px"`（`theme.py:113-114`）
- **已有显式中文字体回退**（`Microsoft YaHei UI` / `Microsoft YaHei`）
- 程序化 `QFont` 仅 3 处：`onboarding.py:236,254`、`widgets.py:295`

**✅ 与 Phase 1 设计系统一致性**：MASTER 要求的系统字体栈与现有 `FONT_FAMILY` **高度一致**（仅优先级顺序不同）。

### 4.6 主题切换（FACT）

**不存在**。无 light/dark 开关，无 `theme_mode`/`set_theme`/`toggle_theme`。`apply_theme()`（`theme.py:1087`）硬编码深色且幂等。

### 4.7 `gui/widgets.py` 可复用组件（FACT）

| 类 / 函数 | 位置 | 用途 |
|---|---|---|
| `set_property(widget,name,value)` | `:53` | 设动态 QSS 属性并 repolish |
| `set_tone(widget,tone)` | `:68` | 状态色调 |
| `chip_html` | `:73` | 状态胶囊 HTML |
| **`class DockPanel(QFrame)`** | **`:104`** | **圆角卡片/面板**（`objectName="dockPanel"`，QSS `theme.py:278`）→ **玻璃面板首选复用对象** |
| **`class OverlayHost(QFrame)`** | **`:175`** | **覆盖层宿主** → 水印挂载点 |
| `class WindowButton(QPushButton)` | `:213` | 自绘最小化/最大化/关闭 |
| `_draw_icon_glyph(...)` | `:283` | 矢量图标绘制（help/license/contact/settings/fullscreen） |
| `icon_pixmap(kind,size,color)` | `:341` | 字形 → QPixmap |
| **`class IconButton(QPushButton)`** | **`:364`** | **自绘图标按钮** → 工具栏复用对象 |
| **`class InfoPopup(QFrame)`** | **`:446`** | **圆角弹出卡片**，`border-radius:10px`（`:433`） |
| `class TitleBar(QFrame)` | `:544` | 标题栏 |
| `class FramelessController(QObject)` | `:691` | 无边框拖拽/边缘缩放 |

**✅ 无专用玻璃/模糊控件**。`DockPanel`(`:104`) 与 `InfoPopup`(`:446`) 是应被**扩展**而非替换的圆角容器。

---

## 5. 设计系统与真实架构的交叉核对（任务书 Phase 1 遗留项回填）

Phase 1 报告 §6 挂了 4 项，现全部回填：

| # | Phase 1 遗留 | Phase 2 结论 | 是否冲突 |
|---|---|---|---|
| 1 | 许可状态权威来源 | `MainWindow._access_status`（`main_window.py:643`）；二态契约；回调接入点在 `_on_access_status_changed`(`:3300`) | ✅ **无冲突** |
| 2 | `video_widget.py` 控件结构 | `VideoWidget(QWidget)`，且**已有 `OverlayHost.add_overlay()`** 现成机制（`widgets.py:186`） | ✅ **无冲突，且比预期更简单** |
| 3 | 顶部按钮完整清单 | 15 个交互项已清点（§3.3）+ 5 个条件隐藏项 | ✅ **无冲突** |
| 4 | `gui/theme.py` 作用域 | 已有 `Palette` 令牌体系 + 单一全局 `apply_theme()`；局部表不泄漏 | ✅ **无冲突，改 `Palette` 自动传播** |

**结论：Phase 1 设计规范与真实架构无冲突。Phase 1 的"有条件通过"现已转为完全通过。**

### 5.1 对 Phase 1 设计的两处修正

| 项 | Phase 1 原设想 | Phase 2 修正 | 理由 |
|---|---|---|---|
| 水印挂载 | "独立透明覆盖 QWidget 作为预览容器兄弟控件" | **改用现有 `OverlayHost.add_overlay(widget, interactive=False)`** | 项目已有该机制与 3 个使用者；自建平行机制违反"优先复用"原则 |
| 玻璃面板 | "新建玻璃面板组件" | **扩展 `DockPanel`(`widgets.py:104`)，不新建类** | 避免样式分裂（Phase 1 `COMPONENT_SPEC.md` §8 已预判此原则） |

### 5.2 新增设计决策：启动期水印闪烁

**问题（FACT + INFERENCE）**：`_license_active` 默认 `False`（`access_status.py:59-61`），离线验证异步完成 → 水印会先显示再消失，造成闪烁。

**处置方案（保守，不违反契约）**：
- 水印**跟随现有 `_activation_overlay` 的显隐时序**，不自行发明"验证中"状态。
- **INFERENCE**：由于 `_activation_overlay` 采用同一时序，若该遮罩本身已无可见闪烁问题，则水印同样可接受。
- **UNKNOWN**：实际闪烁是否可感知 → **Phase 3 启动实测时观察并记录**；若可感知，方案是在水印首次显示前加一个极短的稳定延迟，**但这会引入"状态未确认时的显示歧义"，须先与用户确认**。

---

## 6. 原有功能回归清单（Phase 4 逐项核对用）

### 6.1 必须保全的交互项（15 项）

| ID | 项目 | 保全要求 |
|---|---|---|
| T1 | 使用引导 | 入口存在；`_on_help_clicked` 可达 |
| T2 | 激活许可证 | 入口存在；`_on_license_clicked` 可达 |
| T3 | 联系作者 | 入口存在；`_on_contact_clicked` 可达 |
| **T4** | **设置** | 入口存在；**图标改为可辨识齿轮**；`_on_settings_clicked` 可达 |
| T5 | 全屏工作台 / 退出全屏 | 入口存在；`set_kind` 切换仍生效；`_toggle_fullscreen` 可达 |
| W1 | 最小化 | 不改动 |
| W2 | 最大化 / 还原 | 不改动；`set_kind` 仍生效 |
| W3 | 关闭 | 不改动 |
| A1 | 历史 | 入口存在；`HistoryPopup` 可打开 |
| A2 | ⋯ 溢出 | 入口存在 |
| A2a | 编辑备注… | 入口存在；**启用条件 `bool(url)` 不变** |
| A2b | 清除历史记录… | 入口存在 |
| A3 | 连接 / 断开 | **不改动语义**；`variant`/文字/禁用状态的多点变更（`:2124, 2311, 2549, 3326`）必须仍生效 |
| A4 | 地址输入框 + Enter | 不改动；`returnPressed` 与 `textChanged` 仍连接 |
| P1/P2 | 收起/展开面板 | 入口存在；显隐逻辑（`:927, 1466`）不变 |

### 6.2 快捷键回归（FACT）

**项目仅有 1 个隐式快捷键**：地址输入框 Enter（`main_window.py:852`）。
无 `QShortcut`、无 `QAction.setShortcut`、无 `setShortcut`。
→ **回归要求：改造后仍无新增/丢失快捷键，Enter 仍触发连接。**

### 6.3 受保护区域（禁止改动）

| 区域 | 文件 | 理由 |
|---|---|---|
| 直播接入 | `stream/adapters/douyin.py` | 任务书红线 |
| FFmpeg 解码 | `runtime/`、`stream/` | 任务书红线 |
| 帧队列/调度 | `main_window.py:240-318`（`_FrameBridge`）、`:3051-3099` | 任务书红线 |
| 识别调度 | `ocr/` | 任务书红线 |
| ROI 坐标映射 | `ocr/roi_selector.py:222-252`、`video_widget.py:174-177` | 任务书红线 |
| 许可验证语义 | `utils/access_status.py`、`utils/license_manager.py`、`license_server/` | 任务书红线 |
| 游戏配置/识别规则 | `ocr/` | 任务书红线 |

---

## 7. 风险清单（FACT / INFERENCE / UNKNOWN）

### 7.1 FACT（已确认的事实风险）

| # | 风险 | 证据 |
|---|---|---|
| F1 | `set_callbacks()` 重复调用会**覆盖**现有回调 | `access_status.py:67-70`，已占用 `main_window.py:652` |
| F2 | `_activation_overlay` 最后注册（`:977`），**层级最高**；后加的水印会盖住它 | `widgets.py:198` 顺序入栈 |
| F3 | `OverlayHost` 覆盖层填满**整个容器**而非视频可视矩形；letterbox 黑边也包含在内 | `widgets.py:198` + `video_widget.py:150-158` |
| F4 | `_build_status_bar`(`:1826-1876`) 是**死代码**，重构时若误调用会创建重复按钮并覆盖 `_help_btn`/`_license_btn` | 无调用点 |
| F5 | 设置齿轮在 `s=6.0`、线宽 1.2、`TEXT_DIM` 色下渲染为星芒而非齿轮 | `widgets.py:318-329, 399-420` |
| F6 | `utils/offline_activation.py` 在 worktree 中**缺失**（被 `.gitignore`） | 导入于 `license_manager.py:46`、`activation_dialog.py:44` |
| F7 | 全局样式表仅一处 `apply_theme()`；新增第二个 `QApplication.setStyleSheet` 会破坏现有对话框 | `theme.py:1100`、`main_window.py:748` |
| F8 | `AccessStatus` **无锁**，须视为 GUI 线程独占 | `access_status.py` 全文无锁 |

### 7.2 INFERENCE（推断，需验证）

| # | 推断 | 验证阶段 |
|---|---|---|
| I1 | 水印层需 `lower()` 到 `_activation_overlay` 之下，或按需 `raise_()` | Phase 5 实测 |
| I2 | 水印须自行复刻 `KeepAspectRatio` 居中数学才能贴合视频可视矩形 | Phase 5 实测 |
| I3 | 启动期水印可能闪烁（默认未激活 → 异步验证完成） | Phase 3 启动观察 |
| I4 | 扩展 `Palette` 即可让 4 个对话框局部样式自动跟随，仅 `update_dialog.py`/`expired_dialog.py` 需手工改 | Phase 6 验证 |
| I5 | 现有 `enable_backdrop()`(`theme.py:1119`) 可直接用于玻璃质感，无需新写平台调用 | Phase 6 原型实测 |

### 7.3 UNKNOWN（未确认）

| # | 未知项 | 影响 | 保守处理 |
|---|---|---|---|
| U1 | `enable_backdrop()` 是否已在生产路径被调用、效果如何 | 玻璃质感实现路径 | Phase 6 先读调用点 + 最小原型实测 |
| U2 | 启动期闪烁是否可感知 | 视觉质量 | Phase 3 观察记录；若可感知再决策 |
| U3 | 按钮"使用频率"无埋点数据 | 收纳决策 | 按代码语义 + 状态耦合度判断，**标注为 INFERENCE** |
| U4 | 极小预览区下水印的合法降级观感 | 视觉质量 | Phase 5 实测 |
| U5 | 高 DPI 200% 下自绘齿轮的清晰度 | 图标质量 | Phase 6 实测 |

---

## 8. Phase 2 门禁判定

| 门禁条件 | 结果 |
|---|---|
| 授权状态来源有代码证据 | ✅ `main_window.py:643` + `access_status.py:27-31,108-109` |
| 预览边界有代码证据 | ✅ `video_widget.py:138-200` + `widgets.py:175-207` |
| 工具栏全部交互项已清点 | ✅ 15 项 + 5 项条件隐藏 |
| 主题系统作用域有代码证据 | ✅ `theme.py:51,163,1028,1087` + 全部 `setStyleSheet` 调用点 |
| 遗留 UNKNOWN 已写明影响与保守方案 | ✅ §7.3 共 5 项 |

### ✅ **Phase 2 门禁通过，允许进入 Phase 3。**

**额外收获**：
1. Phase 1 的 4 项遗留 UNKNOWN **全部回填，且无冲突** → Phase 1 转为完全通过。
2. 发现 3 处可**显著降低实现风险**的既有设施：`OverlayHost.add_overlay()`、`Palette` 令牌体系、`enable_backdrop()`。
3. 发现 1 处**死代码**（`_build_status_bar`），Phase 4 应删除而非重构。
