# Review: Quality, Compliance & Context Spec
## Reviewer: Engineering Team Lead (Power User Persona)

**Spec reviewed:** `specs/quality-compliance-context.md` (F2, F3, F6, F7, F8, F9, F10)
**Date:** 2026-03-28

---

### 1. Spec-gated pass execution (F2)

**Honest reaction:** The 50% cliff is going to confuse my team. I run reviews on half-finished branches all the time -- that is literally where I need the most help. If I am at 48% implemented, I get a full review. At 52%, I get a wall that says "come back later." Developers will not understand why the tool sometimes reviews and sometimes does not. They will assume it is broken, file a bug, and then ignore it. Worse: the branches that are most incomplete are the ones where I want the review to say "you have not handled error paths yet" or "this interface is missing a method." A full review with a completeness note is vastly more useful than a gate that blocks the review entirely.

The spec says the gate only fires when `--spec` is provided and there are 3+ must-requirements. That scoping is reasonable -- most of my team's PRs will not hit it. But the moment someone does hit it, they will be frustrated. A "spec verification found major gaps, detailed review deferred" message is not actionable feedback. What gaps? What should I work on next?

**What would change my answer:** Replace the hard gate with a "completeness-first" mode: always run the review, but when >50% of requirements are unimplemented, front-load the spec completeness report and add a clear banner: "Most requirements are unimplemented -- findings below apply to the code that exists, but expect significant changes." Give me the review AND the completeness data. Do not make me choose.

**Verdict:** Would try -- but only if the gate becomes a warning, not a blocker. As a hard gate, Would skip.

---

### 2. Review summary for PR descriptions (F3)

**Honest reaction:** This is the feature I would use on day one. My team's PR template already has a "Review Notes" section, and right now everyone writes "LGTM" or "see comments." A structured summary with verdict + must-fix + should-fix items is exactly what I would paste.

The format is almost right. "Must Fix" and "Should Fix" are clear enough -- I slightly prefer "Blocking" and "Non-blocking" because that is the language my team uses in review discussions, but this is minor. The one-line-per-finding format with `file:line -- description` is perfect for PR descriptions. The 10-line cap is smart.

If the summary says "Must Fix: SQL injection at login.py:42" -- yes, that changes how we handle the PR. It becomes a blocker. My team knows that if the automated review says "Must Fix," you fix it before requesting human review. That is the calibration I want.

The spec status line ("8/10 requirements implemented") is useful context. I would want it even without `--spec` if ticket verification (F9) is active.

**What would change my answer:** Add a clipboard-ready markdown block (triple backtick wrapped) so I can literally copy-paste it. Maybe a `--summary-only` flag that just prints the summary without the full report, for use in CI scripts that post to PRs.

**Verdict:** Would ship. This is the highest-value feature in the spec for daily team workflow.

---

### 3. Context sufficiency feedback loop (F6)

**Honest reaction:** I do not care about this as a user, and that is the correct outcome. This is infrastructure that should be invisible. If the tool is smarter about finding callers of my changed function, great -- I want to see the result (fewer false positives, more cross-file bugs caught), not the mechanism.

If the tool tells me "Context collection: 2 rounds, found additional callers" -- that is noise. I do not need to know how many rounds of context gathering happened. I need the findings to be accurate. This is like my database telling me it did 3 index lookups. Do not care. Show me the data.

The 30% trigger rate from Kodus data is interesting from an engineering perspective, but as a user it should be completely transparent. The max-2-rounds cap is the right call -- I do not want my review taking 2x longer because the tool is spiraling on context collection.

**What would change my answer:** Keep this entirely invisible. No user-facing messages about sufficiency rounds. If you want to expose it, put it in a `--verbose` or `--debug` mode for me when I am diagnosing slow reviews. The config keys (`sufficiency_check`, `max_rounds`) should exist but never appear in a "getting started" guide.

**Verdict:** Would ship -- as invisible infrastructure. Would skip if it adds visible noise to the review output.

---

### 4. Documentation context injection (F7)

**Honest reaction:** The full version (detecting deprecated APIs, breaking changes) would be valuable maybe once a month for my team. We hit "this FastAPI pattern was deprecated" issues roughly once per quarter, and they are painful when they slip through. But the setup friction matters. Requiring a context7 MCP server is a non-starter for initial adoption. I am not going to ask 8-12 developers to configure an MCP server before they can use a code review tool.

The baseline version (detecting package names and versions) is... fine. "This code uses Django 5.1" is marginally useful context. It might help the model avoid suggesting APIs that do not exist in that version. But I would never enable a feature just for that.

The real question is: does this slow down my reviews? If fetching docs adds 10+ seconds per review, the cost-benefit does not work for a once-a-quarter win.

