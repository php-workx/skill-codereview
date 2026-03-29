# Spec: Verification Architecture (F0 + F1 + F5)

**Status:** Draft — extracted from `docs/plan-verification-pipeline.md`
**Author:** Research session 2026-03-28
**Depends on:** orchestrate.py (existing), SKILL.md (existing)
**Source plan:** `docs/plan-verification-pipeline.md` Features 0, 1, 5
**Related:** Spec A (expert selection), Spec B (per-expert enrichment)

## Problem

The judge currently does everything in one pass: verify findings, check contradictions, deduplicate, calibrate severity, and produce a verdict. This overloads a single LLM call with two cognitively distinct tasks — verification (is this finding real?) and synthesis (what's the verdict?). The result: false positives leak through because the judge is simultaneously trying to verify evidence and compose a coherent narrative.

## Goals

1. **Add a 3-stage verification pipeline** between explorers and the judge that individually assesses each finding as `confirmed`, `false_positive`, or `needs_investigation`
2. **Strengthen the existing judge panel handoffs** — Gatekeeper receives pre-verified findings, Verifier handles `needs_investigation` — so verification completes before narrative construction begins
3. **Validate suggested fixes** so broken code suggestions don't erode trust in the review

## Non-Goals

- Multi-model spot-check (Feature 4 — deferred pending empirical data)
- Changing the explorer output schema
- Changing which experts run (that's Spec A)

---

## Design

### Feature 0: Dedicated Finding Verification Round

Three stages between explorers and judge, inspired by Kodus-AI's 3-stage safeguard pipeline.

```
Explorers (parallel)
    │
    ▼
Stage 1: Feature Extraction (batch LLM call — one call for ALL findings)
    │  Extract 11 boolean features per finding
    ▼
Stage 2: Deterministic Triage (no LLM — pure logic)
    │  Apply rules: discard obvious false positives, route rest to verification
    ▼
Stage 3: Verification Agent (LLM with tools — only for "verify" findings)
    │  Per-finding: read cited code, search for defenses, assign verdict
    ▼
Judge (receives only confirmed + needs_investigation findings)
```

#### Stage 1: Feature Extraction

**One batch LLM call** extracts structured boolean features for all findings. Cheap (one call, not per-finding).

New prompt: `prompts/reviewer-feature-extractor.md`

**13 boolean features per finding:**

| Feature | Category | What it detects |
|---------|----------|----------------|
| `has_resource_leak` | Structural defect | File/connection/memory not closed on all paths |
| `has_inconsistent_contract` | Structural defect | Function behavior doesn't match signature/docstring/callers |
| `has_wrong_algorithm` | Structural defect | Algorithm produces incorrect results for valid inputs |
| `has_data_exposure` | Structural defect | Sensitive data written to logs, responses, or errors |
| `has_missing_error_handling` | Structural defect | Error path leads to crash, corruption, or undefined state |
| `has_redundant_work_in_loop` | Structural defect | Repeated I/O, allocation, or computation inside a loop |
| `has_unsafe_data_flow` | Structural defect | Untrusted input reaches sensitive operation without validation |
| `has_concurrency_issue` | Structural defect | Race condition, deadlock, shared mutable state without synchronization |
| `requires_assumed_behavior` | Speculation | Finding depends on how unseen/imported code behaves |
| `is_quality_opinion` | Opinion | Style/preference/best-practice, not a defect |
| `targets_unchanged_code` | Scope violation | References code not changed in the diff |
| `targets_test_code` | Low-value | Finding is about test file code (mock setup, fixture, test helper) |
| `duplicates_linter_result` | Redundancy | Finding restates what a linter/tool already caught |

Derived from Kodus-AI's 13-feature SafeguardFeatureSet, adapted for our use case. Key differences:
- Renamed `requires_assumed_workload` to `requires_assumed_behavior` (broader scope)
- Renamed `is_anti_pattern_only` to `is_quality_opinion` (clearer intent)
- Added `has_concurrency_issue` — race conditions need a structural feature; without it they hit no structural feature and survive triage only by accident
- Added `targets_test_code` — test files generate disproportionate low-value findings; detectable from file paths (`test`, `spec`, `__tests__`, `fixtures`) and code patterns (`assert`, `mock`, `@pytest.fixture`)
- Added `duplicates_linter_result` — catches linter duplicates at triage (free) instead of in the judge (expensive)

**Batch size limit:** Max 15 findings per extraction call. If more findings exist, split into multiple calls. At 30+ findings, extraction quality degrades significantly (attention fatigue on later items).

**Feature extraction prompt** (`prompts/reviewer-feature-extractor.md`):

```markdown
You are a finding feature extractor. For each finding, extract boolean features
that describe its nature. Do NOT make keep/discard decisions — just extract features.

Rules:
- Only set structural defect features to true if the finding points to specific
  lines in the diff where the defect occurs.
- Set `has_concurrency_issue` to true if the finding describes a race condition,
  deadlock, or unsynchronized shared mutable state.
- Set `requires_assumed_behavior` to true if verifying the finding requires knowing
  how code you cannot see behaves (imported functions, external APIs, database schema).
  Exception: if the defect is structural (unsafe for ANY input), it's a defect even
  if some context is assumed.
- Set `is_quality_opinion` to true for: naming suggestions, style preferences,
  "consider using X instead of Y" without a concrete bug, refactoring suggestions,
  documentation suggestions.
- Set `targets_unchanged_code` to true if the finding's cited lines are all
  in unchanged code (- lines or context lines, not + lines).
- Set `targets_test_code` to true if the finding's file path contains `test`,
  `spec`, `__tests__`, or `fixtures`, OR the code uses test framework patterns
  (assert, mock, @pytest.fixture, describe/it).
- Set `duplicates_linter_result` to true if the finding restates what a
  deterministic tool (semgrep, shellcheck, eslint, etc.) would catch.
- When uncertain, prefer false for structural features (avoids false keeps)
  and true for speculation/low-value features (avoids false keeps).

Output ONLY the JSON below. No commentary before or after.
```

**Calibration examples** (include in the prompt for consistent extraction):

```
Example 1 — Nil map write on error path:
Finding: "Map m is nil when skip path is taken, write on line 45 will panic"
Features: { "has_resource_leak": false, "has_inconsistent_contract": false,
  "has_wrong_algorithm": false, "has_data_exposure": false,
  "has_missing_error_handling": true, "has_redundant_work_in_loop": false,
  "has_unsafe_data_flow": false, "has_concurrency_issue": false,
  "requires_assumed_behavior": false, "is_quality_opinion": false,
  "targets_unchanged_code": false, "targets_test_code": false,
  "duplicates_linter_result": false }

Example 2 — Style suggestion in test file:
Finding: "Consider using table-driven tests instead of repeated assertions"
Features: { "has_resource_leak": false, "has_inconsistent_contract": false,
  "has_wrong_algorithm": false, "has_data_exposure": false,
  "has_missing_error_handling": false, "has_redundant_work_in_loop": false,
  "has_unsafe_data_flow": false, "has_concurrency_issue": false,
  "requires_assumed_behavior": false, "is_quality_opinion": true,
  "targets_unchanged_code": false, "targets_test_code": true,
  "duplicates_linter_result": false }
```

**Output format:**
```json
{
  "findings": [
    { "finding_index": 0, "features": { "has_resource_leak": false, ... } },
    ...
  ]
}
```

#### Stage 2: Deterministic Triage

**No LLM call.** Pure logic in `scripts/triage-findings.py` (or added to `enrich-findings.py`).

```python
STRUCTURAL_DEFECT_FEATURES = [
    'has_resource_leak', 'has_inconsistent_contract', 'has_wrong_algorithm',
    'has_data_exposure', 'has_missing_error_handling', 'has_redundant_work_in_loop',
    'has_unsafe_data_flow', 'has_concurrency_issue'
]

def triage(features: dict) -> str:
    has_structural = any(features.get(f) for f in STRUCTURAL_DEFECT_FEATURES)
    has_speculation = features.get('requires_assumed_behavior', False)
    has_hard_discard = (
        features.get('is_quality_opinion', False)
        or features.get('targets_unchanged_code', False)
        or features.get('targets_test_code', False)
        or features.get('duplicates_linter_result', False)
    )

    # Rule 1: Hard discard — quality opinions, out-of-scope, test-only, linter dupes
    if has_hard_discard:
        return 'discard'

    # Rule 2: Speculation + structural — ambiguous, needs verification
    if has_speculation and has_structural:
        return 'verify'

    # Rule 3: Speculation only — may still be real (e.g., unhandled API error),
    #          send to verification rather than discarding
    if has_speculation and not has_structural:
        return 'verify'

    # Rule 4: Structural defect without speculation — may be mitigated
    if has_structural and not has_speculation:
        return 'verify'

    # Rule 5: No signals — conservative, send to verification
    return 'verify'
```

**Index validation:** Before triage runs, validate that Stage 1 output `finding_index` values match the input finding indices. If misaligned (off-by-one, reordered, missing), log a warning and fall back to sending all findings to verification (skip triage).

**Triage logging:** Log every triage decision with the features and rule that fired. This enables calibrating triage rules over time — without it, discards are invisible and rules can't be tuned.

```python
# Log format per finding:
progress("triage_decision", finding_index=i, decision="discard",
         rule="rule_1_hard_discard", features={"is_quality_opinion": True, ...})
```

**No `keep` outcome.** All non-discarded findings go through verification. Even "obvious" structural defects may be mitigated by code the explorer didn't see. The verification agent fast-tracks clear defects anyway.

**Triage routing:**

| Triage Decision | Action |
|----------------|--------|
| `discard` | Drop finding. Log count in `dropped.triage_discarded`. |
| `verify` | Pass to Stage 3 (verification agent). |

#### Stage 3: Verification Agent

For findings routed to `verify`. LLM with tool access (Read, Grep, Glob).

New prompt: `prompts/reviewer-verifier.md`

**Verifier prompt preamble:**

```markdown
You are a finding verifier. Your job is to check whether each explorer finding
is real. You are skeptical by default — you are looking for reasons the finding
is WRONG, not reasons it is right.
```

**Execution model:** One LLM call per finding. Each finding needs independent tool use to read different code locations. All findings routed to `verify` by triage are verified — no artificial cap.

**Tool budget:** Up to 10 tool calls per finding (Read, Grep, Glob). This is enough for cross-module defense tracing (read cited line → check caller → check caller's caller → check middleware → check config).

**Per finding:**
1. **Read the code.** Use Read to examine the file and line cited in the finding. If the file or line doesn't exist, verdict: `false_positive`.
2. **Check the claim.** Does the code actually do what the finding says?
   - If the finding says "nil map write panics" — is the map actually nil at that point?
   - If the finding says "SQL injection" — is the input actually user-controlled?
   - If the finding says "missing error check" — is there a check the explorer missed?
3. **Search for defenses.** Use Grep to actively search for evidence that DISPROVES the finding. Search strategies by defect type:
   - **Resource leak** → search for close()/release()/defer in same function and callers
   - **Wrong algorithm** → trace with concrete input values, verify output is wrong
   - **Race condition** → search for concurrent callers, check if lock/mutex exists
   - **Missing error handling** → check if error is handled by caller or middleware
   - **Interface change** → search for callers, verify they handle the new behavior
   - **Dead code path** → search for actual callers that reach the path
   - **Unsafe data flow** → search for input validation before the cited line, sanitization middleware
4. **Generate verification evidence.** For each finding you verify, produce a `verification_command` — a concrete grep/read command that anyone can run to confirm the finding independently. This is not optional for `confirmed` findings. Examples:
   - `grep -n 'validate_token' src/auth/*.py` → "No validation function found in auth module"
   - `Read src/api/handler.py:42-55` → "Error return on line 48 is caught by middleware at line 12"
   - `grep -rn 'defer.*Close' src/db/` → "Connection.Close() is deferred in all 3 callers"
   Inspired by CodeRabbit's verification agent: "comments come with receipts."
5. **Assign verdict:** `confirmed` | `false_positive` | `needs_investigation`

**Default verdict:** If after using up to 10 tool calls you cannot find concrete evidence that the defect exists AND you cannot find evidence that disproves it, assign `false_positive`. Err on the side of discarding — it's better to miss a speculative finding than to burden the judge with noise.

**`needs_investigation` meaning:** The verifier has exhausted its 10-call budget AND believes the finding is likely real but cannot produce a `verification_command` that proves it. This is a genuine dead end — complex dynamic dispatch, runtime configuration, external system behavior — not a budget constraint. The judge does NOT re-investigate with tools; it makes a keep/drop judgment based on the verifier's notes about what was checked and what remained unclear.

**Output:** Produce ONLY the JSON array below. No commentary before or after. Use lowercase verdict strings exactly: `confirmed`, `false_positive`, `needs_investigation`.

```json
[
  { "finding_index": 0, "verdict": "confirmed", "reason": "...", "verification_command": "grep ..." },
  { "finding_index": 1, "verdict": "false_positive", "reason": "...", "verification_command": null }
]
```

#### Activation Threshold

| Condition | Behavior |
|-----------|----------|
| Explorer findings ≤ 5 | Skip Stage 3 (judge handles directly). Stages 1-2 still run (cheap). |
| Explorer findings 6-30 | Full 3-stage verification |
| Explorer findings > 30 (chunked) | Full 3-stage verification (always) |
| `--no-verify` flag | Skip all verification |
| `--force-verify` flag | Always run full verification |

Configurable:
```yaml
verification:
  threshold: 6
  always_triage: true    # always run Stages 1-2 even below threshold
  always_in_chunked: true
```

#### Chunked Mode Interaction

In chunked mode (large diffs split into file-based chunks), verification runs **per-chunk** — after each chunk's explorers complete, before the final cross-chunk judge. This is where verification has the most value: chunked reviews produce 50-100+ findings across all chunks, and the per-chunk verification filters noise before the final judge sees the combined output.

- **Step 4-L (chunked):** Each chunk runs its own Stage 1-2-3 verification pipeline
- **Final judge:** Receives pre-verified findings from all chunks
- Verification is always active in chunked mode when `always_in_chunked: true` (default), regardless of the per-chunk finding count
- **Cross-chunk consistency:** The same defect pattern could get `confirmed` in chunk A but `false_positive` in chunk B because different context is available per chunk. The final judge should cross-reference verdicts for similar patterns across chunks and flag inconsistencies. If the same pattern (e.g., "missing nil check on map write") appears in multiple chunks with contradictory verdicts, the judge should investigate the discrepancy and prefer the `confirmed` verdict (false negatives are worse than false positives at the judge stage).

#### SKILL.md Integration

New **Step 4a.5** between explorer collection (Step 4a) and judge launch (Step 4b):

```
Step 4a.5: Finding Verification (when threshold met)

Stage 1: Feature Extraction
- Launch feature extractor with all explorer findings (combined)
- One batch LLM call
- Prompt: prompts/reviewer-feature-extractor.md

Stage 2: Deterministic Triage
- Run triage logic (scripts/triage-findings.py)
- Route: discard (drop) or verify (Stage 3)
- Log: "Triage: 12 findings → 3 discarded, 9 to verify"

Stage 3: Verification Agent (if threshold met)
- Launch verifier with "verify" findings
- Tools: Read, Grep, Glob
- Prompt: prompts/reviewer-verifier.md
- Verdict per finding: confirmed | false_positive | needs_investigation

Filter:
- confirmed → pass to judge
- false_positive → drop (log count)
- needs_investigation → pass to judge with flag

Update the finding count summary for the judge prompt to reflect
post-verification counts (e.g., "12 findings from 5 explorers → 3 discarded
by triage, 2 false positives from verification → 7 findings for judge review").
```

**Step 4a unchanged** — explorers still produce findings as today. Verification is a new step between explorer collection and judge launch.

Judge prompt gains a note: "These findings have been pre-verified. Findings marked `needs_investigation` require your deeper analysis. Do not re-verify `confirmed` findings — focus on synthesis, deduplication, and verdict."

### Feature 1: Strengthen Judge Panel Handoffs (replaces Two-Pass Judge)

The original design proposed restructuring the judge into two explicit passes (verify then synthesize). Per persona review feedback, this is **redundant** with the existing 4-stage Expert Panel (Gatekeeper → Verifier → Calibrator → Synthesizer) which already enforces verify-before-synthesize sequentially.

Instead of adding a new abstraction layer, strengthen the existing handoffs:

1. **Gatekeeper → Verifier handoff:** When Feature 0 is active, the Gatekeeper receives pre-verified findings. Add to Gatekeeper instructions: "These findings have been pre-verified by a dedicated verification agent. Findings marked `confirmed` do not need re-verification — focus on the 6 auto-discard rules. Findings marked `needs_investigation` should be passed to the Verifier for deeper analysis based on the verifier's notes."

2. **Verifier handling of `needs_investigation`:** The judge's Verifier stage investigates `needs_investigation` findings using the verifier's notes (what was checked, what remained unclear). It applies judgment — keep or drop — but does NOT repeat the tool-based investigation already performed by the Stage 3 verification agent.

3. **When Feature 0 is skipped** (below threshold or `--no-verify`): The judge's existing 4-stage panel runs on all explorer findings as today. No change needed — the panel already sequences verification (Gatekeeper + Verifier) before synthesis (Calibrator + Synthesizer).

### Feature 5: Fix Validation

Extension to Feature 0's verification agent. When verifying a finding, also check the suggested fix.

**Added to `prompts/reviewer-verifier.md`:**

**Hard checks (trigger `fix_valid: false`):**
- Undefined variables/functions/types introduced by the fix
- Type mismatches in typed languages
- Missing imports required by the fix
- Syntax errors in the suggested code

**Soft checks (noted in finding, do NOT trigger `fix_valid: false`):**
- Fix changes behavior beyond what the finding describes (scope creep) — too subjective for reliable LLM assessment (~40-50% accuracy per prompt engineer review). Noted as: "Fix may change behavior beyond the reported issue."
- Fix requires changes in other files (not self-contained) — noted as: "Fix may require changes in other files."

**Explicitly NOT checked:**
- Style or formatting of the fix
- Whether the fix is the "best" approach
- Performance characteristics of the fix

After verdict assignment, add to `prompts/reviewer-verifier.md`:
```
5. Validate the fix. If finding includes a `fix` field:
   - Does fix introduce undefined variables, functions, or types?
   - Does fix introduce type mismatches?
   - Does fix require missing imports?
   - Does fix contain syntax errors?
   - Does fix change behavior beyond what the finding describes?
   - Is the fix self-contained (no changes needed in other files)?

   If broken: keep finding as confirmed (bug is real), add `fix_valid: false`
   and `fix_issue: "<what's wrong>"`. Judge strips broken fix from output.

   If valid or no fix suggested: `fix_valid: true` (default).
```

**Interaction with Stage 1:** Stage 1 does not evaluate fix correctness — fix validation is owned entirely by Stage 3 (this step), which has tool access. Stage 2 does NOT discard based on fix quality — a real bug with a broken fix is still a real bug.

**Judge handling:** Finding with `fix_valid: false` → keep finding, remove or flag the `fix` field. Optionally note "Fix suggestion removed — \<reason\>" in the finding.

---

## Kodus-AI Comparison

| Aspect | Kodus-AI | Our Design | Why different |
|--------|----------|-----------|---------------|
| Stage 1 model | GPT-4O Mini | Same model as explorers (sonnet) | No external API key requirement |
| Stage 2 logic | TypeScript service | Python script | Consistent with scripts-over-prompts |
| Stage 3 approach | Multi-turn agent, max 6 turns | Single verification pass with tools | No sandbox infrastructure; Read/Grep/Glob sufficient |
| Stage 3 default | Discard if unverifiable | `false_positive` if unverifiable | Same principle — skeptical by default |
| Features count | 13 | 13 | Renamed 2, added 3 (concurrency, test-code, linter-dupe) |

Key Kodus-AI lessons:
1. Feature extraction is a batch operation — one LLM call, not per-finding
2. Deterministic triage eliminates cheapest false positives for free
3. "Hard discard" features exist — quality opinions and unchanged-code targets need no verification
4. Default should be skeptical — when uncertain, discard
5. Phantom knowledge is #1 false positive source — `requires_assumed_behavior` targets this

---

## Degradation Matrix

| Failure | Capabilities Lost | Fallback | Log |
|---------|-------------------|----------|-----|
| **Feature extraction LLM fails** | Boolean features | Skip to Stage 3 (verify all findings directly) | `"Feature extraction failed — sending all findings to verification"` |
| **Triage script crashes** | Deterministic filtering | Send all findings to Stage 3 | `"Triage failed — sending all findings to verification"` |
| **Verification agent fails** | Per-finding verdicts | All findings pass to judge as-is (current behavior) | `"Verification failed — judge receives unverified findings"` |
| **Verification times out** | Remaining findings | Partial results used; unprocessed findings pass to judge | `"Verification timed out — {n} findings unverified"` |
| **Below threshold** | Stage 3 verification | Stages 1-2 still run if `always_triage: true` | Normal behavior |

---

## Implementation Plan

### Wave 1: Feature Extraction + Triage (F0 Stages 1-2)

1. Write `prompts/reviewer-feature-extractor.md` with 13-feature schema, calibration examples, and output enforcement
2. Implement `triage_findings()` in `scripts/triage-findings.py` (or add to `enrich-findings.py`) with index validation
3. Implement batch splitting (max 15 findings per extraction call)
4. Add structured triage logging (every decision with features and rule that fired)
5. Add Step 4a.5 (Stages 1-2 only) to SKILL.md
6. Tests: feature extraction output schema (13 features), batch splitting at >15 findings, triage routing logic for each rule (5 rules), index validation, threshold gating, triage log format

### Wave 2: Verification Agent + Fix Validation + Judge Handoffs (F0 Stage 3 + F5 + F1)

1. Write `prompts/reviewer-verifier.md` with verification instructions (10-call budget, one call per finding, inline search strategies, strict output enforcement) + fix validation (4 hard checks, 2 soft signals)
2. Add Stage 3 to Step 4a.5 in SKILL.md
3. Wire verification verdicts into judge input
4. Update judge prompt: Gatekeeper instructions for pre-verified findings, Verifier handling of `needs_investigation` based on verifier notes
5. Tests: verification verdicts, fix validation (hard vs soft checks), `confirmed`/`false_positive`/`needs_investigation` routing, output JSON format compliance, chunked mode cross-chunk consistency

---

## Acceptance Criteria

### Wave 1
- [ ] Feature extractor produces 13 boolean features per finding
- [ ] Batch splitting: >15 findings → 2+ extraction calls
- [ ] Calibration examples included in feature extraction prompt
- [ ] Output enforcement: "Output ONLY the JSON" instruction present
- [ ] Index validation: misaligned finding indices → warning + fallback to verify-all
- [ ] Triage: `is_quality_opinion` → discard
- [ ] Triage: `targets_unchanged_code` → discard
- [ ] Triage: `targets_test_code` → discard
- [ ] Triage: `duplicates_linter_result` → discard
- [ ] Triage: structural + speculation → verify
- [ ] Triage: speculation only → verify (NOT discard)
- [ ] Triage: structural only → verify
- [ ] Triage: no signals → verify
- [ ] Every triage decision logged with features and rule
- [ ] Stages 1-2 run even below threshold when `always_triage: true`
- [ ] Stages 1-2 skipped when `--no-verify`

### Wave 2
- [ ] Verifier: one call per finding, up to 10 tool calls per finding
- [ ] Verifier reads cited code and searches for defenses using inline search strategies
- [ ] Verifier produces `verification_command` for confirmed findings
- [ ] Verifier defaults to `false_positive` when can't confirm within 10 tool calls
- [ ] `needs_investigation` = genuinely unresolvable after thorough investigation (not budget constraint)
- [ ] Fix validation hard checks: undefined names, type mismatches, missing imports, syntax errors → `fix_valid: false`
- [ ] Fix validation soft signals: scope creep, not self-contained → noted in finding, NOT `fix_valid: false`
- [ ] Verifier output: strict JSON format, lowercase verdict strings only
- [ ] Judge Gatekeeper: skips re-verification for `confirmed` findings
- [ ] Judge Verifier stage: handles `needs_investigation` using verifier notes (no tool re-investigation)
- [ ] Judge strips broken fixes (`fix_valid: false`) from output
- [ ] Judge receives only confirmed + needs_investigation findings
- [ ] Chunked mode: cross-chunk verdict consistency check by judge

---

## Performance Budget

| Operation | Expected | Hard Limit |
|-----------|----------|------------|
| Stage 1: Feature extraction (per batch of ≤15) | 3-8s | 30s |
| Stage 2: Deterministic triage + index validation | <50ms | 200ms |
| Stage 3: Verification agent (per finding, up to 10 tool calls) | 5-15s | 60s |
| Total verification overhead (9 findings typical) | 50-140s | 300s |

**Cost:** Stage 1 is 1-2 LLM calls (~$0.05-0.10). Stage 3 is one LLM call per verified finding (~$0.10-0.25 each). For a typical review with 9 findings routed to verify: ~$1.00-2.50 total verification cost. This is justified by the precision improvement — without verification, the judge processes 30-90% more findings, most of which are noise.

---

## Config Schema Changes

```yaml
verification:
  threshold: 6
  always_triage: true
  always_in_chunked: true
```

Add `"verification"` to `CONFIG_ALLOWLIST`.

---

## Files to Create

| File | Feature |
|------|---------|
| `prompts/reviewer-feature-extractor.md` | F0 Stage 1 |
| `prompts/reviewer-verifier.md` | F0 Stage 3 + F5 |

## Files to Modify

| File | Features | Change |
|------|----------|--------|
| `SKILL.md` | F0 | Add Step 4a.5 |
| `prompts/reviewer-judge.md` | F0, F1, F5 | Pre-verified note, Gatekeeper/Verifier handoff updates, fix handling |
| `references/design.md` | F0, F1, F5 | Rationale entries |
| `references/acceptance-criteria.md` | F0, F5 | Verification scenarios |
| `scripts/enrich-findings.py` or new `scripts/triage-findings.py` | F0 | Triage logic |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Verification adds 50-140s latency (9 findings typical) | Threshold gating (≤5 findings → skip Stage 3); Stages 1-2 are cheap; per-finding parallelism possible |
| Feature extractor hallucinates features | Conservative defaults (false for structural, true for speculation) |
| Verification agent is too aggressive (discards real findings) | Judge still runs Pass 1 verification on `needs_investigation`; `--force-verify` for safety-critical reviews |
| Strengthened handoffs add prompt length | Same total content, just reordered; no additional LLM calls |
