import json
import tempfile
import unittest
from pathlib import Path

from scripts.eval_store import EvalStore


class EvalStoreTests(unittest.TestCase):
    def test_update_run_metrics_persists_benchmark_metrics_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvalStore(Path(tmpdir) / "eval.db")
            store.ensure_benchmark("owasp-python", "OWASP Python")
            run_id = store.create_run("owasp-python", None)

            store.update_run_metrics(
                run_id,
                {
                    "precision": None,
                    "benchmark_metrics": {"avg_tpr": 0.5, "avg_fpr": 0.1},
                },
            )

            row = store.conn.execute(
                "SELECT precision, benchmark_metrics_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            store.close()

        self.assertIsNone(row["precision"])
        self.assertEqual(
            json.loads(row["benchmark_metrics_json"]),
            {"avg_tpr": 0.5, "avg_fpr": 0.1},
        )

    def test_import_results_uses_utf8_and_strict_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = EvalStore(tmp / "eval.db")
            store.ensure_benchmark("bench", "Bench")
            results_json = {
                "aggregate": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
                "per_pr": [
                    {
                        "pr_id": "pr-1",
                        "repo_key": "repo",
                        "language": "python",
                        "true_positives": [
                            {
                                "candidate": "Unicode summary caf\u00e9 Evidence: line",
                                "confidence": 0.9,
                                "reasoning": "match",
                            }
                        ],
                        "all_findings": [
                            {
                                "summary": "Unicode summary caf\u00e9",
                                "evidence": "line",
                                "severity": "high",
                                "file": "a.py",
                                "line": 1,
                            }
                        ],
                    }
                ],
            }
            reviews_dir = tmp / "reviews"
            reviews_dir.mkdir()
            (reviews_dir / "pr-1.raw.json").write_text(
                json.dumps({"claude_meta": {}, "elapsed_s": 1.2}),
                encoding="utf-8",
            )
            store.import_from_json("bench", results_json, reviews_dir=reviews_dir)

            verdicts = store.conn.execute(
                "SELECT COUNT(*) AS c FROM judge_verdicts"
            ).fetchone()["c"]
            store.close()

        self.assertEqual(verdicts, 1)


if __name__ == "__main__":
    unittest.main()
