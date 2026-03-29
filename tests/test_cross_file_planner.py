"""Tests for scripts/cross_file_planner.py deterministic logic."""

import json
import unittest

from scripts.cross_file_planner import (
    MAX_QUERIES,
    TOKEN_BUDGET_CHARS,
    VALID_CATEGORIES,
    _deterministic_queries,
    _enforce_budget,
    _format_output,
)


class DeterministicQueriesTests(unittest.TestCase):
    """Tests for _deterministic_queries()."""

    def test_generates_queries_from_functions(self) -> None:
        functions_data = {
            "functions": [
                {"name": "create_token", "file": "auth.py"},
                {"name": "verify_user", "file": "auth.py"},
            ]
        }
        queries = _deterministic_queries(functions_data, graph_data=None)
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["symbol_name"], "create_token")
        self.assertEqual(queries[0]["category"], "consumers")
        self.assertEqual(queries[0]["risk_level"], "high")
        self.assertIn("create_token", queries[0]["pattern"])
        self.assertIn("callers of create_token()", queries[0]["rationale"])

    def test_skips_short_names(self) -> None:
        functions_data = {
            "functions": [
                {"name": "x", "file": "a.py"},
                {"name": "ab", "file": "b.py"},
                {"name": "abc", "file": "c.py"},
            ]
        }
        queries = _deterministic_queries(functions_data, graph_data=None)
        names = [q["symbol_name"] for q in queries]
        self.assertNotIn("x", names)
        self.assertNotIn("ab", names)
        self.assertIn("abc", names)

    def test_caps_at_max_queries(self) -> None:
        functions_data = {
            "functions": [
                {"name": f"function_{i}", "file": "mod.py"} for i in range(20)
            ]
        }
        queries = _deterministic_queries(functions_data, graph_data=None)
        self.assertLessEqual(len(queries), MAX_QUERIES)

    def test_returns_empty_for_none_functions(self) -> None:
        queries = _deterministic_queries(None, graph_data=None)
        self.assertEqual(queries, [])

    def test_returns_empty_for_empty_functions(self) -> None:
        queries = _deterministic_queries({"functions": []}, graph_data=None)
        self.assertEqual(queries, [])

    def test_adds_upstream_queries_from_graph_data(self) -> None:
        functions_data = {"functions": [{"name": "process", "file": "main.py"}]}
        graph_data = {
            "imports": {
                "main.py": ["utils.helper"],
            }
        }
        queries = _deterministic_queries(functions_data, graph_data)
        categories = [q["category"] for q in queries]
        self.assertIn("consumers", categories)
        self.assertIn("upstream", categories)

    def test_handles_missing_function_fields(self) -> None:
        functions_data = {
            "functions": [
                {"name": "", "file": "a.py"},
                {"file": "b.py"},
                {"name": "valid_fn", "file": "c.py"},
            ]
        }
        queries = _deterministic_queries(functions_data, graph_data=None)
        names = [q["symbol_name"] for q in queries]
        self.assertEqual(names, ["valid_fn"])


class BudgetEnforcementTests(unittest.TestCase):
    """Tests for _enforce_budget()."""

    def test_under_budget_returns_unchanged(self) -> None:
        queries = [{"risk_level": "high"}]
        results = {
            "0": {"query": queries[0], "matches": ["a.py"]},
        }
        enforced = _enforce_budget(results, queries)
        self.assertEqual(enforced, results)

    def test_drops_low_risk_first(self) -> None:
        # Create results that collectively exceed the budget
        big_matches = [f"path/to/file_{i}.py" for i in range(100)]
        queries = [
            {"risk_level": "low", "category": "consumers"},
            {"risk_level": "high", "category": "consumers"},
        ]
        results = {
            "0": {"query": queries[0], "matches": big_matches},
            "1": {"query": queries[1], "matches": big_matches},
        }
        # Force a tiny budget to trigger enforcement
        original_budget = TOKEN_BUDGET_CHARS
        import scripts.cross_file_planner as planner_mod

        planner_mod.TOKEN_BUDGET_CHARS = 100
        try:
            enforced = _enforce_budget(dict(results), queries)
            # Low-risk should be dropped first
            if "0" in enforced and "1" in enforced:
                self.fail("Budget not enforced — both results still present")
            # If only one remains, it should be the high-risk one (key "1")
            if len(enforced) == 1:
                self.assertIn("1", enforced)
        finally:
            planner_mod.TOKEN_BUDGET_CHARS = original_budget


