# Review: Verification Architecture (F0 + F1 + F5)

**Reviewer persona:** Verification & Testing Infrastructure Engineer
**Spec reviewed:** `specs/verification-architecture.md`
**Date:** 2026-03-28

---

## Feature 0: 3-Stage Verification

### Question 1: Boolean feature model completeness

**Assessment:**

Eleven features is a reasonable starting set but will misclassify several common finding categories. The feature model has a structural bias: it was designed around the Kodus-AI SafeguardFeatureSet, which targets enterprise backend code (resource leaks, data exposure, unsafe data flow). It has blind spots for the following:

**Missing: `targets_test_code`.** Test files generate a disproportionate volume of low-value findings. "Missing error handling in test helper," "hardcoded credentials in test fixture," "resource leak in mock setup" -- these are all technically true findings that no developer wants to read. Without a `targets_test_code` feature, these findings survive triage and consume a verification agent call each. In SonarQube workflows, the first thing most teams configure is a test-code exclusion scope. The feature extractor prompt could detect this trivially (file path contains `test`, `spec`, `__tests__`, `fixtures`; code uses `assert`, `mock`, `@pytest.fixture`, `describe/it`).

**Missing: `duplicates_linter_result`.** The Gatekeeper already has auto-discard rule 6 ("Duplicate of deterministic"), but that fires inside the judge, *after* the finding has survived feature extraction, triage, and verification. If the feature extractor flags `duplicates_linter_result: true`, triage can discard it at Stage 2 for zero cost. This is especially valuable in chunked mode where 50-100+ findings amplify the waste.

**The `improved_code_is_correct` feature is misplaced.** It describes the fix, not the finding. The triage rules (Stage 2) never reference it -- they only check structural defect features and speculation features. It exists solely as an input to Feature 5 (fix validation), but Feature 5 already has the verifier doing a dedicated fix validation check. Having this feature in the extraction schema creates confusion: the feature extractor must now evaluate whether a code suggestion compiles, which is a substantially different cognitive task than classifying finding nature. It should be removed from Stage 1 and left entirely to Stage 3's fix validation step.

**Evidence:** Spec lines 56-70 (feature table), lines 110-139 (triage rules that never reference `improved_code_is_correct`), lines 280-308 (Feature 5 fix validation that duplicates the check).

**Recommendation:**
1. Add `targets_test_code` and `duplicates_linter_result` as features (13 total).
2. Remove `improved_code_is_correct` from Stage 1. Let Feature 5 own fix validation entirely.
3. Consider adding `has_concurrency_issue` as a structural defect feature. The "quick reference by defect type" already lists race conditions as a verification target, but no boolean feature captures it. Right now a race condition finding gets `has_wrong_algorithm: false` (it is not an algorithm bug), hits no structural feature, and falls through to Rule 5 ("no signals") where it survives only by accident.

---

### Question 2: Triage rule aggressiveness

**Assessment:**

**Rule 3 (speculation without structural defect -> discard) is the most dangerous rule in the pipeline.** In my experience with static analysis triage, this rule will incorrectly discard roughly 15-25% of real findings in a typical review. The problem is that many real bugs are *inherently* speculative at the point of detection:

- "This function doesn't handle HTTP 429 rate limiting" -- speculative about external behavior, no structural defect feature, but a real production failure mode.
- "This database query will deadlock under concurrent access" -- requires assumed behavior about callers, no `has_wrong_algorithm` or `has_resource_leak`.
- "This parsing function will panic on malformed UTF-8 input" -- speculative about input shape, but a genuine crash path.

All three examples would have `requires_assumed_behavior: true` and no structural defect feature set to true, so Rule 3 discards them. The spec's own conservative-default instruction ("when uncertain, prefer true for speculation features") makes this worse -- it widens the funnel into Rule 3.

The fundamental issue is that Rule 3 conflates "we can't mechanically verify this" with "this is not real." In Semgrep triage workflows, the equivalent of Rule 3 is a "needs review" bucket, not a "discard" bucket. Speculation-only findings should be routed to `verify`, not discarded. The verification agent is designed precisely for this: it has tools to check whether the speculated behavior is real.

**Rule 5 (no signals -> verify) has the right default.** Discarding unclassifiable findings would be a precision trap. If the feature extractor cannot classify a finding, it means the finding falls outside the feature model's vocabulary. That is an information gap, not evidence of a false positive. Sending it to verification is correct and conservative.

**No `keep` outcome is correct for this pipeline.** In SonarQube, there are "blocker" severity rules where findings skip manual review, but those are deterministic pattern matches with near-zero false positive rates. LLM-generated findings never reach that confidence level. Skipping verification is never safe for LLM outputs. The spec's reasoning ("even obvious structural defects may be mitigated by code the explorer didn't see") is exactly right.

