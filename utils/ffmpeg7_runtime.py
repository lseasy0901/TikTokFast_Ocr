# -*- coding: utf-8 -*-
"""FFmpeg 7 外部运行时：定位 + 校验 + 状态缓存（B5 裸流正式接入）。

背景
----
B5 裸流是增强 FLV 封装 HEVC，随包内置的运行时解不了（实测），只有外部
FFmpeg 7 级运行时可以。本模块只回答一个问题：**这台机器上有没有可用的
外部 FFmpeg 运行时，在哪，什么版本，为什么不可用。**

设计约束（对应 RELEASE-02-B5 任务书 Phase 3.B）
------------------------------------------------
- **不得只凭文件名或 ``os.path.isfile()`` 判定可用**：必须真正拉起进程、
  读回版本横幅、确认解码能力（hevc）与解复用能力（flv/live_flv）。
- **每一步探测都有超时**：任何一步卡住都不能把调用方（GUI）拖死。
- **结论缓存**：一次进程生命周期内只做一次探测（首次需要时惰性执行）。
  运行时在应用运行期间变更需重启应用才生效（已在下方定位规则中说明）。
- **用户可见信息与技术细节分离**：``reason`` 是稳定枚举，供 GUI 映射成
  一句人话；``detail`` 是给日志的技术细节（路径 / 版本 / 退出码 / stderr 尾）。

定位优先级（按序取第一个「存在且非空」的候选）
----------------------------------------------
1. 环境变量 ``DLV_FFMPEG7_EXE``（部署 / 排障覆盖，与 ``DLV_FFMPEG_PATH`` 同模式）
2. 冻结构建（PyInstaller）：``<MEIPASS>/runtime/ffmpeg7/ffmpeg.exe``
3. 冻结构建：``<exe_dir>/runtime/ffmpeg7/ffmpeg.exe``
4. 源码树：``<项目根>/runtime/ffmpeg7/ffmpeg.exe``
5. 用户级外部运行时目录：``%LOCALAPPDATA%/LiveLens/runtime/ffmpeg7/ffmpeg.exe``

第 5 条是「用户自备运行时」的约定落点：普通用户把 FFmpeg 7 运行时目录放到
该位置即可被自动发现，**不需要配置任何环境变量**。第 1 条只作为部署覆盖，
不是普通用户必须使用的配置方式。

注意：随包内置运行时位于 ``runtime/ffmpeg/``（不同子目录），**永远不会**成为
本模块的候选——裸流必须使用外部 FFmpeg 7 级运行时。

版本门禁（可确定、可测试）
--------------------------
能力探测（hevc + flv）**单独不足以**区分「FFmpeg 5/6」与「FFmpeg 7」——本项目
内置的 2023 自定义构建同样带 hevc 解码器与 flv 解复用器。因此版本门禁采用
「二者取其一」的确定性判据：

a) 版本串可解析出数字发布号（如 ``7.1.1``）→ 要求 ``major >= 7``；
b) 版本串是快照 / 自定义标识（如 ``N-127054-g9d3f0f2c58-20261001``）→
   要求版本横幅里的 ``libavcodec`` 主版本 ``>= 61``（= FFmpeg 7 一代）。

两条都拿不到（横幅既无数字发布号、又无 libavcodec 版本行）→ 判定为
「版本信息不可识别」并**拒绝**——这正好拦下内置那个把 lib 版本行裁掉的
自定义构建。残余限制：若用户提供的是**同样能自证 libavcodec >= 61** 的
非 7.x 构建，门禁会放行；裸流解码失败时由 ``FFmpegReader`` 的
``_stderr_decode_fail`` 快速击杀子进程并走**有界重启**路径
（RELEASE-02-B5-CLEANUP 起不再有旧路径可回退）——属可接受的降级路径。
"""

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from utils.runtime_paths import _frozen_base_dirs, _is_usable, _project_root

logger = logging.getLogger("DouyinLowLatencyViewer.ffmpeg7_runtime")

__all__ = [
    "Ffmpeg7Status",
    "candidate_paths",
    "find_existing_candidates",
    "probe",
    "status",
    "resolved_exe",
    "reset_cache",
    "reason_message",
    "REASON_OK",
    "REASON_NOT_FOUND",
    "REASON_NOT_EXECUTABLE",
    "REASON_PROBE_TIMEOUT",
    "REASON_PROBE_FAILED",
    "REASON_VERSION_UNREADABLE",
    "REASON_VERSION_TOO_OLD",
    "REASON_CAPABILITY_MISSING",
]

