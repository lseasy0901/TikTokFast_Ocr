# -*- coding: utf-8 -*-
"""
随包分发的运行时可执行文件路径解析 — 发布打包修复

背景：
    发布构建在客户机上没有 FFmpeg、也没有任何开发环境。此前 FFmpeg 是以裸命令
    ``ffmpeg`` 交给 subprocess 的，解析完全依赖 PATH；开发机上靠临时注入的开发者
    目录才能工作，客户机上则直接失败。

职责：
    统一解析随包分发的可执行文件路径，使 release 构建使用确定性的相对路径
    ``<MEIPASS>/runtime/<subdir>/<tool>.exe``，不依赖 PATH。

查找顺序：
    1. 环境变量覆盖（部署用，便于同一份构建适配不同部署）
    2. 冻结（PyInstaller）环境：``<MEIPASS>/runtime/...``，其次 ``<exe_dir>/runtime/...``
    3. 源码开发环境：``<项目根>/runtime/...``
    4. 仅限非冻结的开发环境，回退到裸命令（走 PATH）

冻结环境刻意不做 PATH 回退：客户机上并没有 FFmpeg，静默回退只会把一个明确的
「随包文件缺失」变成含糊的「找不到命令」，掩盖打包缺陷。

本模块只负责路径解析，不涉及 FFmpeg 参数或流水线行为。
"""

import os
import sys

__all__ = ["resolve_ffmpeg_path", "resolve_ffprobe_path"]

#: runtime/ 下的子目录名。ffmpeg 与 ffprobe 同处 runtime/ffmpeg/。
_FFMPEG_SUBDIR = "ffmpeg"

#: 部署覆盖用的环境变量名，与 DLV_LICENSE_SERVER_URL / SUSI_HELPER_PATH 同一模式。
_ENV_FFMPEG = "DLV_FFMPEG_PATH"
_ENV_FFPROBE = "DLV_FFPROBE_PATH"


def _exe_name(name: str) -> str:
    """Windows 下补 .exe 后缀，其余平台沿用裸名。"""
    return name + ".exe" if os.name == "nt" else name


def _project_root() -> str:
    """源码树根目录（本文件位于 <root>/utils/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_usable(path: str) -> bool:
    """存在且非空。

    runtime/ffmpeg/ffprobe.exe 在源码树里是一个 0 字节占位文件；把它当成
    「已找到」只会让调用方去执行一个空文件。空文件一律视作缺失。
    """
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _frozen_base_dirs():
    """冻结构建中可能承载 runtime/ 的目录，按优先级排列。

    返回空列表表示当前不是冻结环境，调用方应改走源码树路径。
    """
    if not getattr(sys, "frozen", False):
        return []

    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if exe_dir not in dirs:
        dirs.append(exe_dir)

    return dirs


def _resolve(name: str, subdir: str, env_var: str) -> str:
    """按上述顺序解析 ``runtime/<subdir>/<name>``。"""
    override = os.environ.get(env_var, "")
    if override.strip():
        return override.strip()

    frozen_dirs = _frozen_base_dirs()
    if frozen_dirs:
        candidates = [
            os.path.join(base, "runtime", subdir, _exe_name(name))
            for base in frozen_dirs
        ]
        for candidate in candidates:
            if _is_usable(candidate):
                return candidate
        # 随包文件缺失时仍返回确定性的首选位置：交给 subprocess 抛出
        # FileNotFoundError，由调用方既有的错误处理呈现，而不是静默走 PATH。
        return candidates[0]

    # 源码开发环境：优先用仓库内自带的 runtime/，便于与发布构建行为一致。
    dev_path = os.path.join(_project_root(), "runtime", subdir, _exe_name(name))
    if _is_usable(dev_path):
        return dev_path

    # 仅开发环境允许的 PATH 回退；冻结构建永远不会走到这里。
    return name


def resolve_ffmpeg_path() -> str:
    """解析 FFmpeg 可执行文件路径。"""
    return _resolve("ffmpeg", _FFMPEG_SUBDIR, _ENV_FFMPEG)


def resolve_ffprobe_path() -> str:
    """解析 ffprobe 可执行文件路径。

    ffprobe 仅用于可选的分辨率预探测；未随包分发时（发布构建即如此）返回
    确定性的缺失路径，调用方会回退到基于 FFmpeg 的检测方案。
    """
    return _resolve("ffprobe", _FFMPEG_SUBDIR, _ENV_FFPROBE)
