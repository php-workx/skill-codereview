# Adversarial QA Review: Explorer & Judge Behavior Plan

**Reviewer:** Adversarial QA (automated)
**Plan:** `docs/plan-explorer-judge-behavior.md`
**Date:** 2026-03-28
**Scope:** Stress-testing 8 features (F4, F5, F6, F7, F8, F9, F10, F11) for measurability, failure modes, and feature interaction risks

---

## 1. Phantom Knowledge Self-Check (F10) -- Will the Model Actually Self-Check?

**Severity: MODERATE**

### Attack Scenario

The self-check is 30 lines of "ask yourself these 4 questions before every finding." This is the classic "think about this before answering" pattern. The model reads the self-check at token position ~800 of the global contract. By the time it is deep into Phase 2 investigation (token position 40k+), the self-check has been diluted by 40k tokens of code, grep results, and intermediate reasoning. The model will not stop mid-reasoning to re-derive the 4 questions from memory.

Research on LLM self-correction (Huang et al. 2023, "Large Language Models Cannot Self-Correct Reasoning Without External Feedback") demonstrates that self-checks without new information generation degrade to no-ops. The proposed self-check generates no new information -- it asks the model to re-evaluate claims it already made, using the same context it already has.

**Specific failure modes:**

1. **Compliance theater.** The model learns to produce plausible-sounding evidence citations ("Verified by reading src/auth/login.py:45") without actually performing the Read call. The self-check asks "can you point to a specific visible line?" and the model answers "yes" by confabulating a line number. There is no external verification that the Read call happened.

2. **Over-correction on unfamiliar codebases.** When reviewing a codebase with deep abstractions (e.g., a DI container, a macro-heavy Rust crate, or a metaprogramming-heavy Python project), the model genuinely cannot trace execution through opaque layers. The self-check's absolutist framing ("If you cannot point to a specific visible line, DO NOT make the claim") suppresses legitimate findings about code that calls through opaque interfaces. The model should still be able to say "this call to `container.resolve(AuthService)` may not validate tokens" even if the DI resolution code is not visible -- that is a real finding, not phantom knowledge.

3. **No verifiable artifact.** The self-check is a mental exercise, not a structured output. Compare with F5 (certification), which produces a JSON object with `tools_used`. If F10 produced a per-finding `phantom_check` field (e.g., `"phantom_check": "evidence_source": "Read src/auth/login.py:45-60"`) the judge could verify it. As designed, F10 is unauditable.

### Metrics

- **Success metric:** Phantom-knowledge false positive rate (findings where `evidence` references code not in the diff or investigation context) drops by >30% compared to baseline. Measurable by re-running the OWASP and Martian benchmarks and manually auditing the `evidence` field of each finding for phantom references.
- **Failure metric:** True positive recall drops >10% without a corresponding precision gain. This indicates over-suppression. Measure via the same benchmarks, comparing finding counts and TP/FP classifications.
- **Over-correction metric:** Count findings where the explorer returns `[]` on diffs that human reviewers found real bugs in. Compare "certified clean" rates before/after F10.

### Proposed Mitigations

1. Convert the self-check from a mental exercise to a structured output: add a `evidence_source` field to each finding that forces the explorer to cite the specific Read/Grep call that provided the evidence. The judge can then verify the citation exists in the tool call history.
2. Soften the absolutist framing. Instead of "DO NOT make the claim," use "flag the claim as assumption-based and set confidence <= 0.69." This preserves the finding for the judge to evaluate rather than silently suppressing it.
3. Move the self-check to a position closer to output generation (after Phase 4, not as a standalone section) so it is fresher in context when findings are being finalized.

---

## 2. Mental Execution Framing (F11) -- Instruction Following Under Context Pressure

**Severity: MODERATE**

### Attack Scenario

The correctness explorer already has 8 investigation phases spanning ~170 lines of dense instructions. The Mental Execution Protocol adds a 9th block as a *preamble* before Phase 1. By the time the model reaches Phase 4 (State Invariant Check), it has consumed the diff (~5-30k tokens), grep/read results from Phases 1-3 (~10-30k tokens), and its own chain-of-thought reasoning (~5-10k tokens). The preamble is now 20-60k tokens in the past.

