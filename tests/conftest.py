"""Pytest configuration and shared fixtures."""
import os
import pytest

# Prevent scheduler from starting during tests
os.environ.setdefault("SKIP_SCHEDULER", "1")
# Use a dummy DB URL for unit tests (no actual DB connection needed)
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret")
