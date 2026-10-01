# -*- coding: utf-8 -*-
"""
抖音直播流地址解析模块

流程：
    直播间URL
      -> 解析 room_id
      -> 获取 Cookie (ttwid 等)
      -> 请求直播信息（三种方式按序尝试）
      -> 提取真实直播播放地址
      -> 返回 stream_url

三种获取方式（按优先级）：
    1. 页面 pace_f 数据：从直播间 HTML 的 self.__pace_f.push() 提取（当前主流方式）
    2. 页面 RENDER_DATA：旧版页面格式（兼容回退）
    3. API 接口：web/enter/ 接口（最终回退）

依赖：
    仅依赖 requests，不依赖 GUI / FFmpeg / OpenCV

使用方法：
    ds = DouyinStream("https://live.douyin.com/123456789")
    result = ds.get_stream_url()
    # result = {"room_id": "123456789", "stream_url": "...", "stream_type": "flv"}
"""

import html as html_mod
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import requests

from utils.stream_history import default_data_dir
from ..models import StreamCandidate, StreamInfo, StreamResolveError
from ..selector import select
from .base import PlatformAdapter

logger = logging.getLogger("DouyinLowLatencyViewer.stream.douyin")

#: ttwid 等 Cookie 的持久化文件（与 stream_history 同一数据目录）。
#: ttwid 是长时效的浏览器会话标识：首次首页获取后存盘复用，热启动可整段
#: 跳过首页请求（迁移审计第 1 步的解析链提速项；基线实测解析 2.2s 中
#: HTTP 链占 ~1.85s）。失效场景由 get_live_info 的"刷新重试一次"兜底。
_COOKIE_FILENAME = "douyin_cookies.json"


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

logger = logging.getLogger("DouyinLowLatencyViewer.stream.douyin")


class DouyinStreamError(StreamResolveError):
    """抖音直播流解析异常

    继承 StreamResolveError：GUI/核心层只需要捕获统一基类，
    不需要认识平台特有的异常类型。
    """


