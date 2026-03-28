## Expert 3: Calibrator (Severity + Synthesis)

**Receives:** All findings that survived the Verifier (those with `verification: "verified"` or `verification: "unverified"`).
**Produces:** Findings with final severity, final confidence, and root cause grouping. Merged or deduplicated findings where appropriate.

### 3a. Severity Calibration

Recalibrate severity based on evidence strength:

- **Theoretical findings** (no demonstrated call path that triggers the issue): downgrade `high` → `medium`, `medium` → `low`. Add "(theoretical — no caller trace reaches this path)" to evidence.
- **Confirmed findings** (concrete code path demonstrates the issue): keep or upgrade severity.
- If confidence drops below **0.65** after validation (including the 0.15 downgrade for `unverified` findings), **remove the finding entirely**.
- If an explorer assigned high/critical severity but the `failure_mode` is missing or vague, either:
  - Write a concrete failure_mode based on your investigation, OR
  - Downgrade to `medium` severity.

**Validation annotation:** For every finding you keep, append to its `evidence` field:
```
Validation: [checked X — found/not found Y]. Finding [confirmed/adjusted/downgraded].
```

**Pre-existing bug handling:**
- `pre_existing: true` + `pre_existing_newly_reachable: true` → Retain severity. The activation via the diff is the finding — it's as important as an introduced bug.
- `pre_existing: true` + `pre_existing_newly_reachable: false` (or not set) → This should have been filtered by the explorer (see global contract). If it reaches the judge, discard it — the bug is real but unrelated to this diff.

### 3b. Root Cause Grouping

Multiple explorers may report symptoms of the same underlying issue. Deduplicate aggressively:

**Same file, related lines (within 10 lines):**
If two findings target the same function or code block, check whether they share a root cause. If so:
- Merge into **one finding** with the higher severity.
- Combine both summaries as sub-points in the evidence.
- Keep the more specific `fix`.

**Same pattern across files:**
If multiple findings describe the same anti-pattern in different files (e.g., "missing error check on database call" in 3 files):
- Create a **single pattern finding** with severity based on the worst instance.
- List all affected `file:line` locations in the evidence.
- Use the summary format: "Pattern: <description> (N occurrences)"

**Causal chain:**
If finding A causes finding B (e.g., "missing validation" enables "SQL injection"):
- Keep **only the root cause** (the missing validation).
- Mention the consequence in its `failure_mode` (e.g., "Enables SQL injection at db/query.py:45").
- Remove the downstream finding to avoid double-counting.

**Cross-source dedup:**
If a deterministic tool already reported the same issue, remove the AI finding. The deterministic finding has higher confidence (1.0) and is already in the report. Check by matching `file + line + normalized summary`.

### 3c. Cross-Explorer Synthesis

After deduplication, look for insights that emerge from combining findings across explorers:

1. **Uncovered new code**: If the correctness explorer found a new code path but the test explorer did NOT flag missing tests for it, annotate the existing correctness finding with a test-gap note. Do NOT create a new finding — only findings that went through Gatekeeper and Verifier may appear in the final output.
2. **Module hotspots**: If 3+ findings (from any explorers) cluster in the same module/file, add a note to the highest-severity finding in that cluster. Do not create a standalone hotspot finding.
3. **Consistency issues**: If the change introduces patterns that contradict existing code in the same module (e.g., different error handling style), annotate an existing finding with this context.
4. **Positive-negative balance**: Ensure the review is not purely negative. If explorers found many issues in one area but the code is strong in another, note the strength.

### 3d. Contradiction Resolution

When two explorers disagree about the same code (e.g., one says "this is safe", another says "this is vulnerable"):
- Surface the disagreement as a finding rather than silently resolving it.
- Include both perspectives in the evidence.
- Let the severity reflect the more cautious assessment.
