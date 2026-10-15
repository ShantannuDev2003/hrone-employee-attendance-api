"""
FIX 6a: Set MONGO_DB=hrone_test BEFORE app.main is imported.
This guarantees tests never touch the production (or load) database.
The test database is dropped at session start for a clean slate.
"""
import os
import sys
from pathlib import Path

# MUST happen before any import of app.main so the module-level MongoClient
# picks up the test database name.
os.environ.setdefault("MONGO_DB", "hrone_test")

# Ensure repository root is on sys.path for pytest
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest
from pymongo import MongoClient


@pytest.fixture(scope="session", autouse=True)
def drop_test_database():
    """Drop hrone_test at session start so every run starts clean."""
    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGO_DB", "hrone_test")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    client.drop_database(db_name)
    client.close()
    yield
    # Leave the database in place after the run so failures can be inspected.
