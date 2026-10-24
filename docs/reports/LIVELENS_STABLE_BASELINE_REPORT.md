# LiveLens 稳定版基线报告（OCR 冻结与验收准备）

本阶段**只做只读检查与必要测试**：未修改生产代码、未执行破坏性 Git 操作、未提交、未推送、未改授权文件。

---

## 1. 稳定版状态

| 项 | 值 |
|---|---|
| 分支 | `master` |
| HEAD | `3f16e9e chore(git): ignore local secrets and development artifacts` |
| 已跟踪文件改动 | 13 个（`gui/` 2、`ocr/` 6、`test_*.py` 4、`.gitignore` 1） |
| 未跟踪新增 | 31 项 |
| stash / rebase | 无 |
| 冻结结论 | **算法冻结**：本轮起不再修改识别/纠正/仲裁/时序逻辑 |

**关于已跟踪文件的改动**：这些是本轮 OCR 专项（Phase 2–4）之前与之中累积的未提交工作，
本次**未回退、未覆盖、未提交**。Phase 3 的 `length_escalation` 代码保留在文件中（默认关闭），
以便后续对照与回滚。

### 1.1 实验性能力是否进入正式识别链 —— 已逐项确认

| 能力 | 状态 | 证据 |
|---|---|---|
| 长度异常复看 `length_escalation` | **默认关闭** | `ocr/room_code.py:301`、`ocr/worker.py:101` 均为 `= False` |
| GUI 是否启用该开关 | **未启用** | `grep length_escalation gui/` → **0 处命中**；生产构造点未传该参数 |
| 首尾滑窗护栏（Phase 3 离线提出） | **未进入生产** | 生产代码中无 `is_slide` / 护栏符号；仅 docstring 提到"不引入" |
| 字形切分 / 字形图集 | **仅存在于离线实验脚本** | `_tools/phase4_*.py`，不在 `ocr/` 生产模块内 |
| 白名单受限识别 | **未进入生产** | 同上，仅在 Phase 2 离线探针中评估过 |
| 任何字符替换类新规则 | **无新增** | 纠正逻辑仍是 `ocr/correction.py` 的既有有限映射表 |

**结论：正式识别链中只有既有稳定算法，没有本轮实验产物。**

---

## 2. 冻结配置（稳定版关键参数）

| 参数 | 冻结值 | 位置 |
|---|---|---|
| 预处理缩放 `DEFAULT_SCALE` | **1.0**（恒等，不做灰度/二值化/降噪） | `ocr/room_code.py:67` |
| 可选缩放 `SUPPORTED_SCALES` | (1.0, 1.5, 2.0) | `ocr/room_code.py:66` |
| case-fold 复看倍率 `_ESCALATION_SCALE` | 2.0（仅 `case_fold_count > 0` 触发） | `ocr/room_code.py:74` |
| 长度异常复看倍率 `_LENGTH_ESCALATION_SCALE` | 1.5（**开关关闭，不生效**） | `ocr/room_code.py:85` |
| 时序裁决 | `TemporalResolver(min_agreement=2, window_size=3)` | `ocr/temporal.py:37-38` |
| 稳态识别间隔 `interval_ms` | **300 ms** | `ocr/worker.py:93` |
| 确认窗口采样间隔 `pending_interval_ms` | 100 ms（只改采样节奏，不改判定） | `ocr/worker.py:98` |
| 全 PSM 观测 `observe_all_psm` | False（只影响诊断） | `ocr/worker.py:100` |
| 游戏档 | `delta_force`(7位) / `delta_force_1v1`(8位) / `valorant`(6位) / `auto` | `ocr/profiles.py` |
| 显式选档约束 | `auto` 与 `None` 被 `_require_explicit_game()` 拒绝 | `ocr/worker.py:39-55` |
| 帧缓冲策略 | 单槽覆盖（宁可丢帧，不累计延迟） | `stream/frame_buffer.py` |

---

## 3. 调用链（稳定版）

```
ffmpeg_reader                    ← 分辨率探测、单槽最新帧、断流置错
   ↓  LatestFrameBuffer（覆盖式）
OCRWorker.run()                   ← 300ms 稳态 / 100ms 确认窗口
   ↓  _crop_roi(frame, roi)       ← 用原始帧像素，不用叠加层
RoomCodeRecognizer.recognize_frame()
   ↓  prepare()                   ← scale=1.0 → 恒等，不复制
   ↓  _decide()                   ← PSM 提前停止 + 逐位纠正 + 严格校验
   ↓  [case-fold 复看 2.0x]       ← 仅 case_fold_count > 0
   ↓  [长度异常复看 1.5x]         ← 默认关闭，不执行
   ↓  resolver.offer()            ← TemporalResolver(2,3)：连续 2 次一致才成立
copy_requested 信号                ← 仅 candidate.repeated == False 时发出
   ↓
MainWindow._on_copy_requested()
   ↓  ClipboardManager.copy()
   ├─ 成功 → copy_log 记账 + diagnostics.note_copy_success
   ├─ 失败 → diagnostics.note_copy_failure
   └─ 跳过 → diagnostics.note_copy_skipped
```

