# PHASE3_RUNTIME_BASELINE — 隔离工作区运行环境与基线

> **阶段**：Phase 3
> **日期**：2026-10-14
> **工作区**：`D:\TIikTok_Ocr\_wt_uilg`（分支 `feature/ui-liquid-glass`，起始 HEAD `adfe074`）
> **前置**：Phase 2 门禁通过（见 `PHASE2_UI_AUDIT.md`）
> **证据标签**：FACT / INFERENCE / UNKNOWN

---

## 1. 任务 3.1 — 运行环境准备

### 1.1 缺失资源逐项核查（FACT）

新 worktree 只有 **507 个 tracked 文件**；以下被 `.gitignore` 排除的目录/文件**在 worktree 中缺失**：

| 资源 | `.gitignore` 依据 | 主工作区状态 | 对启动的必要性 | 处置 |
|---|---|---|---|---|
| `venv/` | `:10 venv/` | 存在 | **必需**（PySide6 运行时） | **直接调用主工作区 venv**，不建 junction |
| `utils/offline_activation.py` | `:89 /utils/offline_activation.py` | 存在（9,170 B） | **必需** —— `license_manager.py:46` **模块级导入**，缺失即 `ImportError`，程序无法启动 | **复制** |
| `license_public_key.pem` | `:22 license_public_key.pem` | 存在 | **必需** —— 验签公钥 | **复制** |
| `runtime/onnx/` | `:64 runtime/onnx/` | 存在（76.5 MB ONNX + charset.txt） | 识别后端需要 | **junction** |
| `runtime/ppocr/` | `:58 runtime/ppocr/` | 存在但**为空**（0 文件） | 未 provision | **junction**（保持行为一致） |
| `admin_web/` | `:105 admin_web/` | 存在 | **不需要**（vendored 管理端 Web UI，桌面程序不引用） | **不提供**，已记录 |
| `susi_helper/target/` | `target/` | **不存在**（未构建） | 影响本地验签 | **不提供**，记为环境阻塞（§3.2） |
| `license_server/*.pem` | `:20` | **不存在** | 仅测试需要 | **不提供**，记为环境阻塞（§3.2） |

**FACT — `runtime/ppocr/` 在主工作区中本身就是空目录。** 这台机器没有 provision PP-OCRv6 ppocr 权重。junction 后 worktree 行为与主工作区**完全一致**（都是空目录），因此不影响 UI 改造验证。

### 1.2 venv 处置决策（FACT + INFERENCE）

**未对 `venv/` 建 junction，改为直接调用主工作区的解释器。**

理由（FACT）：
- 主工作区 venv **没有** 对本项目的 editable 安装（`site-packages` 中只有 `distutils-precedence.pth`，无 `__editable__*` / 无指向项目根的 `.pth`）。
- `main.py:15` 执行 `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))`，**以脚本自身位置**决定导入根。
- 因此 `cd <worktree> && <主工作区>/venv/Scripts/python.exe main.py` 会导入 **worktree 的代码**，而非主工作区的。

**✅ 实测验证**：import smoke 输出 `theme OK` / `MainWindow import OK` / `public key resolved: True`，且程序真实启动成功（§2）。

**⚠️ 写入风险（FACT，已实测）**：直接用主工作区 venv 意味着**任何 `pip install` 都会写进主工作区**。本任务**不安装任何依赖**，风险为零；但必须在报告中显式声明。

### 1.3 junction 建立与验证（FACT）

```
D:\TIikTok_Ocr\_wt_uilg\runtime\onnx   →  D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\onnx
D:\TIikTok_Ocr\_wt_uilg\runtime\ppocr  →  D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ppocr
```

- 建立前确认目标路径不存在（未覆盖任何数据）。
- 建立后验证：`onnx` 解析出 76,555,019 B 的 `PP-OCRv6_medium_rec_fp32.onnx` + `charset.txt`；`ppocr` 解析为 0 文件（与源一致）。

**🔴 写入穿透实测（FACT —— 必须记录的风险）**：

