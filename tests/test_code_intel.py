"""Tests for scripts/code_intel.py — all subcommands, regex fallback path."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure the skill package root is importable
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "skills" / "codereview")
)

from scripts.code_intel import (  # noqa: E402
    CO_CHANGE_MAX_EDGES,
    CO_CHANGE_MIN_FREQUENCY,
    COMMON_NAMES,
    MAX_RESULTS_PER_SYMBOL,
    _build_semantic_edges,
    _complexity_regex,
    _detect_language,
    _detect_python_env,
    _detect_semantic_backend,
    _find_enclosing_function,
    _graph_cache_path,
    _is_exported,
    _load_graph_cache,
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
        # summary key with missing_by_tier counts
        self.assertIn("summary", result)
        summary = result["summary"]
        self.assertIn("installed", summary)
        self.assertIn("total", summary)
        self.assertEqual(summary["total"], len(result["dependencies"]))
        self.assertIn("missing_by_tier", summary)
        self.assertIn("minimal", summary["missing_by_tier"])
        self.assertIn("full", summary["missing_by_tier"])

    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_install_executes_commands(
        self, mock_which: unittest.mock.MagicMock, mock_run: unittest.mock.MagicMock
    ) -> None:
        """Mock subprocess.run, verify pip install is called."""
        from argparse import Namespace

        # Make all dep checks fail (not installed) so they get installed
        mock_which.return_value = "/usr/bin/pip"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        # Patch all dep checks to return False (not installed)
        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "tree-sitter",
                    "installer": "pip",
                    "package": "tree-sitter",
                    "tier": "minimal",
                    "check": lambda: False,
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=False,
            )
            result = cmd_setup(args)

        self.assertIn("results", result)
        self.assertIn("tier", result)
        self.assertIn("python_env", result)
        self.assertIn("verification", result)
        self.assertEqual(result["installed"], 1)
        self.assertEqual(result["failed"], 0)
        # subprocess.run should have been called for the install
        mock_run.assert_called()
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "pip")
        self.assertIn("tree-sitter", call_args)

    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_install_skips_installed(
        self, mock_which: unittest.mock.MagicMock, mock_run: unittest.mock.MagicMock
    ) -> None:
        """Already-installed deps are skipped."""
        from argparse import Namespace

        mock_which.return_value = "/usr/bin/pip"

        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "tree-sitter",
                    "installer": "pip",
                    "package": "tree-sitter",
                    "tier": "minimal",
                    "check": lambda: True,  # already installed
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=False,
            )
            result = cmd_setup(args)

        self.assertEqual(result["results"], [])
        self.assertEqual(result["installed"], 0)
        # subprocess.run should NOT have been called for install (only for verification)
        mock_run.assert_not_called()

    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_install_handles_missing_installer(
        self, mock_which: unittest.mock.MagicMock, mock_run: unittest.mock.MagicMock
    ) -> None:
        """When npm is not found, skip with reason."""
        from argparse import Namespace

        # shutil.which returns None for npm
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "npm":
                return None
            return f"/usr/bin/{cmd}"

        mock_which.side_effect = which_side_effect

        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "eslint",
                    "installer": "npm",
                    "package": "eslint",
                    "tier": "minimal",
                    "check": lambda: False,
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=False,
            )
            result = cmd_setup(args)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertIn("npm not available", result["results"][0]["reason"])

    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_non_interactive_exit_code_success(
        self, mock_which: unittest.mock.MagicMock, mock_run: unittest.mock.MagicMock
    ) -> None:
        """All succeed -> exit_code 0."""
        from argparse import Namespace

        mock_which.return_value = "/usr/bin/pip"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "tree-sitter",
                    "installer": "pip",
                    "package": "tree-sitter",
                    "tier": "minimal",
                    "check": lambda: False,
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=True,
            )
            result = cmd_setup(args)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["installed"], 1)

    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_non_interactive_exit_code_failure(
        self, mock_which: unittest.mock.MagicMock, mock_run: unittest.mock.MagicMock
    ) -> None:
        """Any fail -> exit_code 1."""
        from argparse import Namespace

        mock_which.return_value = "/usr/bin/pip"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error occurred"
        )

        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "tree-sitter",
                    "installer": "pip",
                    "package": "tree-sitter",
                    "tier": "minimal",
                    "check": lambda: False,
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=True,
            )
            result = cmd_setup(args)

        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["failed"], 1)

    @patch("scripts.code_intel._detect_python_env", return_value="system")
    @patch("scripts.code_intel.subprocess.run")
    @patch("scripts.code_intel.shutil.which")
    def test_setup_install_uses_user_flag_for_system_python(
        self,
        mock_which: unittest.mock.MagicMock,
        mock_run: unittest.mock.MagicMock,
        mock_env: unittest.mock.MagicMock,
    ) -> None:
        """system env -> --user flag on pip install."""
        from argparse import Namespace

        mock_which.return_value = "/usr/bin/pip"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with patch(
            "scripts.code_intel._DEPS",
            [
                {
                    "name": "tree-sitter",
                    "installer": "pip",
                    "package": "tree-sitter",
                    "tier": "minimal",
                    "check": lambda: False,
                },
            ],
        ):
            args = Namespace(
                check=False,
                install=True,
                tier="minimal",
                json_output=False,
                non_interactive=False,
            )
            result = cmd_setup(args)

        self.assertEqual(result["installed"], 1)
        call_args = mock_run.call_args[0][0]
        self.assertIn("--user", call_args)
        self.assertEqual(call_args, ["pip", "install", "--user", "tree-sitter"])


class TestDetectPythonEnv(unittest.TestCase):
    @patch("scripts.code_intel.sys")
    def test_detects_venv(self, mock_sys: unittest.mock.MagicMock) -> None:
        mock_sys.prefix = "/home/user/myproject/.venv"
        mock_sys.base_prefix = "/usr"
        self.assertEqual(_detect_python_env(), "venv")

    @patch("scripts.code_intel.sys")
    def test_detects_system(self, mock_sys: unittest.mock.MagicMock) -> None:
        mock_sys.prefix = "/usr"
        mock_sys.base_prefix = "/usr"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_detect_python_env(), "system")

    @patch("scripts.code_intel.sys")
    def test_detects_conda(self, mock_sys: unittest.mock.MagicMock) -> None:
        mock_sys.prefix = "/home/user/anaconda3/envs/myenv"
        mock_sys.base_prefix = "/home/user/anaconda3/envs/myenv"
        with patch.dict(os.environ, {"CONDA_DEFAULT_ENV": "myenv"}):
            self.assertEqual(_detect_python_env(), "conda")

    @patch("scripts.code_intel.sys")
    def test_detects_pipx(self, mock_sys: unittest.mock.MagicMock) -> None:
        mock_sys.prefix = "/home/user/.local/pipx/venvs/myapp"
        mock_sys.base_prefix = "/usr"
        self.assertEqual(_detect_python_env(), "pipx")


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
        self.assertIn("summary", result)
        self.assertIn("missing_by_tier", result["summary"])

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
        result = cmd_graph([sample_py], repo_root=str(FIXTURES_DIR))
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertIn("stats", result)
        self.assertIsInstance(result["nodes"], list)
        self.assertIsInstance(result["edges"], list)
        self.assertEqual(result["stats"]["files_traversed"], 1)
        self.assertEqual(result["stats"]["depth"], 1)

    def test_graph_has_function_nodes(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py], repo_root=str(FIXTURES_DIR))
        func_nodes = [n for n in result["nodes"] if n.get("modified_in_diff")]
        node_ids = [n["id"] for n in func_nodes]
        self.assertTrue(any("simple_function" in nid for nid in node_ids))
        for node in func_nodes:
            self.assertEqual(node["kind"], "function")
            self.assertTrue(node["modified_in_diff"])

    def test_graph_has_import_edges(self) -> None:
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py], repo_root=str(FIXTURES_DIR))
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
        result = cmd_graph([sample_py], depth=2, repo_root=str(FIXTURES_DIR))
        self.assertEqual(result["stats"]["depth"], 2)

    def test_graph_multiple_files(self) -> None:
        files = [str(FIXTURES_DIR / "sample.py"), str(FIXTURES_DIR / "sample.go")]
        result = cmd_graph(files, repo_root=str(FIXTURES_DIR))
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
            [sys.executable, SCRIPT, "graph", "--repo-root", str(FIXTURES_DIR)],
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


class TestGraphRepoSearch(unittest.TestCase):
    """Tests for repo-wide reference search in cmd_graph (step 2)."""

    def setUp(self) -> None:
        """Create a temp directory with Python files that cross-reference each other."""
        self.tmpdir = tempfile.mkdtemp()
        # A "library" file that defines a function
        self.lib_file = os.path.join(self.tmpdir, "lib_auth.py")
        Path(self.lib_file).write_text(
            "def validate_token(token):\n    return token is not None\n"
        )
        # A "caller" file that imports and calls the library function
        self.caller_file = os.path.join(self.tmpdir, "api_handler.py")
        Path(self.caller_file).write_text(
            "from lib_auth import validate_token\n"
            "\n"
            "def handle_request(req):\n"
            "    if validate_token(req.token):\n"
            "        return 200\n"
            "    return 401\n"
        )
        # Another caller file with a bare call (no wrapping function detected)
        self.caller2_file = os.path.join(self.tmpdir, "cli_runner.py")
        Path(self.caller2_file).write_text(
            "import lib_auth\n"
            "\n"
            "result = lib_auth.validate_token('abc')\n"
            "print(result)\n"
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_graph_finds_calls_edges(self) -> None:
        """Graph should produce 'calls' edges from callers to changed function."""
        result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        calls_edges = [e for e in result["edges"] if e["type"] == "calls"]
        self.assertTrue(
            len(calls_edges) > 0,
            f"Expected at least one 'calls' edge, got: {result['edges']}",
        )
        # The edge should point TO the changed function
        targets = [e["to"] for e in calls_edges]
        self.assertTrue(
            any("validate_token" in t for t in targets),
            f"Expected validate_token as a call target, got: {targets}",
        )

    def test_graph_adds_external_reference_nodes(self) -> None:
        """External caller files should appear as nodes with modified_in_diff=False."""
        result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        external_nodes = [n for n in result["nodes"] if not n["modified_in_diff"]]
        self.assertTrue(
            len(external_nodes) > 0,
            f"Expected external reference nodes, got: {result['nodes']}",
        )
        self.assertEqual(result["stats"]["external_references"], len(external_nodes))

    def test_graph_function_level_caller_node(self) -> None:
        """When the caller function is identifiable, add a function-level node."""
        result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        # api_handler.py::handle_request should be a caller node
        fn_nodes = [
            n
            for n in result["nodes"]
            if n.get("kind") == "function" and not n.get("modified_in_diff")
        ]
        fn_ids = [n["id"] for n in fn_nodes]
        self.assertTrue(
            any("handle_request" in fid for fid in fn_ids),
            f"Expected handle_request as a caller function node, got: {fn_ids}",
        )

    def test_graph_common_name_filtering(self) -> None:
        """Short names and COMMON_NAMES should be skipped in repo search."""
        # Create a file with only a common-name function
        common_file = os.path.join(self.tmpdir, "common.py")
        Path(common_file).write_text(
            "def get(key):\n    return key\n\ndef go(x):\n    return x\n"
        )
        # Create a file that calls them
        caller = os.path.join(self.tmpdir, "uses_common.py")
        Path(caller).write_text(
            "from common import get, go\nresult = get('x')\nresult2 = go(1)\n"
        )
        result = cmd_graph([common_file], repo_root=self.tmpdir)
        calls_edges = [e for e in result["edges"] if e["type"] == "calls"]
        # "get" is in COMMON_NAMES, "go" is < 3 chars; neither should produce calls
        self.assertEqual(
            len(calls_edges),
            0,
            f"Common/short names should not produce calls edges, got: {calls_edges}",
        )
        # Verify the constants are what we expect
        self.assertIn("get", COMMON_NAMES)
        self.assertTrue(len("go") < 3)

    def test_graph_max_results_per_symbol_cap(self) -> None:
        """Should cap matching files per symbol at MAX_RESULTS_PER_SYMBOL."""
        # Create many caller files
        for i in range(MAX_RESULTS_PER_SYMBOL + 10):
            f = os.path.join(self.tmpdir, f"caller_{i:03d}.py")
            Path(f).write_text(
                f"def caller_{i:03d}():\n    return validate_token('tok_{i}')\n"
            )
        result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        calls_edges = [e for e in result["edges"] if e["type"] == "calls"]
        # Should not exceed MAX_RESULTS_PER_SYMBOL callers
        self.assertLessEqual(
            len(calls_edges),
            MAX_RESULTS_PER_SYMBOL,
            f"Expected at most {MAX_RESULTS_PER_SYMBOL} calls edges, got {len(calls_edges)}",
        )

    def test_graph_timeout_handling(self) -> None:
        """Subprocess timeout should be caught gracefully, not crash."""
        with patch("scripts.code_intel.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="grep", timeout=5)
            result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        # Should still return valid graph with at least the original nodes
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        # The changed function nodes should still be present
        func_nodes = [n for n in result["nodes"] if n.get("modified_in_diff")]
        self.assertTrue(len(func_nodes) > 0)
        # No calls edges since grep was mocked to timeout
        calls_edges = [e for e in result["edges"] if e["type"] == "calls"]
        self.assertEqual(len(calls_edges), 0)

    def test_graph_stats_include_external_references(self) -> None:
        """Stats should include external_references count."""
        result = cmd_graph([self.lib_file], repo_root=self.tmpdir)
        self.assertIn("external_references", result["stats"])
        self.assertIsInstance(result["stats"]["external_references"], int)

    def test_graph_skips_changed_files_in_caller_search(self) -> None:
        """Files already in the changed set should not appear as callers."""
        # Both lib_auth.py and api_handler.py are "changed"
        result = cmd_graph([self.lib_file, self.caller_file], repo_root=self.tmpdir)
        # api_handler.py should NOT appear as an external caller node
        external_nodes = [n for n in result["nodes"] if not n["modified_in_diff"]]
        external_files = [n["file"] for n in external_nodes]
        self.assertNotIn(
            self.caller_file,
            external_files,
            "Changed files should not appear as external callers",
        )


class TestGraphCache(unittest.TestCase):
    """Tests for --cache incremental indexing in cmd_graph."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        # Create a simple Python source file
        self.src_file = os.path.join(self.tmpdir, "module.py")
        with open(self.src_file, "w") as f:
            f.write(
                "import os\n\n"
                "def compute_value(x):\n"
                "    return x * 2\n\n"
                "def helper():\n"
                "    return 42\n"
            )

    def test_graph_cache_miss_builds_and_saves(self) -> None:
        """First run with --cache creates cache file on disk."""
        cache_path = _graph_cache_path(self.cache_dir, self.tmpdir)
        self.assertFalse(cache_path.exists())

        result = cmd_graph(
            [self.src_file], repo_root=self.tmpdir, cache_dir=self.cache_dir
        )
        self.assertTrue(cache_path.exists())
        # Verify the result has expected structure
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertTrue(result["stats"]["nodes"] > 0)

        # Verify cache contents
        cached = _load_graph_cache(cache_path)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["version"], 1)
        self.assertIn("graph", cached)
        self.assertIn("file_mtimes", cached)
        self.assertIn(self.src_file, cached["file_mtimes"])

    def test_graph_cache_hit_reuses_data(self) -> None:
        """Second run with same files reuses cached data (no re-extraction)."""
        # First run: populate cache
        result1 = cmd_graph(
            [self.src_file], repo_root=self.tmpdir, cache_dir=self.cache_dir
        )

        # Second run: should use cache — patch _extract_functions to verify
        # it is NOT called for the cached file
        with patch("scripts.code_intel._extract_functions") as mock_extract:
            mock_extract.return_value = []
            result2 = cmd_graph(
                [self.src_file], repo_root=self.tmpdir, cache_dir=self.cache_dir
            )
            # _extract_functions should not be called since file is unchanged
            mock_extract.assert_not_called()

        # Output should be identical (same nodes/edges from cache)
        self.assertEqual(
            [n["id"] for n in result1["nodes"] if n.get("modified_in_diff")],
            [n["id"] for n in result2["nodes"] if n.get("modified_in_diff")],
        )

    def test_graph_cache_invalidation(self) -> None:
        """Modified file triggers re-extraction instead of using cache."""
        # First run: populate cache
        result1 = cmd_graph(
            [self.src_file], repo_root=self.tmpdir, cache_dir=self.cache_dir
        )
        original_node_ids = sorted(
            n["id"] for n in result1["nodes"] if n.get("modified_in_diff")
        )

        # Modify the file (also bump mtime to ensure OS registers the change)
        import time as _time

        _time.sleep(0.05)
        with open(self.src_file, "w") as f:
            f.write(
                "import os\n\n"
                "def compute_value(x):\n"
                "    return x * 2\n\n"
                "def new_function(y):\n"
                "    return y + 1\n"
            )

        # Second run: should re-extract because mtime changed
        result2 = cmd_graph(
            [self.src_file], repo_root=self.tmpdir, cache_dir=self.cache_dir
        )
        new_node_ids = sorted(
            n["id"] for n in result2["nodes"] if n.get("modified_in_diff")
        )

        # The node set should be different (new_function instead of helper)
        self.assertNotEqual(original_node_ids, new_node_ids)
        self.assertTrue(
            any("new_function" in nid for nid in new_node_ids),
            f"Expected new_function in {new_node_ids}",
        )

    def test_graph_no_cache_no_regression(self) -> None:
        """Without --cache, behaves exactly as before (no cache file created)."""
        result = cmd_graph([self.src_file], repo_root=self.tmpdir)
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertIn("stats", result)
        self.assertTrue(result["stats"]["nodes"] > 0)

        # No cache file should exist anywhere in cache_dir
        cache_path = _graph_cache_path(self.cache_dir, self.tmpdir)
        self.assertFalse(cache_path.exists())


