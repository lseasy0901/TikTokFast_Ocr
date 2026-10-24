# AudioService 实施报告

日期：2026-09-29
范围：LiveLens（DouyinLowLatencyViewer）直播声音播放功能
状态：已完成，tests/audio 32 项全部通过，既有回归（stream_resolver / frame_ready / stream_history_paths 共 65 项）无失败

---

## 1. 架构说明

```
StreamService（现有播放生命周期，main_window 接线）

        │  同一个 FFmpeg 实例（同一 -i 输入）
        │
        ├── stdout: rawvideo bgr24 ──→ 视频链路（LatestFrameBuffer / OCR）
        │                             【命令与字节流与改动前完全一致】
        │
        └── 第二路输出: s16le PCM ──→ tcp://127.0.0.1:<随机端口>
                                          │
                                     FFmpegReader 音频读取线程
                                          │  audio_pcm_callback(bytes|b""|None)
                                          ▼
                                   AudioService（生命周期门面）
                                          │  不阻塞队列（满则丢最旧）
                                          ▼
                                   AudioWorker（单播放线程）
                                          │  缓冲策略（Sync / LowLatency）
                                          ▼
                                   QtAudioOutput（QAudioSink push 模式）
```

模块职责（`audio/`）：

| 模块 | 职责 |
|---|---|
| `service.py` | start / stop / restart / set_volume / set_mode；状态机（IDLE/STARTING/PLAYING/NO_AUDIO_TRACK/ERROR）；吞掉一切音频异常 |
| `worker.py` | 播放线程：队列 → 策略缓冲 → 输出；音量/模式变更即时生效；输出在本线程内打开与释放 |
| `strategy.py` | Sync（起播水位 80ms、欠载重灌、安全上限 2s）/ LowLatency（上限 120ms、丢旧优先、欠载不重灌） |
| `output.py` | QAudioSink push 封装，QtMultimedia 唯一接触点（延迟导入），`AudioOutput` 接口供测试注入 |
| `models.py` | 模式/状态/配置（跨线程带锁）/统计/PCM 常量 |

耦合边界：
- OCR 不依赖 audio；audio 不依赖 OCR。video stdout 字节流与命令的视频段逐字未动。
- `main_window` 仅 4 处接线：import、`AudioService()` 创建、`_build_video_toolbar` 挂 `AudioPanel`（信号互连）、`_start_playback` 里 `audio.start()` / `_teardown_playback` 里 `audio.stop()`。无任何音频处理逻辑。

### 传输通道选型（Windows 实测结论，重要）

需求原定 `pipe:3` 双输出。开发中实测（本项目自带 ffmpeg 5.x / avformat-59，构建 e6a2447）：

1. **pipe:3 + 句柄继承**：fd 3 未映射到子进程 CRT，ffmpeg 直接 fastfail 崩溃（0xC0000409），不可用；
2. **Windows 命名管道 `\\.\pipe\...`**：本机环境 Python 的 `CreateNamedPipeW` 被安全软件 hook 拦截（错误 123，PowerShell 却可创建），不可依赖；
3. **127.0.0.1 回环 TCP**（最终采用）：零依赖、跨平台、延迟 <1ms；ffmpeg 作为客户端主动连出，本层监听并 accept 恰好一个连接。保持"单 FFmpeg 实例、无第二条拉流"。

### 无音频轨判定

音频输出使用**非可选** `-map 0:a`（刻意不用 `0:a?`：实测可选映射在流无音频轨时使输出零流，整个 ffmpeg 报错退出，视频陪葬）。非可选映射在**配置输出阶段**（尚未打开网络）即失败退出，由此得到确定性判据：

> stdout EOF + ffmpeg 进程退出 + 音频端口从未被连入 ⇒ 无音频轨

读取线程据此**去掉音频输出重启一次**（纯进程重启，无网络等待，仅一次，有上限），并回调 `audio_pcm_callback(None)` → 服务显示"当前直播无声音"。

## 2. 修改文件列表

新增：
- `audio/__init__.py`、`audio/service.py`、`audio/worker.py`、`audio/output.py`、`audio/strategy.py`、`audio/models.py`
- `gui/audio_panel.py`（音量滑块 + 同步/极速模式 + 状态显示；无播放按钮/进度条/延迟数字）
- `tests/audio/`（7 个测试文件 + 桩件，共 32 用例）

