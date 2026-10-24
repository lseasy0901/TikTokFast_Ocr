# VERSION_PRE_BUILD_CHECK.md —— v2.1.0 构建前准备检查

日期：2026-09-30
状态：**构建前准备完成，未运行 PyInstaller / 未运行 vpk pack / 未修改业务代码**

---

## A. 当前版本

- `utils/version.py`：`__version__ = "2.1.0"`（已修改并验证）
- 官方读取链路验证：`scripts.velopack_release.read_version()` → **2.1.0** ✓
- 源码树 "2.0.0" 全量检索：**零残留**（仅剩 VERSION_AUDIT_2.1.0.md 分类 B/C 允许项：文档历史、测试桩数据 1.1.1、Admin 原型标签）

## B. 增量基线

- **基线版本：2.0.0（线上已发布版）**，full nupkg 已恢复进 feed
- `feed/LiveLensDesktop-2.0.0-stable-full.nupkg` = 298,368,204 B（md5 `6806d3a2…`，与备份中已发布版逐字节一致）
- 历史 full 包链完整：1.1.0（273.9 MB）→ 1.1.1（690.9 MB）→ 2.0.0（284.6 MB）均就位

### ⚠ 处理过程中的重要发现（已妥善处置）

feed 里原本有 5 个文件——是 2.0.0 **基线验证构建**（含音频、从未发布）写进去的，与线上已发布的 2.0.0 **同名但内容不同**（md5 `1ef3c5fc…` vs `6806d3a2…`）。增量基线必须对准**线上用户实际持有的版本**，因此：

| 处置 | 对象 | 去向 |
|---|---|---|
| 保留 | 已发布的 2.0.0 full nupkg（备份副本） | `feed/`（增量基线） |
| 隔离 | 音频版 2.0.0 full nupkg / Setup / RELEASES / assets / releases 元数据 | `_velopack_release/feed_quarantine_audio_2.0.0_baseline_20260930/`（未删除） |

隔离元数据的原因：它们只登记了音频版 2.0.0，与恢复后的基线不一致；vpk pack 2.1.0 时会依据 feed 内现有包**重新生成**，无需手工修复。

### feed 当前内容（8 项）

```
LiveLensDesktop-1.1.0-stable-full.nupkg   273,962,847
LiveLensDesktop-1.1.1-stable-full.nupkg   690,964,100
LiveLensDesktop-2.0.0-stable-full.nupkg   298,368,204   ← 增量基线（线上版）
livelens-2.0.0-SetUp.exe                  695,425,668
（无 2.1.0 文件 —— 尚未打包，符合预期）
（无 RELEASES/assets/releases 元数据 —— 由 vpk 在 pack 时重新生成）
```

## C. 待生成产物（运行 `scripts/velopack_release.py` 后）

| 产物 | 说明 |
|---|---|
| `LiveLensDesktop-2.1.0-stable-full.nupkg` | 全量包（自动更新用） |
| `LiveLensDesktop-2.1.0-stable-Setup.exe` | 安装器（归档至 installers/） |
| `LiveLensDesktop-2.1.0-stable-delta.nupkg` | 增量包：2.0.0 → 2.1.0（基线已在 feed 中） |
| `releases.stable.json` / `assets.stable.json` / `RELEASES-stable` | 由 vpk 重新生成，将含 1.1.0→2.1.0 完整链 |

构建/校验链路：PyInstaller → `verify_bundle`（含 QtMultimedia 音频闸门，失败即中止）→ vpk pack。

## D. 风险检查

| 检查项 | 结果 |
|---|---|
| feed 是否包含 2.0.0 基线 | ✅ 已恢复（298,368,204 B，md5 与已发布版一致）|
| version 来源是否唯一 | ✅ 全树 "2.0.0" 源码字面量零残留；`read_version()` 返回 2.1.0；未动 `--version` 参数 |
| 是否误修改 2.0.0 历史数据 | ✅ 服务器 DB 未触碰；备份目录原样保留；两个 2.0.0 构建均已存证（feed=线上版，quarantine=音频基线版），md5 已记录在案 |
| 2.1.0 文件是否已存在（防覆盖） | ✅ 不存在，pack 不会覆盖任何东西 |
| channel / PACK_ID / 更新配置 | ✅ 未改动（stable / LiveLensDesktop）|
| 业务代码 | ✅ 零改动（本次仅 version.py 一行 + feed 文件操作）|

## E. 下一步（待指令）

运行 `venv/Scripts/python.exe scripts/velopack_release.py`，成功标准：
1. `verify_bundle` 全绿（含 QtMultimedia 音频项 PASS）
2. 生成 C 节所列三类产物
3. `releases.stable.json` 同时登记 2.0.0 与 2.1.0
