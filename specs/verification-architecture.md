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
2. **Restructure the judge into two explicit passes** — verify-then-synthesize — so verification completes before narrative construction begins
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

**11 boolean features per finding:**

| Feature | Category | What it detects |
|---------|----------|----------------|
| `has_resource_leak` | Structural defect | File/connection/memory not closed on all paths |
| `has_inconsistent_contract` | Structural defect | Function behavior doesn't match signature/docstring/callers |
| `has_wrong_algorithm` | Structural defect | Algorithm produces incorrect results for valid inputs |
| `has_data_exposure` | Structural defect | Sensitive data written to logs, responses, or errors |
| `has_missing_error_handling` | Structural defect | Error path leads to crash, corruption, or undefined state |
| `has_redundant_work_in_loop` | Structural defect | Repeated I/O, allocation, or computation inside a loop |
| `has_unsafe_data_flow` | Structural defect | Untrusted input reaches sensitive operation without validation |
| `requires_assumed_behavior` | Speculation | Finding depends on how unseen/imported code behaves |
| `is_quality_opinion` | Opinion | Style/preference/best-practice, not a defect |
| `targets_unchanged_code` | Scope violation | References code not changed in the diff |
| `improved_code_is_correct` | Meta | The suggested fix compiles/works |

Derived from Kodus-AI's 13-feature SafeguardFeatureSet (dropped `requires_assumed_workload` and `is_anti_pattern_only` — merged into existing features).

**Feature extraction prompt** (`prompts/reviewer-feature-extractor.md`):

```markdown
You are a finding feature extractor. For each finding, extract boolean features
that describe its nature. Do NOT make keep/discard decisions — just extract features.

Rules:
- Only set structural defect features to true if the finding points to specific
  lines in the diff where the defect occurs.
- Set `requires_assumed_behavior` to true if verifying the finding requires knowing
  how code you cannot see behaves (imported functions, external APIs, database schema).
  Exception: if the defect is structural (unsafe for ANY input), it's a defect even
  if some context is assumed.
- Set `is_quality_opinion` to true for: naming suggestions, style preferences,
  "consider using X instead of Y" without a concrete bug, refactoring suggestions,
  documentation suggestions.
- Set `targets_unchanged_code` to true if the finding's cited lines are all
  in unchanged code (- lines or context lines, not + lines).
- When uncertain, prefer false for structural features (avoids false keeps)
  and true for speculation features (avoids false keeps).
```

