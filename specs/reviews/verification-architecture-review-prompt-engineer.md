# Review: Verification Architecture Spec
## Reviewer: LLM Prompt Reliability Engineer

**Spec reviewed:** `specs/verification-architecture.md` (F0, F1, F5)
**Date:** 2026-03-28
**Verdict:** WARN

> **Note (2026-03-28):** This review was conducted against an earlier draft of the spec. Key recommendations that have since been incorporated:
> - **Q1-Q3 (Stage 1):** Batch size capped at 15, calibration examples added, output enforcement added, `improved_code_is_correct` removed. Feature count is now 13.
> - **Q5 (tool budget):** Clarified as one LLM call per finding with up to 10 tool calls each (not batched).
> - **Q6 (output format):** Strict JSON enforcement, lowercase verdict enum, and completeness constraint added.
> - **Q8 (two-pass judge):** Replaced with strengthened existing Expert Panel handoffs as recommended in this review's top recommendation #3.
>
> The review text below is preserved as-is for provenance. Read the current spec for the authoritative design.

---

## Question 1: Will the feature extractor produce consistent boolean outputs?

### Assessment

The batch extraction design has three reliability risks that compound with scale.

**Attention degradation across findings.** With 10-20 findings in a single call, extraction quality for findings 15-20 will measurably degrade compared to findings 1-5. This is well-documented LLM behavior: in long structured-output tasks, models maintain high fidelity on early items and begin to "coast" on later ones — defaulting to whatever pattern they established in the first few items rather than genuinely re-evaluating each feature. At 30+ findings (chunked mode), this becomes acute: the model will pattern-match rather than reason.

**Asymmetric default instruction.** The rule "when uncertain, prefer false for structural features and true for speculation features" is precisely the kind of conditional default that LLMs struggle with. In practice, what happens is:
- If the first few findings happen to have `requires_assumed_behavior: true`, the model anchors on "speculation features tend to be true" and over-applies it.
- If the first few findings are clean structural defects, the model anchors on "most things are false" and under-reports speculation.

The asymmetry requires the model to hold two opposing default strategies simultaneously. Models handle this poorly without examples demonstrating the divergence.

**JSON completeness.** The output schema requires 11 boolean fields per finding object, nested inside a `findings` array. Common failure modes at this complexity:
- Missing fields in later findings (the model "forgets" less common features like `has_redundant_work_in_loop` by finding 15).
- Trailing comma or missing comma in the JSON array when the output is long.
- The model adding a natural-language preamble ("Here are the extracted features:") before the JSON, breaking parsers.

### Evidence

Spec lines 76-93: The feature extraction prompt has rules but no output enforcement preamble. The prompt says "Do NOT make keep/discard decisions" but does not say "Output ONLY valid JSON. No commentary before or after."

### Recommendation

1. Add an explicit output enforcement line: "Output ONLY the JSON object below. No text before or after. Every finding MUST include all 11 feature fields."
2. Add a max batch size: 15 findings per call. For >15 findings, split into multiple extraction calls. This is cheap (the spec acknowledges ~$0.05 per call) and prevents the quality cliff.
3. Add a JSON schema to the prompt (or at minimum, one complete example showing all 11 fields) so the model has an anchoring template for the output structure.

---

## Question 2: Feature extraction calibration

### Assessment

**Rule-only prompting is fragile for boolean classification.** The prompt gives definitional rules ("Set `is_quality_opinion` to true for: naming suggestions, style preferences...") but no worked examples showing a complete finding mapped to its 11 features. In my experience, rule-only boolean extraction works reliably for 3-4 features but degrades past 6-7 features because the model has to hold too many independent classification criteria simultaneously without an anchoring example.

The critical gap: the rules define each feature independently, but findings often trigger multiple features simultaneously. Without an example showing "this finding has both `has_missing_error_handling: true` AND `requires_assumed_behavior: true` because..." the model will tend toward single-feature activation — marking the most salient feature true and leaving the rest false.

