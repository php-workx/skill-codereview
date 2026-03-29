# Persona Reviews: Expert Selection Specs

**Date:** 2026-03-28
**Specs reviewed:** Spec A (`adaptive-expert-selection.md`), Spec B (`dynamic-expert-enrichment.md`)
**Both reviewers:** WARN verdict

---

## Agent Architect Review — Key Findings

### Top 3 Recommendations (by production impact)

1. **Add deterministic pre-judge finding dedup.** With 6 experts producing 30-90 raw findings, the judge's quality degrades. A pre-pass that groups by file+line+token-similarity before the judge sees them is the highest-leverage quality improvement. Without it, judge quality scales inversely with expert count — exactly the scenario these specs create.

2. **Fix noisy activation for `error-handling` and `reliability`.** Patterns like `catch|except|error` and `close|fetch|http` will fire on 70-80% of diffs. Add `ignore_paths` for test files, or require 2+ signal sources for these experts. Currently they're effectively core experts without being marked as such.

3. **Flip enrichment priority: domain checklists before language checklists.** For specialized experts (database, concurrency), the domain checklist is higher signal than the language checklist. If the 1,500 token P5 budget is tight, the database expert should keep `checklist-sql-safety.md` and drop `checklist-go.md`, not the reverse.

### Other Notable Findings

- **Shell expert: merge 6 phases into 4.** LLMs reliably follow 3-4 phases. After that, quality degrades. Merge Phases 4+6 into Phase 3, keep Phase 5 standalone. Four phases is the sweet spot.
- **Remove file+line dedup prescription.** The judge already does semantic dedup via Gatekeeper/Verifier/Calibrator. The mechanical file+line rule contradicts the judge's mandate and will cause incorrect merges.
- **Accessibility expert is a trap.** Produces high-volume, low-severity findings ("missing alt text"). Teams that care use axe-core/Pa11y in CI. Demote to a checklist folded into frontend, or add strong `deactivates_on` when a11y testing tools are in project dependencies.
- **Missing: `source_expert` finding provenance.** No mechanism to trace which expert produced each finding through the judge pipeline. Essential for evaluating expert quality over time.
- **Missing: backwards-compatibility/API-breaking-changes expert.** Would produce outsized value (highest-severity bugs) and no existing expert reliably detects breaking API changes.
- **Coverage map: reference specific files, not just domains.** "No database specialist reviewed `migrations/0042.py`" is actionable. "No database domain coverage" is not.
- **Add 500-token minimum floor for enrichments in cascade.** Ensures at least one checklist survives even under extreme diff pressure.
- **Add checklist preamble.** "Before concluding your analysis, verify your findings against the following checklist" compensates for the post-diff injection position.
- **End-to-end estimate for the reviewed scenario:** 6 experts, 7 LLM calls, $4-6 total, 2-4 minutes wall-clock. Meaningfully better review than current 3-expert system.
- **Positive surprise:** Degradation matrices are production-grade. "This is the kind of defensive engineering that separates production systems from prototypes."

---

## Product & Cost Engineer Review — Key Findings

### Top 3 Recommendations (by impact per dollar)

1. **Run a baseline eval before building anything in Wave 1.** Execute Martian + OWASP benchmarks with current 10-expert panel. Record F1 and Youden. After Wave 1, run again. Define success as F1 +5% or Youden +0.05. Define kill: no improvement = revert. Costs 2 hours and $20 in API calls. Without this, the project is hope-based.

2. **Ship Wave 0 + shell-script expert only as minimum increment.** The shell expert is fully specified with clear provenance (9 CodeRabbit gap findings). Ship it alone, measure true positive rate on 10 shell-heavy diffs. Use result to calibrate expectations for remaining 4 experts. 2-3 days → first real signal, vs 2-3 weeks for full Wave 1.

3. **Defer Spec B entirely.** Per-expert enrichment saves <1% of token budget. The spec's own text says "the LLM naturally focuses on relevant items." Build checklists, inject globally, measure whether irrelevant checklists degrade quality. If they don't, Spec B is unnecessary complexity.

