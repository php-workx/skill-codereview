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
    _skip_cross_file_planning,
    assemble_expert_panel,
    assemble_explorer_prompt,
    assemble_report_envelope,
    build_cross_file_context,
    build_parser,
    check_token_budget,
    drop_least_relevant_checklist,
    extract_diff,
    finalize,
    load_config,
    load_domain_checklists,
    load_path_instructions,
    load_review_md_directives,
    load_review_md_skip_patterns,
    post_explorers,
    prepare,
    select_mode,
    truncate_cross_file_top3_high_risk,
    truncate_review_md_always_check_only,
    truncate_to_changed_hunks_only,
    truncate_spec_to_5k,
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

            context_responses = {
                "discover-project.py": {"language": "python"},
                "code_intel.py:complexity": {
                    "hotspots": [{"file": "tracked.txt", "score": "C"}],
                    "analyzer": "regex-only",
                    "tool_status": {},
                },
                "code_intel.py:functions": {"functions": []},
                "git-risk.sh": {"tiers": [{"file": "tracked.txt", "tier": "high"}]},
                "run-scans.sh": {"findings": [{"tool": "semgrep"}]},
                "coverage-collect.py": {
                    "coverage": [{"file": "tracked.txt", "lines": 75}]
                },
            }

            def fake_run(command, *args, **kwargs):
                from pathlib import Path

                script_name = next(
                    (Path(p).name for p in command if p.endswith((".py", ".sh"))),
                    None,
                )
                if script_name == "code_intel.py":
                    sub = command[-1] if len(command) > 2 else "complexity"
                    script_name = f"code_intel.py:{sub}"
                if script_name and script_name in context_responses:
                    return context_responses[script_name]
                return {}

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=fake_run
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

    def test_prompt_context_new_fields_default_empty(self) -> None:
        """New context enrichment fields default to empty string."""
        context = PromptContext(
            global_contract="c",
            pass_prompt="p",
            diff="d",
            changed_files="f",
            complexity="cx",
            git_risk="r",
            scan_results="s",
            callers="cl",
            language_standards="ls",
            review_instructions="ri",
            spec="sp",
        )
        self.assertEqual(context.prescan_signals, "")
        self.assertEqual(context.domain_checklists, "")
        self.assertEqual(context.cross_file_context, "")
        self.assertEqual(context.review_md_directives, "")
        self.assertEqual(context.path_instructions, "")
        self.assertEqual(context.functions_summary, "")
        self.assertEqual(context.graph_summary, "")

    def test_prompt_context_render_includes_new_sections(self) -> None:
        """New fields render with correct section headers when non-empty."""
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="diff",
            changed_files="f.py",
            complexity="",
            git_risk="",
            scan_results="",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
            prescan_signals="CRITICAL: 1 secret",
            domain_checklists="## SQL Safety\n- check params",
            cross_file_context="views.py calls login()",
            review_md_directives="always check auth",
            path_instructions="src/auth: focus on bypass",
            functions_summary="| f.py | login | user | bool |",
            graph_summary="changed: login -> callers: views.py",
        )
        rendered = context.render()
        self.assertIn("### Prescan Signals\nCRITICAL: 1 secret", rendered)
        self.assertIn("### Domain-Specific Checklists\n## SQL Safety", rendered)
        self.assertIn("### Cross-File Context\nviews.py calls login()", rendered)
        self.assertIn(
            "<review-directives>\nalways check auth\n</review-directives>", rendered
        )
        self.assertIn(
            "<path-instructions>\nsrc/auth: focus on bypass\n</path-instructions>",
            rendered,
        )
        self.assertIn("### Function Definitions\n| f.py | login", rendered)
        self.assertIn("### Dependency Graph\nchanged: login -> callers", rendered)

    def test_prompt_context_render_omits_empty_new_sections(self) -> None:
        """Empty new fields don't produce section headers in render output."""
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="diff",
            changed_files="f.py",
            complexity="",
            git_risk="",
            scan_results="",
            callers="",
            language_standards="",
            review_instructions="",
            spec="",
        )
        rendered = context.render()
        self.assertNotIn("Prescan Signals", rendered)
        self.assertNotIn("Domain-Specific Checklists", rendered)
        self.assertNotIn("Cross-File Context", rendered)
        self.assertNotIn("Repo-Level Review Directives", rendered)
        self.assertNotIn("Path-Specific Instructions", rendered)
        self.assertNotIn("Function Definitions", rendered)
        self.assertNotIn("Dependency Graph", rendered)

    def test_budget_cascade_worst_case_survives(self) -> None:
        """Synthetic prompt exceeding 70k tokens passes cascade without crash."""
        big = "x " * 20_000
        context = PromptContext(
            global_contract="contract " * 500,
            pass_prompt="focus " * 500,
            diff="@@\n+" + "line\n+" * 30_000,
            changed_files="a.py\nb.py",
            complexity="hotspot " * 500,
            git_risk="risk " * 500,
            scan_results="finding " * 500,
            callers="caller " * 500,
            language_standards="standard " * 500,
            review_instructions="instruction " * 500,
            spec="spec " * 5_000,
            prescan_signals=big,
            domain_checklists=big,
            cross_file_context=big,
            review_md_directives=big,
            path_instructions=big,
            functions_summary=big,
            graph_summary=big,
        )
        self.assertGreater(context.estimate_tokens(), 70_000)
        rendered = check_token_budget(
            context, "correctness", prompt_budget_tokens=70_000
        )
        self.assertLessEqual(len(rendered) // 4, 70_000)

    def test_budget_cascade_sheds_in_priority_order(self) -> None:
        """Truncation cascade sheds lowest-value context first."""
        context = PromptContext(
            global_contract="contract",
            pass_prompt="focus",
            diff="+" + "d" * 200,
            changed_files="f.py",
            complexity="",
            git_risk="risk " * 50,
            scan_results="scan " * 50,
            callers="",
            language_standards="std " * 50,
            review_instructions="",
            spec="",
            graph_summary="graph " * 50,
            functions_summary="func " * 50,
        )
        check_token_budget(context, "correctness", prompt_budget_tokens=200)
        # Language standards shed first (priority P10)
        self.assertEqual(context.language_standards, "")
        # Scan results summarized next (priority P9)
        self.assertIn("scan summary:", context.scan_results)

    def test_truncate_to_changed_hunks_handles_format_diff(self) -> None:
        """truncate_to_changed_hunks_only detects format-diff syntax."""
        format_diff = (
            "## File: src/auth/login.py\n"
            "\n"
            "@@ def validate_session (line 42)\n"
            "__new hunk__\n"
            "42  session = cache.get(token)\n"
            "43 +if session and not session.expired:\n"
            "44 +    session.refresh()\n"
            "45 +    return session\n"
            "46  return None\n"
            "__old hunk__\n"
            " session = cache.get(token)\n"
            "-if session:\n"
            "-    return session\n"
            " return None\n"
        )
        result = truncate_to_changed_hunks_only(format_diff)
        self.assertIn("## File: src/auth/login.py", result)
        self.assertIn("__new hunk__", result)
        self.assertIn("+if session and not session.expired:", result)
        self.assertIn("-if session:", result)

    def test_truncate_spec_to_5k(self) -> None:
        """truncate_spec_to_5k truncates large specs and leaves small ones alone."""
        short = "spec content"
        self.assertEqual(truncate_spec_to_5k(short), short)
        long_spec = "x" * 25_000
        result = truncate_spec_to_5k(long_spec)
        self.assertIn("[spec truncated for budget]", result)
        self.assertLess(len(result), 25_000)

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


class DomainChecklistTests(unittest.TestCase):
    """Tests for domain-specific checklist detection and loading."""

    def test_sql_pattern_triggers_sql_checklist(self) -> None:
        diff = "+    cursor.execute(SELECT * FROM users WHERE id = ?)"
        result = load_domain_checklists(diff)
        self.assertIn("SQL Safety", result)
        self.assertIn("parameterized queries", result)

    def test_orm_pattern_triggers_sql_checklist(self) -> None:
        diff = "+from sqlalchemy import Column, Integer"
        result = load_domain_checklists(diff)
        self.assertIn("SQL Safety", result)

    def test_llm_pattern_triggers_llm_checklist(self) -> None:
        diff = "+import openai\n+client = openai.Client()"
        result = load_domain_checklists(diff)
        self.assertIn("LLM Trust", result)
        self.assertIn("API keys", result)

    def test_anthropic_pattern_triggers_llm_checklist(self) -> None:
        diff = "+from anthropic import Anthropic"
        result = load_domain_checklists(diff)
        self.assertIn("LLM Trust", result)

    def test_concurrency_pattern_triggers_concurrency_checklist(self) -> None:
        diff = "+async def fetch_data():\n+    await asyncio.gather(*tasks)"
        result = load_domain_checklists(diff)
        self.assertIn("Concurrency", result)
        self.assertIn("deadlock", result.lower())

    def test_goroutine_pattern_triggers_concurrency_checklist(self) -> None:
        diff = "+go func() {\n+    mu.Lock()\n+}"
        result = load_domain_checklists(diff)
        self.assertIn("Concurrency", result)

    def test_multiple_checklists_load_simultaneously(self) -> None:
        diff = (
            "+cursor.execute(SELECT * FROM users)\n"
            "+import openai\n"
            "+async def handler():\n"
        )
        result = load_domain_checklists(diff)
        self.assertIn("SQL Safety", result)
        self.assertIn("LLM Trust", result)
        self.assertIn("Concurrency", result)

    def test_no_checklist_when_no_patterns_match(self) -> None:
        diff = "+x = 1 + 2\n+print(x)\n"
        result = load_domain_checklists(diff)
        self.assertEqual(result, "")

    def test_checklist_includes_header(self) -> None:
        diff = "+from sqlalchemy import create_engine"
        result = load_domain_checklists(diff)
        self.assertIn("## Domain-Specific Checklists (auto-detected)", result)
        self.assertIn("triggered by:", result)

    def test_drop_least_relevant_checklist_empty_input(self) -> None:
        self.assertEqual(drop_least_relevant_checklist(""), "")

    def test_drop_least_relevant_checklist_single_section(self) -> None:
        text = "### SQL Safety\n\n- [ ] Item 1\n"
        result = drop_least_relevant_checklist(text)
        self.assertEqual(result, text)

    def test_drop_least_relevant_checklist_drops_last_section(self) -> None:
        text = (
            "### SQL Safety\n\n- [ ] Item 1\n\n"
            "### Concurrency\n\n- [ ] Item 2\n\n"
            "### LLM Trust\n\n- [ ] Item 3\n"
        )
        result = drop_least_relevant_checklist(text)
        self.assertIn("SQL Safety", result)
        self.assertIn("Concurrency", result)
        self.assertNotIn("LLM Trust", result)


class ReviewMdDirectivesTests(unittest.TestCase):
    """Tests for structured REVIEW.md parsing (sc-drzb)."""

    _FULL_REVIEW_MD = (
        "# Code Review Guidelines\n\n"
        "## Always check\n"
        "- New API endpoints have corresponding integration tests\n"
        "- Database migrations are backward-compatible\n\n"
        "## Style\n"
        "- Prefer early returns over nested conditionals\n"
        "- Use structured logging (key=value)\n\n"
        "## Skip\n"
        "- Generated files under src/gen/\n"
        "- Vendored dependencies\n"
    )

    def test_parse_all_three_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(self._FULL_REVIEW_MD, encoding="utf-8")
            result = load_review_md_directives(repo)

        self.assertIn("### Mandatory Checks", result)
        self.assertIn(
            "- New API endpoints have corresponding integration tests", result
        )
        self.assertIn("- Database migrations are backward-compatible", result)
        self.assertIn("### Style Preferences", result)
        self.assertIn("- Prefer early returns over nested conditionals", result)
        self.assertIn("- Use structured logging (key=value)", result)

    def test_only_always_check_section(self) -> None:
        content = "# Guidelines\n\n## Always check\n- All tests pass\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(content, encoding="utf-8")
            result = load_review_md_directives(repo)

        self.assertIn("### Mandatory Checks", result)
        self.assertIn("- All tests pass", result)
        self.assertNotIn("### Style Preferences", result)

    def test_missing_review_md_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_review_md_directives(Path(tmpdir))
        self.assertEqual(result, "")

    def test_no_recognized_sections_returns_empty(self) -> None:
        content = "# Code Review Guidelines\n\n## Some Other Section\n- Random item\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(content, encoding="utf-8")
            result = load_review_md_directives(repo)
        self.assertEqual(result, "")

    def test_thirty_item_cap_per_section(self) -> None:
        items = "\n".join(f"- Item {i}" for i in range(35))
        content = f"## Always check\n{items}\n\n## Style\n{items}\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(content, encoding="utf-8")
            with mock.patch("scripts.orchestrate.progress") as mock_progress:
                result = load_review_md_directives(repo)

        # Should cap at 30 items per section
        self.assertEqual(result.count("- Item"), 60)
        # Should have warned twice (once per section)
        cap_calls = [
            c for c in mock_progress.call_args_list if c[0][0] == "review_md_cap"
        ]
        self.assertEqual(len(cap_calls), 2)

    def test_truncation_keeps_mandatory_drops_style(self) -> None:
        directives = (
            "### Mandatory Checks\n"
            "- Check A\n"
            "- Check B\n\n"
            "### Style Preferences\n"
            "- Style A\n"
        )
        result = truncate_review_md_always_check_only(directives)
        self.assertIn("### Mandatory Checks", result)
        self.assertIn("- Check A", result)
        self.assertIn("- Check B", result)
        self.assertNotIn("### Style Preferences", result)
        self.assertNotIn("- Style A", result)

    def test_truncation_empty_input(self) -> None:
        self.assertEqual(truncate_review_md_always_check_only(""), "")

    def test_truncation_no_mandatory_section(self) -> None:
        text = "### Style Preferences\n- Style A\n"
        self.assertEqual(truncate_review_md_always_check_only(text), "")

    def test_skip_pattern_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(self._FULL_REVIEW_MD, encoding="utf-8")
            result = load_review_md_skip_patterns(repo)

        self.assertEqual(
            result,
            [
                "Generated files under src/gen/",
                "Vendored dependencies",
            ],
        )

    def test_skip_patterns_missing_review_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_review_md_skip_patterns(Path(tmpdir))
        self.assertEqual(result, [])

    def test_skip_patterns_no_skip_section(self) -> None:
        content = "## Always check\n- Item\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "REVIEW.md").write_text(content, encoding="utf-8")
            result = load_review_md_skip_patterns(repo)
        self.assertEqual(result, [])


class PathInstructionsTests(unittest.TestCase):
    """Tests for load_path_instructions()."""

    def test_single_pattern_match(self) -> None:
        config = {
            "path_instructions": [
                {"path": "src/auth/*", "instructions": "Focus on auth bypass."},
            ]
        }
        result = load_path_instructions(["src/auth/login.py"], config)
        self.assertIn("src/auth/*", result)
        self.assertIn("Focus on auth bypass.", result)

    def test_multiple_patterns_match_same_file(self) -> None:
        config = {
            "path_instructions": [
                {"path": "src/*.py", "instructions": "Check types."},
                {"path": "src/auth*", "instructions": "Check auth."},
            ]
        }
        result = load_path_instructions(["src/auth.py"], config)
        self.assertIn("Check types.", result)
        self.assertIn("Check auth.", result)

    def test_no_patterns_match(self) -> None:
        config = {
            "path_instructions": [
                {"path": "migrations/**", "instructions": "Check migrations."},
            ]
        }
        result = load_path_instructions(["src/app.py"], config)
        self.assertEqual(result, "")

    def test_missing_path_instructions_in_config(self) -> None:
        result = load_path_instructions(["src/app.py"], {})
        self.assertEqual(result, "")

    def test_empty_changed_files(self) -> None:
        config = {
            "path_instructions": [
                {"path": "src/*", "instructions": "Review carefully."},
            ]
        }
        result = load_path_instructions([], config)
        self.assertEqual(result, "")


class CrossFilePlanningTests(unittest.TestCase):
    """Tests for cross-file context planner (sc-xpjx)."""

    def _make_diff(self, files: list[str]) -> DiffResult:
        return DiffResult(
            mode="base",
            base_ref="main",
            merge_base="abc123",
            changed_files=files,
            diff_text="fake diff",
        )

    # --- _skip_cross_file_planning ---

    def test_skip_test_only_diffs(self) -> None:
        dr = self._make_diff(["tests/test_foo.py", "src/bar_test.go"])
        self.assertTrue(_skip_cross_file_planning(dr))

    def test_skip_docs_only_diffs(self) -> None:
        dr = self._make_diff(["README.md", "docs/guide.txt", "CHANGELOG.rst"])
        self.assertTrue(_skip_cross_file_planning(dr))

    def test_no_skip_normal_diffs(self) -> None:
        dr = self._make_diff(["src/app.py", "lib/utils.ts"])
        self.assertFalse(_skip_cross_file_planning(dr))

    def test_no_skip_mixed_test_and_impl(self) -> None:
        dr = self._make_diff(["src/app.py", "tests/test_app.py"])
        self.assertFalse(_skip_cross_file_planning(dr))

    def test_skip_empty_changed_files(self) -> None:
        dr = self._make_diff([])
        self.assertTrue(_skip_cross_file_planning(dr))

    # --- truncate_cross_file_top3_high_risk ---

    def test_truncate_keeps_first_3_sections(self) -> None:
        text = (
            "Preamble line\n"
            "#### Section A — high risk\ndetails A\n"
            "#### Section B — high risk\ndetails B\n"
            "#### Section C — medium risk\ndetails C\n"
            "#### Section D — low risk\ndetails D\n"
        )
        result = truncate_cross_file_top3_high_risk(text)
        self.assertIn("Section A", result)
        self.assertIn("Section B", result)
        self.assertIn("Section C", result)
        self.assertNotIn("Section D", result)
        self.assertIn("Preamble", result)

    def test_truncate_empty_input(self) -> None:
        self.assertEqual(truncate_cross_file_top3_high_risk(""), "")

    def test_truncate_fewer_than_3_sections(self) -> None:
        text = "#### Only one\ndetails\n"
        result = truncate_cross_file_top3_high_risk(text)
        self.assertIn("Only one", result)

    # --- build_cross_file_context ---

    def test_disabled_in_config_returns_empty(self) -> None:
        config = {**DEFAULT_CONFIG, "cross_file_planner": {"enabled": False}}
        result = build_cross_file_context(
            diff_summary="some diff",
            graph_data=None,
            functions_data={"functions": [{"name": "foo", "file": "a.py"}]},
            config=config,
        )
        self.assertEqual(result, "")

    def test_empty_functions_data_returns_empty(self) -> None:
        result = build_cross_file_context(
            diff_summary="some diff",
            graph_data=None,
            functions_data=None,
            config=DEFAULT_CONFIG,
        )
        self.assertEqual(result, "")

    def test_empty_functions_list_returns_empty(self) -> None:
        result = build_cross_file_context(
            diff_summary="some diff",
            graph_data=None,
            functions_data={"functions": []},
            config=DEFAULT_CONFIG,
        )
        self.assertEqual(result, "")

    def test_returns_sections_for_valid_functions(self) -> None:
        functions_data = {
            "functions": [
                {"name": "create_token", "file": "auth.py"},
                {"name": "verify_user", "file": "auth.py"},
            ]
        }
        result = build_cross_file_context(
            diff_summary="some diff",
            graph_data=None,
            functions_data=functions_data,
            config=DEFAULT_CONFIG,
        )
        self.assertIn("#### create_token", result)
        self.assertIn("#### verify_user", result)
        self.assertIn("callers of create_token()", result)

    def test_caps_at_10_functions(self) -> None:
        functions_data = {
            "functions": [{"name": f"func_{i}", "file": "mod.py"} for i in range(15)]
        }
        result = build_cross_file_context(
            diff_summary="diff",
            graph_data=None,
            functions_data=functions_data,
            config=DEFAULT_CONFIG,
        )
        # Should have at most 10 sections
        self.assertLessEqual(result.count("####"), 10)

    def test_skips_short_function_names(self) -> None:
        functions_data = {
            "functions": [
                {"name": "x", "file": "a.py"},
                {"name": "valid_name", "file": "b.py"},
            ]
        }
        result = build_cross_file_context(
            diff_summary="diff",
            graph_data=None,
            functions_data=functions_data,
            config=DEFAULT_CONFIG,
        )
        self.assertNotIn("#### x", result)
        self.assertIn("#### valid_name", result)


if __name__ == "__main__":
    unittest.main()
