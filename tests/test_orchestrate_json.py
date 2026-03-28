import unittest

from scripts.orchestrate import (
    dedup_exact,
    extract_json_from_text,
    parse_explorer_output,
)


class ExtractJsonFromTextTests(unittest.TestCase):
    def test_extract_direct(self) -> None:
        self.assertEqual(extract_json_from_text('[{"a": 1}]'), [{"a": 1}])

    def test_extract_markdown_fenced(self) -> None:
        self.assertEqual(
            extract_json_from_text('```json\n[{"a": 1}]\n```'),
            [{"a": 1}],
        )

    def test_extract_with_preamble(self) -> None:
        self.assertEqual(
            extract_json_from_text('Here are findings:\n[{"a": 1}]'),
            [{"a": 1}],
        )

    def test_extract_smart_quotes(self) -> None:
        self.assertEqual(
            extract_json_from_text("[{“summary”: “test”, “msg”: “use {x} here”}]"),
            [{"summary": "test", "msg": "use {x} here"}],
        )

    def test_extract_trailing_comma(self) -> None:
        self.assertEqual(extract_json_from_text('[{"a": 1},]'), [{"a": 1}])

    def test_extract_braces_in_strings(self) -> None:
        self.assertEqual(
            extract_json_from_text('[{"msg": "use {x} here"}]'),
            [{"msg": "use {x} here"}],
        )

    def test_extract_nested_arrays(self) -> None:
        self.assertEqual(
            extract_json_from_text('[{"tests": ["a", "b"]}]'),
            [{"tests": ["a", "b"]}],
        )

    def test_extract_truncated_recovers_partial(self) -> None:
        # Truncated array is not valid, but the first object inside is — extract it
        result = extract_json_from_text('[{"a": 1}, {"b":')
        self.assertEqual(result, {"a": 1})

    def test_extract_no_json_at_all(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_from_text("No JSON content here at all")

    def test_extract_empty(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_from_text("")

    def test_extract_trailing_text(self) -> None:
        self.assertEqual(
            extract_json_from_text('[{"a": 1}]\nI found 1 issue.'),
            [{"a": 1}],
        )

    def test_extract_json_after_prose_with_brace(self) -> None:
        text = 'The confidence is {high}. Here are findings:\n[{"a": 1}]'
        self.assertEqual(extract_json_from_text(text), [{"a": 1}])

    def test_extract_json_after_prose_with_bracket(self) -> None:
        text = 'See items [above] for context.\n[{"a": 1}]'
        self.assertEqual(extract_json_from_text(text), [{"a": 1}])


class DedupExactTests(unittest.TestCase):
    def test_removes_duplicates(self) -> None:
        findings = [
            {
                "file": "a.py",
                "line": 10,
                "pass": "correctness",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.7,
            },
            {
                "file": "a.py",
                "line": 10,
                "pass": "correctness",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.9,
            },
        ]

        self.assertEqual(len(dedup_exact(findings)), 1)

    def test_keeps_different_pass(self) -> None:
        findings = [
            {
                "file": "a.py",
                "line": 10,
                "pass": "correctness",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.7,
            },
            {
                "file": "a.py",
                "line": 10,
                "pass": "security",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.8,
            },
        ]

        deduped = dedup_exact(findings)
        self.assertEqual(len(deduped), 2)

    def test_keeps_higher_confidence(self) -> None:
        findings = [
            {
                "file": "a.py",
                "line": 10,
                "pass": "correctness",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.4,
            },
            {
                "file": "a.py",
                "line": 10,
                "pass": "correctness",
                "severity": "medium",
                "summary": "x",
                "confidence": 0.95,
            },
        ]

        deduped = dedup_exact(findings)
        self.assertEqual(deduped[0]["confidence"], 0.95)

    def test_empty_list(self) -> None:
        self.assertEqual(dedup_exact([]), [])


class ParseExplorerOutputTests(unittest.TestCase):
    def test_list(self) -> None:
        findings, reqs, certification, completeness_gate = parse_explorer_output(
            [{"summary": "x"}], "correctness"
        )

        self.assertEqual(reqs, [])
        self.assertIsNone(certification)
        self.assertIsNone(completeness_gate)
        self.assertEqual(findings, [{"summary": "x", "pass": "correctness"}])

    def test_dict_with_requirements(self) -> None:
        findings, reqs, certification, completeness_gate = parse_explorer_output(
            {
                "pass": "security",
                "findings": [{"summary": "y"}],
                "requirements": [{"id": "r1"}],
            },
            "security",
        )

        self.assertEqual(findings, [{"summary": "y", "pass": "security"}])
        self.assertEqual(reqs, [{"id": "r1"}])
        self.assertIsNone(certification)
        self.assertIsNone(completeness_gate)

    def test_wrong_shape(self) -> None:
        findings, reqs, certification, completeness_gate = parse_explorer_output(
            "oops", "correctness"
        )

        self.assertIsNone(findings)
        self.assertEqual(reqs, [])
        self.assertIsNone(certification)
        self.assertIsNone(completeness_gate)

    def test_list_with_non_dict_items(self) -> None:
        raw = [{"summary": "real"}, None, 42, "stray"]
        findings, _reqs, _cert, _gate = parse_explorer_output(raw, "correctness")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["summary"], "real")

    def test_dict_findings_with_non_dict_items(self) -> None:
        raw = {"findings": [{"summary": "ok"}, None], "requirements": []}
        findings, _reqs, _cert, _gate = parse_explorer_output(raw, "correctness")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["summary"], "ok")

    def test_dict_with_certification(self) -> None:
        cert_data = {"files": [{"file": "a.py", "certified": True, "reason": "ok"}]}
        raw = {
            "findings": [{"summary": "z"}],
            "certification": cert_data,
        }
        findings, reqs, certification, completeness_gate = parse_explorer_output(
            raw, "correctness"
        )

        self.assertEqual(findings, [{"summary": "z", "pass": "correctness"}])
        self.assertEqual(reqs, [])
        self.assertEqual(certification, cert_data)
        self.assertIsNone(completeness_gate)

    def test_dict_with_completeness_gate(self) -> None:
        gate_data = {"passed": True, "coverage": 0.95, "gaps": []}
        raw = {
            "findings": [],
            "completeness_gate": gate_data,
        }
        findings, reqs, certification, completeness_gate = parse_explorer_output(
            raw, "correctness"
        )

        self.assertEqual(findings, [])
        self.assertEqual(reqs, [])
        self.assertIsNone(certification)
        self.assertEqual(completeness_gate, gate_data)

    def test_dict_with_unknown_key_emits_warning(self) -> None:
        import io
        import sys

        raw = {"findings": [{"summary": "w"}], "unknown_field": "surprise"}
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            findings, reqs, certification, completeness_gate = parse_explorer_output(
                raw, "correctness"
            )
        finally:
            sys.stderr = old_stderr

        output = captured.getvalue()
        self.assertIn("unknown_field", output)
        self.assertIn("parse_explorer_output_unexpected_keys", output)
        self.assertEqual(findings, [{"summary": "w", "pass": "correctness"}])
        self.assertIsNone(certification)
        self.assertIsNone(completeness_gate)


if __name__ == "__main__":
    unittest.main()
