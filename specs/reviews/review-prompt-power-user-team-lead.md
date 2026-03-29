# Review Prompt: Power User / Engineering Team Lead

**Target spec:** `specs/quality-compliance-context.md` (F2: spec-gated, F3: review summary, F6: context sufficiency, F7: doc injection, F8: scoring, F9: ticket verification, F10: output repair)

**Reviewer persona:** An engineering team lead who uses AI code review tools daily on a team of 8-12 developers. You evaluate tools by: does this help my team ship fewer bugs? Does it slow us down? Do my developers trust it or ignore it? You've tried 3-4 code review tools and dropped most of them because of false positives, noise, or configuration overhead. You're the person who decides whether this tool stays in the team's workflow or gets uninstalled.

---

## Context to Read First

1. Read the spec fully — focus on what each feature does for YOUR team, not how it's built
2. You don't need to read code files — evaluate the spec from a user perspective

## Review Questions

### Would I use this? (Feature-by-Feature)

**1. Spec-gated pass execution (F2).**
You sometimes run reviews on half-finished branches to get early feedback.
- Would you want the tool to say "implementation too incomplete, skipping review"? Or would you prefer it to review what exists and note what's missing?
- If you're at 48% implemented, you get full review. At 52%, you get "come back later." Is this cliff useful or annoying?
- Would your team understand why sometimes they get a full review and sometimes they don't?

**2. Review summary for PR descriptions (F3).**
Your team's PR template has a "Review Notes" section.
- Would you actually paste this summary into your PRs? What format would make you do it?
- The summary shows "Must Fix (2)" and "Should Fix (3)." Is this the right framing for PR descriptions? Would "Blocking (2)" and "Non-blocking (3)" be clearer?
- If the summary says "Must Fix: SQL injection at login.py:42" — does this change how your team handles the PR? (immediate fix before merge, or "we'll address it in the next PR"?)

**3. Context sufficiency feedback loop (F6).**
The tool does a second round of context gathering if the first round missed things.
- Do you care about this as a user? Or is it invisible infrastructure that should "just work"?
- If the tool tells you "Context collection: 2 rounds, found additional callers of your changed function" — does this build trust, or is it noise?

**4. Documentation context injection (F7).**
The tool detects which libraries your code uses and can inject their docs into the review.
- Would your team benefit from this? (e.g., "FastAPI deprecated this pattern in v0.115, here's the migration path")
- Would you enable this? The spec says off-by-default, requires context7 MCP server. How much setup friction is too much?
- The baseline version (just detecting package versions, no doc fetching) — is "this code uses Django 5.1" useful information in a review?

**5. Per-finding numeric scoring (F8).**
Each finding gets a 0-10 score in addition to severity and confidence.
- As a team lead reviewing a report with 8 findings, would scores help you triage? Or would you just look at "Must Fix" vs "Should Fix"?
- Would you use `--min-score 3` to filter low-quality findings? Or would you worry about missing something?
- If you could only have one: severity (critical/high/medium/low), confidence (0.0-1.0), OR score (0-10) — which would you keep?

**6. Ticket & task verification (F9).**
The tool auto-detects which ticket/issue your branch implements and checks implementation completeness.
- If your team uses tk/bd for issue tracking: would automatic "you haven't implemented requirement 3 of 5" be valuable, or do you already know this from your task board?
- The scope creep detection ("cmd/server.go not mentioned in ticket — verify this change is intentional") — would this catch real scope issues, or would it flag every utility file your developers touch?
- The dependency check ("Dependency att-drm1 is still open — this implementation may be premature") — useful warning or annoying false alarm?

**7. Output repair (F10).**
The tool auto-fixes malformed JSON output instead of failing the review.
- Do you care about this? Or should the tool "just work" without you knowing about repairs?
- If the tool frequently repairs output (>10% of reviews), should it tell you? Or is this an internal quality issue?

### Team Adoption

**8. Configuration burden.**
This spec adds 12 config keys. Combined with existing config, that's ~20 total keys.
- When you first set up a tool for your team, how many config keys are you willing to set? 0? 3? 10?
- Which features would you enable immediately, which would you try after a month, and which would you never touch?
- Is there a "recommended config for teams" preset missing from the spec?

**9. Trust calibration.**
Your team currently gets reviews from the tool. After these 7 features ship:
- Which feature would most increase your team's trust in the tool?
- Which feature has the highest risk of decreasing trust (false alarms, confusing output, slowed reviews)?
- After a month of use, how would you measure whether these features helped? (fewer bugs in prod? fewer review rounds? faster merges?)

**10. Missing features.**
From your perspective as a team lead:
- Is there a feature NOT in this spec that would make you more likely to adopt/keep the tool?
- Is there a feature IN this spec that you'd rather not have (adds complexity without value for your team)?
- If you could only ship 3 of these 7 features, which 3 would you pick and why?

---

## Output Format

For each question, provide:
1. **Your honest reaction** as a team lead
2. **What would change your answer** (e.g., "I'd use the summary if it was in checklist format")
3. **Verdict on the feature** — Would ship / Would try / Would skip / Would remove

Conclude with:
- **Overall verdict:** PASS / WARN / FAIL from a team adoption perspective
- **Top 3 features** ranked by value to your team
- **Bottom 2 features** that you'd defer or remove
- **One thing that would make you champion this tool** to your engineering org