**What would change my answer:** Make the baseline (package detection) zero-config and always-on. It costs nothing and helps the model. For the full doc injection: make it a single CLI flag (`--with-docs`) that works without any pre-configuration if context7 happens to be available. No config file, no setup. If context7 is not available, silently skip. Opportunistic, not required.

**Verdict:** Would try -- baseline only. Full doc injection is Would skip until the setup friction drops to zero.

---

### 5. Per-finding numeric scoring (F8)

**Honest reaction:** I have mixed feelings. As a team lead triaging 8 findings, I look at severity first: "Must Fix" vs "Should Fix" is my primary sort. Within those buckets, a score might help me prioritize, but honestly, I am going to read all the "Must Fix" items regardless. The score adds the most value at the margins -- should I look at this score-4 finding or skip it?

The `--min-score 3` filter is interesting. I would absolutely use this if I trusted the scoring calibration. But would I trust it after a month? That depends entirely on whether score-2 findings are consistently noise and score-7+ findings are consistently real. If I ever filter out a real bug because it scored a 2, I will never use the filter again.

The explicit caps from PR-Agent are the best part of this feature. Capping "verify/ensure" suggestions at 6 and documentation suggestions at 2 is exactly the kind of opinionated filtering that reduces noise. These caps are worth more than the numeric scores themselves.

If I could only have one: severity. It maps directly to my workflow: "fix before merge" vs "fix when you can" vs "consider this." The numeric score is refinement on top of that.

**What would change my answer:** Show scores but default `min_score` to 0 (keep all). Let teams raise it over time as they calibrate trust. The explicit caps should be non-configurable defaults -- they represent the tool's opinion about what matters, and that opinion is correct.

**Verdict:** Would ship -- the caps alone justify this. The numeric score is a nice-to-have.

---

### 6. Ticket & task verification (F9)

**Honest reaction:** This is the feature where I got excited and then got worried. The auto-detection of tickets from branch names and commit messages is slick -- my team already follows `feat/<ticket-id>-description` conventions, so this would "just work." The completeness check ("you have not implemented requirement 3 of 5") would be genuinely useful. Right now I do this manually by re-reading the ticket before reviewing the PR.

But the scope creep detection makes me nervous. "cmd/server.go not mentioned in ticket -- verify this change is intentional" will fire on virtually every PR. Developers touch utility files, config files, shared modules, test helpers. These are not scope creep. They are how code works. If every review has 3-4 "unexpected file" warnings, my team will learn to ignore the entire scope section, including the one time it catches a real issue.

The dependency check ("att-drm1 is still open -- this implementation may be premature") is a good idea if it works. In practice, ticket dependencies in my team's tracker are maybe 60% accurate. So 40% of the time, this warning is based on stale metadata. That is a trust-killer.

**What would change my answer:** Scope creep detection needs a much smarter heuristic. At minimum: exclude test files, config files, and files that are direct imports of changed files. Better yet: only flag files that are in a completely different module/package from the ticket's mentioned files. The dependency check should be off by default and require explicit opt-in, because it depends on tracker hygiene that most teams do not have.

**Verdict:** Would try -- completeness check is valuable. Scope creep and dependency checks are Would skip until the false positive rate is demonstrably low.

---

### 7. Output repair (F10)

**Honest reaction:** I do not care about this and I should not have to. The tool should produce valid output. If it does not, fix it silently. The `"repaired": true` flag is fine for internal telemetry, but do not show it to me. I do not want to see "Repaired: extracted from code block" in my review output. That is like my IDE telling me it recovered from a segfault. Great, but that should not have happened.

If the tool is repairing >10% of outputs, that is a quality problem the maintainers need to address, not something to surface to users. If it causes a visible delay, that is a problem.

**What would change my answer:** Ship it, keep it silent, use the `repaired` flag for internal quality metrics. If repair rate exceeds 10%, fix the prompts. Users should never know this feature exists.

**Verdict:** Would ship -- as invisible infrastructure. This is a reliability feature, not a user feature.

---

### 8. Configuration burden

**Honest reaction:** 12 new config keys on top of ~20 existing ones is too many. When I set up a tool for my team, I am willing to set 0-3 keys. Beyond that, I start a spreadsheet to track what each key does, and that means I have already spent more time configuring the tool than it has saved me.

Features I would enable immediately: F3 (summary), F8 (scoring with caps), F10 (repair -- should be default). These are zero-risk improvements.

Features I would try after a month of baseline trust: F9 (ticket verification, completeness only), F6 (sufficiency check).

Features I would probably never touch: F7 full (doc injection with context7), F2 (spec gating as a hard gate), F9 scope creep detection.

What is missing: a "recommended config for teams" preset. Something like:
```yaml
preset: team
# Enables: summary, scoring (min_score: 0), output repair, ticket completeness
# Disables: spec gating, scope creep, doc injection, dependency checks
```

