# Review: Quality, Compliance & Context Spec (F2 + F3 + F6 + F7 + F8 + F9 + F10)

**Reviewer:** Developer Experience & Workflow Engineer
**Spec:** `specs/quality-compliance-context.md`
**Date:** 2026-03-28

---

## Question 1: Gating Threshold UX (Feature 2)

**Assessment:**
The 50% threshold creates an all-or-nothing cliff. A developer at 49% gets a full review; a developer at 51% gets nothing but "come back later." In practice, early implementation is exactly when developers need feedback -- they want to know if the code they have written is on the right track before finishing the rest. Deferring review until the spec is "mostly done" optimizes for tool efficiency at the expense of developer utility.

The `--force-verify` override exists for verification, but there is no `--force-review` to bypass the gate itself. A developer who knows their spec is half-implemented and still wants code review has no recourse except to omit `--spec` entirely, which also loses the spec verification output they may have wanted.

**Evidence:**
- Spec Section "Feature 2: Spec-Gated Pass Execution" -- logic block shows binary >50% threshold with hard skip.
- Spec says `--force-verify` exists (referenced in review prompt) but no `--force-review` is specified.
- Activation requires >=3 "must" requirements, which is a reasonable minimum, but the 50% cliff remains.

**Recommendation:**
1. Add `--force-review` flag that bypasses the gate. This is table stakes for developer trust.
2. Consider a softer degradation: at >50% gaps, still run explorers but prepend a prominent "incomplete implementation" banner on the report, rather than suppressing the review entirely. Developers who invoke a review tool want a review.
3. At minimum, when the gate fires, list which requirements are implemented vs not, so the developer gets actionable output instead of just a dismissal.

---

## Question 2: Summary Format for Real-World Use (Feature 3)

**Assessment:**
The format is solid. `file:line` one-liners grouped by action tier is the format that actually gets read -- it matches how developers scan code review output. PR description pasting is a secondary use case; most developers will read this in the terminal and copy specific lines if needed.

The 10-line cap is reasonable. In my experience, summaries over 7 lines start getting skimmed rather than read. 10 is a defensible upper bound, especially with the "See full report for N additional findings" overflow.

The one gap: the summary has no timestamp or diff range context. A summary that says "2 issues to address" is ambiguous if the developer doesn't know which diff was reviewed. Including the base ref or commit range (e.g., "Reviewed: main..HEAD (12 files, +340/-89)") would make the summary self-contained when pasted into a PR.

**Evidence:**
- Spec Section "Feature 3: Review Summary for PR Descriptions" -- output format example shows `file:line` references and action tiers.
- Cap at 10 lines is explicit.
- No mention of diff metadata in the summary block.

**Recommendation:**
Add a one-line diff context header to the summary block (base ref, file count, line delta). Otherwise, the format is well-designed. No change to the 10-line cap.

---

## Question 3: Latency Impact of the Sufficiency Loop (Feature 6)

**Assessment:**
The sufficiency check adds one LLM call (evaluate context) plus potentially 5 more Grep queries. On a typical review, that is 3-8 seconds of added latency. For a tool that already runs multiple explorer passes and a judge pass (likely 30-90 seconds total), this is not a deal-breaker, but it adds up.

The 30% trigger rate from Kodus production data is encouraging -- it means 70% of reviews pay only the evaluation cost, not the re-query cost. Two rounds maximum is correct; three would be excessive and diminishing returns are steep.

The bigger UX issue: developers have no visibility into what is happening. The spec does not specify any user-facing output for the sufficiency check. When the review takes 8 seconds longer and the developer does not know why, they assume it is slow. Transparency builds trust.

**Evidence:**
- Spec Section "Feature 6: Context Sufficiency Feedback Loop" -- architecture shows 2 rounds max.
- "Kodus-AI's production data shows the sufficiency check triggers additional queries in ~30% of reviews."
- No user-facing output or progress indicator mentioned.

**Recommendation:**
Add a brief status line to the pipeline output: "Context check: sufficient (1 round)" or "Context check: 2 gaps found, running additional queries (round 2/2)." This is cheap to implement and converts perceived slowness into perceived thoroughness.

---

## Question 4: Context7 MCP Integration Realism (Feature 7)

