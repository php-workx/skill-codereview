import json
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


class OrchestratePrepareTests(unittest.TestCase):
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
        config_path = Path("tests/fixtures/orchestrate/codereview.yaml")
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
                name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                mapping: dict[str, dict[str, object]] = {
                    "discover-project.py": {"language": "python"},
                    "complexity.sh": {
                        "hotspots": [{"file": "tracked.txt", "score": "C"}]
                    },
                    "git-risk.sh": {"tiers": [{"file": "tracked.txt", "tier": "high"}]},
                    "run-scans.sh": {"findings": [{"tool": "semgrep"}]},
                    "coverage-collect.py": {
                        "coverage": [{"file": "tracked.txt", "lines": 75}]
                    },
                }
                return mapping[name]

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_run
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertTrue((session_dir / "diff.patch").exists())
            self.assertTrue((session_dir / "changed-files.txt").exists())
            self.assertTrue((session_dir / "launch.json").exists())

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
                name = next(
                    Path(part).name for part in command if part.endswith((".py", ".sh"))
                )
                invocations[name] = {
                    "command": command,
                    "cwd": cwd,
                    "timeout": timeout,
                    "input_text": input_text,
                }
                mapping: dict[str, dict[str, object]] = {
                    "discover-project.py": {"language": "python"},
                    "complexity.sh": {"hotspots": []},
                    "git-risk.sh": {"tiers": []},
                    "run-scans.sh": {"findings": [], "tool_status": {}},
                    "coverage-collect.py": {"coverage": []},
                }
                return mapping[name]

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_run
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertEqual(
                invocations["complexity.sh"]["input_text"], "tracked.txt\n"
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
                        Path.cwd()
                        / "skills"
                        / "codereview"
                        / "scripts"
                        / "run-scans.sh"
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
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
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

    def test_extract_diff_staged_mode_with_staged_files(self) -> None:
        """extract_diff in staged mode returns staged files when they exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo(Path(tmpdir))
            # Modify and stage a file (but don't commit)
            (repo / "tracked.txt").write_text("one\nstaged change\n", encoding="utf-8")
            self._run(["git", "add", "tracked.txt"], cwd=repo)

            result = extract_diff(repo_root=repo, mode="staged")

        self.assertIsInstance(result, DiffResult)
        self.assertIn("tracked.txt", result.changed_files)
        self.assertIn("+staged change", result.diff_text)
        self.assertEqual(result.mode, "staged")

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
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
