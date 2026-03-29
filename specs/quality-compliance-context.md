# Spec: Quality, Compliance & Context (F2 + F3 + F6 + F7 + F8 + F9 + F10)

**Status:** Draft — extracted from `docs/plan-verification-pipeline.md`
**Author:** Research session 2026-03-28
**Depends on:** orchestrate.py (existing), SKILL.md (existing)
**Source plan:** `docs/plan-verification-pipeline.md` Features 2, 3, 6, 7, 8, 9, 10
**Related:** Spec A (expert selection), Verification Architecture spec (F0/F1/F5)

## Problem

After the verification architecture improves finding precision, several quality-of-life and compliance features are needed: the review report needs a copy-pasteable summary, findings need finer-grained scoring, the context gathering phase needs a sufficiency check, documentation context should be available to explorers, local planning artifacts should be auto-detected for compliance checks, and malformed model output should be auto-repaired.

## Features Overview

| Feature | Effort | Dependencies | Theme |
|---------|--------|-------------|-------|
| F2: Spec-gated pass execution | Small | `--spec` flag (existing) | Compliance |
| F3: Review summary for PR descriptions | Small | Judge output (existing) | Quality |
| F6: Context sufficiency feedback loop | Medium | Cross-file planner (context enrichment F12) | Context |
| F7: Documentation context injection | Small-Medium | `code_intel.py imports` (context enrichment F0c), context7 MCP | Context |
| F8: Per-finding numeric scoring | Small | Judge prompt (existing) | Quality |
| F9: Ticket & task verification | Medium-Large | `tk`/`bd` CLI (optional) | Compliance |
| F10: Output repair | Small | `validate_output.sh` (existing) | Quality |

---

## Feature 2: Spec-Gated Pass Execution

**Goal:** When `--spec` is provided and most requirements are unimplemented, skip detailed code quality passes — they'll report issues in code that needs rewriting anyway.

### Logic

```
If --spec provided:
  Run spec-verification explorer FIRST (before others)
  Read spec_requirements from output

  If >50% of "must" requirements are "not_implemented":
    Skip remaining explorers
    Report: "Spec verification found major implementation gaps.
             Detailed code review deferred until implementation catches up."
    Verdict: FAIL (spec gaps)

  Else:
    Run remaining explorers normally
    Include spec results in judge input
```

### Activation

Only when `--spec` provided AND spec has ≥3 extractable "must" requirements. Fewer requirements → not enough signal to gate on.

### SKILL.md Changes

Modify Step 4a to support sequential spec-first execution when `--spec` is active:
1. Launch spec-verification explorer (sequential, before other explorers)
2. Read its output, count `not_implemented` among `must` requirements
3. If gap ratio > 50%: skip remaining explorers, go directly to report with FAIL verdict
4. Otherwise: launch remaining explorers in parallel as normal

### Interaction with Feature 9

Auto-detected plan context (F9) serves as the spec source when no `--spec` is provided. `--spec` takes precedence if explicitly given. Same gating threshold applies to ticket-derived requirements.

---

## Feature 3: Review Summary for PR Descriptions

**Goal:** Generate a concise, copy-pasteable review summary suitable for inclusion in PR descriptions or review comments. This is NOT inline PR comments (the skill does not post to GitHub) — it's a formatted summary block that the user can paste wherever they want.

### Output Format

```markdown
## Review Summary

**Verdict:** WARN — 2 issues to address before merge

**Must Fix (2):**
- `src/auth/login.py:42` — SQL injection via string formatting in user lookup
- `src/api/orders.py:78` — Race condition: concurrent updates can double-charge

**Should Fix (3):**
- `src/utils/cache.py:23` — Cache invalidation missing after role change
- `src/models/user.py:156` — Unused `admin_override` parameter (YAGNI)
- `tests/test_auth.py:34` — Test mocks database, cannot catch schema drift

**Spec:** 8/10 requirements implemented, 1 partial, 1 not started
```

### Implementation

- One line per finding: `file:line` — one-sentence summary
- Group by action tier
- Include spec status if `--spec` was used
- Cap at 10 lines (if more findings exist, add "See full report for N additional findings") (link to full report for more)
- Add "Summary Block" section to `references/report-template.md`
- Instruct judge to produce summary block in verdict output

---

## Feature 6: Context Sufficiency Feedback Loop