**Assessment:**
The baseline version (package names + versions in context) is marginally useful. Telling a model "this code uses FastAPI 0.115" does not meaningfully change its review behavior for most reviews. The model already infers framework usage from import statements. The baseline becomes useful only when the version matters -- e.g., catching deprecated APIs that changed between versions -- and even then, the model's training data may already cover it.

The context7 MCP integration is speculative. MCP server availability depends on the developer's setup, network access, and whether the context7 server is even running. The spec acknowledges this by making it opt-in (`documentation.enabled: false` by default), which is the right call. But the fallback from "context7 unavailable" to "baseline only" is silent -- the developer enables docs, gets no docs, and does not know why.

The "web_search" fallback provider is mentioned but unspecified. What API? What rate limits? This is underspecified.

**Evidence:**
- Spec Section "Feature 7: Documentation Context Injection" -- baseline version at "Minimum Viable Version" subsection.
- Activation section: "Off by default."
- Fallback: "Or web search as fallback" -- no implementation detail for web search.
- No mention of what happens when context7 is configured but unreachable.

**Recommendation:**
1. When `documentation.enabled: true` and the provider is unavailable, emit a visible warning: "Documentation context: context7 server not available, using baseline (package names + versions only)."
2. Drop the "web_search" provider until it has a concrete implementation plan. Listing it as an option when it does not exist yet creates false expectations.
3. Ship baseline as the default behavior (no config needed) -- if you can detect packages from manifests, just include them. The opt-in should be for the external doc fetching, not for the package detection.

---

## Question 5: Score vs Severity vs Confidence -- User Confusion (Feature 8)

**Assessment:**
Four quality signals per finding (severity, confidence, action tier, score) is too many for a developer scanning results. In practice, developers will look at exactly one signal to decide what to fix: the action tier (Must Fix / Should Fix / Consider). The score, severity, and confidence become noise unless the developer is debugging the tool itself.

The spec says "scores don't replace action tiers -- they provide finer ranking within tiers." This is a nuance that no developer will internalize from documentation. They will see `score: 3, severity: medium, confidence: 0.7, tier: should_fix` and ask: "so is this important or not?"

The explicit caps (docs suggestions max 2, "verify/ensure" max 6) are a good engineering decision but are invisible to the developer. If a finding is capped to score 2, the developer cannot tell whether it scored low because it is low-quality or because it was capped. This erodes trust: "why is this SQL injection only a 2?" (answer: it is not SQL injection, it is a docs suggestion, but the developer does not see that).

**Evidence:**
- Spec Section "Feature 8: Per-Finding Numeric Scoring" -- scoring bands table, explicit caps, "scores don't replace action tiers."
- Finding schema gains `score` and `score_reason` fields.
- No mention of cap visibility in output.

**Recommendation:**
1. Default `show_scores: false`. Scores are a power-user feature and a ranking mechanism, not a user-facing signal. Action tiers are the interface; scores are the implementation.
2. If scores are shown, always show `score_reason` alongside. A bare number without context is worse than no number.
3. Make caps visible in `score_reason`: "Score 2 (capped: documentation suggestion)" so developers understand why.

---

## Question 6: --min-score Filtering (Feature 8)

**Assessment:**
`--min-score` as a CLI flag on `enrich-findings.py` is the wrong place for most developers. Nobody calls `enrich-findings.py` directly -- it is an internal pipeline script called by the orchestrator. The useful surface for this setting is `.codereview.yaml` (already mentioned as `scoring.min_score`) or a `/codereview` CLI flag.

The default of `min_score: 0` (keep all) is correct for initial rollout -- you want developers to see everything first and decide what to filter. But the spec should document a recommended value for teams that want lower noise. A score of 3 is a reasonable recommendation: it drops findings the spec itself classifies as "speculative, style preference" (1-2) and "wrong" (0).

**Evidence:**
- Spec Section "Feature 8" -- `enrich-findings.py` gains `--min-score` flag.
- Config section shows `scoring.min_score: 0`.
- Scoring bands: 0 = wrong, 1-2 = speculative/style.

