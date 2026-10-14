# PHASE5_WATERMARK — 未激活动态水印

> **阶段**：Phase 5（第二个代码实现阶段）
> **日期**：2026-10-14
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`，起始 HEAD `00f17fe`）
> **依据**：`design-system/livelens/pages/preview-watermark.md`、`docs/ui-redesign/WATERMARK_SPEC.md`、`docs/ui-redesign/PHASE2_UI_AUDIT.md`
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 1. 阶段目标与达成

| 目标（任务书 Phase 5） | 状态 |
|---|---|
| 独立 UI 覆盖层，不修改视频帧 | ✅ 实测帧字节零变化 |
| 不参与解码/拉流/OCR/ROI | ✅ 水印模块不导入任何许可/视频模块 |
| 不拦截预览区鼠标事件 | ✅ `WA_TransparentForMouseEvents` |
| 文本 `LiveLens`，唯一实例 | ✅ 容器内恰好 1 个 |
| 仅未激活时显示 | ✅ 与 `is_expired()` 严格一致 |
| 边界反弹（含角落双轴反转） | ✅ 纯函数 + 43 项算法断言 |
| 不越出可视预览矩形 | ✅ 3000 步真实推进零越界 |
| 尺寸变化重算边界并钳位 | ✅ 三种容器尺寸 + letterbox 实测 |
| 极小预览区安全降级 | ✅ 冻结该轴，不抖动不异常 |
| 销毁/隐藏/关闭正确停表 | ✅ 隐藏即停，关闭后 0 个活动 QTimer |
| 与帧率解耦、受控刷新 | ✅ 独立 QTimer，30fps，246.7 µs/帧 |
| 不用 `QGraphicsBlurEffect` 冒充背景模糊 | ✅ 未使用任何模糊效果 |
| 不做磁盘/网络 I/O | ✅ 动画循环内无 I/O |

---

## 2. 架构（FACT）

### 2.1 新增/改动文件

| 文件 | 改动 |
|---|---|
| **`gui/watermark.py`** | **新增**：`bounce_step()` 纯函数 + `WatermarkOverlay` 覆盖层 |
| `gui/video_widget.py` | **新增只读访问器** `visible_video_rect()` |
| `gui/main_window.py` | 挂载覆盖层；`_sync_watermark()`；在 2 处状态同步点接入 |
| `tests/ui_redesign/test_watermark.py` | **新增**：60 项断言 |
| `tests/ui_redesign/watermark_evidence.py` | **新增**：视觉证据生成器 |

### 2.2 分层

```
预览容器 OverlayHost (previewFrame)
 ├─ VideoWidget               ← 帧显示（**未改动 set_frame/paintEvent**）
 ├─ QLabel 连接提示
 ├─ QLabel 复制提示
 ├─ _ActivationOverlay        ← 未激活遮罩（interactive=True，可点）
 └─ WatermarkOverlay          ← 水印（interactive=False，鼠标穿透）