执行 `touch <wt>/runtime/onnx/__junction_write_probe__` 后，**该文件出现在主工作区的 `runtime/onnx/` 中**。
→ **确认 junction 存在写入穿透**。探针文件已删除，主工作区未残留。

**缓解措施（本任务承诺）**：
1. **不对 `runtime/` 下任何路径执行写入操作。**
2. Phase 6 若需背景图片等运行时资源，**一律写入 worktree 自有目录**，不落 `runtime/`。
3. 任务书明确要求"不得把 junction 当成已实现物理隔离" —— 本报告确认：**junction 只提供只读共享，不提供隔离**。

### 1.4 环境依赖清单（FACT）

| 项 | 值 |
|---|---|
| Python | 3.11.9（主工作区 venv） |
| PySide6 | 6.11.2 |
| Qt | 6.11.2 |
| pytest | 9.1.1 |
| psutil | 7.2.2 |
| 解释器绝对路径 | `D:\TIikTok_Ocr\DouyinLowLatencyViewer\venv\Scripts\python.exe` |
| 启动方式 | `cd <worktree> && <venv-python> main.py` |

**未安装任何新依赖（FACT）。**

---

## 2. 任务 3.2 — 启动与基线验证

### 2.1 启动方式与结果（FACT）

新增可复用探针：`tests/ui_redesign/ui_baseline_probe.py`

```bash
cd D:\TIikTok_Ocr\_wt_uilg
D:\TIikTok_Ocr\DouyinLowLatencyViewer\venv\Scripts\python.exe \
  tests/ui_redesign/ui_baseline_probe.py \
  --screenshot docs/ui-redesign/evidence/screenshots/baseline_main_window.png \
  --json docs/ui-redesign/evidence/baseline_metrics.json \
  --settle 5 --cpu-window 3
```

**结果：`exit=0`，`ok: true`，`exited_cleanly: true`。程序真实启动并正常退出。**

> 探针**刻意不调用** `start_heartbeat()` / `start_update_check()` / `maybe_show_first_run_guide()`（这些由 `main.py` 调用），因此**不产生任何网络请求**，也不弹模态框。

### 2.2 基线性能数据（FACT）

| 指标 | 基线值 | 测量方法 |
|---|---|---|
| import 耗时 | **251.0 ms** | `perf_counter` 包裹导入段 |
| 主窗口构造耗时 | **1007.7 ms** | `perf_counter` 包裹 `MainWindow()` |
| 首帧绘制耗时 | **753.5 ms** | `show()` → 事件循环泵 50 次（每次 10ms 上限） |
| 许可状态落地 | **51.5 ms** | 轮询至状态可读 |
| 空闲 CPU 均值 | **1.43 %** | psutil `cpu_percent`，3.0s 窗口，30 采样点 |
| 空闲 CPU 峰值 | **14.3 %** | 同上（瞬时尖峰） |
| 内存 RSS（显示后） | **248.0 MB** | psutil `memory_info().rss` |
| 内存 RSS（空闲） | **248.1 MB** | 同上 |
| 窗口尺寸 | **1400 × 860** | `window.width()/height()` |
| QTimer 总数 / 活动 | **8 / 1** | `findChildren(QTimer)` |
| 干净退出 | **true** | `close()` → `app.exec()` 正常返回 |

> **采样说明**：`idle_cpu_samples` 完整 30 个采样点已存入 JSON。均值 1.43%、峰值 14.3% —— 峰值来自采样窗口内的偶发事件（日志/心跳定时器），非持续负载。
> **可比性**：改造后必须用**完全相同的命令与参数**重跑，方可对比。

### 2.3 许可状态（FACT）

```json
"license_state": "AccessState.EXPIRED",
"license_status_text": "激活失败",
"license_is_expired": true
```

**关键结论**：本机处于**未激活**状态（`is_expired() == True`）→ **正是水印需要显示的工况**，Phase 5 可直接在真实环境下验证"未激活显示"。

**INFERENCE**：`status_text` 是「激活失败」而非「未激活」，说明存在一次失败的激活尝试记录；但 `AccessState` 仍塌缩为 `EXPIRED`，与 Phase 2 审计的"二态契约"一致，**未产生新的状态分支**。