**Recommendation:**
1. Keep `--min-score` on the script for internal use, but make the primary developer-facing config `scoring.min_score` in `.codereview.yaml`. Document it prominently.
2. Add a "recommended for teams" note: `min_score: 3` to filter speculative findings out of the box.
3. Consider adding `--min-score` to `/codereview` CLI flags for one-off overrides.

---

## Question 7: Auto-Detection Reliability (Feature 9)

**Assessment:**
The branch name regex `/\b[a-z]{2,4}-[a-z0-9]{4}\b/` is too broad and will produce false positives. Consider these real branch names:

- `feat/fix-auth` -- "fix-auth" matches `[a-z]{3}-[a-z]{4}`, detected as ticket ID
- `bugfix/add-cors-support` -- "add-cors" matches `[a-z]{3}-[a-z]{4}`, detected as ticket ID
- `chore/test-e2e2` -- "test-e2e2" matches `[a-z]{4}-[a-z]{4}`, detected as ticket ID

The regex assumes ticket IDs look like `att-0ogy` (prefix + alphanumeric), but the pattern `[a-z]{2,4}-[a-z0-9]{4}` also matches any common English word pair split by a hyphen where the second part is exactly 4 characters. This is extremely common in branch names.

When auto-detection finds the wrong ticket, the entire F9 compliance pipeline runs against wrong requirements. There is no confirmation step. The tool silently looks up "fix-auth" as a ticket, gets a "not found" from `tk query`, and... what happens? The spec does not say what happens when a detected ticket ID does not exist in the tracker.

The heuristic plan-file matching (Step 5) is even more fragile. Matching branch name keywords against plan feature titles will produce false matches on common terms like "auth," "api," "config."

**Evidence:**
- Spec Section "Feature 9: Ticket & Task Verification" -- regex `/\b[a-z]{2,4}-[a-z0-9]{4}\b/`.
- Auto-detection logic Steps 1-5.
- No error handling specified for when detected ID does not resolve to an actual ticket.
- No confirmation step.

**Recommendation:**
1. Tighten the regex. If `tk` tickets have a known prefix format (project-specific), use that. If not, require the ID to actually resolve via `tk query` before treating it as a match -- unresolved IDs are discarded silently.
2. Add "Detection confidence" to the output: `"detection_confidence": "high"` (explicit `--ticket` flag), `"medium"` (regex match resolved to real ticket), `"low"` (heuristic plan match). Surface this to the user.
3. Specify the behavior when a detected ID does not resolve: discard it and fall through to the next detection step (plan files, then nothing). Do not run compliance checks against a phantom ticket.
4. Consider a `--confirm-ticket` flag or interactive confirmation for medium/low confidence detections.

---

## Question 8: Scope Creep Detection UX (Feature 9)

**Assessment:**
This will be noisy. Developers routinely touch files that no ticket mentions:

- Lock files and dependency manifests (`package-lock.json`, `go.sum`)
- Shared utilities, helpers, and constants
- Test fixtures and test helpers
- CI config, linting config, editor config
- Documentation and changelogs
- Migration files triggered by schema changes

A ticket that says "Add ClaimableStore interface to state/types.go" does not mention the 5 test files, 2 test fixtures, the migration, and the CHANGELOG entry that come along for the ride. Flagging all of these as "scope creep" trains developers to ignore the warning entirely.

**Evidence:**
- Spec Section "Feature 9: Verification Checks -- Scope" -- "Changed files NOT mentioned in ticket/plan? (potential scope creep)."
- Output schema shows `unexpected_files` list.
- No ignore list or noise reduction strategy mentioned.

**Recommendation:**
1. Add a default ignore list for scope analysis: `*.lock`, `*_test.*`, `test_*`, `*_test.go`, `tests/`, `fixtures/`, `*.md`, `*.json` (config), `*.yaml` (config), `*.toml` (config), generated code patterns. Make it configurable.
2. Reframe the output: instead of "unexpected files" (which implies wrongdoing), use "files beyond ticket scope" and present them as informational, not as warnings. Only escalate to a warning when the count exceeds a threshold (e.g., >5 unrelated files).
3. Consider a smarter heuristic: if a file is imported by a file mentioned in the ticket, it is "related" not "unexpected."

---

## Question 9: Dependency Status Warnings (Feature 9)

