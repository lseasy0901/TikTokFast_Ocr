# RELEASE_GATE_AUDIO_PATCH_REPORT.md —— MUST-1 修复：验证器音频产物核对

日期：2026-09-30
范围：仅 `scripts/verify_frozen_bundle.py`（发布闸门验证器）+ 新增测试
禁止项确认：业务逻辑 / 音频架构 / 播放策略 / UI **零改动**（本次只动了打包验证脚本与测试）

---

## 1. 修改文件

| 文件 | 变更 |
|---|---|
| `scripts/verify_frozen_bundle.py` | ① 常量：`QTMULTIMEDIA_PYD` / `QTMULTIMEDIA_QT_DLL` / `MULTIMEDIA_PLUGIN_DIR` / `MULTIMEDIA_BACKEND_PLUGINS`；② `expected_payload()` 新增 §8 音频期望集（从构建环境 PySide6 目录推导，字节数逐一对照）；③ 新增 `audio_payload_failures(internal_dir)`：包内侧无条件断言；④ `verify_bundle()` 在 ORT 检查后接入音频核对（沿用既有 fail/ok 输出风格）；`__all__` 导出新函数 |
| `tests/scripts/test_verify_audio_payload.py` | 新增 6 项测试（缺失/完整两态 + 期望集检查） |

## 2. 设计说明（保持验证器既有风格）

- **双层核对，与 ONNX Runtime 完全同构**：
  - `expected_payload()` 层——从**构建环境** PySide6 目录推导 `QtMultimedia.pyd`、`Qt6Multimedia.dll`、`plugins/multimedia/*.dll`，进包后与源文件**字节数逐一对照**；构建环境本身缺文件时降级为 WARN（不阻塞其它核对）。
  - `audio_payload_failures()` 层——**无条件**核对包内：pyd、Qt6Multimedia.dll 必须存在；`plugins/multimedia/` 至少要有 `windowsmediaplugin.dll` 或 `ffmpegmediaplugin.dll` 之一。该层不依赖构建环境，构建环境残缺（期望集为空）时仍能兜底。
- **路径零硬编码**：构建环境 PySide6 目录用 `importlib.util.find_spec("PySide6")` 解析（与 spec 的做法同源）；包内路径用相对 `_internal` 的常量拼接，与既有 `ORT_BUNDLE_DLLS`/`FORBIDDEN_PATHS` 风格一致。
- **QtMultimediaWidgets 刻意不要求**：全项目 grep 确认只 import QtMultimedia（`QAudioSink`/`QMediaDevices`/`QAudio`），没有任何代码使用 QtMultimediaWidgets——要求它只会白白增大包体。如未来引入视频画面组件（QVideoWidget），需同步把它加进期望集（已在报告与代码注释中说明）。

## 3. 测试结果

`tests/scripts/test_verify_audio_payload.py`：**6/6 通过**

| 测试 | 结果 |
|---|---|
| 完整包（按构建环境真实文件搭建） | ✅ `audio_payload_failures()` 返回空 |
| 缺 `Qt6Multimedia.dll` | ✅ 失败，报缺失项 |
| 缺 `plugins/multimedia/` 整个目录 | ✅ 失败，报后端插件缺失 |
| 插件目录存在但无 Windows 后端（只有无关 dll） | ✅ 失败 |
| 缺 `QtMultimedia.pyd` | ✅ 失败 |
| `expected_payload()` 期望集含 pyd + Qt DLL + 插件且与构建环境文件一一对应 | ✅ |

**端到端实证**（用仓库里现存的旧 dist 产物，它先于音频功能构建）：
`python scripts/verify_frozen_bundle.py dist/DouyinLowLatencyViewer` 现在**退出码 1**，并明确报出 4 条音频缺失（pyd / Qt6Multimedia.dll / 两个后端插件）——补丁前该包是全绿放行的，证明 MUST-1 的缺口已关闭。

**回归**：tests/audio 37/37、tests/history 37/37 全部通过；业务代码零改动（无回归面）。

## 4. 是否可以进入 PyInstaller

**YES。** MUST-1 已关闭：打包后 `verify_bundle()` 会无条件核对音频产物，缺失即退出码 1、`velopack_release.py` 发布链会在 vpk pack 之前失败。剩余发布前置项只有原 MUST-2（冻结环境音频冒烟），属打包后流程。
