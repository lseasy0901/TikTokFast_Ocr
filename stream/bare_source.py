# -*- coding: utf-8 -*-
"""B5: 视频版裸流来源 —— 唯一拉流架构的取流入口。

裸流 = 增强 FLV HEVC，由对照软件逆向（liveplayer-case/final-report.md §P0-1）
确认为 App/SDK 分发线的原画直传。唯一拉流架构：

    本模块（页面直取裸流 URL）→ FFmpeg 7（utils/ffmpeg7_runtime.py）→ 解码/显示

**从直播间页面直取**真实的 `stream-<id>.flv`（不能从 `_uhd` 等转码档后缀剥离
派生：实测剥离后 URL 签名失效 —— 403 Forbidden，sign 绑定 path）。

识别判据（2026-10-12 实测，房间 50942209695）：
  - basename 恰为 `stream-<id>.flv`（无质量后缀）；
  - URL 不含 `only_audio=1`（含者 = 纯音频中继，无播放价值）；
  - 含 `unique_id=stream-` 与 `t_id=`（排除页面转义残留的截断片段）。

RELEASE-02-B5-CLEANUP 语义：
  - 本模块是**唯一**取流来源；旧 `_uhd` 转码档路径及其回退已整体移除；
  - 取不到裸流时**不再回退任何旧路径**，而是返回结构化原因（`BareProbe.reason`），
    由 GUI 给出明确提示（"未开播" / "未找到裸流" / "网络失败"）；
  - 本模块的取流函数会发起**阻塞式** HTTP 请求，**不得在 GUI 线程调用**。

安全约束：
  - 只读页面文本，不做任何写操作；不改动用户配置 / ROI。
  - 请求有界超时（`FETCH_TIMEOUT_S`），绝不无限等待。
"""

import html as html_mod
import logging
import os
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("DouyinLowLatencyViewer.stream.bare_source")

__all__ = [
    "BareProbe",
    "bare_stream_enabled",
    "bare_stream_applicable",
    "extract_video_bare",
    "page_is_live",
    "probe_video_bare",
    "reason_message",
    "FETCH_TIMEOUT_S",
    "REASON_OK",
    "REASON_OFFLINE",
    "REASON_NO_BARE",
    "REASON_FETCH_FAILED",
    "REASON_UNSUPPORTED",
    "REASON_DISABLED",
]

#: 显式关闭裸流总开关的取值（其余值——含未设置——都视为开启）。
_FALSY = ("0", "false", "no", "off")

#: 页面直取的有界超时（秒）。正常取流为亚秒级（且带 45s 缓存），
#: 超时即视为"本次取不到"，由调用方给出明确提示，不让用户为一次慢请求久等。
FETCH_TIMEOUT_S = 6.0

#: 视频版裸流：basename 恰为 stream-<id>.flv 的完整 URL（含参数链）
_BARE_URL_RE = re.compile(r'https?://[^\s"<>\\]+?/stream-\d+\.flv\?[^\s"<>\\]*')
_BARE_BASENAME_RE = re.compile(r"stream-\d+\.flv")

#: 页面文本里的常见转义（JSON 内嵌 + HTML 实体）
_UNESCAPES = (
    ("\\u0026", "&"), ("\\u002F", "/"), ("\\/", "/"), ("\\", ""),
    ("&amp;", "&"),
)

#: **直播中**的页面标志字段。仅用于判断"这个房间此刻在不在播"，
#: **不解析其内容**（内容属旧转码档路径，已移除）。
#: 实测依据：10 个房间样本上，该标志的存在/缺失与"能否取到裸流"100% 一致
#: （5 个在播房间全部命中，5 个未开播/不存在房间全部缺失）。
_LIVE_MARKERS = ("live_core_sdk_data", "flv_pull_url")

#: 同一房间的探测结果缓存（live_url → (时刻, BareProbe)），避免同一会话内
#: 重复取页面。TTL 短于抖音 token 时效（分钟级）。
_CACHE: dict = {}
_CACHE_TTL_S = 45.0

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ---- 结构化原因（供 GUI 映射成一句人话，不泄漏技术细节） ----
REASON_OK = "ok"
REASON_OFFLINE = "offline"
REASON_NO_BARE = "no_bare_url"
REASON_FETCH_FAILED = "fetch_failed"
REASON_UNSUPPORTED = "unsupported_platform"
REASON_DISABLED = "disabled"

_REASON_TEXT = {
    REASON_OFFLINE: "主播未开播",
    REASON_NO_BARE: "未找到可用的直播流",
    REASON_FETCH_FAILED: "无法获取直播间信息，请检查网络后重试",
    REASON_UNSUPPORTED: "该平台不支持低延迟播放",
    REASON_DISABLED: "低延迟播放已关闭",
}


@dataclass(frozen=True)
class BareProbe:
    """一次裸流探测的结论（不可变）。

    :param url:    视频版裸流 URL；None = 本次取不到
    :param live:   页面是否显示该房间正在直播
    :param reason: 稳定枚举，供 GUI 映射成用户可见文案
    :param detail: 给日志的技术细节（不进 UI）
    """

    url: str | None = None
    live: bool = False
    reason: str = REASON_NO_BARE
    detail: str = ""


def reason_message(reason: str) -> str:
    """把稳定 reason 枚举映射成一句用户可见的话（不含技术细节）。"""
    return _REASON_TEXT.get(reason, "无法获取直播流")


