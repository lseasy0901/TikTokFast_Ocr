# LiveLens 正式程序诊断入口清理报告

基于 `243d513`（stable baseline `d376f6a` + 测试补齐 `34c94bc` + 诊断补强 `243d513`）。

**本阶段未修改任何代码，未提交。**

---

## 0. 结论：没有可删除的测试入口

按第一步的要求做了完整枚举（不是抽样），结论是：

> **正式 GUI 中不存在任何「仅用于开发测试 / 调试 / 一次性实验 / Phase 2·3·4 专用」的入口。**
> 因此**本阶段删除 0 个入口、修改 0 个文件。**

GUI 里与 OCR 相关的 4 个控件（开始诊断 / 导出诊断 CSV / 全 PSM 观测 / 采集 ROI 样本）
**恰好就是第三步要求「正式保留」的那四项能力**，而第五步又要求「全 PSM 观测入口仍可用」
「ROI 样本采集仍可用」——即这些**入口本身**要留。删掉它们会直接违反验收条件 4 与 5。

我没有为了"完成任务"而删除任何东西。下面是支撑这个结论的证据与逐项分类。

---

## 1. 第一步：完整入口枚举

### 1.1 枚举方法

- 静态扫描 `gui/` 全部 9 个模块，抓取 `QPushButton("…")` / `QCheckBox("…")` /
  `QRadioButton("…")` / `addAction(…)`；
- 实测构造 `MainWindow`，遍历真实控件树打印全部按钮与勾选框（不依赖代码阅读）；
- 额外扫描：键盘快捷键、`setVisible(False)`/`hide()` 隐藏控件、`QMenu`、`QComboBox`、
  `subprocess`/`Popen`/`QProcess`（是否有入口去拉起外部脚本）、CLI 参数。

### 1.2 实测控件树（构造出的 MainWindow，共 14 个）

```
QToolButton  ⋯                    历史菜单（编辑备注 / 清除历史）
QPushButton  连接
QPushButton  »  «                 面板折叠
QPushButton  导出 CSV             ← 复制记录导出（D）
QPushButton  开始诊断             ← 诊断开关（A）
QPushButton  导出诊断 CSV          ← 诊断导出（A）
QCheckBox    全 PSM 观测           ← 观测开关（B）
QCheckBox    采集 ROI 样本         ← 样本采集（C）
QPushButton  说明                  ROI 说明
QPushButton  选择识别区域
QPushButton  开始OCR
QPushButton  立即激活 / 联系作者
```

其余 GUI 模块（`activation_dialog`、`expired_dialog`、`onboarding`、`settings_dialog`、
`update_dialog`）的按钮全部属于授权、引导、更新等正式流程，无测试入口。

### 1.3 逐项分类

| 控件 | 分类 | 判定 |
|---|---|---|
| 连接 / 开始OCR / 选择识别区域 / 说明 | 正式产品功能 | 保留 |
| 立即激活 / 联系作者 / 使用引导 / 激活许可证 | 正式产品功能（授权/引导） | 保留 |
| ⋯ 历史菜单（编辑备注 / 清除历史） | 正式产品功能 | 保留 |
| « » 面板折叠 | 正式产品功能 | 保留 |
| **导出 CSV**（房间码记录） | **正式诊断能力 D** | **保留** |
| **开始诊断** | **正式诊断能力 A** | **保留** |
| **导出诊断 CSV** | **正式诊断能力 A** | **保留** |
| **全 PSM 观测** | **正式诊断能力 B** | **保留** |
| **采集 ROI 样本** | **正式诊断能力 C** | **保留** |

**「应该删除」的条目：0 个。**

### 1.4 为什么没有 Phase 2/3/4 专用入口

- **Phase 3 `length_escalation`**：从未接入 GUI —— `grep length_escalation gui/` = **0 处**。
  它只存在于 `ocr/` 的构造参数里，默认 `False`。