That is what I would paste into my team's repo on day one. Let me opt into complexity, do not start me there.

**What would change my answer:** Ship a `preset: team` config that enables the safe, high-value features. Make every other feature opt-in with a clear one-line description of what it does and what it costs (latency, false positive risk).

**Verdict:** WARN -- the feature set is good, but the configuration surface area needs a defaults-first approach.

---

### 9. Trust calibration

**Most trust-building feature:** F3 (Review Summary). It is the most visible, most frequently used, and most directly actionable feature. When my team sees a structured summary with the right findings in the right categories, they trust the tool more. Every accurate "Must Fix" that catches a real bug before human review builds cumulative trust.

**Highest risk of decreasing trust:** F9 scope creep detection. False alarms on "unexpected files" will train developers to ignore the tool's warnings. This is the worst possible outcome -- you do not just lose the value of scope detection, you lose the credibility halo that makes developers pay attention to real findings. One month of noisy scope warnings can undo six months of earned trust.

**How I would measure success after a month:**
- Primary: fewer review round-trips (PRs merged after fewer human review cycles)
- Secondary: fewer bugs in prod that were in code the tool reviewed
- Proxy: developer survey -- "do you read the AI review before requesting human review?" If yes rate goes up, the tool is working. If it stays flat or drops, something is wrong.

**Verdict:** WARN -- high upside if scope creep is either fixed or removed. High downside if it ships noisy.

---

### 10. Missing features

**Feature NOT in this spec that I want:** Incremental reviews. When I push a fix for a finding, I want the next review to say "Previously flagged: SQL injection at login.py:42 -- RESOLVED" or "Previously flagged: race condition -- STILL PRESENT." Right now, every review is stateless. My developers push a fix, re-run the review, and have to mentally diff the old and new reports to see if they actually fixed the issue. This is the single biggest friction point in my team's workflow.

**Feature IN this spec I would rather not have:** F7 full doc injection (context7). The setup friction is high, the value is episodic, and it adds latency. The baseline (package detection) is fine. The full version is solving a problem that happens once a quarter with infrastructure that takes an hour to set up and slows every review.

**If I could only ship 3 of these 7:**
1. **F3 (Review Summary)** -- highest daily value, zero risk, immediate adoption
2. **F10 (Output Repair)** -- invisible reliability improvement, prevents tool failures that destroy trust
3. **F8 (Per-finding Scoring)** -- the explicit caps alone reduce noise significantly, scores enable future filtering

Why these three: they are all in Wave 1, have no dependencies, and improve the experience without any risk of false positives or configuration burden. Ship the safe wins first, earn trust, then layer on compliance features.

---

## Overall Verdict: PASS (with conditions)

This spec has the right instincts. The features target real problems -- noisy output, missing context, incomplete reviews, brittle output parsing. The research grounding (PR-Agent scoring, Kodus sufficiency data) is solid. The wave-based implementation plan is smart.

But there is a tension between "features that help" and "features that annoy." The spec needs a stronger opinion about defaults. Too many features are on-by-default or ambiguously scoped. The safe path is: default everything off except F3, F8, F10, and let teams opt in as they build trust.

### Top 3 Features (ranked by value to my team)

1. **F3: Review Summary** -- paste-able, actionable, visible. This is the feature that turns the tool from "that AI thing that runs in CI" to "the first thing I check before reviewing a PR."
2. **F10: Output Repair** -- invisible but critical. A tool that fails 5% of the time gets uninstalled. A tool that silently recovers earns trust by being reliable.
3. **F8: Per-finding Scoring** -- the explicit caps (documentation max 2, verify/ensure max 6) are the real value. They encode the opinion "not all findings are equal" into the tool's behavior, which reduces noise without requiring user configuration.

### Bottom 2 Features (defer or remove)

1. **F7: Full Doc Injection (context7)** -- high setup friction, episodic value, adds latency. Ship the baseline (package detection) as part of normal context. Defer the full version until context7 is ubiquitous.
2. **F9: Scope Creep Detection** -- the false positive rate on "unexpected files" will be unacceptable for most teams. Either invest heavily in smarter heuristics (exclude tests, configs, transitive imports) or defer until you have data on what "unexpected" actually means in real codebases.

### One thing that would make me champion this tool

**Incremental reviews with finding persistence.** If I could push a fix and see "2/3 must-fix items resolved, 1 remaining" instead of a brand-new report with no memory, I would present this tool at our next engineering all-hands. That is the feature that makes developers feel like the tool is a collaborator, not a one-shot judge. It closes the feedback loop. Every other code review tool I have tried treats each review as independent. The one that tracks findings across pushes wins.