### Other Notable Findings

- **No eval plan in either spec.** Neither spec defines success metrics, baseline measurements, or kill criteria. Acceptance criteria test "does the code work?" not "does the review improve?" This is the most critical gap.
- **Cost increase is 25-60% unbounded.** Going from 3-5 to 5-6 experts at ~$1/expert. No `max_cost_per_review` config. No data on diminishing returns per expert rank.
- **Language checklists: zero before/after evidence.** Nobody tested whether injecting a checklist changes model behavior. The Wharton study tested personas, not checklists — the extrapolation is reasonable but unvalidated. Run 5 diffs with/without checklist before investing further.
- **User gets no visibility.** The coverage map goes to the judge, not the user. No report section shows which experts ran. Add an "Expert Panel" section to the report — cheapest way to demonstrate value.
- **Configuration is fine.** Defaults are sensible, no config needed for typical user. Good.
- **Prompt maintenance cost unaddressed.** 15 expert prompts with calibration examples will rot as models improve. Add quarterly eval per expert; drop experts below 50% true positive rate.
- **More experts = more noise risk.** 6 experts × 5-15 findings = 30-90 raw findings for the judge. Consider `max_findings_per_expert` cap (default: 15).
- **One sentence:** "Measure the true positive rate of the current 10-expert panel on the existing eval suite before writing a single new expert prompt, so you have a number to beat."

---

## Synthesis: Where Both Reviewers Agree

| Topic | Agent Architect | Product Engineer | Consensus |
|-------|----------------|-----------------|-----------|
| **Eval baseline needed** | Implicit (recommends finding provenance for quality metrics) | Explicit (#1 recommendation) | **Strong consensus: measure before building** |
| **Shell expert first** | Good prompt, merge to 4 phases | Ship alone as minimum increment | **Ship shell expert first, but consolidate phases** |
| **Noisy activation** | error-handling/reliability fire on 70%+ of diffs | More experts = more noise risk | **Fix activation patterns before expanding roster** |
| **Spec B premature** | Per-expert filtering has real value for cross-domain | Less than 1% token savings, defer entirely | **Disagreement — architect sees value, product says defer** |
| **Accessibility expert** | Trap — demote to checklist | Not explicitly flagged | **Consider demotion** |
| **Finding dedup** | Add deterministic pre-judge pass | Add max_findings_per_expert cap | **Both want finding volume bounded before judge** |
| **User visibility** | Coverage map should reference specific files | Add Expert Panel section to report | **Both want user to see which experts ran** |
| **Degradation design** | "Production-grade, best part of the spec" | Not flagged (focused on impact) | **Strength confirmed** |

---

## Actionable Changes to Consider

### Must-do (both reviewers flag)

1. **Add eval plan to Spec A** — baseline measurement, success metric (F1 +5%), kill criterion
2. **Fix noisy activation** for error-handling and reliability — add `ignore_paths` or require 2+ signals
3. **Add user-facing expert panel section** in the report template
4. **Add `source_expert` field** to finding schema for provenance through judge

### Should-do (one reviewer flags strongly)

5. **Ship shell expert first** as minimum Wave 1 increment, measure before writing 4 more
6. **Merge shell expert to 4 phases** (combine 4+6 into 3, keep 5 standalone)
7. **Add deterministic pre-judge finding dedup** (file+line+similarity grouping)
8. **Remove mechanical file+line dedup prescription** — let judge do semantic dedup
9. **Flip enrichment priority** — domain checklists before language checklists for specialized experts

### Consider (one reviewer suggests)

10. Demote accessibility to frontend checklist
11. Add backwards-compatibility/API-breaking expert to roster
12. Add `max_findings_per_expert: 15` cap
13. Add 500-token minimum floor for enrichments
14. Add prompt maintenance process (quarterly eval per expert)
15. Run 5-diff A/B test on language checklists before investing in per-expert routing