**Assessment:**
Dependency warnings for unresolved tickets will produce false alarms in the majority of cases. Development teams commonly work on dependent tickets in parallel. Ticket A depends on Ticket B, but both are in progress simultaneously, and B will be closed before A is merged. The tool runs at review time, not at merge time, so the dep will often still be open.

Worse, parent epic sibling checks ("Parent epic sibling tickets resolved?") are almost never useful. Sibling tickets are parallel workstreams -- they are expected to be open.

These false alarms will erode trust in the compliance output. After seeing "WARNING: dependency att-drm1 is still in_progress" three times when the dep is actively being worked on, developers will stop reading compliance warnings entirely.

**Evidence:**
- Spec Section "Feature 9: Verification Checks -- Dependencies" -- "All deps in closed status? If not: warn premature implementation."
- "Parent epic sibling tickets resolved?"
- No mention of timing context (review time vs merge time).

**Recommendation:**
1. Change the default for `verify_deps` to `false`, or at minimum change the warning level from "warn" to "info." Dep status is informational context, not a compliance gate.
2. Drop the sibling ticket check entirely. It has no practical value and will only produce noise.
3. If dep checks remain, add status nuance: only warn if a dep is `blocked` or `not_started`. An `in_progress` dep is normal and should not trigger a warning.

---

## Question 10: Repair Masking Prompt Issues (Feature 10)

**Assessment:**
This is a real risk, but the spec already has the right mitigation: the `repaired: true` flag and stderr logging. The question is whether anyone will actually look at these signals.

In practice, repair success rates should be monitored as a prompt quality metric. If >20% of reviews require JSON repair, the prompt needs fixing. But the spec does not define this monitoring or alerting threshold.

The 6 repair strategies are well-ordered (cheapest first). The truncation recovery strategy (Strategy 6) is the most dangerous -- closing open arrays/objects and adding `truncated: true` means the tool is knowingly producing incomplete output. This should be surfaced prominently, not just as a JSON field.

**Evidence:**
- Spec Section "Feature 10: Output Repair" -- 6 strategies listed in order, `repaired: true` flag.
- "a high repaired rate may indicate a prompt issue" -- acknowledged but no threshold defined.
- Truncation recovery adds `truncated: true` but no user-facing warning specified.

**Recommendation:**
1. When truncation recovery fires (Strategy 6), surface a visible warning: "Warning: judge output was truncated and auto-completed. Some findings may be missing. Consider increasing token budget." This is not a silent-repair scenario.
2. Add a diagnostic mode or periodic report that surfaces repair rates. Even a simple "Repair stats: 3/10 recent reviews required JSON repair" in verbose output would be enough.
3. The `repaired: true` flag is sufficient for downstream consumers. No change needed there.

---

## Question 11: Configuration Complexity (Cross-Cutting)

**Assessment:**
12 new config keys across 4 namespaces, on top of the existing ~10 keys (`cadence`, `pushback_level`, `confidence_floor`, `ignore_paths`, `large_diff.*`, `token_budget.*`, `pass_models`, `judge_model`, `experts.*`, `triage.*`), puts the total config surface at ~22+ keys. This is approaching "nobody reads the docs" territory.

Of these 12 new keys, the ones a typical developer would actually change:
- `scoring.min_score` -- yes, to reduce noise
- `plan_context.auto_detect` -- maybe, to disable if false positives are annoying

The rest (`verification.threshold`, `verification.always_triage`, `cross_file.sufficiency_check`, `cross_file.max_rounds`, `documentation.enabled`, `documentation.provider`, `scoring.show_scores`, `plan_context.source`, `plan_context.verify_deps`, `plan_context.scope_analysis`) are power-user or team-lead settings that 95% of developers will never touch.

**Evidence:**
- Existing `DEFAULT_CONFIG` in `orchestrate.py` lines 40-70: already 10+ keys with nesting.
- Spec adds 12 new keys across Features 6, 7, 8, 9.
- No "quick start" config example or "recommended config" in the spec.

**Recommendation:**
1. Ship with aggressive defaults that work for 90% of cases. The tool should produce good results with zero config.
2. Add a "recommended team config" example to documentation that shows only the 3-4 keys worth customizing. Do not enumerate all 22 keys in the getting-started flow.
3. Consider a single `level: strict | standard | lenient` meta-key that sets sensible defaults for groups of keys. Power users can still override individual keys.

