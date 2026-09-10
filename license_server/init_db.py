# -*- coding: utf-8 -*-
"""
Initialize database with new schema
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import Base, engine
from models import Authorization, License

# Create all tables
Base.metadata.create_all(bind=engine)
print("Database initialized successfully")
