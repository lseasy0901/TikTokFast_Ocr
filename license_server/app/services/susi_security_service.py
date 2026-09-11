# -*- coding: utf-8 -*-
"""
Susi Security Service for Phase 7.2-6
Handles Susi security operations using the existing susi_helper subprocess interface
"""

import subprocess
import json
import base64
import logging
from typing import Tuple, Dict, Optional
from datetime import datetime, timezone
import tempfile
import os

logger = logging.getLogger(__name__)

#: <repo>/license_server -- used to locate the developer default signing key
#: without depending on the process working directory.
_LICENSE_SERVER_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
#: Developer default, gitignored, never shipped. Used only when neither
#: SUSI_DEVELOPMENT_PRIVATE_KEY_FILE nor SUSI_DEVELOPMENT_PRIVATE_KEY is set.
_DEV_SIGNING_KEY_PATH = os.path.join(_LICENSE_SERVER_DIR, "test_rsa_key.pem")


def _normalize_pem_text(value: Optional[str]) -> str:
    """Normalize a PEM carried through .env / an environment variable.

    ``python-dotenv`` only preserves a multi-line value when it is quoted, so the
    common .env representation escapes the line breaks as literal ``\\n``. Windows
    values may instead carry real CRLF. Handle both, then trim.

    Never logs or returns anything derived from the key beyond the normalized PEM.
    """
    if not value:
        return ""
    text = (
        value.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    return text.strip()


def _read_pem_file(path: str) -> str:
    """Read a PEM file as text, independent of the platform's locale encoding.

    Reads bytes and decodes explicitly as UTF-8. A UTF-16 file (the usual result of
    writing a key from PowerShell redirection) would otherwise yield interleaved NUL
    bytes, which the Rust PEM parser reports as
    "PEM preamble contains invalid data (NUL byte)" -- a message that points at the
    key material instead of at the file encoding. Reject that case explicitly.
    """
    with open(path, "rb") as f:
        raw = f.read()

    if b"\x00" in raw:
        raise RuntimeError(
            f"signing key file contains NUL bytes (is it UTF-16 encoded? "
            f"re-save it as UTF-8): {path}"
        )

    return _normalize_pem_text(raw.decode("utf-8-sig"))


def _looks_like_private_key_pem(value: Optional[str]) -> bool:
    """Cheap structural check: does this look like a private-key PEM at all?

    Deliberately not a cryptographic validation -- susi_helper remains the sole
    authority on whether the key is well-formed and usable. This only catches the
    "nothing was configured / the value was truncated" case, which the helper
    otherwise reports with a misleading encoding error.
    """
    if not value or "-----BEGIN" not in value or "-----END" not in value:
        return False
    label = value.split("-----BEGIN", 1)[1].split("-----", 1)[0]
    return "PRIVATE KEY" in label


def _helper_basename(platform_name: Optional[str] = None) -> str:
    """susi_helper 的文件名按平台约定。

    Windows 上是 ``susi_helper.exe``；其余平台没有扩展名。与客户端
    ``utils/susi_verifier.default_helper_path()`` 使用同一约定。

    ``platform_name`` 仅用于测试：默认取 ``os.name``。
    """
    name = os.name if platform_name is None else platform_name
    return "susi_helper.exe" if name == "nt" else "susi_helper"


def _default_helper_path() -> str:
    """项目根下的 susi_helper 默认位置（不含任何配置覆盖）。"""
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(
        project_root, "susi_helper", "target", "release", _helper_basename()
    )


def _resolve_helper_path(configured: Optional[str] = None) -> str:
    """解析 susi_helper 可执行文件路径。

    顺序：
        1. ``SUSI_HELPER_PATH``（环境变量或 .env）
        2. 项目根下的默认位置，文件名按 :func:`_helper_basename` 的 OS 约定

    空值或纯空白按「未设置」处理。刻意不做「配置了但文件不存在就回退」——
    那会把拼写错误伪装成默认行为。文件是否存在由调用方检查并报错。
    """
    if configured and str(configured).strip():
        return str(configured).strip()
    return _default_helper_path()


class SusiSecurityService:
    """Service for Susi security operations using susi_helper subprocess"""

    def __init__(self, config):
        self.config = config
        self.susi_helper_path = _resolve_helper_path(
            self._get_setting('SUSI_HELPER_PATH')
        )

        # Check if helper exists
        if not os.path.exists(self.susi_helper_path):
            raise RuntimeError(f"susi_helper not found at: {self.susi_helper_path}")
        self.development_private_key, self._private_key_source = (
            self._resolve_private_key()
        )
        self.development_public_key = self._get_setting('SUSI_DEVELOPMENT_PUBLIC_KEY')

        # Surface a misconfiguration at startup. The signing key is not needed to
        # boot (the server also serves validation/admin routes), so this warns
        # rather than raises; create_signed_license() is where it becomes fatal.
        # Only the *source* is ever logged -- never key material.
        if not _looks_like_private_key_pem(self.development_private_key):
            logger.warning(
                "No usable Susi signing key configured (source: %s). "
                "License activation will fail until SUSI_DEVELOPMENT_PRIVATE_KEY_FILE "
                "or SUSI_DEVELOPMENT_PRIVATE_KEY is set.",
                self._private_key_source,
            )

    def _resolve_private_key(self) -> Tuple[str, str]:
        """Resolve the development signing key PEM.

        Returns ``(pem, source)`` where ``source`` describes *where* the value came
        from (a path or a variable name) so failures can be diagnosed without ever
        logging key material. ``pem`` is "" when nothing usable was configured.

        Precedence:
            1. SUSI_DEVELOPMENT_PRIVATE_KEY_FILE -- an explicit file path
            2. SUSI_DEVELOPMENT_PRIVATE_KEY      -- an explicit inline PEM
            3. <license_server>/test_rsa_key.pem -- developer default, when present

        An explicitly configured source is never silently skipped: if it is set but
        unusable, that is reported rather than falling through to the next option,
        so a typo cannot be masked by a leftover default key.
        """
        file_setting = self._get_setting('SUSI_DEVELOPMENT_PRIVATE_KEY_FILE')
        if file_setting and str(file_setting).strip():
            # Relative paths resolve against <repo>/license_server rather than the
            # process working directory, which is not fixed (uvicorn vs run_server.py).
            path = str(file_setting).strip()
            if not os.path.isabs(path):
                path = os.path.join(_LICENSE_SERVER_DIR, path)
            if not os.path.isfile(path):
                return "", f"SUSI_DEVELOPMENT_PRIVATE_KEY_FILE={path} (file not found)"
            try:
                return _read_pem_file(path), f"file {path}"
            except (OSError, UnicodeDecodeError, RuntimeError) as e:
                return "", f"file {path} ({e})"

        inline = self._get_setting('SUSI_DEVELOPMENT_PRIVATE_KEY')
        if inline and str(inline).strip():
            pem = _normalize_pem_text(str(inline))
            if _looks_like_private_key_pem(pem):
                return pem, "SUSI_DEVELOPMENT_PRIVATE_KEY"
            # Reports the shape of the failure, never the value.
            return "", (
                "SUSI_DEVELOPMENT_PRIVATE_KEY (set, but not a private-key PEM -- "
                "an unquoted multi-line value in .env is truncated; "
                "use SUSI_DEVELOPMENT_PRIVATE_KEY_FILE instead)"
            )

        if os.path.isfile(_DEV_SIGNING_KEY_PATH):
            try:
                return _read_pem_file(_DEV_SIGNING_KEY_PATH), f"file {_DEV_SIGNING_KEY_PATH}"
            except (OSError, UnicodeDecodeError, RuntimeError) as e:
                return "", f"file {_DEV_SIGNING_KEY_PATH} ({e})"

        return "", "not configured"

    def _require_private_key(self) -> str:
        """Return the signing key PEM or raise a clear, key-free error.

        Without this the empty string reaches susi_helper, which reports
        "PEM preamble contains invalid data (NUL byte)" -- an encoding-shaped message
        for what is really an unconfigured key.
        """
        if not _looks_like_private_key_pem(self.development_private_key):
            raise RuntimeError(
                "Susi signing key is not configured: no usable private key PEM. "
                f"Source: {self._private_key_source}. Set "
                "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE to a PEM file path (preferred), "
                "or SUSI_DEVELOPMENT_PRIVATE_KEY to a private-key PEM."
            )
        return self.development_private_key

    def _get_setting(self, key: str):
        """Read a setting from either a plain dict (tests) or Settings (the app).

        app/main.py passes config.settings (a pydantic Settings object), which
        has no .get(); tests pass a plain dict. Support both.
        """
        if isinstance(self.config, dict):
            return self.config.get(key)
        return getattr(self.config, key, None)

    @staticmethod
    def _to_rfc3339(value: Optional[datetime]) -> Optional[str]:
        """Serialize a datetime as RFC3339 with an explicit UTC offset.

        SQLAlchemy ``DateTime`` columns (declared without ``timezone=True``)
        return naive datetimes. Susi's ``LicensePayload.created``/``expires``
        are ``chrono::DateTime<Utc>``, whose deserializer requires an RFC3339
        offset -- a naive ``isoformat()`` is rejected by susi_helper.
        Naive values are assumed to already be UTC.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(microsecond=0).isoformat()

    def get_machine_code(self) -> str:
        """Get machine code using susi_helper GetMachineCode command"""
        try:
            # Prepare JSON command
            command = {
                "command": "GetMachineCode"
            }
            command_json = json.dumps(command)

            # Execute susi_helper directly (avoid shell=True on Windows) with timeout
            # When text=True, input must be str, not bytes
            result = subprocess.run(
                [self.susi_helper_path],
                input=command_json,
                capture_output=True,
                text=True,
                check=True,
                timeout=10  # 10 second timeout
            )

            logger.debug("susi_helper GetMachineCode returned %s", result.returncode)

            # Parse JSON response
            response = json.loads(result.stdout)

            # Handle susi_helper custom response format: {"Success": {"data": "value"}} or {"Error": {"message": "error"}}
            if "Success" in response:
                success_data = response["Success"]
                if isinstance(success_data, dict) and "data" in success_data:
                    machine_code = success_data["data"]
                    logger.debug("Obtained machine code from susi_helper")
                    return machine_code
                else:
                    raise RuntimeError(f"Invalid Success response format: {response}")
            elif "Error" in response:
                error_data = response["Error"]
                if isinstance(error_data, dict) and "message" in error_data:
                    error_msg = error_data["message"]
                    raise RuntimeError(f"Failed to get machine code: {error_msg}")
                else:
                    raise RuntimeError(f"Invalid Error response format: {response}")
            else:
                raise RuntimeError(f"Failed to get machine code: unknown response format")

        except FileNotFoundError:
            raise RuntimeError(f"susi_helper not found at: {self.susi_helper_path}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"susi_helper timed out after 10 seconds")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"susi_helper execution failed (return code {e.returncode}): {e.stderr}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {result.stdout if 'result' in locals() else 'No output'}")
        except Exception as e:
            raise RuntimeError(f"Unexpected error calling susi_helper: {e}")

    def create_signed_license(
        self,
        license_data: Dict,
        device_id: str,
        duration_days: int
    ) -> Dict:
        """Create SignedLicense using susi_helper sign_license command

        Args:
            license_data: License data containing id, license_key, created_at, features
            device_id: Device identifier
            duration_days: License duration in days

        Returns:
            Dict containing signed_license (optional) and any error information
        """
        logger.debug("Creating signed license for device %s", device_id)

        # Resolve the signing key before the try block so a configuration error
        # surfaces with its own message rather than being rewrapped by the generic
        # handler below as an "unexpected error".
        private_key_pem = self._require_private_key()

        try:
            # Generate machine code for this device
            machine_code = self.get_machine_code()

            # Prepare license payload for signing
            # Note: Susi LicensePayload format - we use Susi fields, not our business fields
            payload = {
                "id": str(license_data["id"]),
                "product": "DouyinLowLatencyViewer",
                "customer": "DouyinLowLatencyViewer",  # Default customer
                "license_key": license_data["license_key"],
                "created": self._to_rfc3339(license_data["created_at"]),
                # Map real Authorization.expires_at to Susi expires field
                "expires": self._to_rfc3339(license_data["authorization"]["expires_at"]),
                "features": sorted(list(license_data["features"])),  # Convert set to sorted list for deterministic JSON serialization
                "machine_codes": [machine_code],
                "lease_expires": None,
                "lease_grace_period": None,
                "require_signed_binary": False
            }

            # Prepare sign command with private key and payload
            sign_command = {
                "command": "SignLicense",
                "private_key_pem": private_key_pem,
                "payload": payload
            }
            sign_command_json = json.dumps(sign_command)

            # Check for any issues with the JSON
            try:
                parsed_back = json.loads(sign_command_json)
            except Exception as e:
                logger.error("Generated sign command is not valid JSON: %s", e)
                raise RuntimeError(f"Invalid JSON generated: {e}")

            # Execute susi_helper directly (avoid shell=True on Windows) with timeout
            # When text=True, input must be str, not bytes
            result = subprocess.run(
                [self.susi_helper_path],
                input=sign_command_json,
                capture_output=True,
                text=True,
                check=True,
                timeout=10  # 10 second timeout
            )

            logger.debug("susi_helper SignLicense returned %s", result.returncode)

            # Parse JSON response
            response = json.loads(result.stdout)

            # Handle susi_helper custom response format: {"Success": {"data": "{\"license_data\":\"...\",\"signature\":\"...\"}"}}
            if "Success" in response:
                success_data = response["Success"]
                if isinstance(success_data, dict) and "data" in success_data:
                    # The data field contains a JSON string, need to parse it
                    data_string = success_data["data"]
                    try:
                        signed_license_data = json.loads(data_string)
                        if isinstance(signed_license_data, dict) and "license_data" in signed_license_data and "signature" in signed_license_data:
                            signed_license = json.dumps(signed_license_data)
                            logger.debug("Created signed license (%d bytes)", len(signed_license))
                            return {
                                "signed_license": signed_license,
                                "machine_code": machine_code
                            }
                        else:
                            raise RuntimeError(f"Invalid signed license data format: {signed_license_data}")
                    except json.JSONDecodeError as e:
                        raise RuntimeError(f"Failed to parse signed license data: {e}")
                else:
                    raise RuntimeError(f"Invalid Success response format: {response}")
            elif "Error" in response:
                error_data = response["Error"]
                if isinstance(error_data, dict) and "message" in error_data:
                    error_msg = error_data["message"]
                    raise RuntimeError(f"Failed to sign license: {error_msg}")
                else:
                    raise RuntimeError(f"Invalid Error response format: {response}")
            else:
                raise RuntimeError(f"Failed to sign license: unknown response format")

        except FileNotFoundError:
            raise RuntimeError(f"susi_helper not found at: {self.susi_helper_path}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"susi_helper timed out after 10 seconds")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"susi_helper execution failed (return code {e.returncode}): {e.stderr}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {result.stdout if 'result' in locals() else 'No output'}")
        except Exception as e:
            raise RuntimeError(f"Unexpected error creating signed license: {e}")

    def verify_license(self, signed_license: str, public_key_pem: str) -> Dict:
        """Verify a signed license using susi_helper"""
        logger.debug("Verifying signed license (%d bytes)", len(signed_license))

        try:
            # Prepare verify command - ensure public key is not JSON escaped
            verify_command = {
                "command": "Verify",
                "signed_license": signed_license,
                "public_key_pem": public_key_pem.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
            }
            verify_command_json = json.dumps(verify_command)

            # Execute susi_helper directly (avoid shell=True on Windows) with timeout
            # When text=True, input must be str, not bytes
            result = subprocess.run(
                [self.susi_helper_path],
                input=verify_command_json,
                capture_output=True,
                text=True,
                check=True,
                timeout=10  # 10 second timeout
            )

            logger.debug("susi_helper Verify returned %s", result.returncode)

            # Parse JSON response
            response = json.loads(result.stdout)

            # Handle susi_helper custom response format: {"Success": {"data": {...}}} or {"Error": {"message": "error"}}
            if "Success" in response:
                success_data = response["Success"]
                if isinstance(success_data, dict) and "data" in success_data:
                    verification_result = success_data["data"]
                    logger.debug("License signature verified by susi_helper")
                    return verification_result
                else:
                    raise RuntimeError(f"Invalid Success response format: {response}")
            elif "Error" in response:
                error_data = response["Error"]
                if isinstance(error_data, dict) and "message" in error_data:
                    error_msg = error_data["message"]
                    raise RuntimeError(f"License verification failed: {error_msg}")
                else:
                    raise RuntimeError(f"Invalid Error response format: {response}")
            else:
                raise RuntimeError(f"License verification failed: unknown response format")

        except FileNotFoundError:
            raise RuntimeError(f"susi_helper not found at: {self.susi_helper_path}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"susi_helper timed out after 10 seconds")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"susi_helper execution failed (return code {e.returncode}): {e.stderr}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {result.stdout if 'result' in locals() else 'No output'}")
        except Exception as e:
            raise RuntimeError(f"Unexpected error verifying license: {e}")