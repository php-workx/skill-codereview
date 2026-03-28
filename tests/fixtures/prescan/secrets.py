"""Fixture for prescan tests — hardcoded secrets (NOT a test file)."""

# This file simulates production code with hardcoded secrets.

DB_PASSWORD = "super_secret_123"  # gitleaks:allow
API_KEY = "sk-1234567890abcdef"  # gitleaks:allow
token = "my-auth-token-value"  # gitleaks:allow

# These should NOT match (no value assigned or empty)
password = os.getenv("PASSWORD")  # noqa: F821 — intentional fixture
secret = ""