#: 运行时子目录名（与内置运行时的 ffmpeg 子目录区分开）。
_RUNTIME_SUBDIR = "ffmpeg7"
_EXE_NAME = "ffmpeg.exe"

#: 部署 / 排障用的覆盖环境变量（非普通用户必填项）。
_ENV_OVERRIDE = "DLV_FFMPEG7_EXE"

#: 用户级外部运行时目录名（与 utils/first_run.py、utils/update_state.py 同约定）。
_USER_DIR_NAME = "LiveLens"

#: 单次探测（拉起进程读版本 / 能力）的超时上限。
PROBE_TIMEOUT_S = 8.0

#: 可解析出数字发布号时要求的最低主版本。
MIN_MAJOR = 7

#: 版本串不可解析（快照 / 自定义标识）时使用的兜底判据：libavcodec 主版本。
#: 对应关系（FFmpeg 官方发布线）：5.x → 59，6.x → 60，7.x → 61，8.x → 62，
#: master 快照 → 更高。要求 >= 61 等价于"至少 FFmpeg 7 一代"。
#: 该行由官方构建在版本横幅中输出；若构建把 lib 版本行裁掉（例如本项目内置的
#: 2023 自定义构建），则"版本信息不可识别"——按任务书要求视为不可用。
MIN_AVCODEC_MAJOR = 61

REASON_OK = "ok"
REASON_NOT_FOUND = "not_found"
REASON_NOT_EXECUTABLE = "not_executable"
REASON_PROBE_TIMEOUT = "probe_timeout"
REASON_PROBE_FAILED = "probe_failed"
REASON_VERSION_UNREADABLE = "version_unreadable"
REASON_VERSION_TOO_OLD = "version_too_old"
REASON_CAPABILITY_MISSING = "capability_missing"

#: 供 GUI 映射的稳定人话（不暴露路径 / 环境变量 / 底层错误码）。
_REASON_TEXT = {
    REASON_NOT_FOUND: "未安装低延迟播放组件",
    REASON_NOT_EXECUTABLE: "低延迟播放组件无法启动",
    REASON_PROBE_TIMEOUT: "低延迟播放组件检测超时",
    REASON_PROBE_FAILED: "低延迟播放组件检测失败",
    REASON_VERSION_UNREADABLE: "低延迟播放组件版本无法识别",
    REASON_VERSION_TOO_OLD: "低延迟播放组件版本过低",
    REASON_CAPABILITY_MISSING: "低延迟播放组件缺少必要解码能力",
}

_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
_RELEASE_MAJOR_RE = re.compile(r"^(\d+)\.")
_AVCODEC_RE = re.compile(r"libavcodec\s+(\d+)\.\s*\d+")
#: 解码器列表行形如 `` VFS..D hevc                 HEVC (...``
_DECODER_HEVC_RE = re.compile(r"^\s*\S+\s+hevc\s+\S")
#: 解复用器列表行形如 `` D   flv             FLV (Flash Video)``
_DEMUXER_FLV_RE = re.compile(r"^\s*\S+\s+(?:live_)?flv\s+\S")


@dataclass(frozen=True)
class Ffmpeg7Status:
    """一次运行时探测的结论（不可变）。"""

    ok: bool
    reason: str
    path: str | None = None
    version: str | None = None
    major: int | None = None
    avcodec_major: int | None = None
    gpl: bool | None = None
    detail: str = ""

    def as_log_line(self) -> str:
        """给日志的一行技术摘要（含路径 / 版本 / 原因）。"""
        if self.ok:
            return (f"运行时可用: path={self.path} version={self.version} "
                    f"major={self.major} libavcodec={self.avcodec_major} "
                    f"gpl={self.gpl}")
        return f"运行时不可用: reason={self.reason} path={self.path} detail={self.detail}"


def reason_message(reason: str) -> str:
    """把稳定 reason 枚举映射成一句用户可见的话（不含技术细节）。"""
    return _REASON_TEXT.get(reason, "低延迟播放组件不可用")


