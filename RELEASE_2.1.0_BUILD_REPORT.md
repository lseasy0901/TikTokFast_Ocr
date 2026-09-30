# RELEASE_2.1.0_BUILD_REPORT.md —— v2.1.0 Release Build

日期：2026-09-30
前置：VERSION_PRE_BUILD_CHECK.md 通过（version.py=2.1.0，feed 基线=线上 2.0.0）
约束遵守：业务代码零改动；历史版本文件零改动；未上传服务器；releases 元数据全部由 vpk 生成、无手工编辑

---

## 1. 构建时间线（均有日志佐证）

| 阶段 | 耗时 | 结果 |
|---|---|---|
| PyInstaller onedir（`_velopack_release/2.1.0/dist`） | ~67 s（构建内部计时 66,777 ms） | ✅ Build complete |
| verify_bundle（vpk 之前强制闸门） | 即时 | ✅ 4 项全 PASS（见 §3） |
| vpk pack 第 1 次（脚本按既有设计 `--delta None`） | 34 s | ✅ 生成 full + Setup |
| vpk pack 第 2 次（默认 delta 模式，补生成 delta） | 37 s | ✅ **Delta 2.0.0 → 2.1.0 生成**（1040 文件：3 patched / 1037 unchanged / 40 new / 0 removed） |

> 说明：发布脚本历史上固定 `--delta None`（当时策略为不发增量）。本次任务要求 delta，故在脚本参数完全一致的前提下仅去掉 `--delta None` 重跑了一次 vpk（元数据由 vpk 原子重生成，无手工编辑）。**建议后续把脚本 delta 策略参数化**（工具链改动，非本次范围）。

## 2. 产物清单与大小 / SHA256

| 产物 | 大小 | SHA256 |
|---|---|---|
| `LiveLensDesktop-2.1.0-stable-full.nupkg` | 312,532,114 B（298.1 MB） | `16bbe5c8d20f93062b1aa0a28141cbd421bc9966c44bbec0332ce5bdf71736d1` |
| `LiveLensDesktop-2.1.0-stable-delta.nupkg` | **15,106,417 B（14.4 MB）** | `81b5978f88666c29b43a25b64583813293a3614d97b2d78a0f19d3b3faa85292` |
| `installers/LiveLensDesktop-2.1.0-stable-Setup.exe` | 316,993,682 B（302.3 MB） | `3f0d39c1913a47695e0f3b53ddeb6c70b89e856ab829cd87e1e48024da0dcee3` |
| onedir 构建目录 | 651 MB | —（verify 逐文件字节数对照已覆盖） |

delta 体积 14.4 MB（约为 full 的 4.8%）——音频 DLL 为新增文件走 40 个"new"，其余 1037 个文件未变，符合"2.0.0→2.1.0 主要是新增音频/UI 功能"的预期。

## 3. verify_frozen_bundle 结果（4 项全 PASS）

```
[PASS] 必需文件全部到位（25 项，含 ONNX 产物 SHA-256）
[PASS] 已退役的 Paddle/CUDA 运行时均不在包内（3 项）
[PASS] ONNX Runtime 原生库到位（2 项，含 DirectML）
[PASS] QtMultimedia 音频产物到位（pyd + Qt6Multimedia.dll + Windows 后端插件）
[INFO] 重复文件 15 组约 71.7 MiB（datas/BINARY 双收，既有已知现象）
```

包内音频文件实测 33 个（Qt6Multimedia.dll、QtMultimedia.pyd、Windows 后端插件等全部到位）。

## 4. 敏感文件扫描（2.1.0 包内全树）

- 内容级 `PRIVATE KEY` grep：**零命中**
- `.env` / `test_rsa*` / `*.log`：**零命中**
- 仅 `license_public_key.pem`（设计内公钥）与 `certifi/cacert.pem`（公共 CA）

## 5. Velopack 状态（未上传、未手工改元数据）

`releases.stable.json` 由 vpk 生成，登记 5 条：

| Version | Type | 说明 |
|---|---|---|
| 2.1.0 | **Full** | 本次新版本 |
| 2.1.0 | **Delta** | **基线 = 2.0.0（线上版，SHA1 000CD35A… 与 releases 表登记一致）** |
| 2.0.0 | Full | 线上版本（feed 内文件与已发布包逐字节一致，md5 `6806d3a2…` 已在 PRE_BUILD_CHECK 留档） |
| 1.1.1 / 1.1.0 | Full | 历史链 |

`RELEASES-stable`（legacy 格式）由 vpk 重新生成，登记 4 个 full（Velopack 的 legacy 文件本就不含 delta 条目，客户端走 `releases.stable.json`）。

feed 中曾短暂存在的"无 delta 版 2.1.0 产物"已移入
`_velopack_release/feed_quarantine_audio_2.0.0_baseline_20260930/no_delta_2.1.0/`（未删除）。

## 6. 是否可以进入安装测试

**YES（本地验证维度全部通过）。**

安装测试清单（建议在干净虚拟机/干净账户执行）：
1. 双击 2.1.0 Setup 全新安装 → 启动 → 连接直播间 → ①视频 ②**声音** ③历史弹窗开关 ④首次进入备注询问
2. 在 2.0.0 机器上走应用内更新 → 确认**走 delta 包**（更新日志/下载体积 ~15 MB 量级）→ 更新后功能同上
3. 更新后检查 `%LOCALAPPDATA%/LiveLens/` 历史记录、备注、授权状态完整保留

## 7. 遗留警告（同基线，不阻塞安装测试）

1. **未签名**（148 文件 + Setup）；正式对外发布前必须配置签名环境变量重新打包。
2. vpk 1.2.161 可用但构建链固定 1.2.0，发布窗口内不升级。
3. 上传更新源（livelen.icu / 服务器 update_releases 表登记 2.1.0）属于下一步发布动作，本次未执行。
