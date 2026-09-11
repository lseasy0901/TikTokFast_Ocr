# -*- coding: utf-8 -*-
"""
许可证管理器（客户端） - Phase 7.2-6.5

这是客户端激活流程的唯一编排点。UI 只跟它打交道。

架构分工（不可颠倒）：
    业务层（许可证服务器）权威负责：一次性密钥兑换、Authorization、
        有效期累积、max_hosts、密钥状态、授权状态。
    Susi 负责：签名凭据、密码学验签、机器绑定、到期校验、本地安全校验。
    本模块负责：把这两者串起来，并在本地复核凭据。

客户端明确不做：
    - 不实现自己的许可证时长计算（有效期只读服务器签名的 expires 字段）
    - 不实现自己的密钥消费逻辑（是否已兑换完全由服务器裁决）
    - 不把 Susi 原生 activate() 当作业务兑换

激活流程：
    key + 本机机器码 → 服务器兑换 → 服务器签发 SignedLicense
    → 本地验签 → 本地机器绑定校验 → 本地到期校验 → 落盘 → 激活成功

关键顺序保证：先本地验证通过，再写本地存储。
服务器不可达、凭据无效、机器不匹配等失败路径都不会改动本地许可证状态。
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import FrozenSet, Optional

from utils.license_client import (
    DEFAULT_SERVER_URL,
    REASON_ALREADY_REDEEMED,
    REASON_INVALID_KEY,
    REASON_REVOKED,
    REASON_SERVER_ERROR,
    REASON_TEXT,
    REASON_UNREACHABLE,
    LicenseServerClient,
    LicenseServerError,
)
from utils.license_store import LicenseStore
from utils.susi_verifier import (
    SusiVerifier,
    SusiVerifierError,
    resolve_public_key,
)

logger = logging.getLogger("DouyinLowLatencyViewer.license.manager")


class LicenseStatus(Enum):
    """客户端许可证状态。"""

    NOT_ACTIVATED = "not_activated"        # 未激活
    ACTIVATED = "activated"                # 已激活
    ACTIVATION_FAILED = "activation_failed"  # 激活失败（可重试：网络、服务器错误）
    EXPIRED = "expired"                    # 许可证已过期
    INVALID = "invalid"                    # 许可证无效（密钥无效/已吊销/凭据被篡改）
    WRONG_MACHINE = "wrong_machine"        # 许可证不属于当前设备


#: 对用户展示的状态文案
STATUS_TEXT = {
    LicenseStatus.NOT_ACTIVATED: "未激活",
    LicenseStatus.ACTIVATED: "已激活",
    LicenseStatus.ACTIVATION_FAILED: "激活失败",
    LicenseStatus.EXPIRED: "许可证已过期",
    LicenseStatus.INVALID: "许可证无效",
    LicenseStatus.WRONG_MACHINE: "许可证不属于当前设备",
}


@dataclass
class LicenseResult:
    """一次激活/校验的结果。"""

    status: LicenseStatus
    message: str = ""
    expires_at: Optional[datetime] = None
    features: FrozenSet[str] = field(default_factory=frozenset)
    signed_license: Optional[str] = None

    @property
    def is_activated(self) -> bool:
        return self.status is LicenseStatus.ACTIVATED

    @property
    def display_text(self) -> str:
        return STATUS_TEXT.get(self.status, "未知状态")


def _parse_rfc3339(value) -> Optional[datetime]:
    """解析 Susi 凭据里的 RFC3339 时间。

    chrono 序列化 DateTime<Utc> 时使用 'Z' 后缀；Python 3.11+ 的
    fromisoformat 能直接解析。这里额外兼容 '+00:00' 与非法值。
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("凭据内的时间字段无法解析")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LicenseManager:
    """客户端许可证编排：激活、本地验证、启动恢复。"""

    def __init__(
        self,
        server_url: Optional[str] = None,
        store: Optional[LicenseStore] = None,
        verifier: Optional[SusiVerifier] = None,
        client: Optional[LicenseServerClient] = None,
    ):
        self.server_url = server_url or DEFAULT_SERVER_URL
        self.store = store or LicenseStore()

        if verifier is None:
            public_key = resolve_public_key(extra_dirs=[self.store.data_dir])
            verifier = SusiVerifier(public_key_pem=public_key)
            if public_key is None:
                logger.warning("未配置验签公钥，激活后无法在本地验证凭据")
        self.verifier = verifier

        self.client = client or LicenseServerClient(self.server_url)

        # 并发保护：同一时刻只允许一个激活流程
        self._activation_lock = threading.Lock()

        self._machine_code: Optional[str] = None
        self.last_result: Optional[LicenseResult] = None

    # ------------------------------------------------------------------
    # 设备标识
    # ------------------------------------------------------------------
    def get_machine_code(self, refresh: bool = False) -> str:
        """获取本机机器码（结果缓存，机器码在一次进程生命周期内不变）。"""
        if self._machine_code and not refresh:
            return self._machine_code
        code = self.verifier.get_machine_code()
        self._machine_code = code
        return code

    # ------------------------------------------------------------------
    # 激活
    # ------------------------------------------------------------------
    def activate(self, license_key: str) -> LicenseResult:
        """执行一次完整激活：兑换 → 签发 → 本地验证 → 落盘。

        Raises:
            无。所有失败都以 LicenseResult 返回，便于 UI 统一展示。
        """
        # 并发拦截：第二次调用立即返回，不排队、不重复提交
        if not self._activation_lock.acquire(blocking=False):
            return LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, "正在激活中，请稍候"
            )

        try:
            result = self._activate_locked(license_key)
            self.last_result = result
            return result
        finally:
            self._activation_lock.release()

    def _activate_locked(self, license_key: str) -> LicenseResult:
        # 1) 取得本机机器码 —— 同时作为服务器侧的 device_id
        try:
            machine_code = self.get_machine_code()
        except SusiVerifierError as e:
            logger.error("获取机器码失败: %s", e)
            return LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, f"无法获取设备标识: {e}"
            )

        # 2) 交服务器裁决（是否已兑换 / 是否有效，全部由服务器决定）
        try:
            response = self.client.activate(license_key, machine_code)
        except LicenseServerError as e:
            # 失败路径：本地存储完全未被触碰
            return self._result_from_server_error(e)

        signed_license = response["signed_license"]

        # 3) 本地验证凭据真伪、机器绑定与到期时间
        result = self._verify_artifact(signed_license, machine_code=machine_code)
        if not result.is_activated:
            # 服务器说成功但本地验不通过 —— 不落盘，保持原有本地状态
            logger.error("服务器已签发凭据，但本地验证未通过: %s", result.status.value)
            return result

        # 4) 只有验证通过才落盘
        try:
            self.store.save(signed_license, machine_code, self.server_url)
        except OSError as e:
            logger.error("保存许可证失败: %s", e)
            return LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, f"无法保存许可证: {e}"
            )

        return result

    @staticmethod
    def _result_from_server_error(error: LicenseServerError) -> LicenseResult:
        """服务器错误 → 客户端状态。"""
        mapping = {
            REASON_INVALID_KEY: LicenseStatus.INVALID,
            REASON_ALREADY_REDEEMED: LicenseStatus.INVALID,
            REASON_REVOKED: LicenseStatus.INVALID,
            REASON_UNREACHABLE: LicenseStatus.ACTIVATION_FAILED,
            REASON_SERVER_ERROR: LicenseStatus.ACTIVATION_FAILED,
        }
        status = mapping.get(error.reason, LicenseStatus.ACTIVATION_FAILED)
        return LicenseResult(status, REASON_TEXT.get(error.reason, error.message))

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    def load_stored(self) -> LicenseResult:
        """启动时加载并本地验证已保存的许可证。"""
        record = self.store.load()
        if not record:
            result = LicenseResult(LicenseStatus.NOT_ACTIVATED, "尚未激活许可证")
            self.last_result = result
            return result

        result = self._verify_artifact(record["signed_license"])
        self.last_result = result
        return result

    # ------------------------------------------------------------------
    # 本地验证（不联网）
    # ------------------------------------------------------------------
    def _verify_artifact(
        self, signed_license_json: str, machine_code: Optional[str] = None
    ) -> LicenseResult:
        """在本地验证一份 SignedLicense。

        步骤严格按此顺序，任何一步失败都立即返回：
            1. 结构可解析
            2. 签名有效（用公钥，证明凭据由服务器签发且未被篡改）
            3. 机器绑定匹配本机
            4. 未过期

        只有第 2 步通过后，payload 里的字段才是可信的。
        """
        # 1) 结构
        try:
            outer = json.loads(signed_license_json)
            if not isinstance(outer, dict):
                raise ValueError("外层不是对象")
            license_data = outer["license_data"]
            if not isinstance(license_data, str):
                raise ValueError("license_data 不是字符串")
            if not outer.get("signature"):
                raise ValueError("缺少 signature")
            payload = json.loads(license_data)
            if not isinstance(payload, dict):
                raise ValueError("license_data 不是对象")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("许可证凭据结构非法: %s", e)
            return LicenseResult(LicenseStatus.INVALID, "许可证凭据格式非法")

        # 2) 签名（在信任任何字段之前）
        try:
            signature_ok = self.verifier.verify_signature(signed_license_json)
        except SusiVerifierError as e:
            logger.error("本地验签无法完成: %s", e)
            return LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, f"本地验证失败: {e}"
            )

        if not signature_ok:
            return LicenseResult(
                LicenseStatus.INVALID, "许可证签名无效（凭据可能已被篡改）"
            )

        # 3) 机器绑定（签名已通过，以下字段可信）
        try:
            own_code = machine_code or self.get_machine_code()
        except SusiVerifierError as e:
            logger.error("获取机器码失败: %s", e)
            return LicenseResult(
                LicenseStatus.ACTIVATION_FAILED, f"无法获取设备标识: {e}"
            )

        bound_codes = payload.get("machine_codes") or []
        if not isinstance(bound_codes, list) or own_code not in bound_codes:
            return LicenseResult(
                LicenseStatus.WRONG_MACHINE, "该许可证未绑定到当前设备"
            )

        # 4) 到期（有效期只认服务器签名的 expires，客户端不做时长计算）
        expires_at = _parse_rfc3339(payload.get("expires"))
        if payload.get("expires") and expires_at is None:
            return LicenseResult(LicenseStatus.INVALID, "许可证到期时间非法")
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            return LicenseResult(
                LicenseStatus.EXPIRED,
                "许可证已过期",
                expires_at=expires_at,
                signed_license=signed_license_json,
            )

        features = payload.get("features") or []
        return LicenseResult(
            LicenseStatus.ACTIVATED,
            "已激活",
            expires_at=expires_at,
            features=frozenset(f for f in features if isinstance(f, str)),
            signed_license=signed_license_json,
        )
