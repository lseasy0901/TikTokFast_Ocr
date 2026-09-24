# -*- coding: utf-8 -*-
"""虎牙平台适配器。

职责边界与 stream/adapters/base.py 一致：只回答两件事 ——
"这个 URL 是不是虎牙直播间" 和 "解析成什么 StreamInfo"。
播放、渲染、OCR、CDN 测速都不在这里。

数据来源：房间页 HTML 内联的 `stream: {...}` JS 对象

    stream.data[0].gameLiveInfo        直播间信息（主播、游戏、开播时间）
    stream.data[0].gameStreamInfoList  各 CDN 的拉流信息（本阶段候选来源）

该对象是 JS object literal 而不是 JSON，因此先做局部平衡括号提取，
再尝试直接 json.loads，失败才把未加引号的键名规范化后重试
（实验阶段已验证的做法，见 HuyaStreamExperiment/stage2_parse.py）。

鉴权说明（刻意固定）：
    AntiCode 一律原样取自页面的 sFlvAntiCode，本项目不计算 wsSecret、
    不生成 wsTime、不绕过鉴权、不请求隐藏 API。日志与异常中只输出去掉
    query 的 URL（见 _mask_url），避免把鉴权参数写进日志文件。

本阶段协议范围：
    只产出 FLV 候选。HLS 在当前环境下多为 403，不进主播放路径，
    因此不生成 HLS 候选（StreamSelector 取首个候选，生成即可能被选中）。
"""

import json
import logging
import re

import requests

from ..models import StreamCandidate, StreamInfo, StreamResolveError
from ..selector import select
from .base import PlatformAdapter

logger = logging.getLogger("DouyinLowLatencyViewer.stream.adapters.huya")

__all__ = ["HuyaAdapter", "HuyaStreamError"]


class HuyaStreamError(StreamResolveError):
    """虎牙解析失败。

    继承 StreamResolveError，使 GUI 只捕获一种异常类型就能同时覆盖
    "没有可用平台" 与 "虎牙解析失败"。
    """


# `stream:` 这个键在页面里紧跟着一个 JS 对象字面量
_JS_STREAM_RE = re.compile(r"\bstream\s*:\s*\{")