def bare_stream_enabled() -> bool:
    """裸流总开关：**默认开启**，仅显式 `DLV_BARE_STREAM=0/false/no/off` 时关闭。

    只判断总开关，**不判断运行时是否可用**——运行时校验需要拉起子进程探测，
    由 `utils.ffmpeg7_runtime.status()` 在后台完成。这样本函数保持纯环境变量
    读取、零 IO，可以安全地在任意线程调用。
    """
    val = (os.environ.get("DLV_BARE_STREAM") or "").strip().lower()
    return val not in _FALSY


def bare_stream_applicable(platform: str) -> bool:
    """本平台是否应走裸流路径（总开关 + 平台支持），**不做**运行时探测。

    仅抖音支持裸流（对照软件逆向结论的平台）；其余平台恒 False，
    因此虎牙 / B 站既不会请求页面，也不会等待运行时探测。
    """
    if not bare_stream_enabled():
        return False
    return (platform or "").lower() == "douyin"


def _normalize(u: str) -> str:
    for a, b in _UNESCAPES:
        u = u.replace(a, b)
    return html_mod.unescape(u)


def extract_video_bare(page_html: str) -> str | None:
    """从直播间页面文本提取视频版裸流 URL；无则 None（纯函数，可单测）。"""
    if not page_html:
        return None
    for m in _BARE_URL_RE.finditer(page_html):
        u = _normalize(m.group(0))
        base = u.split("?", 1)[0].rsplit("/", 1)[-1]
        if not _BARE_BASENAME_RE.fullmatch(base):
            continue
        if "only_audio=1" in u:
            continue
        if "unique_id=stream-" not in u or "t_id=" not in u:
            continue    # 页面转义残留的截断片段
        return u
    return None


def page_is_live(page_html: str) -> bool:
    """页面是否显示该房间正在直播（只读标志字段，不解析其内容）。"""
    return any(marker in page_html for marker in _LIVE_MARKERS)


def _fetch_page(live_url: str) -> str | None:
    """取直播间页面文本（复用 douyin 适配器的 Cookie 体系）。

    **阻塞调用**（有界超时 `FETCH_TIMEOUT_S`）：调用方必须在后台线程执行。
    """
    try:
        import requests
        from stream.adapters.douyin import ensure_session_cookies

        sess = requests.Session()
        try:
            ensure_session_cookies(sess)
        except Exception as e:  # noqa: BLE001 - 无 Cookie 时直接匿名请求
            logger.debug("[BARE] Cookie 装载失败（匿名请求）: %s", e)
        r = sess.get(live_url, headers={"User-Agent": _UA,
                                        "Referer": "https://live.douyin.com/"},
                     timeout=FETCH_TIMEOUT_S)
        logger.info("[BARE] 页面获取 HTTP %d len=%d", r.status_code, len(r.text))
        return r.text if r.status_code == 200 else None
    except Exception as e:  # noqa: BLE001 - 取流失败不抛，转结构化原因
        logger.warning("[BARE] 页面获取失败: %s", e)
        return None


def probe_video_bare(platform: str, room_id: str) -> BareProbe:
    """探测视频版裸流（**唯一**取流入口）。

    **阻塞调用**：内部会发起有界超时的 HTTP 页面请求，必须在后台线程执行
    （GUI 侧经裸流解析 worker 调用，不得直接在 GUI 线程调用）。

    返回结构化结论：取到 → `reason=ok` + url；未开播 → `offline`；
    页面在播但取不到裸流 → `no_bare_url`；页面取不到 → `fetch_failed`。
    **任何失败都不回退旧路径**——由调用方给出明确提示。
    """
    if not bare_stream_enabled():
        return BareProbe(reason=REASON_DISABLED, detail="总开关已关闭")
    if not bare_stream_applicable(platform):
        return BareProbe(reason=REASON_UNSUPPORTED,
                         detail=f"平台不支持裸流: {platform}")
    if not room_id:
        return BareProbe(reason=REASON_NO_BARE, detail="房间号为空")

    live_url = "https://live.douyin.com/%s" % room_id
    now = time.time()
    hit = _CACHE.get(live_url)
    if hit and now - hit[0] < _CACHE_TTL_S:
        prev = hit[1]
        logger.debug("[BARE] 命中缓存: %s", prev.reason)
        return prev

    page = _fetch_page(live_url)
    if page is None:
        result = BareProbe(reason=REASON_FETCH_FAILED,
                           detail="页面获取失败（HTTP 非 200 或网络异常）")
        _CACHE[live_url] = (now, result)
        return result

    live = page_is_live(page)
    url = extract_video_bare(page)
    if url:
        logger.info("[BARE] 视频版裸流: %s...", url[:90])
        result = BareProbe(url=url, live=True, reason=REASON_OK,
                           detail="页面直取成功")
    elif not live:
        logger.info("[BARE] 房间未开播（页面无直播标志）: %s", room_id)
        result = BareProbe(url=None, live=False, reason=REASON_OFFLINE,
                           detail="页面无直播标志字段")
    else:
        logger.warning("[BARE] 页面在播但未找到视频版裸流（房间 %s）", room_id)
        result = BareProbe(url=None, live=True, reason=REASON_NO_BARE,
                           detail="页面在播但判据未命中（可能页面结构变化）")
    _CACHE[live_url] = (now, result)
    return result
