# -*- coding: utf-8 -*-
"""
Configuration for the License Server
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings

#: <repo>/license_server -- the directory that holds .env. Derived from this
#: file's location instead of the process working directory.
_LICENSE_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Absolute path to the .env file.
_ENV_FILE = os.path.join(_LICENSE_SERVER_DIR, ".env")


class Settings(BaseSettings):
    """Application settings"""

    # Database
    DATABASE_URL: str = "sqlite:///./license_server.db"

    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here-change-in-production")
    ALGORITHM: str = "HS256"

    # API
    API_PREFIX: str = "/api/v1"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Admin (for development endpoints)
    ADMIN_API_KEY: Optional[str] = os.getenv("ADMIN_API_KEY")

    # License settings
    DEFAULT_MAX_DEVICES: int = 1

    # Business time zone - the single source of truth for "which calendar day is
    # it" across the project (Phase 7.4).
    #
    # Used by two consumers, deliberately kept as one setting:
    #   1. DAU bucketing (services/heartbeat_service.bucket_date) -- which day a
    #      heartbeat belongs to.
    #   2. Admin datetime display (app/admin/views.py) -- what the operator reads.
    # If these two ever disagreed, the admin would show activity on a different
    # day than the one the count was bucketed into.
    #
    # Storage and all business calculation stay UTC. Only the calendar-day
    # boundary and the display layer use this zone.
    BUSINESS_TZ_NAME: str = "Asia/Shanghai"

    # Susi security settings - Phase 7.2-6
    # Path to the susi_helper executable. Empty means "not configured": the service
    # then falls back to the project-root default with an OS-aware filename
    # (susi_helper.exe on Windows, susi_helper elsewhere). An absolute path baked in
    # here would silently pin the server to one developer machine and make the
    # fallback unreachable.
    SUSI_HELPER_PATH: str = os.getenv("SUSI_HELPER_PATH", "")
    # Signing key material is supplied via environment / .env only.
    # No private or public key is embedded in source.
    #
    # The private key is a multi-line PEM, which does not survive a bare .env /
    # environment round trip on Windows: unquoted it is silently truncated to the
    # "-----BEGIN ..." marker, and a UTF-16 paste yields NUL bytes. So the preferred
    # representation is a path to a PEM file -- SUSI_DEVELOPMENT_PRIVATE_KEY_FILE.
    # The inline variable is kept as a fallback and may use escaped \n.
    #
    # Precedence (implemented in SusiSecurityService):
    #   1. SUSI_DEVELOPMENT_PRIVATE_KEY_FILE, when set
    #   2. SUSI_DEVELOPMENT_PRIVATE_KEY, when set
    #   3. license_server/test_rsa_key.pem, when it exists (developer default)
    SUSI_DEVELOPMENT_PRIVATE_KEY_FILE: str = os.getenv(
        "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE", ""
    )
    SUSI_DEVELOPMENT_PRIVATE_KEY: str = os.getenv("SUSI_DEVELOPMENT_PRIVATE_KEY", "")
    SUSI_DEVELOPMENT_PUBLIC_KEY: str = os.getenv("SUSI_DEVELOPMENT_PUBLIC_KEY", "")

    class Config:
        # Absolute path. A bare ".env" is resolved against the process working
        # directory, so every setting below would be silently skipped whenever the
        # server is started with a CWD other than license_server/ (a process
        # manager, a service unit, or uvicorn --app-dir from elsewhere) -- and the
        # fallbacks below would quietly take over. Precedence is unchanged:
        # environment variable > .env > the defaults declared here.
        env_file = _ENV_FILE


settings = Settings()