要点：
- 未通过**严格校验**或**时序确认**的结果不会到剪贴板（`copy_requested` 只在候选成立时发出）。
- 剪贴板失败**不会**写入 copy_log（记账只在成功分支）。
- 「选错游戏」冲突帧会被拦下、不进入时序裁决、也不触发剪贴板。

---

## 4. 测试结果

本阶段运行 **18 个测试脚本**（OCR / 时序 / Worker / 剪贴板 / 诊断）：

| 类别 | 脚本 | 结果 |
|---|---|---|
| 剪贴板 | `test_clipboard`、`test_copy_log_export` | **通过** |
| 时序裁决 | `test_phase_8_3_ocr_temporal_resolver` | **通过** |
| 读法仲裁 | `test_phase_8_4_ocr_reading_arbitration` | **通过** |
| 窗口残差 | `test_ocr_window_residue_safety` | **通过** |
| 自适应间隔 | `test_phase_ocr2_adaptive_interval` | **通过** |
| 格式感知 / 游戏链 / 选档 / 冲突提示 | `test_phase_8_0_*`、`test_phase_8_5_*`、`test_phase_8_6_*`、`test_phase_8_1_*` | **通过** |
| 1v1 档位 | `test_ocr_delta_1v1_profile` | **通过** |
| 诊断 | `test_ocr_diagnostics` | **通过** |
| 生命周期 / 图像 / 采样 | `test_phase_ocr31_lifecycle`、`test_ocr_image`、`test_ocr_sample_capture_and_bench` | **通过** |
| Phase 3 开关 | `test_phase_3_length_escalation` | **通过**（37 项） |
| **历史失败 1** | `test_ocr_worker` | **失败（文件本身语法错误）** |
| **历史失败 2** | `test_phase_ocr3_persistent_engine` | **失败（1 项信息性检查）** |

**汇总：18 个脚本 —— 16 通过 / 2 历史失败 / 0 新回归。**

两个失败均为**历史失败**，已逐项确认与本轮无关：

1. **`test_ocr_worker.py`** —— 文件第 149 行 `IndentationError: unexpected indent`，
   **文件本身语法就是坏的**（`ast.parse` 直接报错）。mtime `2026-09-14`，未被本轮改动过。
2. **`test_phase_ocr3_persistent_engine.py`** —— 唯一失败项是
   `case-fold 复看确实走进识别路径（与安全无关）`，测试自己标注为**信息性检查**；
   两个返回值为 `None`，属真实 OCR 在合成图上的表现差异。mtime `2026-09-24`，未被本轮改动过。

> 按要求，**没有为了让测试通过而修改任何正式算法**。

---

## 5. 真实直播验收：诊断能力盘点

### 5.1 已具备（可直接复用，无需新增代码）

| 需求 | 现状 | 位置 |
|---|---|---|
| 识别时间戳 | ✅ 诊断 CSV 的「时间」列（`HH:MM:SS.mmm`） | `ocr/diagnostics.py` |
| 候选码 | ✅ 「各读法候选」「仲裁结果」列 | 同上 |
| **实际剪贴板复制事件** | ✅ 独立事件账：序号 / 复制时间 / 房间码 | `ocr/copy_log.py` |
| OCR 耗时 | ✅ 「耗时ms」列（逐帧） | `ocr/diagnostics.py` |
| 连续确认过程 | ✅ 「连续确认」「确认窗口」列 | 同上 |
| 复制请求 / 结果 | ✅ 「复制请求」「复制结果」「备注」列，含失败原因 | 同上 |
| 异常（识别侧） | ⚠️ 部分：「备注」列记放大复看、提前停止、游戏冲突 | 同上 |
| 断流检测 | ⚠️ 有错误文本（"视频断流：FFmpeg已停止输出"） | `stream/ffmpeg_reader.py:488` |
| FPS / 分辨率 | ⚠️ 有日志与状态打印 | `stream/ffmpeg_reader.py` |

诊断 CSV 现有 13 列：序号、时间、游戏档、格式、耗时ms、PSM读法、各读法候选、仲裁结果、
连续确认、确认窗口、复制请求、复制结果、备注。
复制记录 CSV 3 列：序号、复制时间、房间码。

### 5.2 缺口（**只提出最小补充方案，本阶段不实现**）

| # | 缺失项 | 影响 | 最小补充方案 |
|---|---|---|---|
| G1 | **识别间隔未直接记录** | 诊断只有绝对时间戳，间隔需离线相减；无法直接看到"间隔是否被拖长" | 无需改代码：验收后用一列 `diff(时间)` 后处理即可。若需现场可见，再加一列「距上帧ms」 |
| G2 | **丢帧 / 画面卡顿无计数** | `LatestFrameBuffer` 是单槽覆盖式，丢帧是**静默**的；卡顿时无法区分"视频卡"还是"OCR 慢" | 在 `frame_buffer.update()` 覆盖旧帧时**加一个计数器**（`dropped`），并暴露只读属性。约 3 行，纯旁路 |
| G3 | **断流 / 重连无事件时间线** | 现在只有一行错误文本，没有"何时断、何时恢复、恢复耗时" | 在 `ffmpeg_reader` 的断流置错处与重连成功处各写一条带时间戳的事件；`stream/failover.py` 已明确"本阶段不做自动重连"，需先确认验收是否要求自动重连 |
| G4 | **OCR 侧异常未进诊断 CSV** | worker 级错误只走 `logger` + `error_occurred` 信号，`diagnostics.note_*` 没有 worker 错误入口；跑完一轮后拿不到自包含的证据文件 | 给 `DiagnosticsRecorder` 加一个 `note_worker_error(text)`，在 worker 的 `except` 分支调用。约 5 行 |
| G5 | **剪贴板失败原因** 已记录但未验证落盘 | `note_copy_failure` 存在且 GUI 已在失败分支调用，但**未在真实失败场景下验证过 CSV 落盘** | 验收前做一次人为失败演练（如锁定剪贴板），确认「复制结果」列写入 `失败:<原因>` |

