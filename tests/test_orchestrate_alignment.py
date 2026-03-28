import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from scripts.orchestrate import (
    DEFAULT_CONFIG,
    DiffResult,
    build_parser,
    cleanup,
    extract_diff,
    extract_json_from_text,
    finalize,
    load_config,
    load_language_standards,
    load_review_instructions,
    load_spec,
    prepare,
)


class OrchestrateAlignmentTests(unittest.TestCase):
    def test_prepare_rejects_existing_non_directory_session_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session"
            session_file.write_text("not a dir", encoding="utf-8")
            args = Namespace(
                session_dir=session_file,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                range=None,
                pr=None,
                path=None,
                no_chunk=False,
                force_chunk=False,
                force_all_experts=False,
                confidence_floor=None,
                mode="base",
                timeout=1200,
            )

            result = prepare(args)

        self.assertEqual(result, 1)

    def test_prepare_parser_accepts_alignment_flags(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "prepare",
                "--session-dir",
                "/tmp/session",
                "--base",
                "main",
                "--range",
                "a..b",
                "--pr",
                "42",
                "--path",
                "src/auth",
                "--no-chunk",
                "--force-chunk",
                "--force-all-experts",
                "--confidence-floor",
                "0.8",
            ]
        )

        self.assertEqual(args.base, "main")
        self.assertEqual(args.range, "a..b")
        self.assertEqual(args.pr, 42)
        self.assertEqual(args.path, "src/auth")
        self.assertTrue(args.no_chunk)
        self.assertTrue(args.force_chunk)
        self.assertTrue(args.force_all_experts)
        self.assertEqual(args.confidence_floor, 0.8)

    def test_load_config_requires_pyyaml_when_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".codereview.yaml"
            config_path.write_text("confidence_floor: 0.8\n", encoding="utf-8")

            with mock.patch("scripts.orchestrate.yaml", None):
                with self.assertRaises(RuntimeError):
                    load_config(config_path)

    def test_extract_json_from_text_recovers_wrapped_json(self) -> None:
        self.assertEqual(
            extract_json_from_text('Here are findings:\n```json\n[{"a": 1},]\n```\n'),
            [{"a": 1}],
        )

    def test_extract_json_from_text_recovers_partial_from_truncated(self) -> None:
        # Truncated array is not valid, but the first object inside is recoverable
        result = extract_json_from_text('[{"a": 1}, {"b":')
        self.assertEqual(result, {"a": 1})

    def test_extract_diff_path_mode_errors_on_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / ".git").mkdir()

            with self.assertRaises(FileNotFoundError):
                extract_diff(repo_root=repo, mode="path", pathspec="missing.txt")

    def test_count_changed_lines_matches_exact_file_not_substring(self) -> None:
        diff_text = (
            "diff --git a/src/app.py b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/src/app.py.bak b/src/app.py.bak\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+newer\n"
        )

        from scripts.orchestrate import _count_changed_lines_for_file

        self.assertEqual(_count_changed_lines_for_file(diff_text, "src/app.py"), 2)

    def test_prepare_writes_contract_fields_and_allowlisted_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                range=None,
                pr=None,
                path=None,
                no_chunk=False,
                force_chunk=False,
                force_all_experts=False,
                confidence_floor=None,
                mode="base",
                timeout=1200,
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
                {
                    "findings": [],
                    "tool_status": {
                        "semgrep": {"status": "ran", "finding_count": 0, "note": None}
                    },
                },
                {"coverage": []},
            ]

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=side_effect
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
            ):
                result = prepare(args)

            self.assertEqual(result, 0)
            launch = json.loads(
                (session_dir / "launch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(launch["status"], "ready")
            self.assertEqual(launch["scope"], "branch")
            self.assertEqual(launch["mode"], "standard")
            self.assertEqual(launch["file_count"], 1)
            self.assertEqual(launch["diff_lines"], 2)
            self.assertIn("_config", launch)
            self.assertNotIn("config", launch)
            self.assertEqual(
                launch["_config"]["confidence_floor"],
                DEFAULT_CONFIG["confidence_floor"],
            )

    def test_finalize_writes_finalize_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            session_dir = repo_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "changed-files.txt").write_text(
                "scripts/orchestrate.py\n", encoding="utf-8"
            )
            judge_output_path = session_dir / "judge.json"
            judge_output_path.write_text(
                json.dumps({"strengths": [], "spec_gaps": [], "spec_requirements": []}),
                encoding="utf-8",
            )
            (session_dir / "launch.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "review_id": "review-123",
                        "session_dir": str(session_dir),
                        "mode": "standard",
                        "scope": "branch",
                        "base_ref": "main",
                        "head_ref": "HEAD",
                        "pr_number": None,
                        "changed_files": ["scripts/orchestrate.py"],
                        "file_count": 1,
                        "diff_lines": 2,
                        "waves": [],
                        "judge": {"output_file": str(judge_output_path.absolute())},
                        "scan_results": {"findings": []},
                        "tool_status": {
                            "semgrep": {
                                "status": "ran",
                                "finding_count": 0,
                                "note": None,
                            }
                        },
                        "_config": {"confidence_floor": 0.65},
                        "context_summary": "1 files, 2 lines, 0 experts",
                        "chunks": None,
                    }
                ),
                encoding="utf-8",
            )

            enriched = {
                "findings": [],
                "tier_summary": {"must_fix": 0, "should_fix": 0, "consider": 0},
                "dropped": {"below_confidence_floor": 0},
            }
            lifecycle = {
                "findings": [],
                "suppressed_findings": [],
                "lifecycle_summary": {
                    "new": 0,
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
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ),
            ):
                result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            finalize_payload = json.loads(
                (session_dir / "finalize.json").read_text(encoding="utf-8")
            )
            self.assertEqual(finalize_payload["status"], "complete")
            self.assertTrue(finalize_payload["json_artifact"].endswith(".json"))
            self.assertTrue(finalize_payload["markdown_artifact"].endswith(".md"))

    def test_load_review_instructions_reads_all_documented_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "REVIEW.md").write_text("always check\n", encoding="utf-8")
            (repo_root / ".github").mkdir()
            (repo_root / ".github" / "codereview.md").write_text(
                "github rules\n", encoding="utf-8"
            )
            (repo_root / ".codereview.md").write_text("local rules\n", encoding="utf-8")
            (repo_root / ".codereview.yaml").write_text(
                "custom_instructions: config rules\n", encoding="utf-8"
            )

            fake_yaml = mock.Mock()
            fake_yaml.safe_load.return_value = {"custom_instructions": "config rules"}
            with mock.patch("scripts.orchestrate.yaml", fake_yaml):
                instructions = load_review_instructions(repo_root)

            self.assertIn("always check", instructions)
            self.assertIn("github rules", instructions)
            self.assertIn("local rules", instructions)
            self.assertIn("config rules", instructions)

    def test_load_spec_truncates_large_files_with_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = Path(tmpdir) / "plan.md"
            spec_path.write_text("x" * 60_000, encoding="utf-8")

            content = load_spec(spec_path)

            self.assertIn("Spec truncated to 50KB", content)
            self.assertLessEqual(len(content.encode("utf-8")), 51_500)

    def test_load_language_standards_reads_local_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            reference = repo_root / "skills" / "codereview" / "references" / "python.md"
            reference.parent.mkdir(parents=True)
            reference.write_text("Prefer typed functions.\n", encoding="utf-8")

            with mock.patch(
                "scripts.orchestrate.detect_repo_root", return_value=repo_root
            ):
                standards = load_language_standards(["src/app.py"])

            self.assertIn("Prefer typed functions.", standards)

    def test_finalize_records_non_fatal_validation_and_lifecycle_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            session_dir = repo_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "changed-files.txt").write_text(
                "scripts/orchestrate.py\n", encoding="utf-8"
            )
            judge_output_path = session_dir / "judge.json"
            judge_output_path.write_text(
                json.dumps({"strengths": [], "spec_gaps": [], "spec_requirements": []}),
                encoding="utf-8",
            )
            (session_dir / "launch.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "review_id": "review-123",
                        "session_dir": str(session_dir),
                        "mode": "standard",
                        "scope": "branch",
                        "base_ref": "main",
                        "head_ref": "HEAD",
                        "pr_number": None,
                        "changed_files": ["scripts/orchestrate.py"],
                        "file_count": 1,
                        "diff_lines": 2,
                        "waves": [],
                        "judge": {"output_file": str(judge_output_path.absolute())},
                        "scan_results": {"findings": []},
                        "tool_status": {
                            "semgrep": {
                                "status": "ran",
                                "finding_count": 0,
                                "note": None,
                            }
                        },
                        "_config": {"confidence_floor": 0.65},
                        "context_summary": "1 files, 2 lines, 0 experts",
                        "chunks": None,
                    }
                ),
                encoding="utf-8",
            )
            enriched = {
                "findings": [],
                "tier_summary": {"must_fix": 0, "should_fix": 0, "consider": 0},
                "dropped": {"below_confidence_floor": 0},
            }
            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=repo_root
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json",
                    side_effect=[enriched, RuntimeError("lifecycle boom")],
                ),
                mock.patch(
                    "scripts.orchestrate.subprocess.run",
                    return_value=mock.Mock(
                        returncode=1,
                        stdout="validator failed",
                        stderr="validator failed",
                    ),
                ),
            ):
                result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            finalize_payload = json.loads(
                (session_dir / "finalize.json").read_text(encoding="utf-8")
            )
            self.assertEqual(finalize_payload["validation_status"], "fail")
            self.assertEqual(finalize_payload["lifecycle_status"], "fallback")
            report = json.loads(
                (session_dir / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["validation_status"], "fail")
            self.assertEqual(report["lifecycle_status"], "fallback")

    def test_cleanup_removes_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            (session_dir / "launch.json").write_text("{}", encoding="utf-8")

            result = cleanup(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            self.assertFalse(session_dir.exists())

    def test_prepare_records_timing_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                range=None,
                pr=None,
                path=None,
                no_chunk=False,
                force_chunk=False,
                force_all_experts=False,
                confidence_floor=None,
                mode="base",
                timeout=1200,
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
                {"hotspots": []},
                {"tiers": []},
                {"findings": [], "tool_status": {}},
                {"coverage": []},
            ]

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=side_effect
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
            ):
                prepare(args)

            timing_lines = (
                (session_dir / "timing.jsonl").read_text(encoding="utf-8").splitlines()
            )
            self.assertTrue(timing_lines)
            timing = json.loads(timing_lines[0])
            self.assertEqual(timing["phase"], "prepare")

    def test_prepare_force_chunk_writes_chunk_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                range=None,
                pr=None,
                path=None,
                no_chunk=False,
                force_chunk=True,
                force_all_experts=False,
                confidence_floor=None,
                mode="base",
                timeout=1200,
            )
            diff_result = DiffResult(
                mode="base",
                base_ref="main",
                merge_base="abc123",
                changed_files=[f"src/file_{index}.py" for index in range(20)],
                diff_text="\n".join(["@@", *["+line"] * 120]),
            )
            side_effect = [
                {"language": "python"},
                {"hotspots": []},
                {"tiers": []},
                {"findings": [], "tool_status": {}},
                {"coverage": []},
            ]

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=side_effect
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
            ):
                prepare(args)

            launch = json.loads(
                (session_dir / "launch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(launch["mode"], "chunked")
            self.assertIsInstance(launch["chunks"], list)
            self.assertGreaterEqual(len(launch["chunks"]), 1)

    def test_prepare_applies_spec_scope_to_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            spec_path = Path(tmpdir) / "plan.md"
            spec_path.write_text(
                "# Auth\nKeep auth token checks here.\n\n# Billing\nOnly billing should remain.\n",
                encoding="utf-8",
            )
            args = Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=str(spec_path),
                spec_scope="Billing",
                base="main",
                range=None,
                pr=None,
                path=None,
                no_chunk=False,
                force_chunk=False,
                force_all_experts=False,
                confidence_floor=None,
                mode="base",
                timeout=1200,
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
                {"hotspots": []},
                {"tiers": []},
                {"findings": [], "tool_status": {}},
                {"coverage": []},
            ]

            with (
                mock.patch(
                    "scripts.orchestrate.extract_diff", return_value=diff_result
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json", side_effect=side_effect
                ),
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=Path.cwd()
                ),
            ):
                prepare(args)

            prompt_text = (session_dir / "explorer-correctness-prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Billing", prompt_text)
            self.assertNotIn("Keep auth token checks here.", prompt_text)

    def test_finalize_chunked_report_includes_chunk_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            session_dir = repo_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "changed-files.txt").write_text(
                "src/a.py\nsrc/b.py\n", encoding="utf-8"
            )
            (session_dir / "timing.jsonl").write_text(
                json.dumps(
                    {"phase": "prepare", "start_ms": 1, "end_ms": 2, "duration_ms": 1}
                )
                + "\n",
                encoding="utf-8",
            )
            judge_output_path = session_dir / "judge.json"
            judge_output_path.write_text(
                json.dumps({"strengths": [], "spec_gaps": [], "spec_requirements": []}),
                encoding="utf-8",
            )
            (session_dir / "launch.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "review_id": "review-123",
                        "session_dir": str(session_dir),
                        "mode": "chunked",
                        "scope": "branch",
                        "base_ref": "main",
                        "head_ref": "HEAD",
                        "pr_number": None,
                        "changed_files": ["src/a.py", "src/b.py"],
                        "file_count": 2,
                        "diff_lines": 20,
                        "waves": [],
                        "judge": {"output_file": str(judge_output_path.absolute())},
                        "scan_results": {"findings": []},
                        "tool_status": {
                            "semgrep": {
                                "status": "ran",
                                "finding_count": 0,
                                "note": None,
                            }
                        },
                        "_config": {"confidence_floor": 0.65},
                        "context_summary": "2 files, 20 lines, 0 experts",
                        "chunks": [
                            {
                                "id": 1,
                                "description": "src",
                                "files": ["src/a.py", "src/b.py"],
                                "file_count": 2,
                                "diff_lines": 20,
                                "risk_tier": "standard",
                                "passes_run": 3,
                                "findings": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            enriched = {
                "findings": [],
                "tier_summary": {"must_fix": 0, "should_fix": 0, "consider": 0},
                "dropped": {"below_confidence_floor": 0},
            }
            lifecycle = {
                "findings": [],
                "suppressed_findings": [],
                "lifecycle_summary": {
                    "new": 0,
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
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ),
            ):
                result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            report = json.loads(
                (session_dir / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["review_mode"], "chunked")
            self.assertEqual(report["chunk_count"], 1)
            self.assertEqual(report["chunks"][0]["description"], "src")
            markdown = (session_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("### Review Mode: Chunked", markdown)


if __name__ == "__main__":
    unittest.main()
