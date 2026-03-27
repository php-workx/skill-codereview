import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


def load_eval_owasp_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval-owasp.py"
    spec = importlib.util.spec_from_file_location("eval_owasp", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EvalOwaspTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_eval_owasp_module()

    def test_build_ai_review_prompt_uses_language_specific_examples(self) -> None:
        python_prompt = self.mod.build_ai_review_prompt(
            cwe_list="CWE-89", files_text="file body", lang="python"
        )
        java_prompt = self.mod.build_ai_review_prompt(
            cwe_list="CWE-89", files_text="file body", lang="java"
        )

        self.assertIn("configparser reads", python_prompt)
        self.assertIn("cursor.execute()", python_prompt)
        self.assertIn("HttpServletRequest.getParameter", java_prompt)
        self.assertIn("PreparedStatement", java_prompt)
        self.assertNotIn("configparser reads", java_prompt)

    def test_review_batch_raises_on_non_zero_claude_exit(self) -> None:
        test_case = self.mod.TestCase(
            name="BenchmarkTest00001",
            file_path="missing.py",
            category="sqli",
            is_vulnerable=True,
            cwe=89,
        )
        completed = subprocess.CompletedProcess(
            args=["claude"],
            returncode=1,
            stdout="",
            stderr="claude failed",
        )

        with mock.patch.object(self.mod.subprocess, "run", return_value=completed):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                self.mod.review_batch([test_case], "CWE-89", "python")

        self.assertEqual(ctx.exception.returncode, 1)
        self.assertIn("claude failed", ctx.exception.stderr)

    def test_cmd_score_reports_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            owasp_dir = root / "owasp"
            repo_dir = owasp_dir / "BenchmarkPython"
            results_dir = owasp_dir / "results"
            results_dir.mkdir(parents=True)
            (repo_dir / "testcode").mkdir(parents=True)
            (repo_dir / "expectedresults-0.1.csv").write_text(
                "BenchmarkTest00001,sqli,true,89\n", encoding="utf-8"
            )

            with mock.patch.object(self.mod, "OWASP_DIR", owasp_dir):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    ok = self.mod.cmd_score(Namespace(lang="python"))

        self.assertFalse(ok)
        self.assertIn(
            "No results to score. Run 'scan' or 'review' first.", stdout.getvalue()
        )

    def test_cmd_score_does_not_map_owasp_metrics_to_precision_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            owasp_dir = root / "owasp"
            eval_dir = root
            repo_dir = owasp_dir / "BenchmarkPython"
            results_dir = owasp_dir / "results"
            results_dir.mkdir(parents=True)
            (repo_dir / "testcode").mkdir(parents=True)
            (repo_dir / "expectedresults-0.1.csv").write_text(
                "BenchmarkTest00001,sqli,true,89\n", encoding="utf-8"
            )
            (results_dir / "scan-python-latest.json").write_text(
                json.dumps({"findings_by_test": {"BenchmarkTest00001": [89]}}),
                encoding="utf-8",
            )

            captured = {}

            class FakeStore:
                def __init__(self, _db_path):
                    pass

                def ensure_benchmark(self, *args, **kwargs):
                    return None

                def create_run(self, *args, **kwargs):
                    return "run-1"

                def update_run_metrics(self, run_id, metrics):
                    captured["run_id"] = run_id
                    captured["metrics"] = metrics

                def close(self):
                    return None

            fake_module = types.SimpleNamespace(EvalStore=FakeStore)

            with (
                mock.patch.object(self.mod, "OWASP_DIR", owasp_dir),
                mock.patch.object(self.mod, "EVAL_DIR", eval_dir),
                mock.patch.dict(sys.modules, {"eval_store": fake_module}),
            ):
                ok = self.mod.cmd_score(Namespace(lang="python"))

        self.assertTrue(ok)
        self.assertEqual(captured["run_id"], "run-1")
        self.assertIsNone(captured["metrics"].get("precision"))
        self.assertIsNone(captured["metrics"].get("recall"))
        self.assertIsNone(captured["metrics"].get("f1"))
        self.assertIn("benchmark_metrics", captured["metrics"])


if __name__ == "__main__":
    unittest.main()
