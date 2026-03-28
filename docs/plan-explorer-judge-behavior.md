# Plan: Explorer & Judge Behavior

Improve review precision and quality through prompt engineering, judge architecture changes, and finding enrichment. Quick wins (phantom knowledge detection, mental execution framing) can start immediately. Judge changes (certification, completeness gate, batching) should be coordinated. Provenance features depend on enrich-findings.py from the Context Enrichment plan.

## Status

| Feature | Status | Notes |
|---------|--------|-------|
| F10: Phantom Knowledge Self-Check | Not started | Quick win — prompt only |
| F11: Mental Execution Framing | Not started | Quick win — prompt only |
| F4: Test Pyramid Vocabulary | Not started | Prompt + schema |
| F5: Per-File Certification | Not started | Judge + global contract |
| F6: Contract Completeness Gate | Not started | Judge + spec-verification |
| F7: Output File Batching | Not started | Judge + global contract + orchestrate.py |
| F8: Pre-Existing Bug Classification | Not started | Depends on enrich-findings.py |
| F9: Provenance-Aware Review Rigor | Not started | Depends on enrich-findings.py |

## Architecture Context

The code review pipeline uses an explorer/judge architecture:

- **Explorers** are parallel sub-agents, each with a specialty pass (correctness, security, reliability, test-adequacy, etc.). They follow a shared investigation protocol defined in `reviewer-global-contract.md` and produce findings arrays.
- **The judge** (`reviewer-judge.md`) uses a 4-expert panel — Gatekeeper, Verifier, Calibrator, Synthesizer — applied sequentially. Gatekeeper pre-filters obvious false positives. Verifier checks evidence with tools. Calibrator adjusts severity and deduplicates. Synthesizer produces the final verdict and report.
- **The global contract** (`reviewer-global-contract.md`) defines the shared investigation protocol, output schema, confidence calibration table, and rules all explorers follow.
- **Each explorer** has its own pass-specific prompt (e.g., `reviewer-correctness-pass.md`, `reviewer-test-adequacy-pass.md`) with investigation phases, calibration examples, and false positive suppression rules.
- The security explorer has been split into `security-dataflow` + `security-config` (done).
- The `suggest_missing_tests` flag exists (default: off), suppressing "add test" suggestions in non-test-adequacy passes.
- File-level triage is enabled by default — files are risk-tiered before explorer dispatch.
- The orchestrator (`scripts/orchestrate.py`) manages pipeline execution, explorer dispatch, and finding collection.

## Execution Order

**Wave 1** (immediate, parallel — prompt-only, no dependencies):
- F10 (Phantom Knowledge) — edit `reviewer-global-contract.md`
- F11 (Mental Execution) — edit `reviewer-correctness-pass.md`

**Wave 2** (parallel, but coordinate on shared files):
- F4 (Test Pyramid) — edit `reviewer-test-adequacy-pass.md` + `findings-schema.json`
- F5 (Per-File Certification) — edit `reviewer-judge.md` + `reviewer-global-contract.md`
- F6 (Contract Completeness Gate) — edit `reviewer-judge.md` + `reviewer-spec-verification-pass.md`
- F7 (Output File Batching) — edit `reviewer-judge.md` + `reviewer-global-contract.md` + `scripts/orchestrate.py`

  Warning: F5, F6, F7 all touch `reviewer-judge.md` — serialize these three.

**Wave 3** (depends on `enrich-findings.py` from Context Enrichment plan):
- F8 (Pre-Existing Bug Classification) — edit `reviewer-global-contract.md` + `reviewer-correctness-pass.md` + schema + `enrich-findings.py`
- F9 (Provenance-Aware Rigor) — edit `reviewer-global-contract.md` + `scripts/orchestrate.py` + `enrich-findings.py`

## File Conflict Matrix

| File | Features | Coordination |
|------|----------|-------------|
| `reviewer-global-contract.md` | F5, F7, F8, F9, F10 | F10 adds independent section; F5/F7 coordinate; F8/F9 after enrich-findings |
| `reviewer-judge.md` | F5, F6, F7 | Serialize these three |
| `reviewer-correctness-pass.md` | F8, F11 | Independent sections |
| `findings-schema.json` | F4, F8 | Independent fields |
| `enrich-findings.py` | F8, F9 | Both in Wave 3, can parallelize |
| `reviewer-test-adequacy-pass.md` | F4 | No conflicts |
| `reviewer-spec-verification-pass.md` | F6 | No conflicts |
| `scripts/orchestrate.py` | F7, F9 | Different sections — can parallelize |

---

## Feature 10: Phantom Knowledge Self-Check

**Goal:** Add an explicit self-check framework to the global contract that forces explorers to verify they aren't hallucinating about code they cannot see. This is the #1 source of false positives in LLM-based code review — the model claims how unseen code behaves and builds findings on that phantom knowledge.

Inspired by analysis of the Kodus-AI code review platform, which embeds "Phantom Knowledge Detection" as a core guardrail throughout their review prompts. Their safeguard pipeline identifies `targets_unchanged_code` and `requires_assumed_input` as the most common false positive triggers.

### Where it fits

Addition to `prompts/reviewer-global-contract.md` — a new section after the existing Calibration section. No pipeline changes, no scripts, no schema changes.

### Current state of reviewer-global-contract.md

The global contract currently has these sections:
1. Rules (7 numbered rules)
2. Chain-of-Thought Investigation Protocol (Phase 1-4)
3. Output Schema
4. Chunked Review Mode

The final rule (rule 7) says: "If you find something outside your focus area, include it anyway — the judge will route it."

The Output Schema section ends with: `Return [] if no issues found in your focus area.`

The Calibration section (Phase 4) covers confidence ranges from 0.65-0.95.

### Prompt additions

Add the following section to `reviewer-global-contract.md` after the existing Phase 4 (Confidence Calibration), before the Output Schema section:

```markdown
## Self-Check: Phantom Knowledge Detection

Before finalizing ANY finding, perform this self-check. Phantom knowledge — making
claims about code you cannot see — is the #1 source of false positives.

**The Rule:** If your finding depends on how code you CANNOT see behaves, STOP.
You are hallucinating.

**Self-check questions (ask all 4 before every finding):**

1. **Am I claiming how unseen code behaves?**
   "The auth system hashes the full key" — can you see the auth system?
   "These are executed as separate calls" — can you see the caller?
   "The default limit is 100" — can you see the config?
   If you cannot point to a specific visible line → DO NOT make the claim.

2. **Am I assuming what an imported function returns or accepts?**
   If code imports `validate_token()` from another file and you cannot see that file,
   you CANNOT claim it "returns None on invalid tokens" or "expects a string argument."
   Only analyze what you can see being used in the visible code.

3. **Am I assuming database schema, API contracts, or external system behavior?**
   "The database column is NOT NULL" — can you see the migration?
   "The API returns a 401 on invalid tokens" — can you see the API code?
   If not, these are assumptions, not findings.

4. **Am I building a finding on an assumption from questions 1-3?**
   A chain of reasoning that starts with an assumption produces a speculative finding,
   no matter how rigorous the rest of the chain is.

**Common traps (red flags in your own output):**
- "The implementation does Y" — verify Y is visible
- "The caller expects..." — verify you traced the caller
- "The system will..." — verify you can see the system
- "This is inconsistent with how X works" — verify you can see X

**Exception:** Code gathered during your investigation (via Read/Grep/Glob)
IS visible evidence. Cross-file context provided in the context packet IS visible.
The self-check only applies to claims about code you never read.
```