修改：
- `stream/ffmpeg_reader.py`：`audio_enabled` / `audio_pcm_callback` 参数；`_build_ffmpeg_cmd` 追加音频输出段（不传参时命令与旧版逐字一致）；音频监听 socket 与读取线程；无音轨重启一次逻辑；`stop()` 音频资源回收。视频 stdout 读取路径逻辑不变。
- `gui/main_window.py`：4 处最小接线（见上）。
- `douyin_viewer.spec`：hiddenimports 增加 `PySide6.QtMultimedia`；plugins 收集增加 `multimedia` 目录（windowsmediaplugin.dll / ffmpegmediaplugin.dll）。

## 3. 数据流说明

1. 连接成功 → `_start_playback` 创建 FFmpegReader（`audio_enabled=True`，回调=`AudioService.feed`）→ `reader.start()`：bind 随机回环端口 → spawn ffmpeg（视频 stdout + 音频 tcp 两路输出）→ 启动音频 accept/读取线程 → `audio.start()`（状态 STARTING）。
2. ffmpeg 打开输入后连入音频端口；音频线程逐块（8KB）回调 `feed`；首个 PCM 块 → 状态 PLAYING。
3. `feed` 永不阻塞：投递线程同步计数，队列（48 块 ≈ 2s 吸收能力）满时**丢最旧块**——保证 TCP 不反压 ffmpeg（ffmpeg 单进程任一输出阻塞会拖住视频，这是隔离设计的关键）。
4. worker 循环：策略 `on_chunk` → 达到起播水位后写入 QAudioSink（4096B/片）；设备缓冲满时余量留在策略内；真欠载（缓冲空+队列空+设备 IdleState）由策略决定重灌或继续。
5. 模式切换：worker 下一次迭代检测到模式变化即换新策略实例（缓冲语义不做迁移）。
6. 音量：GUI 线程写 `AudioConfig`（带锁），worker 每循环同步到 `sink.setVolume`。
7. 断开/failover/关窗：`_teardown_playback` → `audio.stop()`（幂等）→ worker 退出并释放 sink，reader 关闭音频 socket。

## 4. 延迟分析

- 通道延迟：回环 TCP < 1ms；FFmpeg 解码 PCM 即时刷出。
- **Sync 模式**：起播水位 80ms，稳态延迟 ≈ 80ms 缓冲 + QAudioSink 内部缓冲（设备默认，典型 60~150ms）≈ **150~250ms**，声音连贯优先，欠载时静音等待重灌。
- **LowLatency 模式**：上限 120ms（超即丢旧），稳态延迟 ≈ **40~120ms**，最新声音优先，接受偶发静音。
- 与视频的同步方式为"缓冲水位对齐"（视频链路本身约 100~200ms 延迟），不做 PTS 精确对齐——实时观看场景二者同源同墙钟，感知偏移在可接受范围。精确音视频同步需要共享时钟，超出本阶段范围（已知限制）。

## 5. CPU / 内存影响

实测环境：Windows 11 / 本项目自带 ffmpeg 5.x / 640x360 mpeg4 30fps + AAC 44.1k 源，`-re` 实时速率，psutil 采样 8s 稳态（含 4 轮交叉对照）：

| 指标 | 仅视频（基线） | 视频+音频第二路 | 增量 |
|---|---|---|---|
| ffmpeg 进程 CPU | ~23ms/s | ~21~39ms/s | **≤16ms/s（≈1.6% 单核）** |
| Python 进程 RSS | 基线 | — | **+2~6MB**（QtMultimedia DLL + 缓冲区） |
| Python 音频线程 CPU | — | — | 每秒 3 万字节 PCM 的拷贝+入队，可忽略 |
| 新增线程 | — | — | 2 条（音频 accept/读取、播放），均为守护线程 |

打包体积：QtMultimedia.dll + 2 个 multimedia 插件 + 依赖的 QtNetwork 等，估计 +6~10MB（spec 已配置收集）。

## 6. 测试结果

