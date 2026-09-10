# -*- coding: utf-8 -*-
"""
Database setup for the License Server
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import config

# Create database engine
engine = create_engine(
    config.settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in config.settings.DATABASE_URL else {}
)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()

# Import all models for registration
from models import Authorization, License


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()