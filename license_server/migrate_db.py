# -*- coding: utf-8 -*-
"""
Migrate database to include features column
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import Base, engine, SessionLocal
from models import Authorization, License
from sqlalchemy import text

# Drop all tables
print("Dropping all tables...")
Base.metadata.drop_all(bind=engine)
print("Tables dropped")

# Create all tables with new schema
print("Creating tables with new schema...")
Base.metadata.create_all(bind=engine)
print("Tables created successfully")

# Verify columns exist
print("\nVerifying columns...")
db = SessionLocal()

try:
    # Check licenses table has features column
    license_columns = [c[1] for c in db.execute(text("PRAGMA table_info(licenses)")).fetchall()]
    print(f"License columns: {license_columns}")
    if 'features' not in license_columns:
        print("ERROR: features column not found in licenses table!")
        sys.exit(1)

    # Check authorizations table
    auth_columns = [c[1] for c in db.execute(text("PRAGMA table_info(authorizations)")).fetchall()]
    print(f"Authorization columns: {auth_columns}")

    print("\nDatabase migration completed successfully!")
finally:
    db.close()
