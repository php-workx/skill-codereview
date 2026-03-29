# Review Prompt: Multi-Agent Review Systems Architect

**Target specs:**
- `specs/adaptive-expert-selection.md` (Spec A: Expert Registry & Structured Selection)
- `specs/dynamic-expert-enrichment.md` (Spec B: Per-Expert Enrichment)

**Reviewer persona:** A senior engineer who has designed and shipped production multi-agent code review systems (comparable to Qodo 2.0, CodeRabbit, or Ellipsis). Deep experience with: LLM agent orchestration, reviewer prompt engineering, finding deduplication across parallel agents, token budget management, and the gap between "spec looks complete" and "this actually works in production."

---

## Context to Read First

1. Read both specs fully — they are self-contained
2. Skim `scripts/orchestrate.py` lines 82-107 (current expert definitions), 887-950 (current `assemble_expert_panel()`), and 983-1040 (`assemble_explorer_prompt()`) to understand the existing code
3. Read `skills/codereview/prompts/reviewer-judge.md` lines 1-80 to understand the judge's 4-expert panel (Gatekeeper → Verifier → Calibrator → Synthesizer)
4. Read one existing expert prompt (`skills/codereview/prompts/reviewer-correctness-pass.md`) to see the current investigation phase structure

## Review Questions

### Expert Selection (Spec A)

**1. Binary activation realism.**
The spec uses binary activation (any signal fires = candidate) with priority sort (more signals = higher rank). In your experience shipping agent selection systems:
- Does this produce the right expert panel in practice, or do you see failure modes (e.g., `error-handling` activating on every diff because `catch|except` appears in any non-trivial codebase)?
- Is `max_experts: 6` the right default? In your systems, what's the sweet spot for parallel agent count before quality degrades (judge overload, redundant findings, token pressure)?
- The spec has no "negative signals" (patterns that should suppress an expert even if positive signals fire, beyond `deactivates_on`). Is that a gap? For example: `error-handling` shouldn't activate if the only `except` in the diff is inside a test file.

**2. Shell expert prompt quality.**
The shell expert has 6 investigation phases (~150 lines). In your experience with LLM reviewer agents:
- Is this too prescriptive? Do LLM agents actually follow 6 sequential phases faithfully, or do they lose coherence after Phase 3-4?
- The phases are very specific (e.g., "Phase 1: trace each command substitution under `set -e` to determine if failure is caught"). Does this level of specificity help or hurt? In your experience, do LLMs perform better with 6 specific phases or 3 broader ones?
- The calibration examples use real findings from this codebase. Is self-referential calibration (training the expert on its own repo's bugs) a good pattern, or does it overfit?

**3. Cross-pass finding categorization.**
The spec says the shell expert emits findings with `pass: "reliability"` or `pass: "security"` depending on the finding type. The judge deduplicates by file+line.
- In production, does cross-pass categorization actually work, or does it confuse the judge when two findings at the same location have different severity/confidence from different experts?
- When Qodo's orchestrator resolves conflicts between agents, does it use file+line dedup, or a more semantic approach (e.g., finding similarity embedding)?
- Should the judge know which expert produced which finding, or should findings be anonymized before the judge sees them?

**4. Coverage gap detection value.**
The spec detects uncovered domains and reports them to the judge via a coverage map. In your experience:
- Does telling the judge "no Terraform specialist reviewed this" actually change judge behavior, or does the judge ignore informational context and focus on the findings it received?
- Would it be more effective to have the judge ask itself "what concerns about this diff are NOT addressed by any finding?" without the explicit coverage map?

**5. 5 new experts: right roster?**
The spec adds database, infrastructure, frontend, accessibility, ai-integration. In your experience building code review agent pools:
- Which of these 5 produce the highest signal-to-noise ratio in practice?
- Are any of these a trap (sounds valuable but produces mostly false positives or low-severity style findings)?
- Is there a domain expert you've seen produce outsized value that's NOT in this list?

### Per-Expert Enrichment (Spec B)

**6. Per-expert vs global checklist injection.**
Spec B routes domain checklists to specific experts (database expert gets SQL checklist, not concurrency checklist). Global injection is the fallback.
- In your experience, does per-expert context filtering improve finding quality? Or do LLMs ignore irrelevant checklist items anyway, making the filtering overhead pointless?
- The P5 budget is 1,500 tokens for enrichments. Is this enough? In your systems, how much domain-specific context per agent produces the best results?
- Is there a risk that per-expert filtering causes the system to miss cross-domain bugs? (e.g., a database query in a concurrent code path — the concurrency expert doesn't get the SQL checklist, and the database expert doesn't get the concurrency checklist)

**7. Language checklists: 15-25 items per language.**
The spec curates Go (25 items) and Python (24 items) checklists from real PR discussions (Baz Awesome Reviewers).
- In your experience, does injecting a 25-item checklist into an existing expert prompt improve its performance, or does it dilute the expert's core focus?
- Is there a better format than a checklist? (e.g., "test cases" — given this code pattern, what should the reviewer check?)
- Should the checklist be injected at the beginning of the prompt (prime the LLM's attention) or at the end (near the diff)?

**8. Token budget realism.**
Spec B's worst case is 2 language checklists (1,000 tokens) + 1 domain checklist (500 tokens) = 1,500 tokens at P5. The total explorer budget is 70k.
- In practice, does 1,500 tokens of checklist content compete with diff space? When diffs are large, does the truncation cascade cut checklists before they've been useful?
- Should enrichments have a minimum floor (like the diff has a 5,000 token floor in the cascade)?

### Architecture Cross-Cutting

**9. End-to-end: how does this feel in practice?**
Walk through a concrete scenario: a PR that modifies 3 Go files (one with goroutines, one with SQL queries, one with HTTP handlers) and 2 Helm chart YAML files. Under Spec A + B:
- Which experts activate? (correctness, security-config, test-adequacy, concurrency, database, infrastructure, api-contract = 7, capped at 6)
- Which expert gets cut by the cap? Is the cut the right one?
- What enrichments does each expert get? (Go checklist for all, SQL checklist for database, concurrency checklist for concurrency)
- How many total LLM calls? What's the estimated cost and wall-clock time?
- Does the judge produce a better review than the current 3-expert system?

**10. What would you do differently?**
If you were building this system from scratch with the same constraints (Claude-only models, no external API keys, sub-agent spawning via Task tool):
- What's the single highest-leverage change you'd make to the design?
- What's the biggest risk that neither spec addresses?
- What surprised you (positively or negatively) about the design?

---

## Output Format

For each question, provide:
1. **Assessment** — direct answer based on your experience
2. **Evidence** — what in the spec led to your assessment (cite section/line)
3. **Recommendation** — concrete change if needed, or "no change needed" with rationale

Conclude with a verdict: PASS / WARN / FAIL and your top 3 recommendations ranked by impact.
