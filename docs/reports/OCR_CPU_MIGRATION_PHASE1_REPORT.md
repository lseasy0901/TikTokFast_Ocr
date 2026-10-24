# LiveLens OCR 架构迁移 · Phase 1 报告
# PP-OCRv6 CPU 可行性验证

日期：2026-09-27
范围：只验证，不改生产。所有新增文件在 `tools/ocr_cpu_bench/`。

机器生成的原始报告：`tools/ocr_cpu_bench/results/PHASE1_CPU_FEASIBILITY_REPORT.md`
原始数据：`results/{gpu_run,cpu_run,compare,thread_sweep}.json`

---

## 结论（先说结果）

**精度：CPU 完全保住了 PP-OCRv6 的识别能力。**
107 帧真实样本上，CPU 与 GPU 的**原始读法逐帧完全一致（107/107），
最终房间号也逐帧完全一致（107/107）**，零纠正、零冲突、零异常。
CPU 没有改变任何一帧的识别内容 —— 不是"接近"，是逐字符相同。

**延迟：CPU 慢了约 57 倍，单次推理已超过 300ms 识别间隔。**

| | GPU | CPU | 倍数 |
|---|---|---|---|
| 单次推理 P50 | 8.8 ms | 497.4 ms | 57x |
| P95 | 15.6 ms | 527.1 ms | 34x |
| Max | 25.5 ms | 541.1 ms | 21x |
| 模型加载 | 744 ms | 514 ms | CPU 略快 |

**所以：可行性成立，但需要产品决策 —— 不是"能不能跑"，而是"节奏降多少"。**
300ms 的识别间隔在 CPU 上不可能维持，实际节拍会掉到约 1.25 次/秒
（GPU 是 3.2 次/秒）。详见第 5 节。

---

## 1. GPU 当前实现摘要（只读检查，未修改）

生产 OCR 后端是 `ocr/ppocr_engine.py` 的 `PPOCREngine`，它是**唯一**知道
PP-OCRv6 存在的地方；识别规则（归一化/纠正/格式校验/仲裁/时序裁决）
一行都不在那里。

| 项目 | 当前实现 |
|---|---|
| 模型名 | `PP-OCRv6_medium_rec`（PaddleX 官方名，须与 `inference.yml` 一致） |
| 模型权重目录 | `runtime/ppocr/PP-OCRv6_medium_rec_infer`（**随包**那份） |
| 权重 | `inference.pdiparams` 76,465,087 bytes<br>sha256 `1b01c79a914587933f615569e75de54f2e638ebb5d3f3b3c1b38c24ede8c7319` |
| 定位顺序 | ① `DLV_PPOCR_MODEL_DIR` ② `<base>/runtime/ppocr/PP-OCRv6_medium_rec_infer` ③ 仅非冻结环境兜底 `D:\new_model\PP-OCRv6_medium_rec_infer`。冻结环境**不回退开发路径** |
| 运行时 | paddle 3.3.0（CUDA 编译版）+ paddlex 3.7.2（源码环境实测版本） |
| device | 硬编码 `"gpu"`（模块常量 `_DEVICE`），**没有 CPU 路径** |
| engine / batch | `paddle_static` / `batch_size=1`（逐帧，不做批处理） |
| CUDA 依赖 | `_ensure_cuda_dlls()` 把 `site-packages/nvidia/cu13/bin` 加进 PATH 与 `os.add_dll_directory` —— 这是目前**必须让用户装 NVIDIA 生态**的直接来源 |
| GPU 前置校验 | `_assert_gpu_available()`：必须 CUDA 编译版且 `device_count() > 0`，否则抛 `PPOCREngineError`。**明确不回退** CPU/Tesseract |
| 加载时机 | 构造时加载（不是首次推理时），使初始化失败只报一次错 |
| 输入契约 | `uint8` BGR `(H, W, 3)`；**不做任何预处理**（灰度/缩放/归一化全在模型内部） |
| 推理调用 | `self._model.predict(image, batch_size=1)` |
| 输出 | 从结果对象取 `rec_text`（`_result_text`，兼容多版本字段名）、`rec_score`（诊断用） |
| 对外契约 | `iter_readings()` / `recognize()` / `shutdown()` —— 与 `OCREngine` 完全相同，替换引擎不需要改 worker |
| 线程归属 | 模型句柄只在创建它的线程内使用（与常驻 Tesseract 同一条约束） |
| 释放 | `del model` + `gc.collect()` + `paddle.device.cuda.empty_cache()`（归还显存） |

**与本次迁移直接相关的两点**：
1. CUDA 依赖是**显式且集中**的 —— 只在 `_ensure_cuda_dlls()` 与 `_DEVICE` /
   `_assert_gpu_available()` 三处。这是好消息：CPU 化的改动面很小。