### Why this is high-impact

Kodus-AI's production data shows that phantom knowledge findings account for the largest share of false positives. Their "Edward" gatekeeper persona exists primarily to catch this pattern. By embedding the self-check directly into our explorer contract, we catch these at the source instead of filtering them later.

This pairs well with Feature 5 (per-file certification) — certification forces explorers to document what they checked, and the phantom knowledge self-check forces them to verify their claims before reporting.

### Files to modify

- `skills/codereview/prompts/reviewer-global-contract.md` — Add Phantom Knowledge Self-Check section
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Tiny (one prompt section, ~30 lines)

---

## Feature 11: Mental Execution Framing for Correctness Explorer

**Goal:** Reframe the correctness explorer's investigation from pattern-matching to mental code execution. Instead of looking for code that *matches known bug patterns*, the explorer *mentally simulates execution* through changed code paths and reports where execution definitively breaks.

Inspired by analysis of the Kodus-AI code review platform, whose v2 system prompt frames the reviewer as a "Bug-Hunter" performing mental simulation through multiple execution contexts. Their approach produces more concrete findings with traceable execution paths because it forces the LLM to reason about actual runtime behavior rather than surface patterns.

### Where it fits

Enhancement to `prompts/reviewer-correctness-pass.md` — adds a mental execution protocol to the existing investigation phases. No pipeline changes, no scripts, no schema changes.

### Current state of reviewer-correctness-pass.md

The correctness pass currently has these investigation phases:
- Phase 1 — Diff Scan
- Phase 2 — Caller Trace
- Phase 3 — Boundary Analysis (with default/empty parameter analysis, truth table enumeration)
- Phase 4 — State Invariant Check (with multi-run state reasoning)
- Phase 5 — Backward Compatibility
- Phase 6 — Default/Skip Path Analysis
- Phase 7 — Serialization Boundary Tracing
- Phase 8 — Cross-Function Data Contract Tracing

Followed by: Calibration Examples, False Positive Suppression, Investigation Tips.

### Prompt additions

Add the following section to `reviewer-correctness-pass.md` as a preamble before Phase 1 (Diff Scan):

```markdown
## Mental Execution Protocol

For each changed function, do not pattern-match — mentally execute the code.
Trace variable values, follow control flow, and identify where execution breaks.

### Execution Contexts

Simulate the changed code in these contexts (check all that apply):

1. **Repeated invocations** — Does state accumulate incorrectly across calls?
   Check mutable default arguments, module-level caches, class attributes
   that persist between method calls.

2. **Concurrent execution** — What breaks when two threads/goroutines/requests
   hit this code simultaneously? Check shared mutable state, read-modify-write
   sequences without locks.

3. **Delayed execution** — For callbacks, closures, deferred functions: what
   variable values exist when the code ACTUALLY runs vs when it was scheduled?
   Check loop variable capture, closure over mutable references.

4. **Failure mid-operation** — If this function fails halfway through, what
   state is left behind? Check partial writes, uncommitted transactions,
   resources acquired but not released on error paths.

5. **Cardinality analysis** — Are N operations performed when M unique operations
   would suffice (M << N)? Check loops that do redundant work, repeated
   allocations, duplicate network calls.

### What to report

Only report issues where you can trace the EXACT execution path:
- Specific input values that trigger the issue
- Step-by-step execution showing the failure
- The specific line where behavior is wrong
- The concrete incorrect result

Do NOT report: "this could potentially fail if..." — either trace the failure
or don't report it.
```

### Calibration example addition

Add one calibration example to `reviewer-correctness-pass.md` in the Calibration Examples section, after the existing examples:

```json
// TRUE POSITIVE (mental execution traced the failure)
{
  "pass": "correctness",
  "severity": "high",
  "confidence": 0.90,
  "file": "src/cache.py",
  "line": 34,
  "summary": "Mutable default argument accumulates state across calls",
  "evidence": "Mental execution: def process(items, seen={}): ... First call: seen={}, works correctly. Second call: seen still contains entries from first call (mutable default persists). Third call: seen grows further. After N calls, seen contains all items ever processed, causing memory growth and incorrect deduplication.",
  "failure_mode": "Memory leak + incorrect behavior: items processed in earlier calls are treated as 'already seen' in later calls"
}
```

### Files to modify

- `skills/codereview/prompts/reviewer-correctness-pass.md` — Add mental execution protocol preamble, add calibration example
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Tiny (one prompt section + one calibration example)

---

## Feature 4: Test Pyramid Vocabulary

**Goal:** Update the test-adequacy explorer prompt to use structured test pyramid levels (L0-L7) and bug-finding levels (BF1-BF9) when classifying test gaps. This makes test gap findings more actionable by telling the user exactly what *kind* of test is needed, not just that "a test is missing."

Inspired by the AgentOps standards skill's test pyramid classification, adapted for our explorer output format.

**Note:** The `suggest_missing_tests` flag already exists (default: off) and suppresses "add test" suggestions in non-test-adequacy passes. This feature enhances the test-adequacy pass itself, which is not affected by the flag — test-adequacy findings are always emitted when the pass runs.

### Where it fits

Prompt modification + minor schema addition. Changes `prompts/reviewer-test-adequacy-pass.md` (vocabulary and calibration examples) and `findings-schema.json` (3 optional fields). No pipeline changes, no new scripts.

### Test pyramid levels (for classifying existing tests)

| Level | Name | What It Catches | Example |
|-------|------|----------------|---------|
| L0 | Contract/Spec | Spec boundary violations | Schema validation, API contract tests |
| L1 | Unit | Logic bugs in isolated functions | `test_calculate_discount()` |
| L2 | Integration | Module interaction bugs | DB + service layer together |
| L3 | Component | Subsystem-level failures | Auth service end-to-end |
| L4 | Smoke | Critical path regressions | Login -> dashboard flow |
| L5 | E2E | Full system behavior | Browser test of complete user journey |