class TestGraphCoChange(unittest.TestCase):
    """Tests for co-change frequency analysis in cmd_graph (step 4)."""

    @staticmethod
    def _create_temp_git_repo() -> str:
        tmpdir = tempfile.mkdtemp()
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmpdir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmpdir,
            capture_output=True,
        )
        return tmpdir

    def setUp(self) -> None:
        self.tmpdir = self._create_temp_git_repo()
        self.file_a = os.path.join(self.tmpdir, "alpha.py")
        self.file_b = os.path.join(self.tmpdir, "beta.py")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _commit_files(self, files: list[str], message: str) -> None:
        """Write a line to each file and commit them together."""
        for fpath in files:
            with open(fpath, "a") as f:
                f.write(f"# {message}\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.tmpdir,
            capture_output=True,
        )

    def test_graph_co_change_edges(self) -> None:
        """Two files committed together 3+ times produce a co_change edge."""
        # Write initial function so nodes get extracted
        Path(self.file_a).write_text("def process_data(x):\n    return x\n")
        Path(self.file_b).write_text("def helper(y):\n    return y\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir,
            capture_output=True,
        )

        # Commit both files together 3 more times
        for i in range(3):
            self._commit_files([self.file_a, self.file_b], f"change {i}")

        result = cmd_graph(["alpha.py"], repo_root=self.tmpdir)
        co_edges = [e for e in result["edges"] if e["type"] == "co_change"]
        self.assertTrue(
            len(co_edges) > 0,
            f"Expected at least one co_change edge, got: {result['edges']}",
        )
        # The edge should point from alpha.py to beta.py
        targets = [e["to"] for e in co_edges]
        self.assertTrue(
            any("beta.py" in t for t in targets),
            f"Expected beta.py as co-change target, got: {targets}",
        )
        # Frequency should be >= CO_CHANGE_MIN_FREQUENCY
        for e in co_edges:
            if "beta.py" in e["to"]:
                self.assertGreaterEqual(e["frequency"], CO_CHANGE_MIN_FREQUENCY)
        # Stats should include co_change_edges count
        self.assertIn("co_change_edges", result["stats"])
        self.assertGreater(result["stats"]["co_change_edges"], 0)

    def test_graph_co_change_frequency_threshold(self) -> None:
        """Files changed together only twice should NOT produce a co_change edge."""
        Path(self.file_a).write_text("def process_data(x):\n    return x\n")
        Path(self.file_b).write_text("def helper(y):\n    return y\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir,
            capture_output=True,
        )

        # Only 2 co-changes (below threshold of 3)
        for i in range(2):
            self._commit_files([self.file_a, self.file_b], f"change {i}")

        result = cmd_graph(["alpha.py"], repo_root=self.tmpdir)
        co_edges = [e for e in result["edges"] if e["type"] == "co_change"]
        beta_edges = [e for e in co_edges if "beta.py" in e["to"]]
        self.assertEqual(
            len(beta_edges),
            0,
            f"Expected no co_change edge for frequency < {CO_CHANGE_MIN_FREQUENCY}, "
            f"got: {beta_edges}",
        )

    def test_graph_co_change_max_edges_cap(self) -> None:
        """Verify co_change edges are capped at CO_CHANGE_MAX_EDGES."""
        # Create many files that all change together with alpha.py
        extra_files = []
        for i in range(CO_CHANGE_MAX_EDGES + 10):
            fpath = os.path.join(self.tmpdir, f"extra_{i}.py")
            extra_files.append(fpath)

        Path(self.file_a).write_text("def process_data(x):\n    return x\n")
        for fpath in extra_files:
            Path(fpath).write_text(f"# file {fpath}\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir,
            capture_output=True,
        )

        # Commit all files together CO_CHANGE_MIN_FREQUENCY times
        all_files = [self.file_a] + extra_files
        for i in range(CO_CHANGE_MIN_FREQUENCY):
            self._commit_files(all_files, f"batch {i}")

        result = cmd_graph(["alpha.py"], repo_root=self.tmpdir)
        co_edges = [e for e in result["edges"] if e["type"] == "co_change"]
        self.assertLessEqual(
            len(co_edges),
            CO_CHANGE_MAX_EDGES,
            f"Expected at most {CO_CHANGE_MAX_EDGES} co_change edges, "
            f"got {len(co_edges)}",
        )


