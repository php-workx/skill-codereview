# Systems Architecture Review: Explorer & Judge Behavior Plan

**Reviewer perspective:** Multi-agent pipeline architecture -- composition, data flow, failure modes, ordering dependencies, evolvability.

**Artifacts reviewed:**
- `docs/plan-explorer-judge-behavior.md` (full plan, F4-F11)
- `skills/codereview/prompts/reviewer-judge.md` (current judge prompt, 290 lines)
- `skills/codereview/prompts/reviewer-global-contract.md` (shared explorer contract, 100 lines)
- `skills/codereview/findings-schema.json` (output schema, 380 lines)
- `scripts/orchestrate.py` -- `parse_explorer_output()`, `post_explorers()`, `finalize()`, `assemble_judge_prompt()`
- `docs/plan-context-enrichment.md` (F0b section, enrichment script spec)

---

## 1. Judge Complexity

**Assessment: FRAGILE**

### Current state

The judge prompt is 290 lines with 4 sequential expert stages. Each stage has clear input/output boundaries:

```
Gatekeeper (6 discard rules)
  -> Verifier (3 checks: existence, contradiction, annotation)
    -> Calibrator (4 sub-phases: severity, root cause, synthesis, contradiction)
      -> Synthesizer (5 sub-steps: strengths, spec compliance with 5 sub-sub-steps, verdict)
```

Counting decision points in the current judge: 6 (Gatekeeper rules) + 3 (Verifier checks) + 4 (Calibrator rules) + 3 (Synthesizer verdict branches) + 5 (spec compliance sub-steps) = **21 decision points**.

### After the plan

The plan adds:

- **Step 0.5: Certification Review (F5)** -- 4 checks per explorer with empty findings. For a typical 6-explorer run where 2 return empty, this adds 8 decision evaluations.
- **Finding Input Mode (F7)** -- Conditional branching at the top of the prompt: if findings are file paths, use Read tool; if inline, process directly. The judge must now determine its own input mode before any expert stage begins.
- **Pre-existing bug handling (F8)** -- No explicit judge change, but the judge inherits two new boolean fields (`pre_existing`, `pre_existing_newly_reachable`) that the Calibrator must reason about during severity calibration. The plan says "no explicit change" but the Calibrator's severity logic implicitly must handle these.
- **Completeness Gate in Synthesizer (F6)** -- Step 4b-iii.5 inserted between existing spec compliance sub-steps. The Synthesizer's spec compliance section grows from 5 sub-steps to 6.

New decision point count: 21 (existing) + 8 (certification per typical run) + 2 (input mode) + 2 (pre-existing severity branching) + 4 (completeness gate checks) = **~37 decision points**.

### The instruction drift risk

The judge prompt will grow from ~290 lines to an estimated ~380-420 lines. This is the same trajectory that motivated moving pipeline logic from SKILL.md into `orchestrate.py` -- the prompt grows until the LLM starts skipping steps or conflating instructions.

The specific risk: **Step 0.5 (Certification Review) runs before the Gatekeeper, but its outputs ("notes") are consumed by the Synthesizer, five expert stages later.** The LLM must carry forward unstructured notes across the entire expert panel. In a 400-line prompt with 37 decision points, these notes will be dropped or garbled during long reviews.

Additionally, the Finding Input Mode section (F7) adds a meta-instruction -- "determine how your input is structured before you start" -- that violates the otherwise clean sequential flow. The judge must now introspect on its own prompt structure, which is a qualitatively different kind of instruction from "evaluate this finding."

---

## 2. Shared File Conflicts

**Assessment: FRAGILE**

### Serialization realism

The plan correctly identifies that F5, F6, F7 must be serialized on `reviewer-judge.md`. The question is whether this serialization holds in practice.

**Problem 1: Insertion point drift.** F5 inserts Step 0.5 before Expert 1. F6 inserts Step 4b-iii.5 inside the Synthesizer. F7 inserts a new section at the top. These are three different locations in the file, so they do not directly conflict on content. But each insertion changes the line numbers that subsequent features reference. The plan specifies insertion points by section name ("before Expert 1", "after 4b-iii"), not line number, which is correct -- but the prose descriptions reference "the Gatekeeper phase" and "Step 5" which may shift meaning after insertions.