**Goal:** After cross-file context collection (context enrichment F12), evaluate whether collected context is sufficient. If gaps remain, generate additional queries and do a second collection round. Kodus-AI's production data shows the sufficiency check triggers additional queries in ~30% of reviews.

### Architecture

```
Step 2m: Cross-File Context Planning (context enrichment F12)
    ├── Phase 1: Planner generates queries from diff
    ├── Phase 2: Execute queries via Grep
    ▼
Step 2m.5: Context Sufficiency Check (NEW)
    ├── Send: original queries + results + collected snippets
    ├── LLM evaluates: sufficient or insufficient?
    ├── If sufficient: proceed to context assembly
    └── If insufficient:
        ├── LLM returns up to 5 additional queries + gap descriptions
        ├── Execute additional queries
        ├── Merge new results with existing context
        └── Proceed (max 2 rounds total — no infinite loops)
```

### Sufficiency Criteria

**Sufficient** when:
1. All high-risk queries found at least some results
2. Symmetric counterparts (create/validate, encode/decode) are covered
3. Direct consumers/callers of changed public APIs are present

**Insufficient** when:
1. High-risk queries returned nothing for public APIs/exported types
2. Symmetric counterpart identified but no consumer/verifier found
3. Test files changed but no implementation found (or vice versa)

### Prompt

New file: `prompts/reviewer-context-sufficiency.md`

```markdown
You are evaluating whether the cross-file context collected for a code review
is sufficient to detect cross-file bugs.

You receive:
- The original search queries and whether each found results
- A summary of collected code snippets (file paths, symbols, rationale)
- The changed file names and a diff summary

Evaluate:
1. Did all high-risk queries find results? If a high-risk query found nothing,
   that's a gap — the relevant code may exist under a different name.
2. Are symmetric counterparts covered? If the diff changes a create/encode/write
   operation, is the corresponding validate/decode/read operation in the context?
3. Are consumers of changed public APIs present? If a function signature changed,
   are callers in the context?

If sufficient: { "sufficient": true }

If insufficient:
{
  "sufficient": false,
  "gaps": ["verify_token() not found — symmetric counterpart of changed create_token()"],
  "additional_queries": [
    { "pattern": "\\bverify\\b.*\\btoken\\b", "rationale": "Find token verification logic", "risk_level": "high" }
  ]
}

Max 5 additional queries. Use word-boundary ripgrep patterns only.
```

### Activation

- Only when cross-file planner (context enrichment F12) is active
- Only when ≥1 query found zero results
- Max 2 rounds total (initial + one sufficiency round)
- Configurable:
```yaml
cross_file:
  sufficiency_check: true
  max_rounds: 2
```

---

## Feature 7: Documentation Context Injection

**Goal:** Discover which libraries/frameworks are used in changed code and inject relevant documentation into explorer context. Helps catch deprecated API usage or breaking changes the model's training data doesn't know.

Inspired by Kodus-AI's doc pipeline: package discovery → LLM generates doc queries → external search → cached results injected into context.

### Architecture

```
Step 2n: Documentation Context (NEW)
    ├── Phase 1: Discover packages
    │   ├── Read manifests (package.json, requirements.txt, go.mod, etc.)
    │   ├── Extract dependency names and versions
    │   └── Filter to packages actually imported by changed files
    ├── Phase 2: Generate documentation queries
    │   ├── For each relevant package: what documentation helps?
    │   ├── Focus: API contracts, deprecation, breaking changes, security advisories
    │   └── Max 5 queries (token budget)
    ├── Phase 3: Fetch documentation
    │   ├── Use context7 MCP server (resolve-library-id → query-docs)
    │   ├── Or web search as fallback
    │   └── Format results as context snippets
    └── Include in context packet
```

### Activation

Off by default. Enabled via:
```yaml
documentation:
  enabled: true
  provider: "context7"    # context7 | web_search | none
```

Or CLI: `/codereview --with-docs`

### Minimum Viable Version (no external service)

Even without doc fetching:
1. Detect which packages are used (from manifests + imports)
2. Include package names and versions in context packet
3. Explorers can flag: "this code uses library X v2.3 — check if this API was deprecated"

### Context7 Integration

When context7 MCP is available:
1. `resolve-library-id` to map package name → context7 library ID
2. `query-docs` with focused queries ("FastAPI dependency injection changes in v0.115")
3. Inject relevant doc snippets into context packet at P8 tier (~1,000 tokens max)

