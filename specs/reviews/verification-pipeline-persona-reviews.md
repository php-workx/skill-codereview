# Persona Reviews: Verification Pipeline Sub-Specs

**Date:** 2026-03-29
**Specs reviewed:**
- Verification Architecture (`specs/verification-architecture.md`) — by infra engineer + prompt engineer
- Quality/Compliance/Context (`specs/quality-compliance-context.md`) — by DX engineer + team lead

**All 4 verdicts: WARN** (architecture sound, specific risks need addressing)

---

## Verification Architecture: Consensus Findings

### Both reviewers agree (high confidence)

**1. Rule 3 ("speculation without structural = discard") is too aggressive.**
- Infra engineer: "15-25% of real bugs reference external behavior. A finding about unhandled 429 rate limits is speculative but potentially real. Change Rule 3 to `verify`."
- Prompt engineer: Not directly flagged, but aligned — recommends sending more to verification, not less.
- **Action: Change Rule 3 from `discard` to `verify`.**

**2. `improved_code_is_correct` feature is misplaced in Stage 1.**
- Infra engineer: "Triage rules never reference it. It duplicates Feature 5's fix validation. Remove from Stage 1."
- Prompt engineer: "Unreliable without tool access. Redundant with Feature 5's tool-assisted fix validation."
- **Action: Remove from Stage 1 feature set. Feature 5 owns fix validation entirely.**

**3. Stage 1 needs batch size limits and few-shot examples.**
- Prompt engineer: "At 30+ findings, extraction quality degrades. Add max batch size (15), few-shot examples, and output enforcement ('Output ONLY the JSON. No commentary.')."
- Infra engineer: Implicitly aligned — questions whether feature extraction on 3 findings is worth the cost.
- **Action: Add max batch size of 15, 2-3 calibration examples, explicit output enforcement instruction.**

**4. Fix validation scope creep check is unreliable.**
- Infra engineer: "Scope creep is not reliably detectable by an LLM. Should be a soft signal, not a hard `fix_valid: false`."
- Prompt engineer: "Scope creep detection is too subjective — ~40-50% accuracy. Reduce fix checks to 3 reliable ones (syntax errors, undefined names, missing imports)."
- **Action: Make scope creep a soft signal (note, not `fix_valid: false`). Core checks: syntax, undefined names, missing imports, type mismatches.**

### Infra engineer unique findings

**5. Missing features: `targets_test_code` and `duplicates_linter_result`.**
Test files generate disproportionate low-value findings. Linter duplicates waste verification calls. Both are cheap to detect.
- **Action: Add both features (13 total after removing `improved_code_is_correct`).**

**6. Add `has_concurrency_issue` structural feature.**
Race condition findings hit no structural feature currently and survive triage only by accident (Rule 5, "no signals").
- **Action: Add `has_concurrency_issue` (14 features total).**

**7. Increase verification tool budget from 3 to 5 calls.**
3 calls is sufficient for co-located defects but insufficient for cross-module defenses.
- **Action: Increase to 5 calls. Add explicit `needs_investigation` escape when verifier believes finding is real but can't produce verification_command.**

**8. Add index validation between Stage 1 and Stage 2.**
Feature extractor returning misaligned finding indices would cause correlated misclassification.
- **Action: Add index validation step.**

**9. Add triage logging for feedback loop.**
Without structured logging of discard decisions, triage rules ossify and silently discard real findings.
- **Action: Log every triage decision with features and rule that fired.**

**10. Cross-chunk consistency check needed.**
Same pattern getting `confirmed` in chunk A but `false_positive` in chunk B.
- **Action: Add note about cross-chunk consistency in chunked mode section.**

### Prompt engineer unique findings

**11. Clarify: is Stage 3 one call per finding or batched?**
The spec is ambiguous. One call for 9 findings has context pressure issues. One per finding is expensive.
- **Action: Specify explicitly. Recommended: one call per finding (each needs independent tool use), with max 10 findings verified per review (cost control).**

**12. Two-pass judge overlay is redundant.**
The existing 4-stage Expert Panel (Gatekeeper → Verifier → Calibrator → Synthesizer) already enforces verify-before-synthesize. Adding a Pass 1/Pass 2 boundary creates instruction clutter.
- **Action: Consider dropping the two-pass overlay. Instead, strengthen the existing Expert Panel handoffs. If keeping it, acknowledge the overlap explicitly.**

**13. Verifier needs stricter output enforcement.**
LLMs add narrative before/after JSON, use inconsistent verdict strings, omit findings they deem obvious.
- **Action: Add "Output ONLY the JSON array. No commentary. Use lowercase verdict strings exactly: confirmed, false_positive, needs_investigation."**

**14. Quick reference table should be integrated into Step 3.**
Appended reference tables compete with per-finding instructions for attention.
- **Action: Inline the search strategies into Step 3 ("Search for defenses. For resource leaks, search for close()/defer...").**

---

## Quality/Compliance/Context: Consensus Findings

### Both reviewers agree (high confidence)

**15. The 50% spec gate (F2) should be a warning, not a hard block.**
- DX engineer: "Early implementation is when developers most need feedback. The cliff suppresses reviews when they're most valuable."
- Team lead: "Developers will assume it's broken. Replace with completeness-first mode: always review, but front-load the spec completeness report with a banner."
- **Action: Change from hard gate to warning. Always run review. Add prominent "incomplete implementation" banner when >50% gaps. Add `--force-review` flag.**