class TestFormatDiffExpandContext(unittest.TestCase):
    """Tests for --expand-context function boundary expansion."""

    def setUp(self) -> None:
        """Create a temp directory with a Python file that has functions."""
        self.tmpdir = tempfile.mkdtemp()
        self.src_file = os.path.join(self.tmpdir, "module.py")
        # Source file: lines 1-12
        Path(self.src_file).write_text(
            "import os\n"  # line 1
            "\n"  # line 2
            "def compute(x):\n"  # line 3
            "    y = x * 2\n"  # line 4
            "    z = y + 1\n"  # line 5
            "    result = z ** 2\n"  # line 6
            "    return result\n"  # line 7
            "\n"  # line 8
            "def helper():\n"  # line 9
            "    return 42\n"  # line 10
            "\n"  # line 11
            "total = compute(5)\n"  # line 12
        )
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def tearDown(self) -> None:
        os.chdir(self._orig_cwd)

    def _make_diff(self, file_path: str, hunk_start: int = 5) -> str:
        """Create a minimal diff for a hunk starting at the given line."""
        return (
            f"diff --git a/{file_path} b/{file_path}\n"
            f"index abc..def 100644\n"
            f"--- a/{file_path}\n"
            f"+++ b/{file_path}\n"
            f"@@ -{hunk_start},3 +{hunk_start},4 @@\n"
            f" z = y + 1\n"
            f"-    result = z ** 2\n"
            f"+    result = z ** 3\n"
            f"+    # changed exponent\n"
            f" return result\n"
        )

    def test_format_diff_expand_context_adds_enclosing_function(self) -> None:
        """A hunk inside a function gets the def line prepended as context_before."""
        diff = self._make_diff("module.py", hunk_start=5)
        result = cmd_format_diff(diff, expand_context=True)
        # Should contain __context_before__ with the enclosing function definition
        self.assertIn("__context_before__", result)
        self.assertIn("def compute(x):", result)
        # The context_before should appear BEFORE __new hunk__
        cb_pos = result.index("__context_before__")
        nh_pos = result.index("__new hunk__")
        self.assertLess(cb_pos, nh_pos)
        # The enclosing function line number (3) should appear
        context_lines = []
        for line in result.split("\n"):
            if line == "__context_before__":
                collecting = True
                continue
            elif line.startswith("__"):
                collecting = False
                continue
            if "collecting" in dir() and collecting:
                context_lines.append(line)
        # Verify line numbers are present in context_before
        self.assertTrue(
            any("3" in ln and "def compute" in ln for ln in result.split("\n")),
            f"Expected line 3 with def compute in:\n{result}",
        )

    def test_format_diff_expand_context_adds_after_context(self) -> None:
        """Hunk expansion includes up to 3 lines of after-context."""
        diff = self._make_diff("module.py", hunk_start=5)
        result = cmd_format_diff(diff, expand_context=True)
        self.assertIn("__context_after__", result)
        # After-context should appear before __new hunk__
        ca_pos = result.index("__context_after__")
        nh_pos = result.index("__new hunk__")
        self.assertLess(ca_pos, nh_pos)

    def test_format_diff_expand_context_no_enclosing(self) -> None:
        """A hunk at the very top of the file has no enclosing function."""
        # Diff starting at line 1 — above any function definition
        diff = (
            "diff --git a/module.py b/module.py\n"
            "index abc..def 100644\n"
            "--- a/module.py\n"
            "+++ b/module.py\n"
            "@@ -1,2 +1,3 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
        )
        result = cmd_format_diff(diff, expand_context=True)
        # Should NOT have __context_before__ since there's no enclosing function
        self.assertNotIn("__context_before__", result)
        # Should still have the normal hunk structure
        self.assertIn("__new hunk__", result)
        self.assertIn("__old hunk__", result)

    def test_format_diff_expand_context_file_not_found(self) -> None:
        """Missing source file gracefully skips expansion."""
        diff = self._make_diff("nonexistent.py", hunk_start=5)
        result = cmd_format_diff(diff, expand_context=True)
        # Should still produce valid output without crashing
        self.assertIn("## File: nonexistent.py", result)
        self.assertIn("__new hunk__", result)
        self.assertIn("__old hunk__", result)
        # No context sections since source can't be read
        self.assertNotIn("__context_before__", result)
        self.assertNotIn("__context_after__", result)

    def test_format_diff_expand_context_heuristic_fallback(self) -> None:
        """Works without tree-sitter (keyword scan via _find_enclosing_function)."""
        # Directly test the helper function
        source_lines = [
            "import os",  # line 1
            "",  # line 2
            "def compute(x):",  # line 3
            "    y = x * 2",  # line 4
            "    z = y + 1",  # line 5
            "    result = z ** 2",  # line 6
            "    return result",  # line 7
        ]
        # Hunk starts at line 5 — should find def compute at line 3
        result = _find_enclosing_function(source_lines, 5)
        self.assertEqual(result, 3)

        # Hunk starts at line 1 — no enclosing function
        result = _find_enclosing_function(source_lines, 1)
        self.assertIsNone(result)

        # Test with async def
        async_lines = [
            "import asyncio",  # line 1
            "",  # line 2
            "async def fetch():",  # line 3
            "    await do_stuff()",  # line 4
            "    return data",  # line 5
        ]
        result = _find_enclosing_function(async_lines, 5)
        self.assertEqual(result, 3)

        # Test with class
        class_lines = [
            "class MyService:",  # line 1
            "    def __init__(self):",  # line 2
            "        self.x = 1",  # line 3
        ]
        result = _find_enclosing_function(class_lines, 3)
        self.assertEqual(result, 2)

        # Test with Rust fn
        rust_lines = [
            "use std::io;",  # line 1
            "",  # line 2
            "pub fn process(x: i32) -> i32 {",  # line 3
            "    let y = x * 2;",  # line 4
            "    y + 1",  # line 5
            "}",  # line 6
        ]
        result = _find_enclosing_function(rust_lines, 5)
        self.assertEqual(result, 3)

    def test_format_diff_expand_context_max_scan_limit(self) -> None:
        """Scan stops after max_scan (8) lines upward."""
        # Function at line 1, hunk at line 15 — gap of 13 lines, exceeds max_scan
        lines = ["def far_away():"] + ["    pass"] * 13 + ["    target_line = 1"]
        result = _find_enclosing_function(lines, 15)
        self.assertIsNone(result)

        # Function at line 7, hunk at line 15 — gap of 7, within max_scan (8)
        lines = (
            ["# comment"] * 6
            + ["def close_enough():"]
            + ["    pass"] * 7
            + ["    target_line = 1"]
        )
        result = _find_enclosing_function(lines, 15)
        self.assertEqual(result, 7)

    def test_format_diff_expand_context_preserves_existing_structure(self) -> None:
        """Expansion adds context sections but doesn't break hunk structure."""
        diff = self._make_diff("module.py", hunk_start=5)
        result_expanded = cmd_format_diff(diff, expand_context=True)
        result_normal = cmd_format_diff(diff, expand_context=False)

        # Both should have ## File header
        self.assertIn("## File: module.py", result_expanded)
        self.assertIn("## File: module.py", result_normal)

        # Both should have __new hunk__ and __old hunk__
        self.assertIn("__new hunk__", result_expanded)
        self.assertIn("__old hunk__", result_expanded)

        # Expanded should have context sections that normal doesn't
        self.assertIn("__context_before__", result_expanded)
        self.assertNotIn("__context_before__", result_normal)