### Bug-finding levels (for classifying what's missing)

| Level | Name | What It Finds | When Needed |
|-------|------|--------------|-------------|
| BF1 | Property | Edge cases from randomized inputs | Data transformations, parsers |
| BF2 | Golden/Snapshot | Output drift | Serializers, formatters, template renderers |
| BF4 | Chaos/Negative | Unhandled failures | External API calls, DB operations, file I/O |
| BF6 | Regression | Reintroduced bugs | Any area with a history of fixes |
| BF8 | Backward compat | Breaking changes | Public APIs, serialization formats |

### How the explorer uses these

The test-adequacy explorer currently reports findings like:
```json
{
  "summary": "Missing test for cancel_order function",
  "tests_to_add": ["Test that cancel_order handles already-cancelled orders"],
  "test_category_needed": ["integration"]
}
```

With the pyramid vocabulary, it would report:
```json
{
  "summary": "Missing test for cancel_order function",
  "tests_to_add": ["L2: Integration test that cancel_order rolls back partial DB writes on failure"],
  "test_category_needed": ["integration"],
  "test_level": "L2",
  "bug_finding_level": "BF4",
  "gap_reason": "cancel_order calls payment API and writes to DB — failure between these steps needs chaos/negative testing, currently only has L1 unit test with mocked DB"
}
```

### Current state of reviewer-test-adequacy-pass.md

The test-adequacy pass currently has these sections:
- Using Measured Coverage Data
- Phase 1 — Test Mapping
- Phase 2 — Branch Coverage Analysis
- Phase 3 — Error Path Testing
- Phase 4 — Integration Boundary Testing
- Phase 5 — Stale Test Detection
- Phase 6 — Test Category Classification (with unit/integration/e2e classification heuristics)
- Calibration Examples
- False Positive Suppression
- Investigation Tips

Phase 6 already classifies tests into unit/integration/e2e. The pyramid vocabulary extends this with finer-grained L0-L5 levels and adds the BF1-BF8 bug-finding dimension.

The output schema currently uses `test_category_needed` as an enum array with values `"unit"`, `"integration"`, `"e2e"`.

### Prompt changes

Add to `reviewer-test-adequacy-pass.md`:

1. **Classification vocabulary section** (add after Phase 6, as a new Phase 7): Define L0-L5 and BF1/BF2/BF4/BF6/BF8 with examples using the tables above.

2. **Gap analysis instructions** (within Phase 7): For each function without adequate test coverage, determine:
   - What test level exists (if any)
   - What test level is needed (and why)
   - What bug-finding level would catch the specific risk

3. **Calibration examples**: Add 2-3 examples showing how to classify test gaps using the vocabulary. Example:

```json
{
  "pass": "testing",
  "severity": "medium",
  "confidence": 0.80,
  "file": "src/serializers/json_export.py",
  "line": 45,
  "summary": "JSON export format changed but no snapshot test guards against output drift",
  "evidence": "Lines 45-52: format_record() output structure changed (added nested 'metadata' key). Existing tests at tests/test_json_export.py only assert non-empty output (test_export_produces_output). No golden file or snapshot test captures the exact format. Downstream consumers (API clients, data pipelines) expect a stable schema.",
  "failure_mode": "Output format drift undetected until downstream consumer breaks in production.",
  "fix": "Add BF2 golden/snapshot test: serialize a reference record, compare output against a committed snapshot file.",
  "tests_to_add": ["L1: Snapshot test comparing format_record() output against golden file"],
  "test_category_needed": ["unit"],
  "test_level": "L1",
  "bug_finding_level": "BF2",
  "gap_reason": "Output format is consumed by external systems — snapshot test catches drift that assertion-based tests miss"
}
```

### Schema changes

Add optional fields to the finding schema for test-adequacy findings in `findings-schema.json`:

```json
{
  "test_level": "L0|L1|L2|L3|L4|L5",
  "bug_finding_level": "BF1|BF2|BF4|BF6|BF8",
  "gap_reason": "string"
}
```

These are optional — only populated by test-adequacy findings. Other passes don't use them.

Specifically, add these three properties to the `findings.items.properties` object in `findings-schema.json`:

```json
"test_level": {
  "type": "string",
  "enum": ["L0", "L1", "L2", "L3", "L4", "L5"],
  "description": "Test pyramid level for test-adequacy findings: L0=Contract/Spec, L1=Unit, L2=Integration, L3=Component, L4=Smoke, L5=E2E"
},
"bug_finding_level": {
  "type": "string",
  "enum": ["BF1", "BF2", "BF4", "BF6", "BF8"],
  "description": "Bug-finding level for test-adequacy findings: BF1=Property, BF2=Golden/Snapshot, BF4=Chaos/Negative, BF6=Regression, BF8=Backward compat"
},
"gap_reason": {
  "type": ["string", "null"],
  "description": "Why the identified test level/bug-finding level is needed for this specific code"
}
```

### Edge cases

- **No test files in the diff**: The explorer still runs (it greps for test files in the repo, not just the diff). Pyramid classification applies to found tests.
- **Language without conventional test patterns**: Explorer falls back to generic classification. L1-L3 levels apply across languages.
- **Existing tests hard to classify**: Explorer reports `test_level: "L1"` with a note if uncertain. The judge doesn't re-classify — it trusts the explorer's assessment.

### Files to modify

- `skills/codereview/prompts/reviewer-test-adequacy-pass.md` — Add pyramid vocabulary, gap analysis instructions, calibration examples
- `skills/codereview/findings-schema.json` — Add optional `test_level`, `bug_finding_level`, `gap_reason` fields
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 5: Per-File Certification for Explorers

**Goal:** Require explorers to explicitly state what they checked and why they found nothing, instead of returning an empty `[]`. This forces thorough investigation and creates an audit trail that the judge can verify.

Currently, an explorer that returns `[]` provides no signal — the judge can't distinguish "I checked everything thoroughly and it's clean" from "I skimmed the diff and nothing jumped out." The AgentOps deep audit protocol solves this with per-file category certification: each explorer must either report a finding OR explicitly certify its focus area as clean with a reason.

### Where it fits

Modification to `prompts/reviewer-global-contract.md` (the shared rules all explorers follow). No pipeline changes, no new scripts, no schema changes to the findings output.

### Current behavior

The global contract currently says (line 83):
```
Return `[]` if no issues found in your focus area.
```

### New behavior

Replace the empty-return instruction with a certification requirement:

```markdown
## Empty Result Certification

If you find NO issues in your focus area, you MUST NOT return a bare `[]`.
Instead, return a certification object explaining what you checked:

```json
{
  "certification": {
    "status": "clean",
    "files_checked": ["src/auth/login.py", "src/auth/session.py"],
    "checks_performed": [
      "Traced all 3 callers of login() — none assume a return value that changed",
      "Verified session.pop() uses default=None (safe for missing keys)",
      "Checked backward compatibility — function signature unchanged"
    ],
    "tools_used": ["Grep: callers of login()", "Read: src/auth/session.py:40-60"]
  },
  "findings": []
}
```

**Rules for certification:**
1. `files_checked` must list every file in CHANGED_FILES that is relevant to your focus area. If you skipped a file, explain why (e.g., "test file — not relevant to correctness").
2. `checks_performed` must list 3-5 concrete checks you did (not generic statements). Each check should reference a specific function, line, or pattern you investigated.
3. `tools_used` must list the actual Grep/Read/Glob calls you made. If you made zero tool calls and certified clean, that's a red flag — the judge will flag it.
4. If the diff contains no code relevant to your focus area (e.g., concurrency explorer on a CSS-only diff), certify with: `"status": "not_applicable", "reason": "No code in diff is relevant to concurrency analysis"`.
```

### Investigation scope discipline

Add to the global contract alongside the certification requirement:

```markdown
## Investigation Scope

Your investigation MUST stay within the scope of the diff and its direct dependencies.

- **In scope:** Changed files, callers of changed functions, callees of changed functions,
  types/interfaces used by changed code, test files for changed code.
- **Out of scope:** Code that is unrelated to the diff, even if it has bugs.

If you discover a bug in unrelated code while tracing a call path, do NOT report it
as a standalone finding. If the bug is relevant because the diff makes it reachable,
report it with `pre_existing: true` (Feature 8). If it's unrelated, ignore it.

This prevents investigation drift — especially in large codebases where every file
has something that could be improved. Your job is to review THIS diff, not audit
the entire repository.
```

Inspired by Claude Octopus's "auto-freeze" pattern, which locks investigation scope to the affected module during debugging. We don't mechanically freeze scope (explorers need Read/Grep across the codebase to trace callers), but we make the discipline explicit in the contract.

### How the judge uses certifications

The judge already validates each explorer's work (Step 1 in `reviewer-judge.md` — the Gatekeeper phase). Add a new sub-step.

**Step 0.5: Certification Review (before adversarial validation)**

For each explorer that returned `findings: []`:
1. Read the certification. If no certification present (bare `[]`), note: "Explorer <pass> returned empty without certification — investigation depth unknown."
2. Check `tools_used` — if the explorer made zero tool calls, flag in the report: "Explorer <pass> certified clean without investigation. Findings may be missed."
3. Check `files_checked` — if relevant changed files are missing from the list, the explorer may have missed them.
4. Do NOT re-run the explorer's analysis — just assess whether the certification is plausible.

This is a lightweight check (read the certification, sanity-check it). The judge does not re-do the explorer's work.

### Current state of reviewer-judge.md (relevant sections)

The judge prompt begins with: "You are the review judge..." and defines the 4-expert panel:
```
Gatekeeper -> Verifier -> Calibrator -> Synthesizer
```

The Gatekeeper (Expert 1) receives all raw explorer findings and applies 6 auto-discard rules. The certification review step should be inserted before the Gatekeeper as a new "Step 0.5".

### Judge prompt additions

Add to `reviewer-judge.md`, before the "Expert 1: Gatekeeper" section:

```markdown
## Expert 0.5: Certification Review

**Receives:** All explorer outputs, including those with `findings: []`.
**Produces:** Notes on investigation depth for each explorer.

Before adversarial validation begins, review each explorer's certification:

1. **For explorers with `findings: []`:**
   - If a `certification` object is present, read it:
     - Check `files_checked` — does it cover the changed files relevant to this pass?
     - Check `tools_used` — did the explorer actually investigate (make Read/Grep calls)?
     - Check `checks_performed` — are the checks concrete and specific?
   - If no certification (bare `[]`), note: "Explorer <pass> returned empty without certification — investigation depth unknown."
   - If certification exists but `tools_used` is empty, flag: "Explorer <pass> certified clean without tool-based investigation."

2. **For explorers with findings:** Skip certification review — findings are the evidence.

3. **Do NOT re-run any explorer's analysis.** This is a plausibility check, not re-investigation.

4. **Carry forward any notes** about missing certifications into the Synthesizer's verdict_reason if they affect confidence in the review's completeness.
```

### Output handling

The certification object is consumed by the judge and NOT included in the final findings output. It's an internal quality signal, not a user-facing artifact. The judge may mention certification gaps in its `verdict_reason` if they affect confidence.

### Interaction with existing pipeline

- **Step 4a (explorer launch)**: No change to how explorers are launched. The certification requirement is in the global contract prompt, not the orchestrator.
- **Step 4b (judge)**: Add Step 0.5 (certification review) before Step 1 (adversarial validation).
- **Step 4-L (chunked mode)**: Same — certification is per-explorer, works identically in chunked mode.
- **Findings schema**: No change — certification is internal to the explorer-judge exchange.

### Edge cases