**16. Ticket auto-detection regex will produce false positives.**
- DX engineer: "The regex `/\b[a-z]{2,4}-[a-z0-9]{4}\b/` matches `fix-auth`, `add-cors`, `test-e2e2`. False detections cascade through compliance checks and spec gating."
- Team lead: Not flagged directly but notes scope creep detection trust risk — which is downstream of the same auto-detection.
- **Action: Tighten the regex. Add a confirmation prompt when auto-detection is ambiguous. Add a "detected ticket: att-0ogy — correct? (y/n)" step, or at minimum log what was detected prominently.**

**17. Scope creep detection (F9) has high false positive risk.**
- DX engineer: "Developers frequently touch utility files, test helpers, config files. Will scope warnings become noise developers learn to ignore?"
- Team lead: "This feature has the highest trust-destruction risk. If it flags every utility file, my team will disable F9 entirely."
- **Action: Add default ignore list for scope analysis: `*.lock`, `*.json` config, test fixtures, generated code. Make scope warnings informational, not flagged as findings.**

**18. Four quality signals per finding is too many (F8).**
- DX engineer: "Severity, confidence, action tier, AND score creates decision paralysis. Which one does the developer look at?"
- Team lead: "If I could only keep one, I'd keep action tier. Scores are useful for triage but should default to hidden."
- **Action: Default `show_scores: false`. Scores used internally for ranking within tiers but not shown in the report unless the user opts in.**

### DX engineer unique findings

**19. No `--force-review` flag for spec gate.**
The spec has `--force-verify` but no bypass for the gate itself.
- **Action: Add `--force-review` flag.**

**20. Sufficiency loop (F6) needs user-facing progress output.**
"Context collection: 2 rounds, 12 queries, all resolved" would build trust.
- **Action: Add progress log line showing collection rounds and resolution status.**

**21. Output repair may mask prompt issues (F10).**
If the judge consistently produces malformed JSON that gets auto-repaired, nobody notices.
- **Action: Track repair rate. If >10% of reviews need repair, surface a warning about prompt quality.**

**22. Feature interaction ordering not explicit.**
F10 (repair) should run before F8 (scoring) and F3 (summary). Pipeline ordering not stated.
- **Action: Add explicit pipeline ordering note in the implementation plan.**

### Team lead unique findings

**23. Top 3 features: F3 (summary), F10 (output repair), F8 (scoring with caps).**
"The review summary is the feature I'd use on day one."
- **Action: Prioritize these in Wave 1.**

**24. Bottom 2 features: F7 full doc injection, F9 scope creep detection.**
F7 has high setup friction, episodic value. F9 scope detection risks trust destruction.
- **Action: F7 full version deferred. F9 scope analysis ships with default ignore list and informational-only warnings.**

**25. Missing: incremental reviews with finding persistence.**
"The one thing that would make me champion this tool: track which findings have been addressed between pushes."
- **Action: Note as future feature. Not in this spec's scope but high-value signal.**

**26. Need a `preset: team` config.**
Enable safe wins (F3, F8, F10) with one config key, default everything else off.
- **Action: Add preset concept to config schema.**

---

## Summary: All Findings by Priority

### Must-address (both reviewers in a pair agree)

| # | Spec | Finding | Action |
|---|------|---------|--------|
| 1 | Verif | Rule 3 too aggressive | Change to `verify` |
| 2 | Verif | `improved_code_is_correct` misplaced | Remove from Stage 1 |
| 3 | Verif | Stage 1 needs batch limits + examples | Max 15, add few-shot, add output enforcement |
| 4 | Verif | Fix validation scope creep unreliable | Make soft signal only |
| 15 | QCC | Spec gate hard-blocks reviews | Change to warning with banner |
| 16 | QCC | Ticket regex false positives | Tighten regex, add confirmation |
| 17 | QCC | Scope creep detection noise | Add default ignore list, make informational |
| 18 | QCC | Four quality signals too many | Default `show_scores: false` |

### Should-address (one reviewer flags strongly)

| # | Spec | Finding | Action |
|---|------|---------|--------|
| 5 | Verif | Missing `targets_test_code` feature | Add feature |
| 6 | Verif | Missing `has_concurrency_issue` feature | Add feature |
| 7 | Verif | 3-call budget too tight | Increase to 5 |
| 8 | Verif | Index validation missing | Add between Stage 1-2 |
| 9 | Verif | No triage logging | Add structured logging |
| 11 | Verif | Stage 3 batching ambiguous | Specify one-call-per-finding |
| 12 | Verif | Two-pass judge redundant | Consider dropping overlay |
| 13 | Verif | Verifier output enforcement | Add strict format instructions |
| 19 | QCC | No `--force-review` flag | Add flag |
| 23 | QCC | Prioritize F3, F10, F8 in Wave 1 | Reorder waves |

### Consider (one reviewer suggests)

| # | Spec | Finding | Action |
|---|------|---------|--------|
| 10 | Verif | Cross-chunk consistency | Add note |
| 14 | Verif | Inline quick reference into Step 3 | Restructure prompt |
| 20 | QCC | Sufficiency loop progress output | Add log line |
| 21 | QCC | Repair rate tracking | Add monitoring |
| 22 | QCC | Pipeline ordering explicit | Add ordering note |
| 24 | QCC | F7 full/F9 scope as bottom features | Defer/scope-down |
| 25 | QCC | Incremental reviews (future) | Note for future |
| 26 | QCC | `preset: team` config | Add preset concept |
