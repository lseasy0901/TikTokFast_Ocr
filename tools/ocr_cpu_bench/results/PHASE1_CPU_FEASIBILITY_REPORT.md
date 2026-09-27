# Phase 1 — PP-OCRv6 CPU 可行性验证报告

生成时间：2026-09-27T05:15:31

## 0. 被测对象与环境

**GPU baseline**

- model_dir: `D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ppocr\PP-OCRv6_medium_rec_infer`
- inference.pdiparams: 76465087 bytes, sha256 `1b01c79a914587933f615569e75de54f2e638ebb5d3f3b3c1b38c24ede8c7319`
- paddle 3.3.0 / paddlex 3.7.2 / python 3.11.9
- paddle 编译含 CUDA: True, 本进程可见 GPU: 1, CUDA_VISIBLE_DEVICES=<unset>
- engine backend_status: {"model_name": "PP-OCRv6_medium_rec", "model_dir": "D:\\TIikTok_Ocr\\DouyinLowLatencyViewer\\runtime\\ppocr\\PP-OCRv6_medium_rec_infer", "device": "gpu", "batch_size": 1, "init_ms": 744.1213999409229, "closed": false}

**CPU**

- model_dir: `D:\TIikTok_Ocr\DouyinLowLatencyViewer\runtime\ppocr\PP-OCRv6_medium_rec_infer`
- inference.pdiparams: 76465087 bytes, sha256 `1b01c79a914587933f615569e75de54f2e638ebb5d3f3b3c1b38c24ede8c7319`
- paddle 3.3.0 / paddlex 3.7.2 / python 3.11.9
- paddle 编译含 CUDA: True, 本进程可见 GPU: 0, CUDA_VISIBLE_DEVICES=-1
- engine backend_status: {"model_name": "PP-OCRv6_medium_rec", "model_dir": "D:\\TIikTok_Ocr\\DouyinLowLatencyViewer\\runtime\\ppocr\\PP-OCRv6_medium_rec_infer", "device": "cpu", "batch_size": 1, "init_ms": 513.7506000464782, "closed": false}
- CPU 隔离: isolated=True, 可见 GPU=0

**同一模型校验：通过**

## 1. 数据集

- 目录：`C:\Users\ZhuanZ（无密码）\Documents\LiveLens-OCR-Dataset-20260927-043111`
- 样本：107 条，可用图像 107 张，缺图 0 张
- GT 长度分布：{'6': 107}，游戏档：['valorant']
- 标注口径：{"ground_truth": "PP-OCRv6 原始读法（本测试阶段不做人工标注）", "raw_text": "PP-OCRv6 原始读法，未经任何纠正/美化", "confidence": "现有链路未把逐帧置信度传到 worker，本列为空", "filter": "只接受 aaa123, bbbb123, aaaa1234"}

> **GT 口径警告**：本数据集的 ground_truth 是采集时 GPU 生产链路的原始读法，不是人工标注。因此「对 GT 的准确率」度量的是**与 GPU 的一致性**，不能当作绝对识别能力。跨设备的真实结论看第 3 节的逐帧差异清单。

## 2. 精度对比（同一批图、同一模型、同一后处理规则）

| 指标 | GPU | CPU | 差异 |
|---|---|---|---|
| 样本数 total | 107 | 107 |  |
| 原始读法 exact_match | 107 | 107 |  |
| 原始读法 exact_match_rate | 100.00% | 100.00% |  |
| 字符准确率 char_accuracy | 100.00% | 100.00% |  |
| 原始 no_read（空输出） | 0 | 0 |  |
| 原始 wrong_length | 0 | 0 |  |
| 原始 invalid_format | 0 | 0 |  |
| 原始 wrong_code（长度格式对但内容错） | 0 | 0 |  |
| 校验后 code_exact | 107 | 107 |  |
| 校验后 wrong_code | 0 | 0 |  |
| 校验后 no_read（无结果） | 0 | 0 |  |
| 做过纠正的帧 | 0 | 0 |  |
| 推理异常 error | 0 | 0 |  |
| 游戏冲突帧 | 0 | 0 |  |

## 3. 跨设备一致性（CPU 是否保持 GPU 的识别能力）

- 共同样本：107
- 原始读法一致：107 / 107（100.00%）
- 最终房间号一致：107 / 107（100.00%）
- 其中「一设备对、另一设备错」的样本：0 条

原始读法逐帧完全一致——CPU 未改变任何一帧的识别内容。

最终房间号逐帧完全一致。

## 4. 延迟对比

单次推理 = `engine.recognize()` 一次调用（即生产 300ms 识别间隔里的那一次），不含图像解码。

| 指标 | GPU | CPU |
|---|---|---|
| 模型加载 load_ms | 744 | 514 |
| 预热总耗时 warmup_total_ms | 570 | 4923 |
| 推理 P50 (ms) | 8.78 | 497.35 |
| 推理 P95 (ms) | 15.62 | 527.06 |
| 推理 P99 (ms) | 20.32 | 531.31 |
| 推理 Max (ms) | 25.47 | 541.12 |
| 推理 Mean (ms) | 9.72 | 495.10 |
| 推理 Min (ms) | 7.30 | 397.77 |
| 推理 标准差 (ms) | 2.68 | 22.39 |
| 推理样本数 | 321 | 321 |

### 预热曲线（单张首次调用的算子初始化代价）

- GPU: 首次 449.6ms → 末次 9.4ms（共 10 次，合计 570ms）
- CPU: 首次 592.8ms → 末次 473.0ms（共 10 次，合计 4923ms）

### CPU 延迟在 300ms 间隔下的余量

- P95 527.1ms 占 300ms 间隔的 176%
- P99 531.3ms 占 177%；Max 541.1ms 占 180%

> **注意**：最慢一次 541.1ms 已超过 300ms 识别间隔。单帧超时不会阻塞视频管线（OCR 在工作线程、丢旧帧优先），但会让该帧的识别机会丢失，实时性需要按 P95/P99 而不是均值评估。

