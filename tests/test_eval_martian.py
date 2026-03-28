import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "eval-martian.py"
SPEC = importlib.util.spec_from_file_location("eval_martian", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
eval_martian = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = eval_martian
SPEC.loader.exec_module(eval_martian)


class EvalMartianTests(unittest.TestCase):
    def test_find_session_file_returns_none_when_claude_projects_dir_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with mock.patch.object(eval_martian.Path, "home", return_value=fake_home):
                self.assertIsNone(eval_martian._find_session_file("session-123"))

    def test_find_session_file_returns_none_when_claude_projects_path_is_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            projects_path = fake_home / ".claude" / "projects"
            projects_path.parent.mkdir(parents=True)
            projects_path.write_text("not a directory", encoding="utf-8")
            with mock.patch.object(eval_martian.Path, "home", return_value=fake_home):
                self.assertIsNone(eval_martian._find_session_file("session-123"))

    def test_run_single_review_returns_false_on_nonzero_claude_exit(self) -> None:
        pr = eval_martian.BenchmarkPR(
            pr_id="keycloak-deadbeef",
            repo_key="keycloak",
            language="java",
            pr_title="Test PR",
            url="https://example.com/pr",
            original_url="https://example.com/commit/deadbeefcafebabe",
            pr_number=0,
            golden_comments=[],
            commit_sha="deadbeefcafebabe",
        )
        git_ok = subprocess.CompletedProcess(
            ["git"], 0, " 1 file changed, 1 insertion(+)\n", ""
        )
        claude_fail = subprocess.CompletedProcess(
            ["claude"], 1, "", "rate limit exceeded"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            reviews_dir = Path(tmpdir) / "reviews"
            repo_dir.mkdir()
            reviews_dir.mkdir()
            with (
                mock.patch.object(eval_martian, "git", return_value=git_ok),
                mock.patch.object(
                    eval_martian.subprocess, "run", return_value=claude_fail
                ),
            ):
                result = eval_martian.run_single_review(
                    pr, repo_dir, reviews_dir, "sonnet"
                )

            self.assertFalse(result)
            self.assertFalse((reviews_dir / f"{pr.pr_id}.json").exists())
            raw = json.loads(
                (reviews_dir / f"{pr.pr_id}.raw.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["returncode"], 1)
            self.assertIn("rate limit exceeded", raw["stderr"])

    def test_judge_batch_raises_on_nonzero_claude_exit(self) -> None:
        failure = subprocess.CompletedProcess(["claude"], 1, "", "judge failed")
        with mock.patch.object(eval_martian.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "claude judge failed"):
                eval_martian.judge_batch([(0, 0, "golden", "candidate")], "sonnet")

    def test_aggregate_prompt_test_verdicts_enforces_one_to_one_matches(self) -> None:
        pr = eval_martian.BenchmarkPR(
            pr_id="repo-1",
            repo_key="keycloak",
            language="java",
            pr_title="Test PR",
            url="https://example.com/pr",
            original_url="https://example.com/pr/1",
            pr_number=1,
            golden_comments=[
                eval_martian.GoldenComment(comment="golden A", severity="high"),
                eval_martian.GoldenComment(comment="golden B", severity="high"),
            ],
        )
        all_findings = {
            "repo-1": [
                {"summary": "candidate 0", "evidence": ""},
                {"summary": "candidate 1", "evidence": ""},
            ]
        }
        all_verdicts = [
            ("repo-1", 0, 0, True, 0.95),
            ("repo-1", 1, 0, True, 0.90),
            ("repo-1", 0, 1, True, 0.85),
        ]

        results = eval_martian._aggregate_prompt_test_verdicts(
            {"repo-1": pr}, all_findings, all_verdicts
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tp"], 1)
        self.assertEqual(results[0]["fp"], 1)
        self.assertEqual(results[0]["fn"], 1)

    def test_prompt_test_raises_when_judge_batch_fails(self) -> None:
        pr = eval_martian.BenchmarkPR(
            pr_id="repo-1",
            repo_key="keycloak",
            language="java",
            pr_title="Test PR",
            url="https://example.com/pr",
            original_url="https://example.com/pr/1",
            pr_number=1,
            golden_comments=[
                eval_martian.GoldenComment(comment="golden A", severity="high")
            ],
        )
        with (
            mock.patch.object(eval_martian, "load_prs", return_value=[pr, pr]),
            mock.patch.object(
                eval_martian,
                "prompt_test_single",
                return_value=[{"summary": "candidate"}],
            ),
            mock.patch.object(
                eval_martian,
                "judge_batch",
                side_effect=RuntimeError("judge batch failed"),
            ),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            with mock.patch.object(eval_martian, "EVAL_DIR", Path(tmpdir)):
                args = type(
                    "Args",
                    (),
                    {
                        "prompt_file": str(
                            MODULE_PATH.parent.parent
                            / "skills"
                            / "codereview"
                            / "prompts"
                            / "reviewer-correctness-pass.md"
                        ),
                        "model": "sonnet",
                        "limit": 1,
                        "workers": 1,
                        "resume": False,
                        "repo": None,
                        "judge_model": "sonnet",
                    },
                )()
                with self.assertRaisesRegex(RuntimeError, "Judge batch failed"):
                    eval_martian.cmd_prompt_test(args)


if __name__ == "__main__":
    unittest.main()
