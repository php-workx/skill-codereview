# Plan: Verification Pipeline

Twelve features focused on review precision, judge architecture, plan compliance, and output integration. Where v1.3 enriches what explorers *see* (context, checklists, prescan signals), Verification Pipeline improves what happens *after* explorers report — verification, synthesis, and how findings reach the user. Features 0-7 come from the initial design and Kodus-AI analysis. Features 8-10 are informed by analysis of the PR-Agent (Qodo) code review platform (local path `~/workspaces/pr-agent`). Feature 11 comes from CodeRabbit gap analysis around adaptive expert selection and shell-script coverage.

## Relationship to v1.3

| v1.3 Feature | Relevance to Verification Pipeline |
|-------------|-------------------|
| **F2** (enrich-findings.py) | Verification Pipeline Feature 0 extends it with verification verdicts |
| **F12** (per-file certification) | Verification Pipeline Feature 1 extends certification into a full verification round |
| **F14** (output file batching) | Verification Pipeline Feature 1 benefits — verified findings are smaller, reducing judge context pressure |
| **Multi-model council** (deferred from v1.2/v1.3) | Verification Pipeline Feature 4 scopes a lightweight version. Feature 8 scoring bands informed by PR-Agent's self-reflection mechanism. |

### Design Principles

Carried forward from v1.3:

**Scripts Over Prompts** — Mechanical steps are scripts. Judgment steps are AI.

**Checklists Over Instructions** — Concrete items, not open-ended guidance.

New in Verification Pipeline:

**Verify Before Synthesize** — The judge should not simultaneously verify findings AND synthesize the verdict. Verification is a distinct cognitive task that benefits from focused attention. Separate the steps.

**Cost-Controlled Quality** — Multi-model and multi-pass features must have clear cost controls. Every additional model call needs a threshold that prevents it from running on trivial reviews. The default experience (single-model, single-pass) must remain fast and free of API key requirements.

### Deferred ideas (from PR-Agent analysis, not planned as features)

These patterns were identified during the PR-Agent analysis but don't justify standalone features. Noted here for future reference:

- **AI metadata caching across passes**: PR-Agent's `/describe` generates per-file summaries that `/review` and `/improve` reuse without extra API calls. In our architecture, each explorer gathers its own context via tools. Caching context across explorers could reduce redundant Read/Grep calls, but the explorers' investigation is inherently different per pass — a security explorer reads different things than a test-adequacy explorer. Revisit if profiling shows significant tool-call redundancy across passes.
- **Asymmetric diff context**: PR-Agent uses 5 lines before hunks, 1 line after (more context needed before a change than after). Our explorers have full tool access so they can read surrounding code themselves. However, if we ever present diffs inline in prompts (e.g., for the verification agent in Feature 0), asymmetric context is the right default.

---

## Feature 0: Dedicated Finding Verification Round

**Goal:** Add a verification step between explorers and the judge where each finding is individually assessed as `confirmed`, `false_positive`, or `needs_investigation`. This is the single highest-impact precision improvement — it catches false positives before they reach the judge, and it identifies findings that need deeper investigation.

Currently, the judge does everything in one pass: verify findings, check for contradictions, deduplicate, calibrate severity, and produce a verdict. By extracting verification into a separate step, the judge receives pre-filtered findings and can focus on synthesis and verdict.

Inspired by Claude Octopus's Round 2 verification gate, which assigns a verdict to each finding before synthesis. Architecture significantly refined based on analysis of the **Kodus-AI code review platform** ([`kodustech/kodus-ai`](https://github.com/kodustech/kodus-ai), especially [`evals/promptfoo-safeguard/`](https://github.com/kodustech/kodus-ai/tree/main/evals/promptfoo-safeguard)), whose 3-stage safeguard pipeline (feature extraction → deterministic triage → agent verification) provides a concrete, production-tested blueprint for this feature.

### Architecture

The verification round has **three stages**, inspired by Kodus-AI's safeguard pipeline. The key insight: separating feature extraction (cheap batch LLM call), deterministic triage (free, instant logic), and deep verification (expensive per-finding agent) dramatically reduces cost while improving precision.

```text
Explorers (parallel)
    │
    ▼
Stage 1: Feature Extraction (batch LLM call — one call for ALL findings)
    │  Extract boolean features per finding (structural defect? phantom knowledge? etc.)
    │
    ▼
Stage 2: Deterministic Triage (no LLM — pure logic in enrich-findings.py)
    │  Apply rules: auto-discard obvious false positives, fast-track clear defects,
    │  route ambiguous findings to verification
    │
    ▼
Stage 3: Verification Agent (LLM with tools — only for "verify" findings)
    │  For each finding routed to verification:
    │  1. Read the cited code (file:line from finding)
    │  2. Check: does the code actually do what the finding claims?
    │  3. Search for defenses the explorer may have missed
    │  4. Assign verdict: confirmed | false_positive | needs_investigation
    │
    ▼
Judge (existing — receives only confirmed + needs_investigation findings)
    │  Adversarial validation, deduplication, verdict
    ▼
Enrichment (enrich-findings.py)
```

### Stage 1: Feature Extraction

**One batch LLM call** extracts structured boolean features for all findings simultaneously. This is cheap (one call, not per-finding) and produces the structured data that Stage 2 needs.

New prompt file: `prompts/reviewer-feature-extractor.md`

**Features to extract per finding (11 boolean features):**

| Feature | Category | What it detects |
|---------|----------|----------------|
| `has_resource_leak` | Structural defect | File/connection/memory not closed on all paths |
| `has_inconsistent_contract` | Structural defect | Function behavior doesn't match its signature/docstring/callers |
| `has_wrong_algorithm` | Structural defect | Algorithm produces incorrect results for valid inputs |
| `has_data_exposure` | Structural defect | Sensitive data written to logs, responses, or errors |
| `has_missing_error_handling` | Structural defect | Error path leads to crash, corruption, or undefined state |
| `has_redundant_work_in_loop` | Structural defect | Repeated I/O, allocation, or computation inside a loop |
| `has_unsafe_data_flow` | Structural defect | Untrusted input reaches sensitive operation without validation |
| `requires_assumed_behavior` | Speculation | Finding depends on how unseen/imported code behaves |
| `is_quality_opinion` | Opinion | Finding is a style/preference/best-practice, not a defect |
| `targets_unchanged_code` | Scope violation | Finding references code not changed in the diff |
| `improved_code_is_correct` | Meta | The suggested fix actually compiles/works |

These features are derived from Kodus-AI's 13-feature SafeguardFeatureSet, adapted for our use case. Key differences from Kodus:
- Dropped `requires_assumed_workload` (merged into `requires_assumed_behavior`)
- Dropped `is_anti_pattern_only` (merged into `is_quality_opinion`)
- Kept `improved_code_is_correct` — valuable for catching broken fix suggestions

**Feature extraction prompt instructions:**

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

Output:
{
  "findings": [
    { "finding_index": 0, "features": { "has_resource_leak": false, ... } },
    ...
  ]
}
```

### Stage 2: Deterministic Triage

**No LLM call.** Pure logic implemented in `scripts/triage-findings.py` (or added to `enrich-findings.py`). This stage applies deterministic rules to the extracted features and routes each finding to one of three outcomes.

**Triage logic (priority order):**

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

    # Rule 1: Hard discard — quality opinions and out-of-scope findings
    if has_hard_discard:
        return 'discard'

    # Rule 2: Speculation + structural defect — ambiguous, needs verification
    if has_speculation and has_structural:
        return 'verify'

    # Rule 3: Speculation only — no structural defect to justify it
    if has_speculation and not has_structural:
        return 'discard'

    # Rule 4: Structural defect without speculation — likely real, but may be
    #          mitigated by code the explorer didn't see
    if has_structural and not has_speculation:
        return 'verify'

    # Rule 5: No signals at all — conservative, send to verification
    return 'verify'
```

**Why "verify" is the default, not "keep":** Kodus-AI's production experience shows that findings without clear structural defect signals are more likely false positives than true positives. Verification catches them; auto-keeping would pass noise to the judge.

**Triage routing:**

| Triage Decision | Action |
|----------------|--------|
| `discard` | Drop finding. Log count in `dropped.triage_discarded`. |
| `verify` | Pass to Stage 3 (verification agent). |

Note: There is no `keep` outcome from triage. All non-discarded findings go through verification. This is a deliberate design choice — Kodus-AI's data shows that even "obvious" structural defects benefit from verification (they may be mitigated by code the explorer didn't see). The verification agent fast-tracks clear defects anyway.

### Stage 3: Verification Agent