### 2.4 真实截图（FACT）

- 路径：`docs/ui-redesign/evidence/screenshots/baseline_main_window.png`（51,400 B，1400×860）
- 获取方式：`window.grab()` —— **真实运行窗口的像素**，非设计稿。

**截图揭示的现状（FACT，可直接对照任务书目标）**：

| 观察 | 与任务书目标的差距 |
|---|---|
| 标题栏右侧依次为：`?`（帮助）、钥匙（许可）、对话气泡（联系作者）、**齿轮（设置）**、四角括号（全屏）、`—` `□` `✕` | 图标风格不统一；5 个图标按钮**无可见文字** |
| **齿轮图标实际渲染为一个小而暗的星芒**，与相邻图标难以区分 | ✅ **直接证实任务书所述"设置按钮图标难以识别"** |
| 地址栏：URL 输入框 + 「历史」+「⋯」+「连接」 | 按钮分组不清晰 |
| 预览区被**未激活遮罩**占据：`LiveLens` / `请激活 LiveLens` / 「立即激活」「联系作者」/ 说明文字 | 水印需与遮罩共存（见 §4.2） |
| 右侧面板：OCR 控制 / 尚未识别 / 本次会话 / 导出 CSV / 游戏下拉 / 识别区域 / 开始 OCR | 主题为深色但**无玻璃层次** |
| 底部性能栏：未连接 / 分辨率 / FPS / 🔊 / 音量滑块 / 极速模式 / 音频 | 与任务书目标 C 的"简洁清晰"有差距 |

---

## 3. 任务 3.3 — 基线测试

### 3.1 执行结果（FACT）

12 个 UI/许可/ROI 相关测试，原始完整日志：`docs/ui-redesign/evidence/baseline_tests.txt`

| # | 测试 | 退出码 | 结果 | **归因** |
|---|---|---|---|---|
| 1 | `test_phase_ui3_activation_settings.py` | 0 | **通过 62 / 失败 0** | ✅ 基线通过 |
| 2 | `test_access_status.py` | 0 | **18/18 通过** | ✅ 基线通过 |
| 3 | `test_offline_activation.py` | 0 | **32 通过 / 0 失败** | ✅ 基线通过 |
| 4 | `test_first_run_onboarding.py` | 0 | **225 通过 / 0 失败** | ✅ 基线通过 |
| 5 | `test_phase_ui3_1_layout.py` | 1 | `AttributeError: 'MainWindow' object has no attribute '_stream_history'` | ⚠️ **改造前已存在** |
| 6 | `test_ui_internal_diagnostics_gate.py` | 1 | 同上（`_stream_history`） | ⚠️ **改造前已存在** |
| 7 | `test_ui_tune_02_address_alignment.py` | 1 | `AttributeError: ... '_recent_combo'` | ⚠️ **改造前已存在** |
| 8 | `test_ui_tune_01_roi_entry.py` | 1 | 通过 24 / **失败 1**：`[FAIL] 工具栏只剩直播状态/分辨率/FPS：['未连接','分辨率 --','FPS --','🔊','音频 --']` | ⚠️ **改造前已存在** |
| 9 | `test_expiration_access_control.py` | 1 | `TypeError: _StubConnectWorker.__init__() got an unexpected keyword argument 'resolver'` | ⚠️ **改造前已存在** |
| 10 | `test_roi_verification.py` | 1 | `TypeError: 'int' object is not callable`（`ocr/roi_selector.py:228`，测试 mock 传入 `int` 而非 QRect） | ⚠️ **改造前已存在** |
| 11 | `test_license_priority.py` | 1 | `FileNotFoundError: license_server\test_rsa_key.pem` | 🚧 **环境阻塞** |
| 12 | **`test_roi_persistence_gui.py`** | 1 | `AttributeError: '_StubBuffer' object has no attribute 'get_with_seq'` | 🔴 **由导入的在途工作引入** |

### 3.2 归因证据（FACT —— 逐项可复核）

**关键区分**：基线 commit `18476a6` 导入了主工作区的在途工作。因此必须区分「pure HEAD `f0418ea` 就失败」与「在途工作引入」。

