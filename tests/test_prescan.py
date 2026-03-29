"""Tests for scripts/prescan.py -- prescan signal detection module."""

import json
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

# Ensure the skill package root is importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "skills" / "codereview")
)

from scripts.prescan import (  # noqa: E402
    CommentedCodeChecker,
    DeadCodeChecker,
    LongFunctionChecker,
    SecretChecker,
    StubChecker,
    SwallowedErrorChecker,
    TodoChecker,
    UnwiredChecker,
    _detect_language,
    _detect_language_fallback,
    _extract_function_ranges,
    _is_test_file,
    _read_file_safe,
    _read_file_safe_fallback,
    _should_skip,
    format_prescan_context,
    run_prescan,
    truncate_prescan_critical_only,
)
import scripts.prescan as prescan_module  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "prescan"
REPO_ROOT = TESTS_DIR.parent
SCRIPT = str(REPO_ROOT / "scripts" / "prescan.py")


# ---------------------------------------------------------------------------
# Checker tests
# ---------------------------------------------------------------------------


class TestSecretChecker(unittest.TestCase):
    checker = SecretChecker()

    def test_detects_hardcoded_password(self) -> None:
        # Read the fixture content but pass a non-test path so P-SEC fires.
        content = _read_file_safe(str(FIXTURES_DIR / "secrets.py"))
        self.assertIsNotNone(content)
        findings = self.checker.check("src/config/secrets.py", content, "python")  # type: ignore[arg-type]
        # Should find DB_PASSWORD, API_KEY, token
        self.assertGreaterEqual(len(findings), 3)
        patterns_found = {f["pattern_id"] for f in findings}
        self.assertEqual(patterns_found, {"P-SEC"})

    def test_skips_test_files(self) -> None:
        content = _read_file_safe(str(FIXTURES_DIR / "secrets.py"))
        self.assertIsNotNone(content)
        # When the file path looks like a test, P-SEC should skip it.
        findings = self.checker.check("tests/test_auth.py", content, "python")  # type: ignore[arg-type]
        self.assertEqual(len(findings), 0, "P-SEC should skip test files")


class TestSwallowedErrorChecker(unittest.TestCase):
    checker = SwallowedErrorChecker()

    def test_detects_except_pass(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        self.assertGreaterEqual(len(findings), 2, "Should find except:pass patterns")
        for f in findings:
            self.assertEqual(f["pattern_id"], "P-ERR")

    def test_detects_go_err_discard(self) -> None:
        fpath = str(FIXTURES_DIR / "errors.go")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "go")  # type: ignore[arg-type]
        # Should find _ = err
        err_discards = [
            f
            for f in findings
            if "err" in f.get("evidence", "").lower()
            or "_ = err" in f.get("evidence", "")
        ]
        self.assertGreaterEqual(len(err_discards), 1)

    def test_detects_empty_catch(self) -> None:
        content = "try { riskyOp(); } catch(e) { }"
        findings = self.checker.check("app.js", content, "javascript")
        self.assertGreaterEqual(len(findings), 1)


