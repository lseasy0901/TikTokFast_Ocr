# -*- coding: utf-8 -*-
"""
许可证服务器 HTTP 客户端（客户端） - Phase 7.2-6.5

只负责一件事：把「密钥 + 设备标识」发到许可证服务器，把服务器返回的
激活结果（含 SignedLicense）原样带回来。

明确不做：
    - 不做任何业务判断（是否已兑换、时长累积、功能合并，全部由服务器决定）
    - 不解析、不信任响应里的时长字段做本地计算
    - 不写本地存储（那是 utils.license_store 的职责）

安全：
    日志中不出现完整密钥、不出现完整请求/响应体、不出现 SignedLicense 内容。

端点（与 license_server/app/main.py 实际路由一致）：
    POST {base_url}/licenses/activate
    body: {"license_key": "...", "device_id": "..."}
    200:  {"id":.., "state":.., "features":[..], "signed_license":"..", ...}
    400:  {"detail": "invalid_key" | "already_redeemed" | "revoked"}
"""

import logging
import os
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger("DouyinLowLatencyViewer.license.client")

#: license_server/app/schemas.py 的 LicenseActivate 约束
_KEY_MIN_LENGTH = 16
_KEY_MAX_LENGTH = 64
_DEVICE_MAX_LENGTH = 64

#: 环境变量覆盖：让同一份构建用于不同部署而无需改代码。
#: 与验签公钥的 SUSI_PUBLIC_KEY 是同一个模式（utils/susi_verifier.py）。
#: 刻意不提供 GUI 输入框：地址属于部署配置，不是终端用户设置。
_ENV_SERVER_URL = "DLV_LICENSE_SERVER_URL"

#: 生产默认值：随包分发的客户端直接指向生产许可证服务器，客户机无需任何配置。
#: 上面的环境变量仍可覆盖，用于把同一份构建指向其他部署（例如测试环境）。
DEFAULT_SERVER_URL = "https://livelen.icu/api/v1"


def resolve_server_url(override: Optional[str] = None) -> str:
    """解析激活端点的 base_url。

    顺序：
        1. 显式传入的 override（``LicenseServerClient`` 构造函数参数）
        2. 环境变量 ``DLV_LICENSE_SERVER_URL``
        3. :data:`DEFAULT_SERVER_URL`（生产默认）

    空值或纯空白按「未设置」处理，避免一个空的环境变量把地址变成空串
    而让激活静默失败。返回值不带尾随斜杠。
    """
    if override and override.strip():
        return override.strip().rstrip("/")
    env_value = os.environ.get(_ENV_SERVER_URL)
    if env_value and env_value.strip():
        return env_value.strip().rstrip("/")
    return DEFAULT_SERVER_URL

#: 可预期的失败原因（其余一律归类为 server_error）
REASON_INVALID_KEY = "invalid_key"
REASON_ALREADY_REDEEMED = "already_redeemed"
REASON_REVOKED = "revoked"
REASON_UNREACHABLE = "unreachable"
REASON_SERVER_ERROR = "server_error"


class LicenseServerError(Exception):
    """激活请求失败。reason 为上面的 REASON_* 之一。"""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