2. 生产实际用的是**随包模型**，不是 `D:\new_model`。基准与生产指向同一份
   权重（sha256 已核对，两份结果一致）。

---

## 2. 基准设计：怎么保证"只差一个 device"

对比要成立，必须让除 device 之外的一切都相同。做法：

**复用生产的加载代码，不另写一份。**
`tools/ocr_cpu_bench/device_engine.py` 在**实验进程内**做两处运行时覆盖
（生产源码一个字节都没改）：

```
ocr.ppocr_engine._DEVICE              -> "cpu" / "gpu"
PPOCREngine._assert_gpu_available     -> 该设备对应的前置校验
```

于是 `__init__ / _load_model / _infer / iter_readings / recognize / shutdown`
全是**生产实现**，唯一变量是 device 字符串。另写一份加载器会有两处独立演化，
对比就不再是"只换 device"。

**前置校验被反过来用，不是关掉。**
`device="cpu"` 时校验的是**必须零块可见 GPU**（`device_count() == 0`），
否则直接抛错。也就是说"没有偷偷回退 GPU"是一条**会在违规时失败的硬约束**，
不是一句注释。实际用 `CUDA_VISIBLE_DEVICES=-1` 在 `import paddle` 之前生效。

**后处理原样复用**：`RoomCodeRecognizer`（严格校验/逐位纠正/仲裁）直接
import 生产实现跑第二遍，得到最终房间号。

**输入逐字节相同**：107 张图在计时前一次性解码缓存，GPU 与 CPU 看到的是
同一批 ndarray；图像解码不计入 OCR 耗时。

**同一模型**：报告里比对 `inference.pdiparams` 的 sha256（本次：一致）。

失败路径也是显式的：本次环境 GPU 可用，所以如实跑了 GPU baseline，
没有出现 `GPU baseline unavailable`。

---

## 3. 数据集

`C:\Users\ZhuanZ（无密码）\Documents\LiveLens-OCR-Dataset-20260927-043111`

- 107 条已标注样本，107 张可用图，0 张缺图
- 全部 6 位（`LLLDDD` → 游戏档 `valorant`），采样间隔 300ms
- 只读适配（`tools/ocr_cpu_bench/dataset_adapter.py`），没有改动数据集本身

> **GT 口径必须说清楚**：该数据集的 `ground_truth` 是采集时 **GPU 生产链路的
> 原始读法**，不是人工标注（数据集 README 自己写明了）。
> 因此"对 GT 的准确率"度量的是**与 GPU 的一致性**，**不能**当作绝对识别能力
> 读成"CPU 准确率 100%"。真正的结论在第 4 节的逐帧一致性。

---

## 4. 精度对比

指标定义见 `compare_devices.py` 顶部；规范化只做一次（去空白 + 大写），
两个设备共用同一条。

| 指标 | GPU | CPU |
|---|---|---|
| 样本数 | 107 | 107 |
| 原始读法 exact_match | 107（100%） | 107（100%） |
| 字符准确率 | 100.00% | 100.00% |
| 原始 no_read / wrong_length / invalid_format / wrong_code | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| 校验后 code_exact | 107 | 107 |
| 校验后 wrong_code | 0 | 0 |
| 校验后 无结果 | 0 | 0 |
| 做过纠正的帧 | 0 | 0 |
| 推理异常 / 游戏冲突 | 0 / 0 | 0 / 0 |

**跨设备一致性**

- 原始读法一致：**107 / 107（100.00%）**
- 最终房间号一致：**107 / 107（100.00%）**
- 「一设备对、另一设备错」的样本：**0 条**
- 差异清单：**空**（原始读法、最终房间号两张 diff 表都没有行）

### 这个 100% 不是"对比功能没生效"

做了一次负对照：人为把 CPU 结果的第 8 帧读法改成 `BST602`、第 10 帧最终码
改成 `ZZZ999`，重跑对比 →

```
原始读法 exact_match      107 | 106
字符准确率 char_accuracy  100.00% | 99.22%
校验后 code_exact         107 | 106
校验后 wrong_code         0   | 1
原始读法一致：106 / 107（99.07%）
最终房间号一致：106 / 107（99.07%）
| 000011 | CSW957 | CSW957 | BST602 |
| 000013 | CSW957 | CSW957 | ZZZ999 |
```

对比管线能精确捕获并逐条列出差异。所以真实的 0 条差异是测量结果，不是空转。

同时核对过两份结果确实来自两个独立进程：GPU 进程可见 1 块 GPU、CPU 进程
可见 0 块、`backend_status.device` 分别为 gpu/cpu，同一张图的单次耗时
一个是 ~9ms、一个是 ~500ms。

### 覆盖面的诚实说明

