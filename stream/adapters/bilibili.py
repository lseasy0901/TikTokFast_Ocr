# -*- coding: utf-8 -*-
"""B 站（bilibili）平台适配器。

职责边界与 stream/adapters/base.py 一致：只回答两件事 ——
"这个 URL 是不是 B 站直播间" 和 "解析成什么 StreamInfo"。
播放、渲染、OCR 都不在这里。

实现来源（合规说明）：
    对照播放器的解析链由**静态逆向报告**给出（liveplayer-case/final-report.md
    §P0-1，证据等级 [A]：`sub_14001F3D0` → `room/v1/Room/room_init` →
    `xlive/web-room/v2/index/getRoomPlayInfo`，字段路径
    `playurl_info → playurl → stream[] → format[] → codec[]`，
    URL = `url_info[].host + base_url(+ extra)`）。
    该报告为静态分析产物、未运行样本；**未复用任何二进制代码**。本适配器是
    依据上述公开接口与字段路径的**独立实现**（Python + requests），
    字段结构已用真实直播间只读探测核对。

清晰度策略（对齐对照软件）：**不做清晰度遍历**，直接取接口返回的默认档
    （`current_qn`），与逆向报告 §2.1 一致。

编码策略：只产出 **H.264（avc）** 候选。B 站另提供增强 HEVC 变体
    （`codec_name=hevc`，如 minihevc），本项目内置 FFmpeg 6.x 无法解，
    与抖音 hiq* 同理跳过（`DLV_BARE_STREAM` 的 ffmpeg7 路径与 B 站无关）。

鉴权说明（刻意固定）：
    仅走匿名公开接口，不计算签名、不绕过鉴权；日志/异常只输出**去掉 query**
    的 URL（见 `_mask_url`），避免把 sign/expires 等写进日志。
"""

import logging
import re

import requests

from ..models import StreamCandidate, StreamInfo, StreamResolveError
from ..selector import select
from .base import PlatformAdapter

logger = logging.getLogger("DouyinLowLatencyViewer.stream.adapters.bilibili")

__all__ = ["BilibiliAdapter", "BilibiliStreamError"]


class BilibiliStreamError(StreamResolveError):
    """B 站解析失败。

    继承 StreamResolveError，使 GUI 只捕获一种异常类型即可同时覆盖
    "没有可用平台" 与 "B 站解析失败"。
    """


#: 直播间 URL：live.bilibili.com/<房间号>（房间号可能很短或为长 id，均要求数字）
_ROOM_URL_RE = re.compile(
    r"^https?://live\.bilibili\.com/(\d+)(?:[/?#].*)?$", re.IGNORECASE)

_ROOM_INIT_API = "https://api.live.bilibili.com/room/v1/Room/room_init"
_PLAY_INFO_API = ("https://api.live.bilibili.com/xlive/web-room/v2/index/"
                  "getRoomPlayInfo")

_TIMEOUT = 15

#: 桌面 UA + Referer（对照软件同款，反爬与防盗链最小依赖）。不发 Cookie：
#: B 站拉流只需要 Referer，匿名可解析；不引入登录态。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://live.bilibili.com/",
}

#: 候选所需的播放请求头（FFmpeg 拉流侧防盗链）。
#: **实测（2026-10-12）**：B 站 FLV CDN 需 Referer 与浏览器 UA **同时具备**，
#: 只带其一（或都不带）一律 403 Forbidden。UA 由 reader 走 `-user_agent` 注入。
_MEDIA_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://live.bilibili.com/",
}

#: qn → 清晰度名（B 站标准档位；未知则原样保留 qn）
_QN_NAME = {30000: "杜比", 20000: "4K", 10000: "原画", 400: "蓝光",
            250: "超清", 150: "高清", 80: "流畅"}


def _mask_url(url: str) -> str:
    """只保留 scheme://host/path，丢掉 query（避免日志泄露 sign/expires/token）。"""
    return url.split("?", 1)[0]


def build_media_url(codec_obj: dict) -> str | None:
    """按报告字段路径拼出可直连的媒体 URL。

    URL = url_info[].host + base_url + extra。base_url 通常已以 '?' 结尾，
    因此 extra 直接接在其后。缺字段返回 None。
    """
    base = codec_obj.get("base_url") or ""
    infos = codec_obj.get("url_info") or []
    if not base or not infos:
        return None
    host = (infos[0].get("host") or "").rstrip("/")
    extra = infos[0].get("extra") or ""
    if not host:
        return None
    return "%s%s%s" % (host, base, extra)