- **Explorer returns bare `[]` (no certification)**: The judge proceeds but notes the gap. This is a degraded experience, not a failure — older prompt versions or third-party explorers may not certify.
- **Explorer certifies clean but the judge finds an issue in the same area**: The judge reports its finding normally. The certification gap is noted in the verdict reason for transparency.
- **Large-diff chunked mode**: Each chunk explorer certifies independently for its chunk. The cross-chunk synthesizer does NOT certify — it either finds cross-chunk issues or returns `[]` (it's investigating interactions, not doing per-file review).

### Files to modify

- `skills/codereview/prompts/reviewer-global-contract.md` — Replace empty-return instruction with certification requirement, add investigation scope section
- `skills/codereview/prompts/reviewer-judge.md` — Add Step 0.5 (certification review)
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: explorer certifies clean, explorer returns bare [], explorer certifies but judge finds issue

### Effort: Small

---

## Feature 6: Contract Completeness Gate for Spec Verification

**Goal:** Strengthen the spec-verification pass with a structured completeness gate that catches categories of spec gaps that the current free-form requirement tracing misses. When reviewing code against a spec, the explorer should not only trace individual requirements but also verify that the spec's behavioral contracts are mechanically verifiable.

The AgentOps council enforces a 4-item contract completeness gate before allowing a PASS verdict on spec validation. Our spec-verification explorer already does requirement tracing and test category mapping — this feature adds a structured completeness assessment on top.

### Where it fits

Addition to `prompts/reviewer-spec-verification-pass.md` — a new Phase 6 after the existing Phase 5 (Category Adequacy Assessment). Also a small addition to the judge prompt's Step 5 (Spec Compliance Check).

### Current state of reviewer-spec-verification-pass.md

The spec-verification pass currently has:
- Pre-Conditions (runs only when spec is provided)
- Phase 1 — Requirement Extraction (with --spec-scope filtering)
- Phase 2 — Implementation Tracing (with behavioral verification)
- Phase 3 — Test Coverage Mapping
- Phase 4 — Test Category Classification
- Phase 5 — Category Adequacy Assessment
- Calibration Examples
- False Positive Suppression
- Output Format (returns `{ "requirements": [...], "findings": [...] }`)

### The completeness gate

After tracing individual requirements (Phases 1-5), the spec-verification explorer performs a structured assessment of the spec's behavioral completeness. This catches a different class of issues than per-requirement tracing — it catches what the spec *forgot to specify*.

**Gate items (4 checks):**

| # | Check | What It Catches | Example |
|---|-------|----------------|---------|
| 1 | **State transitions** | Missing states, undefined transitions, contradictory flows | Spec says "order can be cancelled" but doesn't define what happens to a partially-shipped order |
| 2 | **Error/edge behavior** | Unspecified error responses, boundary conditions, concurrent access | Spec defines happy path for payment but not what happens when payment gateway times out |
| 3 | **Cross-requirement consistency** | Requirements that contradict each other, or leave gaps between them | REQ-003 says "admin can delete users" but REQ-007 says "user data must be retained for 90 days" |
| 4 | **Testability** | Requirements that cannot be mechanically verified | "The system should be fast" — no metric, no threshold, no test can verify this |

### Explorer prompt additions

Add to `reviewer-spec-verification-pass.md` after Phase 5:

```markdown
### Phase 6 — Contract Completeness Assessment

After tracing individual requirements, assess the spec's completeness as a behavioral contract.
This catches what the spec *forgot to specify*, not what the code forgot to implement.

For each gate item below, determine: PASS (adequately specified), GAP (missing or incomplete),
or N/A (not relevant to this spec).

**6a. State Transitions**
If the spec describes entities with lifecycles (orders, users, sessions, workflows, jobs):
1. List all states mentioned in the spec (e.g., pending, active, suspended, closed)
2. List all transitions mentioned (e.g., pending->active on approval)
3. Check for gaps:
   - Are there states with no outgoing transitions (terminal states)? Are they intentional?
   - Are there transitions that could produce contradictions (e.g., simultaneous cancel and complete)?
   - Is the initial state defined?
   - Is error recovery defined (what state does the entity enter on failure)?
If no lifecycle entities exist in the spec, mark N/A.

**6b. Error/Edge Behavior**
For each integration point mentioned in the spec (API calls, database operations, file I/O,
external services):
1. Does the spec define what happens when the operation fails?
2. Does the spec define timeout behavior?
3. Does the spec define retry/backoff strategy, or explicitly state "no retry"?
4. Does the spec define behavior for malformed input?
If the spec has no integration points, mark N/A.

**6c. Cross-Requirement Consistency**
Review all extracted requirements together:
1. Do any two requirements contradict each other?
2. Are there logical gaps between requirements (e.g., requirement A produces output X,
   requirement B consumes input Y, but X != Y)?
3. Do all requirements use consistent terminology (same term for same concept)?

**6d. Testability**
For each requirement classified as `must` or `should`:
1. Can it be tested with a deterministic assertion?
2. If not (e.g., "should be performant"), flag as a testability gap.
3. Suggest a testable reformulation if possible (e.g., "p95 latency < 200ms").

**Output:** Add a `completeness_gate` object to your output alongside `requirements` and `findings`:

```json
{
  "completeness_gate": {
    "state_transitions": {
      "status": "PASS|GAP|N/A",
      "detail": "Found 4 states, 6 transitions, no gaps" | "Missing: error recovery state for failed payments"
    },
    "error_edge_behavior": {
      "status": "PASS|GAP|N/A",
      "detail": "All 3 integration points have error handling specified" | "Payment gateway timeout behavior unspecified"
    },
    "cross_requirement_consistency": {
      "status": "PASS|GAP|N/A",
      "detail": "No contradictions found" | "REQ-003 and REQ-007 contradict on data deletion"
    },
    "testability": {
      "status": "PASS|GAP|N/A",
      "detail": "All must/should requirements are testable" | "REQ-012 ('system should be responsive') has no testable threshold"
    },
    "overall": "PASS|GAP",
    "gap_count": 0
  }
}
```

**Gate verdict rules:**
- All PASS/N/A -> `overall: "PASS"`
- Any GAP -> `overall: "GAP"`, and report each gap as a finding with `pass: "spec_verification"`,
  `severity: "medium"`, summary: "Spec completeness gap: <description>"
```

### Current state of reviewer-judge.md (Step 4b / Spec Compliance)

The judge's Synthesizer (Expert 4) has a section "4b. Spec Compliance Check" with sub-steps:
- 4b-i. Merge Spec Verification Data
- 4b-ii. Validate Implementation Claims
- 4b-iii. Validate Test Category Claims
- 4b-iv. Synthesize Final `spec_requirements`
- 4b-v. Derive `spec_gaps`

The gate evaluation should be inserted as step 4b-iii.5 (between 4b-iii and 4b-iv), renumbered as appropriate.

### Judge prompt additions

Add to `reviewer-judge.md` in the Synthesizer's Spec Compliance Check section, after step 4b-iii:

```markdown
#### 4b-iii.5. Evaluate Completeness Gate

If the spec-verification explorer returned a `completeness_gate` object:
1. Include the gate results in the `spec_requirements` output as a summary note.
2. If `overall: "GAP"`:
   - The gate gaps are already in the findings as `spec_verification` findings.
   - Validate them with your normal adversarial checks (existence, contradiction, severity).
   - A spec gap is NOT a code bug — do not conflate the two. Spec gaps are advisory
     findings suggesting the spec needs clarification, not that the code is wrong.
   - Spec gaps alone do NOT cause a FAIL verdict. They contribute to WARN if the gaps
     could lead to implementation ambiguity.
3. If no `completeness_gate` in the explorer output, skip this step.
```

### Report additions

The spec verification section already exists in the report. Gate results appear as findings in the "Spec Verification" section. Add a gate summary if the gate was evaluated:

```
### Spec Contract Completeness
| Check | Status | Detail |
|-------|--------|--------|
| State Transitions | PASS | 4 states, 6 transitions, no gaps |
| Error/Edge Behavior | GAP | Payment gateway timeout unspecified |
| Cross-Requirement | PASS | No contradictions |
| Testability | GAP | REQ-012 has no testable threshold |
```

### Interaction with existing pipeline

- **Step 4a (explorer launch)**: No change — the spec-verification explorer already runs when `--spec` is provided.
- **Step 4b (judge Step 5)**: Add 4b-iii.5 for gate evaluation.
- **Step 6 (report)**: Spec verification section already exists. Gate results appear as findings in the "Spec Verification" section of the report. Add gate summary table.
- **Findings schema**: No change — gate gaps are standard findings with `pass: "spec_verification"`.

### Edge cases

- **No spec provided**: Spec-verification explorer doesn't run -> no gate -> nothing changes.
- **Spec is too vague for gate assessment**: Explorer marks all gate items as N/A with detail explaining why. No gap findings.
- **Spec is a brief acceptance criteria list (not a full spec)**: The gate still runs but most items will be N/A or PASS. Acceptance criteria lists don't typically have state transitions or integration points. This is fine — the gate adds value proportional to spec complexity.
- **Large-diff chunked mode**: Spec verification runs as a global pass (already the case). The gate runs once, globally, not per-chunk.

### Files to modify

- `skills/codereview/prompts/reviewer-spec-verification-pass.md` — Add Phase 6 (contract completeness gate)
- `skills/codereview/prompts/reviewer-judge.md` — Add Step 4b-iii.5 (gate evaluation)
- `skills/codereview/references/report-template.md` — Add gate summary table to spec verification section
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: gate all PASS, gate with gaps, no spec, vague spec

### Effort: Small-Medium

---

## Feature 7: Output File Batching for Large Reviews

**Goal:** When reviews produce large volumes of findings (20+ findings from 8 explorers), prevent the judge's context window from being overwhelmed by writing explorer results to disk instead of passing them inline. This is the same pattern the AgentOps council uses — and it becomes critical as we add more context to explorer prompts (domain checklists, prescan signals, historical risk, test pyramid data).

### The problem

Currently, each explorer's findings are collected by the orchestrator and passed to the judge in a single prompt:
```
## Explorer Findings
<JSON arrays from all explorers>
```

For a typical review with 4 core + 2-3 extended explorers producing 5-15 findings each, this is 35-100 findings x ~200 tokens per finding = 7,000-20,000 tokens of findings JSON. Add the context packet (~10-20k), deterministic scan results (~2-5k), spec (~0-10k), and the judge prompt itself (~3k), and the judge's input can reach 40-60k tokens, leaving limited room for investigation and output.

For large-diff chunked reviews, the problem is worse: all chunk explorers' findings are sent to the final judge — potentially 100+ findings from 20+ explorers.

### Solution

Write explorer findings to temp files. The judge reads them with the Read tool during its analysis, controlling how much context it loads at once.

### Where it fits

Modification to **Step 4a** (explorer result collection) and **Step 4b** (judge prompt construction) in SKILL.md. Also applies to Step 4-L (chunked mode).

### Implementation

**Step 4a change — write explorer results to disk:**

After each explorer completes, the orchestrator writes its findings to a temp file:
```bash
# Explorer results written by orchestrator (not by the explorer itself)
/tmp/codereview-explorer-correctness.json
/tmp/codereview-explorer-security.json
/tmp/codereview-explorer-reliability.json
/tmp/codereview-explorer-test-adequacy.json
/tmp/codereview-explorer-error-handling.json   # if ran
/tmp/codereview-explorer-api-contract.json     # if ran
/tmp/codereview-explorer-concurrency.json      # if ran
/tmp/codereview-explorer-spec-verification.json # if ran
```

Each file contains the explorer's raw JSON array (or certification object if empty).

**Step 4b change — judge receives file paths, not inline findings:**

Replace the inline findings in the judge prompt with file paths and a summary:

```
## Explorer Findings

Explorer results are written to disk. Read each file to review findings.

| Explorer | File | Finding Count | Key Signals |
|----------|------|--------------|-------------|
| Correctness | /tmp/codereview-explorer-correctness.json | 4 | 1 high (nil map), 2 medium, 1 low |
| Security | /tmp/codereview-explorer-security.json | 2 | 1 high (SQL injection), 1 medium |
| Reliability | /tmp/codereview-explorer-reliability.json | 0 | Certified clean (3 files checked) |
| Test Adequacy | /tmp/codereview-explorer-test-adequacy.json | 3 | 2 missing tests, 1 stale test |
| Error Handling | /tmp/codereview-explorer-error-handling.json | 2 | 1 high (swallowed error) |
| Spec Verification | /tmp/codereview-explorer-spec-verification.json | 5 | 2 not_implemented, 1 partial |

Total: 16 findings across 6 explorers.

Read each file with the Read tool before performing adversarial validation.
Start with the highest-severity signals first.
```

The summary table gives the judge a triage map — it can prioritize reading the files with high-severity findings first, and skip reading files with 0 findings (certified clean).

**Activation threshold:**

This is an optimization, not always needed. Apply file batching when:

| Condition | Mode |
|-----------|------|
| Total explorer findings > 20 | File batching |
| Chunked review mode (any) | File batching (always — too many explorers for inline) |
| Total explorer findings <= 20 AND standard mode | Inline (current behavior — simpler) |

Configurable via `.codereview.yaml`:
```yaml
output_batching:
  threshold: 20          # findings count to trigger file batching
  always_in_chunked: true # always use file batching in chunked mode
```

### Current state of reviewer-judge.md (relevant section)

The judge prompt begins with: "You are the review judge..." The Gatekeeper receives "All raw explorer findings + deterministic scan results." Currently findings are inline in the prompt. The judge needs a new instruction at the top for file-based loading.

### Judge prompt additions

Add to `reviewer-judge.md`, at the top of the file (after the opening description, before the Expert Panel section):

```markdown
## Finding Input Mode

If explorer findings are provided as **file paths** (a table of explorer names and file paths
rather than inline JSON), use the **Read** tool to load each file before performing adversarial
validation. Load files in priority order — start with explorers that have high-severity signals.

For explorers with 0 findings (certified clean), you may skip reading the file unless you need
to review the certification (see Expert 0.5: Certification Review).
```

### Global contract additions

Add to `reviewer-global-contract.md` — update the Output Schema section to note that explorer output may be written to disk:

The current instruction `Return [] if no issues found in your focus area.` should be updated (this is also modified by F5 certification). Add a note that explorer output is collected by the orchestrator and may be written to temp files for the judge.

### Interaction with existing pipeline

- **Step 4a**: Orchestrator writes explorer JSON to `/tmp/codereview-explorer-<pass>.json` when batching is active. Otherwise, passes inline as today.
- **Step 4b**: Judge prompt includes file path table instead of inline JSON when batching is active.
- **Step 4-L (chunked mode)**: Always use file batching. Write per-chunk explorer results to `/tmp/codereview-chunk-<N>-<pass>.json`. The final judge receives a manifest of all chunk result files.
- **Judge prompt (`reviewer-judge.md`)**: Add instructions at the top for reading findings from files when paths are provided.
- **Cleanup**: Temp files are ephemeral — they're in `/tmp/` and cleaned up by the OS. No explicit cleanup needed.

### Edge cases

- **Judge can't read files (permission issue)**: Fall back to inline passing. The orchestrator detects this if the first Read fails and re-sends findings inline.
- **Explorer produces very large output (>50k tokens)**: The file batching handles this naturally — the judge reads the file and processes it, unlike inline where it would consume prompt space.
- **Empty explorer results**: Write the certification object to the file. The judge reads it and processes Step 0.5 (certification review from Feature 5).

### Files to modify

- `skills/codereview/SKILL.md` — Update Steps 4a and 4b with file batching logic and activation threshold. Update Step 4-L to always use file batching.
- `skills/codereview/prompts/reviewer-judge.md` — Add instruction for reading findings from files when paths are provided
- `skills/codereview/prompts/reviewer-global-contract.md` — Note about output collection
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: threshold exceeded, chunked mode, inline mode, Read failure fallback

### Effort: Small

---

## Feature 8: Pre-Existing Bug Classification

**Goal:** Distinguish between bugs introduced by the current diff and pre-existing bugs that become newly reachable through the diff's code changes. This reduces noise (reviewers don't want to fix old bugs in a feature PR) while still surfacing important issues (a dormant bug that the PR activates is critical context).