class TestGraphDepth2(unittest.TestCase):
    """Tests for depth-2 traversal in cmd_graph."""

    def setUp(self) -> None:
        """Create a temp directory with A -> B -> C call chain.

        A (changed file) defines process_data(), which is called by
        B (api_handler.py, depth 1). B defines handle_request(), which
        is called by C (cli_runner.py, depth 2).
        """
        self.tmpdir = tempfile.mkdtemp()
        # A: the changed file
        self.file_a = os.path.join(self.tmpdir, "lib_core.py")
        Path(self.file_a).write_text(
            "def process_data(payload):\n    return payload.upper()\n"
        )
        # B: calls A (depth 1)
        self.file_b = os.path.join(self.tmpdir, "api_handler.py")
        Path(self.file_b).write_text(
            "from lib_core import process_data\n"
            "\n"
            "def handle_request(req):\n"
            "    return process_data(req.body)\n"
        )
        # C: calls B (depth 2)
        self.file_c = os.path.join(self.tmpdir, "cli_runner.py")
        Path(self.file_c).write_text(
            "from api_handler import handle_request\n"
            "\n"
            "def run_cli(args):\n"
            "    return handle_request(args)\n"
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_graph_depth_2_produces_more_nodes(self) -> None:
        """Depth 2 should find C (cli_runner.py) that calls B."""
        result_d1 = cmd_graph([self.file_a], depth=1, repo_root=self.tmpdir)
        result_d2 = cmd_graph([self.file_a], depth=2, repo_root=self.tmpdir)
        # Depth 2 should have at least as many nodes as depth 1
        self.assertGreaterEqual(
            len(result_d2["nodes"]),
            len(result_d1["nodes"]),
            f"depth 2 nodes ({len(result_d2['nodes'])}) should be >= "
            f"depth 1 nodes ({len(result_d1['nodes'])})",
        )
        # Depth 2 should include cli_runner.py
        all_files_d2 = {n["file"] for n in result_d2["nodes"]}
        cli_runner_abs = str(Path(self.file_c).resolve())
        self.assertIn(
            cli_runner_abs,
            all_files_d2,
            f"Expected cli_runner.py in depth-2 graph, got files: {all_files_d2}",
        )

    def test_graph_depth_2_hop_distance(self) -> None:
        """Nodes should have correct hop_distance values (0, 1, 2)."""
        result = cmd_graph([self.file_a], depth=2, repo_root=self.tmpdir)
        nodes = result["nodes"]
        distances = {n.get("hop_distance") for n in nodes}
        # Should have at least distances 0 and 1
        self.assertIn(0, distances, f"Expected hop_distance=0, got: {distances}")
        self.assertIn(1, distances, f"Expected hop_distance=1, got: {distances}")
        # Depth 2 nodes should be present
        self.assertIn(2, distances, f"Expected hop_distance=2, got: {distances}")
        # Verify specific nodes
        lib_core_abs = str(Path(self.file_a).resolve())
        for n in nodes:
            if n["file"] == lib_core_abs or n["file"] == self.file_a:
                self.assertEqual(
                    n["hop_distance"],
                    0,
                    f"Changed file node should have hop_distance=0, got: {n}",
                )

    def test_graph_depth_1_unchanged(self) -> None:
        """Depth 1 (default) should behave exactly as before -- no hop_distance=2 nodes."""
        result = cmd_graph([self.file_a], depth=1, repo_root=self.tmpdir)
        distances = {n.get("hop_distance") for n in result["nodes"]}
        # Should have 0 and possibly 1, but NOT 2
        self.assertNotIn(
            2,
            distances,
            "Depth 1 should not produce hop_distance=2 nodes",
        )
        # Stats should NOT include depth2_nodes key
        self.assertNotIn(
            "depth2_nodes",
            result["stats"],
            "Depth 1 stats should not include depth2_nodes",
        )

    def test_graph_depth_2_node_cap(self) -> None:
        """Nodes should be capped at MAX_DEPTH2_NODES (500) for depth 2."""
        # Create a large chain: many files calling functions in the caller file
        # This tests the cap without needing 500+ real files

        result = cmd_graph([self.file_a], depth=2, repo_root=self.tmpdir)
        # The result should be valid regardless of cap
        self.assertIn("nodes", result)
        self.assertLessEqual(
            len(result["nodes"]),
            500,
            f"Expected at most 500 nodes, got {len(result['nodes'])}",
        )

    def test_graph_depth_2_stats(self) -> None:
        """Stats should include depth2_nodes count when depth >= 2."""
        result = cmd_graph([self.file_a], depth=2, repo_root=self.tmpdir)
        self.assertIn("depth2_nodes", result["stats"])
        self.assertIsInstance(result["stats"]["depth2_nodes"], int)
        self.assertGreaterEqual(result["stats"]["depth2_nodes"], 0)
        self.assertEqual(result["stats"]["depth"], 2)

    def test_graph_depth_2_only_traverses_calls_edges(self) -> None:
        """Depth-2 should only follow 'calls' edges, not imports or co_change."""
        # Create a file that is only imported (no function call)
        import_only = os.path.join(self.tmpdir, "constants.py")
        Path(import_only).write_text("MAX_SIZE = 100\nMIN_SIZE = 1\n")
        # Create a changed file that imports constants but defines a callable function
        changed = os.path.join(self.tmpdir, "processor.py")
        Path(changed).write_text(
            "from constants import MAX_SIZE\n"
            "\n"
            "def transform_data(items):\n"
            "    return items[:MAX_SIZE]\n"
        )
        result = cmd_graph([changed], depth=2, repo_root=self.tmpdir)
        # The depth-2 nodes should only come from calls edges, not imports
        d2_nodes = [n for n in result["nodes"] if n.get("hop_distance") == 2]
        for n in d2_nodes:
            # Verify each depth-2 node has a corresponding calls edge
            has_calls_edge = any(
                e["type"] == "calls" and (e["from"] == n["id"] or e["to"] == n["id"])
                for e in result["edges"]
            )
            self.assertTrue(
                has_calls_edge,
                f"Depth-2 node {n['id']} should have a calls edge, not import-only",
            )


class TestSemanticSearch(unittest.TestCase):
    """Tests for --semantic vector-based similarity search (graceful degradation)."""

    def test_detect_semantic_backend_returns_none(self) -> None:
        """When deps are missing, _detect_semantic_backend returns None."""
        # In a normal test environment, sqlite-vec is not installed
        result = _detect_semantic_backend()
        # Result is either None (most likely) or a string if deps happen to be present
        self.assertIn(result, (None, "model2vec", "onnx-minilm"))

    def test_build_semantic_edges_no_backend(self) -> None:
        """When no backend is available, returns empty edges and disabled stats."""
        with patch("scripts.code_intel._detect_semantic_backend", return_value=None):
            edges, stats = _build_semantic_edges(
                nodes=[
                    {
                        "id": "a.py::foo",
                        "kind": "function",
                        "file": "a.py",
                        "line": 1,
                        "modified_in_diff": True,
                    },
                ],
                files=["a.py"],
                repo_root="/tmp",
                cache_dir=None,
            )
        self.assertEqual(edges, [])
        self.assertFalse(stats["enabled"])
        self.assertEqual(stats["reason"], "dependencies not available")

    def test_semantic_not_available_graceful(self) -> None:
        """When --semantic passed but deps missing, no crash, graph still returns structural data."""
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py], semantic=True, repo_root=str(FIXTURES_DIR))
        # Graph should still have structural data
        self.assertIn("nodes", result)
        self.assertIn("edges", result)
        self.assertIn("stats", result)
        self.assertTrue(len(result["nodes"]) > 0)
        # Import edges should still exist
        import_edges = [e for e in result["edges"] if e["type"] == "imports"]
        self.assertTrue(len(import_edges) > 0)
        # Semantic stats should show disabled
        if "semantic" in result["stats"]:
            self.assertFalse(result["stats"]["semantic"]["enabled"])

    def test_semantic_flag_accepted(self) -> None:
        """CLI accepts --semantic flag without error."""
        proc = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "graph",
                "--semantic",
                "--repo-root",
                str(FIXTURES_DIR),
            ],
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

    def test_semantic_without_flag_has_no_semantic_stats(self) -> None:
        """Without --semantic, stats should not contain a semantic key."""
        sample_py = str(FIXTURES_DIR / "sample.py")
        result = cmd_graph([sample_py], semantic=False, repo_root=str(FIXTURES_DIR))
        self.assertNotIn("semantic", result["stats"])

    def test_build_semantic_edges_too_few_functions(self) -> None:
        """With a backend but < 2 functions, returns empty edges with reason."""
        with patch(
            "scripts.code_intel._detect_semantic_backend", return_value="model2vec"
        ):
            edges, stats = _build_semantic_edges(
                nodes=[
                    {
                        "id": "a.py::foo",
                        "kind": "function",
                        "file": "/nonexistent/a.py",
                        "line": 1,
                        "modified_in_diff": True,
                    },
                ],
                files=["/nonexistent/a.py"],
                repo_root="/tmp",
                cache_dir=None,
            )
        self.assertEqual(edges, [])
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["symbols_indexed"], 0)
        self.assertEqual(stats["reason"], "too few functions")

    def test_build_semantic_edges_with_mocked_backend(self) -> None:
        """With mocked model2vec + numpy, produces semantic_similarity edges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two Python files with functions
            file_a = os.path.join(tmpdir, "auth.py")
            Path(file_a).write_text(
                "def validate_token(token):\n"
                "    if not token:\n"
                "        return False\n"
                "    return True\n"
            )
            file_b = os.path.join(tmpdir, "checker.py")
            Path(file_b).write_text(
                "def check_credentials(creds):\n"
                "    if not creds:\n"
                "        return False\n"
                "    return True\n"
            )

            nodes = [
                {
                    "id": f"{file_a}::validate_token",
                    "kind": "function",
                    "file": file_a,
                    "line": 1,
                    "modified_in_diff": True,
                },
                {
                    "id": f"{file_b}::check_credentials",
                    "kind": "function",
                    "file": file_b,
                    "line": 1,
                    "modified_in_diff": False,
                },
            ]

            # Mock the backend and model2vec
            import types

            mock_model2vec = types.ModuleType("model2vec")

            class MockStaticModel:
                @staticmethod
                def from_pretrained(name: str) -> "MockStaticModel":
                    return MockStaticModel()

                def encode(self, texts: list[str]) -> list[list[float]]:
                    # Return simple vectors that have high cosine similarity
                    import math

                    result = []
                    for i, _ in enumerate(texts):
                        # All vectors point roughly the same direction
                        vec = [1.0] * 8
                        vec[i % 8] += 0.1  # slight variation
                        # Normalize
                        norm = math.sqrt(sum(v * v for v in vec))
                        result.append([v / norm for v in vec])
                    return result

            mock_model2vec.StaticModel = MockStaticModel  # type: ignore[attr-defined]

            with (
                patch(
                    "scripts.code_intel._detect_semantic_backend",
                    return_value="model2vec",
                ),
                patch.dict("sys.modules", {"model2vec": mock_model2vec}),
            ):
                edges, stats = _build_semantic_edges(
                    nodes,
                    [file_a, file_b],
                    tmpdir,
                    None,
                )

            self.assertTrue(stats["enabled"])
            self.assertEqual(stats["model"], "model2vec")
            self.assertEqual(stats["symbols_indexed"], 2)
            self.assertIn("index_time_ms", stats)
            # Should find at least one semantic edge (vectors are similar)
            self.assertTrue(
                len(edges) > 0,
                f"Expected semantic edges with similar vectors, got: {edges}",
            )
            for edge in edges:
                self.assertEqual(edge["type"], "semantic_similarity")
                self.assertIn("score", edge)
                self.assertGreater(edge["score"], 0.5)


if __name__ == "__main__":
    unittest.main()
