# -*- coding: utf-8 -*-
"""B4: 裸流候选派生（**已弃用** —— RELEASE-02-B5 起不再进入生产链路）。

.. deprecated::
    本模块的"从 `_uhd` 后缀**剥离派生**裸流 URL"方案已被实测证否：
    剥离后 URL 签名失效（**403 Forbidden**，sign 绑定 path）。
    正式方案是 `stream/bare_source.py` 的**页面直取**
    （`video_bare_url_for`），生产链路（`gui/main_window.py`）只使用后者。
    本模块仅为兼容既有单元测试保留，**请勿在新代码中引用**。

保留原因：`tests/stream/test_bare_candidate.py` 覆盖 `derive_bare_url` 的
纯函数边界；这些用例仍有效，删除模块会一并删除用例，超出本次改动范围。

从已选中的 `_uhd`/`_hd` 等 FLV 候选派生同主机的裸流 URL：
    https://host/.../stream-<id>_<variant>.flv?query
      → https://host/.../stream-<id>.flv?query
裸流 = 增强 FLV HEVC（需 FFmpeg 7 级运行时；内置运行时不可解）。

安全约束：
  - 门禁关闭（默认）时恒返回 None，零行为变化；
  - 非 FLV（HLS 等）或结构不匹配 → None；
  - 不修改 douyin.py；仅消费 selector 已选出的候选。
"""
import os
import re
from dataclasses import replace as _dataclass_replace

_TRUTHY = ("1", "true", "yes", "on")

#: stream-<id>_<variant>.flv → 捕获 id 与 variant
_VARIANT_RE = re.compile(r"^(?P<base>stream-\d+)_(?P<variant>[A-Za-z0-9]+)\.flv$")

#: 允许派生裸流的变体（转码档，内容同源）
_DERIVABLE_VARIANTS = {"uhd", "hd", "sd", "ld", "md"}


def bare_stream_enabled() -> bool:
    """DLV_BARE_STREAM 门禁（默认关闭）。

    开启还需 DLV_FFMPEG7_EXE 指向可执行的 FFmpeg 7 二进制——
    裸流为增强 FLV HEVC，默认 6.x 运行时不可解；未配置 = 无法执行 → 不派生。
    """
    if (os.environ.get("DLV_BARE_STREAM") or "").strip().lower() not in _TRUTHY:
        return False
    exe = os.environ.get("DLV_FFMPEG7_EXE") or ""
    return bool(exe.strip()) and os.path.isfile(exe.strip())


def ffmpeg7_exe() -> str:
    """返回 DLV_FFMPEG7_EXE（调用方需已确认 bare_stream_enabled）。"""
    return (os.environ.get("DLV_FFMPEG7_EXE") or "").strip()


def derive_bare_url(url: str) -> str | None:
    """从 FLV 转码档 URL 派生裸流 URL；结构不匹配返回 None。"""
    if not url or ".flv" not in url:
        return None
    path, sep, query = url.partition("?")
    fname = path.rsplit("/", 1)[-1]
    m = _VARIANT_RE.match(fname)
    if not m or m.group("variant") not in _DERIVABLE_VARIANTS:
        return None
    return path[: len(path) - len(fname)] + m.group("base") + ".flv" + sep + query


def bare_candidate_from(candidate):
    """从已选候选派生裸流候选（门禁关闭/不可派生时返回 None）。

    候选类型为 dataclass StreamCandidate（stream/models.py），
    用 dataclasses.replace 复制并改 url 与 runtime 标记。
    """
    if not bare_stream_enabled():
        return None
    bare_url = derive_bare_url(getattr(candidate, "url", "") or "")
    if not bare_url:
        return None
    try:
        new = _dataclass_replace(candidate, url=bare_url, runtime="ffmpeg7")
    except TypeError:
        return None
    return new
