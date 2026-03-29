# Review Prompt: LLM Prompt Reliability Engineer

**Target spec:** `specs/verification-architecture.md` (F0: 3-stage verification, F1: two-pass judge, F5: fix validation)

**Reviewer persona:** A prompt engineer who specializes in reliable, structured LLM output for production systems. Deep experience with: getting LLMs to follow multi-step instructions faithfully, designing prompts that produce parseable JSON under pressure, calibrating LLM behavior with few-shot examples, and debugging cases where LLMs silently deviate from instructions. Knows that the gap between "the prompt says to do X" and "the LLM actually does X" is where production systems break.

---

## Context to Read First

1. Read the spec fully
2. Read the existing feature extraction prompt instructions in the spec (the full markdown block in Stage 1)
3. Read the existing verifier prompt in the spec (Stage 3, including the preamble, 5 steps, quick reference, and examples)
4. Read the existing judge prompt at `skills/codereview/prompts/reviewer-judge.md` — focus on the Expert Panel structure (lines 9-25) and the Gatekeeper rules (lines 28-44) to understand what the two-pass restructure modifies
5. Read one explorer prompt at `skills/codereview/prompts/reviewer-correctness-pass.md` to see how the existing investigation phases are structured (this is the quality bar for the verifier prompt)

## Review Questions

### Feature Extraction Prompt (Stage 1)

**1. Will the feature extractor produce consistent boolean outputs?**
The prompt asks the LLM to extract 11 boolean features per finding in one batch call.
- With 10-20 findings in a single call, will the LLM maintain consistent feature extraction quality across all findings? Or will quality degrade for later findings (attention fatigue)?
- The rules say "when uncertain, prefer false for structural features and true for speculation features." In practice, do LLMs follow asymmetric default instructions, or do they default to whatever the first example showed?
- The output format is `{findings: [{finding_index, features}]}`. Will the LLM reliably produce valid JSON with all 11 fields per finding? What's the typical failure mode (missing fields? extra commentary? features as strings instead of booleans)?

**2. Feature extraction calibration.**
The prompt has rules but no few-shot examples.
- Should the feature extraction prompt include 2-3 calibration examples showing a finding and its correct features? (e.g., "This finding about a nil map write has `has_resource_leak: false, has_wrong_algorithm: false, has_missing_error_handling: true`")
- Without examples, the LLM has to infer the mapping from rules alone. In your experience, does rule-only prompting produce reliable boolean extraction, or do few-shot examples meaningfully improve consistency?
- The `improved_code_is_correct` feature is asking the LLM to evaluate code correctness in a batch extraction call (no tool access). Is this reliably extractable without actually running/reading the code? Or should this feature be moved to Stage 3 (verification) where the agent has tool access?

**3. Batch size limits.**
The spec says "one batch LLM call for ALL findings." With 30+ findings (chunked mode), that's 30+ finding objects in a single prompt.
- At what point does batch size degrade extraction quality? Is there a practical limit (e.g., 15 findings per call)?
- Should the spec define a max batch size with overflow handling (split into 2 calls if >15 findings)?

### Verifier Prompt (Stage 3)

**4. Will the verifier follow the 5-step protocol?**
The verifier has a structured 5-step protocol (read code, check claim, search for defenses, generate evidence, assign verdict).
- In your experience, do LLMs follow 5-step verification protocols faithfully, or do they shortcut (e.g., jump from Step 1 to Step 5 if the code looks suspicious)?
- The "skeptical by default — looking for reasons the finding is WRONG" framing is strong. But does it cause the opposite problem — a verifier that's too aggressive in discarding findings because it was told to be skeptical?
- Step 4 (generate verification_command) requires the LLM to produce a concrete grep/read command. In your experience, do LLMs generate valid, runnable grep commands with correct regex syntax? Or is this a common failure point (wrong flags, invalid regex, wrong file paths)?