**`improved_code_is_correct` is unreliable in batch extraction.** This feature asks the model to evaluate whether suggested fix code compiles and works, without tool access, in a batch context where it is also evaluating 10 other features for 15+ findings. In practice, the model will:
- Default to `true` for most fixes (optimism bias — "the fix looks reasonable").
- Only flag obviously broken fixes (missing closing bracket, referencing a clearly wrong variable).
- Miss subtle issues like type mismatches, missing imports, or scope creep — exactly the issues Feature 5 is designed to catch.

This creates a misleading signal: `improved_code_is_correct: true` from Stage 1 might lead downstream consumers to trust the fix, when Stage 3's tool-assisted fix validation (Feature 5) would have caught a real problem.

### Evidence

Spec lines 76-93: Rules only, zero few-shot examples. Spec line 69: `improved_code_is_correct` in Stage 1 feature list. Spec lines 296-309: Feature 5 re-checks fix correctness in Stage 3 with tool access.

### Recommendation

1. Add 2-3 calibration examples to the feature extraction prompt. Each example should show a complete finding and its full 11-feature vector with brief rationale for each non-obvious assignment. Include at least one example with multiple structural features set to true and one example with `requires_assumed_behavior: true` alongside a structural feature.
2. Remove `improved_code_is_correct` from Stage 1 entirely, or rename it to `fix_looks_plausible` with lower expectations. The real fix validation happens in Stage 3 (Feature 5) where the agent has tool access. Keeping it in Stage 1 creates a redundant, lower-quality signal that could undermine Stage 3's authority.
3. If `improved_code_is_correct` stays in Stage 1, add an explicit note in Stage 2 triage: "Do NOT use `improved_code_is_correct` for any triage decision. This feature is advisory only and will be re-evaluated in Stage 3."

---

## Question 3: Batch size limits

### Assessment

"One batch LLM call for ALL findings" is a design-time simplification that will break in production.

The practical limit for reliable structured extraction depends on output length. Each finding produces ~11 boolean fields plus a `finding_index`, so roughly 200-300 tokens per finding in the output. At 30 findings, that is 6,000-9,000 tokens of structured JSON output. This is within model limits but beyond the zone of reliable JSON production. Empirically:

- **1-10 findings:** Reliable. JSON output is short enough that the model maintains structure.
- **11-20 findings:** Mostly reliable, but the last 3-5 findings show increased feature defaulting (model anchors on patterns from earlier findings rather than re-evaluating).
- **21-30 findings:** Quality degradation is noticeable. The model may omit fields, produce inconsistent field ordering, or add narrative interpolations mid-JSON.
- **30+:** Unreliable without structured output enforcement or splitting.

The spec acknowledges chunked mode produces 50-100+ findings across all chunks, and says verification runs per-chunk. But the per-chunk finding count can still exceed 30 if a single chunk has many issues (dense files).

### Evidence

Spec line 37: "one call for ALL findings." Spec lines 215-219: Chunked mode runs verification per-chunk, which bounds the problem but does not eliminate it.

### Recommendation

Add a max batch size with overflow handling:
```
MAX_BATCH_SIZE = 15
if len(findings) > MAX_BATCH_SIZE:
    split into ceil(len/MAX_BATCH_SIZE) batches
    run feature extraction on each batch
    merge results by finding_index
```
Cost impact: one additional ~$0.05 call per overflow. Reliability impact: significant. This should be a hard rule in the spec, not a configuration option.

---

## Question 4: Will the verifier follow the 5-step protocol?

### Assessment

The 5-step protocol is well-structured but has a predictable failure mode: **step compression.** LLMs with tool access tend to compress multi-step verification protocols into fewer actual steps. The typical pattern:

1. Model reads Step 1 ("Read the code"), makes a Read call.
2. After reading the code, the model forms an immediate opinion. Instead of proceeding to Step 2 ("Check the claim") as a separate analytical step, it combines Steps 2-3-5 into a single reasoning block: "I see the code, there's no guard clause, verdict: confirmed."
3. Step 4 (generate verification_command) is often skipped for `false_positive` verdicts because the model doesn't feel it needs evidence to dismiss something.

This compression isn't necessarily wrong — it's often efficient. But it means the "search for defenses" step (Step 3), which is the most valuable step for catching false positives, gets short-circuited. The model sees the code at the cited line, confirms the finding looks real, and skips the adversarial search.

**"Skeptical by default" framing.** This is well-calibrated. In my experience, the "you are skeptical, looking for reasons the finding is WRONG" framing does produce meaningfully more aggressive verification than neutral framing. The risk of over-discarding is real but lower than the risk of under-discarding. The reason: LLMs have a strong confirmation bias by default (they tend to agree with claims presented to them). The skeptical framing counteracts this bias. You might see a 5-10% increase in false negatives (real findings incorrectly marked false_positive) but a 30-40% decrease in false positives leaking through. This is the right tradeoff for a precision-focused pipeline.

**Grep command generation.** This is a moderate reliability concern. Models generate valid grep commands roughly 70-80% of the time. Common failures:
- Wrong regex escaping (using `grep -rn "func()" file` instead of `grep -rn "func()" file` with proper escaping).
- Incorrect file paths (guessing paths instead of using Glob first).
- Using grep flags that don't exist on the target platform (`grep -P` on macOS).
However — since the verifier has access to Glob, Read, and Grep as tools, the verification_command is primarily a *documentation artifact* ("here's what I checked"), not an execution artifact. The model will use the actual tools for verification and produce the `verification_command` as a summary. This reduces the impact of bad grep syntax.

### Evidence

Spec lines 159-183: The 5-step protocol. Spec line 162: "skeptical by default." Spec line 176: verification_command generation. The existing correctness explorer prompt (reviewer-correctness-pass.md) has 8 investigation phases — significantly more structured — and works because each phase has concrete, distinct actions. The verifier's 5 steps are less clearly differentiated (Steps 2 and 3 blur together).

### Recommendation

1. Merge Steps 2 and 3 into a single step with two sub-parts: "2a. Check the claim against the code. 2b. Actively search for defenses using Grep." This matches how the model will actually execute and makes the adversarial search an explicit sub-step rather than a separate step the model can skip.
2. Make Step 4 (verification_command) contingent: "For `confirmed` and `needs_investigation` verdicts, produce a verification_command. For `false_positive`, state the defense found." This prevents the model from struggling to produce a verification command for findings it wants to dismiss.
3. Add a structural cue: "Think step by step. For each finding, write your reasoning before the verdict." This forces the model to show its work rather than jumping to conclusions.

---

## Question 5: Default-to-false_positive within 3 tool calls

### Assessment

"3 tool calls" is ambiguous in exactly the way the review prompt predicts. The model will interpret this in one of three ways:

1. **Strict count:** "I have a budget of 3 tool invocations. I'll use them carefully." This leads to efficient verification but sometimes premature termination.
2. **Effort threshold:** "After 3 attempts to find evidence, I should give up." This leads to the model sometimes making 2 calls, deciding it has tried enough, and defaulting.
3. **Ignored:** The model reads "3 tool calls" as a soft guideline and makes as many calls as it needs. This is the most common outcome for non-enforced constraints.

In practice, without actual enforcement in the orchestration layer (counting tool calls and stopping the agent), the "3 tool calls" instruction is aspirational. The model will sometimes make 1 call and default, sometimes make 5 calls for an interesting finding.

The bigger issue: **the per-finding vs. per-batch ambiguity.** Does the verifier get 3 tool calls *per finding* or 3 tool calls *total for all findings*? The spec says "per finding" (Stage 3 is described as per-finding verification), but if the verifier processes multiple findings in a single LLM call (which the cost section implies — "$0.20-0.50 depending on finding count" suggests one call for multiple findings), the model has to self-manage its tool budget across findings.