---

## Feature 8: Per-Finding Numeric Scoring

**Goal:** Add 0-10 score to each finding for threshold-based filtering and finer ranking within action tiers.

Inspired by PR-Agent's self-reflection scoring with calibrated bands and explicit caps.

### Scoring Bands

| Score | Meaning | Examples |
|-------|---------|---------|
| 9-10 | Confirmed defect with evidence and clear failure mode | Verified SQL injection, proven race condition |
| 7-8 | Likely defect, strong evidence, some uncertainty | Missing error check on fallible call |
| 5-6 | Plausible issue, moderate evidence | Missing test for new public API |
| 3-4 | Minor concern, weak evidence or low impact | Suboptimal algorithm choice |
| 1-2 | Speculative, style preference | "Consider using X instead of Y" |
| 0 | Wrong — finding is clearly invalid | Targets unchanged code |

### Explicit Caps (from PR-Agent)

- "Verify/ensure" suggestions (no concrete defect): max 6
- Error handling additions (defensive, not fixing bug): max 7
- Identical to deterministic tool result: max 5
- Documentation/comment suggestions: max 2

### Integration

- Judge assigns score in Pass 2 (synthesis) alongside severity calibration
- Finding schema gains `score` (int) and `score_reason` (string)
- `enrich-findings.py` gains `--min-score` flag:
  ```bash
  python3 scripts/enrich-findings.py \
    --judge-findings /tmp/judge.json \
    --scan-findings /tmp/scans.json \
    --min-score 3 \          # drop findings scoring below 3
    > /tmp/enriched.json
  ```
- Within action tiers, findings sorted by score (descending), tiebreaker: `severity_weight * confidence`

```yaml
scoring:
  min_score: 0      # drop findings below this (0 = keep all)
  show_scores: true
```

---

## Feature 9: Ticket & Task Verification

**Goal:** Auto-detect local planning artifacts (tk tickets, bd beads, plan files) that describe what the current branch should implement. Verify implementation against them.

### Planning Artifact Sources

| Source | Storage | Detection | Read via | Structured fields |
|--------|---------|-----------|----------|-------------------|
| `tk` tickets | `.tickets/*.md` | `.tickets/` dir exists | `tk show <id>`, `tk query` (JSON) | id, status, deps, acceptance, description, tags, parent, type, priority |
| `bd` beads | `.beads/issues.jsonl` | `.beads/` dir exists | `bd show <id>` | id, status, deps, description |
| Plan files | `docs/plan-*.md` | glob | Read directly | Features with goals, files, acceptance criteria |

### Auto-Detection Logic

New script: `scripts/detect-plan-context.sh`

1. Parse commit messages on current branch for ticket/bead IDs (regex: `/\b[a-z]{2,4}-[a-z0-9]{4}\b/`)
2. Parse branch name for IDs (`feat/att-0ogy-claim-store` → `att-0ogy`)
3. If IDs found + `.tickets/` exists: `tk query` for matched IDs (include parent + deps)
4. If IDs found + `.beads/` exists: `bd show` for matched IDs
5. If no IDs but `docs/plan-*.md` exists: heuristic match branch name against feature titles
6. Accept explicit overrides: `--ticket <id>`, `--bead <id>`, `--plan <file>[#N]`

### Detection Output Schema

`detect-plan-context.sh` produces JSON to stdout — this is the contract between detection and all downstream consumers:

```json
{
  "source": "tk",
  "tickets": [
    {
      "id": "att-0ogy",
      "title": "Add ClaimableStore interface + engine claim methods",
      "status": "in_progress",
      "description": "Add ClaimableStore interface to state/types.go...",
      "acceptance_criteria": "...",
      "files_mentioned": ["state/types.go", "engine.go"],
      "deps": ["att-rbg7", "att-drm1"],
      "dep_statuses": { "att-rbg7": "closed", "att-drm1": "closed" },
      "parent": "att-jndm",
      "tags": ["claim-wave-3"],
      "type": "feature",
      "priority": "high"
    }
  ]
}
```

When no tickets/beads are found but a plan file matches, the `source` is `"plan"` and `tickets` is replaced by a `plan` object with extracted feature goals and acceptance criteria.

### Verification Checks

**Completeness:**
- Were all files mentioned in ticket/plan modified in diff?
- Are there "files to create" that don't exist yet?
- For each acceptance criterion: code in diff addresses it?
- Required tests present?