Inspired by Claude Octopus's `pre_existing_newly_reachable` finding field, which tracks bugs that existed before the PR but become reachable via new code paths.

### Where it fits

Schema change (`findings-schema.json`), enrichment script change (`enrich-findings.py` from Feature 0b), explorer prompt change (global contract + calibration examples). No pipeline changes, no new scripts.

### Schema additions

Add two optional fields to the finding schema in `findings-schema.json`, under `findings.items.properties`:

```json
{
  "pre_existing": false,
  "pre_existing_newly_reachable": false
}
```

Specifically, add these properties:

```json
"pre_existing": {
  "type": "boolean",
  "description": "True when the bug exists in code NOT changed in this diff — the explorer traced a call path from changed code into unchanged buggy code. Default false (omitted = bug introduced by the diff)."
},
"pre_existing_newly_reachable": {
  "type": "boolean",
  "description": "True when the bug existed before but the diff creates a new code path that reaches it. Default false."
}
```

| Field | Type | When set |
|-------|------|----------|
| `pre_existing` | boolean | `true` when the bug exists in code that was NOT changed in this diff — the explorer traced a call path from changed code into unchanged buggy code |
| `pre_existing_newly_reachable` | boolean | `true` when the bug existed before but the diff creates a new code path that reaches it (e.g., a new caller of a function with an existing nil-map bug) |

