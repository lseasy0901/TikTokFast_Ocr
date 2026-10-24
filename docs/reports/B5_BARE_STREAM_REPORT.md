# LiveLens B5：对照软件裸流接入 —— 执行报告

日期：2026-10-12
分支：`feature/b5-bare-stream`（独立工作树 `D:\TIikTok_Ocr\_wt_b5`，基线 `f0418ea`）
验证房间：`https://live.douyin.com/50942209695`（内层 stream id `408474773338456894`）

---

## 1. 源码恢复程度

对照软件 `live-player.exe`（Qt6 + 自带 FFmpeg 8 系：`avcodec-62`）的**静态逆向取证此前已完成**，
产物在 `~/.zcode/workspace/default/liveplayer-case/`（`final-report.md` 52KB + 逐函数反汇编
`fn_*.txt` + `func_index.json`，全部结论带 [A]/[B]/[C]/[D] 证据等级）。

与"视频版裸流获取"直接相关的恢复结论（[A] 级，来自 `final-report.md` §P0-1）：

- 抖音网页端：解析 `data → stream_data → main`，正则取首个 `.flv`；
- 抖音移动端：`webcast.amemv.com/webcast/room/reflow/info` → `status → stream_url →
  live_core_sdk_data → flv_pull_url`，**先取 `h265PullData`，空则回退 `h264PullData`**；
- 最终 URL 原样交给 FFmpeg（`avformat_open_input` @0x140033BC0），token/expire 不解析；
- 播放侧 FFmpeg 按域名分档注入 `live_flv`/`flv_ignore_prevtag`/`reconnect`/`rw_timeout` 等参数。

即：**对照软件取的就是"App/SDK 分发线的原画裸流"，与 LiveLens 现有 `_extract_sdk_candidates`
同源**；关键差异在对照软件自带的 FFmpeg 8 能解增强 FLV（HEVC），而 LiveLens 内置 6.x 不能。

本次仅补充验证了"获取 + 解码"这一必要环节，未重复既有可行性验证。

## 2. 实际修改了哪些文件

| 文件 | 分支/状态 | 说明 |
|---|---|---|
| `stream/ffmpeg_reader.py` | `feature/b5-bare-stream` 提交 `d2ba006` | **本次唯一代码改动**：ffmpeg≥7 运行时不再注入 `-thread_queue_size`（见 §3） |
| `stream/bare_candidate.py`、`stream/ffmpeg_reader.py`(exe_path)、`stream/models.py`(runtime) | 已在 `f0418ea`/`5e502ae` 提交 | B4：裸流候选派生 + FFmpegReader `exe_path` 覆盖 + 门禁 |
| `stream/adapters/douyin.py`、`gui/main_window.py` | 主工作树**未提交**（他人在改，未触碰） | 页面/SDK 裸流候选提取（douyin.py）、ROI 记忆 + 诊断埋点（main_window.py） |
| `gui/main_window.py`（裸流播放接线 `exe_path=ffmpeg7_exe()`） | 在 `feature/bare-candidate-sdk`（`0e9d9d5`） | 播放侧接线在另一分支，本次未合并 |

**保护文件**（本次均未修改，基线 SHA256 前 16 位）：
`stream/adapters/douyin.py`=8bd66375314a7772、`gui/main_window.py`=41675926b03faa5d、
`ocr/roi_presets.py`=e4c889359d751ea0、`config.yaml`=62864201c271b678。

## 3. 是否成功取得并显示视频裸流

**取得：是。** 房间页面直接扫到视频版裸流 URL（basename `stream-<id>.flv`、无 `only_audio=1`、
含 `unique_id`/`t_id`）：
`http://pull-flv-q95.douyincdn.com/thirdgame/stream-408474773338456894.flv?expire=...&sign=...&unique_id=stream-408474773338456894_830_flv&t_id=...`

**解码：是（ffmpeg7）。** 独立 ffprobe/解码：`hevc 1920x1080 yuv420p 60fps ~20.5Mbps`，
6s 出 360 帧完整。
**内置 ffmpeg（e6a2447/6.x）：否** —— `Video codec (c) is not implemented` / `Video: none [12]`，0 字节。
这正是门禁 `DLV_FFMPEG7_EXE` 存在的原因。

**发现并修复一处集成阻塞（本次关键产出）：**
`-thread_queue_size 1` 在 ffmpeg 7.x 中已属 muxer（输出）选项，作为**输入选项**放在 `-i` 前，
被 CLI 直接拒绝（`cannot be applied to input url`）→ **整条命令开流失败，裸流经 FFmpegReader 永远起不来**。
（此前原型测试用的是自建参数集，从未跑过 LiveLens 的完整命令行——许可证审计已预警该组合未补测。）