**Scope:**
- Changed files NOT mentioned in ticket/plan? (potential scope creep)
- Does the diff touch areas unrelated to the ticket's description?
- If ticket has tags (e.g., `claim-wave-3`), do changes stay within scope?

**Dependencies:**
- All `deps` in `closed` status? If not: warn premature implementation
- Parent epic sibling tickets resolved?

**Status:**
- Is the ticket in `in_progress` or appropriate status for review?
- If ticket is already `closed`: warn that review is on already-completed work

### Output Schema

```json
{
  "plan_context": { "source": "tk", "ticket_id": "att-0ogy", "ticket_title": "..." },
  "spec_requirements": [
    { "requirement": "Add ClaimableStore interface", "source": "ticket:att-0ogy",
      "status": "implemented", "evidence": "state/types.go:15" }
  ],
  "scope_analysis": {
    "expected_files": ["state/types.go", "engine.go"],
    "unexpected_files": ["cmd/server.go"],
    "missing_files": []
  },
  "dependency_status": { "all_resolved": true, "deps": [...] }
}
```

### Pipeline Integration

- **Step 1 (parse arguments):** `detect-plan-context.sh` runs alongside diff generation. Output stored as context.
- **Step 2 (gather context):** Plan context included in context packet for all explorers.
- **Step 3.5 (expert selection):** Spec-verification pass auto-enabled when plan context detected (no `--spec` flag needed).
- **Step 4a (explorers):** The spec-verification explorer receives full plan context and produces `spec_requirements` + `scope_analysis`. Other explorers receive a one-line summary ("This branch implements ticket att-0ogy: Add ClaimableStore interface...") for awareness but don't perform compliance checks.
- **Feature 2 (spec-gated):** Auto-detected plan context serves as spec source when no `--spec`. `--spec` takes precedence if explicitly provided. The skill distinguishes source: `source: "ticket:att-0ogy"` vs `source: "spec:docs/spec.md"`.
- **Judge:** Receives plan context, includes compliance summary in verdict reasoning.

### Activation

On by default when `.tickets/` or `.beads/` exist. Disable via `/codereview --no-plan-context`. Configurable:
```yaml
plan_context:
  auto_detect: true
  source: "auto"       # auto | tk | bd | plan | none
  verify_deps: true
  scope_analysis: true
```

---

## Feature 10: Output Repair

**Goal:** Add JSON repair strategies to `validate_output.sh` so minor formatting issues don't trigger hard failure.

Inspired by PR-Agent's `try_fix_yaml` (7+ fallback strategies, ~80% recovery rate).

### Repair Strategies (in order)

1. **Extract from code block:** ```` ```json ... ``` ```` → extract content
2. **Strip trailing content:** Remove text after closing `}` or `]`
3. **Fix trailing commas:** Remove commas before `}` or `]`
4. **Fix single quotes:** `'key': 'value'` → `"key": "value"`
5. **Fix unquoted keys:** `key:` → `"key":`
6. **Truncation recovery:** Close open arrays/objects, add `"truncated": true`

### Implementation

Add `repair_json()` to `validate_output.sh` before existing validation checks. Called after judge output (Step 4b) and after enrichment (Step 5).

