import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "skills" / "codereview")
)

from scripts.orchestrate import (  # noqa: E402
    DEFAULT_CONFIG,
    DiffResult,
    PromptContext,
    assemble_expert_panel,
    check_token_budget,
    detect_repo_root,
    extract_diff,
    filter_config_allowlist,
    load_config,
    prepare,
    select_mode,
)

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


class OrchestratePrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        # Clear GIT_DIR/GIT_WORK_TREE so temp repo tests use their own git context
        self._saved_git_env = {}
        for key in ("GIT_DIR", "GIT_WORK_TREE"):
            if key in os.environ:
                self._saved_git_env[key] = os.environ.pop(key)

    def tearDown(self) -> None:
        # Clean up format-diff temp dir if it was created
        if hasattr(self, "_fmt_tmpdir"):
            self._fmt_tmpdir.cleanup()
        # First pop keys that may have been set during the test
        for key in ("GIT_DIR", "GIT_WORK_TREE"):
            os.environ.pop(key, None)
        # Then restore original values
        os.environ.update(self._saved_git_env)

    def test_extract_diff_base_mode_returns_changed_files_and_diff_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo(Path(tmpdir))
            self._run(["git", "checkout", "-b", "feature"], cwd=repo)
            (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
            self._run(["git", "commit", "-am", "update"], cwd=repo)

            result = extract_diff(repo_root=repo, mode="base", base_ref="main")

        self.assertIsInstance(result, DiffResult)
        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_extract_diff_empty_base_mode_returns_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo(Path(tmpdir))
            result = extract_diff(repo_root=repo, mode="base", base_ref="main")

        self.assertEqual(result.changed_files, [])
        self.assertEqual(result.diff_text, "")

    def test_select_mode_returns_chunked_on_large_diff(self) -> None:
        with mock.patch("scripts.orchestrate.progress") as progress:
            mode = select_mode(file_count=81, diff_line_count=10)

        self.assertEqual(mode, "chunked")
        progress.assert_not_called()

    def test_assemble_expert_panel_core_and_shell_activation(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["scripts/run.sh"],
            diff_text="@@\n+#!/usr/bin/env bash\n+set -euo pipefail\n",
        )

        panel = assemble_expert_panel(diff_result, config={}, spec_content=None)

        self.assertEqual(
            [expert["name"] for expert in panel[:3]],
            ["correctness", "security-config", "test-adequacy"],
        )
        self.assertIn("shell-script", [expert["name"] for expert in panel])

    def test_assemble_expert_panel_force_all_and_filters(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )

        panel = assemble_expert_panel(
            diff_result,
            config={
                "expert_panel": {"force_all": True},
                "passes": [
                    "correctness",
                    "security-config",
                    "test-adequacy",
                    "shell-script",
                ],
            },
            spec_content="spec",
        )

        self.assertEqual(
            [expert["name"] for expert in panel],
            ["correctness", "security-config", "test-adequacy", "shell-script"],
        )

    def test_prompt_context_render_and_budget_truncation(self) -> None:
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="diff " * 200,
            changed_files="tracked.txt",
            complexity="complexity",
            git_risk="risk " * 200,
            scan_results="scan " * 200,
            callers="callers",
            language_standards="standards " * 200,
            review_instructions="instructions",
            spec="spec",
        )

        rendered = context.render()
        truncated = check_token_budget(context, "correctness", prompt_budget_tokens=120)

        self.assertIn("## Global Contract", rendered)
        self.assertIn("## Your Focus", rendered)
        self.assertIn("scan summary:", truncated)
        self.assertIn("risk summary:", truncated)
        self.assertNotIn("standards standards", truncated)

    def test_load_config_defaults_file_and_cli_override(self) -> None:
        config_path = TESTS_DIR / "fixtures" / "orchestrate" / "codereview.yaml"
        fake_yaml = mock.Mock()
        fake_yaml.safe_load.return_value = {
            "cadence": "wave-end",
            "pushback_level": "selective",
            "confidence_floor": 0.65,
            "ignore_paths": ["vendor/"],
        }
        with mock.patch("scripts.orchestrate.yaml", fake_yaml):
            loaded = load_config(config_path)
        overridden = load_config(config_path, no_config=True)

        self.assertEqual(loaded["cadence"], "wave-end")
        self.assertEqual(loaded["pushback_level"], "selective")
        self.assertEqual(loaded["confidence_floor"], 0.65)
        self.assertEqual(loaded["large_diff"], DEFAULT_CONFIG["large_diff"])
        self.assertEqual(overridden, DEFAULT_CONFIG)

    def test_filter_config_allowlist_keeps_only_expected_keys(self) -> None:
        filtered = filter_config_allowlist(
            {"cadence": "wave-end", "ignore_paths": ["vendor/"], "extra": True},
            {"cadence", "ignore_paths"},
        )

        self.assertEqual(filtered, {"cadence": "wave-end", "ignore_paths": ["vendor/"]})

    def test_detect_repo_root_finds_temp_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo(Path(tmpdir))
            nested = repo / "a" / "b"
            nested.mkdir(parents=True)

            self.assertEqual(detect_repo_root(nested), repo)

    def test_prepare_writes_launch_packet_and_session_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            stale_prompt = session_dir / "explorer-correctness-prompt.md"
            stale_prompt.parent.mkdir(parents=True, exist_ok=True)
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            stale_prompt.write_text("stale", encoding="utf-8")
            (session_dir / "launch.json").write_text("stale", encoding="utf-8")

            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )
            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=["tracked.txt"],
                diff_text="@@\n+two\n",
            )

            def fake_run(
                command: list[str],
                cwd: Path | None = None,
                timeout: float | None = None,
                input_text: str | None = None,
            ) -> dict[str, object]:
                script_name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                # Disambiguate code_intel.py subcommands
                if script_name == "code_intel.py":
                    sub = command[-1] if len(command) > 2 else "complexity"
                    script_name = f"code_intel.py:{sub}"
                mapping: dict[str, dict[str, object]] = {
                    "discover-project.py": {"language": "python"},
                    "code_intel.py:complexity": {
                        "hotspots": [{"file": "tracked.txt", "score": "C"}],
                        "analyzer": "regex-only",
                        "tool_status": {},
                    },
                    "code_intel.py:functions": {
                        "functions": [
                            {
                                "file": "tracked.txt",
                                "name": "main",
                                "params": [],
                                "returns": "",
                                "line_start": 1,
                                "line_end": 5,
                                "exported": True,
                            }
                        ]
                    },
                    "git-risk.sh": {"tiers": [{"file": "tracked.txt", "tier": "high"}]},
                    "run-scans.sh": {"findings": [{"tool": "semgrep"}]},
                    "coverage-collect.py": {
                        "coverage": [{"file": "tracked.txt", "lines": 75}]
                    },
                }
                return mapping[script_name]

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_run
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_text",
                    return_value="formatted diff output",
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertTrue((session_dir / "diff.patch").exists())
            self.assertTrue((session_dir / "changed-files.txt").exists())
            self.assertTrue((session_dir / "launch.json").exists())
            self.assertTrue((session_dir / ".codereview-session").exists())

            launch = json.loads(
                (session_dir / "launch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(launch["session_dir"], str(session_dir))
            self.assertEqual(launch["diff_result"]["changed_files"], ["tracked.txt"])
            self.assertTrue(launch["waves"])
            self.assertTrue(launch["judge"]["output_file"].endswith("judge.json"))
            self.assertNotEqual(stale_prompt.read_text(encoding="utf-8"), "stale")

    def test_prepare_passes_changed_files_and_base_ref_to_context_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )
            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=["tracked.txt"],
                diff_text="@@\n+two\n",
            )
            invocations: dict[str, dict[str, object]] = {}

            def fake_run(
                command: list[str],
                cwd: Path | None = None,
                timeout: float | None = None,
                input_text: str | None = None,
            ) -> dict[str, object]:
                script_name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                # Disambiguate code_intel.py subcommands
                if script_name == "code_intel.py":
                    sub = command[-1] if len(command) > 2 else "complexity"
                    script_name = f"code_intel.py:{sub}"
                invocations[script_name] = {
                    "command": command,
                    "cwd": cwd,
                    "timeout": timeout,
                    "input_text": input_text,
                }
                mapping: dict[str, dict[str, object]] = {
                    "discover-project.py": {"language": "python"},
                    "code_intel.py:complexity": {
                        "hotspots": [],
                        "analyzer": "regex-only",
                        "tool_status": {},
                    },
                    "code_intel.py:functions": {"functions": []},
                    "git-risk.sh": {"tiers": []},
                    "run-scans.sh": {"findings": [], "tool_status": {}},
                    "coverage-collect.py": {"coverage": []},
                }
                return mapping[script_name]

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_run
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_text",
                    return_value="formatted diff output",
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertEqual(
                invocations["code_intel.py:complexity"]["input_text"], "tracked.txt\n"
            )
            self.assertEqual(invocations["git-risk.sh"]["input_text"], "tracked.txt\n")
            self.assertEqual(
                invocations["coverage-collect.py"]["input_text"], "tracked.txt\n"
            )
            self.assertEqual(invocations["run-scans.sh"]["input_text"], "tracked.txt\n")
            self.assertEqual(
                invocations["run-scans.sh"]["command"],
                [
                    "bash",
                    str(
                        REPO_ROOT / "skills" / "codereview" / "scripts" / "run-scans.sh"
                    ),
                    "--base-ref",
                    "main",
                ],
            )

    def test_prepare_empty_diff_writes_empty_launch_packet(self) -> None:
        """prepare() returns 0 and writes status='empty' when diff is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )
            empty_diff = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=[],
                diff_text="",
            )

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
                mock.patch("scripts.orchestrate.extract_diff", return_value=empty_diff),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertTrue((session_dir / "launch.json").exists())
            launch = json.loads(
                (session_dir / "launch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(launch["status"], "empty")
            self.assertEqual(launch["message"], "No changes found to review")

    def test_extract_diff_worktree_mode_with_staged_files(self) -> None:
        """extract_diff in worktree mode returns staged files when they exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo(Path(tmpdir))
            # Modify and stage a file (but don't commit)
            (repo / "tracked.txt").write_text("one\nstaged change\n", encoding="utf-8")
            self._run(["git", "add", "tracked.txt"], cwd=repo)

            result = extract_diff(repo_root=repo, mode="worktree")

        self.assertIsInstance(result, DiffResult)
        self.assertIn("tracked.txt", result.changed_files)
        self.assertIn("+staged change", result.diff_text)
        self.assertEqual(result.mode, "worktree")

    def test_prepare_refuses_non_session_directory_with_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / "user-file.txt").write_text("keep", encoding="utf-8")
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )

            result = prepare(args)

            self.assertEqual(result, 1)
            self.assertTrue((session_dir / "user-file.txt").exists())

    # -- format-diff wiring tests --

    def _prepare_with_format_diff(
        self,
        *,
        format_diff_return: str = "## File: tracked.txt\nformatted before/after",
        format_diff_side_effect: Exception | None = None,
        diff_text: str = "@@\n+two\n",
        config_overrides: dict | None = None,
    ) -> tuple[int, Path, mock.MagicMock]:
        """Helper: run prepare() with configurable format-diff behavior.

        Returns (exit_code, session_dir, run_subprocess_text_mock).
        """
        self._fmt_tmpdir = tempfile.TemporaryDirectory()
        tmpdir = self._fmt_tmpdir.name
        session_dir = Path(tmpdir) / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

        config = DEFAULT_CONFIG.copy()
        if config_overrides:
            config.update(config_overrides)

        args = Namespace(
            session_dir=session_dir,
            no_config=True,
            spec=None,
            spec_scope=None,
            base="main",
            mode="base",
        )
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["tracked.txt"],
            diff_text=diff_text,
        )

        def fake_json_run(
            command: list[str],
            cwd: Path | None = None,
            timeout: float | None = None,
            input_text: str | None = None,
        ) -> dict[str, object]:
            script_name = next(
                Path(part).name for part in command if part.endswith((".py", ".sh"))
            )
            if script_name == "code_intel.py":
                sub = command[-1] if len(command) > 2 else "complexity"
                script_name = f"code_intel.py:{sub}"
            mapping: dict[str, dict[str, object]] = {
                "discover-project.py": {"language": "python"},
                "code_intel.py:complexity": {
                    "hotspots": [],
                    "analyzer": "regex-only",
                    "tool_status": {},
                },
                "code_intel.py:functions": {"functions": []},
                "code_intel.py:graph": {},
                "git-risk.sh": {"tiers": []},
                "run-scans.sh": {"findings": [], "tool_status": {}},
                "coverage-collect.py": {"coverage": []},
                "prescan.py": {},
            }
            return mapping.get(script_name, {})

        text_mock = mock.MagicMock()
        if format_diff_side_effect is not None:
            text_mock.side_effect = format_diff_side_effect
        else:
            text_mock.return_value = format_diff_return

        with (
            mock.patch("scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT),
            mock.patch("scripts.orchestrate.extract_diff", return_value=diff_result),
            mock.patch(
                "scripts.orchestrate.run_subprocess_json", side_effect=fake_json_run
            ),
            mock.patch("scripts.orchestrate.run_subprocess_text", text_mock),
        ):
            result = prepare(args)

        return result, session_dir, text_mock

    def test_prepare_calls_format_diff_and_uses_formatted_output(self) -> None:
        """prepare() calls format-diff and passes result to explorer prompts."""
        formatted_text = "## File: tracked.txt\n```\nbefore -> after\n```"
        result, session_dir, text_mock = self._prepare_with_format_diff(
            format_diff_return=formatted_text,
        )

        self.assertEqual(result, 0)

        # Verify format-diff was called
        text_mock.assert_called()
        format_diff_calls = [
            c
            for c in text_mock.call_args_list
            if any("format-diff" in str(a) for a in c.args)
        ]
        self.assertTrue(
            len(format_diff_calls) >= 1,
            "format-diff should have been called at least once",
        )

        # Verify the first call passes raw diff as stdin
        first_call = format_diff_calls[0]
        self.assertEqual(first_call.kwargs.get("input_text"), "@@\n+two\n")

        # Verify diff-formatted.patch was written
        self.assertTrue((session_dir / "diff-formatted.patch").exists())
        saved = (session_dir / "diff-formatted.patch").read_text(encoding="utf-8")
        self.assertEqual(saved, formatted_text)

        # Verify explorer prompts contain formatted diff, not raw
        prompt_files = list(session_dir.glob("explorer-*-prompt.md"))
        self.assertTrue(prompt_files, "Should have explorer prompt files")
        for pf in prompt_files:
            content = pf.read_text(encoding="utf-8")
            self.assertIn("before -> after", content)

    def test_prepare_format_diff_failure_falls_back_to_raw_diff(self) -> None:
        """When format-diff fails, prepare() uses raw diff for explorers."""
        result, session_dir, text_mock = self._prepare_with_format_diff(
            format_diff_side_effect=RuntimeError("format-diff crashed"),
        )

        self.assertEqual(result, 0)

        # diff-formatted.patch should NOT exist (format-diff failed)
        self.assertFalse((session_dir / "diff-formatted.patch").exists())

        # Explorer prompts should contain the raw diff
        prompt_files = list(session_dir.glob("explorer-*-prompt.md"))
        self.assertTrue(prompt_files)
        for pf in prompt_files:
            content = pf.read_text(encoding="utf-8")
            self.assertIn("+two", content)

    def test_prepare_format_diff_budget_expansion(self) -> None:
        """When formatted diff < 50% of budget, prepare() re-runs with --expand-context."""
        short_formatted = "short formatted"
        expanded_formatted = "expanded formatted with more context lines"

        call_count = {"n": 0}

        def format_diff_responses(command, cwd=None, timeout=None, input_text=None):
            call_count["n"] += 1
            if "--expand-context" in command:
                return expanded_formatted
            return short_formatted

        text_mock = mock.MagicMock(side_effect=format_diff_responses)

        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )
            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=["tracked.txt"],
                diff_text="@@\n+two\n",
            )

            def fake_json_run(command, cwd=None, timeout=None, input_text=None):
                script_name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                if script_name == "code_intel.py":
                    sub = command[-1] if len(command) > 2 else "complexity"
                    script_name = f"code_intel.py:{sub}"
                mapping = {
                    "discover-project.py": {"language": "python"},
                    "code_intel.py:complexity": {
                        "hotspots": [],
                        "analyzer": "regex-only",
                        "tool_status": {},
                    },
                    "code_intel.py:functions": {"functions": []},
                    "code_intel.py:graph": {},
                    "git-risk.sh": {"tiers": []},
                    "run-scans.sh": {"findings": [], "tool_status": {}},
                    "coverage-collect.py": {"coverage": []},
                    "prescan.py": {},
                }
                return mapping.get(script_name, {})

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_json_run
                ),
                mock.patch("scripts.orchestrate.run_subprocess_text", text_mock),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)

            # Should have called format-diff twice: once plain, once with --expand-context
            format_diff_calls = [
                c
                for c in text_mock.call_args_list
                if any("format-diff" in str(a) for a in c.args)
            ]
            self.assertEqual(
                len(format_diff_calls), 2, "Expected plain + expanded calls"
            )

            # Second call should include --expand-context flag
            second_cmd = format_diff_calls[1].args[0]
            self.assertIn("--expand-context", second_cmd)

            # Final diff-formatted.patch should contain expanded output
            saved = (session_dir / "diff-formatted.patch").read_text(encoding="utf-8")
            self.assertEqual(saved, expanded_formatted)

    def test_prepare_format_diff_no_expansion_when_large(self) -> None:
        """When formatted diff >= 50% of budget, no expansion call is made."""
        # Default budget is 70_000 tokens * 4 chars = 280_000 chars.
        # 50% threshold = 140_000 chars. Make formatted output large enough.
        large_formatted = "x" * 150_000

        result, session_dir, text_mock = self._prepare_with_format_diff(
            format_diff_return=large_formatted,
        )

        self.assertEqual(result, 0)

        # Should have called format-diff only once (no --expand-context)
        format_diff_calls = [
            c
            for c in text_mock.call_args_list
            if any("format-diff" in str(a) for a in c.args)
        ]
        self.assertEqual(len(format_diff_calls), 1, "Should not expand large diffs")
        self.assertNotIn("--expand-context", format_diff_calls[0].args[0])

    def test_prepare_expand_context_failure_emits_progress(self) -> None:
        """When --expand-context format-diff fails, a progress event is emitted."""
        short_formatted = "short formatted"
        call_count = {"n": 0}

        def format_diff_responses(command, cwd=None, timeout=None, input_text=None):
            call_count["n"] += 1
            if "--expand-context" in command:
                raise RuntimeError("expand failed")
            return short_formatted

        text_mock = mock.MagicMock(side_effect=format_diff_responses)

        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
            )
            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=["tracked.txt"],
                diff_text="@@\n+two\n",
            )

            def fake_json_run(command, cwd=None, timeout=None, input_text=None):
                script_name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                if script_name == "code_intel.py":
                    sub = command[-1] if len(command) > 2 else "complexity"
                    script_name = f"code_intel.py:{sub}"
                mapping = {
                    "discover-project.py": {"language": "python"},
                    "code_intel.py:complexity": {
                        "hotspots": [],
                        "analyzer": "regex-only",
                        "tool_status": {},
                    },
                    "code_intel.py:functions": {"functions": []},
                    "code_intel.py:graph": {},
                    "git-risk.sh": {"tiers": []},
                    "run-scans.sh": {"findings": [], "tool_status": {}},
                    "coverage-collect.py": {"coverage": []},
                    "prescan.py": {},
                }
                return mapping.get(script_name, {})

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_json_run
                ),
                mock.patch("scripts.orchestrate.run_subprocess_text", text_mock),
                mock.patch("scripts.orchestrate.progress") as mock_progress,
            ):
                result = prepare(args)

            self.assertEqual(result, 0)

            # Verify the expand failure was reported via progress
            progress_events = [c.args[0] for c in mock_progress.call_args_list]
            self.assertIn("format_diff_expand_failed", progress_events)

            # The non-expanded formatted diff should still be used
            saved = (session_dir / "diff-formatted.patch").read_text(encoding="utf-8")
            self.assertEqual(saved, short_formatted)

    def test_prepare_raw_diff_preserved_for_scans(self) -> None:
        """Raw diff is saved to diff.patch even when format-diff succeeds."""
        formatted_text = "## File: tracked.txt\nformatted"
        result, session_dir, _ = self._prepare_with_format_diff(
            format_diff_return=formatted_text,
        )

        self.assertEqual(result, 0)

        # diff.patch should contain the raw diff (not formatted)
        raw_saved = (session_dir / "diff.patch").read_text(encoding="utf-8")
        self.assertEqual(raw_saved, "@@\n+two\n")

        # diff-formatted.patch should contain the formatted version
        fmt_saved = (session_dir / "diff-formatted.patch").read_text(encoding="utf-8")
        self.assertEqual(fmt_saved, formatted_text)

    def _init_repo(self, repo: Path) -> Path:
        self._run(["git", "init", "-b", "main"], cwd=repo)
        self._run(["git", "config", "user.name", "Test User"], cwd=repo)
        self._run(["git", "config", "user.email", "test@example.com"], cwd=repo)
        (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        self._run(["git", "add", "tracked.txt"], cwd=repo)
        self._run(["git", "commit", "-m", "initial"], cwd=repo)
        return repo

    @staticmethod
    def _run(command: list[str], cwd: Path) -> None:
        env = {
            k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")
        }
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )


if __name__ == "__main__":
    unittest.main()