修复后在 `FFmpegReader`（LiveLens 实际解码路径）实测（房间 50942209695 视频版裸流）：

- 修复前：开流失败、0 帧；
- 修复后：**600 帧、首帧 0.407s、连续出帧**（单槽跳帧属预期）。
- 生产路径（内置 6.x，未传 `exe_path`）命令行**逐字不变**。

**显示：** 显示侧（`VideoWidget.set_frame`）与解码同源，未单独跑 GUI 实机；解码路径已确认出帧。
GUI 播放接线（`exe_path=ffmpeg7_exe()`）在 `feature/bare-candidate-sdk`，尚未并入本分支。

## 4. 接入前后延迟

既有三轮同房间 A/B（`_diag_e4b_20261012/e4b_r{1,2,3}_result.json`；60s 墙钟 60Hz 重采样 NCC，
约定 `L>0 => X leads Y`）：

| 对比 | r1 | r2 | r3 | 结论 |
|---|---|---|---|---|
| `SDK_BARE` vs `SDK_OR4` | +29帧/+0.483s | +28帧/+0.467s | +29帧/+0.483s | **裸流领先 ≈0.48s** |
| `WEB_UHD` vs `SDK_BARE` | -30帧/-0.500s | -32帧/-0.533s | -31帧/-0.517s | 网页 UHD 落后 ≈0.5s |
| `WEB_UHD` vs `SDK_OR4` | -1帧 | -4帧 | -1帧 | 网页线≈SDK转码档 |

NCC ≈0.93，三轮一致。即**视频版裸流内容比生产正在用的转码档新约 0.48–0.50 秒**
（源侧内容领先，与直播解码+显示管线开销无关，可近似全部转化为端到端延迟收益）。

稳定性：既有 40 分钟连续实测（原型自建参数）141,023 帧、58.5fps、>100ms 间隙 0.23%，
直到 URL 令牌过期 EOF——长时稳定。

## 5. 测试结果及 Git 状态

**自动化测试**（`feature/b5-bare-stream`）：`tests/stream/` 12 passed / 1 failed；
`tests/audio/test_audio_no_track.py` + `test_f2/test_f3/test_bare_candidate` 17 passed。

- 唯一失败 `test_f1_primary_404_falls_back` 为**既存**测试时序敏感问题：本地夹具 mp4 被 ffmpeg
  以 ~280x 速度读完，20ms 轮询只采到 4 帧（断言 ≥5）；回退本身已生效（stderr 显示已切回退流）。
  该用例 `exe_path=None`，与本次改动无关（改动前同样失败）。

**Git 状态**：
- 主工作树 `DouyinLowLatencyViewer` @ `feature/bare-stream-integration` HEAD=`f0418ea`，
  未提交改动（`gui/main_window.py`、`stream/adapters/douyin.py` 等）**原样保留，未触碰**；
- 新工作树 `_wt_b5` @ `feature/b5-bare-stream`，提交 `d2ba006`（仅 `stream/ffmpeg_reader.py`）；
- 未合并、未推送。

## 6. 结论：保留

**保留独立分支 `feature/b5-bare-stream`（提交 `d2ba006`），等待人工验收后再决定合并。**

理由：
- 效果成立：裸流内容领先生产转码档 ≈0.48–0.50s（三轮 A/B 一致），且长时稳定；
- 本次修复消除了"裸流经 LiveLens 实际命令行无法开流"这一硬阻塞，解码路径已实测出帧；
- 生产链路零改动（内置 6.x 命令行逐字不变，门禁默认关闭）。

**未决 / 待人工决策（不阻塞保留分支）：**
1. **分发与许可**：外部 ffmpeg7（204MB，GPL v3）不宜直接随包分发；
   现策略为门禁 + 用户自配 `DLV_FFMPEG7_EXE`（`b4_license_audit.md`）。
2. **候选源接线统一**：本分支拿到的是"页面/SDK 直取裸流"；`feature/bare-candidate-sdk`
   采用"从已选候选后缀剥离派生"。两者需二选一并接入播放侧 `exe_path`，全链路才算闭合。
3. **候选完整性判据**：当前宽扫描要求 `s_anchor=1`，本房间裸流 URL 无该参数会被漏掉；
   实测正确判据为"basename 无质量后缀且不含 `only_audio=1`"。
4. 未做 Windows x64 EXE 打包实机验收（需要 GUI + 直播房间，本轮未执行）。
