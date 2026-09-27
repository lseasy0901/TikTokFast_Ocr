# tools/ocr_cpu_bench — Phase 1：PP-OCRv6 CPU 可行性验证

完全隔离的实验目录。**不属于生产链路**：`ocr/` 与 `gui/` 都不 import 这里，
这里也不改任何生产行为（只读地 import 生产模块来复用定位/后处理规则）。

## Scope Lock

IN SCOPE
- 只读检查当前 PP-OCRv6 GPU 生产实现，输出实现摘要
- 建立用 **同一份模型权重**、CPU device、无任何 GPU 回退的独立 benchmark
- 在同一批真实图像上跑 GPU 与 CPU，比对精度指标与逐帧差异
- 量 CPU 的加载/预热/单张/分位数延迟，以及线程数敏感性

OUT OF SCOPE（本阶段不做）
- 不修改 `ocr/worker.py`、`ocr/room_code.py`、`ocr/temporal.py`、
  生产 PP-OCRv6 engine、GUI、FFmpeg/Stream、OCR 调度、ROI 逻辑
- 不替换生产依赖，不删除 CUDA/GPU 依赖，不改 `requirements.txt`
- 不改变任何生产行为；不做 CPU 路径的正式接入（那是后续阶段）

## 文件

| 文件 | 作用 |
|---|---|
| `dataset_adapter.py` | 只读适配 `LiveLens-OCR-Dataset-*`（自动搜索 / 显式指定） |
| `device_engine.py` | 按 device 构造引擎；复用生产加载代码，只换 `device` |
| `bench_device.py` | 单设备跑一遍数据集 → JSON（精度 + 延迟） |
| `compare_devices.py` | 两份 JSON → 精度/一致性/延迟对比 + markdown 报告 |
| `run_phase1.py` | 编排：GPU 子进程 → CPU 子进程 → 对比 |
| `thread_sweep.py` | `OMP_NUM_THREADS` 对 CPU 推理耗时的影响 |
| `results/` | 产物（JSON + 报告） |

## 运行

```bash
# 全部（GPU baseline + CPU + 对比报告）
python tools/ocr_cpu_bench/run_phase1.py

# 常用参数
python tools/ocr_cpu_bench/run_phase1.py --dataset "<数据集目录>" --rounds 5 --warmup 10
python tools/ocr_cpu_bench/run_phase1.py --skip-gpu          # 无 GPU 环境：报告记为 unavailable
python tools/ocr_cpu_bench/run_phase1.py --skip-cpu --skip-gpu   # 只用现成结果重出报告
python tools/ocr_cpu_bench/run_phase1.py --omp-threads 1         # 复现单线程慢档

# 线程敏感性
python tools/ocr_cpu_bench/thread_sweep.py --values unset,1,2,4,8,20
```

## 三个必须遵守的测量前提

1. **同一份模型权重**
   两个设备都必须解析到同一个 `model_dir`。报告里比对
   `inference.pdparams` 的 sha256，不一致就明确写出来。
   （注意：开发目录 `D:\new_model\...` 只是兜底，只要
   `runtime/ppocr/PP-OCRv6_medium_rec_infer` 存在，生产与基准用的都是**随包**那份。）

2. **CPU 必须真的看不见 GPU**
   `CUDA_VISIBLE_DEVICES=-1` 在 `import paddle` 之前生效，构建后再核对
   `paddle.device.cuda.device_count() == 0`；不等于 0 直接失败，
   而不是"大概是 CPU 吧"。这一条把"没有偷偷回退 GPU"变成可验证的断言。

3. **父进程不能 import 过 paddle**（实测踩到的坑）
   父进程只要 `import paddle`，即使随后完全闲置，它拉起的子进程 CPU 推理
   会慢 3 倍（p50 1569ms → 497ms 的反向差值）。原因是 paddle 在 import 时把
   `OMP_NUM_THREADS=1` 写进自己的 `os.environ`，子进程继承后 OpenMP 在加载时
   读到 1，oneDNN 按单线程初始化。
   `run_phase1.py` 自己不 import paddle，并且默认把子进程环境里的
   `OMP_NUM_THREADS` 丢掉；`bench_device.py` 单独跑时会记录并提示这件事。
   要测单线程配置请显式 `--omp-threads 1`。

## 关于 GT 的口径（决定报告怎么读）

数据集 `labels.csv` 的 `ground_truth` 是采集时 **GPU 生产链路的原始读法**，
不是人工标注（数据集 README 自己也写明了）。因此：

- GPU 侧「对 GT 的准确率」接近满分是**自证**，不能读成"GPU 准确率 100%"；
- 它真正的价值是**跨设备一致性**：同一批图上 CPU 的读法若与 GPU 不同，
  会直接落在指标上并被逐帧 diff 清单列出；
- 绝对识别能力需要一个**有独立人工标注**的数据集，本阶段没有，
  报告里不得把一致性数字说成准确率。