- **Phase 2 的滑窗护栏 / 字形切分 / 白名单识别**：从未进入生产代码，
  只在 `ocr_accuracy_round4_report/_tools/` 的离线脚本里。
- **Phase 4 的字形图集 / 样本集**：全是离线数据目录，GUI 不引用。
- `ui_prototype.py`、`capture_ui_layout.py` 等：**独立脚本，不是 GUI 入口**——
  已确认正式程序没有任何地方引用它们（无 import、无 subprocess）。
- **CLI 参数**：`main.py` 只处理 Velopack 安装器回调
  （`--veloapp-install` 等），没有 `--debug` / `--test` 之类开关。
- **隐藏入口**：无键盘快捷键（`QShortcut`/`keyPressEvent`/`QKeySequence` 全无命中），
  无隐藏调试面板。`.hide()` 共 6 个唯一目标，全部是状态/布局联动，与控制测试无关：
  `_activate_btn`（已激活时收起）、`_expand_btn`、`_scene_panel`、
  `_title_hot`（热区）、`_copy_feedback_label`、`_connection_status_label`。

---

## 2. 第二步：删除结果

**删除 0 个入口、0 个按钮、0 个菜单、0 个弹窗、0 条调用链。**

未删除清单（按要求明确保留）：底层测试脚本、诊断模块、sample capture、
全 PSM 观测、诊断 CSV、剪贴板复制日志、任何正常 OCR 功能 —— 全部原样保留。

---

## 3. 第三步：四项诊断能力（全部保留且已实测可用）

### A. 诊断 CSV —— 可用

- 入口：`开始诊断`（`_on_diag_toggled`）+ `导出诊断 CSV`（`_on_export_diagnostics_clicked`）
- 字段（**13 列，格式未动**）：序号、时间、游戏档、格式、耗时ms、PSM读法、各读法候选、
  仲裁结果、连续确认、确认窗口、复制请求、复制结果、备注
- 时间戳（`时间`）、候选（`各读法候选`/`仲裁结果`）、裁决（`仲裁结果`/`连续确认`）、
  耗时（`耗时ms`）、错误与备注（`备注`，含 `worker异常:` / `丢帧:` / `异常:` 前缀）
- 实测：`test_ocr_diagnostics` 91 项通过；导出为 UTF-8 + BOM 的 CSV

### B. 全 PSM 观测 —— 入口仍可用，默认不影响生产路径

- 入口：`全 PSM 观测` 勾选框（`_on_diag_observe_toggled`），**默认不勾选**
- 只有诊断开启时才可勾选；识别运行期间锁定；`observe_all_psm` 默认 `False`
- 补跑的 PSM 7/11 读法**只进诊断**，不进仲裁 / 游戏冲突判定 / 时序裁决 / 剪贴板
- 实测：`test_ocr_observe_all_psm` + `test_ocr_observe_all_psm_gui` 均通过

### C. ROI 样本采集 —— 入口仍可用

- 入口：`采集 ROI 样本` 勾选框（`_on_sample_capture_toggled`），**默认不勾选**
- 落盘到「文档/LiveLens-OCR样本」，有数量与磁盘上限，采满即停
- 采的是识别器真正看到的那一帧 ROI 像素；**只落盘，不参与判定**
- 实测：`test_ocr_sample_capture_and_bench` 56 项通过

### D. Clipboard copy log —— 可用

- 入口：`导出 CSV`（`_on_export_csv_clicked`）导出「本次会话成功复制的房间码」
- 只在**真的写进系统剪贴板**时入账（时序确认 + 写入成功双条件）
- 与诊断 CSV 的 `复制请求`/`复制结果` 列可逐条对照 → 满足「识别结果 ↔ 实际复制结果」对应
- 实测：`test_copy_log_export` 52 项通过

---

## 4. 第四步：死代码检查

由于没有删除任何入口，严格意义上不存在"删除后产生的死代码"。但仍按要求**主动检查**了
现有诊断入口的支撑代码是否已有死代码：