```

**水印层级在未激活遮罩之上** —— 理由见 §4。

### 2.3 唯一新增的对外接口（FACT）

`VideoWidget.visible_video_rect() -> QRect`：纯几何只读访问器，用
`QSize.scaled(..., KeepAspectRatio)` 复算 paintEvent 里同一套居中算法
（与 `QPixmap.scaled` 内部同一个函数，因此**精确一致**而非近似）。

- **不绘制、不改状态、不做像素缩放**（O(1)）；
- 没有帧时返回 `self.rect()`；
- **未改动** `set_frame` / `paintEvent` / 帧队列 / 任何渲染逻辑。

**为什么需要它**：水印必须落在**画面**内而不是上下/左右黑边上。替代方案是让水印自己读
`_pixmap`（私有字段），更脆弱；或每次 tick 调用 `QPixmap.scaled()`（1080p 下每帧几毫秒，不可接受）。

---

## 3. 运动算法（FACT）

### 3.1 纯函数

`gui/watermark.py::bounce_step(x, y, vx, vy, w, h, W, H)` —— 无副作用、可确定性测试。

```
合法范围：0 <= x <= W-w ，0 <= y <= H-h
每 tick：先积分 → 逐轴修正位置 + 反转速度（用 abs() 强制方向）
```

- **两轴独立处理** → 角落处两个方向**同时**反转；
- **位置修正是必须的**（只反转速度会让水印停在边缘外或抖动）；
- `W-w <= 0`（水印比预览区宽）时**冻结该轴**并置 0：此时"反弹"在数学上无意义，
  逐帧钳位会表现为抖动，因此停速。

### 3.2 算法测试（FACT）

`test_watermark.py` §A 共 12 项，含：

| 断言 | 结果 |
|---|---|
| 常规推进 | ✅ |
| 左/右/上/下边界：位置修正 + 速度反转 | ✅ ×4 |
| **右下角 / 左上角：两轴同时反转** | ✅ ×2 |
| **20000 次迭代始终满足合法范围** | ✅ |
| 已越界时即使速度方向为正也被纠正 | ✅ |
| 预览区小于水印 → 冻结两轴 | ✅ |
| 仅单轴放不下 → 只冻结该轴，另一轴正常 | ✅ |

### 3.3 轨迹证据（FACT）

`docs/ui-redesign/evidence/screenshots/phase5_watermark_trail.png`

- 4000 步轨迹（每 30 步画一次），**全部落在边界框内**；
- 碰撞计数：左 5 / 右 5 / 上 6 / 下 7；
- **角落碰撞在 4000 步内为 0** —— 这是轨迹周期的自然结果（x 轴周期 ≈362.5 tick、
  y 轴 ≈302.7 tick，比值不整，短时间内难以同时触边），**不代表角落逻辑有问题**：
  角落双轴反转由 §A 的**直接单元断言**证明。图中内嵌小图另用等速短周期
  演示了角落反弹（260 步内两轴同时反转 1 次，绿框标出）。

---

## 4. 🔴 实施中发现并修复的两个真实缺陷（FACT）

### 4.1 缺陷 A：水印画在了控件左上角，而不是它该在的位置

**现象**：`watermark_rect()` 算出的位置完全正确，但屏幕上该位置什么都没有。

**根因**：`paintEvent` 里用 `QRectF(0.5, 0.5, w-1, h-1)` 画玻璃底、用
`drawText(self.rect(), ...)` 画文字 —— 都是相对**控件原点**。而本控件被
`OverlayHost` 铺满整个预览容器（1058×677），控件原点 ≠ 水印当前位置。
`_x/_y` 从未参与绘制。

**修复**：`paintEvent` 开头 `painter.translate(target.x(), target.y())`，
全部绘制改为相对水印自身矩形。

**回归防护**：新增 §G「真实渲染定位」——用 `window.render()` 渲染整窗，
采样 `watermark_rect()` 映射到窗口坐标后的区域，要求该处峰值显著高于对照区
（实测 285 vs 65）。这类"算对了但画在别处"的 bug，纯逻辑测试**测不出来**。

### 4.2 缺陷 B：水印被未激活遮罩完全盖住（功能等于不存在）

**现象**：截图里完全看不到水印。

**根因**：`_sync_activation_overlay()` 每次都调用
`self._activation_overlay.raise_()`（`main_window.py:3783`）。这会把它提到
**子控件栈顶**，压过任何后注册的兄弟控件。实测子控件序：
`[VideoWidget, QLabel, QLabel, WatermarkOverlay, _ActivationOverlay]`。

**为什么这是致命缺陷**：遮罩用 `rgba(11,12,14,0.88)` 的 scrim 压暗整个预览区，
而水印与遮罩由**同一个** `denied` 布尔量驱动（遮罩可见 ⟺ 水印可见）。
因此"水印在遮罩之下"等价于"水印**永远不可见**"，整个功能失去意义。

**修复**：`WatermarkOverlay._sync_visibility()` 在 `show()` 后显式
`self.raise_()`。主窗口调用顺序为 `_sync_activation_overlay()` →
`_sync_watermark()`，因此水印恰好落在遮罩之上。同时给
`_finish_unlock_transition()` 补上 `_sync_watermark()`，保证该不变式在所有路径成立。

**是否因此遮挡「立即激活」按钮？不会（FACT）**：
- 水印 `WA_TransparentForMouseEvents`，点击一律穿透 → **按钮可操作性零影响**；
- 视觉上水印半透明（底 35% / 描边 20% / 文字 36%）且持续移动，只在经过时短暂叠压。
- 截图证据：`phase5_watermark_t2.png` 中水印位于「立即激活」左侧，按钮完全清晰可读。

> **记录动机**：这两个缺陷都是"逻辑测试全绿但功能实际不可用"的类型。
> 前者靠**像素级渲染断言**发现，后者靠**真实截图**发现。这印证了任务书
> "自动化测试通过不代表人工验收通过"。

---

## 5. 授权状态映射（FACT）

| 状态 | 水印行为 | 实测 |
|---|---|---|
| 未激活（`is_expired()`） | 显示 + 动画运行 | ✅ |
| 已激活 | 立即隐藏 + 计时器**停止** | ✅ |
| 状态切回未激活 | 重新显示 + 动画重启 | ✅ |
| **状态未确认**（许可尚未落地） | **按未激活处理 → 显示**（保守，不误报已激活） | ✅ |
| 重复设置同一状态 | 幂等 | ✅ |

**状态来源**：`self._access_status.is_expired()`，与 `_sync_activation_overlay` 消费
**同一个**布尔量。水印模块**不导入**任何许可模块（`license_manager` /
`access_status` / `license_store` / `offline_activation` / `susi_verifier`）——
已用源码字符串扫描断言（§B）。

**FACT — 本机实际工况**：`AccessState.EXPIRED`，`status_text="激活失败"` →
水印在真实环境下确实显示，Phase 5 的验证不是"造出来的状态"。

---

## 6. 业务隔离（红线，FACT）

| 断言 | 结果 |
|---|---|
| 水印运行 300 帧后，原始视频帧字节**完全未变** | ✅ `hash(frame.tobytes())` 前后一致 |
| 帧内容逐像素一致（`np.array_equal`） | ✅ |
| 水印运行不影响 `visible_video_rect()`（ROI 映射基准） | ✅ |
| `_roi_rect` 未被改动 | ✅ |
| 清空视频帧后水印仍能独立推进（证明不依赖帧数据） | ✅ |
| 水印模块源码不含 `set_frame` / `_pixmap` | ✅ |

**未改动**：`stream/adapters/douyin.py`、FFmpeg 解码、帧队列/调度、
`ocr/`（含 `roi_selector.py` 坐标映射）、许可验证语义（FACT）。

---

## 7. 性能（FACT —— 精确计量）

### 7.1 为什么不用整进程 CPU

整进程 CPU 采样在本机噪声过大（实测 tick=50ms 比 tick=33ms "更耗 CPU"，
物理上不可能），因此改为**直接计量水印自身耗时**，可归因、可复现。

### 7.2 单帧成本（600 帧实测）

| 项 | 单次耗时 |
|---|---|
| `step()`（纯算术 + 计算重绘区域） | **91.5 µs** |
| `paintEvent`（玻璃底 + 描边 + 高光 + 文字） | **155.2 µs** |
| **每帧合计** | **246.7 µs** |

### 7.3 折算 CPU 占用（单核百分比）

| tick | 帧率 | 水印自身 CPU |
|---|---|---|
| **33 ms（当前采用）** | 30.3 fps | **0.75 %** |
| 50 ms | 20.0 fps | 0.49 % |
| 66 ms | 15.2 fps | 0.37 % |

**刷新间隔评估（任务书要求）**：已实测 3 档。选择 **33ms / 30fps** —— 运动是
约 48 px/s 的缓慢漂移，30fps 保证观感平滑；0.75% 单核占用低于应用空闲基线
（≈0.8%），且只在**未激活**时运行。若将来需要进一步压缩，降到 50ms 可省约 1/3。

### 7.4 隐藏态零开销（FACT）

| 状态 | CPU 均值 |
|---|---|
| 水印关闭（已激活） | 0.82 % |
| 水印隐藏（计时器已停） | 0.64 % |

差值 −0.18pp 在噪声内 → **隐藏即停表，不空转**（与 §E 的 `is_animating()==False` 互相印证）。

### 7.5 内存

RSS 248.3 MB → 248.5 MB（**+0.2 MB**）。

---

## 8. 视觉证据（FACT）

| 文件 | 内容 |
|---|---|
| `evidence/screenshots/phase5_watermark_t0.png` | 时刻 0（水印在 (124,186)） |
| `evidence/screenshots/phase5_watermark_t1.png` | 时刻 1（(223,257)，位置不同 → 在动） |
| `evidence/screenshots/phase5_watermark_t2.png` | 时刻 2（(319,326)） |
| `evidence/screenshots/phase5_watermark_trail.png` | 4000 步轨迹 + 角落反弹内嵌示意 |
| `evidence/screenshots/phase5_watermark_crop_4x.png` | 4 倍放大裁剪 |

三张截图的 `watermark_rect()` 两两不同（`distinct positions` 断言为 True），
且 PNG 像素采样确认水印区峰值 285 vs 对照区 65 —— **截图里确实有水印，不是"跑了但没画"**。

> **⚠️ 教训（已记录）**：证据脚本最初设了 `QT_QPA_PLATFORM=offscreen`，
> 导致 Qt 不做系统字体回退、中文渲染成方框，截图无法作为"真实运行"证据。
> 已改为使用真实平台。**逻辑测试可以 offscreen，视觉证据不可以。**

---

## 9. 回归（FACT —— 零回归）

12 项既有测试，改造前后**逐项结果完全一致**：

| 测试 | 基线 | Phase 5 |
|---|---|---|
| `test_phase_ui3_activation_settings.py` | 62 / 0 | 62 / 0 ✅ |
| `test_access_status.py` | 18/18 | 18/18 ✅ |
| `test_offline_activation.py` | 32 / 0 | 32 / 0 ✅ |
| `test_first_run_onboarding.py` | 225 / 0 | 225 / 0 ✅ |
| `test_phase_ui3_1_layout.py` | 失败（`_stream_history`） | 同一点 ✅ |
| `test_ui_internal_diagnostics_gate.py` | 失败（同上） | 同一点 ✅ |
| `test_ui_tune_02_address_alignment.py` | 失败（`_recent_combo`） | 同一点 ✅ |
| `test_ui_tune_01_roi_entry.py` | 24 / 1 | 24 / 1 ✅ |
| `test_expiration_access_control.py` | 失败（`resolver`） | 同一点 ✅ |
| `test_roi_verification.py` | 失败 | 同一点 ✅ |
| `test_license_priority.py` | 失败（缺密钥） | 同一点 ✅ |
| `test_roi_persistence_gui.py` | 失败（`get_with_seq`） | 同一点 ✅ |

**新增测试**：
- `tests/ui_redesign/test_watermark.py` → **60 项全部通过**
- `tests/ui_redesign/test_toolbar_contract.py` → **72 项全部通过**（Phase 4，本次未破坏）

---

## 10. Phase 5 门禁判定

| 门禁条件 | 结果 |
|---|---|
| 运动逻辑全部通过 | ✅ §3（含角落双轴反转） |
| 授权状态来源明确且遵循既有契约 | ✅ §5（同一布尔量，二态契约，未确认按未激活） |
| UI 生命周期正确 | ✅ §E（隐藏即停、关闭后 0 活动 QTimer） |
| 业务隔离（直播 + 识别链路未受影响） | ✅ §6（帧字节零变化） |
| 边界与缩放正确 | ✅ §D（3000 步 + letterbox + 极小区域） |
| 自动化测试结果 | ✅ 60 项通过，零回归 |
| 实际运行截图 | ✅ 3 时刻 + 轨迹 + 放大裁剪 |
| 性能对比 | ✅ §7（246.7 µs/帧，0.75% 单核，隐藏零开销） |

### ✅ **Phase 5 门禁通过，允许进入 Phase 6。**

---

## 11. 遗留项

| # | 项 | 类型 | 处置 |
|---|---|---|---|
| R1 | 水印不透明度的最终视觉值（0.36 文字 alpha） | UNKNOWN | Phase 6 全局主题落地后一并复核 |
| R2 | 水印与遮罩视觉叠压的实际观感 | FACT（已截图） | 用户人工验收时确认 |
| R3 | 4000 步轨迹内角落碰撞为 0 | FACT | 角落逻辑由单元断言证明；非缺陷 |
| R4 | 整进程 CPU 采样噪声过大 | FACT | 已改用单帧精确计量 |
| R5 | 极小预览区（小于水印）时水印会溢出边界 | FACT | 数学上不可满足"完整在内"；当前为静止于原点的安全降级，不崩溃不抖动。实际窗口最小宽度 1100px，该场景不可达 |