class FormatOutputTests(unittest.TestCase):
    """Tests for _format_output()."""

    def test_format_basic_sections(self) -> None:
        queries = [
            {
                "pattern": r"\bfoo\(",
                "rationale": "callers of foo()",
                "risk_level": "high",
                "category": "consumers",
                "symbol_name": "foo",
                "file_glob": None,
            }
        ]
        results = {
            "0": {
                "query": queries[0],
                "matches": ["bar.py", "baz.py"],
            }
        }
        output = _format_output(queries, results)
        self.assertIn("sections", output)
        self.assertIn("stats", output)
        self.assertEqual(len(output["sections"]), 1)
        section = output["sections"][0]
        self.assertEqual(section["category"], "consumers")
        self.assertEqual(section["risk_level"], "high")
        self.assertEqual(section["matches"], ["bar.py", "baz.py"])
        self.assertIn("foo", section["header"])

    def test_invalid_category_defaults_to_consumers(self) -> None:
        queries = [
            {
                "pattern": "x",
                "rationale": "test",
                "risk_level": "low",
                "category": "invalid_category",
                "symbol_name": "x",
                "file_glob": None,
            }
        ]
        results = {"0": {"query": queries[0], "matches": ["a.py"]}}
        output = _format_output(queries, results)
        self.assertEqual(output["sections"][0]["category"], "consumers")

    def test_valid_categories_accepted(self) -> None:
        for cat in VALID_CATEGORIES:
            queries = [
                {
                    "pattern": "x",
                    "rationale": "test",
                    "risk_level": "medium",
                    "category": cat,
                    "symbol_name": "x",
                    "file_glob": None,
                }
            ]
            results = {"0": {"query": queries[0], "matches": ["a.py"]}}
            output = _format_output(queries, results)
            self.assertEqual(
                output["sections"][0]["category"],
                cat,
                f"Category {cat} should be accepted",
            )

    def test_stats_reflect_queries_and_results(self) -> None:
        queries = [
            {
                "pattern": "a",
                "rationale": "",
                "risk_level": "high",
                "category": "consumers",
                "symbol_name": "a",
                "file_glob": None,
            },
            {
                "pattern": "b",
                "rationale": "",
                "risk_level": "low",
                "category": "test_impl",
                "symbol_name": "b",
                "file_glob": None,
            },
        ]
        # Only one query had results
        results = {
            "0": {"query": queries[0], "matches": ["x.py", "y.py"]},
        }
        output = _format_output(queries, results)
        self.assertEqual(output["stats"]["queries_planned"], 2)
        self.assertEqual(output["stats"]["queries_executed"], 1)
        self.assertEqual(output["stats"]["total_matches"], 2)
        self.assertFalse(output["stats"]["llm_used"])

    def test_empty_results_returns_empty_sections(self) -> None:
        output = _format_output(queries=[], results={})
        self.assertEqual(output["sections"], [])
        self.assertEqual(output["stats"]["queries_planned"], 0)
        self.assertEqual(output["stats"]["queries_executed"], 0)
        self.assertEqual(output["stats"]["total_matches"], 0)


class EndToEndPlannerTests(unittest.TestCase):
    """Integration tests for the main() function via subprocess."""

    def _script_path(self) -> str:
        from pathlib import Path

        return str(
            Path(__file__).resolve().parent.parent / "scripts" / "cross_file_planner.py"
        )

    def test_deterministic_fallback_produces_valid_json(self) -> None:
        import subprocess
        import sys

        input_data = json.dumps(
            {
                "diff_summary": "changed auth.py",
                "graph_data": None,
                "functions_data": {
                    "functions": [
                        {"name": "create_token", "file": "auth.py"},
                    ]
                },
                "model": "haiku",
                "prompt_path": "",
            }
        )
        result = subprocess.run(
            [sys.executable, self._script_path()],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("sections", output)
        self.assertIn("stats", output)
        self.assertFalse(output["stats"]["llm_used"])

    def test_empty_functions_produces_valid_json(self) -> None:
        import subprocess
        import sys

        input_data = json.dumps(
            {
                "diff_summary": "changed something",
                "graph_data": None,
                "functions_data": None,
                "model": "haiku",
                "prompt_path": "",
            }
        )
        result = subprocess.run(
            [sys.executable, self._script_path()],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["sections"], [])
        self.assertEqual(output["stats"]["queries_planned"], 0)


if __name__ == "__main__":
    unittest.main()
