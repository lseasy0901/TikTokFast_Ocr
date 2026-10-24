# RELEASE_AUDIT_REPORT.md —— v0.1.0 发布前审计

日期：2026-09-30
范围：打包完整性 / Runtime 资源 / 路径兼容 / 数据文件 / 依赖 / 安全（只检查，未改任何功能代码）

---

# 必须修复

## 编号：MUST-1
**问题**：`scripts/verify_frozen_bundle.py` 的期望文件集**不包含本版新增的音频产物**。
`expected_payload()` 只推导 ONNX/FFmpeg/公钥/config/susi_helper，`verify_bundle()` 只断言
onnxruntime 的 capi DLL——本版新增的 `PySide6.QtMultimedia` hiddenimport、
`plugins/multimedia/`（windowsmediaplugin.dll、ffmpegmediaplugin.dll）、
`Qt6Multimedia*.dll`、`Qt6Network.dll` 都不在"必须存在"也不在"禁止出现"清单里。

**影响**：音频是本版的核心新功能。PyInstaller 对 hook/hiddenimport 的失败只警告不报错，
一旦某天环境变化导致 multimedia 插件没进包，`build_package.py → verify_bundle()` 照样
全绿放行，用户拿到的是"没有声音"的正式包——这正是该脚本文档里写明要防的事故类别
（"有没有真的进包"）。

**建议**：在 `expected_payload()` 增加 §音频：从构建环境 PySide6 目录推导
`plugins/multimedia/*.dll` 与 `Qt6Multimedia*.dll`、`PySide6/QtMultimedia*.pyd|dll`，
加入期望集（与 ONNX/FFmpeg 同等核对存在性+字节数）。约 20 行改动，不动打包逻辑。

## 编号：MUST-2
**问题**：音频功能从未在冻结环境验证过（开发期只验证了源码运行 + 真实声卡）。
QtMultimedia 在冻结包里依赖的插件加载路径（`PySide6/plugins/multimedia` 的相对布局）
与源码环境不同，`QMediaDevices.defaultAudioOutput()` 在无 Qt 插件路径配置的 frozen
环境下行为需实测确认。

**影响**：若插件加载失败，`audio/output.py` 会以 `AudioOutputError("QtMultimedia 不可用")`
呈现为"音频 不可用"状态——不崩溃但核心新功能整体不可用。

**建议**：作为打包后的**强制冒烟项**（非代码修改）：跑 `build_package.py` →
`verify_frozen_bundle` → 在干净机器/干净账号运行 frozen EXE，连接直播间确认：
① 有声音 ② 历史弹窗可开关 ③ 备注询问出现。三项通过才算发布就绪。

---

# 可以接受

## 编号：OK-1
**问题**：tesseract.exe / tessdata 未打包（用户审计清单中列出的项）。
**原因**：**设计如此，非缺失**。PP-OCRv6_medium_rec（ONNX Runtime + DirectML）迁移后
Tesseract 资产已从仓库删除（spec 头部注释明确记录）；pytesseract 保留仅为
`DLV_OCR_BACKEND=legacy` 诊断路径，frozen 包内本就没有可解析的 runtime。
生产 OCR 走 `runtime/onnx/PP-OCRv6_medium_rec_fp32.onnx + charset.txt`，两者均有
spec 构建闸门 + SHA-256 manifest 校验。

## 编号：OK-2
**问题**：仓库内存在开发私钥 `license_server/test_rsa_key.pem` 与 `license_server/.env`。
**原因**：均已 gitignore（实测 `git check-ignore` 通过），且**不会进包**——Analysis 入口
只有 `main.py`，datas 无任何 license_server 引用；`.env.example` 注明 production 必须
在仓库外自备密钥。公钥 `license_public_key.pem` 进包属设计（离线验签），spec 有缺失
即中止的闸门。

## 编号：OK-3
**问题**：仓库根存在开发日志 `debug_gui.log`、`ocr_e2e_10min.log`。
**原因**：`*.log` 已 gitignore，且不在 datas——不会进包、不会随更新分发。

## 编号：OK-4
**问题**：`upx=True` 对 Qt6 大 DLL 有理论上的损坏/误报风险。
**原因**：这是既有发货配置（此前多个版本同配置已验证），本版不应在发布前改变；
如需调整应另立任务。

## 编号：OK-5
**问题**：requirements.txt 未钉 numpy/PySide6/requests 等版本。
**原因**：构建环境由 `requirements-packaging.txt` 约束（onnxruntime-directml==1.24.4、
velopack==1.2.0、opencv==4.10.0.84 钉死），venv 实测与之一致
（Python 3.11.9 / PySide6 6.11.2 / PyInstaller 6.22.2 / hooks 2026.7）。
建议后续把 packaging 清单作为唯一事实来源并补 CI 校验，不阻塞本次发布。

## 编号：OK-6
**问题**：`stream_history.json` 卸载残留。
**原因**：写入 `%LOCALAPPDATA%/LiveLens/`（用户数据目录），不进安装目录、无需管理员、
首次保存时自动建目录、文件损坏有容错（回到空历史）；卸载不清除属常见行为且不会导致
异常——新代码读坏文件走 `_load()` 容错分支。

---

# 审计通过项（无问题）

| 审计项 | 结论 |
|---|---|
| spec 构建闸门 | `require_build_inputs` 对公钥/susi_helper/ffmpeg/ffprobe/其全部伴生 DLL/onnx 模型+charset 逐一存在性校验，缺一即中止构建；pytesseract+PIL.Image 可导入性校验；ffmpeg/ffprobe DLL 数量闸门 |
| hidden-import | QtMultimedia（本版新增）、onnxruntime、pytesseract、PIL.Image、yaml、requests、psutil 等齐备；stream/audio/gui/ocr 全部经 `main.py → gui.main_window` 的 import 图自动收集（含新增 `stream/history.py`、`gui/history_panel.py`、`audio/*`，均为模块级导入，无漏包风险）|
| ffmpeg/ffprobe | 双构建（ffmpeg ACLOS build + ffprobe shared n5.1.3）各带完整 DLL 闭包，dest 路径与 `runtime_paths` 探测契约逐字对齐 |
| OCR 模型 | runtime/onnx 两产物齐备；`scripts/onnx_model_manifest.json` SHA-256 三处一致（spec 闸门/verify 脚本/verify_onnx_model）|
| config.yaml / 公钥 | 均在 datas，dest 为包根，与运行时探测路径一致 |
| `_MEIPASS`/`__file__` | `runtime_paths`（frozen 分支 + 源码分支）、`onnx_engine.is_frozen()`、`susi_verifier.resolve_public_key` 三处全部按 frozen/源码双路径实现；`__file__` 无写入用途；`os.getcwd` 仅出现在开发工具 `ocr/dataset_capture.py`（Documents 优先，良性）|
| 用户目录写入 | 历史/许可/更新状态均在用户数据目录，无安装目录写入、无管理员要求 |
| 安全 | 客户端包内无私钥/无 .env/无日志；开发密钥 gitignored 且不进包；docs 中的密钥字样为占位符片段 |

# 发布确认

**是否建议进入打包：YES（有条件）**

条件（均为流程项，非代码改动）：
1. 先修 MUST-1（verify 脚本补音频期望集，防止"验证器全绿但没声音"）；
2. 打包后执行 MUST-2 三项冻结环境冒烟（声音 / 历史弹窗 / 备注询问）通过；
3. `build_package.py → scripts/verify_frozen_bundle.py` 全绿后再出 vpk。
