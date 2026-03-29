"""Tests for enrich-findings.py severity filtering."""

import importlib.util
import unittest
from pathlib import Path

# Import enrich-findings.py from the skill scripts directory
_ENRICH_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "codereview"
    / "scripts"
    / "enrich-findings.py"
)
spec = importlib.util.spec_from_file_location("enrich_findings", _ENRICH_PATH)
enrich_findings = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(enrich_findings)  # type: ignore[union-attr]


class TestMinimumSeverity(unittest.TestCase):
    """Tests for apply_minimum_severity filter."""

    def _make_finding(self, severity: str) -> dict:
        return {
            "pass": "correctness",
            "severity": severity,
            "confidence": 0.9,
            "file": "test.py",
            "line": 1,
            "summary": f"A {severity} finding",
        }

    def test_low_keeps_all(self) -> None:
        findings = [
            self._make_finding(s) for s in ("low", "medium", "high", "critical")
        ]
        kept, dropped = enrich_findings.apply_minimum_severity(findings, "low")
        self.assertEqual(len(kept), 4)
        self.assertEqual(dropped, 0)

    def test_medium_drops_low(self) -> None:
        findings = [
            self._make_finding(s) for s in ("low", "medium", "high", "critical")
        ]
        kept, dropped = enrich_findings.apply_minimum_severity(findings, "medium")
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, 1)
        self.assertNotIn("low", [f["severity"] for f in kept])

    def test_high_drops_low_and_medium(self) -> None:
        findings = [
            self._make_finding(s) for s in ("low", "medium", "high", "critical")
        ]
        kept, dropped = enrich_findings.apply_minimum_severity(findings, "high")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 2)
        severities = {f["severity"] for f in kept}
        self.assertEqual(severities, {"high", "critical"})

    def test_critical_drops_all_but_critical(self) -> None:
        findings = [
            self._make_finding(s) for s in ("low", "medium", "high", "critical")
        ]
        kept, dropped = enrich_findings.apply_minimum_severity(findings, "critical")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 3)
        self.assertEqual(kept[0]["severity"], "critical")

    def test_empty_findings(self) -> None:
        kept, dropped = enrich_findings.apply_minimum_severity([], "high")
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped, 0)

    def test_unknown_severity_treated_as_low(self) -> None:
        finding = self._make_finding("low")
        finding["severity"] = "unknown"
        kept, dropped = enrich_findings.apply_minimum_severity([finding], "medium")
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped, 1)


class TestSeverityRank(unittest.TestCase):
    """Tests for _SEVERITY_RANK ordering."""

    def test_ordering(self) -> None:
        ranks = enrich_findings._SEVERITY_RANK
        self.assertGreater(ranks["critical"], ranks["high"])
        self.assertGreater(ranks["high"], ranks["medium"])
        self.assertGreater(ranks["medium"], ranks["low"])


if __name__ == "__main__":
    unittest.main()