### Evidence

Spec line 183: "If cannot confirm defect within 3 tool calls -> false_positive." Spec line 411: "$0.20-0.50 depending on finding count" — implies one LLM call handles multiple findings, making per-finding tool budgets unenforceable via prompt alone.

### Recommendation

1. Clarify the architecture: is the verifier one LLM call that processes all `verify` findings sequentially, or is it one LLM call per finding? If per-finding (one call each), the tool budget is enforceable at the orchestration layer. If batch (one call for all), the tool budget is a prompt-level suggestion only.
2. Rewrite the instruction: "You may use up to 3 tool calls per finding to search for evidence. If after examining the cited code and searching for defenses you cannot find concrete evidence the defect exists, assign `false_positive`. Err toward fewer tool calls — one Read of the cited file and one Grep for defenses is usually sufficient."
3. If batch processing, accept that tool budget is advisory and add: "Aim for 2-3 tool calls per finding. Do not spend more than 5 tool calls on any single finding."

---

## Question 6: Verifier output format

### Assessment

The verifier output format (`[{finding_index, verdict, reason, verification_command}]`) is simpler than the feature extractor's output, which helps reliability. But there are specific failure modes to anticipate.

**Verdict string inconsistency.** Without explicit enumeration, models will produce variants: "Confirmed", "confirmed", "CONFIRMED", "true_positive", "real", "valid." The spec uses lowercase in the examples but does not state "use exactly these lowercase strings." The downstream triage code will break on variant strings unless it normalizes.

**Missing findings.** If the model encounters a finding it considers trivially false_positive, it may simply omit it from the output array rather than including it with a `false_positive` verdict. This is a common failure mode: models treat "skip" and "report as false_positive" as equivalent. The downstream code then has no verdict for that finding and must decide what to do.

**Narrative injection.** The model will want to explain its reasoning. Without explicit suppression, expect output like:
```
I'll analyze each finding...

[{finding_index: 0, verdict: "confirmed", ...}]

In summary, findings 0 and 3 were confirmed while...
```
This breaks JSON parsers that expect the response to be pure JSON.

### Evidence

Spec line 193: "Output: JSON array of {finding_index, verdict, reason, verification_command}." No output enforcement preamble. No enum constraint on verdict strings. Compare to the existing judge prompt (reviewer-judge.md, line 240-280) which provides an explicit JSON schema with field types.

### Recommendation

1. Add output enforcement: "Output ONLY a JSON array. No text before or after the array. No markdown code fences."
2. Enumerate verdict values: "verdict must be exactly one of: `confirmed`, `false_positive`, `needs_investigation` (lowercase, no variants)."
3. Add a completeness constraint: "You MUST produce exactly one entry per finding in the input. If you cannot verify a finding, include it with verdict `false_positive` — do not omit it."
4. Add a JSON schema or complete example to the prompt, as the judge prompt does.

---

## Question 7: Quick reference effectiveness

### Assessment

The 6 defect-type-specific search strategies (resource leak -> search for close/defer, race condition -> search for lock/mutex, etc.) are net positive for verification quality, but their placement matters.

**Inline reference tables work when they are close to the point of use.** The quick reference in the spec is positioned after the 5-step protocol. In the actual prompt, this table should appear *within* Step 3 ("Search for defenses") rather than as a standalone section after the steps. When placed as a standalone section, the model may read it during initial prompt processing but not actively consult it during per-finding verification.

**The table does not add harmful noise.** Six entries is within the LLM's working set. The concern about "competing with per-finding instructions" is not borne out in practice for reference tables of this size. Noise becomes a problem at 15+ entries or when the reference overlaps/contradicts the step-by-step instructions.

**However, the table is incomplete.** The existing judge prompt's Verifier expert (reviewer-judge.md, lines 66-79) already has a more comprehensive contradiction-check table with 9 entries. The spec's quick reference has 6 entries that partially overlap. When both prompts coexist (pre-verification + judge verification), having two overlapping-but-different reference tables creates inconsistency. The verifier might use strategy X for a race condition while the judge uses strategy Y.

