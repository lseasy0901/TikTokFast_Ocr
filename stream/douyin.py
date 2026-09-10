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

import json
import logging
import re
from urllib.parse import unquote

import requests

logger = logging.getLogger("DouyinLowLatencyViewer.stream.douyin")


class DouyinStreamError(Exception):
    """抖音直播流解析异常"""


class DouyinStream:
    """抖音直播流地址解析器"""

    # 直播间URL匹配模式：https://live.douyin.com/{room_id}
    _ROOM_URL_PATTERN = re.compile(r"^https?://live\.douyin\.com/(\d+)")

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

    def __init__(self, live_url):
        """
        保存直播间 URL 并创建 requests.Session

        :param live_url: 抖音直播间URL，如 https://live.douyin.com/123456789
        """
        self.live_url = live_url
        self.room_id = None    # 解析出的房间号
        self.live_info = None  # 直播信息原始数据（用于调试）

        # 使用 Session 自动管理 Cookie（关键：ttwid 等）
        self._session = requests.Session()

        logger.info("初始化: 输入 URL = %s", live_url)

    # ------------------------------------------------------------------
    # 第1步：从 URL 解析 room_id
    # ------------------------------------------------------------------
    def get_room_id(self):
        """从URL解析room_id"""
        if not self.live_url or not isinstance(self.live_url, str):
            raise DouyinStreamError("URL格式错误：直播间URL为空或类型不正确")

        match = self._ROOM_URL_PATTERN.match(self.live_url.strip())
        if not match:
            raise DouyinStreamError(
                f"URL格式错误：无法解析room_id -> {self.live_url}"
            )

        self.room_id = match.group(1)
        logger.info("解析 room_id = %s", self.room_id)
        return self.room_id

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
        else:
            logger.warning("[Cookie] ttwid 未获取，后续请求可能失败")

    # ------------------------------------------------------------------
    # 第3步：获取直播信息（主方式 + 备选方式）
    # ------------------------------------------------------------------
    def get_live_info(self):
        """
        请求直播信息，返回流地址结果 dict。

        策略（按优先级）：
            1. 页面 pace_f 数据（当前主流方式）
            2. 页面 RENDER_DATA（旧版兼容）
            3. API 接口（最终回退）

        :return: {"room_id": ..., "stream_url": ..., "stream_type": ...}
        """
        # 确保 room_id 已解析
        if not self.room_id:
            self.get_room_id()

        # 初始化 Session Cookie
        self._init_session()

        # 获取直播间页面 HTML（多个解析方式共用）
        page_html = self._fetch_room_page()

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
    def _extract_stream_from_data(self, data, source=""):
        """
        从数据中按优先级提取流地址。

        优先级：
            1. flv_pull_url        -> stream_type = "flv"
            2. hls_pull_url        -> stream_type = "hls"
            3. flv_pull_url_map    -> stream_type = "flv"
            4. hls_pull_url_map    -> stream_type = "hls"
            5. pull_url            -> stream_type = "flv"
            6. default_pull_url    -> stream_type = "flv"

        :param data:   可能包含流地址的 dict
        :param source: 来源标签（用于日志）
        :return: {"room_id", "stream_url", "stream_type"} 或 None
        """
        if not data or not isinstance(data, dict):
            return None

        logger.info("[%s] 可用键: %s", source, list(data.keys()))

        field_priority = [
            ("flv_pull_url", "flv"),
            ("hls_pull_url", "hls"),
            ("flv_pull_url_map", "flv"),
            ("hls_pull_url_map", "hls"),
            ("pull_url", "flv"),
            ("default_pull_url", "flv"),
        ]

        for field_name, stream_type in field_priority:
            field_value = data.get(field_name)
            if not field_value:
                continue
            url = self._pick_url(field_value)
            if url:
                # 解码 JS Unicode 转义（\u0026 -> &）
                url = self._decode_js_unicode(url)
                logger.info(
                    "[%s] 命中 '%s', type=%s, url=%s...%s",
                    source, field_name, stream_type,
                    url[:60], url[-30:] if len(url) > 90 else "",
                )
                return {
                    "room_id": self.room_id,
                    "stream_url": url,
                    "stream_type": stream_type,
                }

        return None

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
        从接口字段中提取一个可用的流地址。

        延迟优化：优先选择 HD（720p）而非 UHD（1080p），
        因为更低分辨率 = 更少数据 = 更低网络+解码+管道延迟。

        :param field: 可能为 dict(清晰度->URL) 或 str
        :return: 第一个非空URL；无可用地址返回 None
        """
        if isinstance(field, str):
            return field.strip() or None
        if isinstance(field, dict):
            # 延迟优先：HD(720p) > FHD(1080p) > FULL_HD1(UHD) > SD
            # HD 是延迟和清晰度的最佳平衡点
            for preferred_key in ("HD1", "HD", "FHD1", "FHD", "FULL_HD1", "full", "origin", "SD1", "SD2", "SD", "0", "1"):
                url = field.get(preferred_key)
                if isinstance(url, str) and url.strip():
                    logger.info("[Quality] selected: %s (available: %s)",
                                preferred_key, list(field.keys()))
                    return url.strip()
            # 回退：取第一个非空
            for key, url in field.items():
                if isinstance(url, str) and url.strip():
                    logger.info("[Quality] fallback: %s", key)
                    return url.strip()
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

        result = self.get_live_info()

        if result and result.get("stream_url"):
            logger.info(
                ">>> 获取成功 <<<  room_id=%s, stream_type=%s, url=%s",
                result.get("room_id"),
                result.get("stream_type"),
                result.get("stream_url", "")[:80] + "...",
            )

            # 检测视频分辨率
            video_info = self._detect_video_resolution(result.get("stream_url"))

            # 添加视频信息到结果
            if video_info:
                result.update({
                    "video_width": video_info.width,
                    "video_height": video_info.height,
                    "video_pix_fmt": video_info.pix_fmt,
                    "video_fps": video_info.fps,
                    "video_codec": video_info.codec_name,
                })

            return result

        raise DouyinStreamError(
            f"没有可用播放地址 (room_id={self.room_id})"
        )

    def _detect_video_resolution(self, stream_url: str):
        """
        检测视频流的实际分辨率

        参数：
            stream_url: 流媒体 URL

        返回：
            VideoInfo 对象，检测失败返回 None
        """
        try:
            from .video_info import get_video_info, VideoInfo

            video_info = get_video_info(stream_url)

            if video_info.width and video_info.height:
                logger.info(
                    "[VIDEO INFO] width=%d, height=%d, pix_fmt=%s, fps=%s, codec=%s",
                    video_info.width,
                    video_info.height,
                    video_info.pix_fmt or "unknown",
                    f"{video_info.fps:.2f}" if video_info.fps else "unknown",
                    video_info.codec_name or "unknown"
                )
                return video_info
            else:
                logger.warning("[VIDEO INFO] 分辨率检测失败，使用默认值")
                return None

        except ImportError:
            logger.warning("[VIDEO INFO] video_info 模块导入失败")
            return None
        except Exception as e:
            logger.warning("[VIDEO INFO] 分辨率检测异常: %s", e)
            return None


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
