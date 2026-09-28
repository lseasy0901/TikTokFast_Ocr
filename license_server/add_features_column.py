# -*- coding: utf-8 -*-
"""
Add features column to licenses table without dropping tables
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import engine, SessionLocal
from sqlalchemy import text

# Add features column to licenses table
print("Adding features column to licenses table...")
with engine.connect() as conn:
    # Check if column already exists
    check_result = conn.execute(text(
        "PRAGMA table_info(licenses)"
    )).fetchall()
    columns = [row[1] for row in check_result]

    if 'features' in columns:
        print("Column 'features' already exists in licenses table")
    else:
        conn.execute(text("ALTER TABLE licenses ADD COLUMN features TEXT"))
        print("Column 'features' added successfully")
        conn.commit()

# Verify the column exists
print("\nVerifying columns...")
with engine.connect() as conn:
    license_columns = [row[1] for row in conn.execute(text("PRAGMA table_info(licenses)")).fetchall()]
    print(f"License columns: {license_columns}")

    if 'features' in license_columns:
        print("\nDatabase migration completed successfully!")
    else:
        print("\nERROR: features column not found in licenses table!")
        sys.exit(1)