**在途工作究竟改了什么生产代码（FACT）**：
```
git diff --stat f0418ea HEAD -- '*.py'
  gui/main_window.py         | 135 ++++++-
  stream/adapters/douyin.py  | 240 ++++++--
  ocr/roi_presets.py         | 184 +++（新增）
  test_roi_persistence_gui.py | 252 +++（新增）
  tests/roi_presets/test_roi_presets.py | 250 +++（新增）
```
**仅 `gui/main_window.py` 与 `stream/adapters/douyin.py` 两个既有文件被修改。**

| 失败项 | 证据 | 归因结论 |
|---|---|---|
| #5/#6 `_stream_history` | `git show f0418ea:gui/main_window.py \| grep -c _stream_history` = **0**；当前也是 **0**；`master`、`feature/release02-2.2.0`、`feature/b5-gui-integration` **全部为 0** | 该属性**从未在任何分支存在**。测试是为一个未合并的 API 写的 → **改造前已存在** |
| #7 `_recent_combo` | 同上，HEAD=0、当前=0、master=0、rel02=0 | 同上 → **改造前已存在** |
| #8 工具栏多出 `🔊`/`音频` | 在途工作**未触碰**音频面板（diff 中无 `audio_panel.py`）；`_build_video_toolbar` 中音量/模式控件是既有功能 | 测试期望的工具栏内容已过期 → **改造前已存在** |
| #9 `resolver` kwarg | `git show f0418ea:gui/main_window.py` 第 159 行即为 `def __init__(self, live_url, on_success, on_error, resolver=None)` | `resolver` **在 pure HEAD 就已存在** → **改造前已存在** |
| #10 `'int' object is not callable` | 失败于 `ocr/roi_selector.py:228`；`git diff --stat f0418ea HEAD -- ocr/` 只显示**新增** `ocr/roi_presets.py`，**未修改** `roi_selector.py` | 测试 mock 与生产签名不符 → **改造前已存在** |
| #11 `test_rsa_key.pem` | `git check-ignore -v` → `.gitignore:20: license_server/*.pem`；该文件**在磁盘上不存在**（主工作区也没有） | 机器未 provision 测试密钥 → **环境阻塞** |
| #12 `get_with_seq` | `git diff f0418ea HEAD -- gui/main_window.py` 含 `+            frame, seq = self._frame_buffer.get_with_seq()`；`git show f0418ea:...\| grep -c get_with_seq` = **0**，当前 = **1** | 在途工作把 `_frame_buffer.get()` 改为 `get_with_seq()`，而测试的 `_StubBuffer` 只实现 `get()` → **🔴 由导入的在途工作引入** |

### 3.3 基线测试结论

- **通过**：4 / 12
- **改造前已存在失败**：7 / 12（测试与被测代码的版本漂移，与本任务无关）
- **环境阻塞**：1 / 12（缺 gitignored 测试密钥）
- **由导入的在途工作引入**：1 / 12（`test_roi_persistence_gui.py`）

### 3.4 其他环境阻塞（FACT）

| 阻塞项 | 证据 | 影响 |
|---|---|---|
| `susi_helper/target/release/susi_helper.exe` 不存在 | 测试输出：`本地验签无法完成: susi_helper 不存在: ...\susi_helper\target\release\susi_helper.exe`；`susi_helper/target` 被 `.gitignore` 的 `target/` 规则排除，且**从未构建** | 依赖 Rust 构建产物的本地验签路径不可用。**不影响** `AccessStatus` 二态判定（已实测 `is_expired()==True`） |
| `runtime/ppocr/` 为空 | 主工作区与 worktree 均为 0 文件 | ppocr 后端不可用；ONNX 后端可用（模型在位） |

**未将任何既有失败伪装成本次引入（FACT）。未删除、未跳过任何测试（FACT）。**

---

## 4. 设计决策：水印与未激活遮罩的层级

### 4.1 现状（FACT）

截图与代码共同确认：未激活时 `_ActivationOverlay`（`main_window.py:971`，注册于 `:977`）**铺满预览区**，显示「请激活 LiveLens」+ 两个按钮。

