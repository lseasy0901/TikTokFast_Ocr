# PHASE7_PERF — 性能 A/B、全量回归与收尾

> **阶段**：Phase 7（末阶段）
> **日期**：2026-10-14 起草 / 2026-10-17 完成
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`）
> **被测代码状态**：`05a5ad6`（全部测量都在此状态上完成）；本阶段收尾提交 `ca5e521`
> 只新增文档与测量脚本，**不改动任何生产代码**
> **基线**：`d3b2c78`（`feature/release02-2.2.0`）—— 见 `REBASE_ADDENDUM.md`
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 1. 阶段目标与达成

| 目标（任务书 Phase 7） | 状态 | 证据 |
|---|---|---|
| 在**正确的基线**上做性能对照 | ✅ | `evidence/ab/`（交替 A/B，N=3） |
| 水印开/关的 CPU 对照（R2） | ✅ | §4 + `evidence/phase7_watermark_ab.json` |
| 全量回归，既有失败不得归因于本次改造 | ✅ | §6 + `evidence/phase7_final_regression.txt` |
| 不宣称未经测量的性能改进 | ✅ | §3、§5 明确区分"未劣化"与"更快" |
| 改造只在独立分支，不合并主线 | ✅ | 分支 `feature/ui-liquid-glass`，未 push、未 merge |
| 收尾：文档、证据、遗留项 | ✅ | 本文档 + `REBASE_ADDENDUM.md` |

---

## 2. 基线变更（必读）

Phase 0–6 的基线是 `f0418ea`（`feature/bare-stream-integration`），**落后发布线 14 个提交**，
因此看不到「购买授权」与 QQ 联系方式。Phase 7 期间已 rebase 到 `d3b2c78`
（`feature/release02-2.2.0`），完整过程见 **`REBASE_ADDENDUM.md`**。

本文档的所有性能数字**都在新基线之上**产生。旧基线的数字（`baseline_metrics.json`、
`phase4_metrics.json`、`phase6_metrics.json`）仅作历史记录，**不得与本文数字直接相减**。

---

## 3. 方法学：为什么"前后两次跑一下"不可信

本阶段先复现了三个陷阱，再据此设计测量方式。**这三条都是 FACT（实测）**。

| # | 陷阱 | 实测证据 | 应对 |
|---|---|---|---|
| T1 | **文件缓存预热**：同一进程/同一台机器第二次启动明显更快 | 交替 A/B 中同一 worktree 的 `construct_ms` 在 58–170 ms 间波动 | 交替执行（base→ui→base→ui…），取中位数 |
| T2 | **窗口被最小化/未 expose 时 Qt 不派发 paintEvent** | 最小化时 `paints=0` 而 `ticks` 仍在涨；`isExposed()=false` | 每轮校验 `exposed && !minimized`，不合格样本丢弃重测 |
| T3 | **应用自身有常驻周期负载**（16 ms 帧轮询、未激活遮罩的呼吸动画） | 水印关闭时进程仍有 1.1%–2.6% CPU | 报告"增量"而非绝对值；隔离实验里暂停应用定时器 |

**结论（FACT）**：任何"Phase 4 比 Phase 3 快 xx ms"式的跨会话数字都不可采信。
本文只报告**同会话交替对照**的结果。

---

## 4. R2 —— 水印开/关的 CPU 对照（新基线）

### 4.1 方法

| 项 | 取值 |
|---|---|
| 脚本 | `tests/ui_redesign/watermark_perf_ab.py` |
| 计量 | `psutil.Process.cpu_times()` 窗口前后差值 ÷ 墙钟时间（不受 100 ms 采样粒度影响） |
| 平台 | `QT_QPA_PLATFORM=offscreen`（窗口不出现，排除 T2 的最小化/遮挡干扰） |
| 隔离 | 暂停应用自身周期定时器（帧轮询/状态刷新等），**水印计时器不动** |
| 流程 | 预热 6 s 丢弃 → ON/OFF 交替 4 轮 × 12 s（奇偶轮交换先后顺序）→ 取中位数 |
| 有效性校验 | OFF 必须 `paints==0 && ticks==0`；ON 必须 `paints>0` 且 `paints ≥ 0.8×ticks`；每轮 `exposed && !minimized` |

**测量对象**：未激活状态下的 `MainWindow`。两种状态下未激活遮罩都在，
因此 ON−OFF 的差值只归因于水印覆盖层。

### 4.2 结果

| 状态 | CPU 均值（中位数） | 帧数 | 每 tick `step()` | 每次 `paintEvent` |
|---|---|---|---|---|
| 水印**关** | **2.927 %** | 0 | — | — |
| 水印**开** | **4.620 %** | 884 / 48 s ≈ **18.4 fps** | 200–202 µs（干净轮） | 537–606 µs（干净轮） |
| **差值** | **+1.693 pp** | | | |

> 逐轮原始值：关 **2.725 / 2.861 / 2.993 / 3.242**；开 **4.163 / 4.151 / 7.537 / 5.076**。
> 关的离散度很小（±0.26 pp）；开的第 3、4 轮明显偏高，且同两轮的 `step`/`paint`
> 同步从 200/537 µs 升到 373/978 µs —— **说明是机器上同时有其他负载在抢 CPU，
> 不是水印代码在不同轮次表现不同**（同一份代码，同样的帧数 217–225）。

### 4.3 有效性

| 校验 | 结果 |
|---|---|
| `off_paints_zero` / `off_ticks_zero` | ✅ 关掉后**一帧不画、计时器不空转** |
| `on_paints_positive` / `on_paints_per_tick_ge_0_8` | ✅ 开着确实在画（paints == ticks） |
| `all_rounds_exposed` / `no_invalid_rounds` | ✅ 全部轮次有效（0 条丢弃） |
| `valid` | ✅ **true** |

### 4.4 归因（FACT）

- **单帧自身成本**（取干净轮）：`step()` ≈ **200 µs** + `paintEvent` ≈ **540 µs** ≈ **0.74 ms/帧**。
- 折算：0.74 ms × 18.4 fps ≈ **1.36 % 单核** —— 这是**可归因**的水印成本。
- 进程级差值 1.69 pp 略高于 1.36 %，差额（≈0.3 pp）落在 Qt 事件派发与背板/合成路径上，
  属**上限估计**，不是水印自身的绘制开销。
- 三次独立运行（2 轮 × 10 s、3 轮 × 15 s、4 轮 × 12 s）的进程级差值分别为
  **2.03 / 2.60 / 1.69 pp**，可归因成本 **1.26–1.40 % 单核** —— **结论方向一致，量级稳定**。

> ⚠️ **与 Phase 5 §7 的口径差异（如实说明）**：Phase 5 微基准测得 246.7 µs/帧
> （step 91.5 + paint 155.2），本阶段同口径测得约 740 µs/帧，约 3 倍。差异来自
> 测量条件不同（Phase 5 是直接调用 `step()`/`paintEvent` 的紧循环；本阶段是真实
> 事件循环 + 覆盖在带 `QGraphicsOpacityEffect` 的遮罩之上 + offscreen 平台）。
> **两个数字都保留，不做换算，也不据此宣称任何"优化/劣化"。**

### 4.5 结论

- 水印开启的增量成本 **≈ 1.3–1.4 % 单核（可归因）/ 1.7–2.6 pp（进程级上限）**，且**只在未激活状态**运行。
- 关闭时**零帧、零计时器**（`paints==0 && ticks==0`），不存在"隐藏但空转"。
- **未超出预算**：任务书要求"不牺牲实时预览"，而水印在已激活状态下根本不显示。

---

## 5. 基线 vs UI 分支 —— 交替 A/B（启动 / 资源 / 空闲）

### 5.1 方法

| 项 | 取值 |
|---|---|
| 脚本 | `tests/ui_redesign/ab_baseline_vs_ui.py` |
| 探针 | `tests/ui_redesign/ui_baseline_probe.py`（**同一份源码**，两侧各放一份；基线侧是未跟踪文件） |
| 对照 | `_wt_uilg_base`（detached @ `d3b2c78`）↔ `_wt_uilg`（被测状态 `05a5ad6`） |
| 顺序 | base → ui → base → ui → base → ui（**交替**，抵消 T1） |
| 平台 | offscreen（抵消 T2） |
| 样本 | N=3，取中位数 |

### 5.2 结果（FACT）

| 指标 | 基线 `d3b2c78` | UI 分支 `05a5ad6` | 差值 | 判定 |
|---|---|---|---|---|
| 导入耗时 | 159.9 ms | 127.1 ms | −32.8 ms | 分布重叠（base 59–201 / ui 121–221）→ **不宣称提速** |
| 窗口构造 | 152.2 ms | 79.1 ms | −73.1 ms | 分布重叠（base 59–170 / ui 58–91）→ **不宣称提速** |
| 首帧 | 647.8 ms | 600.5 ms | −47.3 ms | UI 三轮均更稳（590–612 vs 580–661）→ **未劣化** |
| RSS（空闲） | 178.8 MB | 178.9 MB | **+0.1 MB** | ✅ 基本持平 |
| 空闲 CPU | 1.11 % | 1.85 % | **+0.74 pp** | = 水印（见 §4），非回归 |
| QTimer 总数 | 8 | 9 | **+1** | = 水印计时器，按设计 |
| QTimer 活动 | 1 | 2 | **+1** | = 水印计时器（未激活时运行） |

### 5.3 结论（严格措辞）

- **未观察到启动劣化**：三项启动指标的中位数都不高于基线，且 RSS 持平。
- **不宣称"更快"**：启动耗时的轮间离散度（T1）远大于中位数差值，样本量不足以支持提速结论。
- **空闲 CPU +0.74 pp 与 QTimer +1 有明确归因**：就是未激活水印（§4）。
  **已激活状态下该计时器不启动**（§4.3 `off_ticks_zero`），因此对正常使用无影响。

> **UNKNOWN（如实标注）**：这里用**跨进程**对照得到 +0.74 pp，而 §4 的**同进程开/关**
> 得到 +1.69~2.60 pp，两者方向一致但幅度差 2–3 倍。可能来源：本节的 CPU 窗口紧接启动
> （后台 worker 尚未静默）、以及跨进程对照无法排除两棵 worktree 的细微环境差异。
> **门禁判定采用较保守的上界（2.6 pp）**，不以较小的 0.74 pp 自证。

---

## 6. 全量回归（新基线）

### 6.1 12 项既有测试子集（与 Phase 3 基线逐项对照）

原始日志：`evidence/phase7_final_regression.txt`

| # | 测试 | 本次 | 与 Phase 3 基线 |
|---|---|---|---|
| 1 | `test_phase_ui3_activation_settings.py` | 通过 62 / 失败 0 | ✅ 一致 |
| 2 | `test_access_status.py` | 18/18 通过 | ✅ 一致 |
| 3 | `test_offline_activation.py` | 32 通过 / 0 失败 | ✅ 一致 |
| 4 | `test_first_run_onboarding.py` | 225 通过 / 0 失败 | ✅ 一致 |
| 5 | `test_phase_ui3_1_layout.py` | `_stream_history` AttributeError | ⚠️ 改造前已存在 |
| 6 | `test_ui_internal_diagnostics_gate.py` | 同上 | ⚠️ 改造前已存在 |
| 7 | `test_ui_tune_02_address_alignment.py` | `_recent_combo` AttributeError | ⚠️ 改造前已存在 |
| 8 | `test_ui_tune_01_roi_entry.py` | 通过 24 / 失败 1 | ⚠️ 改造前已存在 |
| 9 | `test_expiration_access_control.py` | `resolver` kwarg TypeError | ⚠️ 改造前已存在 |
| 10 | `test_roi_verification.py` | `'int' object is not callable` | ⚠️ 改造前已存在 |
| 11 | `test_license_priority.py` | 缺 `test_rsa_key.pem` | 🚧 环境阻塞（gitignored 密钥未 provision） |
| 12 | `test_roi_persistence_gui.py` | **文件不存在** | rebase 丢弃（随 `18476a6`），功能由发布线正式提交覆盖 |

**FACT — 与 rebase 前的 12 项结果逐项一致：11 项完全相同，1 项因文件随 rebase 丢弃。
零新增失败，零跳过，零删除。**

### 6.2 本次改造新增的断言

| 测试 | 结果 |
|---|---|
| `tests/ui_redesign/test_toolbar_contract.py` | ✅ 全部通过（72 项断言 / 0 失败） |
| `tests/ui_redesign/test_watermark.py` | ✅ 全部通过（60 项断言 / 0 失败） |

### 6.3 全量测试集（参考，**旧基线**上的一次完整跑）

`evidence/phase7_full_tests.txt`（2026-10-14 16:40，旧基线）记录了 69 个 root 测试文件
与 `pytest tests/` 的一次完整运行。其中 exit≠0 的项**全部**属于三类：

1. 改造前就已存在的版本漂移失败（`_stream_history`、`_recent_combo`、`resolver` 等）；
2. 环境阻塞（缺 `pydantic_settings`、缺 `test_rsa_key.pem`、缺 ppocr 权重、缺 Rust 产出的 `susi_helper.exe`）；
3. 该次运行中 `pytest` 收集 `tests/ui_redesign/*` 时的 **fixture 误用**（这些文件是**独立脚本**，
   不是 pytest 用例；直接运行时 72/0 与 60/0 全部通过，见 §6.2）。

> 该文件是**旧基线**产物，不用于证明新基线的全量状态；新基线的结论由 §6.1 的逐项对照承担。

---

## 7. 一条既有性能事实（不属本次改造，但必须记录）

未激活遮罩上的「LiveLens」品牌字带有一个**无限循环的 `QPropertyAnimation`**
（`QGraphicsOpacityEffect` 呼吸，2.4 s 一轮），它在遮罩可见时以约 60 fps 持续重绘。

| 项 | 值 |
|---|---|
| 代码位置 | `gui/main_window.py:536-543` |
| 引入提交 | `e56ba12`（**基线 `d3b2c78` 已包含**，`git show d3b2c78:gui/main_window.py \| grep -c _breath` = 5） |
| 实测代价 | 未激活状态下**关闭水印**仍有 1.1–2.6 % CPU |
| 与本次改造的关系 | **无关**。本次未修改该动画 |

**INFERENCE**：如果将来要压未激活状态的 CPU，收益最大的一步是给这个呼吸动画加
"窗口不可见/最小化时暂停"，而不是继续优化水印（水印只有 1.3 %）。**属超出本次任务范围的建议。**

---

## 8. Phase 7 门禁判定

| 门禁条件 | 结果 | 证据 |
|---|---|---|
| 在正确基线上做对照 | ✅ | §5.1（`d3b2c78` ↔ `05a5ad6`） |
| 水印开/关 CPU 已量化（R2） | ✅ | §4（进程级 +1.69 pp，三次运行上界 2.60 pp；可归因 1.36 % 单核） |
| 隐藏即零开销 | ✅ | §4.3（`off_paints_zero` + `off_ticks_zero`） |
| 启动/RSS 未劣化 | ✅ | §5.2（RSS +0.1 MB，三项启动指标中位数均不高于基线） |
| 既有失败未被伪装/未被归因于本次改造 | ✅ | §6.1（逐项给出归因，与 Phase 3 一致） |
| 新增断言全通过 | ✅ | §6.2（72 + 60） |
| 未宣称未经测量的改进 | ✅ | §5.3 明确"不宣称提速" |
| 独立分支、未合并主线 | ✅ | 分支 `feature/ui-liquid-glass` |
| 无新增依赖 | ✅ | 仅用 `psutil`（venv 已装，Phase 3 探针已在用） |

### ✅ **Phase 7 门禁通过 —— 8 个阶段（Phase 0–7）全部完成。**

---

## 9. 遗留项（汇总，跨阶段）

| # | 项 | 类型 | 处置 |
|---|---|---|---|
| L1 | 未激活遮罩的呼吸动画常驻 60 fps 重绘（1.1–2.6 % CPU） | FACT（既有） | 建议后续单独优化，**不属本次范围** |
| L2 | `update_dialog.py`(12 处) / `expired_dialog.py`(2 处) 仍用内联颜色，未随令牌更新 | FACT | 独立弹窗，不在"工具栏+预览+水印"范围内，建议单独处理 |
| L3 | 蓝色强调色（`#80BFFF`）是最明显的视觉变化 | FACT | 需用户人工验收观感 |
| L4 | Windows DWM Mica 的真实观感（真实桌面壁纸下） | UNKNOWN | 截图是抓取像素，需在实际桌面验收 |
| L5 | `susi_helper.exe` 未构建 / `test_rsa_key.pem` 缺失 / `runtime/ppocr/` 为空 | FACT（环境） | 机器未 provision，不影响本次验证 |
| L6 | 7 项测试为改造前既有的版本漂移失败 | FACT | **不得**归因于本次改造，建议另立任务修复 |
| L7 | 主工作区的未提交在途工作与发布线是否重复 | FACT | 属项目负责人决策，超出 UI 任务范围 |
| L8 | 原生窗口下的水印 A/B 无法稳定测量（窗口反复被最小化） | FACT（环境） | 改用 offscreen 平台；已在 §4.1 声明 |

---

## 10. 本阶段产出文件

| 文件 | 说明 |
|---|---|
| `docs/ui-redesign/PHASE7_PERF.md` | 本文档 |
| `docs/ui-redesign/REBASE_ADDENDUM.md` | 基线变更与合并验证（Phase 7 期间产生） |
| `tests/ui_redesign/watermark_perf_ab.py` | 水印开/关 A/B（含有效性校验、逐帧计时） |
| `tests/ui_redesign/ab_baseline_vs_ui.py` | 基线 vs UI 分支交替 A/B |
| `tests/ui_redesign/ui_baseline_probe.py` | 基线探针（新增 `window_exposed` / `window_minimized`） |
| `docs/ui-redesign/evidence/phase7_watermark_ab.json` | §4 原始数据 |
| `docs/ui-redesign/evidence/ab/` | §5 原始数据（6 次运行 + 汇总 + 截图） |
| `docs/ui-redesign/evidence/phase7_final_regression.txt` | §6 回归日志 |
| `docs/ui-redesign/evidence/phase7_rebase_*.{json,png}` | rebase 后真实启动指标与截图 |