**Specific failure modes:**

1. **Preamble amnesia.** The model follows the preamble for the first 1-2 functions, then reverts to its default pattern-matching behavior as context pressure increases. This is well-documented in long-context evaluation research -- instructions at the beginning of the prompt lose influence as the middle grows ("Lost in the Middle," Liu et al. 2023).

2. **Overlap confusion.** The 5 execution contexts (repeated invocations, concurrent, delayed, failure mid-operation, cardinality) overlap substantially with existing phases:
   - "Repeated invocations" = Phase 4 (State Invariant Check, "multi-run state reasoning")
   - "Concurrent execution" = Phase 4 (State Invariant Check, "concurrent access")
   - "Failure mid-operation" = Phase 6 (Default/Skip Path Analysis, "conditional construction")
   - "Cardinality analysis" = Phase 3 (Boundary Analysis, "enumerate edge cases")

   The model faces an ambiguous choice: investigate concurrency under the Mental Execution Protocol, or under Phase 4's explicit instructions? If it does both, it doubles the investigation time (and token cost) for no additional signal. If it does neither (assuming the other phase will handle it), the check is skipped entirely. Neither outcome is good.

3. **High-bar suppression.** The plan says: "Only report issues where you can trace the EXACT execution path." This is the highest possible evidence bar. Medium-confidence findings (0.70-0.84 in the current calibration table) are by definition findings where the "call path is not fully traced." The Mental Execution Protocol effectively tells the explorer to suppress everything below 0.85 confidence, contradicting the global contract's calibration table. Which instruction wins? The model has to resolve the contradiction, and resolving contradictions under context pressure is exactly where LLMs fail unpredictably.

### Metrics

- **Success metric:** Correctness findings have higher average confidence (>0.82 vs baseline ~0.75) AND recall on known bugs (from OWASP/Martian ground truth) does not decrease. Both conditions must hold simultaneously.
- **Failure metric:** Average findings-per-review drops >25% without a corresponding increase in precision. This means the high bar is suppressing valid findings, not just noise.
- **Contradiction metric:** Count findings where the explorer explicitly invokes the Mental Execution Protocol framing in its evidence (e.g., "Mental execution:..."). If <20% of findings reference it, the preamble is being ignored.

### Proposed Mitigations

1. Do not add the Mental Execution Protocol as a preamble. Instead, integrate its execution contexts into the existing phases where they naturally belong:
   - Add "repeated invocations" to Phase 4 (it is already partially there as "multi-run state reasoning")
   - Add "concurrent execution" to Phase 4 (already partially there)
   - Add "delayed execution" (closures, callbacks) to Phase 3 or Phase 6
   - Add "failure mid-operation" to Phase 6 (already partially there)
   - Add "cardinality analysis" to Phase 3
   This avoids the overlap problem entirely.

2. Soften the evidence bar. Instead of "trace the EXACT execution path or don't report," use "if you cannot trace the exact path, set confidence to 0.65-0.69 and note what is unverified." This preserves medium-confidence findings for the judge to evaluate.

3. If keeping the preamble, repeat its key constraint at the output step: add "Reminder: only report issues with traced execution paths" to the Output Schema section so it is fresh when findings are being serialized.

---

## 3. Per-File Certification (F5) -- Gaming and Overhead

**Severity: CRITICAL**

### Attack Scenario

Certification requires explorers to produce `files_checked`, `checks_performed`, and `tools_used`. The model fills these fields from the same generation pass that decides whether to report findings. There is no external verification that the listed tool calls actually happened or that the checks were actually performed.

**Specific failure modes:**