**Problem 2: Semantic interference.** F5 introduces the concept that the judge reads certification objects. F7 introduces the concept that the judge reads findings from files. Both change the judge's input contract. If F5 is implemented first, the judge expects `{ certification: {...}, findings: [...] }` inline. If F7 is then implemented, the judge expects file paths instead of inline JSON. The interaction between these two is underspecified: does the judge read a file that contains a certification object? The plan's F7 section says "Each file contains the explorer's raw JSON array (or certification object if empty)" -- this parenthetical is easy to miss and represents a format decision that spans two features.

**Problem 3: Global contract congestion.** `reviewer-global-contract.md` is touched by F5, F7, F8, F9, F10 -- five features across three waves. The contract is currently 100 lines. After all five features, it will be approximately 250-300 lines. This is the shared prompt that every explorer receives. Explorer prompt length directly impacts the quality of investigation -- longer contracts mean less context window for actual code analysis.

### Should the judge prompt be split?

Yes. The judge prompt has a natural seam: the 4 expert stages are already conceptually isolated (each receives defined input and produces defined output). Splitting them into separate files (`judge-gatekeeper.md`, `judge-verifier.md`, `judge-calibrator.md`, `judge-synthesizer.md`) with a thin `judge-main.md` that defines the sequencing would:

1. Allow F5 (Step 0.5) to be its own file (`judge-certification-review.md`) inserted by the orchestrator only when certifications are present.
2. Allow F6 to modify only the synthesizer file.
3. Allow F7 to modify only the main sequencing file.
4. Reduce cognitive load per file, making prompt engineering easier.

The cost: `assemble_judge_prompt()` in orchestrate.py would need to compose the judge prompt from parts. This is a small mechanical change (concatenate files in order), and it moves complexity into the right place -- the orchestrator, which is testable, rather than the prompt, which is not.

---

## 3. Data Contract Between Explorers and Judge

**Assessment: NEEDS REDESIGN**

### Is the explorer output schema formally defined?

**Partially.** The global contract (line 51-81) specifies the output schema as a JSON array of finding objects. The spec-verification pass already deviates -- it returns `{ "requirements": [...], "findings": [...] }` (see `reviewer-spec-verification-pass.md` line 574 in the plan). The `parse_explorer_output()` function in orchestrate.py (lines 1745-1774) handles both formats:

```python
if isinstance(raw, list):
    # Old format: bare array
    return findings, []
if isinstance(raw, dict):
    # New format: object with findings and requirements
    findings = raw.get("findings", [])
    requirements = raw.get("requirements", [])
```

This is the right normalization pattern, but it is **not aware of the certification field** that F5 introduces. The new format will be `{ "certification": {...}, "findings": [...] }`. The existing `parse_explorer_output()` will silently drop the certification -- it extracts `findings` and `requirements` but ignores unknown keys.

### Mixed format handling

The plan assumes all explorers will adopt the new format simultaneously. But in practice:

1. Wave 1 (F10, F11) does not change output format.
2. Wave 2 (F5) changes the format for explorers that return empty findings.
3. Explorers that find issues continue to return `[{finding}, ...]` (bare array).

So after F5, the judge receives a mix:
- Explorers with findings: bare `[...]` arrays (unchanged)
- Explorers with no findings: `{ "certification": {...}, "findings": [] }` objects

The `parse_explorer_output()` function handles this correctly today for `findings` and `requirements`. But the certification is lost in the normalization step. The certification data never reaches the judge.

### The missing normalization step

**The plan does not update `post_explorers()` or `parse_explorer_output()` to preserve certifications.** F5's "Files to modify" section lists `reviewer-global-contract.md` and `reviewer-judge.md` but does not list `scripts/orchestrate.py`. Yet the certifications must pass through the orchestrator to reach the judge.

There are two possible fixes:

1. **Normalization in orchestrate.py:** `parse_explorer_output()` extracts certifications alongside findings and requirements, and `assemble_judge_prompt()` includes them in the judge input.
2. **Bypass via file batching (F7):** If F7 is implemented, explorers write raw output to files, and the judge reads the files directly -- bypassing the orchestrator's normalization. This is actually cleaner but means F5 depends on F7, which is not documented.

Either way, the current plan has a data flow gap: certifications are defined in the explorer contract but never flow through the orchestrator to the judge.

### Recommendation

Add a formal `explorer-output-schema.json` that defines the explorer output contract (distinct from the final report schema in `findings-schema.json`). This schema should define the three valid shapes:

1. `[]` -- bare array (legacy, will be deprecated by F5)
2. `[{finding}, ...]` -- array of findings (standard)
3. `{ "certification": {...}, "findings": [...], "requirements": [...], "completeness_gate": {...} }` -- full object (post-F5/F6)

Then `parse_explorer_output()` validates against this schema and the data contract is testable.

---

## 4. F7 (Output File Batching) Activation Threshold

**Assessment: FRAGILE**

### Behavioral discontinuity

The threshold creates two distinct judge behaviors:

| Finding count | Judge behavior |
|---------------|---------------|
| 0-20          | Single-prompt agent: reads inline JSON, reasons, outputs |
| 21+           | Tool-using agent: reads file paths, makes Read calls, then reasons |

These are architecturally different agents. The single-prompt judge is deterministic in its input processing -- the entire context is in the prompt. The tool-using judge makes sequential Read calls, introducing ordering effects (which file it reads first affects its reasoning context).

### Off-by-one risk

The plan says ">20 findings -> file batching" and "<=20 AND standard mode -> inline." This is clear: 20 = inline, 21 = file batching. But the threshold is applied in the orchestrator, and the plan does not specify whether the count is pre-dedup or post-dedup. Currently, `post_explorers()` deduplicates and filters before assembling the judge prompt (lines 1878-1890). If the count is post-dedup, a review with 25 raw findings that deduplicates to 18 would use inline mode. If pre-dedup, it would use file batching. The plan should specify which count triggers the threshold.

### Tool-calling judge

This is the most significant architectural change in the plan and it is underspecified. Currently the judge prompt says "use Grep, Read, and Glob tools to investigate" in the Verifier stage (Expert 2). So the judge already makes tool calls. But F7 adds tool calls *before any expert stage begins* -- the judge must Read files to even see its input.

The risk: if the Read tool fails (file deleted, permission error, path wrong), the judge has no findings to evaluate. The plan mentions a fallback ("The orchestrator detects this if the first Read fails and re-sends findings inline") but this is impossible as described -- the orchestrator has already finished `post_explorers()` and the judge is running autonomously. There is no orchestrator process watching the judge's tool calls. The fallback would require the judge prompt itself to handle Read failures, which adds another decision branch to an already complex prompt.

### Testing

The plan does not describe how to test the threshold behavior. A test matrix should cover:
- 19 findings (inline)
- 20 findings (inline)
- 21 findings (file batching)
- 0 findings (inline, all certifications)
- Chunked mode with 5 findings (file batching due to mode, not count)
- File Read failure during judge execution

Without this test matrix, the threshold will be a source of intermittent bugs.

---

## 5. Ordering of the Waves

**Assessment: SOLID (with one caveat)**

### Wave 1 alone

F10 (Phantom Knowledge Self-Check) and F11 (Mental Execution Framing) are prompt-only additions to explorer prompts. They do not change the judge, the schema, or the orchestrator. If Wave 2 is never implemented, the system is in a consistent state. These are the cleanest features in the plan.

### Partial Wave 2

If F5 is done but F6/F7 are not:
- Explorers return certification objects for empty results.
- The judge has Step 0.5 (Certification Review) but no completeness gate (F6) or file batching (F7).
- **Problem:** As identified in section 3, certifications do not flow through `post_explorers()` to the judge. If this data flow gap is fixed as part of F5, the system is consistent. If not, the judge prompt references certifications that never arrive.

If F5 and F6 are done but F7 is not:
- Consistent. The judge processes inline findings with certifications and completeness gate data. No issues.

If F7 is done but F5 is not:
- The judge reads findings from files but has no certification review step. Files with `[]` arrays are read and produce no findings. This is consistent but loses the signal that F5 was meant to provide.

### Minimum viable subset

The features that deliver measurable improvement with the least coupling:

1. **F10 (Phantom Knowledge Self-Check)** -- Directly targets the #1 false positive source. Measurable via precision improvement in OWASP/Martian benchmarks. Zero risk, zero dependencies.
2. **F11 (Mental Execution Framing)** -- Targets correctness pass quality. Measurable via correctness finding accuracy. Zero risk, zero dependencies.
3. **F4 (Test Pyramid Vocabulary)** -- Enriches test-adequacy findings with actionable vocabulary. Low risk, schema additions are optional fields.

These three can ship independently without touching the judge or the orchestrator. Everything else (F5-F9) involves cross-cutting changes to the judge data flow and should be treated as a single coordinated release.

---

## 6. Cross-Plan Dependency Risk

**Assessment: FRAGILE**

