# Review Prompt: Developer Experience & Workflow Engineer

**Target spec:** `specs/quality-compliance-context.md` (F2: spec-gated, F3: review summary, F6: context sufficiency, F7: doc injection, F8: scoring, F9: ticket verification, F10: output repair)

**Reviewer persona:** A senior engineer who has built CI integrations and developer-facing review tools. Deep experience with: GitHub Actions/CI pipelines, PR workflows, developer tool adoption, configuration design, and the gap between "feature works in demo" and "developers actually use it." Knows that developer tools succeed or fail based on: speed, signal-to-noise ratio, and how well they fit into existing workflows.

---

## Context to Read First

1. Read the spec fully — it contains 7 features
2. Skim the existing SKILL.md at `skills/codereview/SKILL.md` (first 50 lines) to understand how the tool is invoked
3. Skim `scripts/orchestrate.py` lines 1-50 for the pipeline architecture

## Review Questions

### Feature 2: Spec-Gated Pass Execution

**1. Gating threshold UX.**
The spec skips detailed review when >50% of "must" requirements are unimplemented.
- Is 50% the right threshold? What happens at 49% (runs full review) vs 51% (skips)? Is there a cliff effect where small spec changes flip the behavior?
- When the gate triggers, the user sees "Spec verification found major implementation gaps. Detailed code review deferred." Is this helpful or frustrating? A developer who just started implementing may want early feedback on what they HAVE written, not just "come back when you're done."
- Should there be a `--force-review` override that ignores the gate? The spec has `--force-verify` for verification but nothing for the spec gate.

### Feature 3: Review Summary

**2. Summary format for real-world use.**
The summary block is designed for PR descriptions.
- Have you seen developers actually paste review summaries into PRs? Or do they just link to the full report?
- The format uses `file:line` references and action tiers (Must Fix / Should Fix). Is this the right format for PR descriptions, or would reviewers prefer a different format (e.g., checklist with checkboxes)?
- The 10-line cap with "See full report for N additional findings" — is 10 the right number? In your experience, what's the sweet spot for a summary that gets read vs skimmed?

### Feature 6: Context Sufficiency Feedback Loop

**3. Latency impact of the sufficiency loop.**
The spec adds a second context collection round when the first round has gaps.
- How much latency does the sufficiency check add? (One LLM call for evaluation + potential Grep calls for additional queries.) Is this noticeable in the developer workflow?
- The spec caps at 2 rounds (initial + one sufficiency round). Is this enough? Or could there be cases where 2 rounds still leave gaps?
- When the sufficiency check says "sufficient," does the developer see this? Or is it invisible? Showing "Context collection: 2 rounds, 12 queries, all resolved" would build trust.

### Feature 7: Documentation Context Injection

**4. Context7 MCP integration realism.**
The spec proposes using the context7 MCP server for doc fetching.
- How reliable is context7 for real-time doc lookups during a code review? What's the latency per query?
- The baseline version (package names + versions in context, no doc fetching) — is this actually useful? Does telling an LLM "this code uses FastAPI 0.115" change its review behavior?
- If context7 is unavailable (MCP server not configured, network issues), the spec falls back to baseline. Is there a visible warning? A developer who configured `documentation.enabled: true` and gets no docs should know why.

### Feature 8: Numeric Scoring

**5. Score vs severity vs confidence — user confusion.**
After this feature, each finding has: `severity` (critical/high/medium/low), `confidence` (0.0-1.0), action tier (must_fix/should_fix/consider), AND `score` (0-10).
- That's 4 different quality signals on every finding. Is this too many? Which one does the developer look at?
- The spec says scores don't replace action tiers — they provide finer ranking within tiers. Will developers understand this distinction, or will they see "score: 7" and "severity: high" and wonder which one to trust?
- The explicit caps (documentation suggestions max 2, "verify/ensure" max 6) — are these visible to the developer? If a finding has `score: 2`, can the developer see it was capped because it's a documentation suggestion?

