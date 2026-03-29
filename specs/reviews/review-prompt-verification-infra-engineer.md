# Review Prompt: Verification & Testing Infrastructure Engineer

**Target spec:** `specs/verification-architecture.md` (F0: 3-stage verification, F1: two-pass judge, F5: fix validation)

**Reviewer persona:** A senior engineer who has built false positive filtering pipelines for static analysis tools — SonarQube quality gates, Semgrep triage workflows, or CodeQL alert suppression systems. Deep experience with: boolean feature models for issue classification, deterministic triage rule design, verification agent architecture, and the precision/recall tradeoff in automated code analysis. Knows that every filtering stage can both catch noise and accidentally discard real bugs.

---

## Context to Read First

1. Read the spec fully — it is self-contained
2. Skim the existing judge prompt at `skills/codereview/prompts/reviewer-judge.md` (lines 1-80) to understand the current 4-expert panel (Gatekeeper → Verifier → Calibrator → Synthesizer) that the two-pass restructure modifies
3. Skim the Kodus-AI comparison table in the spec to understand the production precedent

## Review Questions

### Feature 0: 3-Stage Verification

**1. Boolean feature model completeness.**
The spec defines 11 boolean features extracted per finding. Seven are structural defect features, four are meta/quality features.
- Are 11 features enough to classify code review findings? What categories of findings would be misclassified by this feature set?
- Is there a missing feature for "finding is about test code" (test-specific patterns like mock setup, fixture, assertion — often low-value findings)?
- Is there a missing feature for "finding duplicates a linter/tool result" (the judge's Gatekeeper already checks this, but catching it earlier in triage saves a verification call)?
- The `improved_code_is_correct` feature is unusual — it's about the fix, not the finding. Does this create confusion in the triage logic? (The triage rules only check structural defect features and speculation features, not this one.)

**2. Triage rule aggressiveness.**
The deterministic triage has 5 rules with 2 outcomes: `discard` or `verify`. There is no `keep` (auto-accept) outcome.
- Rule 3 discards "speculation without structural defect." In your experience with static analysis triage, what percentage of real bugs would this rule incorrectly discard? (Example: a finding that says "this function doesn't handle the case where the API returns a 429 rate limit" — speculative about external behavior, no structural defect feature, but potentially real.)
- Rule 5 sends "no signals at all" to verification. This means findings that the feature extractor couldn't classify get verified — is this the right default? Or should they be discarded (since the feature extractor couldn't even identify what kind of issue it is)?
- The spec says "no `keep` outcome — even obvious structural defects go through verification." In SonarQube or Semgrep workflows, is there an equivalent of "this is definitely a bug, skip verification"? Is skipping verification ever safe?

**3. Verification agent behavior.**
The verifier gets up to 10 tool calls per finding (Read, Grep, Glob) and defaults to `false_positive` if it can't confirm within that budget.
- Is 10 tool calls per finding enough? In your experience, how many steps does it typically take to verify a code finding (read the cited line, check the caller, check for a guard clause, trace cross-module defenses)?
- The default-to-`false_positive` rule is skeptical. In practice, does this cause Type II errors (real bugs discarded)? What's the typical false negative rate with a 10-call budget?
- The "quick reference by defect type" gives 6 search strategies. Are these the right 6? Is there a defect type that's hard to verify with Read/Grep/Glob alone (e.g., concurrency issues that require understanding execution order)?
- The `verification_command` requirement (a concrete grep/read command that proves the finding) is inspired by CodeRabbit. In your experience, does requiring reproducible evidence improve or hurt the verifier's accuracy? (It forces rigor but may cause the verifier to reject findings it can't mechanically reproduce.)

**4. Activation threshold.**
Verification is skipped for ≤5 findings, runs fully for 6-30, and always runs in chunked mode (>30 findings).
- Is 5 the right threshold? In your experience, what's the false positive rate for small finding sets (≤5)? Is it low enough that the judge can handle them directly?
- The spec says Stages 1-2 (feature extraction + triage) always run even below threshold when `always_triage: true`. Is this valuable? Does feature extraction on 3 findings provide enough signal to justify the LLM call?
- In chunked mode (50-100+ findings), per-chunk verification runs independently. Could this cause inconsistency where the same pattern is `confirmed` in chunk A but `false_positive` in chunk B (different context available per chunk)?

### Feature 1: Two-Pass Judge

**5. Verify-then-synthesize separation.**
The judge is restructured into Pass 1 (adversarial verification, Steps 1-3) and Pass 2 (synthesis, Steps 4-6), with an explicit "complete Pass 1 before starting Pass 2" rule.
- In your experience with multi-phase analysis pipelines, does forcing sequential completion actually prevent narrative bias? Or does the LLM's attention mechanism carry over context from Pass 1 into Pass 2 regardless of the instruction?
- When Feature 0 is active, the judge receives pre-verified findings. Pass 1 then only handles `needs_investigation` findings. Is it worth restructuring the judge for this partial use case, or should Pass 1 be skipped entirely when Feature 0 has already verified?
- The current judge already has Gatekeeper → Verifier → Calibrator → Synthesizer. How does the two-pass restructure (verification Steps 1-3 / synthesis Steps 4-6) map onto the existing 4-stage panel? Is there a conflict?

### Feature 5: Fix Validation

**6. Fix validation scope.**
The verifier checks 6 things about suggested fixes (undefined vars, type mismatches, missing imports, syntax errors, scope creep, self-containedness) and explicitly does NOT check 3 things (style, "best" approach, performance).
- In your experience, what percentage of LLM-suggested fixes have validation issues that these 6 checks would catch?
- Is "scope creep" (fix changes behavior beyond what the finding describes) reliably detectable by an LLM verifier? This seems like a judgment call, not a mechanical check.
- The spec says broken fixes get `fix_valid: false` but the finding is still kept. Is this the right behavior? Or should a broken fix reduce the finding's credibility (if the fix is wrong, maybe the diagnosis is also wrong)?

### Architecture Cross-Cutting

**7. Pipeline latency and cost.**
The spec estimates 15-40s for full verification (Stage 1: 3-8s, Stage 2: <50ms, Stage 3: 10-30s) at $0.25-0.55 per verified review.
- Is 15-40s of additional latency acceptable in your experience? At what point does review latency cause developers to ignore the tool?
- The spec says Stage 3 is "one LLM call with tool use." For 9 findings routed to verify, does the verifier handle all 9 in one call, or one call per finding? If one call for all 9, is context window pressure a concern?

**8. Degradation and failure modes.**
The degradation matrix has 5 rows (feature extraction fails, triage crashes, verification agent fails, verification times out, below threshold).
- Is there a missing failure mode? What happens if the feature extractor returns features for the wrong finding indices (off-by-one, reordered)?
- What happens if the verifier confirms a finding that should have been discarded by triage (triage had a false negative)?
- What happens if Stage 1 and Stage 3 disagree? (Feature extraction says `is_quality_opinion: true`, triage discards, but if we had verified, the verifier would have confirmed.)

**9. What would you change?**
If you were building this verification pipeline from scratch:
- What's the single highest-leverage improvement to the design?
- What's the biggest risk the spec doesn't address?
- Have you seen a production system with a similar 3-stage architecture? What went wrong?

---

## Output Format

For each question, provide:
1. **Assessment** — direct answer based on your experience
2. **Evidence** — reference specific spec sections
3. **Recommendation** — concrete change if needed

Conclude with verdict (PASS/WARN/FAIL) and top 3 recommendations ranked by impact on precision improvement.