def build_candidates(playurl_info: dict) -> list:
    """把 playurl_info 转成候选列表（仅 H.264），FLV 优先、HLS 兜底。

    接口返回顺序不稳定（同一房间有时 http_stream 先、有时 http_hls 先）。
    本项目**刻意把 FLV 排在 HLS 之前**：FLV 走 `live_flv` demuxer 是本项目
    已验证的低延迟路径（迁移审计 P1-3），HLS 作候选兜底。同协议内保持接口
    原始顺序。核心 selector 取首个候选。
    """
    out = []
    playurl = (playurl_info or {}).get("playurl") or {}
    for stream in playurl.get("stream") or []:
        proto_name = stream.get("protocol_name") or ""
        for fmt in stream.get("format") or []:
            # FLV 与否由 format_name 判定（protocol_name 是 http_stream/http_hls）
            is_flv = (fmt.get("format_name") or "").lower() == "flv"
            for codec in fmt.get("codec") or []:
                if (codec.get("codec_name") or "").lower() != "avc":
                    continue    # 只取 H.264；hevc 变体内置 FFmpeg 6.x 不可解
                url = build_media_url(codec)
                if not url:
                    continue
                qn = codec.get("current_qn")
                out.append(StreamCandidate(
                    url=url,
                    protocol="flv" if is_flv else "hls",
                    quality=_QN_NAME.get(qn, ("qn%s" % qn) if qn else None),
                    metadata={"source": "bilibili",
                              "protocol_name": proto_name,
                              "format_name": fmt.get("format_name"),
                              "qn": qn,
                              "host": (url.split("/")[2] if "//" in url else None)},
                    http_headers=dict(_MEDIA_HEADERS),
                ))
    # FLV 优先（稳定排序，保持同协议内的接口原始顺序）
    out.sort(key=lambda c: 0 if c.protocol == "flv" else 1)
    return out


class BilibiliAdapter(PlatformAdapter):
    """B 站直播间适配器（匿名公开接口）。"""

    name = "bilibili"

    def can_handle(self, url: str) -> bool:
        """URL 是否属于 B 站直播间（纯结构判断，不发请求）。"""
        if not url or not isinstance(url, str):
            return False
        return bool(_ROOM_URL_RE.match(url.strip()))

    def resolve(self, url: str) -> StreamInfo:
        """:raises BilibiliStreamError: 解析失败"""
        m = _ROOM_URL_RE.match((url or "").strip())
        if not m:
            raise BilibiliStreamError(f"URL格式错误：不是 B 站直播间地址 ({_mask_url(url or '')})")
        room_id_in = m.group(1)

        data = self._get_json(_ROOM_INIT_API, {"id": room_id_in}, stage="room_init")
        info = data.get("data") or {}
        if not info:
            raise BilibiliStreamError(f"room_init 未返回房间信息 (room={room_id_in})")
        room_id = str(info.get("room_id") or room_id_in)
        if int(info.get("live_status") or 0) != 1:
            raise BilibiliStreamError(
                f"B 站主播未开播 (room={room_id}, live_status={info.get('live_status')})")

        params = {"room_id": room_id, "protocol": "0,1", "format": "0,1,2",
                  "codec": "0,1", "qn": "10000", "platform": "web", "ptype": "8"}
        data = self._get_json(_PLAY_INFO_API, params, stage="getRoomPlayInfo")
        if int((data.get("data") or {}).get("live_status") or 0) != 1:
            raise BilibiliStreamError(f"B 站直播间未开播 (room={room_id})")

        candidates = build_candidates((data.get("data") or {}).get("playurl_info") or {})
        if not candidates:
            raise BilibiliStreamError(
                f"B 站未返回可用的 H.264 流 (room={room_id})")
        logger.info("[BILI] room=%s 候选 %d 个，首选 %s (%s)",
                    room_id, len(candidates), candidates[0].quality,
                    _mask_url(candidates[0].url))
        return StreamInfo(platform=self.name, room_id=room_id,
                          candidates=candidates, selected=select(candidates))

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------
    def _get_json(self, api: str, params: dict, stage: str) -> dict:
        """:raises BilibiliStreamError: 网络失败 / 非 JSON / 业务码非 0"""
        try:
            resp = requests.get(api, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise BilibiliStreamError(f"[{stage}] 网络异常: {e}")
        if resp.status_code != 200:
            raise BilibiliStreamError(f"[{stage}] HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise BilibiliStreamError(f"[{stage}] 响应非 JSON (HTTP {resp.status_code})")
        if data.get("code") != 0:
            raise BilibiliStreamError(
                f"[{stage}] 业务错误 code={data.get('code')} msg={data.get('message')}")
        return data
