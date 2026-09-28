# -*- coding: utf-8 -*-
"""feijipan 分享链接 → 临时直链（服务端封装）。

来源
----
本模块是已经实测通过的单文件工具 ``feijipan_direct.py`` 的服务端封装。
请求链与算法**逐字保留**，只做三处必要的工程化改动：

1. 去掉 CLI（``argparse``/``sys.argv``/``sys.exit``/``print``）与 ``main()``；
2. **去掉两处 AES 静默回落**。原工具为了独立运行容错，在解密内置密钥失败时
   回落到字符串 ``"feijipan_default_key"``，在加密失败时回落到
   ``md5(plaintext)`` —— 那会用一个**未被验证过的**参数继续发出真实请求。
   服务端不这么做：AES 失败即明确失败（见 :class:`FeijipanResolveError`）。
3. 把 ``API_BASE`` 变成可注入的构造参数，供测试指向本地 HTTP 服务端。
   **默认值与原工具完全一致**（``https://api.feijipan.com/ws/``），
   生产路径上的请求链与请求头逐字不变。

职责边界（刻意为之）
--------------------
本模块**只**做「分享链接 → 临时直链」这一件事：

* 不建 FastAPI 路由，不碰数据库，不碰 release registry；
* 不保存 ``direct_url``，不写任何文件；
* 不把 ``direct_url`` 写进日志 —— 它只存在于本次调用的返回值里。

对外接口
--------
``await resolve(share_url, share_password="", filename=None) -> dict``
    成功返回
    ``{"code": 200, "url", "file_name", "file_size", "message", "selection"}``；
    失败抛 :class:`FeijipanResolveError`（消息面向调用方，**不含**临时直链）。

文件选择（Phase 1.5）
---------------------
``filename`` 给定时按 ``fileName`` **精确匹配**；匹配不到就是 ``404``，
**绝不**退回第一个文件。``filename`` 为 ``None`` 时保留原工具的
「第一个分享项的第一个文件」行为，属于**兼容模式**，返回值里的
``selection`` 会标成 ``first_file_compat`` 并记一条 warning ——
生产链路（Phase 2 的下载路由）应当始终传 ``filename``。
"""

import base64
import json
import logging
import random
import re
import string
import time
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

logger = logging.getLogger("DouyinLowLatencyViewer.license.feijipan")

#: 与原工具一致的 API 根地址（默认值，生产路径使用）。
API_BASE = "https://api.feijipan.com/ws/"

#: 单次解析的总超时（秒）。原工具即 30 秒，保留。
REQUEST_TIMEOUT_SECONDS = 30

#: 允许的分享域名白名单。**不接受**任意 URL。
ALLOWED_SHARE_HOSTS = (
    "share.feijipan.com",
    "www.feijix.com",
    "www.feijipan.com",
)

#: 分享路径必须形如 /s/<id>。
_SHARE_PATH_RE = re.compile(r"^/s/[^/?]+$")

#: 与原工具一致：从链接里取分享密钥。
_SHARE_KEY_RE = re.compile(r"/s/([^/?]+)")

#: 成功结果里的 ``selection`` 取值：按 filename 精确命中。
SELECTION_EXACT = "filename_exact"
#: 成功结果里的 ``selection`` 取值：**兼容模式**（未指定 filename，取第一个文件）。
#: Phase 2 的 download 链路应当永远走精确匹配；出现这个值说明调用方没传 filename。
SELECTION_FIRST_FILE_COMPAT = "first_file_compat"


class FeijipanResolveError(Exception):
    """一次解析失败：输入非法 / 网络异常 / 上游返回异常 / AES 失败。

    ``code`` 沿用原工具的业务码（400/404/500），便于调用方映射 HTTP 状态。
    """

    def __init__(self, message: str, code: int = 502):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------