---

## Question 12: Feature Interaction (Cross-Cutting)

**Assessment:**
The interaction points identified in the review prompt are real concerns.

**F8 -> F3 (scoring into summary):** The summary shows "Must Fix" and "Should Fix" which derive from action tiers. Scores are used for ranking within tiers but not shown in the summary. This is the correct design -- the summary should stay clean. But the spec should be explicit: "Summary block does NOT include numeric scores."

**F9 -> F2 (ticket detection into spec gate):** This is the most dangerous interaction. If ticket auto-detection finds the wrong ticket (see Q7), the spec gate may fire incorrectly, suppressing a review the developer wanted. The blast radius of a false positive in F9 is the entire review being skipped via F2.

**F6 -> explorers (sufficiency into context):** The spec says max 2 rounds. If the second round still has gaps, the spec does not say what happens. Based on the architecture, explorers presumably run with incomplete context. This is fine -- some context is better than none -- but it should be documented. A log line ("Context collection: 2 rounds, 3 gaps remaining -- proceeding with available context") would prevent confusion.

**F10 -> F8 -> F3 (pipeline ordering):** The spec does not explicitly state the pipeline ordering of repair -> scoring -> summary. From the implementation plan and the existing pipeline in `orchestrate.py`, repair runs immediately after judge output (Step 4b), enrichment/scoring runs in Step 5, and summary is part of the final report. This ordering is implied but should be explicit.

**Evidence:**
- Spec Section "Feature 2: Interaction with Feature 9" -- acknowledges F9 feeds into F2.
- Spec Section "Feature 9: Pipeline Integration" -- Step 3.5 auto-enables spec verification.
- No explicit pipeline ordering diagram for F10/F8/F3.
- No specified behavior for F6 second-round still-insufficient.

**Recommendation:**
1. Add a one-paragraph "Pipeline Ordering" section that explicitly states: F10 (repair) -> F8 (scoring/enrichment) -> F3 (summary). Make this an invariant.
2. For F9 -> F2 interaction: add a safety check -- if ticket detection confidence is "low" (heuristic plan match), do NOT auto-enable spec gating. Only gate on high-confidence sources (explicit `--spec`, explicit `--ticket`, or regex-matched + resolved ticket ID).
3. For F6 incomplete context: add the documented behavior -- "proceed with available context, log remaining gaps." Do not silently swallow the gaps.

---

## Verdict: WARN

The spec is well-structured and the individual features are sound. The implementation plan (3 waves, dependencies respected) is practical. However, three systemic risks could undermine developer adoption if not addressed before implementation.

## Top 3 Recommendations (ranked by impact on developer adoption)

### 1. Harden ticket auto-detection or make it opt-in (F9, Questions 7-9)

The regex `/\b[a-z]{2,4}-[a-z0-9]{4}\b/` will produce false positives on common branch names. False ticket detection cascades through the entire pipeline: wrong compliance checks, incorrect scope analysis, and potentially incorrect spec-gating that suppresses the review entirely (F2). This is the highest-risk interaction in the spec.

**Fix:** Require detected IDs to resolve via `tk query` before treating them as matches. Discard unresolved IDs. Add detection confidence levels. Do not auto-enable spec gating on low-confidence detections.

### 2. Add `--force-review` and soften the spec gate cliff (F2, Question 1)

The 50% threshold with hard skip optimizes for tool efficiency but frustrates developers who want early feedback on partial implementations. A developer tool that refuses to do its job is a developer tool that gets uninstalled.

**Fix:** Add `--force-review`. When the gate fires, still list the implemented vs unimplemented requirements (useful output), and consider running explorers with a prominent "incomplete implementation" banner rather than suppressing them entirely.

### 3. Default scores to hidden; reduce visible signal count (F8, Question 5)

Four quality signals per finding (severity, confidence, action tier, score) creates decision paralysis. Developers need one signal: what do I fix first? Action tiers already provide this. Numeric scores are useful internally for ranking but should not be user-facing by default.

**Fix:** Set `show_scores: false` by default. Always show `score_reason` when scores are visible. Make caps visible in the reason string.