**优先级**：G2（丢帧计数）与 G4（异常入 CSV）是验收证据链的关键缺口，建议优先；
G3 需先明确验收是否要求自动重连。

---

## 6. 遗留问题（不阻塞发布，但需知悉）

1. **OCR 准确率专项已结束，未采纳任何改动**。Phase 3 的长度异常复看与 Phase 4 的滑窗护栏
   **均被实测否决**（见 `PHASE3_/PHASE4_REPORT_FOR_CHATGPT.md`），代码以默认关闭状态保留。
2. **已知剩余识别失败（第四轮 100 码实测）**：5 个码 ——
   `HJKT6402`→HIKT6402、`QRTP8641`→ORTP8641（合法八位误读）；
   `UJMI8506`、`HNJM3086`、`MNBQ1769`（九位插入型漏识别）。
   根因是 J/Q 字形的切分问题，**当前无低风险修法**。
3. **`test_ocr_worker.py` 文件本身语法错误**，长期未被发现——说明该文件不在常规测试循环里。
   建议在验收前修复（**但本阶段按要求未动**）。
4. **`test_phase_ocr3_persistent_engine.py` 的 1 项失败**属真实 OCR 在合成图上的表现差异，
   不影响生产路径。
5. **本工作区有 13 个已跟踪文件未提交**。稳定版基线以当前工作区状态为准，
   但发布前需要一次明确的提交动作（**本阶段未提交**）。

---

## 7. 真实直播验收阻塞项

| # | 阻塞项 | 类型 | 说明 |
|---|---|---|---|
| B1 | 工作区未提交 | 流程 | 验收对象必须是一个确定的版本；当前 13 个已跟踪文件处于未提交状态 |
| B2 | G2 丢帧计数缺失 | 证据 | 无法量化"画面卡顿"，验收时无法区分视频侧与 OCR 侧问题 |
| B3 | G4 异常未入诊断 CSV | 证据 | 单轮跑完拿不到自包含证据文件，异常要另查应用日志 |
| B4 | G3 重连行为未定义 | 需求 | 需明确验收是否要求断流自动重连；`failover.py` 明确本阶段不做 |
| B5 | 无真实直播环境 | 环境 | 需要可复现的直播源（含断流、卡顿、真实码变化） |

**不阻塞**：G1（可用后处理解决）、G5（一次演练即可确认）。

---

## 8. 验收清单（真实直播）

**准备**
- [ ] B1 已提交，验收对象是有明确 commit 的版本（**目前未做**）
- [ ] 确认 GUI 中 `length_escalation` 无入口（本阶段已确认：0 处）
- [ ] 准备直播源：正常播放 / 中途断流 / 画面卡顿 / 房间码切换

**运行中记录（复用现有功能）**
- [ ] 开启诊断导出（`ocr/diagnostics.py`），保存 CSV
- [ ] 保存复制记录导出（`ocr/copy_log.py`），保存 CSV
- [ ] 保存应用日志（含 FFmpeg 断流与 FPS 状态）

**核对项**
- [ ] 每个码都能看到「时间 / PSM读法 / 候选 / 仲裁结果 / 连续确认 / 耗时ms」
- [ ] 复制记录与诊断中的「复制请求/复制结果」逐条对得上（成功才入账）
- [ ] 空结果不覆盖剪贴板；连续重复不重复触发
- [ ] 「选错游戏」冲突会提示且不复制
- [ ] 稳态间隔是否稳定在 ~300ms（用「时间」列后处理相减）
- [ ] OCR 耗时分布（均值 / p95 / 最大）与间隔无冲突
- [ ] 断流后行为符合预期（有提示、不误复制、可恢复）
- [ ] 长时间运行无内存增长 / 无卡死

**判定**
- [ ] 无新增崩溃、无错误码被复制、无剪贴板被空结果覆盖

---

## 9. 本阶段未做的事（明确声明）

未修改任何生产代码（`ocr/`、`gui/`、`stream/`、`utils/` 均只读）；
未启用 `length_escalation`；未采纳任何实验性纠错规则；
未回退/覆盖未提交工作；未执行 `reset --hard`/`checkout`/`clean` 等破坏性操作；
未提交、未推送；未修改授权相关文件；未实现 §5.2 的任何补充方案（按要求只提方案）。