Both default to `false` (omitted = bug introduced by the diff, which is the common case).

### Explorer prompt changes

Add to `prompts/reviewer-global-contract.md` in the Output Schema section, as a new subsection:

```markdown
## Pre-Existing vs Introduced Bugs

When investigating a potential issue, determine whether the bug is:

1. **Introduced by this diff** (default) — the diff creates the bug. `pre_existing` is false or omitted.
2. **Pre-existing but relevant** — the bug is in unchanged code, but the diff makes it more likely to trigger (new caller, new code path, changed preconditions). Set `pre_existing: true` and `pre_existing_newly_reachable: true`.
3. **Pre-existing and unrelated** — the bug is in unchanged code and no new code path reaches it. **Do not report.** This is noise.

The key question: *Does this diff change the likelihood of this bug being triggered?* If yes, report it with the pre-existing flags. If no, suppress it.
```

### Calibration example for correctness pass

Add this calibration example to `prompts/reviewer-correctness-pass.md`, in the Calibration Examples section:

```json
{
  "pass": "correctness",
  "severity": "high",
  "confidence": 0.85,
  "pre_existing": true,
  "pre_existing_newly_reachable": true,
  "file": "src/utils/cache.py",
  "line": 78,
  "summary": "Existing race condition in cache invalidation now reachable from new batch endpoint",
  "evidence": "cache.py:78 has unsynchronized read-modify-write on the cache dict (pre-existing, unchanged in this diff). The new batch_process() endpoint at api/batch.py:34 (added in this diff) calls invalidate_cache() from multiple goroutines. Before this diff, invalidate_cache() was only called from the single-threaded CLI path."
}
```

### Enrichment script changes (Feature 0b dependency)

`enrich-findings.py` applies these rules to pre-existing findings:

1. Pre-existing findings that are NOT newly reachable -> drop (should not have been reported, but safety net)
2. Pre-existing + newly reachable + severity high/critical -> keep tier as-is (the activation is important)
3. Pre-existing + newly reachable + severity medium/low -> downgrade action_tier by one level (e.g., should_fix -> consider)

### Interaction with existing pipeline

- **Explorer prompts**: Global contract gets classification guidance. Correctness pass gets one calibration example. Other passes may encounter pre-existing bugs less frequently — no changes needed.
- **Judge (Step 4b)**: No explicit change. The judge already validates findings — it will naturally assess whether pre-existing claims are accurate by checking git blame / diff boundaries.
- **Report**: Pre-existing findings are marked with a `(pre-existing)` label in the report. Grouped separately within their tier to reduce noise.
- **Schema**: Two optional boolean fields added. Backward compatible — omission means `false`.

### Edge cases

- **Explorer unsure if pre-existing**: If the explorer can't determine whether code was changed in the diff, it omits the flag (defaults to introduced). Better to over-report than miss an activation.
- **Pre-existing bug fixed by the diff**: Not a finding. The explorer should not report bugs that the diff fixes.
- **Entire file is new**: No pre-existing bugs possible — the explorer can skip the classification.

### Files to modify

- `skills/codereview/findings-schema.json` — Add `pre_existing` and `pre_existing_newly_reachable` optional boolean fields
- `skills/codereview/prompts/reviewer-global-contract.md` — Add pre-existing vs introduced classification guidance
- `skills/codereview/prompts/reviewer-correctness-pass.md` — Add calibration example
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 9: Provenance-Aware Review Rigor

**Goal:** Adjust review rigor based on how the code was produced. AI-generated code has distinct failure modes (over-abstraction, placeholder logic, unused helpers, mock data in production paths) that human-authored code rarely exhibits. Reviewers who know the provenance can look for the right class of problems.

Inspired by Claude Octopus's provenance-aware review, which elevates scrutiny for AI-assisted and autonomous code with specific risk pattern checklists.

### Where it fits

New CLI flag (`--provenance`), SKILL.md arg parsing, global contract addition, enrichment script change (Feature 0b). No new scripts, no pipeline changes.

