"""Tests for scripts/code_intel.py — all subcommands, regex fallback path."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

# Ensure the skill package root is importable
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "skills" / "codereview")
)

from scripts.code_intel import (  # noqa: E402
    _complexity_regex,
    _detect_language,
    _is_exported,
    _read_file_safe,
    _score_to_rating,
    cmd_callers,
    cmd_complexity,
    cmd_exports,
    cmd_format_diff,
    cmd_functions,
    cmd_graph,
    cmd_imports,
    cmd_patterns,
    cmd_setup,
    format_functions_summary,
    format_graph_summary,
)

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "code_intel"
REPO_ROOT = TESTS_DIR.parent
SCRIPT = str(REPO_ROOT / "scripts" / "code_intel.py")


class TestLanguageDetection(unittest.TestCase):
    def test_python(self) -> None:
        self.assertEqual(_detect_language("src/auth.py"), "python")

    def test_go(self) -> None:
        self.assertEqual(_detect_language("main.go"), "go")

    def test_typescript(self) -> None:
        self.assertEqual(_detect_language("app.ts"), "typescript")
        self.assertEqual(_detect_language("component.tsx"), "typescript")

    def test_javascript(self) -> None:
        self.assertEqual(_detect_language("index.js"), "javascript")
        self.assertEqual(_detect_language("App.jsx"), "javascript")

    def test_java(self) -> None:
        self.assertEqual(_detect_language("Main.java"), "java")

    def test_rust(self) -> None:
        self.assertEqual(_detect_language("lib.rs"), "rust")

    def test_shell(self) -> None:
        self.assertEqual(_detect_language("deploy.sh"), "shell")

    def test_unknown(self) -> None:
        self.assertIsNone(_detect_language("README.md"))
        self.assertIsNone(_detect_language("data.csv"))


class TestScoreToRating(unittest.TestCase):
    def test_ratings(self) -> None:
        self.assertEqual(_score_to_rating(3), "A")
        self.assertEqual(_score_to_rating(8), "B")
        self.assertEqual(_score_to_rating(15), "C")
        self.assertEqual(_score_to_rating(25), "D")
        self.assertEqual(_score_to_rating(35), "F")


class TestIsExported(unittest.TestCase):
    def test_python_public(self) -> None:
        self.assertTrue(_is_exported("calculate", "python"))

    def test_python_private(self) -> None:
        self.assertFalse(_is_exported("_helper", "python"))

    def test_go_exported(self) -> None:
        self.assertTrue(_is_exported("PublicFunc", "go"))

    def test_go_unexported(self) -> None:
        self.assertFalse(_is_exported("privateFunc", "go"))


class TestComplexitySubcommand(unittest.TestCase):
    def test_complexity_produces_valid_json(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_complexity([sample_py])

        self.assertIn("analyzer", result)
        self.assertIn("hotspots", result)
        self.assertIn("tool_status", result)
        self.assertIsInstance(result["hotspots"], list)

    def test_complexity_finds_hotspots_in_python(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_complexity([sample_py])
        hotspot_names = [h["function"] for h in result["hotspots"]]
        self.assertIn("complex_decision", hotspot_names)

    def test_complexity_finds_hotspots_in_go(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_complexity([sample_go])
        hotspot_names = [h["function"] for h in result["hotspots"]]
        self.assertIn("ComplexHandler", hotspot_names)

    def test_complexity_empty_input(self) -> None:
        result = cmd_complexity([])
        self.assertEqual(result["hotspots"], [])

    def test_complexity_nonexistent_file(self) -> None:
        result = cmd_complexity(["/nonexistent/file.py"])
        self.assertEqual(result["hotspots"], [])

    def test_complexity_regex_reports_score_and_rating(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        results, analyzer = _complexity_regex([sample_py])
        self.assertEqual(analyzer, "regex-only")
        for r in results:
            self.assertIsInstance(r.score, int)
            self.assertIn(r.rating, ("A", "B", "C", "D", "F"))
            self.assertGreater(r.score, 0)


class TestFunctionsSubcommand(unittest.TestCase):
    def test_functions_extracts_python_functions(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_functions([sample_py])
        self.assertIn("functions", result)
        names = [f["name"] for f in result["functions"]]
        self.assertIn("simple_function", names)
        self.assertIn("complex_decision", names)
        self.assertIn("_private_helper", names)

    def test_functions_extracts_go_functions(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_functions([sample_go])
        names = [f["name"] for f in result["functions"]]
        self.assertIn("PublicFunction", names)
        self.assertIn("privateHelper", names)
        self.assertIn("ComplexHandler", names)
        self.assertIn("main", names)

    def test_functions_extracts_typescript_functions(self) -> None:
        sample_ts = str(FIXTURES_DIR / "sample.ts")
        result = cmd_functions([sample_ts])
        names = [f["name"] for f in result["functions"]]
        self.assertIn("handleRequest", names)
        self.assertIn("internalHelper", names)
        self.assertIn("defaultExport", names)

    def test_functions_has_expected_fields(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_functions([sample_py])
        for f in result["functions"]:
            self.assertIn("file", f)
            self.assertIn("name", f)
            self.assertIn("params", f)
            self.assertIn("returns", f)
            self.assertIn("line_start", f)
            self.assertIn("line_end", f)
            self.assertIn("exported", f)
            self.assertIn("language", f)

    def test_functions_empty_input(self) -> None:
        result = cmd_functions([])
        self.assertEqual(result["functions"], [])

    def test_functions_python_export_detection(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_functions([sample_py])
        funcs = {f["name"]: f for f in result["functions"]}
        self.assertTrue(funcs["simple_function"]["exported"])
        self.assertFalse(funcs["_private_helper"]["exported"])

    def test_functions_go_export_detection(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_functions([sample_go])
        funcs = {f["name"]: f for f in result["functions"]}
        self.assertTrue(funcs["PublicFunction"]["exported"])
        self.assertFalse(funcs["privateHelper"]["exported"])

    def test_functions_typescript_export_detection(self) -> None:
        sample_ts = str(FIXTURES_DIR / "sample.ts")
        result = cmd_functions([sample_ts])
        funcs = {f["name"]: f for f in result["functions"]}
        self.assertTrue(funcs["handleRequest"]["exported"])
        self.assertFalse(funcs["internalHelper"]["exported"])


class TestImportsSubcommand(unittest.TestCase):
    def test_imports_extracts_python_imports(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_imports([sample_py])
        self.assertIn("imports", result)
        modules = [i["module"] for i in result["imports"]]
        self.assertIn("os", modules)
        self.assertIn("json", modules)
        self.assertIn("pathlib", modules)

    def test_imports_extracts_go_imports(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_imports([sample_go])
        modules = [i["module"] for i in result["imports"]]
        self.assertIn("fmt", modules)
        self.assertIn("strings", modules)

    def test_imports_extracts_typescript_imports(self) -> None:
        sample_ts = str(FIXTURES_DIR / "sample.ts")
        result = cmd_imports([sample_ts])
        modules = [i["module"] for i in result["imports"]]
        self.assertIn("express", modules)
        self.assertIn("fs", modules)

    def test_imports_has_expected_fields(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_imports([sample_py])
        for imp in result["imports"]:
            self.assertIn("file", imp)
            self.assertIn("module", imp)
            self.assertIn("names", imp)
            self.assertIn("line", imp)

    def test_imports_empty_input(self) -> None:
        result = cmd_imports([])
        self.assertEqual(result["imports"], [])

    def test_imports_from_import_extracts_names(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_imports([sample_py])
        pathlib_import = next(
            (i for i in result["imports"] if i["module"] == "pathlib"), None
        )
        self.assertIsNotNone(pathlib_import)
        self.assertIn("Path", pathlib_import["names"])


class TestExportsSubcommand(unittest.TestCase):
    def test_exports_python_public_api(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_exports([sample_py])
        self.assertIn("exports", result)
        names = [e["name"] for e in result["exports"]]
        self.assertIn("simple_function", names)
        self.assertIn("complex_decision", names)
        self.assertIn("UserService", names)
        # Private should be excluded
        self.assertNotIn("_private_helper", names)
        self.assertNotIn("_internal_cache", names)

    def test_exports_go_exported_symbols(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_exports([sample_go])
        names = [e["name"] for e in result["exports"]]
        self.assertIn("PublicFunction", names)
        self.assertIn("ComplexHandler", names)
        # Unexported should NOT be listed
        self.assertNotIn("privateHelper", names)

    def test_exports_typescript_export_keyword(self) -> None:
        sample_ts = str(FIXTURES_DIR / "sample.ts")
        result = cmd_exports([sample_ts])
        names = [e["name"] for e in result["exports"]]
        self.assertIn("handleRequest", names)
        self.assertIn("UserController", names)
        self.assertIn("API_VERSION", names)
        self.assertIn("defaultExport", names)
        # Non-exported internal helper should not appear
        self.assertNotIn("internalHelper", names)

    def test_exports_has_expected_fields(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_exports([sample_py])
        for e in result["exports"]:
            self.assertIn("file", e)
            self.assertIn("name", e)
            self.assertIn("kind", e)
            self.assertIn("line", e)

    def test_exports_empty_input(self) -> None:
        result = cmd_exports([])
        self.assertEqual(result["exports"], [])


class TestCallersSubcommand(unittest.TestCase):
    def test_callers_finds_call_sites(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_callers([sample_py], "simple_function")
        self.assertIn("target", result)
        self.assertEqual(result["target"], "simple_function")
        self.assertIn("call_sites", result)

    def test_callers_finds_go_call_sites(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_callers([sample_go], "PublicFunction")
        self.assertEqual(result["target"], "PublicFunction")
        # Should find the call in main()
        callers = [c["caller"] for c in result["call_sites"]]
        self.assertIn("main", callers)

    def test_callers_has_expected_fields(self) -> None:
        sample_go = str(FIXTURES_DIR / "sample.go")
        result = cmd_callers([sample_go], "PublicFunction")
        for site in result["call_sites"]:
            self.assertIn("file", site)
            self.assertIn("caller", site)
            self.assertIn("line", site)
            self.assertIn("context", site)

    def test_callers_empty_input(self) -> None:
        result = cmd_callers([], "something")
        self.assertEqual(result["call_sites"], [])
        self.assertEqual(result["target"], "something")

    def test_callers_no_match(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_callers([sample_py], "nonexistent_function_xyz")
        self.assertEqual(result["call_sites"], [])


class TestPatternsSubcommand(unittest.TestCase):
    def test_patterns_detects_sql_injection(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_patterns([sample_py])
        self.assertIn("findings", result)
        sql_findings = [
            f for f in result["findings"] if f["pattern"] == "sql-injection"
        ]
        self.assertTrue(len(sql_findings) > 0, "Should detect SQL injection")

    def test_patterns_detects_command_injection(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_patterns([sample_py])
        cmd_findings = [
            f for f in result["findings"] if f["pattern"] == "command-injection"
        ]
        self.assertTrue(len(cmd_findings) > 0, "Should detect command injection")

    def test_patterns_detects_empty_error_handler(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_patterns([sample_py])
        empty_handlers = [
            f for f in result["findings"] if f["pattern"] == "empty-error-handler"
        ]
        self.assertTrue(len(empty_handlers) > 0, "Should detect empty error handler")

    def test_patterns_output_has_deterministic_source(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_patterns([sample_py])
        for finding in result["findings"]:
            self.assertEqual(finding["source"], "deterministic")
            self.assertEqual(finding["confidence"], 1.0)

    def test_patterns_output_structure(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_patterns([sample_py])
        self.assertIn("analyzer", result)
        self.assertIn("findings", result)
        self.assertIn("tool_status", result)
        for finding in result["findings"]:
            self.assertIn("pattern", finding)
            self.assertIn("severity", finding)
            self.assertIn("file", finding)
            self.assertIn("line", finding)
            self.assertIn("summary", finding)
            self.assertIn("evidence", finding)

    def test_patterns_empty_input(self) -> None:
        result = cmd_patterns([])
        self.assertEqual(result["findings"], [])


class TestSetupSubcommand(unittest.TestCase):
    def test_setup_check_produces_output(self) -> None:
        from argparse import Namespace

        args = Namespace(check=True, install=False, tier="minimal", json_output=False)
        result = cmd_setup(args)
        self.assertIn("dependencies", result)
        self.assertIsInstance(result["dependencies"], list)
        for dep in result["dependencies"]:
            self.assertIn("name", dep)
            self.assertIn("installed", dep)
            self.assertIn("installer", dep)

    def test_setup_install_produces_commands(self) -> None:
        from argparse import Namespace

        args = Namespace(check=False, install=True, tier="full", json_output=False)
        result = cmd_setup(args)
        self.assertIn("install_commands", result)
        self.assertIn("tier", result)


class TestCLISubprocess(unittest.TestCase):
    """Test the CLI interface by running code_intel.py as a subprocess."""

    def _run_subcommand(
        self, subcommand: str, stdin: str = "", extra_args: list[str] | None = None
    ) -> dict:
        cmd = [sys.executable, SCRIPT, subcommand] + (extra_args or [])
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        return json.loads(proc.stdout)

    def test_cli_complexity(self) -> None:
        result = self._run_subcommand(
            "complexity", stdin=str(FIXTURES_DIR / "sample.py") + "\n"
        )
        self.assertIn("hotspots", result)
        self.assertIn("analyzer", result)

    def test_cli_functions(self) -> None:
        result = self._run_subcommand(
            "functions", stdin=str(FIXTURES_DIR / "sample.py") + "\n"
        )
        self.assertIn("functions", result)
        names = [f["name"] for f in result["functions"]]
        self.assertIn("simple_function", names)

    def test_cli_imports(self) -> None:
        result = self._run_subcommand(
            "imports", stdin=str(FIXTURES_DIR / "sample.py") + "\n"
        )
        self.assertIn("imports", result)

    def test_cli_exports(self) -> None:
        result = self._run_subcommand(
            "exports", stdin=str(FIXTURES_DIR / "sample.ts") + "\n"
        )
        self.assertIn("exports", result)

    def test_cli_callers(self) -> None:
        result = self._run_subcommand(
            "callers",
            stdin=str(FIXTURES_DIR / "sample.go") + "\n",
            extra_args=["--target", "PublicFunction"],
        )
        self.assertIn("call_sites", result)
        self.assertEqual(result["target"], "PublicFunction")

    def test_cli_patterns(self) -> None:
        result = self._run_subcommand(
            "patterns", stdin=str(FIXTURES_DIR / "sample.py") + "\n"
        )
        self.assertIn("findings", result)
        for f in result["findings"]:
            self.assertEqual(f["source"], "deterministic")
            self.assertEqual(f["confidence"], 1.0)

    def test_cli_setup_check(self) -> None:
        result = self._run_subcommand("setup", extra_args=["--check"])
        self.assertIn("dependencies", result)

    def test_cli_empty_stdin(self) -> None:
        result = self._run_subcommand("complexity", stdin="")
        self.assertEqual(result["hotspots"], [])

    def test_cli_multiple_files(self) -> None:
        files = "\n".join(
            [
                str(FIXTURES_DIR / "sample.py"),
                str(FIXTURES_DIR / "sample.go"),
                str(FIXTURES_DIR / "sample.ts"),
            ]
        )
        result = self._run_subcommand("functions", stdin=files + "\n")
        languages = {f["language"] for f in result["functions"]}
        self.assertTrue(
            len(languages) >= 2, f"Expected multiple languages, got {languages}"
        )


class TestFormatFunctionsSummary(unittest.TestCase):
    def test_empty_functions(self) -> None:
        result = format_functions_summary({"functions": []})
        self.assertEqual(result, "")

    def test_formats_table(self) -> None:
        funcs = {
            "functions": [
                {
                    "file": "src/auth.py",
                    "name": "validate",
                    "params": ["request", "token"],
                    "returns": "bool",
                    "line_start": 10,
                    "line_end": 20,
                    "exported": True,
                },
            ]
        }
        result = format_functions_summary(funcs)
        self.assertIn("### Function Definitions", result)
        self.assertIn("validate", result)
        self.assertIn("request, token", result)
        self.assertIn("10-20", result)
        self.assertIn("yes", result)


class TestEdgeCases(unittest.TestCase):
    def test_read_file_safe_nonexistent(self) -> None:
        self.assertIsNone(_read_file_safe("/nonexistent/file.py"))

    def test_detect_language_no_extension(self) -> None:
        self.assertIsNone(_detect_language("Makefile"))

    def test_all_subcommands_handle_binary_path(self) -> None:
        """Binary / unreadable files should not crash any subcommand."""
        fake_path = "/nonexistent/binary.py"
        self.assertEqual(cmd_complexity([fake_path])["hotspots"], [])
        self.assertEqual(cmd_functions([fake_path])["functions"], [])
        self.assertEqual(cmd_imports([fake_path])["imports"], [])
        self.assertEqual(cmd_exports([fake_path])["exports"], [])
        self.assertEqual(cmd_callers([fake_path], "foo")["call_sites"], [])
        self.assertEqual(cmd_patterns([fake_path])["findings"], [])


class TestGraphSubcommand(unittest.TestCase):
    def test_graph_produces_valid_json(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py])
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertIn("stats", result)
        self.assertIsInstance(result["nodes"], list)
        self.assertIsInstance(result["edges"], list)
        self.assertEqual(result["stats"]["files_traversed"], 1)
        self.assertEqual(result["stats"]["depth"], 1)

    def test_graph_has_function_nodes(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py])
        node_ids = [n["id"] for n in result["nodes"]]
        self.assertTrue(any("simple_function" in nid for nid in node_ids))
        for node in result["nodes"]:
            self.assertEqual(node["kind"], "function")
            self.assertTrue(node["modified_in_diff"])

    def test_graph_has_import_edges(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py])
        import_edges = [e for e in result["edges"] if e["type"] == "imports"]
        self.assertTrue(len(import_edges) > 0)
        modules = [e["to"] for e in import_edges]
        self.assertIn("os", modules)

    def test_graph_empty_input(self) -> None:
        result = cmd_graph([])
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["stats"]["nodes"], 0)
        self.assertEqual(result["stats"]["edges"], 0)

    def test_graph_nonexistent_file(self) -> None:
        result = cmd_graph(["/nonexistent/file.py"])
        self.assertEqual(result["nodes"], [])

    def test_graph_depth_flag(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py], depth=2)
        self.assertEqual(result["stats"]["depth"], 2)

    def test_graph_multiple_files(self) -> None:
        files = [str(FIXTURES_DIR / "sample.py"), str(FIXTURES_DIR / "sample.go")]
        result = cmd_graph(files)
        self.assertEqual(result["stats"]["files_traversed"], 2)
        self.assertTrue(result["stats"]["nodes"] > 0)


class TestFormatGraphSummary(unittest.TestCase):
    def test_empty_graph(self) -> None:
        self.assertEqual(format_graph_summary({"nodes": [], "edges": []}), "")

    def test_formats_changed_symbols(self) -> None:
        graph = {
            "nodes": [
                {
                    "id": "src/auth.py::validate",
                    "kind": "function",
                    "file": "src/auth.py",
                    "line": 42,
                    "modified_in_diff": True,
                }
            ],
            "edges": [],
        }
        result = format_graph_summary(graph)
        self.assertIn("Changed symbols:", result)
        self.assertIn("validate", result)
        self.assertIn("src/auth.py:42", result)

    def test_formats_dependency_edges(self) -> None:
        graph = {
            "nodes": [],
            "edges": [
                {"from": "src/api.py", "to": "src/auth", "type": "imports", "line": 3}
            ],
        }
        result = format_graph_summary(graph)
        self.assertIn("Files that depend on changes:", result)
        self.assertIn("imports", result)


class TestFormatDiff(unittest.TestCase):
    SIMPLE_DIFF = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "index abc..def 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -10,5 +10,6 @@ def validate_session\n"
        " session = cache.get(token)\n"
        "-if session:\n"
        "-    return session\n"
        "+if session and not session.expired:\n"
        "+    session.refresh()\n"
        "+    return session\n"
        " return None\n"
    )

    MULTI_FILE_DIFF = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "index abc..def 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -5,3 +5,4 @@ def login\n"
        " user = db.get(name)\n"
        "+user.last_login = now()\n"
        " return user\n"
        "diff --git a/src/views.py b/src/views.py\n"
        "index abc..def 100644\n"
        "--- a/src/views.py\n"
        "+++ b/src/views.py\n"
        "@@ -20,3 +20,4 @@ def render\n"
        " ctx = build_ctx()\n"
        "+ctx['extra'] = True\n"
        " return template(ctx)\n"
    )

    def test_format_diff_simple(self) -> None:
        result = cmd_format_diff(self.SIMPLE_DIFF)
        self.assertIn("## File: src/auth.py", result)
        self.assertIn("__new hunk__", result)
        self.assertIn("__old hunk__", result)
        # Line numbers should appear in new hunk
        self.assertIn("10", result)
        # Additions should have + marker
        self.assertIn("+", result)
        # Deletions should appear in old hunk
        self.assertIn("-if session:", result)

    def test_format_diff_preserves_function_context(self) -> None:
        result = cmd_format_diff(self.SIMPLE_DIFF)
        self.assertIn("def validate_session", result)

    def test_format_diff_multi_file(self) -> None:
        result = cmd_format_diff(self.MULTI_FILE_DIFF)
        self.assertIn("## File: src/auth.py", result)
        self.assertIn("## File: src/views.py", result)

    def test_format_diff_empty_input(self) -> None:
        result = cmd_format_diff("")
        self.assertEqual(result, "")

    def test_format_diff_whitespace_only(self) -> None:
        result = cmd_format_diff("  \n  \n  ")
        self.assertEqual(result, "")

    def test_format_diff_new_hunk_has_line_numbers(self) -> None:
        result = cmd_format_diff(self.SIMPLE_DIFF)
        lines = result.split("\n")
        in_new = False
        found_numbered = False
        for line in lines:
            if line == "__new hunk__":
                in_new = True
                continue
            if line == "__old hunk__":
                in_new = False
                continue
            if in_new and line.strip():
                # New hunk lines should start with a number
                self.assertTrue(
                    line[0].isdigit(), f"Expected line number, got: {line!r}"
                )
                found_numbered = True
        self.assertTrue(
            found_numbered, "Should have at least one numbered line in new hunk"
        )

    def test_format_diff_old_hunk_no_line_numbers(self) -> None:
        result = cmd_format_diff(self.SIMPLE_DIFF)
        lines = result.split("\n")
        in_old = False
        for line in lines:
            if line == "__old hunk__":
                in_old = True
                continue
            if line.startswith("__") or line.startswith("##") or line.startswith("@@"):
                in_old = False
                continue
            if in_old and line.strip():
                # Old hunk lines should NOT start with a number
                self.assertFalse(
                    line[0].isdigit(),
                    f"Old hunk should not have line numbers: {line!r}",
                )


class TestGraphCLI(unittest.TestCase):
    """Test graph and format-diff CLI via subprocess."""

    def test_cli_graph(self) -> None:
        proc = subprocess.run(
            [sys.executable, SCRIPT, "graph"],
            input=str(FIXTURES_DIR / "sample.py") + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        result = json.loads(proc.stdout)
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertIn("stats", result)

    def test_cli_graph_empty(self) -> None:
        proc = subprocess.run(
            [sys.executable, SCRIPT, "graph"],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        result = json.loads(proc.stdout)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])

    def test_cli_format_diff(self) -> None:
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index abc..def 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@ def foo\n"
            " x = 1\n"
            "+y = 2\n"
            " z = 3\n"
        )
        proc = subprocess.run(
            [sys.executable, SCRIPT, "format-diff"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertIn("## File: foo.py", proc.stdout)
        self.assertIn("__new hunk__", proc.stdout)
        self.assertIn("__old hunk__", proc.stdout)

    def test_cli_format_diff_empty(self) -> None:
        proc = subprocess.run(
            [sys.executable, SCRIPT, "format-diff"],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
