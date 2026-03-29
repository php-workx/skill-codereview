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

from scripts.orchestrate import cleanup, derive_verdict, finalize, post_explorers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class PostExplorersPhaseTests(unittest.TestCase):
    def test_post_explorers_noops_for_empty_launch_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            (session_dir / "launch.json").write_text(
                json.dumps({"status": "empty", "session_dir": str(session_dir)}),
                encoding="utf-8",
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(judge_input["status"], "skipped")
            self.assertEqual(judge_input["reason"], "Launch packet status is empty")

    def test_post_explorers_reports_missing_invalid_and_wrong_shape_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            judge_prompt_file = REPO_ROOT / "skills" / "codereview" / "prompts"
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
                                "name": "missing",
                                "output_file": str(
                                    (session_dir / "explorer-missing.json").absolute()
                                ),
                            },
                            {
                                "name": "wrong-shape",
                                "output_file": str(
                                    (
                                        session_dir / "explorer-wrong-shape.json"
                                    ).absolute()
                                ),
                            },
                            {
                                "name": "invalid-json",
                                "output_file": str(
                                    (
                                        session_dir / "explorer-invalid-json.json"
                                    ).absolute()
                                ),
                            },
                        ],
                    }
                ],
                "judge": {
                    "prompt_file": str(judge_prompt_file.absolute()),
                    "output_file": str((session_dir / "judge.json").absolute()),
                },
                "scan_results": {"tool_status": {}},
                "_config": {"confidence_floor": 0.65},
            }
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            correctness_output = [
                {
                    "summary": "duplicate finding",
                    "file": "scripts/orchestrate.py",
                    "line": 42,
                    "severity": "high",
                    "confidence": 0.92,
                },
                {
                    "summary": "duplicate finding",
                    "file": "scripts/orchestrate.py",
                    "line": 42,
                    "severity": "high",
                    "confidence": 0.60,
                },
                {
                    "summary": "below threshold finding",
                    "file": "scripts/orchestrate.py",
                    "line": 99,
                    "severity": "medium",
                    "confidence": 0.45,
                },
            ]
            (session_dir / "explorer-correctness.json").write_text(
                json.dumps(correctness_output),
                encoding="utf-8",
            )
            (session_dir / "explorer-security.json").write_text(
                json.dumps({"findings": "bad"}),
                encoding="utf-8",
            )
            (session_dir / "explorer-wrong-shape.json").write_text(
                json.dumps({"findings": "bad"}),
                encoding="utf-8",
            )
            (session_dir / "explorer-invalid-json.json").write_text(
                "not json",
                encoding="utf-8",
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)

            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(judge_input["status"], "ready_for_judge")
            self.assertEqual(judge_input["raw_finding_count"], 3)
            self.assertEqual(judge_input["explorer_finding_count"], 1)
            self.assertEqual(judge_input["spec_requirements"], [])
            self.assertEqual(
                judge_input["explorer_status"]["correctness"],
                {"status": "ok", "findings": 3},
            )
            self.assertEqual(
                judge_input["explorer_status"]["security"],
                {"status": "wrong_shape", "findings": 0},
            )
            self.assertEqual(
                judge_input["explorer_status"]["missing"],
                {"status": "missing", "findings": 0},
            )
            self.assertEqual(
                judge_input["explorer_status"]["wrong-shape"],
                {"status": "wrong_shape", "findings": 0},
            )
            self.assertEqual(
                judge_input["explorer_status"]["invalid-json"]["status"], "invalid_json"
            )
            self.assertEqual(
                judge_input["explorer_status"]["invalid-json"]["findings"], 0
            )
            self.assertTrue(judge_input["explorer_status"]["invalid-json"].get("error"))
            self.assertEqual(len(judge_input["findings"]), 1)
            self.assertEqual(judge_input["findings"][0]["confidence"], 0.92)
            self.assertIn("pass", judge_input["findings"][0])
            self.assertTrue((session_dir / "judge-prompt.md").exists())
            judge_prompt = (session_dir / "judge-prompt.md").read_text(encoding="utf-8")
            self.assertIn("Expert 0.5", judge_prompt)  # from judge-main
            self.assertIn("Gatekeeper", judge_prompt)  # from judge-gatekeeper
            self.assertIn("| Explorer |", judge_prompt)  # summary table

    def test_post_explorers_respects_non_default_confidence_floor(self) -> None:
        """Verify that a non-default confidence_floor in _config actually filters findings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            judge_prompt_file = REPO_ROOT / "skills" / "codereview" / "prompts"
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
                        ],
                    }
                ],
                "judge": {
                    "prompt_file": str(judge_prompt_file.absolute()),
                    "output_file": str((session_dir / "judge.json").absolute()),
                },
                "scan_results": {"tool_status": {}},
                "_config": {"confidence_floor": 0.80},
            }
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            # pre_filter_floor = max(0.80 - 0.15, 0.40) = 0.65
            # Findings at 0.70 should survive (>= 0.65 pre-filter floor)
            # Findings at 0.60 should be dropped (< 0.65 pre-filter floor)
            findings = [
                {
                    "summary": "high confidence finding",
                    "file": "app.py",
                    "line": 10,
                    "severity": "high",
                    "confidence": 0.90,
                },
                {
                    "summary": "medium confidence finding above pre-filter",
                    "file": "app.py",
                    "line": 20,
                    "severity": "medium",
                    "confidence": 0.70,
                },
                {
                    "summary": "low confidence finding below pre-filter",
                    "file": "app.py",
                    "line": 30,
                    "severity": "low",
                    "confidence": 0.60,
                },
            ]
            (session_dir / "explorer-correctness.json").write_text(
                json.dumps(findings), encoding="utf-8"
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(judge_input["raw_finding_count"], 3)
            # Only findings with confidence >= 0.65 (pre_filter_floor) should survive
            self.assertEqual(judge_input["explorer_finding_count"], 2)
            confidences = [f["confidence"] for f in judge_input["findings"]]
            self.assertIn(0.90, confidences)
            self.assertIn(0.70, confidences)
            self.assertNotIn(0.60, confidences)

    def test_post_explorers_caps_findings_at_50(self) -> None:
        """When more than 50 findings exist, post_explorers keeps the top 50 by confidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            judge_prompt_file = REPO_ROOT / "skills" / "codereview" / "prompts"

            # Create 51 findings with distinct confidence values (0.50 .. 1.00 step 0.01)
            findings_51 = [
                {
                    "summary": f"Finding {i}",
                    "file": "app.py",
                    "line": i,
                    "severity": "medium",
                    "confidence": round(0.50 + i * 0.01, 2),
                }
                for i in range(51)
            ]
            (session_dir / "explorer-bulk.json").write_text(
                json.dumps(findings_51), encoding="utf-8"
            )

            launch_packet = {
                "session_dir": str(session_dir),
                "waves": [
                    {
                        "wave": 1,
                        "tasks": [
                            {
                                "name": "bulk",
                                "output_file": str(
                                    (session_dir / "explorer-bulk.json").absolute()
                                ),
                            },
                        ],
                    }
                ],
                "judge": {
                    "prompt_file": str(judge_prompt_file.absolute()),
                    "output_file": str((session_dir / "judge.json").absolute()),
                },
                "scan_results": {"tool_status": {}},
                "_config": {"confidence_floor": 0.65},
            }
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(judge_input["explorer_finding_count"], 50)
            confidences = [f["confidence"] for f in judge_input["findings"]]
            # The lowest surviving confidence should be 0.51, since 0.50 is the
            # 51st entry and gets dropped when the list is capped at 50.
            self.assertEqual(len(confidences), 50)
            self.assertNotIn(0.50, confidences)
            self.assertIn(0.51, confidences)
            self.assertIn(1.00, confidences)
            # Verify sorted descending (highest first)
            self.assertEqual(confidences, sorted(confidences, reverse=True))

    def _make_post_explorers_launch_packet(self, session_dir: Path) -> dict:
        """Helper: minimal launch packet for post_explorers tests."""
        judge_prompt_file = REPO_ROOT / "skills" / "codereview" / "prompts"
        return {
            "session_dir": str(session_dir),
            "waves": [
                {
                    "wave": 1,
                    "tasks": [
                        {
                            "name": "security",
                            "output_file": str(
                                (session_dir / "explorer-security.json").absolute()
                            ),
                        },
                    ],
                }
            ],
            "judge": {
                "prompt_file": str(judge_prompt_file.absolute()),
                "output_file": str((session_dir / "judge.json").absolute()),
            },
            "scan_results": {"tool_status": {}},
            "_config": {"confidence_floor": 0.65},
        }

    def test_post_explorers_emits_certification_warning_for_empty_files_checked(
        self,
    ) -> None:
        """certification_warnings appears when files_checked is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            explorer_output = {
                "certification": {
                    "status": "clean",
                    "files_checked": [],
                    "checks_performed": [],
                    "tools_used": [],
                },
                "findings": [],
            }
            (session_dir / "explorer-security.json").write_text(
                json.dumps(explorer_output), encoding="utf-8"
            )
            launch_packet = self._make_post_explorers_launch_packet(session_dir)
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertIn("certification_warnings", judge_input)
            self.assertTrue(
                any("files_checked" in w for w in judge_input["certification_warnings"])
            )

    def test_post_explorers_no_certification_warning_when_files_checked_populated(
        self,
    ) -> None:
        """No certification_warnings when files_checked has entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")

            explorer_output = {
                "certification": {
                    "status": "clean",
                    "files_checked": ["src/app.py"],
                    "checks_performed": ["Checked callers"],
                    "tools_used": ["Grep: callers of login()"],
                },
                "findings": [],
            }
            (session_dir / "explorer-security.json").write_text(
                json.dumps(explorer_output), encoding="utf-8"
            )
            launch_packet = self._make_post_explorers_launch_packet(session_dir)
            (session_dir / "launch.json").write_text(
                json.dumps(launch_packet), encoding="utf-8"
            )

            result = post_explorers(Namespace(session_dir=session_dir))

            self.assertEqual(result, 0)
            judge_input = json.loads(
                (session_dir / "judge-input.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("certification_warnings", judge_input)


class DeriveVerdictTests(unittest.TestCase):
    def test_derive_verdict_warn_when_should_fix_present(self) -> None:
        """derive_verdict returns WARN when should_fix > 0 and must_fix == 0."""
        findings = [
            {"summary": "issue A", "action_tier": "should_fix"},
            {"summary": "issue B", "action_tier": "should_fix"},
            {"summary": "minor C", "action_tier": "consider"},
        ]
        tier_summary = {"must_fix": 0, "should_fix": 2, "consider": 1}

        verdict, reason = derive_verdict(findings, tier_summary)

        self.assertEqual(verdict, "WARN")
        self.assertIn("2", reason)

    def test_derive_verdict_pass_with_consider_only(self) -> None:
        """derive_verdict returns PASS with 'Minor suggestions' when only consider findings exist."""
        findings = [
            {"summary": "minor A", "action_tier": "consider"},
        ]
        tier_summary = {"must_fix": 0, "should_fix": 0, "consider": 1}

        verdict, reason = derive_verdict(findings, tier_summary)

        self.assertEqual(verdict, "PASS")
        self.assertIn("Minor suggestions", reason)


class FinalizePhaseTests(unittest.TestCase):
    def test_finalize_falls_back_when_lifecycle_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            session_dir = repo_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            (session_dir / "changed-files.txt").write_text(
                "scripts/orchestrate.py\n", encoding="utf-8"
            )

            judge_output_path = session_dir / "judge.json"
            judge_output_path.write_text(
                json.dumps(
                    {
                        "strengths": ["Prompt assembly is deterministic."],
                        "spec_gaps": [],
                        "spec_requirements": [],
                    }
                ),
                encoding="utf-8",
            )

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
                "scan_results": {"tool_status": {}, "findings": []},
                "judge": {"output_file": str(judge_output_path.absolute())},
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

            with (
                mock.patch(
                    "scripts.orchestrate.detect_repo_root", return_value=repo_root
                ),
                mock.patch(
                    "scripts.orchestrate.run_subprocess_json",
                    side_effect=[enriched, RuntimeError("boom")],
                ),
                mock.patch(
                    "scripts.orchestrate.subprocess.run",
                    return_value=subprocess.CompletedProcess(["bash"], 0, "", ""),
                ),
            ):
                result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            report = json.loads(
                (session_dir / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["verdict"], "FAIL")
            self.assertEqual(report["lifecycle_summary"]["new"], 1)
            self.assertEqual(report["tool_status"]["semgrep"]["finding_count"], 1)
            self.assertEqual(report["strengths"], ["Prompt assembly is deterministic."])
            self.assertTrue((session_dir / "report.md").exists())

    def test_finalize_noops_for_non_ready_launch_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / ".codereview-session").write_text("1", encoding="utf-8")
            (session_dir / "launch.json").write_text(
                json.dumps({"status": "empty", "session_dir": str(session_dir)}),
                encoding="utf-8",
            )

            result = finalize(Namespace(session_dir=session_dir, judge_output=None))

            self.assertEqual(result, 0)
            finalize_json = json.loads(
                (session_dir / "finalize.json").read_text(encoding="utf-8")
            )
            self.assertEqual(finalize_json["status"], "skipped")
            self.assertEqual(finalize_json["reason"], "Launch packet status is empty")

    def test_cleanup_refuses_directory_without_session_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "session"
            session_dir.mkdir()
            (session_dir / "keep.txt").write_text("data", encoding="utf-8")

            result = cleanup(Namespace(session_dir=session_dir))

            self.assertEqual(result, 1)
            self.assertTrue(session_dir.exists())
            self.assertTrue((session_dir / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
