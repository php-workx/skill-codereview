"""Sample Python module for code_intel tests."""

import os
import json  # noqa: F401 — intentional unused import for code_intel tests
from pathlib import Path  # noqa: F401 — intentional unused import for code_intel tests
from typing import Optional


def simple_function(x, y):
    """A simple function."""
    return x + y


def _private_helper(data):
    """Private helper — should NOT be exported."""
    return data.strip()


def complex_decision(value, mode, flag):
    """Function with high cyclomatic complexity."""
    if value > 100:
        if mode == "strict":
            if flag:
                return "high-strict-flag"
            else:
                return "high-strict-noflag"
        elif mode == "relaxed":
            return "high-relaxed"
        else:
            return "high-other"
    elif value > 50:
        for i in range(value):
            if i % 2 == 0 and flag:
                return f"mid-even-{i}"
            elif i % 3 == 0 or mode == "special":
                continue
        return "mid-default"
    else:
        try:
            result = int(value)
        except (ValueError, TypeError):
            return "low-error"
        while result > 0:
            result -= 1
        return f"low-{result}"


class UserService:
    """A sample class with methods."""

    def get_user(self, user_id: int) -> Optional[dict]:
        """Fetch user by ID."""
        return {"id": user_id}

    def _internal_cache(self):
        """Private method."""
        pass


def fetch_orders(db, order_id):
    """Vulnerable to SQL injection."""
    db.execute("SELECT * FROM orders WHERE id=" + order_id)


def run_command(user_input):
    """Vulnerable to command injection."""
    os.system("echo " + user_input)


def risky_handler():
    """Empty error handler."""
    try:
        result = 1 / 0  # noqa: F841 — intentional for empty-handler test
    except Exception:
        pass
