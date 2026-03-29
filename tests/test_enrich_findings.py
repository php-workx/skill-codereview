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


class TestApplyCodeIntel(unittest.TestCase):
    """Tests for apply_code_intel caller enrichment."""

    def _make_finding(self, file: str, severity: str = "medium") -> dict:
        return {
            "pass": "correctness",
            "severity": severity,
            "confidence": 0.9,
            "file": file,
            "line": 10,
            "summary": f"A {severity} finding in {file}",
        }

    def test_calls_edges_with_qualified_targets(self) -> None:
        """Calls edges using filepath::funcname format should count and boost."""
        findings = [self._make_finding("scripts/foo.py", severity="medium")]
        graph = {
            "edges": [
                {"from": "a.py::f1", "to": "scripts/foo.py::my_func", "type": "calls"},
                {"from": "b.py::f2", "to": "scripts/foo.py::my_func", "type": "calls"},
                {"from": "c.py::f3", "to": "scripts/foo.py::other", "type": "calls"},
                {"from": "d.py::f4", "to": "scripts/foo.py::another", "type": "calls"},
            ],
        }
        result = enrich_findings.apply_code_intel(findings, graph)
        self.assertEqual(result[0]["affected_callers"], 4)
        # 4 > 3 triggers severity boost: medium -> high
        self.assertEqual(result[0]["severity"], "high")

    def test_only_calls_edges_counted(self) -> None:
        """Non-calls edges (co_change, imports, semantic_similarity) must be ignored."""
        findings = [self._make_finding("scripts/foo.py", severity="low")]
        graph = {
            "edges": [
                {"from": "a.py", "to": "scripts/foo.py::f", "type": "calls"},
                {"from": "b.py", "to": "scripts/foo.py", "type": "co_change"},
                {"from": "c.py", "to": "scripts/foo.py", "type": "imports"},
                {"from": "d.py", "to": "scripts/foo.py", "type": "semantic_similarity"},
                {"from": "e.py", "to": "scripts/foo.py::g", "type": "calls"},
            ],
        }
        result = enrich_findings.apply_code_intel(findings, graph)
        # Only 2 calls edges, not 5
        self.assertEqual(result[0]["affected_callers"], 2)
        # 2 <= 3 so no severity boost
        self.assertEqual(result[0]["severity"], "low")

    def test_bare_filepath_edges(self) -> None:
        """Calls edges with bare filepath (no ::funcname) should also work."""
        findings = [self._make_finding("lib/utils.py", severity="low")]
        graph = {
            "edges": [
                {"from": "a.py", "to": "lib/utils.py", "type": "calls"},
                {"from": "b.py", "to": "lib/utils.py", "type": "calls"},
                {"from": "c.py", "to": "lib/utils.py", "type": "calls"},
                {"from": "d.py", "to": "lib/utils.py", "type": "calls"},
            ],
        }
        result = enrich_findings.apply_code_intel(findings, graph)
        self.assertEqual(result[0]["affected_callers"], 4)
        # 4 > 3 triggers boost: low -> medium
        self.assertEqual(result[0]["severity"], "medium")

    def test_empty_graph_returns_findings_unchanged(self) -> None:
        """Empty graph should leave findings untouched."""
        findings = [self._make_finding("x.py", severity="high")]
        result = enrich_findings.apply_code_intel(findings, {})
        self.assertEqual(result[0]["severity"], "high")
        self.assertNotIn("affected_callers", result[0])

    def test_no_callers_sets_zero(self) -> None:
        """File not referenced by any calls edge gets caller_count 0."""
        findings = [self._make_finding("unrelated.py", severity="medium")]
        graph = {
            "edges": [
                {"from": "a.py", "to": "other.py::func", "type": "calls"},
            ],
        }
        result = enrich_findings.apply_code_intel(findings, graph)
        self.assertEqual(result[0]["affected_callers"], 0)
        self.assertEqual(result[0]["severity"], "medium")


if __name__ == "__main__":
    unittest.main()