107 帧全部是同一格式（6 位 `LLLDDD`）、全部清晰、全部一次读对。
它**没有**覆盖：模糊/遮挡样本、7 位与 8 位格式、需要逐位纠正的难例、
以及 CPU 与 GPU 浮点差异可能显现的**低置信度边界样本**。

所以准确的说法是：**在这批清晰样本上 CPU 与 GPU 逐字符等价**；
CPU 是否在难例上也等价，需要一个人工标注、含难例的数据集才能回答。
这是下次验证该补的东西，本报告不作超出数据的推断。

---

## 5. 延迟测试

单次推理 = `engine.recognize()` 一次调用，不含图像解码；
n = 321（107 张 × 3 轮），预热 10 次后开始计时。

| 指标 | GPU | CPU |
|---|---|---|
| 模型加载 load | 744 ms | 514 ms |
| 预热总耗时（10 次） | 570 ms | 4,923 ms |
| 预热：首次 → 末次 | 449.6 → 9.4 ms | 592.8 → 473.0 ms |
| 单张 Min | 7.3 ms | 397.8 ms |
| **P50** | **8.78 ms** | **497.35 ms** |
| **P95** | 15.62 ms | **527.06 ms** |
| **P99** | 20.32 ms | 531.31 ms |
| **Max** | 25.47 ms | **541.12 ms** |
| Mean | 9.72 ms | 495.10 ms |
| 标准差 | 2.68 ms | 22.39 ms |

两点值得注意：

1. **CPU 的加载反而更快**（514ms vs 744ms）—— 慢的是推理，不是初始化。
2. **CPU 的预热很贵**：10 次预热就花掉 4.9 秒（GPU 0.57 秒）。若每次
   "开始识别"都新建引擎（现有生命周期就是这样），CPU 下用户要等
   约 0.5s 加载 + 预热才进入稳态。

### 5.1 落到生产节拍上

> 这一小节是**推算**，不是端到端实测：输入是上面量到的单次推理耗时，
> 加上 `OCRWorker.run()` 的循环结构。没有跑真实的 GUI + 视频 + worker 链路。

`OCRWorker.run()` 的结构是 `识别一次 → wait(interval)`，所以
**实际周期 = 推理耗时 + 等待间隔**，不是 max(两者)：

| | 稳态（间隔 300ms） | 确认窗口（间隔 100ms） |
|---|---|---|
| GPU | 10 + 300 ≈ 310 ms → 3.2 次/秒 | 10 + 100 ≈ 110 ms |
| CPU | 497 + 300 ≈ **797 ms** → **1.26 次/秒** | 497 + 100 ≈ 597 ms |

时序规则要求 `min_agreement=2`（`ocr/temporal.py`），所以**首次出结果的
时间**：

- GPU：约 0.12 秒（第 1 次 ~10ms，确认窗口 100ms 后再来一次）
- CPU：约 **1.1 秒**（第 1 次 ~0.5s，再等 100ms + 第 2 次 ~0.5s）

即：**识别频率掉到约 1/2.5，首次命中时间从 ~0.1s 变成 ~1.1s。**
这不会让视频卡顿（OCR 在工作线程、丢旧帧优先、单槽覆盖），
但用户感知是"识别明显变慢、要盯久一点才出结果"。

### 5.2 线程数敏感性（本机 20 逻辑核）

`thread_sweep.py` 结果（每档 n=30）：

| OMP_NUM_THREADS | P50 (ms) | P95 (ms) | Max (ms) | 相对 unset |
|---|---|---|---|---|
| **unset（生产常态）** | **499** | 527 | 527 | 1.00x |
| 1 | 1586 | 1709 | 1875 | **3.18x** |
| 2 | 855 | 933 | 946 | 1.71x |
| 4 | 617 | 784 | 829 | 1.24x |
| 8 | 788 | 880 | 908 | 1.58x |
| 20 | 511 | 527 | 535 | 1.02x |

**这是本次验证里最值得记住的一条**：CPU 推理能否跑在快档，取决于
`OMP_NUM_THREADS`，而它必须在 OpenMP 运行库**加载时**就是对的。
1 线程比 20 线程慢 3.2 倍；4 线程已经拿到大部分收益（617ms），
而 8 线程（788ms）反而比 4 线程更慢 —— 对这个尺寸的输入，
线程数超过某一点后是超额开销而不是加速。

对**部署**的含义：如果用户机器上别的软件把 `OMP_NUM_THREADS` 设成了 1，
CPU 推理会直接掉进 1.6 秒/次的慢档。是否要在启动时显式指定线程数，
是一个需要在后续阶段决定的问题。

对**测量**的含义：见第 7 节。

---

## 6. 可行性判断