### The F0b dependency chain

F8 (Pre-Existing Bug Classification) and F9 (Provenance-Aware Rigor) both depend on `enrich-findings.py` from the Context Enrichment plan's F0b. The dependency is:

```
Context Enrichment Plan       Explorer/Judge Plan
        F0b ----------------> F8
     (creates               (adds pre-existing
   enrich-findings.py)        classification rules)

        F0b ----------------> F9
     (creates               (adds provenance
   enrich-findings.py)        severity boosting)
```

Looking at the current codebase, `finalize()` (orchestrate.py line 2177) already calls `enrich-findings.py`. So the script exists. But reading the Context Enrichment plan's F0b section, the spec describes the script as something to be *created* ("Extract the mechanical parts into a Python script"). This suggests the current `enrich-findings.py` is a stub or early version, and F0b represents a significant rewrite.

### Interface definition

F0b defines `enrich-findings.py`'s interface:
- Input: `--judge-findings`, `--scan-findings`, `--confidence-floor`
- Output: JSON with `findings`, `tier_summary`, `dropped`

F8 adds: pre-existing classification rules (downgrade tier for pre-existing non-activated bugs).
F9 adds: `--provenance` flag and severity boosting for AI-codegen patterns.

These are additive changes to the script's logic, not interface changes. The interface is stable enough for parallel development -- F8 and F9 add new flags and internal rules, not new required inputs.

### Coordination mechanism

The plan says F8 and F9 are in Wave 3, "depends on enrich-findings.py from Context Enrichment plan." But there is no described coordination mechanism. Questions:

- Who owns `enrich-findings.py`? The Context Enrichment plan creates it; the Explorer/Judge plan modifies it. If both plans are in flight, changes collide.
- What if F0b's interface changes during implementation? F8 and F9 assume the interface described in the Context Enrichment plan. If F0b adds required fields or changes the output shape, F8/F9 break.
- What is the signal that F0b is "done enough" for F8/F9 to start? The plan doesn't define a gate.

The mitigation is straightforward: F0b should define its interface as a contract (input schema, output schema, flag inventory) before implementation begins. F8/F9 code against the contract, not the implementation. But this contract does not currently exist in either plan.

---

## Architectural Recommendation

Before implementation begins, make these structural changes to the plan:

### 1. Split the judge prompt into composable files

Refactor `reviewer-judge.md` into:
- `judge-main.md` -- sequencing and output format
- `judge-gatekeeper.md` -- Expert 1
- `judge-verifier.md` -- Expert 2
- `judge-calibrator.md` -- Expert 3
- `judge-synthesizer.md` -- Expert 4

Then `assemble_judge_prompt()` composes them. This eliminates the shared-file serialization constraint for F5/F6/F7 and makes each expert stage independently testable. Cost: one small orchestrate.py change. This should be done as a prerequisite before Wave 2.

### 2. Fix the certification data flow gap

Either:
- (a) Update `parse_explorer_output()` to extract and preserve certifications, and update `assemble_judge_prompt()` to include them. Add `scripts/orchestrate.py` to F5's file list.
- (b) Make F5 depend on F7 (file batching), so the judge reads raw explorer output directly. Document this dependency explicitly.

Option (a) is safer because it does not create new feature dependencies.

### 3. Define the explorer output schema formally

Create `explorer-output-schema.json` as a shared contract between explorers, `parse_explorer_output()`, and the judge. Validate explorer output against this schema in `post_explorers()`. This catches format mismatches early instead of silently dropping fields.

### 4. Eliminate the F7 threshold discontinuity

Always use file batching. The "optimization" of inline mode for small reviews creates a behavioral bifurcation that doubles the test surface. File batching works for all sizes. The judge already uses Read/Grep tools in the Verifier stage, so adding Read calls at the start is not a qualitative change. If inline mode is retained for simplicity, make it explicit that the judge prompt must be tested in both modes.

### 5. Define the F0b interface contract

Before Wave 3 begins, extract the `enrich-findings.py` interface into a documented contract (input schema, output schema, supported flags). Both plans reference this contract rather than each other's implementation details.

### 6. Treat F5+F6+F7 as a single coordinated release

The plan already warns "serialize these three" but the features are listed separately with separate effort estimates. In practice, they modify the same data flow (explorer output -> orchestrator -> judge input) and should be designed, implemented, and tested as a unit. A partially-implemented subset (F5 without orchestrator changes) produces a broken data flow.
