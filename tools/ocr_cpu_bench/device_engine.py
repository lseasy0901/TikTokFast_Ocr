# -*- coding: utf-8 -*-
"""按设备构造 PP-OCRv6 引擎——**复用生产加载代码**，只换 device。

为什么是"打补丁"而不是"另写一份加载器"
------------------------------------
要证明「CPU 与 GPU 的差异只来自 device」，最可信的做法是让两个设备
**跑同一段加载代码**。ocr/ppocr_engine.py 的 `_load_model()` 已经是
那份代码（模型定位、paddle 导入、create_model 参数、结果对象处理
全都在里面）。另写一份拷贝会有两处独立演化，对比就不再是「只换 device」。

因此本模块在**实验进程内**做两处运行时覆盖（生产源码一个字节都不改）：

    ocr.ppocr_engine._DEVICE                <- "cpu" / "gpu"
    PPOCREngine._assert_gpu_available       <- 该设备对应的前置校验

于是 PPOCREngine.__init__ / _load_model / _infer / iter_readings /
recognize / shutdown 全是生产实现，唯一变量就是 device 字符串。

前置校验被**反过来**用（不是简单地禁用）
------------------------------------
生产版校验的是「GPU 必须可用」。本模块：
    device="gpu" -> 沿用生产校验（CUDA 编译版 + 至少一块可见 GPU）
    device="cpu" -> 换成**反向校验**：必须**零块可见 GPU**
                    （paddle.device.cuda.device_count() == 0），
                    否则直接抛错。
它不是把检查关掉，而是把"不许偷偷用 GPU"变成一条会在
设备可见时就失败的硬约束——这样"CPU 跑的就是 CPU"是可验证的，
而不是靠一句注释。

零可见 GPU 是靠 `CUDA_VISIBLE_DEVICES=-1` 在**import paddle 之前**生效的
（见 prepare_process）。

一个必须记住的坑：不要让「已经 import 过 paddle 的进程」当父进程
------------------------------------------------------------------
实测（2026-09-27，本机）：父进程只要 `import paddle`，即使随后完全闲置
（process_time 增量 0.00s / 5s，仅 1 个线程），它拉起的**子进程**跑同样的
CPU 推理会慢 3 倍（p50 1569ms vs 497ms），且反复可复现。

原因不是 CPU 争抢，而是 paddle 在 import 时把 `OMP_NUM_THREADS=1`
写进了自己的 os.environ，子进程继承了这个变量；OpenMP 运行库在
**加载时**读到 1，oneDNN 就按单线程初始化（实测 1 线程 1604ms、
2 线程 860ms、4 线程 572ms、20 线程 506ms）。

因此：GPU 与 CPU 基准都必须由**从未 import paddle 的父进程**启动，
否则量到的是这个继承效应，不是真实推理耗时。
`run_phase1.py` 遵守这条约束。
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: 让 CPU 进程看不见任何显卡的环境变量值。
#: 实测("-1" 与 "" 都能隐藏)，取 -1：语义明确，且不是"空值=未设置"的歧义。
CUDA_HIDDEN_ENV = {"CUDA_VISIBLE_DEVICES": "-1"}

#: 进程启动时（OpenMP 加载前）的 OMP_NUM_THREADS 快照，供报告使用。
_OMP_AT_START: Optional[str] = None


def cuda_device_count() -> int:
    """当前进程可见的 CUDA 设备数（paddle 视角）。不可用时返回 0。"""
    try:
        import paddle
    except Exception:  # noqa: BLE001
        return 0
    try:
        return int(paddle.device.cuda.device_count())
    except Exception:  # noqa: BLE001
        return 0


def prepare_process(device: str, omp_threads: Optional[str] = None) -> dict:
    """在 `import paddle` **之前**设置好本进程的运行环境。

    参数：
        device: "cpu" 时把 CUDA 设备全部隐藏
        omp_threads: None（默认）= 不动 OMP_NUM_THREADS，保持进程继承到的值。
                     这是**生产口径**：GUI 进程在模型加载前自己 import 过
                     paddle，OpenMP 按默认核心数初始化。
                     显式给值（"1"/"4"/...）用于延迟敏感性实验。

    返回进程启动时的环境快照（进报告）。
    """
    global _OMP_AT_START
    _OMP_AT_START = os.environ.get("OMP_NUM_THREADS")
    if device == "cpu":
        os.environ.update(CUDA_HIDDEN_ENV)
    if omp_threads is not None:
        os.environ["OMP_NUM_THREADS"] = str(omp_threads)
    return {
        "omp_num_threads_at_process_start": _OMP_AT_START,
        "omp_num_threads_forced": omp_threads,
        "cuda_visible_devices_at_start": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
        "parent_had_imported_paddle": bool(os.environ.get("OMP_NUM_THREADS") == "1"
                                           and _OMP_AT_START == "1"),
    }


def verify_isolation(device: str) -> dict:
    """构建完成后核对隔离是否真的成立（只有 cpu 需要，且必须成立）。

    异常：
        RuntimeError: device="cpu" 但本进程仍看得见 GPU
                      —— 此时的结果不能用，直接失败而不是"大概是 CPU 吧"。
    """
    import paddle  # 惰性：调用点已经在 paddle 导入之后

    count = cuda_device_count()
    info = {
        "isolated": device != "cpu" or count == 0,
        "visible_cuda_devices": count,
        "cuda_visible_devices_env": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
        "paddle_compiled_with_cuda": bool(safe_compiled_with_cuda(paddle)),
        "paddle_device_place": None,
    }
    try:
        info["paddle_device_place"] = str(paddle.device.get_device())
    except Exception:  # noqa: BLE001
        pass
    if device == "cpu" and count != 0:
        raise RuntimeError(
            "CPU 基准要求零块可见 GPU，但构建后仍可见 %d 块——"
            "结果不能当作 CPU 数据。" % count
        )
    return info


def safe_compiled_with_cuda(paddle) -> bool:
    try:
        return bool(paddle.device.is_compiled_with_cuda())
    except Exception:  # noqa: BLE001
        return False


def _assert_cpu_only(paddle) -> None:
    """device="cpu" 用的前置校验（替换生产版 _assert_gpu_available）。"""
    from ocr.ppocr_engine import PPOCREngineError

    try:
        count = int(paddle.device.cuda.device_count())
    except Exception:  # noqa: BLE001
        count = 0
    if count > 0:
        raise PPOCREngineError(
            "CPU 基准检测到 %d 块可见 GPU：本进程不满足 CPU 隔离条件，"
            "结果不能当作 CPU 数据（拒绝继续）。" % count
        )


def _assert_gpu_available(paddle) -> None:
    """device="gpu" 用的前置校验：直接复用生产实现（保证 baseline 与生产一致）。"""

    _PRODUCTION_ASSERT_GPU(paddle)


def build_engine(device: str, model_dir: Optional[str] = None):
    """构造一个指定 device 的 PP-OCRv6 引擎（复用生产加载代码）。

    参数：
        device: "cpu" 或 "gpu"
        model_dir: 显式模型目录；None 走生产定位顺序
                   （DLV_PPOCR_MODEL_DIR -> runtime/ppocr/ -> 开发目录）

    返回：
        (engine, patch_info) —— engine 是生产 PPOCREngine 实例；
        patch_info 记录本次覆盖了哪些符号，进报告以便复核。

    异常：
        ValueError: device 不是 cpu/gpu。
        PPOCRModelNotFoundError / PPOCREngineError: 与生产完全一致。
    """
    device = (device or "").strip().lower()
    if device not in ("cpu", "gpu"):
        raise ValueError(f"device 必须是 'cpu' 或 'gpu'，收到 {device!r}")

    import ocr.ppocr_engine as pe

    global _PRODUCTION_ASSERT_GPU
    if _PRODUCTION_ASSERT_GPU is None:
        # 记下生产版校验函数（staticmethod 解包成普通函数），供 gpu 分支原样回填。
        _PRODUCTION_ASSERT_GPU = pe.PPOCREngine.__dict__["_assert_gpu_available"].__func__

    original_device = pe._DEVICE
    pe._DEVICE = device
    if device == "cpu":
        pe.PPOCREngine._assert_gpu_available = staticmethod(_assert_cpu_only)
    else:
        pe.PPOCREngine._assert_gpu_available = staticmethod(_assert_gpu_available)

    engine = pe.PPOCREngine(model_dir)
    patch_info = {
        "patched_symbols": ["ocr.ppocr_engine._DEVICE",
                            "PPOCREngine._assert_gpu_available"],
        "production_device_constant": original_device,
        "effective_device": pe._DEVICE,
        "assert_mode": "cpu-only (零可见 GPU 才放行)" if device == "cpu"
                       else "production gpu check (原样)",
    }
    return engine, patch_info


#: 生产版 GPU 前置校验的原始函数（第一次 build_engine 时抓到）。
_PRODUCTION_ASSERT_GPU = None


def model_fingerprint(model_dir: str) -> dict:
    """模型身份快照：路径 + inference.pdiparams 的大小与 sha256。

    两个设备必须指向**同一个**模型文件，否则"同一模型"这条前提不成立。
    sha256 对 76MB 文件约几百毫秒，只在启动时算一次。
    """
    params = os.path.join(model_dir, "inference.pdiparams")
    info = {"dir": os.path.abspath(model_dir), "params_path": params}
    try:
        info["params_bytes"] = os.path.getsize(params)
    except OSError:
        info["params_bytes"] = None
    digest = hashlib.sha256()
    try:
        with open(params, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        info["params_sha256"] = digest.hexdigest()
    except OSError:
        info["params_sha256"] = None
    # 推理配置也一并记录：它决定预处理/后处理，模型换了它通常也换。
    for extra in ("inference.yml", "inference.json"):
        p = os.path.join(model_dir, extra)
        try:
            info["%s_bytes" % extra.replace(".", "_")] = os.path.getsize(p)
        except OSError:
            info["%s_bytes" % extra.replace(".", "_")] = None
    return info


def environment_fingerprint() -> dict:
    """运行时环境快照（版本、线程、机器）——报告里"在什么环境下测的"。"""
    import paddle

    info = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_logical": os.cpu_count(),
        "paddle_version": getattr(paddle, "__version__", None),
        "paddle_compiled_with_cuda": safe_compiled_with_cuda(paddle),
        "paddle_device_count": cuda_device_count(),
        "cuda_visible_devices_env": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
        "executable": sys.executable,
    }
    try:
        info["paddle_num_threads"] = int(paddle.get_num_threads())
    except Exception:  # noqa: BLE001
        info["paddle_num_threads"] = None
    try:
        info["paddle_device_place"] = str(paddle.device.get_device())
    except Exception:  # noqa: BLE001
        info["paddle_device_place"] = None
    try:
        import paddlex
        info["paddlex_version"] = getattr(paddlex, "__version__", None)
    except Exception:  # noqa: BLE001
        info["paddlex_version"] = None
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        info[var.lower()] = os.environ.get(var, "<unset>")
    return info
