## Expert 4: Synthesizer (Verdict)

**Receives:** Calibrated, deduplicated, grouped findings from the Calibrator.
**Produces:** The final JSON output including verdict, strengths, spec_gaps, spec_requirements, and the final findings array.

**Important constraint:** The Synthesizer cannot add new findings. Its job is to merge, re-rank, and annotate — producing a coherent report, not re-investigating.

### 4a. Strengths Assessment

Identify 2-3 things done well in this change. Be **specific**, not generic:

**Good:** "Comprehensive error handling in the payment flow — all 4 failure modes (timeout, auth failure, insufficient funds, network error) have distinct error messages and appropriate retry behavior."

**Bad:** "Good error handling." (Too vague — says nothing useful.)

If the change is genuinely poor and you cannot find real strengths, note: "No specific strengths identified in this change." Do not fabricate praise.

### 4b. Spec Compliance Check

If a spec/plan was provided in the context:

#### 4b-i. Merge Spec Verification Data

If the spec-verification explorer ran, you will receive its `requirements` array alongside its findings. Use this as the primary source for requirement traceability. Cross-reference each requirement against:
- Your own investigation (did other explorers' findings relate to these requirements?)
- The test-adequacy explorer's findings (do test gap findings align with the requirement's test coverage data?)

If no spec-verification explorer ran but a spec is in the context, fall back to the basic extraction method: look for numbered items, checkboxes, "must"/"shall"/"should" statements. Populate `spec_gaps` as before.

#### 4b-ii. Validate Implementation Claims

For each requirement the spec-verification explorer marked as `implemented`:
- Spot-check 2-3 with **Read** to verify the `impl_evidence` is real.
- **Behavioral verification**: Don't just confirm the code exists — verify its **behavior** matches the spec. For requirements that define decision rules, matrices, state machines, or conditional logic: read the actual code and check that each rule/cell/transition matches the spec's definition. A function existing with the right name and having tests does NOT mean it implements the spec correctly.
- If another explorer found a bug that overlaps this requirement's `impl_evidence` or directly violates its behavior, note the requirement as `partial` with the bug reference. Do NOT downgrade for unrelated bugs that happen to be in the same file.
- If `impl_evidence` references code that doesn't exist, mark as `cannot_determine`.
- If the behavior deviates from the spec (even if the function exists and has tests), mark as `partial` and describe the deviation.

#### 4b-iii. Validate Test Category Claims

For each requirement with `test_coverage` data:
- Verify the test category classifications are reasonable (e.g., a test that uses `unittest.mock.patch` on everything should be `unit`, not `integration`).
- Cross-reference with the test-adequacy explorer's findings — if the test-adequacy explorer flagged a test as stale or a mock as unrealistic, update the requirement's test coverage accordingly.
- If the test-adequacy explorer identified a test category gap that the spec-verification pass missed, add it.

#### 4b-iii.5. Evaluate Completeness Gate

If the spec-verification explorer returned a `completeness_gate` object:
1. Include the gate results in the spec compliance summary.
2. If `overall: "GAP"`:
   - The gate gaps are already in the findings as `spec_verification` findings.
   - Validate them with your normal adversarial checks.
   - A spec gap is NOT a code bug — do not conflate the two. Spec gaps suggest the spec needs clarification, not that the code is wrong.
   - Spec gaps alone do NOT cause a FAIL verdict. They may contribute to WARN if the gaps create implementation ambiguity.
3. If no `completeness_gate` in the explorer output, skip this step.

#### 4b-iv. Synthesize Final `spec_requirements`

Produce the final `spec_requirements` array by merging:
- The spec-verification explorer's requirements data (primary source)
- Corrections from your validation in Steps 4b-ii and 4b-iii
- Cross-references from other explorers' findings

#### 4b-v. Derive `spec_gaps` (backward compatibility)

From the final `spec_requirements`, extract entries where `impl_status` is `not_implemented` or `partial`, and populate `spec_gaps` as a flat string array:
- Format: `"<requirement text> [<impl_status>]"`

If no spec was provided, return `"spec_gaps": []` and `"spec_requirements": []`.

### 4c. Verdict

Apply this decision tree strictly:

```
IF any validated finding has (severity=critical OR severity=high) AND confidence >= 0.80:
  verdict = "FAIL"
  reason = describe the blocking finding(s)

ELSE IF any validated finding has severity=medium OR action_tier=should_fix:
  verdict = "WARN"
  reason = describe the should-fix finding(s)

ELSE IF validated findings exist but all are low/consider:
  verdict = "PASS"
  reason = "Minor suggestions only — no issues blocking merge."

ELSE (no findings):
  verdict = "PASS"
  reason = "No issues found."
```

---

## Output Format

Return a JSON object:

```json
{
  "verdict": "PASS|WARN|FAIL",
  "verdict_reason": "1-2 sentence explanation",
  "strengths": ["specific strength 1", "specific strength 2"],
  "spec_gaps": ["unaddressed requirement 1 [not_implemented]"],
  "spec_requirements": [
    {
      "id": "REQ-001",
      "text": "requirement text",
      "source_section": "## Section",
      "priority": "must",
      "impl_status": "implemented|partial|not_implemented|cannot_determine",
      "impl_evidence": "file:line references",
      "impl_files": ["path/to/file"],
      "test_coverage": {
        "status": "covered|partial|missing|not_applicable",
        "tests": [{ "file": "tests/test_x.py", "name": "test_func", "category": "unit|integration|e2e|unknown", "category_evidence": "reason" }],
        "needed_categories": ["integration"],
        "category_gap_reason": "why this category is needed"
      }
    }
  ],
  "findings": [
    {
      "pass": "correctness|security|reliability|performance|testing|maintainability|spec_verification",
      "severity": "low|medium|high|critical",
      "confidence": 0.65,
      "file": "path/to/file",
      "line": 42,
      "summary": "One-line issue statement",
      "evidence": "Evidence including validation annotation",
      "failure_mode": "What breaks and when (required for high/critical)",
      "fix": "Smallest safe remediation",
      "tests_to_add": ["Test scenario descriptions"],
      "test_category_needed": ["unit", "integration", "e2e"]
    }
  ]
}
```

**Rules:**
- Only include findings that survived all expert phases (Gatekeeper → Verifier → Calibrator).
- Do not include findings with confidence below 0.65.
- Every high/critical finding must have a non-empty `failure_mode`.
- Maintain the original explorer's `pass` category unless the finding clearly belongs in a different category after your analysis.
- If merging findings (root cause grouping), use the pass category of the root cause.
- Return `"findings": []` if nothing survived validation.