class TestLongFunctionChecker(unittest.TestCase):
    checker = LongFunctionChecker()

    def test_flags_long_function(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        long_fns = [f for f in findings if "long_function" in f["description"]]
        self.assertEqual(len(long_fns), 1, "Should flag long_function")

    def test_does_not_flag_short_function(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        short_fns = [f for f in findings if "short_function" in f["description"]]
        self.assertEqual(len(short_fns), 0, "Should not flag short_function")

    def test_no_language_returns_empty(self) -> None:
        findings = self.checker.check("readme.md", "# Hello", None)
        self.assertEqual(findings, [])


class TestTodoChecker(unittest.TestCase):
    checker = TodoChecker()

    def test_finds_todo_fixme(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        markers = {f["description"] for f in findings}
        self.assertTrue(any("TODO" in m for m in markers))
        self.assertTrue(any("FIXME" in m for m in markers))
        self.assertTrue(any("XXX" in m for m in markers))

    def test_finds_todo_in_go(self) -> None:
        fpath = str(FIXTURES_DIR / "errors.go")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "go")  # type: ignore[arg-type]
        self.assertGreaterEqual(len(findings), 1)


class TestCommentedCodeChecker(unittest.TestCase):
    checker = CommentedCodeChecker()

    def test_detects_commented_code(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        # Should detect "# def old_function():" and "# for item in collection:"
        self.assertGreaterEqual(len(findings), 2)
        for f in findings:
            self.assertEqual(f["pattern_id"], "P-COMMENT")


class TestDeadCodeChecker(unittest.TestCase):
    checker = DeadCodeChecker()

    def test_detects_uncalled_function(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        dead_names = [f["description"] for f in findings]
        # unused_helper is never called from within the file
        self.assertTrue(
            any("unused_helper" in d for d in dead_names),
            f"unused_helper should be flagged as dead code. Got: {dead_names}",
        )

    def test_does_not_flag_called_function(self) -> None:
        fpath = str(FIXTURES_DIR / "swallowed_errors.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        dead_names = " ".join(f["description"] for f in findings)
        # short_function is called by caller()
        self.assertNotIn("short_function", dead_names)


class TestStubChecker(unittest.TestCase):
    checker = StubChecker()

    def test_detects_pass_only_stub(self) -> None:
        fpath = str(FIXTURES_DIR / "stubs.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        stub_names = [f["description"] for f in findings]
        self.assertTrue(any("stub_function" in d for d in stub_names))

    def test_detects_not_implemented_stub(self) -> None:
        fpath = str(FIXTURES_DIR / "stubs.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        stub_names = [f["description"] for f in findings]
        self.assertTrue(any("todo_stub" in d for d in stub_names))

    def test_does_not_flag_implemented_function(self) -> None:
        fpath = str(FIXTURES_DIR / "stubs.py")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "python")  # type: ignore[arg-type]
        stub_names = " ".join(f["description"] for f in findings)
        self.assertNotIn("implemented", stub_names)

    def test_detects_rust_stubs(self) -> None:
        fpath = str(FIXTURES_DIR / "stubs.rs")
        content = _read_file_safe(fpath)
        self.assertIsNotNone(content)
        findings = self.checker.check(fpath, content, "rust")  # type: ignore[arg-type]
        stub_names = " ".join(f["description"] for f in findings)
        self.assertIn("stub_fn", stub_names)
        self.assertIn("another_stub", stub_names)
        self.assertNotIn("implemented_fn", stub_names)


class TestUnwiredChecker(unittest.TestCase):
    def test_returns_empty_without_import_index(self) -> None:
        checker = UnwiredChecker()
        findings = checker.check("any.py", "def foo(): pass", "python")
        self.assertEqual(
            findings, [], "P-UNWIRED is skipped when import index not built"
        )


# ---------------------------------------------------------------------------
# Integration / full scan tests
# ---------------------------------------------------------------------------


class TestRunPrescan(unittest.TestCase):
    def test_empty_file_list(self) -> None:
        result = run_prescan([])
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["analyzer"], "regex-only")
        self.assertEqual(
            result["summary"], {"critical": 0, "high": 0, "medium": 0, "low": 0}
        )

    def test_full_scan_on_fixtures(self) -> None:
        files = [
            str(FIXTURES_DIR / "swallowed_errors.py"),
            str(FIXTURES_DIR / "stubs.py"),
            str(FIXTURES_DIR / "errors.go"),
            str(FIXTURES_DIR / "stubs.rs"),
        ]
        result = run_prescan(files)
        self.assertEqual(result["file_count"], 4)
        self.assertEqual(result["analyzer"], "regex-only")
        self.assertIn("python", result["languages_detected"])
        self.assertIn("go", result["languages_detected"])
        self.assertIn("rust", result["languages_detected"])

        # Should have findings in multiple categories
        patterns = result["patterns"]
        self.assertIn("swallowed_errors", patterns)
        self.assertIn("stub_placeholder", patterns)
        self.assertIn("todo_fixme", patterns)

        # Fixtures are inside tests/ so P-SEC skips them (correct behavior).
        # High count from swallowed errors + stubs
        self.assertGreater(result["summary"]["high"], 0)
        # Medium count from long functions + dead code
        self.assertGreater(result["summary"]["medium"], 0)
        # Low count from TODOs + commented code
        self.assertGreater(result["summary"]["low"], 0)

    def test_skips_pycache_dirs(self) -> None:
        result = run_prescan(["__pycache__/cached.pyc", "node_modules/lib.js"])
        self.assertEqual(result["file_count"], 0)

    def test_implementation_completeness(self) -> None:
        files = [
            str(FIXTURES_DIR / "stubs.py"),
            str(FIXTURES_DIR / "swallowed_errors.py"),
        ]
        result = run_prescan(files)
        completeness = result["implementation_completeness"]
        self.assertGreater(completeness["files_assessed"], 0)
        levels = completeness["levels"]
        # stubs.py has stub functions, so should not be L4
        all_l4 = levels["L4_functional"]
        stubs_in_l4 = [f for f in all_l4 if "stubs.py" in f]
        self.assertEqual(len(stubs_in_l4), 0, "stubs.py should not be L4_functional")


class TestCLI(unittest.TestCase):
    def test_cli_produces_valid_json(self) -> None:
        files_input = "\n".join(
            [
                str(FIXTURES_DIR / "swallowed_errors.py"),
                str(FIXTURES_DIR / "stubs.py"),
            ]
        )
        proc = subprocess.run(
            [sys.executable, SCRIPT],
            input=files_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        result = json.loads(proc.stdout)
        self.assertIn("file_count", result)
        self.assertIn("patterns", result)
        self.assertIn("summary", result)


# ---------------------------------------------------------------------------
# Context formatting / truncation tests
# ---------------------------------------------------------------------------


class TestFormatPrescanContext(unittest.TestCase):
    def test_empty_prescan(self) -> None:
        self.assertEqual(format_prescan_context({}), "")
        self.assertEqual(format_prescan_context({"file_count": 0}), "")

    def test_formats_findings(self) -> None:
        files = [
            str(FIXTURES_DIR / "swallowed_errors.py"),
            str(FIXTURES_DIR / "stubs.py"),
        ]
        result = run_prescan(files)
        text = format_prescan_context(result)
        self.assertIn("Prescan Signals", text)
        # Should contain HIGH from swallowed errors/stubs
        self.assertIn("HIGH:", text)


class TestTruncatePrescanCriticalOnly(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(truncate_prescan_critical_only(""), "")

    def test_keeps_critical_and_high(self) -> None:
        text = (
            "## Prescan Signals (fast static checks, regex-only mode)\n"
            "\n"
            "CRITICAL: 1 signal(s)\n"
            "- src/config.py:17 -- P-SEC: Possible hardcoded credential\n"
            "\n"
            "HIGH: 2 signal(s)\n"
            "- src/api.py:45 -- P-ERR: Swallowed error\n"
            "- src/api.py:88 -- P-STUB: Stub function\n"
            "\n"
            "MEDIUM: 3 signal(s)\n"
            "- src/util.py:10 -- P-LEN: Long function\n"
            "\n"
            "LOW: 5 signal(s)\n"
            "- src/main.py:1 -- P-TODO: TODO marker\n"
        )
        result = truncate_prescan_critical_only(text)
        self.assertIn("CRITICAL:", result)
        self.assertIn("P-SEC", result)
        self.assertIn("HIGH:", result)
        self.assertIn("P-ERR", result)
        self.assertNotIn("MEDIUM:", result)
        self.assertNotIn("LOW:", result)
        self.assertNotIn("P-LEN", result)
        self.assertNotIn("P-TODO", result)


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_detect_language(self) -> None:
        self.assertEqual(_detect_language("app.py"), "python")
        self.assertEqual(_detect_language("main.go"), "go")
        self.assertEqual(_detect_language("index.ts"), "typescript")
        self.assertEqual(_detect_language("App.java"), "java")
        self.assertEqual(_detect_language("lib.rs"), "rust")
        self.assertIsNone(_detect_language("readme.md"))

    def test_is_test_file(self) -> None:
        self.assertTrue(_is_test_file("tests/test_auth.py"))
        self.assertTrue(_is_test_file("test_auth.py"))
        self.assertTrue(_is_test_file("src/__tests__/auth.spec.js"))
        self.assertFalse(_is_test_file("src/auth.py"))

    def test_should_skip(self) -> None:
        self.assertTrue(_should_skip("__pycache__/mod.pyc"))
        self.assertTrue(_should_skip("node_modules/lib/index.js"))
        self.assertTrue(_should_skip(".git/config"))
        self.assertTrue(_should_skip("pkg/api.pb.go"))
        self.assertFalse(_should_skip("src/auth.py"))

    def test_extract_function_ranges_python(self) -> None:
        content = "def foo():\n    pass\n\ndef bar():\n    return 1\n"
        lines = content.splitlines()
        funcs = _extract_function_ranges(content, lines, "python")
        self.assertEqual(len(funcs), 2)
        self.assertEqual(funcs[0][0], "foo")
        self.assertEqual(funcs[1][0], "bar")


# ---------------------------------------------------------------------------
# code_intel import / fallback tests
# ---------------------------------------------------------------------------


class TestCodeIntelFallback(unittest.TestCase):
    """Test that prescan works in both modes: with and without code_intel."""

    def test_fallback_detect_language(self) -> None:
        """The local fallback _detect_language_fallback matches expected behavior."""
        self.assertEqual(_detect_language_fallback("app.py"), "python")
        self.assertEqual(_detect_language_fallback("main.go"), "go")
        self.assertIsNone(_detect_language_fallback("readme.md"))

    def test_fallback_read_file_safe(self) -> None:
        """The local fallback _read_file_safe_fallback reads files correctly."""
        fpath = str(FIXTURES_DIR / "stubs.py")
        content = _read_file_safe_fallback(fpath)
        self.assertIsNotNone(content)
        self.assertIn("def", content)

    def test_detect_language_delegates_when_code_intel_available(self) -> None:
        """When _CODE_INTEL_AVAILABLE is True, _detect_language delegates."""
        sentinel = "mocked-lang"
        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_detect_language",
                return_value=sentinel,
                create=True,
            ),
        ):
            result = prescan_module._detect_language("anything.xyz")
        self.assertEqual(result, sentinel)

    def test_detect_language_uses_fallback_when_code_intel_unavailable(self) -> None:
        """When _CODE_INTEL_AVAILABLE is False, _detect_language uses local impl."""
        with mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", False):
            result = prescan_module._detect_language("app.py")
        self.assertEqual(result, "python")

    def test_read_file_safe_delegates_when_code_intel_available(self) -> None:
        """When _CODE_INTEL_AVAILABLE is True, _read_file_safe delegates."""
        sentinel = "mocked-content"
        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module, "_ci_read_file_safe", return_value=sentinel, create=True
            ),
        ):
            result = prescan_module._read_file_safe("anything.py")
        self.assertEqual(result, sentinel)

    def test_read_file_safe_uses_fallback_when_code_intel_unavailable(self) -> None:
        """When _CODE_INTEL_AVAILABLE is False, _read_file_safe uses local impl."""
        fpath = str(FIXTURES_DIR / "stubs.py")
        with mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", False):
            result = prescan_module._read_file_safe(fpath)
        self.assertIsNotNone(result)
        self.assertIn("def", result)

    def test_run_prescan_reports_analyzer_mode_regex_only(self) -> None:
        """When code_intel is unavailable, analyzer is 'regex-only'."""
        with mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", False):
            result = prescan_module.run_prescan([])
        self.assertEqual(result["analyzer"], "regex-only")

    def test_run_prescan_reports_analyzer_mode_code_intel(self) -> None:
        """When code_intel is available, analyzer is 'regex+code_intel'."""
        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_cmd_imports",
                return_value={"imports": []},
                create=True,
            ),
        ):
            result = prescan_module.run_prescan([])
        self.assertEqual(result["analyzer"], "regex+code_intel")


# ---------------------------------------------------------------------------
# P-UNWIRED checker tests (with mocked code_intel)
# ---------------------------------------------------------------------------


@dataclass
class _MockExportInfo:
    """Minimal stand-in for code_intel.ExportInfo used in UnwiredChecker tests."""

    file: str
    name: str
    kind: str
    line: int


class TestUnwiredCheckerWithCodeIntel(unittest.TestCase):
    """Test P-UNWIRED when code_intel is available (mocked)."""

    def _make_checker_active(
        self,
        import_index: dict[str, set[str]],
    ) -> UnwiredChecker:
        checker = UnwiredChecker()
        checker._import_index = import_index
        checker._active = True
        return checker

    def test_flags_export_with_no_importers(self) -> None:
        """An exported function with no importers produces a P-UNWIRED signal."""
        checker = self._make_checker_active({})
        export = _MockExportInfo("src/utils.py", "helper", "function", 10)

        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_extract_exports",
                return_value=[export],
                create=True,
            ),
        ):
            findings = checker.check(
                "src/utils.py", "def helper():\n    pass\n", "python"
            )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["pattern_id"], "P-UNWIRED")
        self.assertIn("helper", findings[0]["description"])
        self.assertIn("no importers", findings[0]["description"])

    def test_no_signal_when_imported(self) -> None:
        """An exported function that IS imported produces no signal."""
        import_index = {"helper": {"src/main.py"}}
        checker = self._make_checker_active(import_index)
        export = _MockExportInfo("src/utils.py", "helper", "function", 10)

        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_extract_exports",
                return_value=[export],
                create=True,
            ),
        ):
            findings = checker.check(
                "src/utils.py", "def helper():\n    pass\n", "python"
            )

        self.assertEqual(len(findings), 0, "Should not flag imported symbols")

    def test_self_import_excluded(self) -> None:
        """Import from the same file should not count as an importer."""
        import_index = {"helper": {"src/utils.py"}}  # only self
        checker = self._make_checker_active(import_index)
        export = _MockExportInfo("src/utils.py", "helper", "function", 10)

        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_extract_exports",
                return_value=[export],
                create=True,
            ),
        ):
            findings = checker.check(
                "src/utils.py", "def helper():\n    pass\n", "python"
            )

        self.assertEqual(len(findings), 1, "Self-imports should not count")

    def test_returns_empty_without_lang(self) -> None:
        """P-UNWIRED returns empty for files with no detected language."""
        checker = self._make_checker_active({})

        with mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True):
            findings = checker.check("readme.md", "# Hello", None)

        self.assertEqual(findings, [])

    def test_build_import_index(self) -> None:
        """build_import_index populates the internal index from cmd_imports."""
        fake_imports = {
            "imports": [
                {
                    "file": "src/main.py",
                    "module": "utils",
                    "names": ["helper", "Config"],
                    "line": 1,
                },
                {
                    "file": "src/app.py",
                    "module": "utils.db",
                    "names": ["connect"],
                    "line": 2,
                },
            ]
        }
        checker = UnwiredChecker()
        with (
            mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
            mock.patch.object(
                prescan_module,
                "_ci_cmd_imports",
                return_value=fake_imports,
                create=True,
            ),
        ):
            checker.build_import_index(["src/main.py", "src/app.py", "src/utils.py"])

        self.assertTrue(checker._active)
        self.assertIn("helper", checker._import_index)
        self.assertIn("src/main.py", checker._import_index["helper"])
        self.assertIn("Config", checker._import_index)
        self.assertIn("connect", checker._import_index)
        # Module stem "utils" should be indexed
        self.assertIn("utils", checker._import_index)
        # Module stem "db" (from "utils.db") should be indexed
        self.assertIn("db", checker._import_index)

    def test_build_import_index_noop_without_code_intel(self) -> None:
        """build_import_index is a no-op when code_intel is unavailable."""
        checker = UnwiredChecker()
        with mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", False):
            checker.build_import_index(["any.py"])
        self.assertFalse(checker._active)
        self.assertEqual(checker._import_index, {})

    def test_unwired_signals_in_run_prescan(self) -> None:
        """P-UNWIRED findings appear in run_prescan output when code_intel active."""
        export = _MockExportInfo("src/utils.py", "orphan_func", "function", 5)
        fake_imports: dict[str, Any] = {"imports": []}

        # Create a real temp file so _read_file_safe works
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir)
            fpath = os.path.join(src_dir, "utils.py")
            with open(fpath, "w") as f:
                f.write("def orphan_func():\n    return 42\n")

            with (
                mock.patch.object(prescan_module, "_CODE_INTEL_AVAILABLE", True),
                mock.patch.object(
                    prescan_module,
                    "_ci_detect_language",
                    side_effect=lambda fp: "python",
                    create=True,
                ),
                mock.patch.object(
                    prescan_module,
                    "_ci_read_file_safe",
                    side_effect=prescan_module._read_file_safe_fallback,
                    create=True,
                ),
                mock.patch.object(
                    prescan_module,
                    "_ci_cmd_imports",
                    return_value=fake_imports,
                    create=True,
                ),
                mock.patch.object(
                    prescan_module,
                    "_ci_extract_exports",
                    return_value=[export],
                    create=True,
                ),
            ):
                result = prescan_module.run_prescan([fpath])

        self.assertEqual(result["analyzer"], "regex+code_intel")
        unwired = result["patterns"].get("unwired_components", {})
        self.assertGreater(unwired.get("count", 0), 0, "Should have P-UNWIRED findings")
        self.assertEqual(unwired["findings"][0]["pattern_id"], "P-UNWIRED")
        self.assertIn("orphan_func", unwired["findings"][0]["description"])


if __name__ == "__main__":
    unittest.main()