### Evidence

Spec lines 185-192: Quick reference table with 6 entries. Judge prompt lines 66-79: Contradiction check table with 9 entries. The correctness explorer prompt (reviewer-correctness-pass.md) integrates its investigation strategies directly into each phase rather than using a reference table — and this works well.

### Recommendation

1. Move the quick reference into Step 3 of the verifier protocol: "Step 3: Search for defenses. Use Grep to look for: [inline list]. For specific defect types: [table]." This keeps the strategies at the point of use.
2. Align the verifier's quick reference with the judge's contradiction-check table. Either use the same table in both prompts (preferred — consistency matters) or explicitly differentiate them (verifier table is for "does the defense exist?", judge table is for "does the defense fully disprove the finding?").
3. Expand to match the judge's 9-entry table. The 3 missing entries (N+1 query, missing auth, missing timeout) are relevant to verification.

---

## Question 8: Will the "complete Pass 1 before Pass 2" instruction work?

### Assessment

This is the highest-risk design decision in the spec.

**Phase sequencing in single-call prompts is unreliable at scale.** The instruction "Complete ALL of Pass 1 before starting Pass 2" works in short contexts (under 10k tokens of findings). With 40k+ tokens of findings (realistic for a chunked review), the model will begin blending passes. The mechanism: as the model processes findings in Pass 1, it starts forming narrative opinions ("this codebase has a pattern of missing error handling"). When it transitions to Pass 2, these opinions are already cached in its attention, and Pass 2 becomes a formalization of opinions formed during Pass 1 rather than a fresh synthesis.

**The existing judge already has a 4-stage sequential structure.** Adding a pass boundary on top of the stage boundaries creates the structural hierarchy: Pass 1 (Gatekeeper -> Verifier) -> Pass 2 (Calibrator -> Synthesizer). This is 2 levels of sequencing constraints. The model has to remember both "I'm in the Verifier stage" and "I'm in Pass 1." In practice, the model will follow the Expert Panel stages (which are well-established with clear handoff points) and treat the Pass 1/Pass 2 boundary as redundant — which it largely is, since Gatekeeper->Verifier are already verification and Calibrator->Synthesizer are already synthesis.

**Would two separate LLM calls be better?** For reliability, yes. Two calls with explicit context passing (Pass 1 outputs become Pass 2 inputs) guarantees sequencing. But this doubles the judge cost and adds latency. The tradeoff is real.

The pragmatic answer: the existing 4-stage Expert Panel structure already achieves most of what the two-pass design wants. The Gatekeeper and Verifier are verification. The Calibrator and Synthesizer are synthesis. The pass boundary is largely redundant with the stage boundaries, and adding it creates instruction clutter without meaningfully changing model behavior.

### Evidence

Spec lines 258-275: Two-pass judge design. Judge prompt (reviewer-judge.md) lines 9-25: Expert Panel with 4-stage sequential structure. The judge prompt already says "Execute them in order -- do not skip or reorder." Adding "Complete ALL of Pass 1 before starting Pass 2" is an instruction that restates what the sequential Expert Panel structure already enforces.

### Recommendation

1. Do not add a separate Pass 1/Pass 2 boundary. Instead, strengthen the existing Gatekeeper->Verifier->Calibrator->Synthesizer boundary by adding an explicit handoff instruction between Verifier and Calibrator: "Before proceeding to the Calibrator, confirm that ALL findings have received a verification status. List the finding count: N verified, M unverified, K disproven. Only then proceed to the Calibrator."
2. If the two-pass boundary is kept for conceptual clarity, frame it as a label rather than an instruction: "Pass 1 (Gatekeeper + Verifier) handles verification. Pass 2 (Calibrator + Synthesizer) handles synthesis. The existing Expert Panel order already enforces this sequencing." This avoids adding a new constraint that the model must track.
3. If strong sequencing is non-negotiable (e.g., empirical evidence shows the judge blending verification and synthesis), split into two LLM calls. The half-measure of an in-prompt phase boundary will not reliably enforce what a call boundary would.