### The `--provenance` flag

```bash
/codereview --provenance ai-assisted --base main
/codereview --provenance autonomous --base main
```

| Value | Meaning | Review adjustment |
|-------|---------|-------------------|
| `human` | Written by a person | Standard review (default) |
| `ai-assisted` | Human-directed, AI-generated code (Copilot, Claude suggestions) | Elevated: check for over-abstraction, weak tests, unnecessary flexibility |
| `autonomous` | Fully autonomous agent output (Codex tasks, crank runs, factory mode) | Highest: verify wiring, check for placeholder logic, validate operational safety |
| `unknown` | Provenance not specified | Standard review, no assumptions |

Default when `--provenance` is not provided: `unknown` (equivalent to `human` in practice — no elevated checks).

### SKILL.md changes

Add to Step 1 (argument parsing):

```
**If `--provenance <value>` provided:** Store the provenance value for inclusion in the context packet.
Valid values: human, ai-assisted, autonomous, unknown. Default: unknown.
```

Add to Step 2h (context packet assembly):

```
## Code Provenance: <value>

<provenance-specific instructions from global contract>
```

### Global contract additions

Add a new section to `prompts/reviewer-global-contract.md`:

```markdown
## Provenance-Aware Investigation

If the context packet includes a Code Provenance section, adjust your investigation:

### AI-Assisted Code (elevated rigor)

In addition to your normal focus area checks, look for these AI-codegen risk patterns:

- **Over-abstraction**: Interfaces, factories, or generic wrappers around single implementations. Ask: is there a second implementation? If not, the abstraction is premature.
- **Option-heavy APIs**: Functions with many optional parameters or configuration objects that no caller uses. Check actual call sites.
- **Weak tests**: Tests that assert the code runs without error but don't verify behavior. Tests that mirror implementation rather than testing outcomes.
- **Unnecessary flexibility**: Feature flags, plugin systems, or extension points with no concrete second use case.

### Autonomous Code (highest rigor)

All AI-assisted checks, plus:

- **Placeholder logic**: TODO-driven control flow, stub implementations that return hardcoded values, functions that log but don't act.
- **Unwired components**: Classes/functions defined but never imported or called from the main code path. Check the import graph.
- **Mock/test data in production paths**: Hardcoded test values, example.com URLs, "test" credentials outside of test files.
- **Silent failure handling**: Broad catch/except blocks that swallow errors, missing error propagation, functions that return default values on error without logging.
- **Missing rollback**: Database migrations without down migrations, state changes without recovery paths.
- **Speculative abstractions**: Code that solves problems the spec doesn't mention. Check against spec (if provided) or infer from call sites.

### Human-Authored / Unknown

Standard review. No additional risk patterns — your normal focus area investigation is sufficient.
```

### Enrichment script changes (Feature 0b dependency)

`enrich-findings.py` accepts an optional `--provenance` flag:

```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/codereview-judge.json \
  --scan-findings /tmp/codereview-scans.json \
  --provenance autonomous \
  > /tmp/codereview-enriched.json
```

When provenance is `ai-assisted` or `autonomous`:
- Findings matching AI-codegen risk patterns (placeholder logic, unwired components, mock data) get a severity boost of one level (medium -> high) if they would otherwise be classified as `consider` tier
- The `provenance` value is included in the enriched output envelope for downstream consumers

### Schema additions

Add one optional envelope field to `findings-schema.json` at the top level:

```json
"provenance": {
  "type": "string",
  "enum": ["human", "ai-assisted", "autonomous", "unknown"],
  "description": "How the reviewed code was produced. Affects review rigor — AI-generated code gets elevated scrutiny for codegen-specific risk patterns."
}
```

### Interaction with existing pipeline

- **SKILL.md Step 1**: Parse `--provenance`, store value
- **SKILL.md Step 2h**: Include provenance in context packet
- **Explorer prompts**: Global contract provides risk patterns. Individual explorer prompts do NOT change — the risk patterns are generic across all focus areas.
- **Judge**: No explicit change. The judge sees provenance in the context and may reference it in the verdict reason.
- **Report**: Add provenance to the report header: `**Provenance:** AI-assisted`
- **Validation script**: Add `provenance` to the optional envelope fields check (valid values: human, ai-assisted, autonomous, unknown).

### Edge cases

- **`--provenance` without value**: Error: "Missing value for --provenance. Valid values: human, ai-assisted, autonomous, unknown"
- **Mixed provenance in a single PR**: Not supported in v1.3. The flag applies globally to the entire review. A future version could support per-file provenance via annotations.
- **Large-diff chunked mode**: Provenance applies to all chunks equally — it's a global setting.

### Files to modify

- `skills/codereview/SKILL.md` — Add `--provenance` to Step 1 arg parsing, add to Step 2h context packet
- `skills/codereview/prompts/reviewer-global-contract.md` — Add provenance-aware investigation section
- `skills/codereview/findings-schema.json` — Add optional `provenance` envelope field, add `pre_existing` and `pre_existing_newly_reachable` to finding fields (if not already added by Feature 8)
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: provenance flag values, AI-codegen risk pattern detection

### Effort: Small-Medium

---

## Dependencies on Other Plans

- **F8** and **F9** depend on `enrich-findings.py` (F0b) from Plan: Context Enrichment. The enrichment script must exist before the pre-existing bug classification rules and provenance severity boosting can be implemented.
- **F4** adds fields (`test_level`, `bug_finding_level`, `gap_reason`) to `findings-schema.json`. **F8** also adds fields (`pre_existing`, `pre_existing_newly_reachable`) to the same file. These are independent fields on the finding object and can be added in any order without conflict.
- **F9** adds a top-level `provenance` field to `findings-schema.json` (envelope level, not finding level). This is independent of F4 and F8's finding-level additions.

## Design Principles (from v1.3 plan)

These principles govern implementation of all features:

**Scripts Over Prompts** — Wherever a step is mechanical (deterministic rules, data transformation, tool invocation, arithmetic), implement it as a script. This eliminates agent divergence and makes the pipeline testable.

**Use scripts for:** Tool detection/invocation, data transformation, rule-based classification, hash computation, file I/O and artifact management.

**Use AI for:** Understanding code semantics, investigating call paths and data flow, assessing severity, cross-cutting synthesis, report narration.

**The boundary is judgment.** If a step requires reading code and reasoning about behavior, it's an AI task. If it's applying a formula or running a tool, it's a script.

**Checklists Over Instructions** — When giving AI explorers domain-specific context, provide concrete checklist items (questions to answer, patterns to look for) rather than open-ended instructions. Checklists constrain investigation scope and produce more consistent findings across runs.
