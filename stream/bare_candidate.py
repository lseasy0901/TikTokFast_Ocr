# -*- coding: utf-8 -*-
"""B4: 裸流候选派生（DLV_BARE_STREAM 门控，默认关闭）。

从已选中的 `_uhd`/`_hd` 等 FLV 候选派生同主机的裸流 URL：
    https://host/.../stream-<id>_<variant>.flv?query
      → https://host/.../stream-<id>.flv?query
裸流 = 增强 FLV HEVC（ffmpeg 7+ 可解，6.x 不可解——H7-B1 实测），
故派生候选携带 runtime="ffmpeg7"。

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
    """DLV_BARE_STREAM 门禁（默认关闭）。"""
    return (os.environ.get("DLV_BARE_STREAM") or "").strip().lower() in _TRUTHY


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