| 维度 | 判断 |
|---|---|
| 精度是否保持 | **是**（本批 107 帧逐字符等价，含最终房间号） |
| 模型/权重是否可复用 | **是**，同一份 `inference.pdiparams`，无需转换 |
| 是否需要改生产链路 | **否**（本次零改动；未来只需在 `_DEVICE` 与 `_ensure_cuda_dlls` 处开分支） |
| CPU 是否需要 CUDA 依赖 | **否**（`CUDA_VISIBLE_DEVICES=-1` 下正常加载与推理） |
| 300ms 间隔是否可行 | **否**（497ms 单次推理，是间隔的 1.66 倍） |
| 去掉 NVIDIA 依赖是否可行 | **是**，代价是识别节奏降到约 1.25 次/秒 |

**一句话**：技术上完全可行，唯一真正的代价是**实时性**——
从"约 3.2 次/秒、0.1 秒出结果"变成"约 1.25 次/秒、1.1 秒出结果"。

需要产品侧决策的选项（本次**不做**，仅列出）：

1. **接受慢节奏**，把识别间隔从 300ms 放宽到 500–600ms，让 CPU 稳态周期
   接近原来的感知；代价是首次命中时间进一步变长。
2. **降模型档位**（如 PP-OCRv6 small / mobile 档）或**缩小输入**，
   换回实时性；代价是要重新验证精度。
3. **GPU 优先 + CPU 兜底**：有 NVIDIA 走 GPU（现状），没有则走 CPU。
   这与当前"绝不回退"的设计取向相反，但正是"面向普通 Windows 用户"的
   目标所需要的形态。

---

## 7. 过程中踩到的一个坑（对后续验证很重要）

第一次量 CPU 拿到 **p50 ≈ 1600ms**，但单独量同一张图只有 ~500ms，
差 3 倍。追下去发现**不是机器负载，是测量方法问题**：

> 父进程只要 `import paddle`，即使随后完全闲置（实测 5 秒内
> `process_time` 增量 0.00s、仅 1 个线程），它拉起的**子进程**跑同样的
> CPU 推理就会慢 3 倍（p50 1569ms vs 497ms），反复可复现。

原因不是 CPU 争抢：paddle 在 import 时会把 `OMP_NUM_THREADS=1` 写进自己的
`os.environ`，子进程继承该变量，而 OpenMP 在**加载时**读到 1，oneDNN 就按
单线程初始化。逐项排除过：单独加载 `nvcuda.dll` 无影响、
`MKL_NUM_THREADS=1` 无影响、`cpu_threads=20` 也压不住 —— 只有
"启动时 OMP_NUM_THREADS 是不是 1"这一个变量。

因此 `run_phase1.py` **自己不 import paddle**，并且默认把子进程环境里的
`OMP_NUM_THREADS` 丢掉（丢掉时会打印提示）。`bench_device.py` 单独跑时会
记录并提示这件事。第 5 节所有数字都来自干净父进程。

这条对**未来所有 OCR 性能测量**都适用：**别从 import 过 paddle 的进程里
起性能测量的子进程**，否则会量到 3 倍的假象。

---

## 8. 范围合规

IN SCOPE 全部完成：只读实现摘要 ✅ / CPU 独立基准 ✅ / 真实数据集 ✅ /
精度对比 ✅ / 延迟测试（含 P50·P95·P99·Max）✅

OUT OF SCOPE —— 均未触碰（已用文件时间戳核对，生产目录下最近 60 分钟内
零改动）：

- ✅ 未修改 `ocr/worker.py`、`ocr/room_code.py`、`ocr/temporal.py`、
  生产 `PPOCREngine`、GUI、FFmpeg/Stream、OCR 调度、ROI 逻辑
- ✅ 未替换生产依赖、未删除 CUDA/GPU 依赖、未改 `requirements.txt`
- ✅ 未改变任何生产行为

所有新增文件（仅 `tools/ocr_cpu_bench/`）：

```
README.md               Scope Lock 与三条测量前提
dataset_adapter.py      只读数据集适配
device_engine.py        按 device 构造引擎（复用生产加载代码）
bench_device.py         单设备跑一遍 -> JSON
compare_devices.py      两份 JSON -> 指标 + 逐帧差异 + 报告
run_phase1.py           编排（GPU 子进程 -> CPU 子进程 -> 对比）
thread_sweep.py         线程数敏感性
results/                原始 JSON 与机器生成的报告
```

复现：

```bash
python tools/ocr_cpu_bench/run_phase1.py            # 全量（约 4 分钟）
python tools/ocr_cpu_bench/thread_sweep.py          # 线程敏感性
```

**本阶段在此停止**，未开始任何生产接入。下一步需要先决定第 6 节的
产品取向（接受慢节奏 / 降模型档位 / GPU 优先+CPU 兜底），再进入下一阶段。
