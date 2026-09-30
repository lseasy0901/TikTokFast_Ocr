# RELEASE_2.0.0_BASELINE.md —— v2.0.0 full release 基线验证记录

日期：2026-09-30
流程：清理 → PyInstaller → verify_frozen_bundle → vpk pack（full，无增量）

## 1. 执行记录

1. 清理：删除项目内旧 `build/`、`dist/`（音频功能之前的产物）；旧 feed 内容 8 个文件**移动备份**至 `_velopack_release/feed_backup_pre_audio_20260930/`（未删除）。
2. 构建：`venv/Scripts/python.exe scripts/velopack_release.py`（PyInstaller 6.22.2，~98s，onedir）。
3. 校验：`verify_bundle` 全绿（见 §3）。
4. 打包：`vpk pack --delta None --noPortable`（Velopack CLI 1.2.0，31s），未生成增量包/便携包。
5. 归档：`_velopack_release/installers/LiveLensDesktop-2.0.0-stable-Setup.exe`。

## 2. 生成文件与大小

| 产物 | 大小 |
|---|---|
| `_velopack_release/feed/LiveLensDesktop-2.0.0-stable-full.nupkg` | **298.1 MB** |
| `_velopack_release/feed/LiveLensDesktop-stable-Setup.exe` | **302.3 MB** |
| `_velopack_release/feed/{RELEASES-stable, releases.stable.json, assets.stable.json}` | 更新源元数据 |
| onedir 构建目录（`_velopack_release/2.0.0/dist/`） | 651 MB |
| installers/ 历史归档 | 1.1.0 / 1.1.1 / 2.0.0 三版 Setup |

## 3. verify_frozen_bundle 结果（全绿，exit 0）

- [PASS] 必需文件全部到位（25 项，含 ONNX 产物 SHA-256）
- [PASS] 已退役的 Paddle/CUDA 运行时均不在包内（3 项）
- [PASS] ONNX Runtime 原生库到位（2 项，含 DirectML）
- **[PASS] QtMultimedia 音频产物到位（pyd + Qt6Multimedia.dll + Windows 后端插件）** ← 本版新增检查
- [INFO] 重复文件 15 组约 71.7 MiB（datas 与 BINARY 双收，既有已知现象，不影响可运行性）

## 4. 音频组件确认（包内实测）

`_internal/PySide6/`：`Qt6Multimedia.dll`、`QtMultimedia.pyd`、`QtMultimediaWidgets.dll/pyd`（PySide6 hook 附带，无害）
`_internal/PySide6/plugins/multimedia/`：`windowsmediaplugin.dll`、`ffmpegmediaplugin.dll` —— Windows 后端齐备

## 5. 敏感文件扫描（包内全树）

- 私钥：内容级 grep `PRIVATE KEY` 零命中；仅有 `license_public_key.pem`（设计内，离线验签公钥）与 `certifi/cacert.pem`（公共 CA 列表）
- 无 `.env`、无 `*.log`、无 `test_rsa_key`、无 `*.db`（许可服务器资产全部未进包）
- 命中名 `api-ms-win-crt-private-*.dll` 为 Windows CRT API 集，误报

## 6. 遗留警告（不阻塞，但发布前须知）

1. **未签名**：VELOPACK_SIGN_PARAMS / VELOPACK_AZURE_TRUSTED_SIGN_FILE 均空，148 个文件与 Setup 未签名（与 1.1.x 历史版本一致）。正式对外发布前必须配置签名后重打包。
2. MUST-2 冻结环境冒烟仍未执行：需在干净机器安装 `LiveLensDesktop-2.0.0-stable-Setup.exe`，验证 ①声音 ②历史弹窗开关 ③首次进入备注询问。
3. vpk 提示有新版本（1.2.161），本版按 requirements-packaging 固定 1.2.0，不在发布窗口升级。