```bash
repair_json() {
  local input="$1"
  local repaired

  # Strategy 1: extract from code block
  repaired=$(sed -n '/^```json/,/^```/{ /^```/d; p; }' "$input")
  if [ -n "$repaired" ] && echo "$repaired" | jq . >/dev/null 2>&1; then
    echo "$repaired" > "$input"
    echo "Repaired: extracted from code block" >&2
    return 0
  fi

  # Strategy 2-6: progressive fixes on raw content
  # Each strategy modifies the content, tries jq validation, writes back on success
  # ... (trailing content, trailing commas, single quotes, unquoted keys, truncation)
}
```

Each strategy tries `jq` validation after the fix. If repair succeeds, write back and continue. If all 6 strategies fail, existing error path applies (fallback to manual).

Log repairs: `"Repaired: extracted from code block"` or `"Repaired: fixed 2 trailing commas"`.

**Repaired flag:** When any repair strategy succeeds, add `"repaired": true` to the JSON envelope so downstream consumers know the output was auto-fixed. This enables quality tracking — a high `repaired` rate may indicate a prompt issue.

---

## Implementation Plan

### Wave 1: Quick Wins (F3 + F8 + F10)

No dependencies on other specs. Pure prompt + script changes.

1. F10: Add `repair_json()` to `validate_output.sh`
2. F3: Add summary block template to `references/report-template.md`, instruct judge
3. F8: Add scoring bands + caps to judge prompt, add `score`/`score_reason` to schema, add `--min-score` to `enrich-findings.py`
4. Tests: JSON repair strategies, summary block format, scoring bands

### Wave 2: Spec Compliance (F2 + F9)

F9 depends on F2's spec-gating infrastructure.

1. F2: Modify Step 4a in SKILL.md for spec-first sequential execution
2. F9: Write `scripts/detect-plan-context.sh` for ticket/bead/plan detection
3. F9: Extend `reviewer-spec-verification-pass.md` to consume ticket context
4. F9: Add `plan_context`, `scope_analysis`, `dependency_status` to findings schema
5. Tests: spec-gating threshold, ticket detection, scope analysis, dep checks

### Wave 3: Context Enhancement (F6 + F7)

Depends on context enrichment plan features (cross-file planner, code_intel imports).

1. F6: Write `prompts/reviewer-context-sufficiency.md`
2. F6: Add Step 2m.5 to SKILL.md (sufficiency check between context collection and assembly)
3. F7 (baseline): Add package detection to Step 2 (names + versions in context)
4. F7 (full): Add context7 MCP integration for doc fetching
5. Tests: sufficiency evaluation, additional query generation, doc injection

---

## Acceptance Criteria

### Wave 1 (Quick Wins)
- [ ] F10: Malformed JSON with code block fences → repaired successfully
- [ ] F10: JSON with trailing commas → repaired
- [ ] F10: Truncated JSON → closed and marked `truncated: true`
- [ ] F10: Repair logged to stderr
- [ ] F3: Report contains summary block with verdict, must-fix, should-fix
- [ ] F3: Summary capped at 10 lines
- [ ] F3: Spec status included when `--spec` was used
- [ ] F8: Each finding has `score` (0-10) and `score_reason`
- [ ] F8: Scoring caps enforced (documentation suggestion ≤ 2)
- [ ] F8: `--min-score 3` drops findings scoring below 3

### Wave 2 (Compliance)
- [ ] F2: >50% must-requirements not_implemented → skip remaining explorers, FAIL verdict
- [ ] F2: ≤50% gaps → run all explorers normally
- [ ] F2: Requires ≥3 must-requirements to gate
- [ ] F9: Ticket ID detected from branch name
- [ ] F9: Ticket ID detected from commit messages
- [ ] F9: `tk query` returns structured ticket data
- [ ] F9: Completeness check: missing files flagged
- [ ] F9: Scope check: unexpected files flagged
- [ ] F9: Dependency check: unresolved deps warned
- [ ] F9: Auto-detected plan context enables spec-verification pass

### Wave 3 (Context)
- [ ] F6: Sufficient context → no additional queries
- [ ] F6: Insufficient (query found nothing) → up to 5 additional queries generated
- [ ] F6: Max 2 rounds (no infinite loop)
- [ ] F7 baseline: Package names + versions in context packet
- [ ] F7 full: context7 MCP resolves library → doc snippets injected

---

## Files to Create

| File | Feature |
|------|---------|
| `prompts/reviewer-context-sufficiency.md` | F6 |
| `scripts/detect-plan-context.sh` | F9 |
| `prompts/reviewer-plan-compliance.md` | F9 |

## Files to Modify

| File | Features |
|------|----------|
| `SKILL.md` | F2, F6, F7, F9 |
| `prompts/reviewer-judge.md` | F3, F8 |
| `prompts/reviewer-spec-verification-pass.md` | F9 |
| `scripts/enrich-findings.py` | F8 |
| `scripts/validate_output.sh` | F10 |
| `references/report-template.md` | F3 |
| `references/findings-schema.json` | F8, F9 |
| `references/design.md` | F2, F3, F6, F7, F8, F9, F10 |
| `references/acceptance-criteria.md` | F2, F6, F9 |

---

## Deferred and Superseded Features

| Feature | Status | Reason |
|---------|--------|--------|
| F4: Multi-model spot-check | Deferred | Needs empirical data on same-family model diversity |
| F11: Adaptive expert panel | Superseded by Spec A | Full design in `specs/adaptive-expert-selection.md` |