# AES + 设备标识（与原工具逐字一致，仅把静默回落改为明确失败）
# ----------------------------------------------------------------------
class AESUtils:
    """对应原项目中的 AESUtils。

    原项目逻辑：
      1. 用固定密文 CIPHER_AES2 和 CIPHER_AES_KEY 解出 CIPHER_AES0
      2. 用 CIPHER_AES0 做 AES/ECB/PKCS5Padding
      3. 输出 hex
    """

    CIPHER_AES2 = "YbQHZqK/PdQql2+7ATcPQHREAxt0Hn0Ob9v317QirZM="
    CIPHER_AES_KEY = "AES/ECB/PKCS5Padding"

    @classmethod
    def _generate_key(cls, key_string: str) -> bytes:
        # 完全保持原程序的规则：16 字节，不足右补 L，超长截断
        if len(key_string) > 16:
            key_string = key_string[:16]
        elif len(key_string) < 16:
            key_string = key_string.ljust(16, "L")
        return key_string.encode("utf-8")

    @classmethod
    def _get_cipher_aes0(cls) -> str:
        """解出内置密钥。

        原工具在异常时返回 ``"feijipan_default_key"``；服务端改为直接失败，
        避免用一个未经检验的密钥继续发请求。
        """
        try:
            key = cls._generate_key(cls.CIPHER_AES_KEY)
            cipher = AES.new(key, AES.MODE_ECB)
            encrypted_data = base64.b64decode(cls.CIPHER_AES2)
            decrypted = cipher.decrypt(encrypted_data)
            return unpad(decrypted, AES.block_size).decode("utf-8")
        except Exception as e:  # noqa: BLE001 - 一律转成明确的解析失败
            raise FeijipanResolveError(f"内置密钥解密失败: {e}", code=500) from e

    @classmethod
    def encrypt_to_hex(cls, plaintext: str) -> str:
        """AES/ECB/PKCS5Padding → hex。

        原工具在异常时回落到 ``md5(plaintext)``；服务端改为直接失败。
        """
        try:
            key = cls._generate_key(cls._get_cipher_aes0())
            cipher = AES.new(key, AES.MODE_ECB)
            encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
            return encrypted.hex()
        except FeijipanResolveError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FeijipanResolveError(f"AES 加密失败: {e}", code=500) from e


def generate_uuid(length: int = 21) -> str:
    """对应原程序 UUIDUtil.fj_uuid()。

    小飞机这里不是标准 UUID，而是 21 位随机字符串。
    """
    chars = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(chars) for _ in range(length))


def extract_share_key(url: str) -> Optional[str]:
    """从 ``https://www.feijix.com/s/xxxxxx`` 之类的链接中提取 xxxxxx。"""
    match = _SHARE_KEY_RE.search(url or "")
    return match.group(1) if match else None


def validate_share_url(share_url: str) -> str:
    """校验分享链接并返回 share_key；不合法即抛 :class:`FeijipanResolveError`。

    只接受白名单域名 + ``/s/<id>`` 路径，**拒绝任何其它 URL**。
    """
    if not isinstance(share_url, str) or not share_url.strip():
        raise FeijipanResolveError("share_url 不能为空", code=400)

    url = share_url.strip()
    parts = urlsplit(url)

    if parts.scheme != "https":
        raise FeijipanResolveError("仅接受 https 的分享链接", code=400)
    if parts.username or parts.password:
        raise FeijipanResolveError("分享链接不得包含认证信息", code=400)

    host = (parts.hostname or "").lower()
    if host not in ALLOWED_SHARE_HOSTS:
        raise FeijipanResolveError(
            "仅接受小飞机分享域名: " + ", ".join(ALLOWED_SHARE_HOSTS), code=400
        )
    if parts.port not in (None, 443):
        raise FeijipanResolveError("分享链接端口非法", code=400)
    if not _SHARE_PATH_RE.match(parts.path or ""):
        raise FeijipanResolveError(
            "分享链接必须形如 https://share.feijipan.com/s/<id>", code=400
        )

    share_key = extract_share_key(url)
    if not share_key:
        raise FeijipanResolveError("无法从链接中提取分享密钥", code=400)
    return share_key