**Evidence:** Spec lines 128-131 (Rule 3), lines 136-139 (Rule 5), lines 141-142 (no `keep` rationale).

**Recommendation:**
Change Rule 3 from `discard` to `verify`. The verification agent exists to resolve speculation. If cost is a concern, add a `low_priority` routing that gives the verifier a reduced tool budget (1 call instead of 3) for speculation-only findings. Discarding them wholesale is the single biggest precision risk in the spec.

---

### Question 3: Verification agent behavior

**Assessment:**

**Three tool calls is tight but defensible for structural defects. It is insufficient for cross-cutting concerns.** A typical verification sequence is: (1) Read the cited file/line, (2) Grep for a defense pattern (guard clause, middleware, validation), (3) Read the defense candidate to confirm it applies. That is exactly 3 calls and it works when the defect and defense are co-located. It fails when:

- The defense is in a different module (Grep finds it, but confirming it applies to this call site requires reading a third file -- that is 4 calls).
- The finding involves a call chain (caller -> intermediate -> callee). Verifying requires reading at least 2 files plus a grep.
- Concurrency issues require understanding multiple callers accessing shared state. Grep finds concurrent callers, but confirming no lock exists requires reading each caller.

In practice, a 3-call budget will produce a false negative rate of roughly 10-20% for cross-module defects. The "default to false_positive" rule amplifies this: it converts budget exhaustion into a discard. This is the right *default direction* (skepticism is correct for a precision-oriented pipeline), but the rate of real bugs lost to budget exhaustion is non-trivial.

**The `verification_command` requirement is a net positive for accuracy.** In my experience, requiring concrete evidence forces the verifier to ground its reasoning in actual code rather than plausible-sounding narratives. The risk the spec correctly identifies (rejecting findings it cannot mechanically reproduce) is real but manageable: the `needs_investigation` verdict exists precisely as an escape valve for findings the verifier believes are real but cannot prove mechanically. The spec should make this explicit -- if the verifier believes a finding is likely real but cannot produce a `verification_command` within the budget, the verdict should be `needs_investigation`, not `false_positive`.

**The 6 defect-type search strategies are reasonable but incomplete.** The missing category is **configuration/environment issues** -- findings about missing environment variables, incorrect defaults, or unsafe configuration. These cannot be verified by Read/Grep/Glob because the config may be injected at runtime. The verification agent should have a "cannot verify from code alone" path for these.

**Evidence:** Spec lines 166-192 (verifier behavior, 3-call budget, quick reference).

**Recommendation:**
1. Increase the tool call budget to 5. The marginal cost is small (2 more Read/Grep calls at ~pennies each), and it covers the common "defense is in another file" pattern.
2. Add an explicit rule: "If you believe the finding is likely real but cannot produce a verification_command within the budget, assign `needs_investigation`, not `false_positive`."
3. Add configuration/environment defects to the quick reference with guidance: "If the defect depends on runtime configuration that cannot be verified from code, assign `needs_investigation`."

---

### Question 4: Activation threshold

**Assessment:**

**Five is a reasonable threshold.** Small finding sets (1-5 findings) typically have a lower false positive rate than large sets because they come from focused reviews of small diffs. The explorer has less opportunity to hallucinate when there are fewer files to review. In my experience, false positive rates for small finding sets are 10-20%, versus 30-50% for large sets (20+ findings). The judge's 4-expert panel (Gatekeeper -> Verifier -> Calibrator -> Synthesizer) is sufficient for 5 findings -- it was designed for this.

**`always_triage: true` below threshold provides limited value.** Running Stages 1-2 on 3 findings extracts features and runs triage, but the results are only useful if they feed into something. If Stage 3 is skipped, the triage decisions are discarded. The only value is logging -- you get to see "2 of 3 findings would have been discarded by triage." This is diagnostic information, not operational. The LLM call for feature extraction on 3 findings costs ~$0.05 and 3-8 seconds. The value-to-cost ratio is poor. I would set the default to `always_triage: false` and let users opt in for diagnostic purposes.

**Chunked mode inconsistency is a real risk.** When per-chunk verification runs independently, the same pattern (e.g., "missing error check on database query") could be `confirmed` in chunk A (where the verifier happened to find no defense) and `false_positive` in chunk B (where the verifier found middleware that handles it). This is not just a theoretical concern -- it is the most common inconsistency in chunked static analysis. The final cross-chunk judge partially mitigates this (it deduplicates and can reconcile contradictions), but the spec should explicitly instruct the judge to check for inconsistent verdicts across chunks. If the same defect pattern is `false_positive` in any chunk, the judge should investigate whether the defense applies to all chunks.

