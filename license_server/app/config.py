# -*- coding: utf-8 -*-
"""
Configuration for the License Server
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings


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

    # Susi security settings - Phase 7.2-6
    SUSI_HELPER_PATH: str = os.getenv(
        "SUSI_HELPER_PATH",
        r"D:\TIikTok_Ocr\DouyinLowLatencyViewer\susi_helper\target\release\susi_helper.exe"
    )
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
        env_file = ".env"


settings = Settings()
