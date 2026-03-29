"""Fixture that looks like a test file — P-SEC should skip this."""

# This file is a test file because its name starts with test_

password = "test_password_123"
api_key = "test-api-key-abc"


def test_login():
    secret = "test_secret"
    assert check_auth(secret)  # noqa: F821 — intentional undefined for fixture