`OverlayHost._overlays` 是**按注册顺序入栈**的列表（`widgets.py:195`），后注册者在**更上层**。

### 4.2 决策

**水印注册在 `_activation_overlay` 之前（即层级在其之下）。**

理由（INFERENCE，基于任务书约束）：
- 任务书 Phase 1 要求"水印不应遮挡关键识别区域、**交互控件**或状态信息"。
- 未激活遮罩上的「立即激活」是**关键交互控件**；水印若在其上层，会覆盖按钮文字、造成视觉干扰。
- 遮罩本身已经承担"请激活"的主信息职责；水印是**并存的防盗用标识**，位于其下仍可见（遮罩为半透明深色，非不透明）。

**待验证（UNKNOWN）**：水印在遮罩之下时的实际可见度 → Phase 5 截图复核；若过暗则调整为在遮罩之上但避让按钮区域。

---

## 5. Phase 3 门禁判定

| 门禁条件 | 结果 | 证据 |
|---|---|---|
| 隔离运行环境可复现 | ✅ 通过 | §1 全部命令与绝对路径已记录；探针可重复运行 |
| 环境依赖清单 | ✅ 通过 | §1.4 |
| 启动日志 / 真实截图 | ✅ 通过 | §2.4 真实截图 1400×860；`exited_cleanly: true` |
| 测试基线 | ✅ 通过 | §3 全部 12 项已归类并给出可复核证据 |
| 性能基线 | ✅ 通过 | §2.2 全部指标已测量 |
| 原有失败未被伪装 | ✅ 通过 | §3.2 逐项给出 `git show` / `grep` 证据 |
| 未为启动而更改授权语义或直播链路 | ✅ 通过 | 未修改任何生产代码（本阶段仅新增探针脚本与文档） |
| 未安装任何依赖 | ✅ 通过 | §1.4 |
| 主工作区未被写入 | ✅ 通过 | junction 探针已清理；未执行任何 pip 操作 |

### ✅ **Phase 3 门禁通过，允许进入 Phase 4。**

---

## 6. 遗留风险与 UNKNOWN

| # | 项 | 类型 | 处置 |
|---|---|---|---|
| R1 | junction **写入穿透**已实测确认 | FACT | 本任务承诺不写 `runtime/`；Phase 6/7 资源写入 worktree 自有目录 |
| R2 | 使用主工作区 venv → `pip install` 会污染主工作区 | FACT | 本任务不装任何依赖 |
| R3 | `susi_helper.exe` 未构建 → 本地验签路径不可用 | FACT（环境） | 不影响 `AccessState` 判定；记入最终报告 |
| R4 | `license_server/test_rsa_key.pem` 缺失 | FACT（环境） | 测试环境阻塞，记入最终报告 |
| R5 | `runtime/ppocr/` 为空 | FACT（环境） | ONNX 后端可用；ppocr 后端本就不可用 |
| R6 | 水印在未激活遮罩下的实际可见度 | UNKNOWN | Phase 5 截图复核 |
| R7 | 7 个测试为改造前已存在失败 | FACT | **不得**在 Phase 7 归因于本次改造；建议另立任务修复 |
| R8 | `test_roi_persistence_gui.py` 由在途工作引入失败 | FACT | 属**在途工作**的问题，非本 UI 任务引入；本任务**不修复**（避免越界改他人工作），记入最终报告 |

---

## 7. 本阶段产出文件

| 文件 | 说明 |
|---|---|
| `docs/ui-redesign/PHASE3_RUNTIME_BASELINE.md` | 本文件 |
| `tests/ui_redesign/ui_baseline_probe.py` | 可复用基线探针（Phase 7 用同一命令复测） |
| `docs/ui-redesign/evidence/baseline_metrics.json` | 基线性能 JSON |
| `docs/ui-redesign/evidence/baseline_tests.txt` | 12 个测试的完整原始日志 |
| `docs/ui-redesign/evidence/screenshots/baseline_main_window.png` | 真实运行截图 1400×860 |

**本阶段未修改任何生产代码（FACT）。**
