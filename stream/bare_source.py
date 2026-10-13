# -*- coding: utf-8 -*-
"""B5: 视频版裸流来源（**默认开启**，可用 DLV_BARE_STREAM 显式关闭）。

裸流 = 增强 FLV HEVC（ffmpeg 7+ 可解，内置运行时不可解），由对照软件逆向
（liveplayer-case/final-report.md §P0-1）确认为 App/SDK 分发线的原画直传。

与 `stream/bare_candidate.py`（把 `stream-<id>_<variant>.flv` 后缀剥离派生，
**已弃用**）不同：实测剥离后 URL 签名失效（403 Forbidden——sign 绑定 path），
故本模块**从直播间页面直取**真实的 `stream-<id>.flv`。

识别判据（2026-10-12 实测，房间 50942209695）：
  - basename 恰为 `stream-<id>.flv`（无质量后缀）；
  - URL 不含 `only_audio=1`（含者 = 纯音频中继，无播放价值）；
  - 含 `unique_id=stream-` 与 `t_id=`（排除页面转义残留的截断片段）。

RELEASE-02-B5 语义变更：
  - 本模块**不再**要求 `DLV_FFMPEG7_EXE`；运行时是否可用由
    `utils.ffmpeg7_runtime` 负责定位与校验（需拉起进程探测，属后台工作）。
  - 裸流是**默认正常路径**：总开关默认开启，只有显式 `DLV_BARE_STREAM=0`
    （或 false/no/off）才关闭。
  - 本模块的 `video_bare_url_for()` 会发起**阻塞式** HTTP 请求，因此
    **不得在 GUI 线程调用**——GUI 侧统一经后台 worker 调用。

安全约束：
  - 只读页面文本，不做任何写操作；不改动 douyin.py / 用户配置 / ROI。
  - 请求有界超时（`FETCH_TIMEOUT_S`），绝不无限等待。
"""
import html as html_mod
import logging
import os
import re
import time

logger = logging.getLogger("DouyinLowLatencyViewer.stream.bare_source")

#: 显式关闭裸流总开关的取值（其余值——含未设置——都视为开启）。
_FALSY = ("0", "false", "no", "off")

#: 页面直取的有界超时（秒）。B5 原实现为 20s，正式接入后收紧到 6s：
#: 正常取流为亚秒级（且带 45s 缓存），超时即视为"本次取不到"，
#: 由调用方走标准路径，不能让用户为一次慢请求等待 20 秒。
FETCH_TIMEOUT_S = 6.0

#: 视频版裸流：basename 恰为 stream-<id>.flv 的完整 URL（含参数链）
_BARE_URL_RE = re.compile(r'https?://[^\s"<>\\]+?/stream-\d+\.flv\?[^\s"<>\\]*')
_BARE_BASENAME_RE = re.compile(r"stream-\d+\.flv")

#: 页面文本里的常见转义（JSON 内嵌 + HTML 实体）
_UNESCAPES = (
    ("\\u0026", "&"), ("\\u002F", "/"), ("\\/", "/"), ("\\", ""),
    ("&amp;", "&"),
)

#: 同一房间的裸流 URL 缓存（live_url → (时刻, URL|None)），避免 failover
#: 每次候选尝试都重取页面。TTL 短于抖音 token 时效（分钟级）。
_CACHE: dict = {}
_CACHE_TTL_S = 45.0

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def bare_stream_enabled() -> bool:
    """裸流总开关：**默认开启**，仅显式 `DLV_BARE_STREAM=0/false/no/off` 时关闭。

    只判断总开关，**不判断运行时是否可用**——运行时校验需要拉起子进程探测，
    由 `utils.ffmpeg7_runtime.status()` 在后台完成（见 `bare_stream_applicable`
    与 GUI 的裸流解析 worker）。这样本函数保持纯环境变量读取、零 IO，
    可以安全地在任意线程调用。
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


def _fetch_page(live_url: str) -> str | None:
    """取直播间页面文本（复用 douyin.py 的 ttwid Cookie 缓存）。

    **阻塞调用**（有界超时 `FETCH_TIMEOUT_S`）：调用方必须在后台线程执行。
    """
    try:
        import requests
        sess = requests.Session()
        try:
            from stream.adapters.douyin import _load_cached_cookies
            sess.cookies.update(_load_cached_cookies() or {})
        except Exception:  # noqa: BLE001 - 无缓存 Cookie 时直接匿名请求
            pass
        r = sess.get(live_url, headers={"User-Agent": _UA,
                                        "Referer": "https://live.douyin.com/"},
                     timeout=FETCH_TIMEOUT_S)
        logger.info("[BARE] 页面获取 HTTP %d len=%d", r.status_code, len(r.text))
        return r.text if r.status_code == 200 else None
    except Exception as e:  # noqa: BLE001 - 裸流是增强项，失败不阻断主链路
        logger.warning("[BARE] 页面获取失败: %s", e)
        return None


def video_bare_url_for(platform: str, room_id: str) -> str | None:
    """按平台+房间号取视频版裸流 URL（总开关关闭/平台不支持/未找到时 None）。

    仅抖音支持（对照软件逆向结论的平台）；其余平台恒 None。

    **阻塞调用**：内部会发起有界超时的 HTTP 页面请求，必须在后台线程执行
    （GUI 侧经裸流解析 worker 调用，不得直接在 GUI 线程调用）。
    调用方应已通过 `bare_stream_applicable()` 与运行时校验确认可以尝试。
    """
    if not bare_stream_applicable(platform) or not room_id:
        return None
    live_url = "https://live.douyin.com/%s" % room_id
    now = time.time()
    hit = _CACHE.get(live_url)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    page = _fetch_page(live_url)
    url = extract_video_bare(page) if page else None
    _CACHE[live_url] = (now, url)
    if url:
        logger.info("[BARE] 视频版裸流: %s...", url[:90])
    else:
        logger.warning("[BARE] 页面未找到视频版裸流（房间 %s）", room_id)
    return url