**6. `--min-score` filtering.**
The `--min-score` flag on `enrich-findings.py` drops findings below the threshold.
- Is this the right place for the filter? Should it be a `.codereview.yaml` config instead of a CLI flag on a script that most developers don't call directly?
- What's the recommended default? The spec says `min_score: 0` (keep all). Should the default be higher (e.g., 3) to reduce noise out of the box?

### Feature 9: Ticket & Task Verification

**7. Auto-detection reliability.**
The spec auto-detects tickets from branch names and commit messages.
- The branch name regex `/\b[a-z]{2,4}-[a-z0-9]{4}\b/` — how many false positives does this produce? Branch names like `feat/fix-auth-flow` contain "fix-auth" which matches `[a-z]{3}-[a-z]{4}`. Would this incorrectly try to look up a ticket called "fix-auth"?
- When auto-detection finds the wrong ticket, the entire compliance check is wrong. Is there a confirmation step ("Detected ticket att-0ogy from branch name. Is this correct?")?
- The heuristic plan-file matching (Step 5, branch name keywords against plan feature titles) — how often does this produce useful matches vs noise?

**8. Scope creep detection UX.**
The spec flags "unexpected files" — files changed in the diff but not mentioned in the ticket.
- In practice, how noisy is this? Developers frequently touch utility files, test helpers, and config files that no ticket would mention. Will scope creep warnings become noise that developers learn to ignore?
- Should there be a default ignore list for scope analysis? (e.g., `*.lock`, `*.json` config files, test fixtures, generated code)

**9. Dependency status warnings.**
The spec warns when dependency tickets are unresolved.
- Is this actually useful in practice? Developers often work on tickets in parallel with dependencies, and the dep ticket gets closed before merge.
- Could this create false alarms that reduce trust in the tool?

### Feature 10: Output Repair

**10. Repair masking prompt issues.**
The spec adds 6 JSON repair strategies.
- Is there a risk that repair success masks underlying prompt quality issues? If the judge consistently produces malformed JSON that gets auto-repaired, nobody notices the prompt needs fixing.
- Should repair events be surfaced more prominently? (e.g., "Warning: judge output required repair — this may indicate a prompt quality issue. Repair type: extracted from code block")
- The `repaired: true` flag in the JSON envelope — does anything downstream actually use this? Or is it just for debugging?

### Cross-Cutting

**11. Configuration complexity.**
This spec adds config keys: `verification.threshold`, `verification.always_triage`, `cross_file.sufficiency_check`, `cross_file.max_rounds`, `documentation.enabled`, `documentation.provider`, `scoring.min_score`, `scoring.show_scores`, `plan_context.auto_detect`, `plan_context.source`, `plan_context.verify_deps`, `plan_context.scope_analysis`.
- That's 12 new config keys across 4 namespaces. Combined with Spec A's keys, the total config surface is ~20 keys. Is this approachable for a new user?
- Which of these 12 keys would a typical developer ever change? If the answer is "maybe 2-3," should the rest be hidden in an "advanced" section?

**12. Feature interaction.**
These 7 features interact with each other and with the verification architecture:
- F8 (scoring) feeds into F3 (summary) — the summary shows "Must Fix" and "Should Fix" which are derived from action tiers, but now scores exist too. Should the summary show scores?
- F9 (ticket verification) feeds into F2 (spec-gated) — if ticket detection finds the wrong ticket, the gate may fire incorrectly.
- F6 (context sufficiency) feeds into all explorers — but the spec doesn't say what happens when sufficiency fails on the second round (gap still exists). Do explorers run with incomplete context, or does the review warn the user?
- F10 (output repair) should run before F8 (scoring) and F3 (summary) — is the pipeline ordering explicit?

---

## Output Format

For each question, provide:
1. **Assessment** — based on your experience building developer tools
2. **Evidence** — reference specific spec sections
3. **Recommendation** — concrete change or "no change needed"

Conclude with verdict (PASS/WARN/FAIL) and top 3 recommendations ranked by impact on developer adoption.