class LicenseServerClient:
    """许可证服务器激活端点客户端。"""

    def __init__(self, base_url: Optional[str] = None, timeout: int = 15):
        self.base_url = resolve_server_url(base_url)
        self.timeout = timeout

    def activate(self, license_key: str, device_id: str) -> dict:
        """向服务器提交一次兑换请求。

        Returns:
            服务器返回的激活响应字典（含 signed_license）。

        Raises:
            LicenseServerError: 网络不可达、服务器拒绝或响应非法。
                注意「服务器不可达」不会留下任何本地副作用 ——
                调用方在此之前不会写本地存储。
        """
        key = (license_key or "").strip()
        device = (device_id or "").strip()

        if not _KEY_MIN_LENGTH <= len(key) <= _KEY_MAX_LENGTH:
            raise LicenseServerError(
                REASON_INVALID_KEY, f"许可证密钥长度不合法（应为 {_KEY_MIN_LENGTH}-{_KEY_MAX_LENGTH} 位）"
            )
        if not device or len(device) > _DEVICE_MAX_LENGTH:
            raise LicenseServerError(REASON_SERVER_ERROR, "设备标识不合法")

        url = f"{self.base_url}/licenses/activate"
        logger.info("正在向许可证服务器提交激活请求: %s", url)

        try:
            response = requests.post(
                url,
                json={"license_key": key, "device_id": device},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise LicenseServerError(REASON_UNREACHABLE, "连接许可证服务器超时")
        except requests.exceptions.RequestException as e:
            # 只记录异常类型，不记录请求体
            logger.warning("许可证服务器不可达: %s", type(e).__name__)
            raise LicenseServerError(REASON_UNREACHABLE, "无法连接许可证服务器")

        if response.status_code == 200:
            return self._parse_success(response)

        if response.status_code == 400:
            reason = self._parse_detail(response)
            logger.info("服务器拒绝激活，原因=%s", reason)
            raise LicenseServerError(reason, REASON_TEXT.get(reason, "激活被服务器拒绝"))

        logger.warning("许可证服务器返回状态码 %s", response.status_code)
        raise LicenseServerError(REASON_SERVER_ERROR, f"许可证服务器错误（HTTP {response.status_code}）")

    def heartbeat(self, device_id: str, timeout: int = 5) -> Optional[date]:
        """上报一次启动心跳（Phase 7.4，用于 DAU 统计）。

        与 :meth:`activate` 的关键区别：**本方法永不抛异常**。

        心跳是纯遥测。网络不可达、超时、服务器 4xx/5xx 都必须被静默吞掉，
        否则会把遥测的失败混进调用方的错误处理，甚至影响授权流程。
        调用方因此不需要（也不应该）为它写 try —— 结果通过返回值表达。

        Args:
            device_id: 本机设备标识（激活时使用的同一个机器码）。
            timeout: 独立于 activate 的短超时。心跳不该长期占用后台线程，
                因此默认 5s，而不是 activate 的 15s。

        Returns:
            服务器**本次实际记账的业务日**（响应里的 ``active_date``）；
            None 表示没有可上报的标识、上报失败，或响应无法解析。

        为什么返回服务端的日期而不是 bool
        ---------------------------------
        DAU 的记账权威在服务端 —— ``active_date`` 由服务端用自己的时钟经
        ``bucket_date()`` 算出。客户端**不得**用"收到响应时的本地日期"推断
        业务日：请求贴着业务日边界发出、响应跨过边界时两者会差一天，客户端
        就会把**下一天**误记为"已上报"，那一天永远不会被上报。

        因此成功时一律以服务端返回的 ``active_date`` 为准，客户端只负责原样
        把它交给调度器。

        返回值只用于让调度器判断"哪一天已经记过账"，
        绝不能被当作授权判定使用。
        """
        device = (device_id or "").strip()
        if not device or len(device) > _DEVICE_MAX_LENGTH:
            logger.debug("跳过心跳：设备标识不可用")
            return None

        url = f"{self.base_url}/licenses/heartbeat"
        try:
            response = requests.post(
                url,
                json={"device_id": device},
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            # 只记异常类型，不记录请求体
            logger.debug("心跳上报失败（已忽略）: %s", type(e).__name__)
            return None

        if response.status_code != 200:
            logger.debug("心跳上报被拒绝（已忽略）: HTTP %s", response.status_code)
            return None

        return self._parse_heartbeat_day(response)

    @staticmethod
    def _parse_heartbeat_day(response) -> Optional[date]:
        """从 200 响应里取出服务端记账的业务日；解析不出来返回 None。

        返回 None 会让调度器保持"当天尚未上报"，从而按 watchdog 周期重试。
        这个方向是安全的：重复上报会被服务端的 ``UNIQUE(device_id,
        active_date)`` 去重，代价只是多一次请求；反过来凭空相信一个客户端
        自己推断的日期，则会让某一天永远丢失。
        """
        try:
            payload = response.json()
        except ValueError:
            logger.debug("心跳响应不是合法 JSON（已忽略）")
            return None

        if not isinstance(payload, dict):
            logger.debug("心跳响应结构非法（已忽略）")
            return None

        raw = payload.get("active_date")
        if not isinstance(raw, str) or not raw.strip():
            logger.debug("心跳响应缺少 active_date（已忽略）")
            return None

        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            logger.debug("心跳响应的 active_date 无法解析（已忽略）")
            return None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_detail(response) -> str:
        """把 400 响应的 detail 映射为已知原因。"""
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None

        if detail in (REASON_INVALID_KEY, REASON_ALREADY_REDEEMED, REASON_REVOKED):
            return detail
        return REASON_SERVER_ERROR

    @staticmethod
    def _parse_success(response) -> dict:
        """校验 200 响应结构，尤其是 signed_license 必须存在。"""
        try:
            payload = response.json()
        except ValueError:
            raise LicenseServerError(REASON_SERVER_ERROR, "许可证服务器返回了非法 JSON")

        if not isinstance(payload, dict):
            raise LicenseServerError(REASON_SERVER_ERROR, "许可证服务器返回结构非法")

        signed_license = payload.get("signed_license")
        if not signed_license or not isinstance(signed_license, str):
            # 服务器虽然兑换成功，但没有签发凭据 —— 客户端无法建立可信状态，
            # 必须按失败处理，避免出现「已扣密钥但无凭据」。
            raise LicenseServerError(
                REASON_SERVER_ERROR, "许可证服务器未返回签名凭据"
            )

        return payload


#: reason -> 对用户展示的文案
REASON_TEXT = {
    REASON_INVALID_KEY: "许可证密钥无效",
    REASON_ALREADY_REDEEMED: "许可证密钥已被使用过",
    REASON_REVOKED: "许可证已被吊销",
    REASON_UNREACHABLE: "无法连接许可证服务器",
    REASON_SERVER_ERROR: "许可证服务器错误",
}
