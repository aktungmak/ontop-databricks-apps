"""Pytest fixtures and env defaults for app unit tests."""

from __future__ import annotations

import os

# Set before test modules import main (module-level Settings.from_env()).
os.environ.setdefault("DATABRICKS_HOST", "https://test.cloud.databricks.com")
os.environ.setdefault("DATABRICKS_WAREHOUSE_ID", "test-warehouse")
os.environ.setdefault("MAPPINGS_VOLUME_PATH", "/Volumes/test/mappings")
os.environ.setdefault("FM_MODEL_NAME", "test-model")
