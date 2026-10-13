# -*- coding: utf-8 -*-
"""抖音直播间解析模块（RELEASE-02-B5-CLEANUP 后的单一拉流架构）。

职责边界（本模块**只做两件事**）：
    1. 判断一个 URL 是不是抖音直播间，并解析出 room_id；
    2. 把结果整理成核心层认识的 StreamInfo。

    **不再枚举任何流地址候选。** 唯一拉流架构是：

        裸流页面直取（stream/bare_source.py）
          -> FFmpeg 7（utils/ffmpeg7_runtime.py 定位并校验）
          -> 现有解码/显示管线（stream/ffmpeg_reader.py）

    裸流 URL 由 GUI 在会话开始时于后台线程直取（不阻塞 GUI），本层不参与。
    旧 `_uhd` 转码档候选（`flv_pull_url` / `hls_pull_url` / `flv_pull_url_map` /
    `hls_pull_url_map` / `pull_url` / `default_pull_url`）、SDK 线候选、以及
    「裸流失败回退 `_uhd`」的回退语义**已整体移除** —— 见
    RELEASE-02-B5-CLEANUP 审计报告 A 类清单。

    因此 `get_stream_info()` 返回的 `candidates` **恒为空**、`selected` 恒为
    None，这是刻意设计而非缺陷。

依赖：
    仅依赖 requests，不依赖 GUI / FFmpeg / OpenCV。
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from utils.stream_history import default_data_dir
from ..models import StreamInfo, StreamResolveError
from .base import PlatformAdapter

logger = logging.getLogger("DouyinLowLatencyViewer.stream.douyin")

#: 平台标识（StreamInfo.platform 与适配器 name 共用同一处定义）。
PLATFORM_NAME = "douyin"

#: ttwid 等 Cookie 的持久化文件（与 stream_history 同一数据目录）。
#: ttwid 是长时效的浏览器会话标识：首次首页获取后存盘复用，热启动可整段
#: 跳过首页请求。裸流页面直取复用同一份 Cookie 体系。
_COOKIE_FILENAME = "douyin_cookies.json"

#: 页面请求用的浏览器请求头（裸流直取与首页取 Cookie 共用）。
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

#: 首页地址（用于获取 ttwid）。
_HOME_URL = "https://live.douyin.com/"


def _cookie_store_path() -> Path:
    return Path(default_data_dir()) / _COOKIE_FILENAME


def _load_cached_cookies() -> dict | None:
    """读取持久化 Cookie；无文件/损坏/缺 ttwid 一律视为无缓存（不抛）。"""
    try:
        path = _cookie_store_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if isinstance(cookies, dict) and cookies.get("ttwid"):
            return {str(k): str(v) for k, v in cookies.items() if v}
    except Exception as e:  # noqa: BLE001 - 缓存读不出来就当没有，走正常获取
        logger.debug("[Cookie] 读取持久化 Cookie 失败（忽略）: %s", e)
    return None


def _save_cookies(cookies: dict) -> None:
    """原子写入持久化 Cookie（写临时文件后 os.replace，防半截文件）。"""
    if not cookies.get("ttwid"):
        return
    try:
        path = _cookie_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(), "cookies": cookies}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        logger.info("[Cookie] 已持久化 %d 枚 Cookie 供后续解析复用", len(cookies))
    except Exception as e:  # noqa: BLE001 - 持久化失败不影响本次解析
        logger.debug("[Cookie] Cookie 持久化失败（忽略）: %s", e)


def ensure_session_cookies(session: requests.Session) -> dict:
    """让 session 带上可用 Cookie：优先持久化缓存，否则首页获取一次并落盘。

    裸流页面直取复用同一份 Cookie 体系（ttwid 提升页面稳定性）。**尽力而为**：
    实测匿名请求同样能取到裸流，因此这里失败不阻断调用方，返回已装载的字典。

    :return: 本次装载进 session 的 Cookie 字典（可能为空）
    """
    cached = _load_cached_cookies()
    if cached:
        session.cookies.update(cached)
        logger.info("[Cookie] 使用持久化 Cookie（%s）",
                    ", ".join(sorted(cached.keys())))
        return dict(cached)

    logger.info("[Cookie] 无持久化 Cookie，访问首页获取 ttwid ...")
    try:
        session.get(_HOME_URL, headers=PAGE_HEADERS, timeout=15,
                    allow_redirects=True)
    except requests.RequestException as e:
        logger.warning("[Cookie] 首页请求失败（非致命，继续匿名请求）: %s", e)
        return {}
    cookies = session.cookies.get_dict()
    logger.info("[Cookie] 首页获取到 Cookie 键: %s", sorted(cookies.keys()))
    if cookies.get("ttwid"):
        _save_cookies(cookies)
    else:
        logger.warning("[Cookie] ttwid 未获取，后续请求可能失败")
    return dict(cookies)


class DouyinStreamError(StreamResolveError):
    """抖音直播间解析异常。

    继承 StreamResolveError：GUI/核心层只需要捕获统一基类，
    不需要认识平台特有的异常类型。
    """


class DouyinStream:
    """抖音直播间识别与 room_id 解析。

    本类**不做任何流地址抽取**：唯一拉流架构下，流地址由
    `stream/bare_source.py` 从直播间页面直取（见模块文档）。
    """

    # ------------------------------------------------------------------
    # 直播间 URL 的结构识别（不使用整串正则）
    #
    # 规则一句话：**房间号 = 路径里「直播语义段」后面紧跟的那个数字段**。
    # 位置本身就是语义，所以 /live/{id}、/follow/live/{id}、/root/live/{id}
    # 走的是同一条规则，不需要为每种形态各写一条正则。
    # ------------------------------------------------------------------

    #: 抖音域名（www 与不带 www 等价）。只认这几个，绝不整站认领。
    _DOUYIN_HOSTS = frozenset({"douyin.com", "www.douyin.com", "live.douyin.com"})

    #: 路径里表示「这里是一个直播间」的语义段。
    _LIVE_PATH_SEGMENTS = frozenset({"live"})

    #: 域名本身即直播语义：路径第一段就是房间号（live.douyin.com/96670111738）。
    _LIVE_HOSTS = frozenset({"live.douyin.com"})

    #: 查询串里**明确命名**为直播间资源的参数（仅在没有路径解时回退使用）。
    #: 只认这两个：anchor_id / r_id / r_player / is_paidlive 等分别是主播、
    #: 推荐位、播放器与状态参数，与房间号无关，**绝不可以**拿来当 room_id。
    _ROOM_ID_QUERY_KEYS = ("room_id", "webcast_id")

    def __init__(self, live_url):
        """
        保存直播间 URL

        :param live_url: 抖音直播间URL，如 https://live.douyin.com/123456789
        """
        self.live_url = live_url
        self.room_id = None    # 解析出的房间号
        # 待尝试的 room_id 候选（关注页链接有两个）。标准直播间链接只有一个，
        # 因此这条路径上的行为与从前完全一致。
        self._room_id_candidates: list[str] = []
        logger.info("初始化: 输入 URL = %s", live_url)

    # ------------------------------------------------------------------
    # 从 URL 解析 room_id
    # ------------------------------------------------------------------
    def get_room_id(self):
        """从URL解析room_id"""
        if not self.live_url or not isinstance(self.live_url, str):
            raise DouyinStreamError("URL格式错误：直播间URL为空或类型不正确")

        candidates = self._parse_room_id_candidates(self.live_url.strip())
        if not candidates:
            raise DouyinStreamError(
                f"URL格式错误：无法解析room_id -> {self.live_url}"
            )

        self._room_id_candidates = candidates
        self.room_id = candidates[0]
        logger.info("解析 room_id 候选 = %s", candidates)
        return self.room_id

    @staticmethod
    def _split(url: str):
        """安全地 urlsplit；不是可处理的 http(s) URL 时返回 None（不抛）。

        三道门槛，缺一不可：
            - 必须是字符串且非空；
            - 必须是 http/https（协议相对、ftp、纯文本一律不认）；
            - 端口必须能解析（``parts.port`` 对非法端口抛 ValueError）。
        不满足就当作"不是直播地址"，绝不猜。
        """
        if not url or not isinstance(url, str):
            return None
        try:
            parts = urlsplit(url.strip())
            parts.port          # 非法端口在这里抛 ValueError
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https"):
            return None
        return parts

    @classmethod
    def _path_segments(cls, parts) -> list:
        """路径里的非空段：``/follow/live/123`` -> ``['follow', 'live', '123']``"""
        return [seg for seg in parts.path.split("/") if seg]

    @staticmethod
    def _is_room_id(text: str) -> bool:
        """房间号段是否合法：**只认 ASCII 0-9**。

        不能用 str.isdigit()：它对全角「１２３」、上标「²」、阿拉伯-印度数字
        「٣」同样返回 True，而那些字符不可能是抖音的房间号。位置对了但字符
        不是 ASCII 数字时，按"取不到"处理——宁可报无法识别，也不把非 ASCII
        字符串当 id 拿去拼请求。
        """
        return bool(text) and text.isascii() and text.isdigit()

    @classmethod
    def _room_id_from_path(cls, parts) -> "str | None":
        """按结构取房间号：直播语义段之后紧跟的那一个数字段。

        语义段之后不是数字（例如 /live/abc）就**不算**——位置对了但资源
        id 不合法，宁可不给结果，也不去路径其它地方找一个数字来凑。
        """
        segments = cls._path_segments(parts)
        for index, segment in enumerate(segments):
            if (segment.lower() in cls._LIVE_PATH_SEGMENTS
                    and index + 1 < len(segments)
                    and cls._is_room_id(segments[index + 1])):
                return segments[index + 1]

        # live.douyin.com/{id}：域名本身已经说明这是直播间，第一段就是房间号
        if (parts.hostname or "").lower() in cls._LIVE_HOSTS:
            if segments and cls._is_room_id(segments[0]):
                return segments[0]
        return None

    @classmethod
    def _room_id_from_query(cls, parts) -> "str | None":
        """query 回退：只认明确命名为直播间资源的参数。"""
        params = parse_qs(parts.query)
        for key in cls._ROOM_ID_QUERY_KEYS:
            for value in params.get(key, ()):
                if cls._is_room_id(value):
                    return value
        return None

    @classmethod
    def is_live_url(cls, url: str) -> bool:
        """URL 是否**是**抖音直播间地址（纯结构判断，不发请求）。

        只看「有没有直播语义」，**不要求**房间号一定是数字：房间号合不合法
        留给 get_room_id() 报 "URL格式错误：无法解析room_id"，提示更有指向性，
        也与改动前的行为一致。

        普通视频页 / 用户主页 / 搜索页都不含直播语义段，因此不会被接住。
        """
        parts = cls._split(url)
        if parts is None:
            return False
        host = (parts.hostname or "").lower()
        if host not in cls._DOUYIN_HOSTS:
            return False
        # 直播专用域名：整站都是直播间
        if host in cls._LIVE_HOSTS:
            return True
        # 路径里有直播语义段
        if any(seg.lower() in cls._LIVE_PATH_SEGMENTS
               for seg in cls._path_segments(parts)):
            return True
        # 明确命名的房间/直播资源参数
        params = parse_qs(parts.query)
        return any(key in params for key in cls._ROOM_ID_QUERY_KEYS)

    @classmethod
    def _parse_room_id_candidates(cls, url: str) -> list[str]:
        """从 URL 提取 room_id 候选（当前结构下最多一个）。

        规则：**房间号 = 路径里「直播语义段」后面紧跟的那个数字段**。
        例如 /live/{id}、/follow/live/{id}、/root/live/{id} 都落到同一条规则。
        路径里没有直播语义时，才回退到明确命名的 query 参数（room_id /
        webcast_id）。

        明确不做的事：不取最长数字、不拿 anchor_id 当房间号、不按 URL 长度猜、
        不因为路径里另有一个数字就认一个。
        """
        parts = cls._split(url)
        if parts is None:
            return []
        if (parts.hostname or "").lower() not in cls._DOUYIN_HOSTS:
            return []

        room_id = cls._room_id_from_path(parts) or cls._room_id_from_query(parts)
        return [room_id] if room_id else []

    # ------------------------------------------------------------------
    # 统一的解析结果
    # ------------------------------------------------------------------
    def get_stream_info(self) -> StreamInfo:
        """解析并返回统一的 StreamInfo（房间标识 + 空候选集）。

        **candidates 恒为空、selected 恒为 None** —— 唯一拉流架构下，可播放
        地址是裸流页面直取的结果，不来自本层（见模块文档）。GUI 取到裸流后
        自行构造唯一候选交给播放层。
        """
        if not self.room_id:
            self.get_room_id()
        return StreamInfo(
            platform=PLATFORM_NAME,
            room_id=self.room_id,
            candidates=[],
            selected=None,
            width=None,
            height=None,
        )


# ======================================================================
# 平台适配器：把抖音的房间识别能力暴露给核心层
# ======================================================================
class DouyinAdapter(PlatformAdapter):
    """抖音平台适配器。

    只负责"这个 URL 是不是抖音直播间"和"解析出统一的 StreamInfo"；
    所有抖音特有的识别细节都在 DouyinStream 里，这里不做任何再解析。
    """

    name = PLATFORM_NAME

    def can_handle(self, url: str) -> bool:
        """URL 是否属于抖音直播间（纯结构判断，不发请求）。

        判断规则与房间号提取共用同一份实现（``DouyinStream.is_live_url``），
        因此不存在"能接住却解析不出"或"解析得出却接不住"的分叉。

        只按结构判断归属，**不要求**房间号是数字：非法房间号仍然走到
        resolve() 并报原来的 "URL格式错误：无法解析room_id"。
        """
        return DouyinStream.is_live_url(url)

    def resolve(self, url: str) -> StreamInfo:
        """:raises DouyinStreamError: 解析失败"""
        return DouyinStream(url).get_stream_info()


# ======================================================================
# 命令行测试入口：只验证房间识别
# ======================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    live_url = input("请输入抖音直播间URL: ").strip()
    douyin = DouyinStream(live_url)
    try:
        info = douyin.get_stream_info()
    except DouyinStreamError as e:
        print(f"[识别失败] {e}")
    else:
        print(f"platform: {info.platform}")
        print(f"room_id:  {info.room_id}")
        print("流地址由裸流页面直取提供（stream/bare_source.py）")