1. **Plausible fabrication.** The model produces:
   ```json
   {
     "files_checked": ["src/auth/login.py", "src/auth/session.py"],
     "checks_performed": [
       "Traced 3 callers of login() -- none affected",
       "Verified session handling is consistent"
     ],
     "tools_used": ["Grep: callers of login()", "Read: src/auth/session.py:40-60"]
   }
   ```
   This looks legitimate but may be fabricated. The model can generate plausible-looking tool call descriptions without actually making those calls. The judge has no way to verify whether "Grep: callers of login()" was a real tool invocation or a hallucinated description. The tool call history is available to the orchestrator but is NOT passed to the judge -- only the certification object is.

   **This is the critical gap:** The certification is self-reported with no external validation mechanism. It is exactly as trustworthy as the explorer's other claims -- which is the problem F10 is trying to solve.

2. **Token overhead.** Each certification object is approximately 150-250 tokens. With 8 explorers, that is 1,200-2,000 additional tokens in the judge's input. For the common case where 3-4 explorers find issues and 4-5 certify clean, the judge receives 4-5 certification objects that provide minimal signal ("I looked and it's fine") plus the actual findings. The signal-to-noise ratio of certification objects is low.

   In file-batched mode (F7), the judge must Read each certification file separately, adding tool-call latency. For 5 clean explorers, that is 5 additional Read calls consuming ~3-5 seconds each, adding 15-25 seconds of wall time.

3. **Judge Step 0.5 is not lightweight.** The plan calls this a "lightweight check" and says "Do NOT re-run any explorer's analysis." But the only way to verify whether a certification is genuine vs. fabricated is to:
   - Check if the listed files actually exist (Read call)
   - Check if the described callers actually exist (Grep call)
   - Assess whether the checks described are relevant to the pass

   This is deep reasoning, not a plausibility check. If the judge does it superficially (reads the certification, says "looks fine"), Step 0.5 is theater. If the judge does it properly, it is re-running portions of the explorer's investigation, which the plan explicitly forbids.

### Metrics

- **Success metric:** Reviews where at least one explorer certified clean but the judge found a missed issue in the same area decrease by >50% (measured by running the same diff through the pipeline twice and comparing). This requires a ground truth dataset of diffs with known issues in areas that explorers tend to certify clean.
- **Failure metric:** Average judge token consumption increases >15% without measurable precision improvement. Certification overhead costs tokens but does not improve quality.
- **Gaming detection metric:** Inject a deliberately buggy diff where the correctness explorer should find issues. If the explorer certifies clean AND lists plausible-looking checks, the certification system has been gamed. Run this adversarial test across 10+ diffs to measure gaming rate.

### Proposed Mitigations

1. **Pass tool call history to the judge.** The orchestrator already has the tool call log from each explorer. Include a summary (tool name + arguments, not full output) alongside the certification. The judge can then verify that `tools_used` in the certification matches actual tool calls. This turns the certification from self-reported to externally validated.
2. **Set a minimum tool call threshold.** If an explorer makes <3 tool calls (Read/Grep/Glob) and certifies clean, the orchestrator flags it automatically before the judge sees it. This is a mechanical check, not AI reasoning.
3. **Drop Step 0.5 from the judge entirely.** Use the orchestrator to validate certifications mechanically (tool call count, file coverage). If validation fails, append a warning to the judge's input. This keeps the judge focused on finding validation, not meta-validation.

---

## 4. Contract Completeness Gate (F6) -- Scope Creep Into Spec Review

**Severity: MODERATE**

### Attack Scenario

The completeness gate checks: state transitions, error/edge behavior, cross-requirement consistency, and testability. These are spec review tasks -- they evaluate the *spec's quality*, not the *code's correctness*. The spec-verification explorer's original mandate is "ensuring every requirement in the spec is properly implemented in the diff." The gate changes the mandate to "also audit the spec itself for completeness."

**Specific failure modes:**

1. **Spec review masquerading as code review.** Gate findings are emitted as `pass: "spec_verification"` findings with `severity: medium`. They enter the same findings stream as code bugs. A user receiving "Spec completeness gap: payment gateway timeout behavior unspecified" alongside "Nil map write panics on skip path" has to mentally separate two fundamentally different types of feedback -- one requires changing the spec (a document), the other requires changing code. Mixing them creates confusion about what action to take.

