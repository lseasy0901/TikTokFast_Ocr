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

    class Config:
        env_file = ".env"


settings = Settings()