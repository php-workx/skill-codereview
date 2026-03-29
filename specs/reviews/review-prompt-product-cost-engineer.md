# Review Prompt: Product & Cost Engineer

**Target specs:**
- `specs/adaptive-expert-selection.md` (Spec A: Expert Registry & Structured Selection)
- `specs/dynamic-expert-enrichment.md` (Spec B: Per-Expert Enrichment)

**Reviewer persona:** A product-minded engineer who owns the cost model and user experience of an AI-powered developer tool. Obsessive about proving impact with data before investing in features. Asks "what's the expected improvement?" before "how do we build this?" Skeptical of complexity that can't demonstrate measurable quality gain. Has shipped A/B tests for AI features and knows how to measure review quality.

---

## Context to Read First

1. Read both specs fully
2. Read the research doc at `docs/research-adaptive-expert-selection.md` — especially the "Decisions" section at the bottom and the Wharton persona study findings
3. Optionally skim the pre-mortem reports at `specs/reviews/adaptive-expert-selection-pre-mortem.md` and `specs/reviews/dynamic-expert-enrichment-pre-mortem.md` for context on what's already been challenged

## Review Questions

### Is This Worth Building? (Impact)

**1. Expected quality improvement: what's the hypothesis?**
The current system has 10 experts (3 always run, 7 activated by regex). The spec expands to 15 experts with better selection. Concretely:
- What specific bug categories does the current system miss that the expanded roster catches?
- The spec references "CodeRabbit gap analysis findings #1,5,6,14,15,24,26,27,29" as motivation for the shell expert. How many of those 9 findings would the new shell expert actually catch? Is there an estimate, or is this aspirational?
- For the other 4 new experts (database, infrastructure, frontend, accessibility): what evidence exists that they'll produce findings the correctness/security experts miss?
- **What I want to see:** A concrete prediction — "we expect the expanded roster to increase true positive rate by X% on the eval suite" — with a methodology to validate it.

**2. Cost model: what does this cost per review?**
The current system: 3-5 experts × ~70k tokens input each = $0.60-1.00 in Sonnet input costs per review.
With Spec A:
- 5-6 experts × 70k = $1.00-1.25 per review. That's a 25-60% cost increase.
- Is the quality improvement worth a 25-60% cost increase?
- Does the spec define a cost ceiling? `max_experts: 6` bounds parallelism but not cost. Should there be a `max_cost_per_review` config?
- What's the marginal value of expert #5 vs #6? Is there diminishing returns data?

**3. Language checklists: provable value or feels-good complexity?**
The spec curates 25-item Go and 24-item Python checklists from Baz Awesome Reviewers (real PR discussions). These add ~500 tokens to each expert's prompt.
- Has anyone tested whether injecting a language checklist into an expert prompt actually changes its findings? Or is this theoretical?
- The Wharton study showed personas hurt coding performance. Could checklists have the same dilution effect — giving the LLM more things to look for causes it to look for each less carefully?
- **What I want to see:** A before/after comparison on 5 diffs — run the correctness expert with and without the Go checklist, compare findings.

**4. Per-expert enrichment (Spec B): is this premature optimization?**
Spec B routes checklists to specific experts instead of all experts. The token savings is ~600 per expert (~1% of the 70k budget).
- Is 1% token savings worth the complexity of `ChecklistRegistry`, `parse_checklist_frontmatter()`, `match_enrichments()`, domain/expert matching, and a config flag?
- The spec itself notes "the LLM naturally focuses on relevant items." If that's true, why build filtering?
- **What I want to see:** Evidence that global checklist injection causes measurable false positives. Without that evidence, Spec B is a solution looking for a problem.

### How Do We Know It Worked? (Measurement)

**5. Eval plan: how do we measure success?**
Neither spec defines success criteria beyond "acceptance criteria pass." But acceptance criteria test correctness (does the code work?), not quality (does the review improve?).
- What metric improves when this ships? Finding precision? Recall? F1? User satisfaction?
- How is this measured? The memory references an eval pipeline (Martian F1 16%, OWASP Youden +0.073). Will the expanded roster be measured on the same benchmarks?
- What's the kill criterion? If the expanded roster doesn't improve [metric] by [threshold] on the eval suite, what happens? Revert? Keep anyway?
- **What I want to see:** A concrete eval plan as a section in Spec A.

**6. A/B test design: how do we compare old vs new selection?**
The spec has a `expert_registry: false` rollback flag. This is the mechanism for A/B testing.
- Is there a plan to run both modes on the same diffs and compare?
- What diffs? The eval suite? Real user reviews? Both?
- Who reviews the comparison? Human expert? Automated metric?

**7. Cost tracking: do we know what we're spending?**
The spec adds structured logging (`expert_selected`, `expert_selection_complete`). But:
- Are token counts and costs logged per expert, per review?
- Can a user see "this review used 5 experts at $1.20 total"?
- Is there a cost dashboard or is cost invisible to users?

### User Experience

**8. Does the user notice any of this?**
From a user's perspective, they run `/codereview` and get a report. After Spec A ships:
- Does the report tell them which experts ran and why?
- Can a user see "the infrastructure expert caught 2 findings in your Helm charts that the correctness expert would have missed"?
- If not, how does the user know the expanded roster is working?
- **What I want to see:** The report template change that surfaces expert selection to the user.

**9. Configuration complexity: is this approachable?**
Spec A adds: `max_experts`, `expert_registry`, `experts: {name: false}`, `groups`, `requires_context`, `force_all`, `enrichment_mode` (Spec B).
- How many of these does a typical user need to know about?
- Is the default experience good enough that no config is needed?
- Is there a "simple mode" explanation somewhere? ("By default, the review tool automatically selects the right experts for your code. You don't need to configure anything.")

**10. Time to value: what's the minimum shippable increment?**
Both specs have multi-wave implementation plans. But waves are internal sequencing — the user doesn't care about waves. From the user's perspective:
- When is the first moment the user gets a better review? Wave 0 (frontmatter migration) is invisible. Wave 1 (new experts) is the first visible improvement.
- Could Wave 0 + a single new expert (e.g., just `infrastructure`) ship as a minimal increment before the full 5-expert Wave 1?
- What's the fastest path to "user notices improvement"?

### Risks the Pre-Mortems Didn't Catch

**11. Maintenance cost of 15 expert prompts.**
Each expert is 40-60 lines of domain-specific review instructions with calibration examples. The shell expert alone is ~150 lines.
- Who maintains these prompts as the LLM capabilities change? (Sonnet 5 may not need the same investigation phases as Sonnet 4.5)
- How do you detect when a prompt is stale? (e.g., a calibration example references a pattern that the model now handles natively)
- Is there a prompt quality monitoring process, or do prompts rot silently?

**12. The "more experts = more noise" risk.**
More experts means more findings. More findings means more judge work. The judge has a finite context window.
- Is there a scenario where 6 experts produce so many findings that the judge's quality degrades?
- What's the maximum finding count the judge handles well? (The verification pipeline plan suggests >30 findings triggers special handling)
- Should `max_experts` be lowered if the verification pipeline (Feature 0) is not yet shipped?

---

## Output Format

For each question, provide:
1. **Assessment** — your judgment
2. **Evidence** — reference specific spec sections
3. **Recommendation** — what to add, change, or measure

Conclude with:
- **Verdict:** PASS / WARN / FAIL
- **Top 3 recommendations** ranked by expected impact on review quality per dollar spent
- **One sentence:** What's the single most important thing this project should measure before investing further?