| 检查项 | 结果 |
|---|---|
| `_on_diag_toggled` | 定义 1 次 / 被引用 1 次（connect） |
| `_on_export_diagnostics_clicked` | 定义 1 次 / 被引用 1 次 |
| `_on_diag_observe_toggled` | 定义 1 次 / 被引用 1 次 |
| `_on_sample_capture_toggled` | 定义 1 次 / 被引用 1 次 |
| `_on_export_csv_clicked` | 定义 1 次 / 被引用 1 次 |
| `_update_diag_controls` | 定义 1 次 / 被引用 5 次 |

**结论：没有只被测试入口使用的 UI 代码，也没有孤儿 handler。**

**按要求没有因为"看起来像旧代码"而删除任何东西。**

---

## 5. 第五步：验证结果

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 主程序正常启动 | 实测构造 `MainWindow`（真实控件树遍历） | ✅ 构造成功 |
| 2 | 正常 OCR 路径不变 | 本阶段未改任何代码；相关测试全通过 | ✅ |
| 3 | 诊断 CSV 可导出 | `test_ocr_diagnostics` 91 项 | ✅ |
| 4 | 全 PSM 观测入口仍可用 | `test_ocr_observe_all_psm(_gui)` | ✅ |
| 5 | ROI 样本采集仍可用 | `test_ocr_sample_capture_and_bench` 56 项 | ✅ |
| 6 | Clipboard log 仍可用 | `test_copy_log_export` 52 项 | ✅ |
| 7 | GUI 不再显示旧测试入口 | 实测控件树 14 项，**无任何测试入口**（本就不存在） | ✅ |
| 8 | Phase 3 `length_escalation` 仍关闭 | `room_code.py:301` / `worker.py:101` 均 `= False`；GUI 中 0 处 | ✅ |
| 9 | 未修改授权相关文件 | `git status` 对 `utils/`、`gui/activation_dialog.py`、`gui/expired_dialog.py`、`license_server/` 无改动 | ✅ |

**测试汇总**：

```
test_ocr_observe_all_psm_gui            exit=0
test_ocr_diagnostics                    exit=0
test_copy_log_export                    exit=0
test_ocr_sample_capture_and_bench       exit=0
test_ocr_observe_all_psm                exit=0
```

### 5.1 启动时观察到的一条环境告警（非阻塞）

构造 `MainWindow` 时输出：

```
无法加载业务时区 Asia/Shanghai（原因：'No time zone found with key Asia/Shanghai'）。
心跳将只上报启动那一次，不再做周期补发；请在运行环境安装 tzdata。
```

这是本机 Python 环境缺 `tzdata` 导致的，属**预存在环境问题**，与本次清理无关，
不阻塞 GUI 启动。仅记录，未处理（不在本阶段范围）。

---

## 6. 修改文件清单

**无。** 本阶段没有修改任何文件，没有新增文件（除本报告），没有删除任何文件。

```
$ git status --short | grep -v '^??'
（空）
```

---

## 7. 给后续阶段的建议

1. **验收前不需要再做 GUI 清理** —— 已确认正式界面只有产品功能 + 四项诊断入口。
2. 如果你原本想删的是**某几个具体控件**，请指出控件名或截图位置；
   从当前代码看，能被误认为"测试入口"的只有 `全 PSM 观测` 与 `采集 ROI 样本`
   两个勾选框——而它们正是验收清单第 4、5 条要求保留的**入口**。
   若确实希望**去掉勾选框但保留底层能力**（改成配置文件或固定关闭），
   那是产品决策而非清理，请明确后我再改。
3. 本机建议安装 `tzdata`，以恢复心跳的周期补发能力（与 OCR 无关）。

---

## 8. 约束遵守

未修改 OCR 算法、未调整 PSM、未改 `TemporalResolver`、未改 300ms 间隔、
未开启 Phase 3、未做自动重连、未删除实验报告与实验数据、未提交、未推送。
