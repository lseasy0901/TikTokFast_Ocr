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
from typing import Optional

import requests

logger = logging.getLogger("DouyinLowLatencyViewer.license.client")

#: license_server/app/schemas.py 的 LicenseActivate 约束
_KEY_MIN_LENGTH = 16
_KEY_MAX_LENGTH = 64
_DEVICE_MAX_LENGTH = 64

DEFAULT_SERVER_URL = "http://127.0.0.1:8000/api/v1"

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
        self.base_url = (base_url or DEFAULT_SERVER_URL).rstrip("/")
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