# ----------------------------------------------------------------------
# 定位
# ----------------------------------------------------------------------
def candidate_paths() -> list[str]:
    """按定位优先级返回全部候选路径（未去重前的原始顺序，已去重）。"""
    raw: list[str] = []

    override = (os.environ.get(_ENV_OVERRIDE) or "").strip()
    if override:
        raw.append(override)

    for base in _frozen_base_dirs():
        raw.append(os.path.join(base, "runtime", _RUNTIME_SUBDIR, _EXE_NAME))

    raw.append(os.path.join(_project_root(), "runtime", _RUNTIME_SUBDIR, _EXE_NAME))

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    raw.append(os.path.join(local, _USER_DIR_NAME, "runtime", _RUNTIME_SUBDIR,
                            _EXE_NAME))

    seen: set[str] = set()
    uniq: list[str] = []
    for p in raw:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_existing_candidates() -> list[str]:
    """返回候选中「存在且非空」的那些（廉价检查，可安全在 GUI 线程调用）。"""
    return [p for p in candidate_paths() if _is_usable(p)]


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------
def _run_probe(path: str, args: list[str], timeout: float):
    """同步拉起一次 ffmpeg 探测进程（调用方负责不在 GUI 线程调用）。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        [path, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=flags,
    )


def _text_of(proc) -> str:
    """合并 stdout/stderr——ffmpeg 把横幅写 stderr、列表写 stdout，两者都要。"""
    out = proc.stdout or b""
    err = proc.stderr or b""
    return (out + b"\n" + err).decode("utf-8", errors="replace")


def _parse_major(version: str | None) -> int | None:
    """从版本串解析数字主版本；快照 / 自定义标识返回 None。"""
    if not version:
        return None
    m = _RELEASE_MAJOR_RE.match(version)
    return int(m.group(1)) if m else None


def probe(path: str | None = None, timeout: float = PROBE_TIMEOUT_S) -> Ffmpeg7Status:
    """对指定（或首个存在的候选）运行时做完整探测。

    **会拉起子进程**，因此不要在 GUI 线程直接调用（见 ``status`` 的说明）。
    探测步骤：启动 + 读版本横幅 → 解析版本 → 校验 hevc 解码能力与
    flv 解复用能力 → 解析 libavcodec 主版本与 GPL 标记（审计用）。
    """
    if path is None:
        candidates = find_existing_candidates()
        if not candidates:
            return Ffmpeg7Status(
                False, REASON_NOT_FOUND,
                detail="候选路径下均无运行时文件: "
                       + "; ".join(candidate_paths()),
            )
        path = candidates[0]

    if not _is_usable(path):
        return Ffmpeg7Status(False, REASON_NOT_FOUND, path=path,
                             detail="文件不存在或为空")

    # ---- 步骤 1：版本横幅 + 解码器列表（一次调用同时拿到两者）----
    try:
        dec_proc = _run_probe(path, ["-decoders"], timeout)
    except subprocess.TimeoutExpired:
        return Ffmpeg7Status(False, REASON_PROBE_TIMEOUT, path=path,
                             detail=f"探测超时(>{timeout:.0f}s)")
    except OSError as e:
        return Ffmpeg7Status(False, REASON_NOT_EXECUTABLE, path=path,
                             detail=f"无法启动进程: {e}")

    dec_text = _text_of(dec_proc)
    if dec_proc.returncode != 0:
        return Ffmpeg7Status(
            False, REASON_PROBE_FAILED, path=path,
            detail=f"探测退出码={dec_proc.returncode}; 尾部={dec_text[-300:]}",
        )

    m = _VERSION_RE.search(dec_text)
    if not m:
        return Ffmpeg7Status(False, REASON_VERSION_UNREADABLE, path=path,
                             detail=f"版本横幅无法解析; 尾部={dec_text[-200:]}")
    version = m.group(1)
    major = _parse_major(version)

    avcodec = _AVCODEC_RE.search(dec_text)
    avcodec_major = int(avcodec.group(1)) if avcodec else None
    gpl = ("--enable-gpl" in dec_text) if "configuration:" in dec_text else None

    if major is not None and major < MIN_MAJOR:
        return Ffmpeg7Status(
            False, REASON_VERSION_TOO_OLD, path=path, version=version,
            major=major, avcodec_major=avcodec_major, gpl=gpl,
            detail=f"版本 {version} < 要求主版本 {MIN_MAJOR}",
        )

    # 版本判据（二者取其一即可通过）：
    #   a) 版本串可解析出数字发布号，且 >= 7；
    #   b) 版本串是快照 / 自定义标识时，用 libavcodec 主版本 >= 61（= FFmpeg 7 一代）。
    # 两者都拿不到 = "版本信息不可识别" → 不可用（不得仅凭文件名/存在性放行）。
    if major is None:
        if avcodec_major is None:
            return Ffmpeg7Status(
                False, REASON_VERSION_UNREADABLE, path=path, version=version,
                major=None, avcodec_major=None, gpl=gpl,
                detail=f"版本串 {version!r} 无数字发布号，且横幅缺 libavcodec 版本行",
            )
        if avcodec_major < MIN_AVCODEC_MAJOR:
            return Ffmpeg7Status(
                False, REASON_VERSION_TOO_OLD, path=path, version=version,
                major=None, avcodec_major=avcodec_major, gpl=gpl,
                detail=(f"版本串 {version!r} 无数字发布号，"
                        f"libavcodec {avcodec_major} < 要求 {MIN_AVCODEC_MAJOR}"),
            )

    if not any(_DECODER_HEVC_RE.search(line) for line in dec_text.splitlines()):
        return Ffmpeg7Status(
            False, REASON_CAPABILITY_MISSING, path=path, version=version,
            major=major, avcodec_major=avcodec_major, gpl=gpl,
            detail="解码器列表缺少 hevc",
        )

    # ---- 步骤 2：解复用器列表（flv / live_flv）----
    try:
        dem_proc = _run_probe(path, ["-demuxers"], timeout)
    except subprocess.TimeoutExpired:
        return Ffmpeg7Status(False, REASON_PROBE_TIMEOUT, path=path,
                             version=version, major=major,
                             avcodec_major=avcodec_major, gpl=gpl,
                             detail=f"解复用器探测超时(>{timeout:.0f}s)")
    except OSError as e:
        return Ffmpeg7Status(False, REASON_NOT_EXECUTABLE, path=path,
                             version=version, major=major,
                             avcodec_major=avcodec_major, gpl=gpl,
                             detail=f"无法启动进程: {e}")

    dem_text = _text_of(dem_proc)
    if dem_proc.returncode != 0:
        return Ffmpeg7Status(
            False, REASON_PROBE_FAILED, path=path, version=version, major=major,
            avcodec_major=avcodec_major, gpl=gpl,
            detail=f"解复用器探测退出码={dem_proc.returncode}",
        )
    if not any(_DEMUXER_FLV_RE.search(line) for line in dem_text.splitlines()):
        return Ffmpeg7Status(
            False, REASON_CAPABILITY_MISSING, path=path, version=version,
            major=major, avcodec_major=avcodec_major, gpl=gpl,
            detail="解复用器列表缺少 flv/live_flv",
        )

    return Ffmpeg7Status(True, REASON_OK, path=path, version=version,
                         major=major, avcodec_major=avcodec_major, gpl=gpl,
                         detail="探测通过")


# ----------------------------------------------------------------------
# 缓存（一次进程生命周期只探测一次）
# ----------------------------------------------------------------------
_lock = threading.Lock()
_cached: Ffmpeg7Status | None = None


def status(refresh: bool = False) -> Ffmpeg7Status:
    """返回运行时状态（带缓存）。

    ``refresh=True`` 强制重新探测（测试 / 排障用）。首次调用会拉起子进程，
    因此同样不建议在 GUI 线程首次调用——GUI 侧统一走后台 worker。
    """
    global _cached
    with _lock:
        if _cached is not None and not refresh:
            return _cached
        result = probe()
        _cached = result
        logger.info("[FFMPEG7] %s", result.as_log_line())
        return result


def resolved_exe() -> str | None:
    """返回已通过校验的运行时路径；未探测 / 不可用时返回 None（不触发探测）。"""
    with _lock:
        if _cached is not None and _cached.ok:
            return _cached.path
    return None


def reset_cache() -> None:
    """清空缓存（测试用）。"""
    global _cached
    with _lock:
        _cached = None
