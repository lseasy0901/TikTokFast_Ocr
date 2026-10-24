# VERSION_AUDIT_2.1.0.md —— v2.1.0 Release Version Audit

日期：2026-09-30
范围：全项目版本配置审计（只读，未修改任何文件）
线上用户版本：v2.0.0；本审计目标：确认发布 v2.1.0 需要改动的所有位置

---

## 1. 当前版本状态

- **版本号唯一事实来源**：`utils/version.py` → `__version__ = "2.0.0"`（全项目源码中**唯一**出现 "2.0.0" 的位置，已全树检索证实）
- 该文件被三个消费方按契约读取（见 `version.py` 头部注释）：
  1. 客户端运行时：`utils/update_service.py:143`（`from utils.version import __version__`，与服务器返回版本做三段数值比较）
  2. PyInstaller spec：`douyin_viewer.spec:24-27`（exec 读取，构建日志与产物标识）
  3. 发布流程：`scripts/velopack_release.py:119-124`（`read_version()` exec 读取 → `--packVersion`、`build_root` 目录名、安装器归档文件名）
- **不存在** pyproject.toml / setup.py / setup.cfg；EXE 的 `version_file=None`（无 Windows 版本资源，版本由 Velopack nupkg metadata 承载）
- 版本语义约束（version.py 契约）：纯数字三段式，禁止 `-beta`/`+build` 后缀——"2.1.0" 合规

## 2. 发现的位置

### A. 必须修改为 2.1.0

| # | 位置 | 现状 | 说明 |
|---|---|---|---|
| A-1 | `utils/version.py:28` | `__version__ = "2.0.0"` | **唯一需要编辑的文件**。改后自动传播到：spec 构建标识、`vpk --packVersion`（即 nupkg metadata 与 `releases.stable.json`）、安装器文件名 `LiveLensDesktop-2.1.0-stable-Setup.exe`、构建目录 `_velopack_release/2.1.0/`、客户端更新比较（2.0.0 用户将收到 2.1.0 更新提示） |

> 显式不做的：`velopack_release.py --version` 参数**只用于救急覆盖**，不得用它绕过 version.py（否则客户端运行时仍是 2.0.0，更新比较会错乱）。

### B. 必须保留 2.0.0 / 由流程处理（不手工编辑）

| # | 位置 | 说明 |
|---|---|---|
| B-1 | 许可服务器 DB `update_releases` 表中 v2.0.0 的 release 行 | 历史发布记录 + 增量基线；`(channel, version)` 唯一约束意味着 2.1.0 发布是**新增一行**，绝不能改旧行 |
| B-2 | feed 中的 `LiveLensDesktop-2.0.0-stable-full.nupkg` | Velopack 生成增量包与升级基线的依赖；发布 2.1.0 前必须确保它在 feed 目录里（见风险 R-1） |
| B-3 | `releases.stable.json` / `assets.stable.json` / `RELEASES-stable` | 由 vpk 在 pack 时**重新生成**，手工编辑会被覆盖；它们属于数据而非配置 |
| B-4 | 用户机器上的已装 2.0.0 | 由 Velopack 更新流程处理，与代码无关 |

### C. 无需修改

| # | 位置 | 原因 |
|---|---|---|
| C-1 | `test_phase_3_velopack_update.py` / `test_phase_4b_update_recovery.py` / `test_publish_update_bat.py` 中的 "1.1.1" | 测试固定桩数据（fake Velopack asset / 发布脚本参数化测试），验证的是与版本无关的逻辑；跟着真实版本改反而让测试变脆 |
| C-2 | `LiveLens_Admin_2.0/prototype/assets/js/app.js:103` 的 "v2.0.0-proto" | 管理端原型页的装饰性标签，与客户端版本无关联 |
| C-3 | `scripts/onnx_model_manifest.json` | 是 ONNX 模型文件的 SHA-256 清单，不含应用版本 |
| C-4 | 各历史报告 md（PHASE_*/AUDIO*/RELEASE_*） | 历史记录 |
| C-5 | `scripts/velopack_release.py` 的 `PACK_ID = "LiveLensDesktop"`、`CHANNEL = "stable"` | 构建/分发通道标识，变更会断开既有用户的更新通道；2.1.0 保持不变 |
| C-6 | `douyin_viewer.spec` | 版本经 exec 读取 version.py，spec 本体无版本字面量 |

## 3. 修改建议（发布 2.1.0 的操作序列）

1. 编辑 `utils/version.py`：`__version__ = "2.0.0"` → `"2.1.0"`（唯一代码改动）
2. **恢复 feed 基线**（见 R-1）：把备份里的 `LiveLensDesktop-2.0.0-stable-full.nupkg` 等旧版 full 包放回 `_velopack_release/feed/`
3. `python scripts/velopack_release.py`（会构建 2.1.0、verify 闸门、vpk pack 生成 full 包 + 更新索引；如需增量包，前提就是第 2 步的 2.0.0 包在 feed 里）
4. 按既有流程把 `releases.stable.json` / 新 nupkg 发布到 livelen.icu 更新源（服务端 `update_releases` 新增 2.1.0 行）

## 4. 风险点

| # | 风险 | 缓解 |
|---|---|---|
| R-1 | **2.0.0 full nupkg 目前不在 feed 里**：2.0.0 基线构建时为避免版本混装，把旧 feed 全部移到了 `_velopack_release/feed_backup_pre_audio_20260930/`。若不先恢复就打包 2.1.0：无增量包（功能不损失），且新 `releases.stable.json` 只含 2.1.0——2.0.0 客户端仍能全量更新到 2.1.0，但 2.0.0→2.1.0 之间若有后续版本将失去增量链 | 发布第 2 步必须先恢复旧 full 包 |
| R-2 | 服务器 `(channel, version)` 唯一约束：同版本重复发布会冲突——这也是必须先把 version.py 升到 2.1.0 的硬理由（禁止用 2.0.0 版本号重发音频版） | 已在 A-1 强调 |
| R-3 | 版本号写了第二处（如在 spec/脚本里硬编码 2.1.0）会破坏单一事实来源契约，造成客户端运行时与包 metadata 不一致、更新比较错乱 | 只改 version.py，逐字面量全树检索已确认无第二处 |
| R-4 | EXE 无 Windows 版本资源（`version_file=None`）：右键属性看不到版本号。属既有行为，2.1.0 不引入变更；如要补需另立任务（加 version 文件属新功能，不在本次审计范围） | 记录即可 |
| R-5 | 更新比较按三段数值逐段比较：2.1.0 > 2.0.0 成立；不要使用 2.10.0 之类会引起歧义的写法以外的前导零（如 02.1.0） | "2.1.0" 合规 |

## 5. 结论

版本体系是"单文件单一事实来源"设计，发布 2.1.0 的代码改动**只有 A-1 一处**；其余全部是发布流程动作（恢复 feed 基线 → 构建 → 上传）。当前代码可直接进入 2.1.0 版本号变更。