2. **Noise explosion on vague specs.** Most real-world specs are vague. A typical spec says "the system should handle errors gracefully" without defining timeout behavior, retry strategies, or error state transitions. The gate would flag this as 3-4 GAP findings:
   - GAP: Error behavior -- no timeout specified
   - GAP: Error behavior -- no retry strategy specified
   - GAP: State transitions -- no error recovery state defined
   - GAP: Testability -- "handle gracefully" is not testable

   On a vague spec, EVERY gate item is GAP. The user gets 4+ medium-severity findings about their spec on every review. After 2-3 reviews, they learn to ignore spec_verification findings entirely -- which means they also miss legitimate "requirement not implemented" findings from Phases 1-5.

3. **Gate items are unbounded.** "Cross-requirement consistency" is an O(n^2) analysis (compare every requirement against every other). For a spec with 20+ requirements, the explorer would need to evaluate 190+ pairs. In practice, the model will check 5-10 pairs and call it done, making the gate assessment incomplete but appearing complete.

4. **Wrong place for this work.** Spec quality assessment should happen BEFORE implementation, not during code review. By the time code is being reviewed, the spec is (presumably) agreed upon. Telling a developer "your spec is vague" when they are trying to get code merged is not actionable in the PR context.

### Metrics

- **Success metric:** In reviews with specs, gate findings correctly identify spec gaps that led to actual implementation bugs in >50% of cases (measured by checking whether gap findings correlate with correctness/reliability findings in the same area).
- **Failure metric:** >70% of gate findings are on specs that are "intentionally informal" (acceptance criteria lists, one-pagers). If the gate fires mostly on informal specs, it is not providing useful signal.
- **Noise metric:** Average finding count per review increases by >3 findings (all spec_verification/medium) with no change in the verdict. This means the gate adds noise without changing decisions.

### Proposed Mitigations