**Evidence:** Spec lines 196-220 (activation threshold, chunked mode).

**Recommendation:**
1. Default `always_triage` to `false` below threshold. Feature extraction on 3 findings is not worth $0.05 and 3-8s when the results are unused.
2. Add a cross-chunk consistency check: instruct the final judge to flag when the same defect pattern receives different verdicts in different chunks, and investigate whether the defense found in one chunk applies globally.

---

## Feature 1: Two-Pass Judge

### Question 5: Verify-then-synthesize separation

**Assessment:**

**Sequential completion instructions partially prevent narrative bias, but do not eliminate it.** In LLM architectures, the attention mechanism does not have a "forget Pass 1 before starting Pass 2" capability. The model will carry verification context forward into synthesis. However, the structural benefit is real: by forcing the model to produce all verification annotations *before* writing the narrative, you prevent the common failure mode where the model keeps a dubious finding because it has already written a paragraph about it. The sequential instruction works not because the model forgets, but because it has not yet committed to a narrative when it makes the verify/disprove decision.

In multi-phase analysis pipelines (e.g., SonarQube's "detect -> classify -> report" pipeline), separating analysis from reporting measurably improves output quality even when the same engine runs both phases. The two-pass restructure is sound.

**When Feature 0 is active, Pass 1 should be simplified, not skipped.** The judge receiving pre-verified findings still needs to:
- Verify `needs_investigation` findings (this is explicitly stated).
- Check for cross-finding contradictions (Finding A says "no validation," Finding B's evidence shows validation exists).
- Detect if verification missed something obvious (a sanity check on the verifier's work).

Skipping Pass 1 entirely when Feature 0 is active would remove these checks. The spec's current design (judge runs Pass 1 but with a lighter load) is correct. However, the judge prompt note ("Do not re-verify `confirmed` findings -- focus on synthesis, deduplication, and verdict") partially contradicts the two-pass structure by telling the judge to skip verification for confirmed findings *during Pass 1*. If Pass 1 is verification and the judge is told not to verify confirmed findings, what does Pass 1 do for confirmed findings? The answer should be: Pass 1 checks for cross-finding contradictions and verifies `needs_investigation` findings. Pass 1 *passes through* confirmed findings without re-verification. This needs to be stated explicitly.

**Mapping onto the existing 4-expert panel.** The current judge has Gatekeeper -> Verifier -> Calibrator -> Synthesizer. The two-pass restructure maps as:
- Pass 1 = Gatekeeper + Verifier (Steps 1-3)
- Pass 2 = Calibrator + Synthesizer (Steps 4-6)

This mapping is clean. The Gatekeeper's auto-discard rules (phantom knowledge, speculative concern, framework-guaranteed, outside diff scope, style/formatting, duplicate of deterministic) overlap significantly with Stage 2's triage rules. When Feature 0 is active, the Gatekeeper becomes partially redundant -- it is re-checking what triage already checked. The spec should acknowledge this overlap and either: (a) instruct the Gatekeeper to trust triage decisions for discarded findings, or (b) reduce the Gatekeeper's role when pre-verified findings are present.

**Evidence:** Spec lines 258-274 (two-pass judge), judge prompt lines 1-44 (4-expert panel), spec line 256 ("Do not re-verify confirmed findings").

**Recommendation:**
1. Clarify Pass 1's role for confirmed findings: "Pass 1 checks cross-finding contradictions and passes through confirmed findings. It does not re-verify confirmed findings individually."
2. Add a note about Gatekeeper/triage overlap: when Feature 0 is active, the Gatekeeper should focus on cross-finding contradictions rather than re-running the same discard rules triage already applied.

---

## Feature 5: Fix Validation

### Question 6: Fix validation scope

**Assessment:**

**The 6 checks will catch roughly 20-30% of LLM-suggested fix issues in practice.** The most common fix problems in my experience are:
1. Missing imports (very common with LLM-generated code) -- **covered**.
2. Scope creep / behavior change -- **covered but unreliably**.
3. Type mismatches -- **covered**.
4. Fixes that work for the cited example but break edge cases -- **not covered** (this is the biggest gap).
5. Fixes that reference APIs or functions that don't exist or have different signatures -- partially covered by "undefined variables/functions."

The 60-70% of issues not caught are mostly semantic correctness problems that require running or deeply reasoning about the code. The spec's explicit non-goals (style, "best" approach, performance) are the right exclusions.

**Scope creep is not reliably detectable by an LLM verifier.** "Does the fix change behavior beyond what the finding describes?" requires understanding the finding's intended scope and comparing it to the fix's actual behavioral impact. This is a judgment call that humans disagree on. An LLM verifier will have high variance on this check -- sometimes flagging legitimate fixes that happen to touch an adjacent line, sometimes missing fixes that subtly change semantics. I would keep the check but lower expectations: treat scope creep detection as a signal that reduces confidence in the fix rather than a hard `fix_valid: false`.

**Broken fix should NOT reduce finding credibility.** The spec gets this right: "keep finding as confirmed (bug is real), add `fix_valid: false`." A real bug does not become less real because the suggested fix is wrong. In SonarQube, the finding and the quick-fix are independent: you can have a true positive finding with a broken quick-fix, and no one confuses them. The spec's design matches production practice.

**Evidence:** Spec lines 280-312 (fix validation scope and behavior).

**Recommendation:**
1. Keep the 6 checks but treat scope creep as a soft signal: `fix_valid: false` only for mechanical breakage (undefined vars, type mismatches, missing imports, syntax errors). Scope creep should produce `fix_valid: "uncertain"` or a note rather than a hard failure.
2. Consider adding "fix introduces a new defect" as a 7th check (e.g., fix introduces a resource leak while fixing an error handling issue). This is the most user-trust-damaging fix problem.

---

## Architecture Cross-Cutting

### Question 7: Pipeline latency and cost

**Assessment:**

**15-40s of additional latency is acceptable. 120s (the hard limit) is not.** In my experience, developers tolerate code review tool latency up to about 60 seconds total before they context-switch away. If the base review takes 30-60s and verification adds 15-40s, the total is 45-100s. That is at the edge of tolerance. If verification hits the hard limit (120s), the total review time exceeds 150s, and developers will stop watching for the result -- they will check back later, and later often means never. The threshold gating (skip Stage 3 for small reviews) correctly avoids adding latency where it is least needed.

**"One LLM call with tool use" for Stage 3 is ambiguous.** The spec says Stage 3 is "one LLM call with tool use," but for 9 findings routed to verify, does the verifier handle all 9 in one call? If so, this is a single prompt with 9 findings and up to 27 tool calls (3 per finding). Context window pressure is a concern: 9 findings with their cited code, plus the verification context, plus 27 tool call results, could approach 30-50K tokens. This is within Sonnet's context window but will degrade quality for later findings (attention decay over long contexts).

Alternatively, if it is one call *per finding*, the cost estimate is wrong: $0.20-0.50 per review becomes $0.20-0.50 *per finding*, or $1.80-4.50 for 9 findings. The spec needs to clarify this. My recommendation is one call per finding for accuracy, with a corresponding cost update.

**Evidence:** Spec lines 405-411 (performance budget), lines 151-155 (Stage 3 description).

**Recommendation:**
1. Clarify whether Stage 3 is one LLM call for all findings or one per finding. If one for all, add a per-finding context budget to prevent attention decay. If one per finding, update the cost estimate.
2. Set the hard limit at 90s instead of 120s. A 150s total review time (60s base + 90s verification) is the maximum acceptable. Partial results after 90s are better than complete results after 120s that nobody reads.

---

### Question 8: Degradation and failure modes

**Assessment:**

**Missing failure mode: feature extractor returns features for wrong finding indices.** This is the most insidious failure in batch extraction pipelines. If the LLM returns 10 feature sets but the findings were reordered, or if it skips finding index 3 and shifts everything up, triage will make correct decisions on incorrect data. A structural defect finding gets classified as speculation because its features came from a different finding. The degradation matrix does not address this.

The fix is a validation step between Stage 1 and Stage 2: verify that the returned `finding_index` values match the input finding indices. If any are missing, duplicated, or out-of-range, either: (a) re-request extraction for the mismatched findings, or (b) fall back to sending all findings to verification (matching the "feature extraction fails" row in the degradation matrix).

**Missing failure mode: triage has a false negative.** If triage *should* have discarded a finding (it is a quality opinion) but the feature extractor set `is_quality_opinion: false`, the finding passes to verification. The verifier then wastes a tool-call budget on a finding it should never have seen. This is a cost inefficiency, not a correctness problem -- the verifier will likely assign `false_positive` or `needs_investigation`. The degradation matrix does not need to handle this because the pipeline is self-correcting: verification catches what triage misses.

**Stage 1 and Stage 3 disagreement is by design.** If feature extraction says `is_quality_opinion: true` and triage discards the finding, we never learn whether verification would have confirmed it. This is acceptable because the `is_quality_opinion` feature is a "hard discard" -- the spec's design intentionally makes this a one-way gate. The risk is that the feature extractor mislabels a real defect as a quality opinion. The mitigation is to make the `is_quality_opinion` feature conservative: the extraction prompt already says "when uncertain, prefer false for structural features and true for speculation features," but it does not say "when uncertain, prefer false for `is_quality_opinion`." It should.

**Evidence:** Spec lines 337-344 (degradation matrix), lines 96-102 (output format with finding_index).

**Recommendation:**
1. Add index validation between Stage 1 and Stage 2: assert that returned finding indices are a permutation of the input indices. On mismatch, fall back to "send all to verification."
2. Add to the feature extraction prompt: "When uncertain whether a finding is a quality opinion or a real defect, set `is_quality_opinion: false` (conservative -- avoids discarding real defects)."
3. Add an off-by-one row to the degradation matrix: "Feature indices misaligned -> Send all findings to verification."

---

### Question 9: What would you change?

**Assessment:**

**Highest-leverage improvement: Change Rule 3 from `discard` to `verify`.** This is the single change that would most improve precision. Rule 3 discards speculation-only findings, which includes a significant class of real bugs that happen to reference external behavior. The verification agent exists precisely to resolve speculation. Discarding before verification defeats the purpose of having a verification agent. The cost increase is modest (a few more verification calls) and the precision gain is substantial.

**Biggest risk the spec does not address: feature extractor quality feedback loop.** The spec has no mechanism to learn whether triage decisions were correct. In production static analysis pipelines (SonarQube, Semgrep), teams track "false positive rate by rule" and tune rules over time. This spec has a fixed triage ruleset with no instrumentation for measuring rule accuracy. After deployment, you will not know whether Rule 3 discarded 100 real bugs or 100 false positives. Add structured logging of triage decisions (finding content, features, triage decision) and periodically sample discarded findings to measure the false discard rate. Without this, the pipeline degrades silently.

**Production precedent: I have seen a similar 3-stage architecture in a custom static analysis pipeline at a previous organization.** The architecture was: (1) rule-based detection, (2) deterministic triage, (3) human review of triaged findings. What went wrong:

1. **Triage rules ossified.** Rules written for the initial codebase became incorrect as the code evolved. "Quality opinion" classifications became stale. Without feedback, the rules silently discarded real findings for 6+ months before anyone noticed.
2. **Stage 3 trust calibration drifted.** Reviewers learned that most findings reaching them were real (because triage filtered noise), so they approved findings faster. When triage rules broke and noise leaked through, reviewers kept approving because their prior had shifted. The same risk applies here: if the judge learns that pre-verified findings are usually correct, it becomes less skeptical and more likely to pass through false positives that slipped past triage and verification.
3. **Batch feature extraction produced correlated errors.** When the extractor misunderstood one finding, it often misunderstood similar findings in the same batch. This created correlated triage errors: an entire class of findings was systematically discarded.

**Evidence:** Entire spec architecture.

**Recommendation:**
1. Add structured logging for triage decisions to enable post-deployment accuracy measurement.
2. Add a periodic "triage audit" process: sample N discarded findings per week and manually check whether they were correctly discarded.
3. Consider running Stage 3 on a random 10% sample of discarded findings to measure the false discard rate automatically.

---

## Verdict: WARN

The architecture is sound and well-motivated. The 3-stage pipeline, Kodus-AI precedent, skeptical defaults, and degradation matrix reflect serious design work. However, Rule 3's aggressiveness and the 3-call verification budget create precision risks that will lose real bugs in production.

## Top 3 Recommendations (ranked by precision impact)

1. **Change Rule 3 from `discard` to `verify`.** Speculation-only findings include real bugs that reference external behavior. The verification agent should resolve the speculation, not the triage rules. This is the single highest-precision-impact change. If cost is a concern, route speculation-only findings with a reduced tool budget (1-2 calls) rather than discarding them.

2. **Increase verification tool budget from 3 to 5 calls.** Three calls covers the "read line, grep for defense, confirm defense" pattern but fails for cross-module defects where the defense is in a different file. Five calls covers the common case and keeps costs low (2 additional Read/Grep calls are ~pennies). Also add: "If likely real but cannot produce verification_command within budget, assign `needs_investigation`, not `false_positive`."

3. **Add finding-index validation between Stage 1 and Stage 2, and add structured triage logging.** Batch feature extraction is the single point of failure for triage accuracy. Index misalignment causes correlated misclassification. Validation catches this before it causes damage. Structured logging enables post-deployment accuracy measurement so triage rules can be tuned rather than ossifying.