`python -m unittest discover -s tests/audio`：**32/32 通过**（约 1.4s，全离线，FakeOutput 注入，不依赖声卡/网络）：

1. 启动/停止（5 用例）：状态迁移 IDLE→STARTING→PLAYING→IDLE；start/stop 幂等；未启动 feed 丢弃；PCM 端到端到达输出。
2. 模式切换（5 用例）：策略工厂；Sync 水位/重灌；LowLatency 上限裁剪且保留最新尾部；运行中切换立即生效。
3. 音量控制（3 用例）：默认值；钳位 [0,1]；启动前/后设置均到达输出设备。
4. buffer 策略（6 用例）：FIFO、部分取、安全上限精确值、丢旧保留新尾部、欠载语义（Sync 清空 vs 极速保留）、队列满丢最旧不反压。
5. FFmpeg 异常（4 用例）：打开失败→ERROR 且调用方无感；设备缺失文案；播放中写入故障→ERROR 不外溢；`_notify_audio` 回调异常隔离。
6. 无音频轨（6 用例）：`feed(None)`/EOF 判定；有数据后 EOF 不误判；命令构造（音频关=与旧命令一致；音频开=仅追加音频段且位于 stdout 段之后）。
7. 断线恢复（1 用例）：`restart()` 重建输出并恢复播放，旧输出全部释放。
附：AudioPanel（4 用例）：音量/模式信号、状态渲染（含跨线程 Signal 路径）、静默回显。

真实环境验证（手动）：
- E2E（真实 ffmpeg）：有音轨文件 → 音频 387868B（2s PCM）完整到达 + 视频正常；无音轨文件 → 自动重启一次、`None` 回调触发、视频正常播放。
- 真实声卡：QAudioSink 打开/写入/关闭 OK（Realtek Audio，48kHz/16bit/2ch 被支持）。
- 回归：test_stream_resolver、test_phase_b2_frame_ready、test_stream_history_paths 共 65 项通过。
- GUI：`main_window` 及全部 audio 模块 headless 导入通过。

## 7. 已知风险

1. **音频解码错误可能拖累进程**：ffmpeg 单实例双输出，若解码音频时发生致命写错误会连带视频。概率低（decode 错误通常为非致命告警），且 failover 机制会换候选；未做额外防护（防护需子进程拆分，违背单实例约束）。
2. **回环 TCP 的本机安全性**：127.0.0.1 随机端口 + 仅 accept 一个连接，本机恶意进程理论上可抢先连入（偷听/挤占）。实时观看场景威胁模型下接受；如需加固可加握手前导。
3. **无音频轨时首帧多一次 spawn**（几十毫秒，无网络等待）；真实网络断流也可能触发一次多余的"去音频重启"，代价同样是一次本地 spawn。
4. **音视频非 PTS 精确同步**：水位对齐在换分辨率/设备缓冲突变的极端场景可能出现可感知偏移；可通过手动切换同步/极速模式缓解。
5. **设备格式假设**：固定 48kHz/16bit/2ch；个别不支持该格式的输出设备会报"音频不可用"（不崩溃，可留设置项做重采样作为后续增强）。
6. **打包**：spec 已加 QtMultimedia 与 multimedia 插件，但**尚未跑过完整 vpk 冻结构建验证**，发布前需跑一次 `build_package.py` 并在干净机器验证音频。
7. PySide6 6.11 枚举相等比较异常（`err == QAudio.Error.NoError` 为 False）——已用 `.value` 数值比较规避，升级 PySide6 时注意此处。

## 8. 生命周期对照（需求第七条）

| 场景 | 行为 | 实现点 |
|---|---|---|
| 启动直播 | 视频、音频同时启动 | `_start_playback` |
| failover 换候选 | 音频随本次尝试拆除重建 | `_teardown_playback` + `_start_playback` |
| OCR 暂停 | 声音继续 | audio 与 OCR 无任何依赖 |
| 窗口最小化 | 声音继续 | 无最小化钩子 |
| 关闭窗口 | 停止音频并退出 | `closeEvent` → `_disconnect` → `audio.stop()` |
| 直播断线 | 音频跟随现有视频生命周期（本阶段不新增视频自动重连）；`AudioService.restart()` 已预留 | `service.restart()` |