# 只规范化"紧跟 { 或 , 的裸键名"，避免误伤字符串值里的 "words: " 形态
_UNQUOTED_KEY_RE = re.compile(r"([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

# 虎牙房间 URL：只按主机名 + 一个路径段判断，不发任何请求。
# 要求必须有房间段，因此 https://www.huya.com/ 首页不会被当成直播间。
_ROOM_URL_RE = re.compile(r"^https?://(?:www\.)?huya\.com/([^/?#\s]+)", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TIMEOUT = 20

# ----------------------------------------------------------------------
# 媒体请求头（Phase 2.4）
#
# 实测结论（详见 Phase 2.3 实验）：虎牙 CDN 只接受带 `HYSDK(...)` 前缀的
# User-Agent。用 FFmpeg 默认的 `Lavf/...`、或普通浏览器 UA，都会在 3~8 秒后
# 被服务端切断（HTTP 200 后 Connection: Close + 提前 EOF）。换成下面的 UA
# 后，AL / TX / HS 三个 CDN 均可连续读取 60 秒以上。
#
# 关键变量是 `HYSDK(...)` 这个前缀本身，不是里面的版本号：`pc_exe&7100004`
# 与 `exe&7030003` 两种版本号都验证通过，仅去掉前缀即失败。
#
# Referer / Origin 经实验证明**不是**根因（单独加无效），这里一并带上是因为
# stream-rec 与 rust-srec 两个成熟项目都无条件附带，属于低成本的冗余。
# ----------------------------------------------------------------------
HUYA_USER_AGENT = ("HYSDK(Windows,30000002)_APP(pc_exe&7100004&official)"
                   "_SDK(trans&2.40.0.6448)")
HUYA_ORIGIN = "https://www.huya.com"
HUYA_MEDIA_HEADERS = {
    "User-Agent": HUYA_USER_AGENT,
    "Referer": HUYA_ORIGIN,
    "Origin": HUYA_ORIGIN,
}


# ======================================================================
# 纯函数：HTML -> 数据。便于单测直接喂 fixture，不碰网络。
# ======================================================================
def _mask_url(url: str) -> str:
    """日志用：去掉 query，防止 wsSecret / wsTime 等鉴权参数落盘"""
    if not url:
        return ""
    base, sep, _ = url.partition("?")
    return base + ("?<masked>" if sep else "")


def _extract_balanced(text: str, start: int) -> str | None:
    """取 text[start]（必须指向 '{'）开始的平衡花括号块。

    正确跳过字符串字面量与其中的转义字符，因此 HTML 里字符串内部的
    '{' / '}' / '"' 不会干扰配对。
    """
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_stream_object(html: str) -> dict:
    """从房间页 HTML 中提取 `stream: {...}` 并解析成 dict。

    只提取这一个对象，不对整个页面做 JSON 替换或整体 json.loads。

    :raises HuyaStreamError: 找不到数据块，或数据块无法解析
    """
    match = _JS_STREAM_RE.search(html or "")
    if not match:
        raise HuyaStreamError("虎牙页面中未找到 stream 数据块（可能不是直播间页面）")

    brace_at = html.index("{", match.start())
    block = _extract_balanced(html, brace_at)
    if not block:
        raise HuyaStreamError("虎牙页面 stream 数据块不完整（花括号未闭合）")

    try:
        return json.loads(block)
    except ValueError:
        # 实测页面键名已带引号；这里保留裸键名兼容路径，应对页面形态变化
        normalized = _UNQUOTED_KEY_RE.sub(r'\1"\2":', block)
        try:
            return json.loads(normalized)
        except ValueError as e:
            raise HuyaStreamError(f"虎牙 stream 数据块无法解析: {e}")


def locate_room_data(stream_obj: dict) -> dict:
    """在 stream.data 中定位带 gameStreamInfoList 的房间节点。

    以 gameLiveInfo 与 gameStreamInfoList 的存在作为"这是一个可播放直播间"
    的判断依据 —— 页面中并不存在 iIsReplay / eLiveStatus / roomInfo 字段，
    因此不能依赖它们。

    :raises HuyaStreamError: 数据里没有可用的房间节点
    """
    if not isinstance(stream_obj, dict):
        raise HuyaStreamError("虎牙 stream 数据块结构异常（不是对象）")

    rooms = stream_obj.get("data")
    if not isinstance(rooms, list) or not rooms:
        raise HuyaStreamError("虎牙页面未包含直播间数据（可能未开播或房间不存在）")

    for room in rooms:
        if not isinstance(room, dict):
            continue
        if room.get("gameLiveInfo") and room.get("gameStreamInfoList"):
            return room

    raise HuyaStreamError("虎牙直播间未开播或无可用流（缺少 gameLiveInfo / gameStreamInfoList）")


def build_flv_url(entry: dict) -> str | None:
    """按页面提供的四个字段拼完整 FLV 地址。

    形如 {sFlvUrl}/{sStreamName}.{sFlvUrlSuffix}?{sFlvAntiCode}。
    AntiCode 缺失时返回 None —— 缺了它只会拿到 403，宁可判为无效候选。
    """
    base = (entry.get("sFlvUrl") or "").strip().rstrip("/")
    name = (entry.get("sStreamName") or "").strip()
    suffix = (entry.get("sFlvUrlSuffix") or "").strip()
    anti = (entry.get("sFlvAntiCode") or "").strip()

    if not (base and name and suffix and anti):
        return None
    return f"{base}/{name}.{suffix}?{anti}"


def build_candidates(entries) -> list:
    """把 gameStreamInfoList 转成 FLV 候选，保持页面原始顺序。"""
    candidates = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue

        url = build_flv_url(entry)
        if not url:
            logger.debug("[HUYA] 跳过缺少字段的流条目: cdn=%s", entry.get("sCdnType"))
            continue

        candidates.append(StreamCandidate(
            url=url,
            protocol="flv",
            # 页面没有给出每个 CDN 条目的清晰度名，也没有可信的每流码率
            # （gameLiveInfo.bitRate 与实际码率不符，vMultiStreamInfo 也无法
            #  与 CDN 条目一一对应），因此保持 None，不伪造。
            quality=None,
            bitrate=None,
            width=None,
            height=None,
            fps=None,
            metadata={
                "cdn": entry.get("sCdnType"),
                "stream_name": entry.get("sStreamName"),
            },
            # 每个候选一份独立副本，避免调用方改到共享字典
            http_headers=dict(HUYA_MEDIA_HEADERS),
        ))
    return candidates


# ======================================================================
# 平台适配器
# ======================================================================
class HuyaAdapter(PlatformAdapter):
    """虎牙平台适配器。

    解析细节都在本模块的函数里，这里只做编排：
    URL -> 房间页 -> stream 数据块 -> 候选流 -> StreamInfo。
    """

    name = "huya"

    # ------------------------------------------------------------------
    # 能不能处理
    # ------------------------------------------------------------------
    def can_handle(self, url: str) -> bool:
        """URL 是否属于虎牙直播间（纯 URL 判断，不发请求）"""
        if not url or not isinstance(url, str):
            return False
        return bool(_ROOM_URL_RE.match(url.strip()))

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------
    def _fetch_room_page(self, url: str) -> str:
        """:raises HuyaStreamError: 网络失败或非 200"""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT,
                                allow_redirects=True)
        except requests.RequestException as e:
            raise HuyaStreamError(f"虎牙房间页请求失败: {e}")

        if resp.status_code != 200:
            raise HuyaStreamError(f"虎牙房间页返回 HTTP {resp.status_code}")

        logger.info("[HUYA] 房间页获取成功: %d bytes, final=%s",
                    len(resp.text), _mask_url(resp.url))
        return resp.text

    def _detect_video_resolution(self, stream_url: str):
        """用 ffprobe 探测选中流的真实分辨率。

        与抖音一致：StreamInfo.width/height 必须是**选中流的实际分辨率**。
        这一点是必需的 —— MainWindow 在 info.width 为空时会退回 1920x1080
        并把它当作确定值传给 FFmpegReader，而那会让 FFmpegReader 跳过
        自动探测，最终按错误尺寸切帧。

        返回 VideoInfo 或 None（探测失败不影响解析结果）。
        """
        try:
            from ..video_info import get_video_info

            info = get_video_info(stream_url)
            if info and info.width and info.height:
                logger.info(
                    "[HUYA][VIDEO INFO] width=%d, height=%d, pix_fmt=%s, fps=%s, codec=%s",
                    info.width, info.height, info.pix_fmt or "unknown",
                    f"{info.fps:.2f}" if info.fps else "unknown",
                    info.codec_name or "unknown",
                )
                return info
            logger.warning("[HUYA][VIDEO INFO] 分辨率探测失败，使用默认值")
        except ImportError:
            logger.warning("[HUYA][VIDEO INFO] video_info 模块导入失败")
        except Exception as e:
            logger.warning("[HUYA][VIDEO INFO] 分辨率探测异常: %s", e)
        return None

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def resolve(self, url: str) -> StreamInfo:
        """:raises HuyaStreamError: 解析失败或无有效候选流"""
        match = _ROOM_URL_RE.match((url or "").strip())
        if not match:
            raise HuyaStreamError(f"URL格式错误：不是有效的虎牙直播间地址 -> {url}")
        room = match.group(1)

        logger.info("[HUYA] 开始解析: room=%s", room)

        stream_obj = parse_stream_object(self._fetch_room_page(url))
        room_data = locate_room_data(stream_obj)
        candidates = build_candidates(room_data.get("gameStreamInfoList"))

        if not candidates:
            raise HuyaStreamError(f"虎牙直播间无有效 FLV 候选流 (room={room})")

        selected = select(candidates)
        logger.info("[HUYA] 候选 %d 个, 选中 cdn=%s url=%s",
                    len(candidates), selected.metadata.get("cdn"),
                    _mask_url(selected.url))

        # 分辨率只探测选中的那一个候选，其余候选保持 None（页面未给出）
        width = height = None
        probed = self._detect_video_resolution(selected.url)
        if probed:
            width, height = probed.width, probed.height

        return StreamInfo(
            platform=self.name,
            room_id=room,
            candidates=candidates,
            selected=selected,
            width=width,
            height=height,
        )