# ----------------------------------------------------------------------
# 客户端（请求链与原工具一致）
# ----------------------------------------------------------------------
class FeijipanClient:
    """小飞机分享解析客户端。

    :param api_base: API 根地址。默认即原工具的 ``API_BASE``；仅测试会覆盖。
    :param timeout_seconds: 单次解析总超时（秒）。
    """

    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Length": "0",
        "DNT": "1",
        "Host": "api.feijipan.com",
        "Origin": "https://www.feijix.com",
        "Pragma": "no-cache",
        "Referer": "https://www.feijix.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": (
            '"Google Chrome";v="131", '
            '"Chromium";v="131", "Not_A Brand";v="24"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }

    def __init__(self, api_base: str = API_BASE, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS):
        self.api_base = api_base.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.vip_url = self.api_base + "buy/vip/list"
        self.recommend_url = self.api_base + "recommend/list"
        self.redirect_url = self.api_base + "file/redirect"

    def _headers(self) -> dict:
        """请求头。默认根地址下与原工具逐字一致。

        只有当根地址被换成别的 host（测试用本地服务端）时，才把 ``Host``
        改成那个 host —— 生产路径上这个分支不会被执行。
        """
        headers = dict(self.HEADERS)
        host = urlsplit(self.api_base).hostname
        if host and host != "api.feijipan.com":
            headers["Host"] = host
        return headers

    async def parse(self, share_url: str, share_password: str = "",
                    filename: str | None = None) -> dict:
        """解析分享链接。

        :param filename: 要解析的**精确文件名**。
            * 给定 → 在分享返回的全部文件里查找 ``fileName`` 完全相等的那个，
              找不到返回 ``code=404``（FILE_NOT_FOUND），**绝不回退到第一个文件**。
            * 未给定（``None``）→ **兼容模式**：沿用原工具行为，取第一个分享项
              的第一个文件；结果里的 ``selection`` 会标成
              ``first_file_compat`` 并记一条 warning。

        返回与原工具相同的结构，另加一个 ``selection`` 字段::

            {"code": 200, "url": "...", "file_name": "...", "file_size": 0,
             "message": "解析成功", "selection": "filename_exact"}

        失败时返回 ``code != 200`` 且 ``url is None`` 的字典（与原工具一致）。
        对外推荐使用 :func:`resolve`，它会把失败转成异常。
        """
        if filename is not None and not str(filename).strip():
            # 显式给了空文件名属于调用方错误：宁可报错，也不要静默退回兼容模式。
            raise FeijipanResolveError("filename 不能为空字符串", code=400)

        share_key = validate_share_url(share_url)

        # 第一步：生成设备信息
        now_ts = int(time.time() * 1000)
        timestamp = AESUtils.encrypt_to_hex(str(now_ts))
        uuid = generate_uuid()

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self._headers(),
            ) as session:

                # 第二步：访问 VIP 接口
                # 原程序虽然不读取响应，但保留这一步，避免改变原始请求流程。
                vip_params = {
                    "devType": "6",
                    "devModel": "Chrome",
                    "uuid": uuid,
                    "extra": "2",
                    "timestamp": timestamp,
                }

                async with session.post(
                    self.vip_url,
                    params=vip_params,
                ) as response:
                    await response.read()

                # 第三步：获取分享文件信息
                file_params = {
                    "devType": "6",
                    "devModel": "Chrome",
                    "uuid": uuid,
                    "extra": "2",
                    "timestamp": timestamp,
                    "shareId": share_key,
                    "type": "0",
                    "offset": "1",
                    "limit": "60",
                }

                if share_password:
                    file_params["code"] = share_password

                async with session.post(
                    self.recommend_url,
                    params=file_params,
                ) as response:
                    file_text = await response.text()

                try:
                    file_result = json.loads(file_text)
                except json.JSONDecodeError:
                    return {
                        "code": 500,
                        "url": None,
                        "message": "文件信息响应解析失败",
                    }

                if file_result.get("code") != 200:
                    return {
                        "code": 400,
                        "url": None,
                        "message": (
                            "获取文件信息失败: "
                            + str(file_result.get("msg", "未知错误"))
                        ),
                    }

                file_list = file_result.get("list", [])
                if not file_list:
                    return {
                        "code": 404,
                        "url": None,
                        "message": "文件列表为空，可能链接已失效",
                    }

                # 选择要解析的文件。
                # 层级是 list[分享项].fileList[文件]，且分享项可能不止一个，
                # 所以每一项的 fileList 都要找 —— 目标文件不一定在第一项里。
                if filename is not None:
                    matched = None
                    for item in file_list:
                        for detail in (item.get("fileList") or []):
                            if detail.get("fileName") == filename:
                                matched = (item, detail)
                                break
                        if matched is not None:
                            break

                    if matched is None:
                        # 精确匹配失败即失败：绝不改用第一个文件顶上。
                        logger.info("分享中未找到指定文件: %s", filename)
                        return {
                            "code": 404,
                            "url": None,
                            "message": f"分享中未找到文件: {filename}",
                        }

                    file_info, file_detail = matched
                    selection = SELECTION_EXACT
                else:
                    # 兼容模式（原工具行为）：第一个分享项的第一个文件。
                    logger.warning(
                        "resolve() 未指定 filename：按兼容模式取分享中的第一个文件"
                    )
                    file_info = file_list[0]
                    file_detail_list = file_info.get("fileList", [])

                    if not file_detail_list:
                        return {
                            "code": 404,
                            "url": None,
                            "message": "文件详情为空",
                        }

                    file_detail = file_detail_list[0]
                    selection = SELECTION_FIRST_FILE_COMPAT

                # 2 = 文件夹
                if file_detail.get("fileType") == 2:
                    return {
                        "code": 400,
                        "url": None,
                        "message": "检测到文件夹，当前版本不支持文件夹解析",
                    }

                file_id = file_info.get("fileIds")
                user_id = file_info.get("userId")
                file_name = file_detail.get("fileName", "未知文件")
                file_size = file_detail.get("fileSize", 0)

                # 与原项目完全一致的 userId 兜底逻辑
                if not user_id:
                    user_map = file_info.get("map", {})
                    if user_map and "userId" in user_map:
                        user_id = user_map.get("userId")

                    if not user_id:
                        share_id = file_info.get("shareId")
                        if share_id:
                            user_id = str(share_id)

                if not file_id:
                    return {
                        "code": 500,
                        "url": None,
                        "message": "无法获取文件ID",
                    }

                if not user_id:
                    return {
                        "code": 500,
                        "url": None,
                        "message": "无法获取用户ID，请检查分享链接是否有效",
                    }

                # 第四步：生成下载接口所需参数
                now_ts2 = int(time.time() * 1000)
                timestamp2 = AESUtils.encrypt_to_hex(str(now_ts2))

                # downloadId = AES(file_id|user_id)
                download_id = AESUtils.encrypt_to_hex(f"{file_id}|{user_id}")

                # auth = AES(file_id|当前毫秒时间戳)
                auth = AESUtils.encrypt_to_hex(f"{file_id}|{now_ts2}")

                download_params = {
                    "downloadId": download_id,
                    "enable": "1",
                    "devType": "6",
                    "uuid": uuid,
                    "timestamp": timestamp2,
                    "auth": auth,
                    "shareId": share_key,
                }

                # 第五步：请求 redirect，但禁止 aiohttp 自动跟随重定向。
                # 目标是拿到 HTTP 301/302/... 的 Location。
                async with session.get(
                    self.redirect_url,
                    params=download_params,
                    allow_redirects=False,
                ) as response:

                    if response.status in (301, 302, 303, 307, 308):
                        download_url = response.headers.get("Location")

                        if download_url:
                            # 注意：只记录文件名/大小，绝不记录 download_url。
                            logger.info(
                                "小飞机解析成功: file_name=%s file_size=%s",
                                file_name,
                                file_size,
                            )
                            return {
                                "code": 200,
                                "url": download_url,
                                "message": "解析成功",
                                "file_name": file_name,
                                "file_size": file_size,
                                "selection": selection,
                            }

                    # 把响应体读掉，便于连接复用
                    await response.read()

                    return {
                        "code": 500,
                        "url": None,
                        "message": (
                            "未找到下载链接，可能需要会员权限或链接已失效"
                        ),
                    }

        except aiohttp.ClientError as e:
            # 网络层异常：只记类型，不把可能带 URL 的原始消息写到日志里。
            logger.warning("小飞机解析网络异常: %s", type(e).__name__)
            return {
                "code": 500,
                "url": None,
                "message": f"访问小飞机接口失败: {type(e).__name__}",
            }
        except TimeoutError:
            logger.warning("小飞机解析超时")
            return {
                "code": 500,
                "url": None,
                "message": "访问小飞机接口超时",
            }


