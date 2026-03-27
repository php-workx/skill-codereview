# Report Template Reference

This file contains the full markdown report template for Step 6 of the codereview skill, the JSON envelope format for Step 7, and formatting rules.

---

## Markdown Report Template

```markdown
# Code Review: <target description>

**Verdict: PASS / WARN / FAIL** — <verdict reason>
**Scope:** <branch|staged|commit|pr> | **Base:** <base_ref> | **Head:** <head_ref>
**Reviewed:** <N files> | **Total findings:** <N> (from <total pre-filter> raw)

### Tool Status
| Tool | Key | Status | Findings | Note |
|------|-----|--------|----------|------|
| semgrep | semgrep | ran | 3 | — |
| trivy | trivy | sandbox_blocked | 0 | cache path permission denied |
| osv-scanner | osv_scanner | not_installed | — | go install github.com/google/osv-scanner/cmd/osv-scanner@latest |
| shellcheck | shellcheck | ran | 1 | — |
| pre-commit | pre_commit | skipped | — | no .pre-commit-config.yaml |
| sonarqube | sonarqube | not_installed | — | see skill-sonarqube README |
| radon | radon | ran | 2 hotspots | functions with complexity C or worse |
| standards | standards | ran | 0 | loaded: python, go |
| AI: correctness | ai_correctness | ran | 4 | — |
| AI: security | ai_security | ran | 2 | — |
| AI: reliability | ai_reliability | ran | 1 | — |
| AI: test adequacy | ai_test_adequacy | ran | 3 | — |
| AI: error handling | ai_error_handling | ran | 2 | — |
| AI: API/contract | ai_api_contract | skipped | — | No public API surface changes in diff |
| AI: concurrency | ai_concurrency | skipped | — | No concurrency primitives in diff |
| AI: spec verification | ai_spec_verification | ran | 5 | 12 requirements extracted, 9 implemented, 2 partial, 1 not implemented |
| AI: judge | ai_judge | ran | 8 | 4 findings removed by adversarial validation |

---

## Strengths

- <what's done well — architecture, patterns, testing>
- <another positive observation>

---

## Must Fix (N findings)

Issues that should block merge or be fixed immediately.

| # | Sev | Lifecycle | Source | File | Line | Summary | Fix |
|---|-----|-----------|--------|------|------|---------|-----|
| 1 | critical | [NEW] | AI:security | path/file.py | 42 | SQL injection | Use parameterized query |

### Finding 1: <summary>
**Category:** security | **Confidence:** 0.92 | **Source:** AI:security | **Lifecycle:** NEW
**Failure mode:** <what breaks and when>
**Fix:** <smallest safe remediation>

---

## Should Fix (N findings)

Worth addressing in this PR — fast for code agents.

| # | Sev | Lifecycle | Source | File | Line | Summary | Fix |
|---|-----|-----------|--------|------|------|---------|-----|
| ... |

---

## Consider (N findings)

Fix if convenient, or defer to a follow-up.

| # | Sev | Lifecycle | Source | File | Line | Summary |
|---|-----|-----------|--------|------|------|---------|
| ... |

---

## Spec Verification (if spec provided)

**Source:** <spec file path> | **Scope:** <spec-scope value, or "Full document">
**Requirements:** <N total> | Implemented: <N> | Partial: <N> | Not implemented: <N>

| # | Requirement | Priority | Impl | Tests | Categories | Gap |
|---|-------------|----------|------|-------|------------|-----|
| REQ-001 | <requirement text> | must | ✓ | partial | unit | Needs integration |
| REQ-002 | <requirement text> | must | ✓ | covered | unit, integration | — |
| REQ-003 | <requirement text> | should | partial | missing | — | No tests |

### Details

<expanded detail for partial/missing requirements — include impl_evidence, test_coverage details, and category_gap_reason for each requirement that is not fully implemented or fully tested>

### Spec Gaps (summary)

Requirements not fully addressed by this diff:
- [ ] <requirement text> [partial — <what's missing>]
- [ ] <requirement text> [not implemented]

---

## Test Gaps

- [ ] <test scenario 1> — `path/to/file:line`
- [ ] <test scenario 2> — `path/to/file:line`

---

## Next Steps

Suggested fix order for code agents:
1. Must Fix items (blocking) — fix in order listed
2. Should Fix items — address in this PR
3. Test gaps — add tests for uncovered paths
4. Consider items — fix if time permits

**Pushback hints:** Findings marked with confidence < 0.75 may warrant
verification before fixing. Check the codebase context — the reviewer
may have missed something. Use your judgment.

---

## Suppressed Findings (N)
- X rejected, Y deferred
- To review suppressions, see `.codereview-suppressions.json`
- To un-suppress: remove the entry from `.codereview-suppressions.json`

To suppress a finding: `/codereview suppress <finding-id> --status rejected --reason 'reason'`

---

## Summary

<1-2 sentence overall risk assessment>
**Verdict:** PASS/WARN/FAIL | **Must Fix:** N | **Should Fix:** N | **Consider:** N
```

## Formatting Rules

**Source column format:** The report's Source column combines the `source` and `pass` fields for readability. AI findings show as `AI:<pass>` (e.g., `AI:security` means `source=ai, pass=security`). Deterministic findings show the tool name (e.g., `semgrep`, `shellcheck`).

