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


class SusiSecurityService:
    """Service for Susi security operations using susi_helper subprocess"""

    def __init__(self, config):
        self.config = config
        # Use hardcoded path to avoid path resolution issues
        # Resolve SUSI_HELPER_PATH deterministically from project root
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        self.susi_helper_path = os.path.join(project_root, 'susi_helper', 'target', 'release', 'susi_helper.exe')

        # Check if helper exists
        if not os.path.exists(self.susi_helper_path):
            raise RuntimeError(f"susi_helper not found at: {self.susi_helper_path}")
        self.development_private_key = self._get_setting('SUSI_DEVELOPMENT_PRIVATE_KEY')
        self.development_public_key = self._get_setting('SUSI_DEVELOPMENT_PUBLIC_KEY')

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
                "private_key_pem": self.development_private_key,
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