async def resolve(share_url: str, share_password: str = "",
                  filename: str | None = None) -> dict:
    """解析分享链接，返回指定文件的临时直链与元数据。

    :param share_url: 分享链接（必须命中白名单域名 + ``/s/<id>``）。
    :param share_password: 分享提取码，没有则留空。
    :param filename: 要解析的**精确文件名**（例如
        ``LiveLensDesktop-1.1.1-stable-full.nupkg``）。只有 ``fileName``
        完全相等的文件才会被解析；找不到即失败，**绝不回退到第一个文件**。
        传 ``None`` 时进入**兼容模式**（取第一个文件），返回值里的
        ``selection`` 会标成 ``first_file_compat``。

    :raises FeijipanResolveError: 输入非法 / 目标文件不存在 / 网络异常 /
        上游异常 / AES 失败。异常消息面向调用方，且**不包含**临时直链。

    成功返回值::

        {
            "code": 200,
            "url": "...",          # 临时直链，仅存在于本次返回值
            "file_name": "LiveLensDesktop-1.1.1-stable-full.nupkg",
            "file_size": 0,
            "message": "解析成功",
            "selection": "filename_exact"
        }
    """
    result = await FeijipanClient().parse(share_url, share_password, filename)

    if result.get("code") != 200:
        raise FeijipanResolveError(
            result.get("message", "解析失败"),
            code=result.get("code", 502),
        )

    return result