1. **Separate spec_gap findings from code findings in the output.** Give them a distinct `pass` value (e.g., `spec_quality`) or a sub-category field so consumers can filter them. Do not mix spec critique with code critique in the same stream.
2. **Gate opt-in.** Only run the completeness gate when the spec is tagged as formal (e.g., `--spec-formal` flag, or auto-detect based on the presence of numbered requirements, state diagrams, or RFC-style language). For informal acceptance criteria, skip the gate.
3. **Cap gate findings at 2.** If the gate would produce 4+ GAP findings, emit only the 2 most impactful (by the model's assessment) and note "N additional spec gaps suppressed." This prevents noise explosion.
4. **Lower severity to `low`.** Spec gaps are advisory. They should not contribute to a WARN verdict. Use `severity: low` and `action_tier: consider` for all gate findings.

---

## 5. Feature Interaction: F5 (Certification) + F7 (File Batching) + F10 (Phantom Knowledge)

**Severity: MODERATE**

### Attack Scenario

When all three features are active simultaneously, they create a feedback loop that degrades signal quality.

**The loop:**

1. F10 (Phantom Knowledge Self-Check) makes explorers more cautious. They suppress findings where they cannot fully trace evidence. This is the intended behavior.
2. Increased caution means more explorers return `findings: []` with a certification (F5).
3. Certifications are written to temp files (F7).
4. The judge receives a manifest where 5-6 of 8 explorers report 0 findings.
5. The judge reads 5-6 certification files (F7), performs Step 0.5 (F5), and concludes "investigation looks thorough."
6. Result: The review is a rubber stamp. The explorers were cautious (F10), the certifications look plausible (F5), and the judge approved them (F7 made them easy to process).

**Specific failure modes:**

1. **Certified-clean flood.** When F10 is active, the bar for reporting findings is higher. On simple diffs (3-5 files, minor changes), ALL explorers may certify clean because F10 makes them doubt every finding. The judge sees 8 certifications, 0 findings, and produces a PASS verdict. But a human reviewer would have caught a subtle issue that the model suppressed because it could not fully trace the evidence.

   Estimated frequency: On the current benchmark suite, roughly 40% of diffs produce <5 findings across all explorers. With F10 raising the bar, this could increase to 60-70%, meaning most reviews produce 0-2 findings.

2. **File-batched cross-explorer comparison is harder.** The Calibrator (Expert 3) performs cross-explorer synthesis: root-cause grouping, causal chain dedup, contradiction resolution. These require seeing all findings together. With F7, findings are in separate files. The judge must Read each file, hold all findings in working memory, and then perform synthesis. This is a harder cognitive task than reading inline findings, because the model must assemble the cross-explorer view itself rather than having it presented. The plan does not address how the judge performs 3b (Root Cause Grouping) and 3d (Contradiction Resolution) when findings are in separate files.

3. **Certification objects consume Read-tool budget.** If the judge is implemented as a Claude sub-agent with tool-call limits, reading 5-6 certification files + 2-3 findings files consumes 7-9 Read calls before any verification begins. The Verifier (Expert 2) needs Read/Grep calls for evidence checking. The total tool-call budget may be exhausted on administrative reads.

### Metrics

- **Interaction success metric:** On a fixed benchmark suite, the combined F5+F7+F10 configuration produces the same or better F1 score as the baseline (no features active). If F1 drops, the interaction is harmful.
- **Interaction failure metric:** "Certified clean" rate exceeds 70% of explorer runs (vs. baseline ~40%). This indicates over-suppression amplified by the certification mechanism.
- **Judge tool budget metric:** Count total tool calls in judge runs with F7 active vs. inline. If administrative reads (certification files, findings files) exceed 50% of total tool calls, the judge is spending more time on data loading than analysis.

### Proposed Mitigations

1. **Include all findings inline AND in files.** Use file batching for the detailed evidence, but include a summary of ALL findings (just pass/severity/file/summary, no evidence) inline in the judge prompt. This gives the judge the cross-explorer overview needed for synthesis without requiring it to assemble the view from separate files.
2. **Track and report "certified clean" rate as a pipeline health metric.** If it exceeds a threshold (e.g., 60% of explorers on a diff with >10 changed functions), emit a warning in the review output.
3. **Do not count certification reads against the judge's tool budget.** Have the orchestrator inline certification summaries (just `status`, `files_checked` count, `tools_used` count) so the judge does not need to Read them.

---

## 6. Measurability Assessment

For each feature, here are the metrics that would prove success and prove failure. Features where I cannot define both are not testable.

### F10: Phantom Knowledge Self-Check

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Phantom-knowledge FP rate drops >30% on OWASP+Martian benchmarks | YES -- requires manual annotation of "phantom" vs. "evidence-backed" findings, which is laborious but possible |
| Failure | TP recall drops >10% on same benchmarks | YES -- automated via ground truth comparison |
| **Verdict** | **Testable, but requires manual FP annotation pipeline that does not yet exist.** The plan does not describe how to measure phantom knowledge reduction. |

### F11: Mental Execution Framing

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Average finding confidence increases AND recall holds | YES -- confidence is a numeric field, recall is measurable against ground truth |
| Failure | Finding count drops >25% without precision gain | YES -- automated |
| **Verdict** | **Testable.** But the plan does not propose running the benchmark before and after. Without a committed before/after evaluation, this is a "ship and hope" change. |

### F4: Test Pyramid Vocabulary

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | >80% of test-adequacy findings include `test_level` and `bug_finding_level` fields | YES -- schema validation |
| Failure | Test-adequacy findings decrease in count or quality | PARTIALLY -- quality is subjective; count is measurable |
| **Verdict** | **Testable for compliance, not for quality.** The model may fill in `test_level: "L1"` and `bug_finding_level: "BF4"` on every finding without meaningful differentiation. Need a rubric for whether the classifications are accurate. |

### F5: Per-File Certification

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Missed-bug rate on "certified clean" areas drops >50% | REQUIRES adversarial dataset of diffs with known bugs in specific focus areas |
| Failure | Token overhead >15% without quality improvement | YES -- token counting is automated |
| **Verdict** | **Not testable without a purpose-built adversarial dataset.** The plan does not describe how to build one. |

### F6: Contract Completeness Gate

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Gate findings correlate with implementation bugs >50% of the time | REQUIRES manual analysis of finding pairs |
| Failure | >70% of gate findings are on informal specs | YES -- but requires tagging specs as formal/informal |
| **Verdict** | **Barely testable.** Success metric requires cross-referencing spec findings with code findings, which needs human judgment. |

### F7: Output File Batching

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Judge can process 50+ findings without quality degradation | YES -- compare F1 on large-diff reviews, batched vs. inline |
| Failure | Judge cross-explorer synthesis quality drops | PARTIALLY -- requires manual review of dedup/grouping quality |
| **Verdict** | **Testable for the core claim** (handles large reviews). Quality impact is harder to measure. |

### F8: Pre-Existing Bug Classification

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | Pre-existing bugs correctly classified >80% of the time | REQUIRES dataset of diffs with known pre-existing bugs |
| Failure | Model sets `pre_existing: true` on bugs the diff introduced | YES -- automated check against diff boundaries |
| **Verdict** | **Partially testable.** Failure metric is automatable. Success metric needs a curated dataset. |

### F9: Provenance-Aware Review Rigor

| Metric | Definition | Testable? |
|--------|-----------|-----------|
| Success | On autonomous-generated code, additional AI-codegen-specific findings are surfaced | REQUIRES dataset of AI-generated diffs with known AI-codegen patterns |
| Failure | On human-authored code with `--provenance autonomous`, false positive rate increases from pattern over-matching | YES -- run existing benchmarks with provenance flag and compare FP rates |
| **Verdict** | **Testable for failure mode, hard to test for success.** Need an AI-generated code benchmark. |

---

## 7. Ship/Hold Recommendation

### Ship First (Wave 1)

| Feature | Recommendation | Rationale |
|---------|---------------|-----------|
| **F10: Phantom Knowledge** | SHIP with modifications | Highest-impact problem (phantom knowledge is the #1 FP source). But convert from mental exercise to structured `evidence_source` field. Softened framing, not absolute suppression. |
| **F4: Test Pyramid Vocabulary** | SHIP as-is | Low risk. Adds optional schema fields. If the model fills them poorly, they are ignored. Does not degrade existing behavior. |

### Ship Second (Wave 2, after benchmark validation)

| Feature | Recommendation | Rationale |
|---------|---------------|-----------|
| **F7: Output File Batching** | SHIP with inline summary | The scaling problem is real. But include an inline finding summary alongside file paths so the judge has the cross-explorer view. |
| **F8: Pre-Existing Bug Classification** | SHIP after enrichment script exists | Clean design, clear use case. Blocked by dependency, not by design concerns. |

### Hold (needs redesign or more evidence)

| Feature | Recommendation | Rationale |
|---------|---------------|-----------|
| **F11: Mental Execution Framing** | HOLD -- integrate into existing phases instead | The preamble approach has three problems: amnesia, overlap, and contradictory evidence bars. Integrating the execution contexts into existing phases solves all three with less risk. |
| **F5: Per-File Certification** | HOLD -- needs external validation mechanism | Self-reported certification without tool-call-history verification is security theater. Ship only after the orchestrator passes tool call summaries to the judge. |
| **F6: Contract Completeness Gate** | HOLD -- needs scope limitation | Spec review during code review is scope creep. If shipped, must be opt-in, capped at 2 findings, and severity-capped at low. Otherwise it trains users to ignore spec_verification findings. |
| **F9: Provenance-Aware Review Rigor** | HOLD -- needs AI-generated code benchmark | Cannot validate the feature without a dataset of AI-generated diffs with known AI-codegen patterns. Build the benchmark first, then ship the feature. |

### Summary

Of the 8 features, 2 are ready to ship (F10 with modifications, F4 as-is), 2 are ready to ship after dependency resolution and minor adjustments (F7, F8), and 4 should be held for redesign or evidence gathering (F11, F5, F6, F9). The most dangerous features are F5 (certification theater) and F11 (contradictory evidence bars) -- both could degrade review quality while appearing to improve it.