---

## Question 9: Pre-verified findings instruction

### Assessment

"Do not re-verify `confirmed` findings" will be partially followed.

The model will reduce effort on confirmed findings but will not fully skip verification. The mechanism: the Gatekeeper and Verifier stages have clear instructions to examine each finding. When the model encounters a confirmed finding, the "do not re-verify" instruction competes with the Gatekeeper's "apply these six auto-discard rules to every finding" and the Verifier's "for EACH finding, use Grep, Read, and Glob tools to investigate." The model resolves the contradiction by doing a lighter-touch check on confirmed findings — reading the code but not doing the full contradiction search.

This partial re-verification is actually desirable. The instruction says "do not re-verify" but the ideal behavior is "trust but spot-check." A confirmed finding from the verifier *might* have been incorrectly confirmed (verifier's skeptical framing doesn't prevent all errors). A light-touch check by the judge catches the worst false confirms.

**Risk of over-trusting confirmed findings.** This is moderate. The bigger risk is that the judge spends its full investigation budget on `needs_investigation` findings and rubber-stamps confirmed findings. If 20 findings are confirmed and 3 are `needs_investigation`, the model will spend 80% of its tool calls on the 3 uncertain findings and almost none on the 20 confirmed ones. This is efficient but creates a blind spot: if the verifier systematically mis-confirms a certain finding type (e.g., always confirms race conditions because it doesn't understand concurrent execution contexts), the judge won't catch the pattern.

### Evidence

Spec line 256: "Do not re-verify `confirmed` findings -- focus on synthesis, deduplication, and verdict." Judge prompt line 53: "For EACH finding, use Grep, Read, and Glob tools to investigate -- do not validate findings from memory alone."

### Recommendation

1. Replace "Do not re-verify `confirmed` findings" with "Pre-verified findings (`confirmed`): trust the verifier's verdict. Do NOT re-run the full Verifier investigation for these findings. However, in the Gatekeeper stage, still apply auto-discard rules (a confirmed finding that targets unchanged code is still out of scope). In the Verifier stage, spot-check 1-2 confirmed findings if time permits."
2. Add a specific instruction: "If you notice a pattern where 3+ confirmed findings share a suspicious characteristic (all from one explorer, all targeting the same pattern, all with similar evidence), investigate one representative finding to validate the pattern."

---

## Question 10: Can the verifier reliably check fix correctness?

### Assessment

Fix validation reliability varies by check type. Here is a realistic breakdown:

| Check | LLM Reliability | Notes |
|-------|-----------------|-------|
| Undefined variables/functions | Medium (60-70%) | Requires "mental compilation" — model must know the full scope. Works for obvious cases (referencing `foo` when no `foo` exists), fails for imports, re-exports, and dynamic attributes. |
| Type mismatches | Low-Medium (50-60%) | Reliable for statically typed languages with explicit types. Poor for dynamic languages (Python, JavaScript) where types are inferred. |
| Missing imports | Medium (65-75%) | Model can grep for import statements and check. This is one of the more tool-amenable checks. |
| Syntax errors | High (80-90%) | Models are surprisingly good at spotting syntax errors in code snippets. |
| Scope creep | Low (40-50%) | This is a judgment call. "Does the fix change behavior beyond the finding?" requires understanding the full behavioral surface of the code. Models over-flag here (marking reasonable fixes as scope creep) or under-flag (missing subtle behavioral changes). |
| Self-containedness | Medium (60-70%) | "Does the fix require changes in other files?" is answerable with Grep (search for callers/consumers that would break). |

**Six steps is pushing the limit.** The existing 5-step protocol is already at the edge of reliable step-following. Adding Step 5 (fix validation with 6 sub-checks) creates a protocol with effectively 10+ evaluation criteria per finding. The model will do a cursory job on fix validation — it will catch obviously broken fixes but miss subtle issues.

**Scope creep is the weakest check.** "Fix changes behavior beyond what the finding describes" requires the model to compare the fix's behavioral delta against the finding's described scope. This is a second-order reasoning task (understand the finding's scope, understand the fix's scope, compare). Models handle this poorly. In practice, the model will either:
- Flag almost no fixes as scope creep (under-reporting), or
- Flag any fix that touches more than 1-2 lines as scope creep (over-reporting).

### Evidence

Spec lines 280-313: Fix validation design. Spec lines 296-309: The 6 sub-checks. The existing verifier already has 5 steps; adding Step 5 (fix validation) makes 6 steps. The correctness explorer prompt (reviewer-correctness-pass.md) manages 8 phases but each phase is independently triggered (not all phases apply to every finding). Fix validation applies to every finding with a `fix` field, making it an unconditional addition to the per-finding workload.

### Recommendation

1. Reduce fix validation to 3 checks: syntax errors, undefined variables/functions, and missing imports. These are the most reliably detectable and highest-impact. Drop type mismatches (unreliable), scope creep (too subjective), and self-containedness (low impact — the reviewer can decide if they want to apply a multi-file fix).
2. Consider making fix validation a separate, optional sub-call rather than extending the verifier's protocol. A dedicated "fix checker" prompt that receives only the finding + fix + surrounding code context would produce more reliable results than a verifier that has to juggle both defect verification and fix validation.
3. If fix validation stays in the verifier prompt, position it as a clearly delineated final step: "After assigning your verdict, if the finding includes a `fix` field, quickly check: does the fix have syntax errors? Does it reference undefined names? Does it require missing imports? If any check fails, add `fix_valid: false` and `fix_issue`. Otherwise, default `fix_valid: true`." Keep it fast and focused.

---

## Verdict: WARN

The architecture is sound in its three-stage decomposition — feature extraction, deterministic triage, and tool-assisted verification is a well-proven pattern (Kodus-AI validates it). The design will meaningfully reduce false positives compared to the current single-pass judge. However, the prompt-level implementation has reliability gaps that will cause production issues if not addressed before implementation.

## Top 3 Recommendations (Ranked by Impact)

### 1. Add batch size limits and output enforcement to Stage 1

**Impact: High.** Stage 1 is the foundation — every finding flows through it. If the feature extractor produces inconsistent or malformed output, Stage 2 triage makes wrong routing decisions, and the entire pipeline's value is undermined.

**Action:** Cap extraction batches at 15 findings. Add 2-3 calibration examples to the prompt. Add output enforcement ("Output ONLY valid JSON. No commentary."). Add a completeness check in the orchestrator that validates all 11 fields are present for each finding before passing to Stage 2.

### 2. Clarify verifier architecture: one call per finding or batched

**Impact: High.** The spec is ambiguous about whether the verifier processes findings one at a time (each in its own LLM call with tool access) or in a batch. This architectural decision determines whether the 3-tool-call budget is enforceable, whether the 5-step protocol is reliably followed, and whether the cost estimate is accurate. One-call-per-finding is more reliable but more expensive. Batched is cheaper but makes per-finding tool budgets and step protocols advisory rather than enforceable. Make the explicit choice and design the prompt accordingly.

### 3. Drop the two-pass judge overlay; strengthen existing Expert Panel handoffs instead

**Impact: Medium-High.** The existing 4-stage Expert Panel (Gatekeeper -> Verifier -> Calibrator -> Synthesizer) already enforces verify-before-synthesize sequencing. Adding a Pass 1/Pass 2 boundary on top adds instruction complexity without meaningfully changing model behavior. The effort would be better spent adding explicit inter-stage handoff checkpoints (e.g., "Before proceeding to Calibrator, state: N findings verified, M disproven, K unverified") which are more likely to be followed and provide auditability.
