# LiveLens B5 GUI 验收报告（裸流接入）

日期：2026-10-12
分支：`feature/b5-gui-integration`（独立工作树 `D:\TIikTok_Ocr\_wt_b5gui`）
基线：`0e9d9d5`（= SDK 播放接线）→ 加上 FFmpeg7 参数修复与页面直取裸流
验证房间：`https://live.douyin.com/50942209695`（内层 stream id `408474773338456894`）

---

## 1. 实际整合的提交与文件

分支 `feature/b5-gui-integration`：

| 提交 | 内容 |
|---|---|
| `49f9a23` | `stream/ffmpeg_reader.py`：ffmpeg≥7 运行时不再注入 `-thread_queue_size`（作为输入选项被 CLI 拒绝，导致裸流开流失败）。判据：仅显式传入 `exe_path` 时省略；生产默认路径逐字不变。 |
| `98e6394` | `stream/bare_source.py`（新）＋ `gui/main_window.py` ＋ `main.py`（详见下） |

**新增 `stream/bare_source.py`**：从直播间页面**直取**视频版裸流 URL。
- 判据（修正缺口）：basename 恰为 `stream-<id>.flv` 且 **不含 `only_audio=1`** 且含 `unique_id=stream-` 与 `t_id=`。
  **不再把 `s_anchor=1` 当必要条件**（本房间裸流 URL 无该参数，旧判据会漏识别）。
- 实测证据：从 `_uhd` 后缀**剥离派生**的裸流 URL 返回 **403**（sign 绑定 path），故必须页面直取，不能派生。

**`gui/main_window.py` 接线**：`bare_url = video_bare_url_for(info.platform, info.room_id)`；
门禁关闭 / 非 FLV / 取不到裸流时**行为与改动前逐字一致**；保留 `fallback=(原候选, None)` 故障回退。

**`main.py`**：新增 `LIVELENS_AUTOCONNECT` 验收钩子（仅环境变量启用，非产品功能，与既有 `LIVELENS_E2E_UPDATE` 同风格）。

**保护性约束遵守**：默认关闭（需 `DLV_BARE_STREAM=1` + 存在的 `DLV_FFMPEG7_EXE`）；未改动 `stream/adapters/douyin.py`、用户配置、ROI；主工作树未提交改动**原样保留**（保护文件 SHA256 与基线一致：douyin.py `8bd66375…`、main_window.py `41675926…`、roi_presets.py `e4c88935…`、config.yaml `62864201…`）。

## 2. 自动化测试结果

- `tests/stream/`：**12 passed / 1 failed**。唯一失败 `test_f1_primary_404_falls_back` 为**既存**时序敏感用例（本地夹具 mp4 被 ffmpeg ~300x 读完，20ms 轮询只采到 4 帧，断言 ≥5；回退本身已生效）。该用例走 `exe_path=None` 路径，与本次改动**逐字无关**（改动前同样失败）——非本次回归。
- 新模块纯函数 `extract_video_bare` 抽查：视频裸流→命中；`only_audio=1`→None；截断片段→None；`_uhd`→None；混排取视频→正确。

## 3. GUI/EXE 是否真实运行成功

**真实 GUI 运行成功（裸流模式）**，房间 50942209695：

- 日志链路：`[BARE] 页面获取 HTTP 200` → `[BARE] 视频版裸流` → `[FFMPEG] 裸流模式：尝试 ffmpeg7 裸流，回退 <原候选>` → `[FFMPEG] first frame received` → `[DISPLAY] set_frame 1920x1080` 持续（45s 内 count 达 2600+，无断流）。
- 界面：状态栏"直播中 1920×1080 FPS 56 首帧 259ms 解码 67fps"，画面为直播间实时内容。
- 用户手测确认：**裸流模式画面更快**。

说明：运行的是**真实 LiveLens 应用**（`main.py`——EXE 即其打包形态，代码路径一致）。本轮**未重新打包 EXE**：改动全部门控默认关闭，打包产物行为不变；且按第 8 点，GPL 的 FFmpeg7 二进制不进入发布包（仅外部运行时经环境变量指定）。

## 4. 裸流 vs `_uhd` 的端到端延迟对比

方法：**两个真实 LiveLens 实例并排**（左=裸流模式，右=普通 `_uhd` 模式），同一房间同时播放，录 1920×1080 桌面 30fps×45s，取两路视频区做灰度互相关（NCC）。

结果（约定：**正 lag = 裸流画面领先**）：

| 段 | lag | NCC |
|---|---|---|
| 0–5s | +0.700 s | 0.976 |
| 5–10s | +0.700 s | 0.943 |
| 10–15s | +0.700 s | 0.979 |
| 15–20s | +0.700 s | 0.981 |
| 20–25s | +0.700 s | 0.981 |
| 25–30s | +0.700 s | 0.980 |
| 30–35s | +0.700 s | 0.977 |
| 35–40s | +0.700 s | 0.999 |

**全局：裸流画面领先 `_uhd` +0.700 秒**（8/8 段一致，NCC 0.94–0.999）。这是端到端（含解码＋GUI 显示）结果，非解码 FPS 或源侧数据。

**启动代价（需注意）**：裸流路 `spawn≈3.3s / first_frame≈0.93s`（含额外一次页面 GET 取裸流 URL + ffmpeg7 冷启动 + CUDA 初始化）；`_uhd` 路 `spawn≈10ms / first_frame≈0.28s`。即**稳态领先 0.7s，但起播多花约 1s**——对本项目"蹲房间码"场景通常可接受，但需知悉。

## 5. 故障恢复与回退结果

- **ffmpeg7 不可用**（把 `DLV_FFMPEG7_EXE` 指向存在但非 ffmpeg 的文件）：应用**不卡死**——裸流尝试快速失败（每次 ~12–50ms），FAILOVER 依次切换候选，第 5 个候选成功并**持续出帧**（40s 内 count 达 1900）。证明"外部运行时缺失 → 回退原 `_uhd` 路径"成立。
- 裸流 reader 携带 `fallback=(原候选, None)`：裸流开流失败即回退原候选（默认运行时），保留既有失败切换/看门狗/断流自愈机制。
- **默认行为不变**：A/B 的 `_uhd` 路即门禁关闭下的正常路径，与本改动前一致。

## 6. 结论：保留

**保留独立分支 `feature/b5-gui-integration`（提交 `49f9a23` + `98e6394`），等待人工验收后再决定合并。未合并主线、未推送。**

依据：
- 真实 GUI 下裸流模式**成功取流、显示、持续出帧**（用户手测亦确认更快）；
- 端到端画面**领先 `_uhd` 0.700s**（8/8 段一致，NCC≈0.98）；
- 故障回退成立，默认生产行为零改动，受保护文件与主工作树未触碰。

**待人工决策（不阻塞保留）**：
1. 起播约多 1s（页面取流 + ffmpeg7 冷启动）——是否需要缓存/并行化以缩短；
2. 外部 FFmpeg7（GPL v3，204MB）分发与许可（`b4_license_audit.md`）——本轮不入包；
3. 候选源统一：本分支用"页面直取"，`feature/bare-candidate-sdk` 用"后缀派生"（实测 403，不可用），应以本分支为准；
4. 未重打包 EXE（改动门控默认关闭，产物行为不变）。