**Tool-status prose rule:** when summarizing deterministic tooling in prose, separate categories so readers can tell what happened:
- Missing tools: list only `not_installed` tools, and state explicitly that all listed tools were unavailable.
- Sandbox-blocked tools: list only `sandbox_blocked` tools with the denied path/action.
- Failed tools: list only `failed` tools (non-sandbox runtime errors).
- Never merge these into a single mixed sentence.

Example:
- `Missing tools (all unavailable): semgrep, osv-scanner, sonarqube, radon, gocyclo.`
- `Sandbox-blocked: trivy (cache path permission denied).`
- `Failed: pre-commit (hook runtime error).`

**For PR mode with inline comments:**

Ask the user: "Would you like me to post these findings as inline PR comments?"

If confirmed, use `gh api` to post review comments:
```bash
gh api repos/{owner}/{repo}/pulls/{number}/reviews \
  --method POST \
  --field body="AI Code Review — <N> findings" \
  --field event="COMMENT" \
  --field comments="[{\"path\":\"...\",\"line\":...,\"body\":\"...\"}]"
```

## Large-Diff Mode Report Additions

When `review_mode = "chunked"`, add a chunk summary table between the verdict header and the findings sections:

```markdown
### Review Mode: Chunked

| Chunk | Files | Lines | Risk | Passes Run | Findings |
|-------|-------|-------|------|-----------|----------|
| 1: src/auth/* | 12 | 1,800 | Critical | 6/6 | 14 |
| 2: src/api/orders/* | 10 | 1,500 | Standard | 4/4 | 7 |
| 3: src/api/users/* | 11 | 1,200 | Standard | 4/4 | 5 |
| Cross-chunk synthesis | — | — | — | 1 | 3 |
| **Total** | **87** | **8,247** | — | **38** | **29** |
```

The chunk summary shows how the review was distributed and how many findings survived validation at each stage. This helps users understand which areas of the codebase received the most attention and where the highest finding density is.

For standard mode reviews, omit this section entirely.

## Timing

Add the following section at the end of the markdown report (before the JSON envelope), when timing data is available:

```markdown
## Timing

| Step | Duration | % of Total |
|------|----------|------------|
| Target detection | 0.3s | 1% |
| Project discovery | 1.2s | 3% |
| Complexity analysis | 0.9s | 2% |
| Git history risk | 2.1s | 5% |
| Coverage collection | 3.4s | 8% |
| Deterministic scans | 12.3s | 27% |
| AI explorers | 18.5s | 41% |
| AI judge | 8.2s | 18% |
| Enrichment | 0.5s | 1% |
| Lifecycle | 0.4s | 1% |
| Report formatting | 1.2s | 3% |
| **Total** | **45.2s** | **100%** |

Timing data collected by `scripts/timing.sh`. Omit this section if timing data is unavailable.
```

---

## JSON Envelope Format (Step 7)

Save to `.agents/reviews/YYYY-MM-DD-<target>.json`. Must conform to `findings-schema.json`.

**Standard mode example:**

```json
{
  "run_id": "2026-02-08T14-30-00-a1b2c3",
  "timestamp": "2026-02-08T14:30:00Z",
  "review_mode": "standard",
  "scope": "branch",
  "base_ref": "main",
  "head_ref": "feature/auth-fix",
  "pr_number": null,
  "files_reviewed": ["src/auth.py", "src/middleware.py"],
  "verdict": "WARN",
  "verdict_reason": "Has should-fix issues but no blockers",
  "strengths": ["Clean separation of auth concerns", "Good test coverage for happy path"],
  "spec_gaps": [],
  "spec_requirements": [],
  "tool_status": {
    "semgrep": { "status": "ran", "version": "1.56.0", "finding_count": 3, "note": null },
    "trivy": { "status": "sandbox_blocked", "version": "0.56.0", "finding_count": 0, "note": "cache path permission denied; set TRIVY_CACHE_DIR=/tmp/trivy-cache" },
    "ai_correctness": { "status": "ran", "version": null, "finding_count": 4, "note": null },
    "ai_spec_verification": { "status": "skipped", "version": null, "finding_count": 0, "note": "No spec provided" }
  },
  "findings": [ ],
  "tier_summary": { "must_fix": 1, "should_fix": 5, "consider": 4 }
}
```

**Chunked mode example (additional fields):**

```json
{
  "review_mode": "chunked",
  "chunk_count": 8,
  "chunks": [
    {
      "id": 1,
      "description": "src/auth/*",
      "files": ["src/auth/login.py", "src/auth/session.py", "src/auth/middleware.py"],
      "file_count": 12,
      "diff_lines": 1800,
      "risk_tier": "critical",
      "passes_run": 6,
      "findings": 14
    },
    {
      "id": 2,
      "description": "src/api/orders/*",
      "files": ["src/api/orders/views.py", "src/api/orders/serializers.py"],
      "file_count": 10,
      "diff_lines": 1500,
      "risk_tier": "standard",
      "passes_run": 4,
      "findings": 7
    }
  ]
}
```

## Validation

```bash
bash scripts/validate_output.sh \
  --findings .agents/reviews/YYYY-MM-DD-<target>.json \
  --report .agents/reviews/YYYY-MM-DD-<target>.md
```
