import json
from argparse import Namespace
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.orchestrate import (
    DEFAULT_CONFIG,
    DiffResult,
    PromptBudgetExceeded,
    PromptContext,
    SubprocessError,
    _cleanup_stale_session,
    _apply_spec_scope,
    _chunk_diff,
    _count_changed_lines_for_file,
    assemble_expert_panel,
    assemble_explorer_prompt,
    assemble_report_envelope,
    build_parser,
    check_token_budget,
    extract_diff,
    finalize,
    load_config,
    post_explorers,
    prepare,
    select_mode,
    triage_files,
)

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


class OrchestratePlumbingTests(unittest.TestCase):
    def test_help_lists_expected_subcommands(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "orchestrate.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for subcommand in ("prepare", "post-explorers", "finalize", "cleanup"):
            self.assertIn(subcommand, result.stdout)

    def test_module_exposes_main(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from scripts.orchestrate import main; assert callable(main)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_extract_diff_base_mode_returns_changed_files_and_diff_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._run(["git", "init", "-b", "main"], cwd=repo)
            self._run(["git", "config", "user.name", "Test User"], cwd=repo)
            self._run(["git", "config", "user.email", "test@example.com"], cwd=repo)
            (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
            self._run(["git", "add", "tracked.txt"], cwd=repo)
            self._run(["git", "commit", "-m", "initial"], cwd=repo)
            self._run(["git", "checkout", "-b", "feature"], cwd=repo)
            (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
            self._run(["git", "commit", "-am", "update"], cwd=repo)

            result = extract_diff(repo_root=repo, mode="base", base_ref="main")

        self.assertIsInstance(result, DiffResult)
        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_select_mode_returns_standard_below_thresholds(self) -> None:
        self.assertEqual(select_mode(file_count=12, diff_line_count=400), "standard")

    def test_load_config_merges_yaml_with_defaults(self) -> None:
        fake_yaml = mock.Mock()
        fake_yaml.safe_load.return_value = {
            "cadence": "wave-end",
            "pushback_level": "selective",
            "confidence_floor": 0.65,
            "ignore_paths": ["vendor/"],
        }
        with mock.patch("scripts.orchestrate.yaml", fake_yaml):
            config = load_config(
                TESTS_DIR / "fixtures" / "orchestrate" / "codereview.yaml"
            )

        self.assertEqual(config["cadence"], "wave-end")
        self.assertEqual(config["pushback_level"], "selective")
        self.assertEqual(config["confidence_floor"], 0.65)
        self.assertEqual(config["large_diff"], DEFAULT_CONFIG["large_diff"])
        self.assertEqual(config["ignore_paths"], ["vendor/"])

    def test_assemble_expert_panel_always_includes_core_experts(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )

        panel = assemble_expert_panel(diff_result, config={}, spec_content=None)

        self.assertEqual(
            [expert["name"] for expert in panel[:3]],
            ["correctness", "security-config", "test-adequacy"],
        )

    def test_assemble_expert_panel_activates_shell_script_for_shell_diff(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["scripts/run.sh"],
            diff_text="@@\n+#!/usr/bin/env bash\n+set -euo pipefail\n",
        )

        panel = assemble_expert_panel(diff_result, config={}, spec_content=None)

        self.assertIn("shell-script", [expert["name"] for expert in panel])

    def test_assemble_expert_panel_force_all_enables_all_experts(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )

        panel = assemble_expert_panel(
            diff_result,
            config={"expert_panel": {"force_all": True}},
            spec_content=None,
        )

        names = [expert["name"] for expert in panel]
        self.assertIn("security-dataflow", names)
        self.assertIn("shell-script", names)
        self.assertIn("api-contract", names)
        self.assertIn("concurrency", names)
        self.assertIn("error-handling", names)
        self.assertIn("reliability", names)
        self.assertNotIn("spec-verification", names)

    def test_assemble_expert_panel_honors_disable_and_passes_filters(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["scripts/run.sh"],
            diff_text="@@\n+#!/usr/bin/env bash\n+set -euo pipefail\n",
        )

        panel = assemble_expert_panel(
            diff_result,
            config={
                "passes": ["correctness", "security-config", "test-adequacy"],
                "expert_panel": {"experts": {"security-config": False}},
            },
            spec_content=None,
        )

        self.assertEqual(
            [expert["name"] for expert in panel], ["correctness", "test-adequacy"]
        )

    def test_assemble_expert_panel_activates_dataflow_for_request_diff(self):
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/views.py"],
            diff_text="@@\n+data = request.args.get('q')\n",
        )
        panel = assemble_expert_panel(diff_result, config={}, spec_content=None)
        names = [e["name"] for e in panel]
        self.assertIn("security-dataflow", names)

    def test_assemble_expert_panel_security_alias_expands_to_both(self):
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )
        panel = assemble_expert_panel(
            diff_result,
            config={"passes": ["security"]},
            spec_content=None,
        )
        names = [e["name"] for e in panel]
        self.assertIn("security-dataflow", names)
        self.assertIn("security-config", names)

    def test_assemble_expert_panel_rejects_unknown_only_passes(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )
        with self.assertRaisesRegex(ValueError, "Unknown pass names"):
            assemble_expert_panel(
                diff_result,
                config={"passes": ["definitely-not-a-pass"]},
                spec_content=None,
            )

    def test_cleanup_stale_session_removes_judge_and_report_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            (session_dir / ".codereview-session").write_text("ok\n", encoding="utf-8")
            stale_files = [
                "judge-input.json",
                "judge-prompt.md",
                "judge.json",
                "enriched.json",
                "report.json",
                "report.md",
                "finalize.json",
                "timing.jsonl",
            ]
            for name in stale_files:
                (session_dir / name).write_text("stale\n", encoding="utf-8")

            _cleanup_stale_session(session_dir)

            for name in stale_files:
                self.assertFalse((session_dir / name).exists())

    def test_assemble_report_envelope_uses_lifecycle_findings_for_tier_summary(
        self,
    ) -> None:
        report = assemble_report_envelope(
            launch_packet={"changed_files": ["a.py"]},
            enriched={"tier_summary": {"must_fix": 1, "should_fix": 0, "consider": 0}},
            lifecycle={
                "findings": [{"summary": "warn", "action_tier": "should_fix"}],
                "suppressed_findings": [],
                "lifecycle_summary": {
                    "new": 1,
                    "recurring": 0,
                    "rejected": 0,
                    "deferred": 0,
                    "deferred_resurfaced": 0,
                },
            },
            judge_output={},
        )

        self.assertEqual(
            report["tier_summary"], {"must_fix": 0, "should_fix": 1, "consider": 0}
        )
        self.assertEqual(report["verdict"], "WARN")

    def test_build_expert_pass_models_security_alias(self):
        from scripts.orchestrate import _build_expert

        expert = _build_expert(
            "security-dataflow", {"pass_models": {"security": "opus"}}, "core"
        )
        self.assertEqual(expert["model"], "opus")
        expert2 = _build_expert(
            "security-config", {"pass_models": {"security": "opus"}}, "core"
        )
        self.assertEqual(expert2["model"], "opus")
        expert3 = _build_expert(
            "correctness", {"pass_models": {"security": "opus"}}, "core"
        )
        self.assertEqual(expert3["model"], "sonnet")

    def test_extract_diff_commit_mode_uses_head_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo_with_feature_commit(Path(tmpdir))

            result = extract_diff(repo_root=repo, mode="commit")

        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_extract_diff_range_mode_uses_explicit_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo_with_feature_commit(Path(tmpdir))
            first_commit = self._capture(
                ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=repo
            ).strip()
            head_commit = self._capture(["git", "rev-parse", "HEAD"], cwd=repo).strip()

            result = extract_diff(
                repo_root=repo,
                mode="range",
                revision_range=f"{first_commit}..{head_commit}",
            )

        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_extract_diff_staged_mode_falls_back_to_commit_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo_with_feature_commit(Path(tmpdir))

            result = extract_diff(repo_root=repo, mode="staged")

        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_extract_diff_path_mode_scopes_to_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._init_repo_with_feature_commit(
                Path(tmpdir), add_second_file=True
            )

            result = extract_diff(repo_root=repo, mode="path", pathspec="tracked.txt")

        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertNotIn("second.txt", result.diff_text)

    def test_extract_diff_pr_mode_uses_gh_commands(self) -> None:
        outputs = {
            ("gh", "pr", "view", "42", "--json", "baseRefName,headRefName"): (
                '{"baseRefName":"main","headRefName":"feature"}'
            ),
            ("gh", "pr", "diff", "42", "--name-only"): "tracked.txt\n",
            (
                "gh",
                "pr",
                "diff",
                "42",
            ): "diff --git a/tracked.txt b/tracked.txt\n+two\n",
        }

        def fake_run(
            command: list[str],
            cwd: Path | None = None,
            timeout: float | None = None,
            input_text: str | None = None,
        ) -> str:
            return outputs[tuple(command)]

        with mock.patch(
            "scripts.orchestrate.run_subprocess_text", side_effect=fake_run
        ):
            result = extract_diff(repo_root=Path("."), mode="pr", pr_number="42")

        self.assertEqual(result.base_ref, "main")
        self.assertEqual(result.changed_files, ["tracked.txt"])
        self.assertIn("+two", result.diff_text)

    def test_extract_diff_pr_mode_raises_without_gh(self) -> None:
        with mock.patch(
            "scripts.orchestrate.run_subprocess_text",
            side_effect=SubprocessError("gh: command not found"),
        ):
            with self.assertRaises(SubprocessError):
                extract_diff(repo_root=Path("."), mode="pr", pr_number="42")

    def test_prompt_context_render_orders_sections(self) -> None:
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="diff body",
            changed_files="tracked.txt",
            complexity="complexity",
            git_risk="risk",
            scan_results="scan",
            callers="callers",
            language_standards="standards",
            review_instructions="instructions",
            spec="spec",
        )

        rendered = context.render()

        self.assertIn("## Global Contract\ncontract", rendered)
        self.assertIn("## Your Focus\nfocus", rendered)
        self.assertIn("### Deterministic Scan Results", rendered)
        self.assertGreater(context.estimate_tokens(), 0)

    def test_check_token_budget_truncates_sections_progressively(self) -> None:
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="diff " * 80,
            changed_files="tracked.txt",
            complexity="complexity",
            git_risk="risk " * 80,
            scan_results="scan " * 80,
            callers="callers",
            language_standards="standards " * 80,
            review_instructions="instructions",
            spec="spec",
        )

        rendered = check_token_budget(context, "correctness", prompt_budget_tokens=120)

        self.assertNotIn("standards standards", rendered)
        self.assertIn("scan summary:", rendered)
        self.assertIn("risk summary:", rendered)

    def test_assemble_explorer_prompt_builds_context_from_inputs(self) -> None:
        diff_result = DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["tracked.txt"],
            diff_text="@@\n+two\n",
        )

        context = assemble_explorer_prompt(
            expert_name="correctness",
            diff_result=diff_result,
            global_contract="global contract",
            complexity="complexity",
            git_risk="risk",
            scan_results="scan",
            callers="callers",
            language_standards="standards",
            review_instructions="instructions",
            spec="spec",
        )

        self.assertEqual(
            context.pass_prompt.splitlines()[0],
            "Review this diff for functional correctness.",
        )
        self.assertIn("tracked.txt", context.changed_files)
        self.assertIn("+two", context.diff)

    def test_prepare_writes_launch_packet_and_session_artifacts(self) -> None:
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

            side_effect = [
                {"language": "python"},
                {"hotspots": [{"file": "tracked.txt", "score": "C"}]},
                {"tiers": [{"file": "tracked.txt", "tier": "high"}]},
                {"findings": [{"tool": "semgrep"}]},
                {"coverage": [{"file": "tracked.txt", "lines": 75}]},
            ]

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=side_effect
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            self.assertTrue((session_dir / "diff.patch").exists())
            self.assertTrue((session_dir / "changed-files.txt").exists())
            self.assertTrue((session_dir / "launch.json").exists())

            launch = json.loads((session_dir / "launch.json").read_text())
            self.assertEqual(launch["session_dir"], str(session_dir))
            self.assertEqual(launch["diff_result"]["changed_files"], ["tracked.txt"])
            self.assertTrue(launch["waves"])
            self.assertTrue(launch["judge"]["output_file"].endswith("judge.json"))

    def test_prepare_parser_accepts_timeout_flag(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["prepare", "--session-dir", "/tmp/session", "--timeout", "5"]
        )

        self.assertEqual(args.timeout, 5)

    def test_prepare_timeout_writes_partial_launch_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
                timeout=5,
            )

            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=["tracked.txt"],
                diff_text="@@\n+two\n",
            )

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json",
                    side_effect=TimeoutError("Global timeout exceeded during discover"),
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=REPO_ROOT
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 1)
            launch = json.loads((session_dir / "launch.json").read_text())
            self.assertEqual(launch["status"], "timeout")
            self.assertIn("Global timeout exceeded", launch["error"])

    def test_post_explorers_writes_deduped_judge_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            fixtures = TESTS_DIR / "fixtures" / "orchestrate"
            correctness = json.loads(
                (fixtures / "mock-explorer-correctness.json").read_text()
            )
            duplicate = dict(correctness["findings"][0])
            duplicate["confidence"] = 0.60
            security_text = (fixtures / "mock-explorer-security.json").read_text()
            malformed_text = (fixtures / "mock-explorer-malformed.txt").read_text()

            (session_dir / "explorer-correctness.json").write_text(
                json.dumps(correctness["findings"] + [duplicate]),
                encoding="utf-8",
            )
            (session_dir / "explorer-security.json").write_text(
                security_text, encoding="utf-8"
            )
            (session_dir / "explorer-malformed.json").write_text(
                malformed_text, encoding="utf-8"
            )

            launch_packet = {
                "session_dir": str(session_dir),
                "waves": [
                    {
                        "wave": 1,
                        "tasks": [
                            {
                                "name": "correctness",
                                "output_file": str(
                                    (
                                        session_dir / "explorer-correctness.json"
                                    ).absolute()
                                ),
                            },
                            {
                                "name": "security",
                                "output_file": str(
                                    (session_dir / "explorer-security.json").absolute()
                                ),
                            },
                            {
                                "name": "malformed",
                                "output_file": str(
                                    (session_dir / "explorer-malformed.json").absolute()
                                ),
                            },
                        ],
                    }
                ],
                "judge": {
                    "prompt_file": str(
                        (
                            REPO_ROOT
                            / "skills"
                            / "codereview"
                            / "prompts"
                            / "reviewer-judge.md"
                        ).absolute()
                    ),
                    "output_file": str((session_dir / "judge.json").absolute()),
                },
                "scan_results": {"findings": []},
                "_config": {"confidence_floor": 0.65},
            }
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads((session_dir / "judge-input.json").read_text())
            self.assertEqual(judge_input["status"], "ready_for_judge")
            self.assertEqual(len(judge_input["findings"]), 4)
            summaries = [finding["summary"] for finding in judge_input["findings"]]
            self.assertIn("Session directory is resolved too early", summaries)

    def test_finalize_writes_report_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            session_dir = repo_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            (session_dir / "changed-files.txt").write_text(
                "scripts/orchestrate.py\n", encoding="utf-8"
            )
            judge_output = json.loads(
                (
                    TESTS_DIR / "fixtures" / "orchestrate" / "mock-judge-output.json"
                ).read_text(encoding="utf-8")
            )
            judge_output_path = session_dir / "judge.json"
            judge_output_path.write_text(json.dumps(judge_output), encoding="utf-8")

            launch_packet = {
                "review_id": "review-123",
                "session_dir": str(session_dir),
                "scope": "branch",
                "base_ref": "main",
                "head_ref": "feature",
                "mode": "standard",
                "_config": {"confidence_floor": 0.65},
                "tool_status": {
                    "semgrep": {"status": "ran", "finding_count": 1, "note": None}
                },
                "diff_result": {"changed_files": ["scripts/orchestrate.py"]},
                "scan_results": {"findings": []},
            }
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            enriched = {
                "findings": [
                    {
                        "id": "security-allowlist-01",
                        "source": "ai",
                        "pass": "security",
                        "severity": "high",
                        "confidence": 0.93,
                        "file": "scripts/orchestrate.py",
                        "line": 89,
                        "summary": "Subprocess command is assembled from untrusted input",
                        "failure_mode": "A crafted review task could execute arbitrary commands.",
                        "fix": "Require allowlisted commands before dispatch.",
                        "action_tier": "must_fix",
                    }
                ],
                "tier_summary": {"must_fix": 1, "should_fix": 0, "consider": 0},
                "dropped": {"below_confidence_floor": 0},
            }
            lifecycle = {
                "findings": enriched["findings"],
                "suppressed_findings": [],
                "lifecycle_summary": {
                    "new": 1,
                    "recurring": 0,
                    "rejected": 0,
                    "deferred": 0,
                    "deferred_resurfaced": 0,
                },
            }

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=repo_root
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json",
                    side_effect=[enriched, lifecycle],
                ),
                mock.patch(
                    "scripts.orchestrate.subprocess.run",
                    return_value=subprocess.CompletedProcess(["bash"], 0, "", ""),
                ),
            ):
                result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            report_json = json.loads(
                (session_dir / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report_json["verdict"], "FAIL")
            self.assertTrue((session_dir / "report.md").exists())
            review_dir = repo_root / ".agents" / "reviews"
            self.assertTrue(any(review_dir.glob("*.json")))
            self.assertTrue(any(review_dir.glob("*.md")))

    @staticmethod
    def _run(command: list[str], cwd: Path) -> None:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _capture(command: list[str], cwd: Path) -> str:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _init_repo_with_feature_commit(
        self, repo: Path, *, add_second_file: bool = False
    ) -> Path:
        self._run(["git", "init", "-b", "main"], cwd=repo)
        self._run(["git", "config", "user.name", "Test User"], cwd=repo)
        self._run(["git", "config", "user.email", "test@example.com"], cwd=repo)
        (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        if add_second_file:
            (repo / "second.txt").write_text("alpha\n", encoding="utf-8")
        self._run(["git", "add", "."], cwd=repo)
        self._run(["git", "commit", "-m", "initial"], cwd=repo)
        self._run(["git", "checkout", "-b", "feature"], cwd=repo)
        (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
        if add_second_file:
            (repo / "second.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        self._run(["git", "add", "."], cwd=repo)
        self._run(["git", "commit", "-m", "update"], cwd=repo)
        return repo

    def test_check_token_budget_raises_when_exceeds_after_truncation(self) -> None:
        """PromptBudgetExceeded is raised when the prompt is too large even after all truncations."""
        huge_diff = "+" + "x" * 200_000
        context = PromptContext(
            global_contract="contract " * 500,
            pass_prompt="focus " * 500,
            diff=huge_diff,
            changed_files="a.py\nb.py\nc.py",
            complexity="complexity " * 500,
            git_risk="risk " * 500,
            scan_results="scan " * 500,
            callers="callers " * 500,
            language_standards="standards " * 500,
            review_instructions="instructions " * 500,
            spec="spec " * 500,
        )

        with self.assertRaises(PromptBudgetExceeded):
            check_token_budget(context, "correctness", prompt_budget_tokens=10)

    def test_chunk_diff_filters_to_requested_files(self) -> None:
        """_chunk_diff returns only hunks for files in chunk_files."""
        diff_text = (
            "diff --git a/file1.py b/file1.py\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+added_to_file1\n"
            " line3\n"
            "diff --git a/file2.py b/file2.py\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1,2 +1,3 @@\n"
            " alpha\n"
            "+added_to_file2\n"
            "diff --git a/file3.py b/file3.py\n"
            "--- a/file3.py\n"
            "+++ b/file3.py\n"
            "@@ -1 +1,2 @@\n"
            " gamma\n"
            "+added_to_file3\n"
        )

        result = _chunk_diff(diff_text, ["file2.py"])

        self.assertIn("added_to_file2", result)
        self.assertNotIn("added_to_file1", result)
        self.assertNotIn("added_to_file3", result)
        self.assertIn("diff --git a/file2.py b/file2.py", result)

    def test_chunk_diff_uses_rename_destination_path(self) -> None:
        diff_text = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 95%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
            "--- a/old_name.py\n"
            "+++ b/new_name.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        result = _chunk_diff(diff_text, ["new_name.py"])

        self.assertIn("rename to new_name.py", result)
        self.assertEqual(result, diff_text.rstrip("\n"))

    def test_chunk_diff_returns_empty_when_no_chunk_files_match(self) -> None:
        diff_text = (
            "diff --git a/file1.py b/file1.py\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        result = _chunk_diff(diff_text, ["file2.py"])

        self.assertEqual(result, "")

    def test_apply_spec_scope_returns_full_spec_when_no_heading_matches(self) -> None:
        """_apply_spec_scope returns the full spec unchanged when no headings match the scope."""
        spec_content = (
            "# Introduction\n"
            "This is the introduction.\n"
            "\n"
            "## Authentication\n"
            "Auth details here.\n"
            "\n"
            "## Database\n"
            "Database details here.\n"
        )

        result = _apply_spec_scope(spec_content, "nonexistent-term")

        self.assertEqual(result, spec_content)


class TriageTests(unittest.TestCase):
    def test_triage_files_disabled_returns_all_complex(self):
        result = triage_files(["a.py", "b.md"], "+x", {"triage": {"enabled": False}})
        self.assertEqual(result, {"a.py": "complex", "b.md": "complex"})

    def test_triage_files_default_config_returns_all_complex(self):
        result = triage_files(["a.py", "b.md"], "+x", {})
        self.assertEqual(result, {"a.py": "complex", "b.md": "complex"})

    def test_triage_files_trivial_extensions(self):
        config = {"triage": {"enabled": True, "always_review_extensions": [".py"]}}
        result = triage_files(["README.md", "data.json", "config.yaml"], "+x", config)
        self.assertEqual(result["README.md"], "trivial")
        self.assertEqual(result["data.json"], "trivial")
        self.assertEqual(result["config.yaml"], "trivial")

    def test_triage_files_always_review_extensions(self):
        config = {
            "triage": {"enabled": True, "always_review_extensions": [".py", ".go"]}
        }
        result = triage_files(["app.py", "main.go"], "+x", config)
        self.assertEqual(result["app.py"], "complex")
        self.assertEqual(result["main.go"], "complex")

    def test_triage_files_line_threshold(self):
        diff = "diff --git a/style.css b/style.css\n+line1\n+line2\n"
        config = {
            "triage": {
                "enabled": True,
                "trivial_line_threshold": 3,
                "always_review_extensions": [".py"],
            }
        }
        result = triage_files(["style.css"], diff, config)
        self.assertEqual(result["style.css"], "trivial")

        diff_big = "diff --git a/style.css b/style.css\n+l1\n+l2\n+l3\n+l4\n+l5\n"
        result2 = triage_files(["style.css"], diff_big, config)
        self.assertEqual(result2["style.css"], "complex")

    def test_count_changed_lines_for_file(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " unchanged\n"
            "+added1\n"
            "+added2\n"
            "-removed1\n"
            " unchanged\n"
            "diff --git a/bar.py b/bar.py\n"
            "+other\n"
        )
        self.assertEqual(_count_changed_lines_for_file(diff, "foo.py"), 3)
        self.assertEqual(_count_changed_lines_for_file(diff, "bar.py"), 1)


class SuggestMissingTestsTests(unittest.TestCase):
    def _make_diff_result(self):
        return DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=["src/app.py"],
            diff_text="@@\n+print('ok')\n",
        )

    def test_suggest_missing_tests_off_appends_suppression(self):
        ctx = assemble_explorer_prompt(
            expert_name="test-adequacy",
            diff_result=self._make_diff_result(),
            global_contract="contract",
            complexity="{}",
            git_risk="{}",
            scan_results="{}",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
            config={"suggest_missing_tests": False},
        )
        rendered = ctx.render()
        self.assertIn("Do NOT suggest adding new tests", rendered)
        self.assertIn("Stale tests", rendered)

    def test_suggest_missing_tests_on_no_suppression(self):
        ctx = assemble_explorer_prompt(
            expert_name="test-adequacy",
            diff_result=self._make_diff_result(),
            global_contract="contract",
            complexity="{}",
            git_risk="{}",
            scan_results="{}",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
            config={"suggest_missing_tests": True},
        )
        rendered = ctx.render()
        self.assertNotIn("Do NOT suggest adding new tests", rendered)

    def test_suggest_missing_tests_default_is_off(self):
        ctx = assemble_explorer_prompt(
            expert_name="test-adequacy",
            diff_result=self._make_diff_result(),
            global_contract="contract",
            complexity="{}",
            git_risk="{}",
            scan_results="{}",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
            config={},
        )
        rendered = ctx.render()
        self.assertIn("Do NOT suggest adding new tests", rendered)

    def test_non_test_adequacy_expert_unaffected(self):
        ctx = assemble_explorer_prompt(
            expert_name="correctness",
            diff_result=self._make_diff_result(),
            global_contract="contract",
            complexity="{}",
            git_risk="{}",
            scan_results="{}",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
            config={"suggest_missing_tests": False},
        )
        rendered = ctx.render()
        self.assertNotIn("Do NOT suggest adding new tests", rendered)


if __name__ == "__main__":
    unittest.main()