**5. Default-to-false_positive within tool call budget.**
The spec says the verifier gets up to 10 tool calls per finding and defaults to `false_positive` if it can't confirm within that budget.
- Is "up to 10 tool calls" the right framing? The LLM may interpret this as a hard budget or as a soft guideline. The ambiguity could cause the verifier to either underuse tools (stops early) or overuse them.
- Should the instruction be more specific? e.g., "Use up to 10 tool calls. If after exhausting your budget you cannot find concrete evidence that the defect exists, assign false_positive."

**6. Verifier output format.**
The verifier produces `[{finding_index, verdict, reason, verification_command}]`. The spec already mandates JSON-only output ("Output ONLY the JSON array below. No commentary before or after. Use lowercase verdict strings exactly: `confirmed`, `false_positive`, `needs_investigation`.").
- Will the LLM reliably produce a JSON array with one entry per finding, matching the finding indices from the input?
- Even with the JSON-only mandate, common failure modes include: the LLM omitting findings it deems obvious, reordering findings, or producing inconsistent verdict strings despite explicit enumeration.
- Are there additional enforcement mechanisms (e.g., completeness constraint requiring exactly one entry per input finding) that would improve reliability?

**7. Quick reference effectiveness.**
The spec includes 6 defect-type-specific search strategies (resource leak → search for close/defer, race condition → search for lock/mutex, etc.).
- Do inline reference tables improve LLM verification accuracy, or do they add noise that competes with the per-finding instructions?
- Should these strategies be integrated into the step-by-step protocol instead? (e.g., "Step 3: Search for defenses. For resource leaks, search for close()/defer. For race conditions, search for locks/mutex.")

### Two-Pass Judge (Feature 1)

**8. Will the "complete Pass 1 before Pass 2" instruction work?**
The spec instructs the judge to complete all verification (Pass 1) before starting synthesis (Pass 2).
- In your experience, do LLMs respect "complete phase A before starting phase B" instructions in long prompts? Or do they blend phases when the context is large (40k+ tokens of findings)?
- The current judge already has a 4-stage sequential structure (Gatekeeper → Verifier → Calibrator → Synthesizer). Adding a pass boundary on top of the stage boundaries creates a 2×4 matrix. Is this too many structural constraints for reliable execution?
- Would it be more effective to split the two passes into two separate LLM calls (one for verification, one for synthesis) rather than relying on in-prompt sequencing?

**9. Pre-verified findings instruction.**
When Feature 0 is active, the judge prompt gains: "Do not re-verify `confirmed` findings — focus on synthesis, deduplication, and verdict."
- Will the LLM actually skip re-verification for confirmed findings? In your experience, do "do not re-verify" instructions work, or does the LLM re-verify anyway because it's in "verification mode" from its existing Gatekeeper/Verifier stages?
- Is there a risk that the judge trusts confirmed findings too much and misses errors the verifier made (false confirmed)?

### Fix Validation (Feature 5)

**10. Can the verifier reliably check fix correctness?**
The verifier checks 6 properties of suggested fixes (undefined vars, type mismatches, missing imports, syntax errors, scope creep, self-containedness).
- Checking "does this fix introduce undefined variables" requires the verifier to mentally compile the modified code. Is this reliable without actually running a type checker?
- "Scope creep" (fix changes behavior beyond finding) is a judgment call. In your experience, can LLMs reliably assess scope creep, or is this too subjective for consistent results?
- Should fix validation be a separate prompt/call rather than an extension to the verification prompt? Adding Step 5 to an already-5-step protocol pushes to 6 steps — do you see reliability degradation at that point?

---

## Output Format

For each question, provide:
1. **Assessment** — based on your experience with LLM prompt reliability
2. **Evidence** — reference specific prompt text in the spec
3. **Recommendation** — concrete prompt change, output enforcement, or structural adjustment

Conclude with verdict (PASS/WARN/FAIL) and top 3 recommendations ranked by impact on prompt reliability.
