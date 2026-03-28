"""Fixture for prescan tests — hardcoded secrets (NOT a test file)."""

# This file simulates production code with hardcoded secrets.

DB_PASSWORD = (
    "super_secret_123"  # gitleaks:allow — test fixture for prescan P-SEC checker
)
API_KEY = (
    "sk-1234567890abcdef"  # gitleaks:allow — test fixture for prescan P-SEC checker
)
token = "my-auth-token-value"  # gitleaks:allow — test fixture for prescan P-SEC checker

# These should NOT match (no value assigned or empty)
password = os.getenv("PASSWORD")  # noqa: F821 — intentional, fixture for prescan
secret = ""