class DouyinStream:
    """抖音直播流地址解析器"""

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

    # 旧版页面 RENDER_DATA 提取模式
    _RENDER_DATA_PATTERN = re.compile(
        r'<script\s+id="RENDER_DATA"\s+type="application/json">(.+?)</script>'
    )

    # pace_f chunk 提取模式
    _PACE_F_PATTERN = re.compile(
        r'self\.__pace_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL
    )

    # 直播信息接口
    _LIVE_INFO_API = "https://live.douyin.com/webcast/room/web/enter/"

    # 直播流 URL 匹配模式（直接从 HTML 文本中提取）
    _FLV_URL_PATTERN = re.compile(
        r'(https?://pull-[a-z0-9\-]+\.flive\.douyincdn\.com/'
        r'[^\s"\'\\]+\.flv[^\s"\'\\]*)'
    )
    _HLS_URL_PATTERN = re.compile(
        r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)'
    )

    # 模拟浏览器请求头（用于页面访问）
    _PAGE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://live.douyin.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # API 专用请求头
    _API_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://live.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 请求超时时间（秒）
    _TIMEOUT = 15

    # 流地址字段优先级（含协议判定）。顺序即优先级，越靠前越优先。
    _FIELD_PRIORITY = [
        ("flv_pull_url", "flv"),
        ("hls_pull_url", "hls"),
        ("flv_pull_url_map", "flv"),
        ("hls_pull_url_map", "hls"),
        ("pull_url", "flv"),
        ("default_pull_url", "flv"),
    ]

    # 清晰度键优先级（迁移审计第 2 步修订）：**原画（FULL_HD1/origin）优先**。
    #
    # 旧策略"HD1 优先（低分辨率=低延迟）"对直播是错的：HD1/SD1 是服务端把
    # 原画**重新转码**出来的梯队，转码+重新打包天生多 1~3 秒延迟，且码率被
    # 压低；原画是主播推流的原始码流，CDN 直转直发，端到端最低延迟、最高
    # 码率。live-player（逆向报告 P0-2）正是取 main/origin——同一房间它"又
    # 快又清楚"的来源。（GUI 实测反馈：同房间我们的画面肉眼可见地比闪 live
    # 慢且糊，切原画应同时改善两者。）
    #
    # 代价与护栏：原画 1080p60 过 BGR 管道约 373MB/s，对读取与 GUI 是真实
    # 负载——切档后请观察性能面板（FPS 稳定性 / 管道积压 / 跳帧），机器撑
    # 不住时回退方案是输出限帧（-r 30）或退回本表旧顺序。
    _QUALITY_PRIORITY = (
        "FULL_HD1", "full", "origin", "FHD1", "FHD",
        "HD1", "HD", "SD1", "SD2", "SD", "0", "1",
    )

    # ---- SDK 线（迁移审计刀 3.5）----
    # 页面 pace_f 里另嵌一套 live_core_sdk（App 级）拉流数据：变体含
    # 原画直传（stream-<id>.flv 无后缀）、_or4、_uhd/_hd/_sd/_ld，以及
    # hiquhd5/hiq* 系（增强 FLV 封装的 H265——本机捆绑的 ffmpeg 5.x 内核
    # 不支持该封装，实测 "Video: none [12]" 无法解码，因此**跳过 hiq\***）。
    # App SDK 分发时间线比网页系提前约 1 秒——同房间三方实测
    # 浏览器 < 本程序(网页线) < 闪live(SDK线) 的根因；闪 live 正取此线。
    # 顺序即优先级：原画直传在前。
    _SDK_VARIANT_PRIORITY = (
        "origin", "or4", "uhd", "hd", "sd", "ld", "md",
    )
    _SDK_QUALITY_NAME = {
        "origin": "原画(SDK)", "or4": "原画(or4)",
        "uhd": "蓝光(SDK)", "hd": "超清(SDK)",
        "sd": "高清(SDK)", "ld": "流畅(SDK)", "md": "标清(SDK)",
    }

    def __init__(self, live_url):
        """
        保存直播间 URL 并创建 requests.Session

        :param live_url: 抖音直播间URL，如 https://live.douyin.com/123456789
        """
        self.live_url = live_url
        self.room_id = None    # 解析出的房间号
        # 待尝试的 room_id 候选（关注页链接有两个）。标准直播间链接只有一个，
        # 因此这条路径上的行为与从前完全一致。
        self._room_id_candidates: list[str] = []
        self.live_info = None  # 直播信息原始数据（用于调试）
        # 最近一次流地址提取所用的原始数据 (source, data)：
        # 供 get_stream_info() 还原完整候选列表，不参与任何解析决策。
        self._last_candidate_source = None

        # 使用 Session 自动管理 Cookie（关键：ttwid 等）
        self._session = requests.Session()
        #: 本次解析是否使用了持久化 Cookie（决定失败后要不要刷新重试）
        self._used_cached_cookie = False
        #: 最近一次 _resolve_streams 用的页面 HTML（刀 3.5：SDK 线从同一份
        #: 页面解出，零额外网络请求；get_stream_info 消费）
        self._last_page_html: str | None = None

        logger.info("初始化: 输入 URL = %s", live_url)

    # ------------------------------------------------------------------
    # 第1步：从 URL 解析 room_id
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
    # 第2步：获取 Cookie（ttwid 等必要 Cookie）
    # ------------------------------------------------------------------
    def _init_session(self):
        """
        访问抖音首页获取必要 Cookie（ttwid 等）。

        抖音接口/页面必须携带 ttwid Cookie 才能返回有效数据，
        先 GET 首页让服务器通过 Set-Cookie 下发。
        """
        logger.info("[Cookie] 访问首页获取 Cookie ...")
        try:
            resp = self._session.get(
                "https://live.douyin.com/",
                headers=self._PAGE_HEADERS,
                timeout=self._TIMEOUT,
                allow_redirects=True,
            )
            logger.info(
                "[Cookie] 首页请求: HTTP %d, 最终URL=%s",
                resp.status_code, resp.url,
            )
        except requests.RequestException as e:
            logger.warning("[Cookie] 首页请求失败（非致命，继续尝试）: %s", e)
            return

        # 记录获取到的 Cookie
        cookies = self._session.cookies.get_dict()
        cookie_keys = list(cookies.keys())
        logger.info("[Cookie] 获取到 Cookie 键: %s", cookie_keys)

        if "ttwid" in cookies:
            logger.info("[Cookie] ttwid = %s...%s", cookies["ttwid"][:8], cookies["ttwid"][-4:])
            # 首页拿到的 ttwid 是长时效会话标识：持久化后，后续解析可整段
            # 跳过首页请求（解析链提速项）。失效由 get_live_info 的刷新重试兜底。
            _save_cookies(cookies)
        else:
            logger.warning("[Cookie] ttwid 未获取，后续请求可能失败")

    # ------------------------------------------------------------------
    # 第3步：获取直播信息（主方式 + 备选方式）
    # ------------------------------------------------------------------
    def get_live_info(self):
        """
        请求直播信息，返回流地址结果 dict。

        策略（按优先级）：
            Cookie：优先复用持久化 Cookie（跳过首页预取，热启动提速）；
                    持久化 Cookie 下整条链路失败 → 刷新 Cookie 后重试一次。
            解析：  1. 页面 pace_f 数据（当前主流方式）
                    2. 页面 RENDER_DATA（旧版兼容）
                    3. API 接口（最终回退）

        :return: {"room_id": ..., "stream_url": ..., "stream_type": ...}
        """
        # 确保 room_id 已解析
        if not self.room_id:
            self.get_room_id()

        # 每次解析重新开始记录候选来源，避免上一次解析的残留数据被复用
        self._last_candidate_source = None

        self._ensure_session_cookies()
        try:
            return self._resolve_streams()
        except DouyinStreamError:
            if not self._used_cached_cookie:
                raise
            # 持久化 Cookie 可能已过期/失效：刷新一次再走整条链路
            logger.warning("[Cookie] 持久化 Cookie 下解析失败，刷新 Cookie 后重试一次")
            self._used_cached_cookie = False
            self._init_session()
            return self._resolve_streams()

    def _ensure_session_cookies(self):
        """保证 Session 带有可用 Cookie：优先持久化缓存，否则首页获取。"""
        cached = _load_cached_cookies()
        if cached:
            self._session.cookies.update(cached)
            self._used_cached_cookie = True
            logger.info(
                "[Cookie] 使用持久化 Cookie（%s），跳过首页预取",
                ", ".join(sorted(cached.keys())),
            )
            return
        self._used_cached_cookie = False
        self._init_session()

    def _resolve_streams(self):
        """原 get_live_info 主体：房间页 → pace_f → RENDER_DATA → 正则 → API。"""
        # 获取直播间页面 HTML（多个解析方式共用；刀 3.5 的 SDK 线也从这份
        # 页面解出，见 get_stream_info）
        page_html = self._fetch_room_page()
        self._last_page_html = page_html

        # 方式1: 页面 pace_f 数据
        if page_html:
            try:
                result = self._extract_from_pace_f(page_html)
                if result:
                    return result
            except DouyinStreamError:
                raise  # 明确错误（如未开播），直接抛出
            except Exception as e:
                logger.warning("[pace_f方式] 失败: %s", e, exc_info=True)

        # 方式2: 页面 RENDER_DATA（旧版兼容）
        if page_html:
            try:
                result = self._extract_from_render_data(page_html)
                if result:
                    return result
            except Exception as e:
                logger.warning("[RENDER_DATA方式] 失败: %s", e, exc_info=True)

        # 方式3: 直接正则提取流 URL（终极页面回退）
        if page_html:
            try:
                result = self._extract_urls_regex(page_html)
                if result:
                    return result
            except Exception as e:
                logger.warning("[正则方式] 失败: %s", e, exc_info=True)

        # 方式4: API 接口
        try:
            result = self._get_stream_from_api()
            if result:
                return result
        except DouyinStreamError:
            raise
        except Exception as e:
            logger.warning("[API方式] 失败: %s", e, exc_info=True)

        raise DouyinStreamError(
            f"所有解析方式均失败 (room_id={self.room_id})"
        )

    # ------------------------------------------------------------------
    # 获取直播间页面 HTML
    # ------------------------------------------------------------------
    def _fetch_room_page(self):
        """请求直播间页面，返回 HTML 文本；失败返回 None"""
        room_url = f"https://live.douyin.com/{self.room_id}"
        logger.info("[页面] 请求直播间: %s", room_url)

        try:
            resp = self._session.get(
                room_url,
                headers=self._PAGE_HEADERS,
                timeout=self._TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            logger.warning("[页面] 请求失败: %s", e)
            return None

        logger.info(
            "[页面] HTTP %d, 响应长度=%d bytes, 最终URL=%s",
            resp.status_code, len(resp.text), resp.url,
        )

        if resp.status_code != 200:
            logger.warning("[页面] HTTP %d，无法获取页面", resp.status_code)
            return None

        return resp.text

    # ------------------------------------------------------------------
    # 方式1: 从 pace_f 数据提取（当前主流方式）
    # ------------------------------------------------------------------
    def _extract_from_pace_f(self, html):
        """
        从 self.__pace_f.push([1,"..."]) 中提取 stream_url JSON。

        抖音当前版本使用 React Server Components (RSC) 格式，
        直播数据嵌入在 __pace_f chunk 的 JSON 字符串中。
        chunk 内容使用 JavaScript 转义：\" -> ", \\u0026 -> & 等。
        """
        logger.info("[pace_f] 搜索包含 stream_url 的 chunk ...")

        # 找到包含流数据的 chunk
        stream_chunk = None
        chunk_count = 0
        for m in self._PACE_F_PATTERN.finditer(html):
            chunk_count += 1
            chunk = m.group(1)
            if "stream_url" in chunk or "flv_pull_url" in chunk or "hls_pull_url" in chunk:
                stream_chunk = chunk
                logger.info(
                    "[pace_f] 找到流数据 chunk (第%d个, len=%d)",
                    chunk_count, len(chunk),
                )
                break

        logger.info("[pace_f] 共扫描 %d 个 chunk", chunk_count)

        if not stream_chunk:
            logger.warning("[pace_f] 未找到包含 stream_url 的 chunk")
            return None

        # 在 chunk 中查找 "stream_url":{...} JSON 对象
        # chunk 内容使用 \" 转义引号，先定位 \"stream_url\" 位置
        result = self._parse_stream_url_from_chunk(stream_chunk)
        if result:
            return result

        # 如果结构化解析失败，尝试在 chunk 中直接搜索 URL
        result = self._find_urls_in_text(stream_chunk, "[pace_f chunk]")
        if result:
            return result

        logger.warning("[pace_f] chunk 中未找到可用流地址")
        return None

    def _parse_stream_url_from_chunk(self, chunk):
        """
        从 pace_f chunk 文本中定位并解析 stream_url JSON 对象。

        chunk 中的格式如：
            ...\"stream_url\":{\"flv_pull_url\":{\"FULL_HD1\":\"http://...flv?...\"}}...
        """
        # 搜索 \"stream_url\" 或 "stream_url" 的位置
        for marker in ['\\"stream_url\\"', '"stream_url"']:
            idx = chunk.find(marker)
            if idx < 0:
                continue

            logger.info("[pace_f] 找到 stream_url 标记 at offset %d", idx)

            # 找到标记后的 { 开始位置
            brace_start = chunk.find("{", idx + len(marker))
            if brace_start < 0:
                continue

            # 从 brace_start 开始，找到匹配的 }
            # 使用栈匹配花括号
            depth = 0
            brace_end = -1
            for i in range(brace_start, len(chunk)):
                c = chunk[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        brace_end = i + 1
                        break

            if brace_end < 0:
                logger.warning("[pace_f] 未找到匹配的 }")
                continue

            # 提取 JSON 片段
            json_fragment = chunk[brace_start:brace_end]

            # 解码 JavaScript 转义：\" -> ", \\/ -> /, \u0026 -> &
            # 注意：\uXXXX 是 JSON 标准转义，json.loads 可以处理
            # 但 \" 在 chunk 中是 \\\" (HTML中的 \\"), 需要特殊处理
            try:
                # 尝试直接解析（json.loads 会处理 \uXXXX）
                stream_url_info = json.loads(json_fragment)
            except json.JSONDecodeError:
                # 如果直接解析失败，尝试修复转义
                try:
                    # 将 \\" 替换为 " (JavaScript 字符串中的转义引号)
                    fixed = json_fragment.replace('\\"', '"')
                    stream_url_info = json.loads(fixed)
                except json.JSONDecodeError as e:
                    logger.warning("[pace_f] stream_url JSON 解析失败: %s", e)
                    logger.debug("[pace_f] 片段前200字符: %s", json_fragment[:200])
                    continue

            logger.info(
                "[pace_f] stream_url 解析成功, 键: %s",
                list(stream_url_info.keys()) if isinstance(stream_url_info, dict) else type(stream_url_info).__name__,
            )

            # 从 stream_url_info 中提取流地址
            result = self._extract_stream_from_data(stream_url_info, "pace_f stream_url")
            if result:
                return result

        return None

    # ------------------------------------------------------------------
    # 方式2: 从 RENDER_DATA 提取（旧版兼容）
    # ------------------------------------------------------------------
    def _extract_from_render_data(self, html):
        """
        从直播间 HTML 提取 RENDER_DATA（旧版格式）。

        页面结构：
            <script id="RENDER_DATA" type="application/json">url_encoded_json</script>
        """
        match = self._RENDER_DATA_PATTERN.search(html)
        if not match:
            logger.info("[RENDER_DATA] 未找到标签（可能为当前新版页面）")
            return None

        raw_data = match.group(1)
        decoded_data = unquote(raw_data)
        logger.info("[RENDER_DATA] 解码成功，长度=%d", len(decoded_data))

        try:
            render_data = json.loads(decoded_data)
        except json.JSONDecodeError as e:
            logger.warning("[RENDER_DATA] JSON 解析失败: %s", e)
            return None

        logger.info("[RENDER_DATA] 顶层键: %s", list(render_data.keys()))

        # 查找 roomInfo
        room_info = None
        for key, value in render_data.items():
            if isinstance(value, dict) and "roomInfo" in value:
                room_info = value["roomInfo"]
                break

        if not room_info:
            logger.warning("[RENDER_DATA] 未找到 roomInfo")
            return None

        # 检查开播状态
        room = room_info.get("room") or {}
        status = room.get("status")
        if status != 2:
            raise DouyinStreamError(
                f"主播未开播 (status={status}, msg={room.get('status_msg', '')})"
            )

        # 多路径提取流地址
        # 路径1: liveStreamInfo.streamData
        live_stream_info = room_info.get("liveStreamInfo") or {}
        stream_data_str = live_stream_info.get("streamData")
        if stream_data_str:
            try:
                stream_data = json.loads(stream_data_str) if isinstance(stream_data_str, str) else stream_data_str
                result = self._extract_stream_from_data(stream_data, "RENDER_DATA streamData")
                if result:
                    self.live_info = room_info
                    return result
            except json.JSONDecodeError:
                pass

        # 路径2: room.stream_url
        stream_url_info = room.get("stream_url") or {}
        result = self._extract_stream_from_data(stream_url_info, "RENDER_DATA room.stream_url")
        if result:
            self.live_info = room_info
            return result

        # 路径3: 深度搜索
        result = self._deep_search_stream_url(room_info, "RENDER_DATA roomInfo")
        if result:
            self.live_info = room_info
            return result

        return None

    # ------------------------------------------------------------------
    # 方式3: 直接正则提取流 URL（终极页面回退）
    # ------------------------------------------------------------------
    def _extract_urls_regex(self, html):
        """
        当结构化解析全部失败时，用正则直接从 HTML 提取流 URL。

        匹配抖音 CDN 拉流地址模式：
            FLV: http(s)://pull-*.flive.douyincdn.com/*.flv?...
            HLS: http(s)://.../*.m3u8?...
        """
        logger.info("[正则] 从 HTML 中直接搜索流 URL ...")

        # 先搜索 FLV
        for m in self._FLV_URL_PATTERN.finditer(html):
            url = m.group(1)
            # 解码 \u0026 -> & (JavaScript Unicode 转义)
            url = self._decode_js_unicode(url)
            if url.startswith("http") and ".flv" in url:
                logger.info("[正则] 命中 FLV: %s...%s", url[:60], url[-30:])
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": "flv",
                }

        # 再搜索 HLS/m3u8
        for m in self._HLS_URL_PATTERN.finditer(html):
            url = m.group(1)
            url = self._decode_js_unicode(url)
            if url.startswith("http") and ".m3u8" in url:
                logger.info("[正则] 命中 HLS: %s...%s", url[:60], url[-30:])
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": "hls",
                }

        logger.warning("[正则] 未找到匹配的流 URL")
        return None

    # ------------------------------------------------------------------
    # 在任意文本中搜索流 URL（用于 pace_f chunk 等）
    # ------------------------------------------------------------------
    def _find_urls_in_text(self, text, source=""):
        """在文本中搜索 flv/hls 流 URL"""
        # 搜索 FLV URL
        for m in self._FLV_URL_PATTERN.finditer(text):
            url = self._decode_js_unicode(m.group(1))
            if url.startswith("http") and ".flv" in url:
                logger.info("[%s] 发现 FLV URL: %s...%s", source, url[:60], url[-30:])
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": "flv",
                }

        # 搜索 HLS URL
        for m in self._HLS_URL_PATTERN.finditer(text):
            url = self._decode_js_unicode(m.group(1))
            if url.startswith("http") and ".m3u8" in url:
                logger.info("[%s] 发现 HLS URL: %s...%s", source, url[:60], url[-30:])
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": "hls",
                }

        return None

    # ------------------------------------------------------------------
    # 方式4: 从 API 接口获取
    # ------------------------------------------------------------------
    def _get_stream_from_api(self):
        """
        从 API 接口获取直播信息。

        接口：POST https://live.douyin.com/webcast/room/web/enter/
        需要 Cookie（ttwid）才能返回有效数据。
        """
        logger.info("[API] 请求接口: %s", self._LIVE_INFO_API)

        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "enter_from": "web_live",
            "room_id": self.room_id,
        }
        logger.info("[API] 参数: %s", params)

        try:
            resp = self._session.post(
                self._LIVE_INFO_API,
                params=params,
                headers=self._API_HEADERS,
                timeout=self._TIMEOUT,
            )
        except requests.Timeout:
            raise DouyinStreamError(f"[API] 请求超时 (room_id={self.room_id})")
        except requests.RequestException as e:
            raise DouyinStreamError(f"[API] 网络异常: {e} (room_id={self.room_id})")

        logger.info("[API] HTTP %d, 响应长度=%d", resp.status_code, len(resp.text))

        if resp.status_code != 200:
            raise DouyinStreamError(f"[API] HTTP {resp.status_code}，接口不可用")

        try:
            data = resp.json()
        except ValueError:
            raise DouyinStreamError(f"[API] 响应非JSON (HTTP {resp.status_code})")

        status_code = data.get("status_code")
        status_msg = data.get("status_msg", "")
        logger.info("[API] status_code=%s, status_msg=%s", status_code, status_msg)

        if status_code != 0:
            raise DouyinStreamError(
                f"[API] 接口错误: status_code={status_code}, msg={status_msg}"
            )

        # 提取房间数据
        room_data = (data.get("data") or {}).get("data") or []
        if isinstance(room_data, list):
            room = room_data[0] if room_data else None
        elif isinstance(room_data, dict):
            room = room_data
        else:
            room = None

        if not room:
            logger.error(
                "[API] data.data 为空, 顶层键=%s, data键=%s",
                list(data.keys()),
                list((data.get("data") or {}).keys()),
            )
            raise DouyinStreamError(f"[API] 直播间不存在 (room_id={self.room_id})")

        logger.info("[API] 房间数据键: %s", list(room.keys()) if isinstance(room, dict) else "N/A")

        # 校验开播状态
        status = room.get("status")
        status_msg_api = room.get("status_msg", "")
        logger.info("[API] 房间状态: status=%s, msg=%s", status, status_msg_api)

        if status != 2:
            raise DouyinStreamError(
                f"主播未开播 (status={status}, msg={status_msg_api})"
            )

        # 提取流地址
        stream_url_info = room.get("stream_url") or {}
        result = self._extract_stream_from_data(stream_url_info, "API stream_url")
        if result:
            self.live_info = room
            return result

        # 深度搜索
        result = self._deep_search_stream_url(room, "API room")
        if result:
            self.live_info = room
            return result

        raise DouyinStreamError("[API] 接口返回数据中未找到可用流地址")

    # ------------------------------------------------------------------
    # 流地址提取：从 stream_url 信息 dict 中按优先级提取
    # ------------------------------------------------------------------
    def iter_stream_candidates(self, data, source=""):
        """
        从数据中按优先级枚举**全部**候选流地址。

        这里的顺序就是唯一的优先级定义：_extract_stream_from_data() 取它的第一个，
        DouyinAdapter 把完整列表交给核心作为 StreamCandidate。

        字段优先级：
            1. flv_pull_url        -> protocol = "flv"
            2. hls_pull_url        -> protocol = "hls"
            3. flv_pull_url_map    -> protocol = "flv"
            4. hls_pull_url_map    -> protocol = "hls"
            5. pull_url            -> protocol = "flv"
            6. default_pull_url    -> protocol = "flv"

        :param data:   可能包含流地址的 dict
        :param source: 来源标签（用于日志）
        :return: StreamCandidate 列表（顺序即优先级）；无可用地址返回 []
        """
        # 记录来源，供 get_stream_info() 还原候选集合。只记录，不参与解析决策。
        self._last_candidate_source = (source, data)

        if not data or not isinstance(data, dict):
            return []

        logger.info("[%s] 可用键: %s", source, list(data.keys()))

        candidates = []
        for field_name, stream_type in self._FIELD_PRIORITY:
            field_value = data.get(field_name)
            if not field_value:
                continue
            for quality, url in self._iter_field_urls(field_value):
                url = self._decode_js_unicode(url)
                if not url:
                    continue
                logger.info(
                    "[%s] 命中 '%s', type=%s, quality=%s, url=%s...%s",
                    source, field_name, stream_type, quality,
                    url[:60], url[-30:] if len(url) > 90 else "",
                )
                candidates.append(StreamCandidate(
                    url=url,
                    protocol=stream_type,
                    quality=quality,
                    metadata={"field": field_name},
                ))

        return candidates

    @staticmethod
    def _iter_field_urls(field):
        """
        枚举单个字段里的可用 URL，顺序 = 清晰度优先级。

        先按 _QUALITY_PRIORITY 取，未覆盖的键再按原字典顺序补在后面
        （与旧 _pick_url 的回退顺序一致）。

        :param field: 可能为 dict(清晰度 -> URL) 或 str
        :yield: (quality_key 或 None, url)
        """
        if isinstance(field, str):
            if field.strip():
                yield None, field.strip()
            return

        if not isinstance(field, dict):
            return

        seen = set()
        for key in DouyinStream._QUALITY_PRIORITY:
            url = field.get(key)
            if isinstance(url, str) and url.strip():
                seen.add(key)
                logger.info("[Quality] candidate: %s (available: %s)",
                            key, list(field.keys()))
                yield key, url.strip()

        for key, url in field.items():
            if key in seen:
                continue
            if isinstance(url, str) and url.strip():
                logger.info("[Quality] candidate: %s", key)
                yield key, url.strip()

    def _extract_stream_from_data(self, data, source=""):
        """
        从数据中按优先级提取**第一个**可用流地址。

        :param data:   可能包含流地址的 dict
        :param source: 来源标签（用于日志）
        :return: {"room_id", "stream_url", "stream_type"} 或 None
        """
        candidates = self.iter_stream_candidates(data, source)
        if not candidates:
            return None

        first = candidates[0]
        return {
            "room_id": self.room_id,
            "stream_url": first.url,
            "stream_type": first.protocol,
        }

    # ------------------------------------------------------------------
    # 深度搜索：在整棵数据树中搜索含 flv/hls 的 URL
    # ------------------------------------------------------------------
    def _deep_search_stream_url(self, data, source="", depth=0, max_depth=5):
        """
        当标准路径均失败时，递归搜索包含 flv/hls 的 URL 字符串。
        """
        if depth > max_depth:
            return None

        if isinstance(data, str):
            if ("flv" in data or "hls" in data) and data.startswith("http"):
                url = self._decode_js_unicode(data)
                stream_type = "flv" if ".flv" in url else "hls"
                logger.info(
                    "[%s] 深度搜索发现 (depth=%d): type=%s, url=%s...%s",
                    source, depth, stream_type,
                    url[:60], url[-30:] if len(url) > 90 else "",
                )
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": stream_type,
                }
            return None

        if isinstance(data, dict):
            for value in data.values():
                result = self._deep_search_stream_url(value, source, depth + 1, max_depth)
                if result:
                    return result
            return None

        if isinstance(data, list):
            for item in data:
                result = self._deep_search_stream_url(item, source, depth + 1, max_depth)
                if result:
                    return result
            return None

        return None

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_url(field):
        """
        从接口字段中提取优先级最高的一个流地址。

        延迟优化：优先选择 HD（720p）而非 UHD（1080p），
        因为更低分辨率 = 更少数据 = 更低网络+解码+管道延迟。
        顺序定义在 _QUALITY_PRIORITY，枚举实现只有 _iter_field_urls 一份。

        :param field: 可能为 dict(清晰度->URL) 或 str
        :return: 第一个非空URL；无可用地址返回 None
        """
        for _quality, url in DouyinStream._iter_field_urls(field):
            return url
        return None

    @staticmethod
    def _decode_js_unicode(text):
        """
        解码 JavaScript Unicode 转义序列。

        例如: \\u0026 -> &, \\u002F -> /

        :param text: 可能包含 \\uXXXX 转义的字符串
        :return: 解码后的字符串
        """
        if not isinstance(text, str) or "\\u" not in text:
            return text
        try:
            # json.loads 能正确处理 \uXXXX 转义
            # 但需要先确保字符串本身是有效的 JSON 字符串（加引号）
            return json.loads(f'"{text}"')
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 回退：手动替换常见转义
            result = text
            result = result.replace("\\u0026", "&")
            result = result.replace("\\u002F", "/")
            result = result.replace("\\u003A", ":")
            result = result.replace("\\u003D", "=")
            result = result.replace("\\u003B", ";")
            return result

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def get_stream_url(self):
        """
        获取真实播放地址。

        :return: {"room_id": str, "stream_url": str, "stream_type": "flv"|"hls"}
        :raises DouyinStreamError: 解析失败
        """
        logger.info("=" * 60)
        logger.info("开始获取直播流地址: URL = %s", self.live_url)
        logger.info("=" * 60)

        # 确保 room_id 已解析
        if not self.room_id:
            self.get_room_id()

        result = self._get_live_info_with_candidates()

        if result and result.get("stream_url"):
            logger.info(
                ">>> 获取成功 <<<  room_id=%s, stream_type=%s, url=%s",
                result.get("room_id"),
                result.get("stream_type"),
                result.get("stream_url", "")[:80] + "...",
            )

            # 分辨率不再在解析层探测（迁移审计刀1）：FFmpegReader 从子进程
            # stderr 的 dump 行自报，免去这里对媒体地址的第二次完整连流。
            return result

        raise DouyinStreamError(
            f"没有可用播放地址 (room_id={self.room_id})"
        )

    def _get_live_info_with_candidates(self):
        """按 room_id 候选依次尝试直播信息请求。

        房间号由结构解析给出（见 _parse_room_id_candidates），当前每种 URL
        形态都只有一个候选，因此这里**只请求一次**，错误与异常原样抛出。

        保留"遍历候选"这层结构是为了不改变既有的错误语义（全部失败时抛
        第一个候选的异常）；不要再往候选里塞 anchor_id 之类与房间无关的 id。
        """
        candidates = self._room_id_candidates or (
            [self.room_id] if self.room_id else [])
        first_error = None
        last_result = None
        for index, candidate in enumerate(candidates):
            if index:
                logger.info("[API] 主候选未取到播放地址，改用候选 id=%s", candidate)
            self.room_id = candidate
            try:
                result = self.get_live_info()
            except DouyinStreamError as e:
                if first_error is None:
                    first_error = e
                continue
            if result and result.get("stream_url"):
                return result
            last_result = result

        self.room_id = candidates[0] if candidates else None
        if first_error is not None:
            raise first_error
        return last_result

    def _extract_sdk_candidates(self, page_html: str) -> list:
        """从页面 HTML 解出 live_core_sdk（App 级）拉流线候选（刀 3.5）。

        背景：页面里嵌着两套分发体系——网页系 stream_url.flv_pull_url（浏览
        器梯队）与 App SDK 系 live_core_sdk_data.pull_data.stream_data（变体
        hiquhd5/... ）。SDK 系的分发时间线比网页系提前约 1 秒（同房间三方实
        测 浏览器 < 本程序(网页线) < 闪live(SDK线)），闪 live 正取此线。

        实现要点：
            - 从**已取到的同一份页面**解出，零额外网络请求；
            - SDK 数据是**多重转义**（HTML 实体 + 反斜杠 u0026/u002F 可能叠
              多层），用"任意深度反斜杠折叠"清理；
            - 解析失败/无 SDK 数据返回 []，网页线原样兜底，主流程零风险。
        """
        if not page_html:
            return []
        out, seen = [], set()
        idx = 0
        while True:
            i = page_html.find('\\"stream_data\\"', idx)
            if i < 0:
                break
            idx = i + 1
            window = page_html[i: i + 40000]
            dec = html_mod.unescape(html_mod.unescape(window))
            dec = re.sub(r"\\+u0026", "&", dec)
            dec = re.sub(r"\\+u002F", "/", dec)
            dec = dec.replace("\\/", "/")
            for u in re.findall(r'https?://[^\s"<>]+?\.flv[^\s"<>]*', dec):
                # 转义折叠可能留下孤立反斜杠（如 t_id 值尾部的 \\/）——
                # 正常 URL 不含反斜杠，统一剥掉再入库
                u = u.replace("\\", "")
                if u in seen:
                    continue
                seen.add(u)
                fn = u.split("/")[-1].split("?")[0]
                base = fn[:-4] if fn.lower().endswith(".flv") else fn
                # stream-<id>.flv（无质量后缀）= 原画直传
                variant = ("origin" if re.fullmatch(r"stream-\d+", base)
                           else next((v for v in self._SDK_VARIANT_PRIORITY
                                      if base == v or base.endswith("_" + v)), None))
                if variant is None:
                    continue  # hiq*（H265 增强封装，本运行时不可解）等未知变体跳过
                out.append(StreamCandidate(
                    url=u,
                    protocol="flv",
                    quality=self._SDK_QUALITY_NAME.get(variant, variant),
                    metadata={"field": "sdk_stream_data", "variant": variant},
                ))
        prio = {v: i for i, v in enumerate(self._SDK_VARIANT_PRIORITY)}
        out.sort(key=lambda c: prio.get(
            (c.metadata or {}).get("variant", ""), 99))
        return out[:10]

    def get_stream_info(self):
        """
        解析并返回统一的 StreamInfo（候选流 + 选中流 + 分辨率）。

        行为与 get_stream_url() 完全一致（分辨率检测、异常、选流结果都不变），
        只是把结果整理成核心层认识的模型：
            - candidates 为完整候选列表，顺序即现有优先级；
            - 只有当完整列表的第一个候选与 get_stream_url() 实际选中的地址
              一致时才采用它，否则退化为单候选，保证绝不报告错误候选。

        :raises DouyinStreamError: 解析失败
        """
        result = self.get_stream_url()
        url = result["stream_url"]

        candidates = []
        source = self._last_candidate_source
        if source and source[1] is not None:
            enumerated = self.iter_stream_candidates(source[1], source[0])
            if enumerated and enumerated[0].url == url:
                candidates = enumerated
        if not candidates:
            candidates = [StreamCandidate(url=url, protocol=result["stream_type"])]

        # 刀 3.5：SDK 线候选（App 级分发时间线，快网页线约 1 秒）。
        # 从同一份页面解出、零额外网络请求。**默认关闭**：SDK 全系是增强
        # FLV 封装的 H265，当前捆绑的 ffmpeg 5.x 运行时无法解码——开着会让
        # 每次连接白试 4-8 秒才落到网页线（启动回归）。运行时升级到支持
        # 增强 FLV 的 ffmpeg（≥7.1）后，设 DLV_SDK_LINE=1 启用。
        if os.environ.get("DLV_SDK_LINE") == "1":
            try:
                sdk = self._extract_sdk_candidates(self._last_page_html or "")
            except Exception as e:  # noqa: BLE001 - SDK 线解析失败不影响网页线
                logger.debug("[SDK] SDK 线解析失败（忽略）: %s", e)
                sdk = []
            if sdk:
                web_urls = {c.url for c in candidates}
                candidates = [c for c in sdk if c.url not in web_urls] + candidates

        return StreamInfo(
            platform=DouyinAdapter.name,
            room_id=result["room_id"],
            candidates=candidates,
            selected=select(candidates),
            # 分辨率恒 None：解析层不再探测（刀1），由 FFmpegReader 自报
            width=None,
            height=None,
        )


# ======================================================================
# 平台适配器：把抖音的解析能力暴露给核心层
# ======================================================================
class DouyinAdapter(PlatformAdapter):
    """抖音平台适配器。

    只负责"这个 URL 是不是抖音直播间"和"解析出统一的 StreamInfo"；
    所有抖音特有的解析细节都在 DouyinStream 里，这里不做任何再解析。
    """

    name = "douyin"

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
# 命令行测试入口
# ======================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    live_url = input("请输入抖音直播间URL: ").strip()
    douyin = DouyinStream(live_url)
    try:
        result = douyin.get_stream_url()
    except DouyinStreamError as e:
        print(f"[解析失败] {e}")
    else:
        print(f"room_id:     {result['room_id']}")
        print(f"stream_type: {result['stream_type']}")
        print(f"stream_url:  {result['stream_url']}")