**Output format:**
```json
{
  "findings": [
    { "finding_index": 0, "features": { "has_resource_leak": false, "has_inconsistent_contract": false, ... } },
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
    'has_unsafe_data_flow'
]

def triage(features: dict) -> str:
    has_structural = any(features.get(f) for f in STRUCTURAL_DEFECT_FEATURES)
    has_speculation = features.get('requires_assumed_behavior', False)
    has_hard_discard = (features.get('is_quality_opinion', False)
                        or features.get('targets_unchanged_code', False))

    # Rule 1: Hard discard — quality opinions and out-of-scope
    if has_hard_discard:
        return 'discard'

    # Rule 2: Speculation + structural — ambiguous, needs verification
    if has_speculation and has_structural:
        return 'verify'

    # Rule 3: Speculation only — no structural defect to justify it
    if has_speculation and not has_structural:
        return 'discard'

    # Rule 4: Structural defect without speculation — may be mitigated
    if has_structural and not has_speculation:
        return 'verify'

    # Rule 5: No signals — conservative, send to verification
    return 'verify'
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

**Per finding:**
1. **Read the code.** Use Read to examine the file and line cited in the finding. If the file or line doesn't exist, verdict: `false_positive`.
2. **Check the claim.** Does the code actually do what the finding says?
   - If the finding says "nil map write panics" — is the map actually nil at that point?
   - If the finding says "SQL injection" — is the input actually user-controlled?
   - If the finding says "missing error check" — is there a check the explorer missed?
3. **Search for defenses.** Use Grep to look for:
   - Input validation before the cited line
   - Error handling wrapping the cited code
   - Guard clauses or early returns that prevent the failure mode
   - Configuration or middleware that addresses the concern
4. **Generate verification evidence.** For each finding you verify, produce a `verification_command` — a concrete grep/read command that anyone can run to confirm the finding independently. This is not optional for `confirmed` findings. Examples:
   - `grep -n 'validate_token' src/auth/*.py` → "No validation function found in auth module"
   - `Read src/api/handler.py:42-55` → "Error return on line 48 is caught by middleware at line 12"
   - `grep -rn 'defer.*Close' src/db/` → "Connection.Close() is deferred in all 3 callers"
   Inspired by CodeRabbit's verification agent: "comments come with receipts."
5. **Assign verdict:** `confirmed` | `false_positive` | `needs_investigation`

**Default:** If cannot confirm defect within 3 tool calls → `false_positive`. Skeptical by default.

**Quick reference by defect type:**
- Resource leak → search for close()/release()/defer in same function and callers
- Wrong algorithm → trace with concrete input values, verify output is wrong
- Race condition → search for concurrent callers, check if lock/mutex exists
- Missing error handling → check if error is handled by caller or middleware
- Interface change → search for callers, verify they handle the new behavior
- Dead code path → search for actual callers that reach the path

**Output:** JSON array of `{finding_index, verdict, reason, verification_command}`.

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

### Feature 1: Two-Pass Judge

Restructure the judge's internal workflow into two distinct, non-interleaved passes.

**Pass 1: Adversarial Verification (existing Steps 1-3)**
- Step 1: Existence check (verify cited code exists)
- Step 2: Contradiction check (search for defenses that disprove findings)
- Step 3: Investigate `needs_investigation` findings from verifier

**Pass 2: Synthesis (existing Steps 4-6)**
- Step 4: Root-cause grouping and deduplication
- Step 5: Severity calibration and cross-explorer gap analysis
- Step 6: Verdict and report

**Key rule:** Complete ALL of Pass 1 before starting Pass 2. No interleaving. Prevents the judge from keeping a finding because it "fits the narrative."

**When Feature 0 is skipped** (below threshold or `--no-verify`): Judge runs both passes on all explorer findings. Two-pass structure still helps — forces verify-before-synthesize even without separate verification agent.

### Feature 5: Fix Validation

Extension to Feature 0's verification agent. When verifying a finding, also check the suggested fix.

**Added to `prompts/reviewer-verifier.md`:**

**What gets checked (code breakage only — NOT quality):**
- Undefined variables/functions/types introduced by the fix
- Type mismatches in typed languages
- Missing imports required by the fix
- Syntax errors in the suggested code
- Fix changes behavior beyond what the finding describes (scope creep)
- Fix requires changes in other files (not self-contained)

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

**Interaction with Stage 1:** The `improved_code_is_correct` feature already flags broken fixes. Stage 2 does NOT discard based on this — a real bug with a broken fix is still a real bug.

**Judge handling:** Finding with `fix_valid: false` → keep finding, remove or flag the `fix` field. Optionally note "Fix suggestion removed — \<reason\>" in the finding.

---

## Kodus-AI Comparison

| Aspect | Kodus-AI | Our Design | Why different |
|--------|----------|-----------|---------------|
| Stage 1 model | GPT-4O Mini | Same model as explorers (sonnet) | No external API key requirement |
| Stage 2 logic | TypeScript service | Python script | Consistent with scripts-over-prompts |
| Stage 3 approach | Multi-turn agent, max 6 turns | Single verification pass with tools | No sandbox infrastructure; Read/Grep/Glob sufficient |
| Stage 3 default | Discard if unverifiable | `false_positive` if unverifiable | Same principle — skeptical by default |
| Features count | 13 | 11 | Merged redundant features |

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

1. Write `prompts/reviewer-feature-extractor.md` with 11-feature schema and extraction instructions
2. Implement `triage_findings()` in `scripts/triage-findings.py` (or add to `enrich-findings.py`)
3. Add Step 4a.5 (Stages 1-2 only) to SKILL.md
4. Tests: feature extraction output schema, triage routing logic for each rule, threshold gating

### Wave 2: Verification Agent + Fix Validation (F0 Stage 3 + F5)

1. Write `prompts/reviewer-verifier.md` with verification instructions + fix validation
2. Add Stage 3 to Step 4a.5 in SKILL.md
3. Wire verification verdicts into judge input
4. Update judge prompt with pre-verified findings note
5. Tests: verification verdicts, fix validation, `confirmed`/`false_positive`/`needs_investigation` routing

### Wave 3: Two-Pass Judge (F1)

1. Restructure `prompts/reviewer-judge.md` into Pass 1 (verification) / Pass 2 (synthesis)
2. Add explicit "complete Pass 1 before starting Pass 2" instruction
3. Tests: judge produces correct output with pre-verified findings, judge handles `needs_investigation`

---

## Acceptance Criteria

### Wave 1
- [ ] Feature extractor produces 11 boolean features per finding
- [ ] Triage: `is_quality_opinion` → discard
- [ ] Triage: `targets_unchanged_code` → discard
- [ ] Triage: structural + speculation → verify
- [ ] Triage: speculation only → discard
- [ ] Triage: structural only → verify
- [ ] Triage: no signals → verify
- [ ] Stages 1-2 run even below threshold when `always_triage: true`
- [ ] Stages 1-2 skipped when `--no-verify`

### Wave 2
- [ ] Verifier reads cited code and searches for defenses
- [ ] Verifier produces `verification_command` for confirmed findings
- [ ] Verifier defaults to `false_positive` when can't confirm within 3 tool calls
- [ ] Fix validation: broken fix → `fix_valid: false`, finding still confirmed
- [ ] Judge receives only confirmed + needs_investigation findings
- [ ] Judge prompt includes pre-verified findings note

### Wave 3
- [ ] Judge completes Pass 1 (verification) before starting Pass 2 (synthesis)
- [ ] Judge handles `needs_investigation` findings with deeper analysis in Pass 1
- [ ] Judge strips broken fixes (`fix_valid: false`) from output
- [ ] Two-pass structure works correctly when verification is skipped (below threshold)

---

## Performance Budget

| Operation | Expected | Hard Limit |
|-----------|----------|------------|
| Stage 1: Feature extraction (batch) | 3-8s | 30s |
| Stage 2: Deterministic triage | <50ms | 200ms |
| Stage 3: Verification agent | 10-30s | 120s |
| Total verification overhead | 15-40s | 150s |

**Cost:** Stage 1 is one LLM call (~$0.05). Stage 3 is one LLM call with tool use (~$0.20-0.50 depending on finding count). Total: ~$0.25-0.55 per verified review.

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
| `prompts/reviewer-judge.md` | F0, F1, F5 | Pre-verified note, two-pass structure, fix handling |
| `references/design.md` | F0, F1, F5 | Rationale entries |
| `references/acceptance-criteria.md` | F0, F5 | Verification scenarios |
| `scripts/enrich-findings.py` or new `scripts/triage-findings.py` | F0 | Triage logic |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Verification adds 15-40s latency | Threshold gating (≤5 findings → skip Stage 3); Stages 1-2 are cheap |
| Feature extractor hallucinates features | Conservative defaults (false for structural, true for speculation) |
| Verification agent is too aggressive (discards real findings) | Judge still runs Pass 1 verification on `needs_investigation`; `--force-verify` for safety-critical reviews |
| Two-pass judge is longer/more expensive | Same total content, just reordered; no additional LLM calls |
