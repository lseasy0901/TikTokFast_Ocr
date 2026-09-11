# -*- coding: utf-8 -*-
"""
Susi 本地验证器（客户端） - Phase 7.2-6.5

职责（仅此两项）：
    1. 通过 susi_helper.exe 获取本机机器码
    2. 通过 susi_helper.exe 用【公钥】验证 SignedLicense 的签名

明确不做：
    - 不持有私钥（私钥只存在于许可证服务器）
    - 不做任何授权 / 时长 / 密钥消费决策
    - 不联系许可证服务器（那是 utils.license_client 的职责）

susi_helper 协议：单条 JSON 命令经 stdin 写入，单条 JSON 响应从 stdout 读出。
    {"command": "GetMachineCode"}
    {"command": "Verify", "signed_license": "...", "public_key_pem": "..."}
响应形如 {"Success": {"data": ...}} 或 {"Error": {"message": "..."}}。
"""

import json
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger("DouyinLowLatencyViewer.license.susi")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HELPER_BASENAME = "susi_helper.exe" if os.name == "nt" else "susi_helper"

#: 客户端只允许使用公钥；私钥绝不随客户端分发。
_PUBLIC_KEY_ENV_VARS = ("SUSI_PUBLIC_KEY", "SUSI_DEVELOPMENT_PUBLIC_KEY")
_PUBLIC_KEY_FILENAME = "license_public_key.pem"


class SusiVerifierError(RuntimeError):
    """susi_helper 不可用或返回错误。"""


def default_helper_path() -> str:
    """susi_helper.exe 的默认位置，可用 SUSI_HELPER_PATH 覆盖。"""
    override = os.environ.get("SUSI_HELPER_PATH")
    if override:
        return override
    return os.path.join(
        _PROJECT_ROOT, "susi_helper", "target", "release", _HELPER_BASENAME
    )


def resolve_public_key(extra_dirs: Optional[list] = None) -> Optional[str]:
    """解析验签公钥。

    查找顺序：
        1. 环境变量 SUSI_PUBLIC_KEY / SUSI_DEVELOPMENT_PUBLIC_KEY
        2. <项目根>/license_public_key.pem
        3. extra_dirs 下的 license_public_key.pem（通常是应用数据目录）

    公钥不是机密，可以随客户端分发；但从环境变量或独立文件读取，
    可以让同一份构建用于不同部署而无需改代码。

    Returns:
        公钥 PEM 字符串；未配置时返回 None。
    """
    for var in _PUBLIC_KEY_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            # 环境变量常把换行写成字面量 \n，这里还原
            return value.strip().replace("\\n", "\n")

    search_dirs = [_PROJECT_ROOT] + list(extra_dirs or [])
    for directory in search_dirs:
        candidate = os.path.join(directory, _PUBLIC_KEY_FILENAME)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    pem = f.read().strip()
                if pem:
                    return pem
            except OSError as e:
                logger.warning("读取公钥文件失败 %s: %s", candidate, e)

    return None


class SusiVerifier:
    """susi_helper.exe 的薄封装：机器码 + 签名验证。"""

    def __init__(
        self,
        public_key_pem: Optional[str],
        helper_path: Optional[str] = None,
        timeout: int = 10,
    ):
        self.helper_path = helper_path or default_helper_path()
        self.public_key_pem = public_key_pem
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 内部：调用 susi_helper
    # ------------------------------------------------------------------
    def _call(self, command: dict):
        """执行一次 susi_helper 调用，返回 Success.data；失败抛 SusiVerifierError。"""
        if not os.path.isfile(self.helper_path):
            raise SusiVerifierError(f"susi_helper 不存在: {self.helper_path}")

        try:
            result = subprocess.run(
                [self.helper_path],
                input=json.dumps(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise SusiVerifierError(f"susi_helper 超时（{self.timeout}s）")
        except subprocess.CalledProcessError as e:
            raise SusiVerifierError(
                f"susi_helper 执行失败（返回码 {e.returncode}）"
            )
        except OSError as e:
            raise SusiVerifierError(f"无法启动 susi_helper: {e}")

        try:
            response = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise SusiVerifierError("susi_helper 返回了非法 JSON")

        if not isinstance(response, dict):
            raise SusiVerifierError("susi_helper 返回格式未知")

        if "Success" in response:
            payload = response["Success"]
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            raise SusiVerifierError("susi_helper Success 响应格式非法")

        if "Error" in response:
            payload = response["Error"]
            message = payload.get("message") if isinstance(payload, dict) else None
            raise SusiVerifierError(message or "susi_helper 返回未知错误")

        raise SusiVerifierError("susi_helper 返回格式未知")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def get_machine_code(self) -> str:
        """返回本机机器码（64 位十六进制）。"""
        data = self._call({"command": "GetMachineCode"})
        if not isinstance(data, str) or not data.strip():
            raise SusiVerifierError("susi_helper 未返回有效机器码")
        return data.strip()

    def verify_signature(self, signed_license_json: str) -> bool:
        """用公钥验证 SignedLicense 的签名。

        Returns:
            True  签名有效（且因此在密码学上证明该凭据由服务器签发、未被篡改）
            False 签名无效（凭据被篡改，或不是本服务器签发）

        Raises:
            SusiVerifierError 公钥缺失、helper 不可用等基础设施错误
        """
        if not self.public_key_pem:
            raise SusiVerifierError("未配置验签公钥")

        try:
            self._call(
                {
                    "command": "Verify",
                    "signed_license": signed_license_json,
                    "public_key_pem": self.public_key_pem,
                }
            )
        except SusiVerifierError as e:
            # 区分三种情况，避免把配置错误误报成「许可证无效」：
            #   1. 签名不通过（篡改）      -> False，这是正常的安全结论
            #   2. 公钥本身无法解析        -> 基础设施错误，向上抛
            #   3. helper 不可用/超时/坏 JSON -> 向上抛
            # helper 的包装格式为 "Verification failed: <内层原因>"，
            # 内层原因见 susi_helper/src/main.rs 的 verify_license_from_pem()。
            message = str(e)
            if "Invalid public key PEM" in message:
                raise SusiVerifierError("验签公钥无法解析") from e
            if message.startswith("Verification failed") or message.startswith(
                "License verification error"
            ):
                logger.warning("SignedLicense 签名验证未通过")
                return False
            raise

        return True