For findings routed to `verify`, a verification agent investigates each one using tools. This is the expensive step — it makes LLM calls with tool access per finding.

New prompt file: `prompts/reviewer-verifier.md`

**Core instructions:**

```markdown
You are a finding verifier. Your job is to check whether each explorer finding
is real. You are skeptical by default — you are looking for reasons the finding
is WRONG, not reasons it is right.

For each finding:

1. **Read the code.** Use Read to examine the file and line cited in the finding.
   If the file or line doesn't exist, verdict: false_positive.

2. **Check the claim.** Does the code actually do what the finding says?
   - If the finding says "nil map write panics" — is the map actually nil at that point?
   - If the finding says "SQL injection" — is the input actually user-controlled?
   - If the finding says "missing error check" — is there a check the explorer missed?

3. **Search for defenses.** Use Grep to look for:
   - Input validation before the cited line
   - Error handling wrapping the cited code
   - Guard clauses or early returns that prevent the failure mode
   - Configuration or middleware that addresses the concern

4. **Generate verification evidence.** For each finding you verify, produce a
   `verification_command` — a concrete grep/read command that anyone can run to
   confirm the finding independently. This is not optional for `confirmed` findings.

   Examples:
   - `grep -n 'validate_token' src/auth/*.py` → "No validation function found in auth module"
   - `Read src/api/handler.py:42-55` → "Error return on line 48 is caught by middleware at line 12"
   - `grep -rn 'defer.*Close' src/db/` → "Connection.Close() is deferred in all 3 callers"

   Inspired by CodeRabbit's verification agent, which generates shell/Python scripts
   using grep and ast-grep to extract proof from the codebase. "Comments come with receipts."

5. **Assign verdict:**
   - `confirmed`: The code does what the finding claims, no defense was found,
     and you have a `verification_command` that proves it.
   - `false_positive`: The finding is wrong — the code doesn't behave as claimed,
     or a defense exists that the explorer missed.
   - `needs_investigation`: You can't confirm or deny. The finding may be real
     but requires deeper analysis (complex call chain, dynamic dispatch, runtime config).
     The judge will investigate these.

**Default verdict:** If you cannot confirm the defect causes real harm in actual
execution paths within 3 tool calls, default to `false_positive`. Err on the side
of discarding — it's better to miss a speculative finding than to burden the judge
with noise.

**Quick reference by defect type:**
- Resource leak → search for close()/release()/defer in same function and callers
- Wrong algorithm → trace with concrete input values, verify output is wrong
- Race condition → search for concurrent callers, check if lock/mutex exists
- Missing error handling → check if error is handled by caller or middleware
- Interface change → search for callers, verify they handle the new behavior
- Dead code path → search for actual callers that reach the path

Output your verdicts as a JSON array:
[
  { "finding_index": 0, "verdict": "confirmed", "reason": "Verified: map is nil on skip path, no initialization found" },
  { "finding_index": 1, "verdict": "false_positive", "reason": "Input is validated by middleware at api/middleware.py:23 before reaching this handler" },
  { "finding_index": 2, "verdict": "needs_investigation", "reason": "Call chain too deep to trace — login() → session_manager() → cache.get() → unclear if cache is thread-safe" }
]
```

### Activation threshold

Verification adds latency and cost. Only activate when the review produces enough findings to justify it:

| Condition | Verification |
|-----------|-------------|
| Total explorer findings ≤ 5 | Skip — judge handles directly |
| Total explorer findings 6-30 | Run full 3-stage verification |
| Total explorer findings > 30 (chunked mode) | Run full 3-stage verification (always) |
| `--no-verify` flag | Skip verification |
| `--force-verify` flag | Always run verification |

**Stage-level activation:** Even when verification is skipped (≤5 findings), Stages 1-2 (feature extraction + deterministic triage) can still run — they're cheap and filter obvious false positives. Only Stage 3 (agent verification) is expensive. Consider always running Stages 1-2 and only gating Stage 3 on the threshold.

Configurable via `.codereview.yaml`:
```yaml
verification:
  threshold: 6        # finding count to trigger Stage 3 verification
  always_triage: true  # always run Stages 1-2 (feature extraction + triage)
  always_in_chunked: true
```

### SKILL.md changes

Add new **Step 4a.5** between explorer collection (4a) and judge launch (4b):

```text
Step 4a.5: Finding Verification (when threshold met)

Stage 1: Feature Extraction
- Launch feature extractor with all explorer findings (combined)
- One batch LLM call — extracts boolean features per finding
- Prompt: prompts/reviewer-feature-extractor.md

Stage 2: Deterministic Triage
- Run triage logic (scripts/triage-findings.py or inline in enrich-findings.py)
- Route findings: discard (drop) or verify (pass to Stage 3)
- Log triage summary: "Triage: 12 findings → 3 discarded, 9 to verify"

Stage 3: Verification Agent (if threshold met)
- Launch verification agent with "verify" findings
- Access to Read, Grep, Glob tools
- Prompt: prompts/reviewer-verifier.md
- Verdict per finding: confirmed | false_positive | needs_investigation

Filter results:
- confirmed → pass to judge
- false_positive → drop (log count)
- needs_investigation → pass to judge with flag

Update the finding count summary for the judge prompt.
```

### Interaction with existing pipeline

- **Step 4a**: Unchanged — explorers still produce findings as today.
- **Step 4a.5 (new)**: 3-stage verification runs on combined explorer output.
- **Step 4b (judge)**: Judge receives filtered findings. Its prompt gains a note: "These findings have been pre-verified. Findings marked `needs_investigation` require your deeper analysis. Do not re-verify `confirmed` findings — focus on synthesis, deduplication, and verdict."
- **Step 4-L (chunked mode)**: Verification runs per-chunk (after chunk explorers, before final judge). This is where it has the most value — chunked reviews produce 50-100+ findings.
- **Feature 7 (file batching)**: Verified findings are written to disk. The judge reads verified files instead of raw explorer files.

### Kodus-AI comparison and adaptation notes

Our 3-stage design is directly inspired by Kodus-AI's safeguard pipeline but adapted for our architecture:

| Aspect | Kodus-AI | Our Design | Why different |
|--------|----------|-----------|---------------|
| Stage 1 model | GPT-4O Mini | Same model as explorers (sonnet) | No external API key requirement |
| Stage 2 logic | TypeScript service | Python script (triage-findings.py) | Consistent with our scripts-over-prompts pattern |
| Stage 3 approach | Multi-turn agent with sandbox (max 6 turns) | Single verification pass with tool access | We don't have sandbox infrastructure; tools (Read/Grep/Glob) are sufficient |
| Stage 3 default | Discard if unverifiable | false_positive if unverifiable | Same principle — skeptical by default |
| Features count | 13 | 11 | Merged redundant features, dropped Kodus-specific ones |
| Integration | Inline in pipeline service | Scripts + prompts | Consistent with our architecture |

Key Kodus-AI lessons incorporated:
1. **Feature extraction is a batch operation** — one LLM call for all findings, not per-finding
2. **Deterministic triage eliminates the cheapest false positives for free** — no LLM needed
3. **"Hard discard" features exist** — quality opinions and unchanged-code targets can be discarded without any verification
4. **Default should be skeptical** — when uncertain, discard rather than keep
5. **Phantom knowledge is the #1 false positive source** — the `requires_assumed_behavior` feature specifically targets this (see also v1.3 F17)

### Files to create

- `skills/codereview/prompts/reviewer-feature-extractor.md` — Feature extraction prompt
- `skills/codereview/prompts/reviewer-verifier.md` — Verification agent prompt

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 4a.5 (3-stage verification)
- `skills/codereview/prompts/reviewer-judge.md` — Add note about pre-verified findings
- `skills/codereview/references/design.md` — Add rationale entry (including Kodus-AI comparison)
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: Stage 1 feature extraction, Stage 2 triage decisions, Stage 3 verification verdicts, threshold gating, always-triage mode

### Effort: Medium-Large (expanded from Medium due to 3-stage architecture)

---

## Feature 1: Two-Pass Judge (Verify Then Synthesize)

**Goal:** Restructure the judge's internal workflow into two distinct passes. Pass 1 focuses on adversarial validation of `needs_investigation` findings (from the verifier) and any remaining unverified findings. Pass 2 focuses on synthesis: deduplication, root-cause grouping, severity calibration, cross-explorer gaps, and verdict.

This extends v1.3 F12 (per-file certification) — the judge already has Step 0.5 for certification review. This feature makes the two-phase structure explicit and formal.

### Judge prompt restructure

Currently the judge prompt has Steps 1-6 that mix verification and synthesis. Restructure into:

**Pass 1: Adversarial Verification (Steps 1-3)**
- Step 1: Existence check (verify cited code exists — fast, mechanical)
- Step 2: Contradiction check (search for defenses that disprove findings)
- Step 3: Investigate `needs_investigation` findings from the verifier

**Pass 2: Synthesis (Steps 4-6)**
- Step 4: Root-cause grouping and deduplication
- Step 5: Severity calibration and cross-explorer gap analysis
- Step 6: Verdict and report

The key change: the judge completes ALL of Pass 1 before starting Pass 2. No interleaving. This prevents the judge from being biased toward keeping a finding because it "fits the narrative" of the synthesis.

### When Feature 0 is not active

If the verification round (Feature 0) is skipped (below threshold or `--no-verify`), the judge runs both passes on all explorer findings. The two-pass structure still helps — it forces the judge to verify before synthesizing, even without a separate verification agent.

### Files to modify

- `skills/codereview/prompts/reviewer-judge.md` — Restructure steps into Pass 1 / Pass 2
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 2: Spec-Gated Pass Execution

**Goal:** When `--spec` is provided, run spec verification first and gate other passes on its result. If the implementation doesn't match the spec at all (most requirements `not_implemented`), skip the detailed code quality passes — they'll just report issues in code that needs to be rewritten anyway.

### Logic

```text
If --spec provided:
  Run spec-verification explorer FIRST (before other explorers)
  Read spec_requirements from output

  If >50% of "must" requirements are "not_implemented":
    Skip remaining explorers
    Report: "Spec verification found major implementation gaps.
             Detailed code review deferred until implementation catches up."
    Verdict: FAIL (spec gaps)

  Else:
    Run remaining explorers normally
    Include spec results in judge input
```

### Activation

Only when `--spec` is provided AND the spec has at least 3 extractable "must" requirements. Specs with fewer requirements don't have enough signal to gate on.

### SKILL.md changes

Modify Step 4a to support sequential spec-first execution when `--spec` is active:
1. Launch spec-verification explorer
2. Read its output, count `not_implemented` among `must` requirements
3. If gap ratio > 50%: skip remaining explorers, go directly to report
4. Otherwise: launch remaining explorers in parallel as normal

### Files to modify

- `skills/codereview/SKILL.md` — Modify Step 4a for spec-first gating
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios

### Effort: Small

---

## Feature 3: Review Summary for PR Descriptions

**Goal:** Generate a concise, copy-pasteable review summary suitable for inclusion in PR descriptions or review comments. Not inline PR comments (the skill does not post to GitHub) — a formatted summary block that the user can paste wherever they want.

The skill currently produces a full markdown report and a JSON findings file. Both are detailed — too detailed for a PR description. This feature adds a condensed summary block at the top of the report.

### Output format

Added to the top of the markdown report, before the detailed findings:

```markdown
## Review Summary

**Verdict:** WARN — 2 issues to address before merge

**Must Fix (2):**
- `src/auth/login.py:42` — SQL injection via string formatting in user lookup query
- `src/api/orders.py:78` — Race condition: concurrent order updates can double-charge

**Should Fix (3):**
- `src/utils/cache.py:23` — Cache invalidation missing after user role change
- `src/models/user.py:156` — Unused `admin_override` parameter (YAGNI)
- `tests/test_auth.py:34` — Test mocks the database, cannot catch schema drift

**Spec:** 8/10 requirements implemented, 1 partial, 1 not started
```

### Implementation

The judge already produces all the data needed. Add a "Summary Block" section to the report template (`references/report-template.md`) with formatting rules:

- One line per finding: `file:line` — one-sentence summary
- Group by action tier
- Include spec status if `--spec` was used
- Cap at 10 lines (link to full report for more)

### Files to modify

- `skills/codereview/references/report-template.md` — Add summary block template
- `skills/codereview/prompts/reviewer-judge.md` — Instruct judge to produce summary block
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 4: Multi-Model Spot-Check (Optional)

**Goal:** For high-stakes findings (severity high/critical, confidence < 0.85), optionally cross-check with a different model to validate the finding. This is a lightweight version of a full multi-model council — it only activates for the findings where a second opinion has the most value.

See `docs/research-multi-model-council.md` for broader research on multi-model approaches. This feature implements the narrowest useful slice.

### Architecture

```text
Judge produces findings
    │
    ▼
For each high/critical finding with confidence < 0.85:
    │
    ├── If spot-check enabled AND different model available:
    │     Send finding + cited code to alternate model
    │     Ask: "Is this finding valid? Confirmed / false_positive / uncertain"
    │     If false_positive: downgrade to needs_investigation (judge re-evaluates)
    │     If uncertain: keep as-is, note "unverified by spot-check"
    │
    └── If spot-check disabled OR no alternate model: skip (current behavior)
```

### Activation

Off by default. Enabled via `.codereview.yaml`:

```yaml
spot_check:
  enabled: false
  model: "opus"              # model for spot-checking (must differ from explorer model)
  max_checks: 5              # cap to control cost
  severity_threshold: "high" # only spot-check high/critical
  confidence_ceiling: 0.85   # only spot-check findings below this confidence
```

Or via CLI flag: `/codereview --spot-check`

### Cost control

- Max 5 spot-checks per review (configurable)
- Only triggers on high/critical + low-confidence findings
- Each spot-check is a single, focused model call with ~500 tokens of context
- Estimated cost: $0.01-0.05 per spot-check with Opus

### Requirements

- Requires a model different from the explorer model (no value in asking the same model)
- Does NOT require external API keys — uses Claude model variants (sonnet → opus spot-check, or vice versa)
- The `pass_models` config from v1.1 already supports per-pass model overrides — spot-check uses the same mechanism

### Files to create

- `skills/codereview/prompts/reviewer-spot-check.md` — Spot-check prompt

### Files to modify

- `skills/codereview/SKILL.md` — Add spot-check step after judge, before enrichment
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios

### Effort: Medium

---

## Feature 5: Fix Validation (Suggested Code Correctness)

**Goal:** Verify that the code fixes suggested by explorers are themselves correct — that they compile, don't introduce new bugs, and actually address the finding. Currently explorers suggest fixes (`fix` field in findings) but nobody checks whether the fix is valid. A broken fix erodes trust in the entire review.

Inspired by Kodus-AI's `validateCodeSemantics.ts` prompt, which validates suggested code for runtime errors (undefined variables, type mismatches, null dereference, malformed imports). Their `improved_code_is_correct` feature in the safeguard pipeline also flags broken fixes during verification.

### Where it fits

Addition to the verification agent's responsibilities (Feature 0, Stage 3). When verifying a finding, the agent also checks the suggested fix.

### What gets checked

The verifier already reads the cited code and checks the finding's claim (Stage 3). Add a fix validation step:

**Only checks for code breakage — NOT quality:**
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

### Verifier prompt addition

Add to `prompts/reviewer-verifier.md` after the verdict assignment:

```markdown
5. **Validate the fix.** If the finding includes a suggested fix (`fix` field):
   - Does the fix introduce any new undefined variables, functions, or types?
   - Does the fix require imports that aren't present?
   - Does the fix change behavior beyond what the finding describes?
   - Is the fix self-contained (doesn't require changes in other files)?

   If the fix is broken:
   - Still report the finding as `confirmed` (the bug is real)
   - Add `fix_valid: false` and `fix_issue: "<what's wrong>"` to the verdict
   - The judge will strip the broken fix from the final output

   If the fix is valid or no fix is suggested: `fix_valid: true` (default)
```

### Judge handling

When the judge receives a finding with `fix_valid: false`:
- Keep the finding (the bug is real)
- Remove or flag the `fix` field in the output
- Optionally: note "Fix suggestion removed — <reason>" in the finding

### Interaction with Feature 0 stages

- **Stage 1 (feature extraction):** The `improved_code_is_correct` feature already flags broken fixes. Stage 2 triage does NOT discard based on this — a real bug with a broken fix is still a real bug.
- **Stage 3 (verification):** The verifier now also validates the fix when confirming a finding.
- **Judge:** Receives fix validity information, strips broken fixes.

### Files to modify

- `skills/codereview/prompts/reviewer-verifier.md` — Add fix validation step
- `skills/codereview/prompts/reviewer-judge.md` — Add handling for `fix_valid: false`
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small (prompt additions only, no pipeline changes)

---

## Feature 6: Context Sufficiency Feedback Loop

**Goal:** After the cross-file context planner (v1.3 F19) collects context, evaluate whether the collected context is sufficient to find cross-file bugs. If gaps remain, generate additional search queries and do a second collection round. Currently, cross-file context collection is a single pass — if important context is missed, it's missed forever.

Inspired by Kodus-AI's `codeReviewCrossFileContextSufficiency.ts` prompt, which evaluates context completeness after the initial collection and generates additional targeted queries when gaps are detected. Their production data shows the sufficiency check triggers additional queries in ~30% of reviews, and those additional queries find real cross-file bugs that the first pass missed.

### Where it fits

Extension to v1.3 F19 (cross-file context planner). After the initial context collection, add a sufficiency evaluation step.

### Architecture

```
Step 2m: Cross-File Context Planning (v1.3 F19)
    │
    ├── Phase 1: Planner generates search queries from diff
    ├── Phase 2: Execute queries via Grep, collect results
    │
    ▼
Step 2m.5: Context Sufficiency Check (NEW)
    │
    ├── Send to LLM: original queries + which found results + collected snippets summary
    ├── LLM evaluates: sufficient or insufficient?
    │
    ├── If sufficient: proceed to Step 2h (context assembly)
    │
    └── If insufficient:
        ├── LLM returns up to 5 additional queries + gap descriptions
        ├── Execute additional queries via Grep
        ├── Merge new results with existing context
        └── Proceed to Step 2h (no further iteration — max 2 rounds total)
```

### Sufficiency criteria

Context is **sufficient** when:
1. All high-risk queries found at least some results
2. Symmetric counterparts (create/validate, encode/decode) are covered
3. Direct consumers/callers of changed public APIs are present

Context is **insufficient** when:
1. High-risk queries returned nothing for public APIs/exported types
2. Symmetric counterpart identified by planner but no consumer/verifier found
3. Test files changed but no implementation found (or vice versa)

### Prompt

New file: `prompts/reviewer-context-sufficiency.md`

```markdown
You are evaluating whether the cross-file context collected for a code review
is sufficient to detect cross-file bugs.

You receive:
- The original search queries and whether each found results
- A summary of collected code snippets (file paths, symbols, rationale)
- The changed file names and a diff summary

Evaluate:
1. Did all high-risk queries find results? If a high-risk query found nothing,
   that's a gap — the relevant code may exist under a different name.
2. Are symmetric counterparts covered? If the diff changes a create/encode/write
   operation, is the corresponding validate/decode/read operation in the context?
3. Are consumers of changed public APIs present? If a function signature changed,
   are callers in the context?

If sufficient: { "sufficient": true }

If insufficient:
{
  "sufficient": false,
  "gaps": ["verify_token() not found — symmetric counterpart of changed create_token()"],
  "additional_queries": [
    { "pattern": "\\bverify\\b.*\\btoken\\b", "rationale": "Find token verification logic", "risk_level": "high" }
  ]
}

Max 5 additional queries. Use word-boundary ripgrep patterns only.
```

### Activation

- Only runs when v1.3 F19 (cross-file planner) is active
- Only runs when at least one query found zero results (if all queries found results, context is likely sufficient)
- Max 2 rounds total (initial + one sufficiency round) — no infinite loops
- Configurable via `.codereview.yaml`:

```yaml
cross_file:
  sufficiency_check: true   # enable/disable sufficiency feedback loop
  max_rounds: 2             # max collection rounds (1 = no sufficiency check)
```

### Files to create

- `skills/codereview/prompts/reviewer-context-sufficiency.md` — Sufficiency evaluation prompt

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2m.5 after Step 2m
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: sufficient context, insufficient with gap, max rounds reached

### Effort: Medium

---

## Feature 7: Documentation Context Injection

**Goal:** Automatically discover which libraries/frameworks are used in the changed code and inject relevant documentation into the explorer context. Currently, explorers rely on the model's training data for library knowledge — which may be outdated or incomplete for newer libraries.

Inspired by Kodus-AI's multi-stage documentation pipeline: package discovery from manifest files → LLM generates documentation search queries → external search fetches relevant docs → results cached and injected into review context. Their system supports npm, pip, maven, gradle, go, cargo, and ruby ecosystems.

### Where it fits

New sub-step in Step 2 (Gather Context). Depends on `code_intel.py imports` (v1.3 F3) for reliable import detection.

### Architecture

```
Step 2n: Documentation Context (NEW)
    │
    ├── Phase 1: Discover packages
    │   ├── Read manifest files (package.json, requirements.txt, go.mod, Cargo.toml, etc.)
    │   ├── Extract dependency names and versions
    │   └── Filter to packages actually imported by changed files
    │
    ├── Phase 2: Generate documentation queries
    │   ├── For each relevant package, ask: what documentation would help review this code?
    │   ├── Focus on: API contracts, deprecation notices, breaking changes, security advisories
    │   └── Max 5 queries total (token budget)
    │
    ├── Phase 3: Fetch documentation (requires opt-in)
    │   ├── Search web for relevant docs (requires internet access or cached docs)
    │   └── Format results as context snippets
    │
    └── Include in context packet (Step 2h)
```

### Why opt-in

This feature requires either:
- Internet access for documentation search (not always available in CI/air-gapped environments)
- A pre-built documentation cache (significant setup)
- MCP tools for documentation retrieval (requires MCP server configuration)

Default: **off**. Enable via `.codereview.yaml`:

```yaml
documentation:
  enabled: false
  # Future: cache_dir, mcp_provider, allowed_domains
```

Or via CLI flag: `/codereview --with-docs`

### Minimum viable version

Without external search, we can still provide value:
1. Detect which packages are used (from manifests + imports)
2. Include package names and versions in the context packet
3. Explorers can use this to flag "this code uses library X v2.3 — check if this API was deprecated in v3"

This is the baseline. External doc search is a future enhancement.

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2n (documentation context)
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small (baseline) / Medium-Large (with external search)

---

## Feature 8: Per-Finding Numeric Scoring

**Goal:** Add a 0-10 numeric score to each finding during the judge phase, enabling threshold-based filtering and finer-grained ranking within action tiers. Currently, action tiers are computed mechanically from severity × confidence, which gives coarse 3-bucket classification. A per-finding score adds a quality signal that captures "how real and impactful is this finding" beyond what severity and confidence alone express.

Inspired by PR-Agent's (Qodo) self-reflection scoring mechanism, where a dedicated reasoning model scores each code suggestion 0-10 with calibrated scoring bands and explicit caps per issue type. See `docs/research-multi-model-council.md` § "PR-Agent (Qodo)" for the full analysis.

### Scoring bands

The judge assigns a `score` (0-10) to each finding using these calibrated bands:

| Score | Meaning | Examples |
|-------|---------|---------|
| **9-10** | Confirmed defect with evidence and clear failure mode | Verified SQL injection, proven race condition, resource leak with traced path |
| **7-8** | Likely defect, strong evidence but some uncertainty | Missing error check on fallible call, unvalidated input reaching sensitive op |
| **5-6** | Plausible issue, moderate evidence | Missing test for new public API, error swallowed without logging |
| **3-4** | Minor concern, weak evidence or low impact | Suboptimal algorithm choice, inconsistent naming in new code |
| **1-2** | Speculative, style preference, or extremely low impact | "Consider using X instead of Y", redundant comment |
| **0** | Wrong — finding is clearly invalid | Targets unchanged code, cites non-existent behavior, fix-only suggestion |

**Explicit caps** (adapted from PR-Agent):
- "Verify/ensure" suggestions (no concrete defect): max 6
- Error handling additions (defensive, not fixing a bug): max 7
- Identical finding to a deterministic tool result: max 5 (defer to tool)
- Documentation/comment suggestions: max 2

### Where it fits

Addition to the judge prompt (Feature 1's Pass 2: Synthesis). After severity calibration, the judge assigns a score to each finding. The score is written into the finding JSON alongside existing fields.

**Finding schema addition:**
```json
{
  "score": 8,
  "score_reason": "Verified: map write without nil check on error path, confirmed via Read"
}
```

### Interaction with action tiers

Scores do NOT replace action tiers — they provide finer ranking within tiers. `enrich-findings.py` gains an optional `--min-score` flag:

```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/judge.json \
  --scan-findings /tmp/scans.json \
  --min-score 3 \          # drop findings scoring below 3
  > /tmp/enriched.json
```

Within each action tier, findings are now sorted by `score` (descending) as primary key, then `severity_weight * confidence` as tiebreaker.

Configurable via `.codereview.yaml`:
```yaml
scoring:
  min_score: 0         # drop findings below this score (0 = keep all)
  show_scores: true    # include score in report output
```

### Files to modify

- `skills/codereview/prompts/reviewer-judge.md` — Add scoring instructions and bands to Pass 2
- `skills/codereview/scripts/enrich-findings.py` — Add `--min-score` filter, score-based sorting
- `skills/codereview/references/findings-schema.json` — Add `score` and `score_reason` fields
- `skills/codereview/references/design.md` — Add rationale entry (including PR-Agent comparison)

### Effort: Small (prompt addition + minor script change)

---

## Feature 9: Ticket & Task Verification

**Goal:** Automatically detect local planning artifacts (tickets, beads, plan files) that describe what the current branch should implement, and verify the implementation against them. This catches incomplete implementations, missing acceptance criteria, scope creep, and untested requirements — before the code ever reaches a PR.

Unlike PR-Agent's ticket compliance (which fetches remote GitHub/Jira issues after a PR exists), this feature works with **local, structured, machine-readable** planning artifacts that exist in the repo. Reviews often run during or right after implementation, before any PR is created.

### Planning artifact sources

Three local sources, auto-detected by priority:

| Source | Storage | Detection | Read via | Structured fields |
|--------|---------|-----------|----------|-------------------|
| **`tk` tickets** | `.tickets/*.md` | `.tickets/` dir exists | `tk show <id>`, `tk query` (JSON) | id, status, deps, acceptance, description, tags, parent, type, priority |
| **`bd` beads** | `.beads/issues.jsonl` | `.beads/` dir exists | `bd show <id>` | id, status, deps, description |
| **Plan files** | `docs/plan-*.md` | `docs/plan-*.md` glob | Read directly | Features with goals, files to create/modify, acceptance criteria |

### Auto-detection logic

New script: `scripts/detect-plan-context.sh`

```
1. Parse commit messages on current branch (since divergence from base)
   - Look for ticket/bead IDs: "feat(att-0ogy):", "fixes att-1ko9", "[nw-5c46]"
   - Regex: /\b[a-z]{2,4}-[a-z0-9]{4}\b/ (matches tk/bd ID formats)

2. Parse branch name
   - "feat/att-0ogy-claim-store" → att-0ogy
   - "fix/PROJ-123-auth" → PROJ-123 (external ref on tk ticket)

3. If IDs found and .tickets/ exists:
   - tk query to get ticket JSON for matched IDs
   - Include parent ticket if present (for epic-level context)
   - Include dep tickets (for "was this prerequisite done?")

4. If IDs found and .beads/ exists:
   - bd show for matched IDs

5. If no IDs found but docs/plan-*.md exists:
   - Heuristic: match branch name keywords against plan feature titles
   - "feat/deterministic-pipeline" → plan-treesitter.md "Feature 0: Extract..."
   - This is fuzzy — present matches to the user for confirmation if ambiguous

6. Accept explicit overrides:
   - --ticket <id>        (tk ticket)
   - --bead <id>          (bd bead)
   - --plan <file>[#N]    (plan file, optionally feature number)
```

**Output format:**
```json
{
  "source": "tk",
  "tickets": [
    {
      "id": "att-0ogy",
      "title": "Add ClaimableStore interface + engine claim methods",
      "status": "in_progress",
      "description": "Add ClaimableStore interface to state/types.go...",
      "acceptance_criteria": "...",
      "files_mentioned": ["state/types.go", "engine.go"],
      "deps": ["att-rbg7", "att-drm1"],
      "dep_statuses": { "att-rbg7": "closed", "att-drm1": "closed" },
      "parent": "att-jndm",
      "tags": ["claim-wave-3"]
    }
  ]
}
```

### Verification checks

The spec verification explorer (`prompts/reviewer-spec-verification-pass.md`) is extended to consume ticket/task context in addition to explicit `--spec` files. The verification checks are:

**Completeness checks:**
- Were all files mentioned in the ticket/plan actually modified in the diff?
- Are there "files to create" that don't exist yet?
- For each acceptance criterion: is there code in the diff that addresses it?
- Are required tests present? (ticket mentions "Add 2 engine tests" → verify test files exist)

**Scope checks:**
- Are there changed files NOT mentioned in the ticket/plan? (potential scope creep)
- Does the diff touch areas unrelated to the ticket's description?
- If the ticket has tags (e.g., `claim-wave-3`), do changes stay within that wave's scope?

**Dependency checks:**
- Are all `deps` in `closed` status? If not: "Dependency att-drm1 is still open — this implementation may be premature"
- If the ticket has a parent epic: are sibling tickets that this depends on resolved?

**Status checks:**
- Is the ticket in `in_progress` or appropriate status?
- If closed already: warn that the review is on already-completed work

### Output schema

Extends the existing `spec_requirements` array in findings-schema.json:

```json
{
  "plan_context": {
    "source": "tk",
    "ticket_id": "att-0ogy",
    "ticket_title": "Add ClaimableStore interface + engine claim methods"
  },
  "spec_requirements": [
    {
      "requirement": "Add ClaimableStore interface to state/types.go",
      "source": "ticket:att-0ogy",
      "status": "implemented",
      "evidence": "state/types.go:15 — ClaimableStore interface defined"
    },
    {
      "requirement": "Add ClaimAndDispatch to engine",
      "status": "implemented",
      "evidence": "engine.go:142 — ClaimAndDispatch method"
    },
    {
      "requirement": "Add 2 engine tests",
      "status": "partial",
      "evidence": "engine_test.go:89 — 1 test found, ticket requires 2"
    },
    {
      "requirement": "Pass lease as parameter (no engine→ticket import)",
      "status": "not_verified",
      "evidence": "Need to check import graph — could not confirm"
    }
  ],
  "scope_analysis": {
    "expected_files": ["state/types.go", "engine.go"],
    "actual_files": ["state/types.go", "engine.go", "engine_test.go", "cmd/server.go"],
    "unexpected_files": ["cmd/server.go"],
    "missing_files": [],
    "scope_note": "cmd/server.go not mentioned in ticket — verify this change is intentional"
  },
  "dependency_status": {
    "all_resolved": true,
    "deps": [
      { "id": "att-rbg7", "status": "closed" },
      { "id": "att-drm1", "status": "closed" }
    ]
  }
}
```

### Interaction with existing pipeline

- **Step 1 (parse arguments):** `detect-plan-context.sh` runs here, alongside diff generation. Its output is stored as context for later steps.
- **Step 2 (gather context):** Plan context is included in the context packet assembled at Step 2h. Explorers see what the ticket/plan says so they can flag discrepancies.
- **Step 3.5 (adaptive pass selection):** If plan context is detected, the spec-verification pass is auto-enabled (no `--spec` flag needed).
- **Step 4a (explorers):** The spec-verification explorer receives plan context and produces `spec_requirements` + `scope_analysis`. Other explorers receive a summary ("This branch implements ticket att-0ogy: Add ClaimableStore interface...") for awareness but don't perform compliance checks.
- **Feature 2 (spec-gated):** When plan context shows >50% of requirements `not_implemented`, the spec-gating logic applies — skip detailed code quality passes, report implementation gaps.
- **Judge:** Receives plan context, includes compliance summary in verdict reasoning.

### Interaction with Feature 2 (Spec-Gated Pass Execution)

Feature 2 currently requires `--spec <file>`. With ticket/task verification:
- Auto-detected plan context serves as the spec source when no `--spec` is provided
- `--spec` still takes precedence if explicitly provided (allows overriding auto-detection)
- The gating threshold (>50% must-requirements not implemented) applies to ticket-derived requirements the same way it applies to spec-derived requirements
- The skill distinguishes the source: `source: "ticket:att-0ogy"` vs `source: "spec:docs/spec.md"`

### Activation

Auto-detection is **on by default** when `.tickets/` or `.beads/` directories exist. No flag needed — if planning artifacts are present, the skill uses them.

Configurable via `.codereview.yaml`:
```yaml
plan_context:
  auto_detect: true        # scan for tickets/beads/plans
  source: "auto"           # auto | tk | bd | plan | none
  verify_deps: true        # check dependency ticket statuses
  scope_analysis: true     # flag files not mentioned in ticket
```

Or disabled: `/codereview --no-plan-context`

### Files to create

- `skills/codereview/scripts/detect-plan-context.sh` — Plan artifact detection and extraction
- `skills/codereview/prompts/reviewer-plan-compliance.md` — Plan-aware verification instructions (extends spec-verification pass)

### Files to modify

- `skills/codereview/SKILL.md` — Add plan context detection to Step 1, auto-enable spec pass in Step 3.5
- `skills/codereview/prompts/reviewer-spec-verification-pass.md` — Extend to consume ticket/task context
- `skills/codereview/references/findings-schema.json` — Add `plan_context`, `scope_analysis`, `dependency_status`
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: tk ticket detected, bd bead detected, plan file matched, no context found, scope creep flagged, deps unresolved

### Effort: Medium-Large

---

## Feature 10: Output Repair

**Goal:** Add JSON repair strategies to `validate_output.sh` so that minor formatting issues in model output don't cause the entire review to fall back to manual mode. Currently, invalid JSON triggers a hard failure. Many model output issues are mechanically fixable.

Inspired by PR-Agent's `try_fix_yaml` function, which has 7+ fallback strategies for recovering malformed model output and recovers ~80% of parsing failures.

### Repair strategies (in order)

Applied by `validate_output.sh` before schema validation:

1. **Extract from code block:** If output is wrapped in ` ```json ... ``` `, extract the content between fences
2. **Strip trailing content:** Remove text after the closing `}` or `]` (model added commentary after JSON)
3. **Fix trailing commas:** Remove commas before `}` or `]` (common model error)
4. **Fix single quotes:** Replace `'key': 'value'` with `"key": "value"` (Python-style JSON)
5. **Fix unquoted keys:** Add quotes to bare keys (`key:` → `"key":`)
6. **Truncation recovery:** If JSON is truncated (context exhaustion), close open arrays/objects and add `"truncated": true` to the envelope

### Implementation

Add a `repair_json()` function to `validate_output.sh` that runs before the existing validation checks. If repair succeeds, write the repaired JSON back and continue validation. If repair fails, fall back to the existing error path.

```bash
repair_json() {
  local input="$1"
  local repaired

  # Strategy 1: extract from code block
  repaired=$(sed -n '/^```json/,/^```/{ /^```/d; p; }' "$input")
  if [ -n "$repaired" ] && echo "$repaired" | jq . >/dev/null 2>&1; then
    echo "$repaired" > "$input"
    echo "Repaired: extracted from code block" >&2
    return 0
  fi

  # Strategy 2-6: progressive fixes on raw content
  # ... (each strategy tries jq validation after the fix)
}
```

### Interaction with pipeline

- `validate_output.sh` is called after the judge produces output (Step 4b) and after enrichment (Step 5)
- Repair attempts are logged to stderr for transparency: "Repaired: extracted from code block" or "Repaired: fixed 2 trailing commas"
- If all repair strategies fail, the existing error path applies (fallback to manual)
- The `repaired` flag is written into the JSON envelope so downstream consumers know the output was auto-fixed

### Files to modify

- `skills/codereview/scripts/validate_output.sh` — Add `repair_json()` before validation
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 11: Adaptive Expert Panel

**Goal:** Replace the fixed roster of 4 core + 4 extended explorers with a **change-type-driven panel** where the expert roster is assembled dynamically based on what's actually in the diff. This enables deep domain experts (shell/script, database migrations, Kubernetes manifests) without bloating every review, and avoids paying for experts whose domain isn't present in the change.

Motivated by CodeRabbit gap analysis: findings #1, #5, #6, #14, #15, #24, #26, #27, #29 were all missed because our fixed panel has no expert calibrated for shell/script semantics. Rather than adding calibration to every existing explorer, this feature creates a dedicated Shell & Script expert that activates only when the diff contains shell/script files — and establishes the architectural pattern for future domain experts.

### Current architecture (fixed panel)

```
Always run:  correctness, security, reliability, test-adequacy  (4 core)
Skip logic:  error-handling, api-contract, concurrency, spec-verification  (4 extended, skip signals)
```

The extended passes have simple skip signals (e.g., concurrency skips if no concurrency primitives in diff). But the core passes always run, and no new experts can join based on diff content.

### Proposed architecture (adaptive panel)

```
Always run:     correctness, security, test-adequacy  (3 core — reliability merged, see below)
Activated by    Shell & Script expert        ← .sh, .bash, .ps1, .bat, .cmd, Makefile, Dockerfile
diff content:   API & Contract expert        ← route/endpoint/handler defs, schema files, proto/graphql
                Concurrency expert           ← async/await, goroutines, threads, locks, channels
                Error Handling expert        ← try/catch, if err, Result::Err, rescue, recover
                Reliability & Perf expert    ← resource allocation, external calls, loops, caches
                Spec Verification expert     ← --spec flag provided
                [future] Database expert     ← migration files, schema changes, SQL
                [future] Infrastructure expert ← Kubernetes manifests, Terraform, CI/CD configs
```

**Key changes from current:**
1. **Reliability moves from core to activated** — it's most valuable when the diff touches resource management, external calls, or hot paths. On a diff that's pure business logic, the correctness pass already covers logic bugs.
2. **Error handling stays as an activated expert** (unchanged) but gains shell-specific calibration when Shell expert also activates (see interaction model below).
3. **Shell & Script expert is new** — deep knowledge of bash semantics, POSIX portability, script error patterns.
4. **The activation step replaces the current skip logic** (Step 3.5) with a more general mechanism that both activates and skips.

### Activation signals

The orchestrator analyzes `CHANGED_FILES` and `DIFF` content in Step 3.5 (renamed to "Expert Panel Assembly") and builds the expert roster:

| Expert | Activation signal | Files/patterns |
|--------|------------------|----------------|
| Correctness | Always | — |
| Security | Always | — |
| Test Adequacy | Always | — |
| Shell & Script | File extension | `.sh`, `.bash`, `.zsh`, `.ps1`, `.bat`, `.cmd`, `Makefile`, `Justfile`, `Dockerfile`, `*.dockerfile` |
| API & Contract | Diff content | `route\|endpoint\|handler\|@app\.\|@api\.\|@router\.\|export (function\|class\|interface)\|func [A-Z]\|pub fn\|\.proto\|\.graphql\|openapi\|swagger` |
| Concurrency | Diff content | `goroutine\|go func\|threading\|Thread\|async def\|asyncio\|\.lock\(\|mutex\|chan \|channel\|atomic\|sync\.\|Promise\.all\|Worker\(\|spawn\|tokio` |
| Error Handling | Diff content | `catch\|except\|rescue\|recover\|if err\|Result::Err\|try/catch\|\.catch\(\|on_error` |
| Reliability & Perf | Diff content | `open\(\|connect\(\|pool\|timeout\|retry\|cache\|\.close\(\|defer\|context\.WithTimeout\|http\.Get\|fetch\(` |
| Spec Verification | Flag | `--spec` provided |

**Assembly pseudocode (replaces Step 3.5):**

```bash
# Core experts (always run)
EXPERTS=("correctness" "security" "test-adequacy")

# Detect shell/script files
if echo "$CHANGED_FILES" | grep -qiE '\.(sh|bash|zsh|ps1|bat|cmd)$|Makefile$|Justfile$|Dockerfile'; then
  EXPERTS+=("shell-script")
fi

# Detect API surface changes
if echo "$DIFF" | grep -qE 'route|endpoint|handler|@app\.|@api\.|@router\.|export (function|class|interface)|\.proto|\.graphql|openapi|swagger'; then
  EXPERTS+=("api-contract")
fi

# Detect concurrency primitives
if echo "$DIFF" | grep -qiE 'goroutine|go func|threading|Thread|async def|asyncio|\.lock\(|mutex|chan |channel|atomic|sync\.|Promise\.all|Worker\(|spawn|tokio'; then
  EXPERTS+=("concurrency")
fi

# Detect error handling constructs
if echo "$DIFF" | grep -qiE 'catch|except|rescue|recover|if err|Result::Err|\.catch\(|on_error'; then
  EXPERTS+=("error-handling")
fi

# Detect reliability/performance concerns
if echo "$DIFF" | grep -qiE 'open\(|connect\(|pool|timeout|retry|cache|\.close\(|defer|context\.WithTimeout|http\.Get|fetch\('; then
  EXPERTS+=("reliability")
fi

# Spec verification (flag-based)
if [ -n "$SPEC_CONTENT" ]; then
  EXPERTS+=("spec-verification")
fi

log "Expert panel: ${EXPERTS[*]} (${#EXPERTS[@]} experts)"
```

### Shell & Script Expert

Shared prompt file: `prompts/reviewer-reliability-performance-pass.md`

**Pass value:** `reliability` (shell issues are typically reliability/correctness, not a new category)

**Investigation Phases:**

```markdown
You are the Shell & Script expert. Your focus: bash/shell correctness, portability,
error handling, and security patterns specific to shell scripts. You have deep
knowledge of POSIX shell semantics, bash-specific features, and cross-platform
compatibility issues.

---

## Investigation Phases

### Phase 1 — set -e Interaction Analysis
For each script using `set -e`, `set -euo pipefail`, or `errexit`:
1. **Identify all command substitutions** (`$(...)`, backticks). Under `set -e`,
   a failed command inside `$()` on an assignment line does NOT trigger exit in
   some shells (bash 4.4+), but DOES in assignment-then-use patterns and in
   subshells. Trace each command substitution to determine if failure is caught.
2. **Identify all conditional patterns** (`if cmd; then`, `cmd || handler`,
   `cmd && next`). Under `set -e`, these are exempt — failures inside conditions
   don't trigger exit. But `set -e` DOES trigger on the NEXT uncaught failure.
3. **Check jq/awk/sed pipelines** — under `set -eo pipefail`, a failed jq in a
   pipeline (`cat file | jq . | grep key`) causes the entire pipeline to fail.
   Verify the script handles this (e.g., `|| true`, `|| echo default`).
4. **Trace early-abort paths** — if a command fails and `set -e` triggers, what
   state is left behind? Temp files not cleaned up? Locks not released? Partial
   output written?

### Phase 2 — POSIX Portability
For scripts that claim POSIX or Bash 3 compatibility:
1. **Grep for non-POSIX constructs:**
   - `\s`, `\d`, `\w` in `grep -E` (use `[[:space:]]`, `[[:digit:]]`, `[[:alnum:]_]`)
   - `[[ ]]` double brackets (use `[ ]` for POSIX)
   - `local -r`, `local -a` (not in POSIX `local`)
   - `mapfile`/`readarray` (bash 4+ only)
   - `${var,,}` / `${var^^}` case modification (bash 4+ only)
   - `&>` redirect (use `>file 2>&1`)
   - `<(process substitution)` (not POSIX)
2. **Check shebang** — `#!/usr/bin/env bash` vs `#!/bin/sh`. If shebang says sh,
   all bash-isms are bugs.
3. **Check tool-specific portability:**
   - BSD `sed` vs GNU `sed` (`-i` flag behavior differs)
   - BSD `grep` vs GNU `grep` (ERE `\s` not supported in BSD)
   - `date` flags differ between BSD (macOS) and GNU
   - `mktemp` template syntax differs slightly

### Phase 3 — Error Swallowing Analysis
For each `|| true`, `2>/dev/null`, `|| :`, or `|| echo` pattern:
1. **Classify intent:** Is this swallowing expected errors (cleanup, optional
   features) or hiding real failures?
   - **Intentional:** `rm -f "$tmpfile" 2>/dev/null || true` — file may not exist, that's fine
   - **Harmful:** `chmod +x scripts/*.sh 2>/dev/null || true` — hides permission errors
   - **Harmful:** `jq '.key' "$file" 2>/dev/null || true` — hides malformed JSON
2. **Check scope:** Does `|| true` apply to just one command or to a pipeline?
   `cmd1 | cmd2 || true` catches cmd2 failure but not cmd1 (with pipefail).
3. **Check downstream assumptions:** If the command fails silently, does subsequent
   code assume it succeeded? E.g., `mkdir -p "$dir" 2>/dev/null || true` followed
   by `echo "data" > "$dir/file"` — the write fails if mkdir failed.

### Phase 4 — Dependency Validation
For each external tool the script uses extensively:
1. Check if the script validates the tool's availability upfront (before doing
   work that depends on it).
2. If not, trace what happens when the tool is missing — does it fail fast with
   a clear error, or silently produce wrong/empty output across many invocations?
3. Common pattern: script requires `jq` for 30+ operations but never checks
   `command -v jq`. Missing jq causes 30 silent failures instead of one clear error.

### Phase 5 — String Interpolation Safety
For bash scripts that construct structured data (JSON, YAML, XML, SQL):
1. **Identify construction patterns:**
   - Heredoc with variable substitution: `cat <<EOF ... ${var} ... EOF`
   - echo/printf with variables: `echo "{\"key\": \"$value\"}"`
   - String concatenation: `json="$json,\"$key\":\"$val\""`
2. **Check for injection:** Can the variable contain characters that break the
   format? Quotes in JSON values, newlines in YAML, semicolons in SQL.
3. **Check for safe alternatives:** Does the script have access to `jq -n --arg`
   (for JSON), `python3 -c` (for any format), or format-specific escaping?
4. Report the injection vector AND the safe alternative.

### Phase 6 — Redirect and Pipe Semantics
1. **stdin override:** When a command receives input from both a pipe AND a
   redirect (`echo data | cmd < file`), the redirect wins. The piped data is
   lost. This is a common test bug — piping test data into a function that
   also has a `< source_file` redirect.
2. **stdout/stderr capture:** `result=$(cmd 2>&1)` captures both. `result=$(cmd)`
   captures only stdout. If `cmd` writes errors to stderr and the script checks
   `$result`, stderr content is lost.
3. **Subshell variable isolation:** `echo data | while read line; do VAR=$line; done`
   — the `while` runs in a subshell (due to pipe). `$VAR` is NOT set after the
   loop. Use `while read line; do ...; done < <(echo data)` or a temp file.

---

## Calibration Examples

### True Positive — set -e Interaction (High Confidence)
```json
{
  "pass": "reliability",
  "severity": "high",
  "confidence": 0.90,
  "file": "scripts/validate_output.sh",
  "line": 113,
  "summary": "set -e aborts script when jq fails on malformed .findings, preventing error summary",
  "evidence": "Line 113-120: type check detects non-array .findings and increments ERRORS. But line 125: BAD_FINDING_COUNT=$(jq '[.findings[] | ...]' ...) — jq .findings[] fails with exit 5 when findings is not an array. With set -euo pipefail (line 35), this aborts the script before reaching the RESULT: FAIL summary at line 413. The user sees a raw jq error instead of a structured failure report.",
  "failure_mode": "On malformed input, the validation script crashes instead of reporting a clean FAIL. The review pipeline sees exit code 5 (jq error) instead of exit code 1 (validation failure).",
  "fix": "After the type check fails, set FINDING_COUNT=0 and skip all per-finding validation blocks: if [ \"$(jq '.findings | type' ...)\" != '\"array\"' ]; then FINDING_COUNT=0; else FINDING_COUNT=$(jq '.findings | length' ...); fi"
}
```

### True Positive — JSON Injection via Bash Interpolation (Medium Confidence)
```json
{
  "pass": "security",
  "severity": "medium",
  "confidence": 0.80,
  "file": "scripts/timing.sh",
  "line": 55,
  "summary": "Variable $name interpolated directly into JSON string without escaping",
  "evidence": "Line 55: echo \"{\\\"type\\\":\\\"start\\\",\\\"name\\\":\\\"$name\\\",\\\"ts\\\":$ts}\" >> \"$TIMING_FILE\". The $name variable comes from the first positional argument (line 48). A step name containing a double-quote or backslash (e.g., 'step \"A\"') produces malformed JSONL. Downstream timing summary (line 80) uses jq to parse the JSONL — malformed entry causes jq to fail and summary falls back to zeros.",
  "failure_mode": "Timing data is silently lost when any step name contains JSON-special characters. The summary shows all zeros with no error.",
  "fix": "Use jq for JSON construction: jq -n --arg name \"$name\" --argjson ts \"$ts\" '{type:\"start\",name:$name,ts:$ts}' >> \"$TIMING_FILE\""
}
```

### False Positive — Do NOT Report
**Scenario:** Script uses `rm -f "$tmpfile" 2>/dev/null || true` in a cleanup block.
**Investigation:** The tmpfile may not exist if an earlier step failed. The `rm -f` flag already handles missing files, and `|| true` handles permission errors in cleanup. The script doesn't depend on the cleanup succeeding.
**Why suppress:** Best-effort cleanup where failure is expected and harmless. The main operation's error handling is separate.

---

## False Positive Suppression

Do NOT report:
- **`|| true` on cleanup/teardown operations** (temp files, locks, cache dirs) where failure is harmless
- **Non-POSIX constructs** in scripts with `#!/usr/bin/env bash` shebang that don't claim POSIX compatibility
- **Missing tool check** for tools used only once with proper error handling on that single invocation
- **`2>/dev/null`** on optional/informational commands (version checks, feature detection)
- **Heredoc string interpolation** when the variable is guaranteed to be a safe value (e.g., integer from `wc -l`, filename from a controlled list)

---

Return ALL findings. Use `pass: "reliability"` for shell correctness findings,
`pass: "security"` for injection findings.
Use the JSON schema from the global contract.
```

### Expert interaction model

When multiple activated experts have overlapping concerns, the judge resolves conflicts:

1. **Shell expert + Error handling expert both active:** The shell expert focuses on shell-specific error patterns (`set -e`, `|| true`, dependency checks). The error handling expert focuses on application-level patterns (try/catch, error returns). No overlap in practice — they cover different language families.

2. **Shell expert + Security expert both active:** The shell expert checks string interpolation safety (Phase 5). The security expert checks trust boundaries and data flow. The shell expert may flag JSON injection; the security expert may flag command injection. The judge deduplicates if both flag the same line.

3. **Shell expert finds correctness bugs:** Shell scripts can have logic bugs (wrong grep flags, incorrect conditionals). The shell expert reports these with `pass: "correctness"`. The judge deduplicates with any overlapping correctness explorer findings.

### SKILL.md changes

Replace **Step 3.5 (Adaptive Pass Selection)** with **Step 3.5 (Expert Panel Assembly)**:

```
Step 3.5: Expert Panel Assembly

Analyze CHANGED_FILES and DIFF content to build the expert roster for this review.

Core experts (always run):
- Correctness
- Security
- Test Adequacy

Activated experts (run if activation signal detected):
- Shell & Script: .sh/.bash/.ps1/.bat/.cmd/Makefile/Dockerfile in CHANGED_FILES
- API & Contract: route/endpoint/handler/schema definitions in DIFF
- Concurrency: async/goroutine/thread/mutex/channel primitives in DIFF
- Error Handling: try/catch/except/if-err constructs in DIFF
- Reliability & Performance: resource/connection/timeout/cache patterns in DIFF
- Spec Verification: --spec flag provided

Log: "Expert panel: correctness, security, test-adequacy, shell-script, error-handling (5 experts)"

If force_all_passes: true in config, activate all experts regardless of signals.
```

### Configuration

Configurable via `.codereview.yaml`:

```yaml
# Expert panel configuration
experts:
  # Core experts (always run, cannot be disabled)
  core: ["correctness", "security", "test-adequacy"]

  # Activated experts with custom activation signals
  # Set to false to disable an expert entirely
  shell-script: true          # default: activated by file extension
  api-contract: true          # default: activated by diff content
  concurrency: true           # default: activated by diff content
  error-handling: true        # default: activated by diff content
  reliability: true           # default: activated by diff content
  spec-verification: true     # default: activated by --spec flag

  # Force all experts regardless of activation signals
  force_all: false

  # Model overrides per expert (same as current pass_models)
  models:
    shell-script: "sonnet"
    security: "opus"           # use stronger model for security
```

### Files to create

- `skills/codereview/prompts/reviewer-reliability-performance-pass.md` — Reliability/shell expert prompt (shared prompt content above)

### Files to modify

- `skills/codereview/SKILL.md` — Replace Step 3.5 with Expert Panel Assembly; add shell-script to explorer table in Step 4a; update configuration section
- `skills/codereview/references/design.md` — Add rationale: why adaptive panel, why shell expert, CodeRabbit gap analysis provenance
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: shell files activate shell expert, no shell files skip it, force_all overrides, shell expert finds set-e interaction bug, shell expert finds JSON injection
- `docs/CONFIGURATION.md` — Add experts section documentation

### Interaction with existing pipeline

- **Step 3.5**: Replaces current adaptive skip logic with the panel assembly mechanism
- **Step 4a**: Explorer launch table becomes dynamic — iterate over `EXPERTS` array instead of hardcoded list
- **Step 4b (judge)**: No change — judge receives findings from whatever experts ran
- **Step 4-L (chunked mode)**: Panel assembly runs once globally; same experts review all chunks
- **Feature 0 (verification round)**: Verification runs on findings from all activated experts, regardless of which experts were activated. No interaction.
- **Feature 1 (two-pass judge)**: No interaction — judge structure is independent of which experts ran

### Migration path

Phase 1: Add Shell & Script expert and panel assembly mechanism. Keep existing 4 core + 4 extended as-is but rebrand as "core + activated."
Phase 2: Move reliability from core to activated. Merge its activation signal with the existing extended-pass skip logic.
Phase 3: Add future domain experts (database, infrastructure) as the patterns are identified.

### Effort: Medium

The shell expert prompt is the bulk of the work (written above). The panel assembly mechanism is a refactor of existing Step 3.5 logic. Configuration is a small extension to the existing `.codereview.yaml` schema.

---

## Execution Order

```text
Feature 0 (verification round)      ← architectural, do first
    │
    ├── Feature 1 (two-pass judge)   ← restructures judge prompt, depends on Feature 0 design
    ├── Feature 2 (spec-gated)       ← modifies Step 4a, independent of Features 0-1
    ├── Feature 3 (review summary)   ← report template change, independent
    ├── Feature 4 (spot-check)       ← optional, independent, can be deferred further
    ├── Feature 5 (fix validation)   ← extends Feature 0 Stage 3, depends on Feature 0 [from Kodus-AI analysis]
    ├── Feature 6 (context sufficiency) ← extends v1.3 F19, independent of Features 0-4 [from Kodus-AI analysis]
    ├── Feature 7 (doc injection)    ← independent, opt-in, can be deferred [from Kodus-AI analysis]
    ├── Feature 8 (per-finding scoring) ← extends Feature 1 judge prompt, small [from PR-Agent analysis]
    ├── Feature 9 (ticket/task verification) ← extends Feature 2, independent of Features 0-1 [from PR-Agent analysis]
    ├── Feature 10 (output repair)   ← independent, small [from PR-Agent analysis]
    └── Feature 11 (adaptive panel)  ← refactors Step 3.5 + adds shell expert [from CodeRabbit gap analysis]
```

**Feature 0** is the foundation — it establishes the verification step that Features 1 and 5 build on. Features 2-4 and 6-7 are independent of each other and of Feature 0.

**Feature 4** is optional and can be deferred to v1.5 if Verification Pipeline scope needs trimming. It depends on the multi-model council research (`docs/research-multi-model-council.md`) for design decisions.

**Feature 5** is a small extension to Feature 0's verifier prompt — do it immediately after Feature 0.

**Feature 6** depends on v1.3 F19 (cross-file planner) being implemented first. If Feature 12 is not yet done when Verification Pipeline starts, defer Feature 6.

**Feature 11** is independent of all other features — it refactors how experts are selected (Step 3.5) and adds the shell expert. Can be done in parallel with Feature 0. The shell expert prompt is fully specified and ready to implement.

**Feature 7** is opt-in and can be deferred indefinitely. The baseline version (package detection without external search) is trivial; the full version requires external service integration.

**Feature 8** is a small judge prompt addition that pairs naturally with Feature 1 (two-pass judge). Do it alongside or right after Feature 1.

**Feature 9** is the highest-value new feature from the PR-Agent analysis. It extends Feature 2 (spec-gated execution) to auto-detect planning artifacts instead of requiring `--spec`. Can be built independently of Features 0-1 but benefits from Feature 2 being done first (reuses the spec-gating infrastructure). Depends on `tk` and/or `bd` being installed for full value; degrades to plan-file-only matching otherwise.

**Feature 10** is small and independent — can be done at any time. Pure script work, no prompt changes.

### Total files to create

| File | Feature |
|------|---------|
| `skills/codereview/prompts/reviewer-feature-extractor.md` | 0 |
| `skills/codereview/prompts/reviewer-verifier.md` | 0, 5 |
| `skills/codereview/prompts/reviewer-spot-check.md` | 4 |
| `skills/codereview/prompts/reviewer-context-sufficiency.md` | 6 |
| `skills/codereview/scripts/detect-plan-context.sh` | 9 |
| `skills/codereview/prompts/reviewer-plan-compliance.md` | 9 |
| `skills/codereview/prompts/reviewer-reliability-performance-pass.md` | 11 |

### Total files to modify

| File | Features |
|------|----------|
| `skills/codereview/SKILL.md` | 0, 2, 4, 6, 7, 9, 11 |
| `skills/codereview/prompts/reviewer-judge.md` | 0, 1, 3, 5, 8 |
| `skills/codereview/prompts/reviewer-spec-verification-pass.md` | 9 |
| `skills/codereview/scripts/enrich-findings.py` | 8 |
| `skills/codereview/scripts/validate_output.sh` | 10 |
| `skills/codereview/references/report-template.md` | 3 |
| `skills/codereview/references/findings-schema.json` | 8, 9 |
| `skills/codereview/references/design.md` | 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 |
| `skills/codereview/references/acceptance-criteria.md` | 0, 2, 4, 5, 6, 9, 11 |
| `docs/CONFIGURATION.md` | 11 |
