---
name: codereview
description: 'AI-powered multi-pass code review for PRs and local diffs. Triggers: "code review", "review PR", "review this diff", "review my changes", "review this PR", "/codereview".'
---

# Code Review Skill

> **Purpose:** Comprehensive AI code review using deterministic tools and parallel specialized passes with explorer sub-agents. Finds everything worth fixing — code agents are fast, so surface all actionable issues.

**YOU MUST EXECUTE THIS WORKFLOW. Do not just describe it.**

## Quick Start

```bash
/codereview                             # review staged changes or HEAD~1
/codereview 42                          # review PR #42
/codereview --base main                 # all commits on branch since main
/codereview --range abc123..def456      # specific commit range
/codereview --spec docs/plan.md         # review against a spec/plan
/codereview src/auth/                   # review changes in specific path
/codereview --base main --spec plan.md  # branch review with spec check
```

---

## Execution Steps

### Step 1: Determine Review Target

Parse the argument to determine what to review:

**If PR number provided** (digits only):
```bash
gh pr diff <number> > /tmp/codereview-diff.patch
gh pr view <number> --json title,body,files --jq '{title: .title, body: .body, files: [.files[].path]}' 2>/dev/null
```

**If `--base <branch>` provided** (branch review — all commits since divergence):
```bash
# Three-dot diff: everything committed on this branch since it diverged from <branch>
MERGE_BASE=$(git merge-base <branch> HEAD)
git diff $MERGE_BASE..HEAD
# SCOPE=branch, BASE_REF=<branch>, HEAD_REF=HEAD
```
This is the primary mode for **wave-end reviews** — review all work done on a feature branch.

**If `--range <from>..<to>` provided** (specific commit range):
```bash
git diff <from>..<to>
# SCOPE=range, BASE_REF=<from>, HEAD_REF=<to>
```
Use this for reviewing a specific wave of commits (e.g., "review the last 5 commits").

**If path provided:**
```bash
git diff HEAD -- <path>
# SCOPE=path
```

**If no argument (auto-detect):**
```bash
# Try staged changes first
STAGED=$(git diff --cached --stat 2>/dev/null)
if [ -n "$STAGED" ]; then
  git diff --cached
  # SCOPE=staged
else
  # Fall back to last commit
  git diff HEAD~1
  # SCOPE=commit
fi
```

**Flags can be combined:** `/codereview --spec docs/plan.md --base main` applies both `--base` (for the diff target) and `--spec` (for requirements checking). Parse all flags before selecting the diff mode.

**If `--spec <path>` provided:** Read the spec/plan file. It will be included in the context packet so AI passes can check implementation completeness.

**Pre-flight check:** If the diff is empty, tell the user "No changes found to review" and stop.

**Store results for later steps:**
- `DIFF` — the full diff content
- `CHANGED_FILES` — list of changed file paths (extracted from the diff via `--stat` or `--name-only`)
- `SCOPE` — one of `branch`, `range`, `staged`, `commit`, `pr`, `path`
- `BASE_REF` / `HEAD_REF` — the base and head references

These variables are referenced throughout subsequent steps.

### Step 2: Gather Context (Agentic Exploration)

Before reviewing, understand the surrounding code. This is critical for catching integration bugs.

**2a. Identify scope from the diff:**
- Extract changed file paths, function names, class names
- Note added/removed imports and dependencies

**2b. Explore surrounding code using tools:**

Use `Grep`, `Glob`, and `Read` to examine:
1. **Callers** of changed functions — who calls this code?
2. **Callees** — what does the changed code depend on?
3. **Related test files** — do tests exist for the changed code?
4. **Type definitions and interfaces** referenced in the diff
5. **Configuration files** that might be affected

**2c. Dead code / YAGNI check:**

For each changed or newly added function, check if it's actually called:
```bash
# For each new/modified function name, grep for callers
# If a function has zero callers outside its own file and tests, flag it
```
Include dead-code findings in the context packet so the review judge can flag YAGNI issues rather than reviewing unused code in detail.

**2d. Complexity analysis:**

Run cyclomatic complexity on changed files (best-effort). Check file extensions first to avoid running language-specific tools on irrelevant files:

```bash
# Filter CHANGED_FILES by language before running tools
PY_FILES=$(echo "$CHANGED_FILES" | grep -E '\.py$' || true)
GO_FILES=$(echo "$CHANGED_FILES" | grep -E '\.go$' || true)

# Python — only if there are .py files in the diff
if [ -n "$PY_FILES" ] && command -v radon &>/dev/null; then
  radon cc $PY_FILES -a -s 2>/dev/null | head -30
  radon mi $PY_FILES -s 2>/dev/null | head -30
fi

# Go — only if there are .go files in the diff
if [ -n "$GO_FILES" ] && command -v gocyclo &>/dev/null; then
  gocyclo -over 10 $GO_FILES 2>/dev/null | head -30
fi
```

| Score | Rating | Implication |
|-------|--------|-------------|
| A (1-5) | Simple | Good |
| B (6-10) | Moderate | OK |
| C (11-20) | Complex | Flag for review |
| D (21-30) | Very complex | Recommend refactor |
| F (31+) | Untestable | Must refactor |

Include complexity scores in the context packet so AI passes can flag high-complexity functions.

**2e. Check for repo-level review instructions:**
```bash
for f in .github/codereview.md .codereview.yaml .codereview.md AGENTS.md .github/copilot-instructions.md; do
  if [ -f "$f" ]; then
    echo "Found review instructions: $f"
  fi
done
```

If a config file exists, read it and incorporate its instructions into the review passes.

**2f. Load language standards (if available):**

Check if the `standards` skill is installed. If found, load the Tier 1 reference for each language detected in `CHANGED_FILES`. These give explorers concrete rules to check against (e.g., bare `except:` in Python, missing `err != nil` in Go).

```bash
# Detect languages from CHANGED_FILES extensions
LANGS=""
echo "$CHANGED_FILES" | grep -qE '\.py$'  && LANGS="$LANGS python"
echo "$CHANGED_FILES" | grep -qE '\.go$'  && LANGS="$LANGS go"
echo "$CHANGED_FILES" | grep -qE '\.(ts|tsx)$' && LANGS="$LANGS typescript"
echo "$CHANGED_FILES" | grep -qE '\.(js|jsx)$' && LANGS="$LANGS typescript"
echo "$CHANGED_FILES" | grep -qE '\.sh$'  && LANGS="$LANGS shell"

# Try to find standards skill references
STANDARDS_DIR=""
for dir in \
  skills/standards/references \
  .claude/plugins/cache/agentops-marketplace/agentops/*/skills/standards/references; do
  if [ -d "$dir" ]; then
    STANDARDS_DIR="$dir"
    break
  fi
done

if [ -n "$STANDARDS_DIR" ]; then
  for lang in $LANGS; do
    if [ -f "$STANDARDS_DIR/${lang}.md" ]; then
      echo "Loaded standards: $STANDARDS_DIR/${lang}.md"
      # Read and include in context packet
    fi
  done
else
  echo "INFO: standards skill not installed. Install for language-specific review rules."
  echo "  See: agentops marketplace → standards"
fi
```

Include loaded standards in the context packet so explorers can check code against language-specific best practices. Without standards installed, explorers still work — they just rely on their built-in knowledge.

**2g. Load spec/plan (if provided or auto-detected):**

If `--spec <path>` was provided, read it. Otherwise, look for a plan reference:
```bash
# Check git log for issue/bead references
git log --oneline HEAD~3..HEAD 2>/dev/null | head -5
# Check for plan docs
ls .agents/plans/ docs/plans/ 2>/dev/null | head -5
```
If a spec is found, include it in the context packet for requirements completeness checking.

**2h. Build a context packet** — a summary containing:
- The full diff
- List of changed files with brief descriptions
- Key surrounding code snippets (callers, interfaces, types)
- Dead code flags (functions with no callers)
- Complexity scores for changed files
- Language standards (if loaded)
- Spec/plan content (if available)
- Any repo-level review instructions found

### Step 3: Run Deterministic Scans (Best-Effort)

Before AI review, run available deterministic tools. Their output serves two purposes: (1) findings with `"source": "deterministic"` go directly into the final report, and (2) AI passes receive the scan results so they skip restating what tools already caught.

**Check tool availability and run each.** Scope scans to `CHANGED_FILES` where the tool supports it — this is faster and avoids noise from unrelated files:

```bash
# semgrep — static analysis / custom rules (scoped to changed files)
if command -v semgrep &>/dev/null; then
  semgrep scan --json --quiet $CHANGED_FILES 2>/dev/null | head -500
else
  echo "semgrep not installed (pip install semgrep)"
fi

# trivy — vulnerability / misconfiguration scanner
# trivy fs scans the whole project for dependency vulns — scoping to individual
# files would miss manifest-level findings, so scan the repo root
if command -v trivy &>/dev/null; then
  trivy fs . --format json --quiet 2>/dev/null | head -500
else
  echo "trivy not installed (brew install trivy)"
fi

# osv-scanner — dependency vulnerability scanner (repo-level scan)
if command -v osv-scanner &>/dev/null; then
  osv-scanner scan -r . --format json 2>/dev/null | head -500
else
  echo "osv-scanner not installed (go install github.com/google/osv-scanner/cmd/osv-scanner@latest)"
fi

# shellcheck — shell script linter (scoped to .sh files in diff)
SH_FILES=$(echo "$CHANGED_FILES" | grep -E '\.sh$' || true)
if [ -n "$SH_FILES" ] && command -v shellcheck &>/dev/null; then
  shellcheck --format=json $SH_FILES 2>/dev/null | head -500
else
  if [ -n "$SH_FILES" ]; then
    echo "shellcheck not installed (brew install shellcheck)"
  fi
fi

# pre-commit — repo-configured hooks (scoped to changed files)
if [ -f .pre-commit-config.yaml ] && command -v pre-commit &>/dev/null; then
  pre-commit run --files $CHANGED_FILES 2>&1 | head -200
fi
```

**Record `tool_status`** for each tool (ran / skipped / not_installed / failed) with version and finding count. Use these standard keys:

| Key | Tool |
|-----|------|
| `semgrep` | semgrep |
| `trivy` | trivy |
| `osv_scanner` | osv-scanner |
| `shellcheck` | shellcheck (shell files only) |
| `pre_commit` | pre-commit |
| `radon` | radon (Step 2d) |
| `gocyclo` | gocyclo (Step 2d) |
| `standards` | Language standards (Step 2f) |
| `ai_correctness` | Explorer: correctness |
| `ai_security` | Explorer: security |
| `ai_reliability` | Explorer: reliability/performance |
| `ai_test_adequacy` | Explorer: test adequacy |

This goes into the final JSON artifact envelope.

**Convert deterministic findings** into the standard finding schema with `"source": "deterministic"` and `"confidence": 1.0`.

If no tools are available, continue — the AI passes still run. Log which tools were missing in the final report.

### Step 4: Run AI Review (Explorer Sub-Agents → Review Judge)

The AI review uses an **explorer-judge architecture**: specialized explorer sub-agents investigate specific aspects in parallel, then a single review judge synthesizes their findings into a coherent review. This approach is better than running 4 independent passes because:
- Each explorer can go deep on its specialty without context window pressure
- The judge sees all findings together, enabling cross-cutting analysis and deduplication
- The judge can assess overall quality and produce a coherent verdict

**4a. Launch 4 explorer sub-agents in parallel** (single message, all at once):

```bash
# Paths to prompt files (relative to workspace root)
GLOBAL_CONTRACT="prompts/reviewer-global-contract.md"
CORRECTNESS_PASS="prompts/reviewer-correctness-pass.md"
SECURITY_PASS="prompts/reviewer-security-pass.md"
RELIABILITY_PASS="prompts/reviewer-reliability-performance-pass.md"
TEST_PASS="prompts/reviewer-test-adequacy-pass.md"
```

Read `prompts/reviewer-global-contract.md` first — it defines the shared rules and JSON output schema.

**Explorer 1 — Correctness:**
```
Tool: Task
Parameters:
  subagent_type: "general-purpose"
  model: "sonnet"
  description: "Review explorer: correctness"
  prompt: |
    <global contract content>
    <correctness pass prompt>

    ## Diff to Review
    <diff content>

    ## Context
    <context packet from Step 2 — callers, types, complexity scores>

    ## Deterministic Scan Results (already reported — do not restate)
    <summary of findings from Step 3, if any>

    ## Spec/Plan (if available)
    <spec content, or "No spec provided">

    You are an explorer sub-agent. Investigate thoroughly using
    Grep, Glob, and Read to trace code paths and verify your findings.
    Return ALL findings as a JSON array per the global contract schema.
    Do not self-censor low or medium issues. If no issues found, return [].
```

**Explorer 2 — Security:** (same structure, security pass prompt)

**Explorer 3 — Reliability/Performance:** (same structure, reliability pass prompt)

**Explorer 4 — Test Adequacy:** (same structure, test adequacy pass prompt)

Collect all 4 explorer result sets.

**4b. Review Judge — synthesize and verdict:**

Launch a single review judge that receives all explorer findings plus the context:

```
Tool: Task
Parameters:
  subagent_type: "general-purpose"
  description: "Review judge: synthesize findings"
  prompt: |
    You are the review judge. You have received findings from 4 specialized
    explorer sub-agents (correctness, security, reliability, test adequacy).

    ## Explorer Findings
    <JSON arrays from all 4 explorers>

    ## Context
    <context packet from Step 2>

    ## Deterministic Scan Results
    <findings from Step 3>

    ## Spec/Plan (if available)
    <spec content>

    Your tasks:
    1. DEDUPLICATE: Merge overlapping findings across explorers
    2. VALIDATE: Check each finding against the codebase — use Grep/Read
       to verify claims. Downgrade or remove findings that don't hold up.
    3. ENRICH: Add failure_mode and evidence where missing
    4. ASSESS STRENGTHS: Note 2-3 things done well in this change
    5. SPEC CHECK: If a spec was provided, check requirements completeness.
       Flag any spec requirements not addressed by the diff.
    6. VERDICT: Produce an overall merge readiness verdict:
       - PASS: No must-fix issues, code is ready to merge
       - WARN: Has should-fix issues but no blockers
       - FAIL: Has must-fix issues that block merge

    Return a JSON object:
    {
      "verdict": "PASS|WARN|FAIL",
      "verdict_reason": "1-2 sentence explanation",
      "strengths": ["strength 1", "strength 2"],
      "spec_gaps": ["missing requirement 1"],  // empty if no spec
      "findings": [ ... all validated findings as JSON array ... ]
    }
```

### Step 5: Merge, Deduplicate, and Classify

Combine deterministic findings (Step 3) with the judge's validated findings (Step 4b), then apply signal controls and classify into action tiers.

**5a. Enrich and combine:**

The orchestrator (the agent executing this skill) adds fields that explorers and the judge don't produce:

1. **Assign `source`** — set `"source": "deterministic"` for tool findings, `"source": "ai"` for judge findings
2. **Assign `id`** — generate a stable ID for each finding: `<pass>-<file-hash>-<line>` (e.g., `security-a3f1-42`)
3. **Combine** deterministic findings (confidence 1.0) with judge's findings into one list
4. **Confidence floor** — drop any AI finding with `confidence < 0.65`
5. **Deduplicate** — merge findings that share the same `file` + `line` or describe the same root cause; keep the higher-severity version and note all sources
6. **No linter restatement** — remove findings about formatting, naming conventions, or import ordering that linters/formatters already handle
7. **Evidence check** — for `high` or `critical` severity, verify `failure_mode` is populated; if not, downgrade to `medium`

**5b. Assign `action_tier` to each finding:**

The orchestrator assigns `action_tier` mechanically based on severity and confidence — this is not an AI judgment. Evaluate rules in order; first match wins. Since code agents can address findings quickly, surface everything actionable rather than enforcing a hard cap:

| Priority | Tier | Criteria | Action |
|----------|------|----------|--------|
| 1st | **Must Fix** | (`critical` or `high`) AND confidence >= 0.80 | Block merge or fix immediately |
| 2nd | **Should Fix** | `medium` severity, OR `high` with confidence 0.65-0.79 | Fix in this PR — fast for agents |
| 3rd | **Consider** | Everything else above the confidence floor | Fix if convenient, or defer |

Evaluate in order — a `high`/0.85 finding matches "Must Fix" and stops there; a `medium`/0.90 finding skips "Must Fix" (not high/critical), matches "Should Fix".

**5c. Rank within each tier** by `severity_weight * confidence`:

| Severity | Weight |
|----------|--------|
| critical | 4 |
| high | 3 |
| medium | 2 |
| low | 1 |

### Step 6: Format and Present Review

Output the review in this format:

```markdown
# Code Review: <target description>

**Verdict: PASS / WARN / FAIL** — <verdict reason>
**Scope:** <branch|staged|commit|pr> | **Base:** <base_ref> | **Head:** <head_ref>
**Reviewed:** <N files> | **Total findings:** <N> (from <total pre-filter> raw)

### Tool Status
| Tool | Key | Status | Findings |
|------|-----|--------|----------|
| semgrep | semgrep | ran | 3 |
| trivy | trivy | not_installed | — |
| osv-scanner | osv_scanner | not_installed | — |
| shellcheck | shellcheck | ran | 1 |
| pre-commit | pre_commit | skipped | — |
| radon | radon | ran | 2 hotspots |
| standards | standards | ran | python, go |
| AI: correctness | ai_correctness | ran | 4 |
| AI: security | ai_security | ran | 2 |
| AI: reliability | ai_reliability | ran | 1 |
| AI: test adequacy | ai_test_adequacy | ran | 3 |

---

## Strengths

- <what's done well — architecture, patterns, testing>
- <another positive observation>

---

## Must Fix (N findings)

Issues that should block merge or be fixed immediately.

| # | Sev | Source | File | Line | Summary | Fix |
|---|-----|--------|------|------|---------|-----|
| 1 | critical | AI:security | path/file.py | 42 | SQL injection | Use parameterized query |

### Finding 1: <summary>
**Category:** security | **Confidence:** 0.92 | **Source:** AI:security
**Failure mode:** <what breaks and when>
**Fix:** <smallest safe remediation>

---

## Should Fix (N findings)

Worth addressing in this PR — fast for code agents.

| # | Sev | Source | File | Line | Summary | Fix |
|---|-----|--------|------|------|---------|-----|
| ... |

---

## Consider (N findings)

Fix if convenient, or defer to a follow-up.

| # | Sev | Source | File | Line | Summary |
|---|-----|--------|------|------|---------|
| ... |

---

## Spec Gaps (if spec provided)

Requirements from the spec not addressed by this diff:
- [ ] <missing requirement 1>
- [ ] <missing requirement 2>

---

## Test Gaps

- [ ] <test scenario 1> — `path/to/file:line`
- [ ] <test scenario 2> — `path/to/file:line`

---

## Next Steps

Suggested fix order for code agents:
1. Must Fix items (blocking) — fix in order listed
2. Should Fix items — address in this PR
3. Test gaps — add tests for uncovered paths
4. Consider items — fix if time permits

**Pushback hints:** Findings marked with confidence < 0.75 may warrant
verification before fixing. Check the codebase context — the reviewer
may have missed something. Use your judgment.

---

## Summary

<1-2 sentence overall risk assessment>
**Verdict:** PASS/WARN/FAIL | **Must Fix:** N | **Should Fix:** N | **Consider:** N
```

**For PR mode with inline comments:**

Ask the user: "Would you like me to post these findings as inline PR comments?"

If confirmed, use `gh api` to post review comments:
```bash
gh api repos/{owner}/{repo}/pulls/{number}/reviews \
  --method POST \
  --field body="AI Code Review — <N> findings" \
  --field event="COMMENT" \
  --field comments="[{\"path\":\"...\",\"line\":...,\"body\":\"...\"}]"
```

### Step 7: Save Review Artifacts

Write two artifacts to the workspace:

```bash
mkdir -p .agents/reviews
```

**7a. Markdown report** — `.agents/reviews/YYYY-MM-DD-<target>.md`

Contains the formatted review from Step 6 plus a `## Raw Findings (Pre-Filter)` appendix.

**7b. JSON findings** — `.agents/reviews/YYYY-MM-DD-<target>.json`

Must conform to `findings-schema.json`. Contains the full envelope:

```json
{
  "run_id": "2026-02-08T14-30-00-a1b2c3",
  "timestamp": "2026-02-08T14:30:00Z",
  "scope": "branch",
  "base_ref": "main",
  "head_ref": "feature/auth-fix",
  "pr_number": null,
  "files_reviewed": ["src/auth.py", "src/middleware.py"],
  "verdict": "WARN",
  "verdict_reason": "Has should-fix issues but no blockers",
  "strengths": ["Clean separation of auth concerns", "Good test coverage for happy path"],
  "spec_gaps": [],
  "tool_status": {
    "semgrep": { "status": "ran", "version": "1.56.0", "finding_count": 3, "note": null },
    "trivy": { "status": "not_installed", "version": null, "finding_count": 0, "note": "brew install trivy" },
    "osv_scanner": { "status": "not_installed", "version": null, "finding_count": 0, "note": "go install github.com/google/osv-scanner/cmd/osv-scanner@latest" },
    "shellcheck": { "status": "ran", "version": "0.10.0", "finding_count": 1, "note": null },
    "pre_commit": { "status": "skipped", "version": null, "finding_count": 0, "note": "no .pre-commit-config.yaml" },
    "radon": { "status": "ran", "version": "6.0.1", "finding_count": 2, "note": "2 functions with complexity C or worse" },
    "standards": { "status": "ran", "version": null, "finding_count": 0, "note": "loaded: python, go" },
    "ai_correctness": { "status": "ran", "version": null, "finding_count": 4, "note": null },
    "ai_security": { "status": "ran", "version": null, "finding_count": 2, "note": null },
    "ai_reliability": { "status": "ran", "version": null, "finding_count": 1, "note": null },
    "ai_test_adequacy": { "status": "ran", "version": null, "finding_count": 3, "note": null }
  },
  "findings": [ ... ],
  "tier_summary": { "must_fix": 1, "should_fix": 5, "consider": 4 }
}
```

**7c. Validate output** (optional, if `jq` available):
```bash
bash skills/codereview/scripts/validate_output.sh \
  --findings .agents/reviews/YYYY-MM-DD-<target>.json \
  --report .agents/reviews/YYYY-MM-DD-<target>.md
```

---

## Configuration

The skill respects optional repo-level configuration. If a `.codereview.yaml` file exists in the repo root, read it for:

```yaml
# .codereview.yaml (optional)
passes:
  - correctness
  - security
  - reliability
  - test-adequacy

confidence_floor: 0.65

# Review cadence — controls when /codereview runs automatically
# Options:
#   manual     — only when explicitly invoked (default)
#   pre-commit — run before every commit (use with git hooks or agent workflow)
#   pre-push   — run before push
#   wave-end   — run after a batch of implementation steps completes
cadence: manual

# Pushback level — controls how aggressively findings are surfaced
# Options:
#   fix-all    — surface everything, expect agents to fix most issues (default)
#   selective  — surface must-fix and should-fix, mark consider items as optional
#   cautious   — only surface must-fix, everything else is informational
pushback_level: fix-all

ignore_paths:
  - "*.generated.*"
  - "vendor/"
  - "node_modules/"

focus_paths:
  - "src/auth/"
  - "src/payments/"

custom_instructions: |
  This repo uses Django ORM. Flag any raw SQL queries.
  All API endpoints must have rate limiting.
```

If no config file exists, use defaults (all 4 passes, 0.65 confidence, manual cadence, fix-all pushback).

### Cadence Modes

| Mode | When It Runs | Use Case |
|------|-------------|----------|
| `manual` | Only when user invokes `/codereview` | Default, on-demand |
| `pre-commit` | Before every commit in agent workflows | Catch issues early, prevent accumulation |
| `pre-push` | Before push (agent checks before sharing) | Balance between speed and quality |
| `wave-end` | After a batch of tasks completes | Efficient for multi-task agent sessions |

For `pre-commit` and `pre-push` modes, the calling agent should invoke `/codereview` at the appropriate point in its workflow. The cadence setting is advisory — it tells the agent *when* to call the skill, not a git hook.

For `wave-end` mode, the agent should track implementation steps and invoke `/codereview --base main` (or `--range <start>..HEAD`) after completing a logical batch (e.g., after implementing 3-5 related tasks). This reviews all committed changes in the wave.

### Pushback Levels

| Level | Must Fix | Should Fix | Consider |
|-------|----------|------------|----------|
| `fix-all` | Fix immediately | Fix in this PR | Fix if time permits |
| `selective` | Fix immediately | Fix in this PR | Informational only — agent's discretion |
| `cautious` | Fix immediately | Informational — agent's discretion | Informational only |

The pushback level affects the **Next Steps** section in the report. At `fix-all`, the report tells agents to address everything. At `cautious`, only must-fix items are listed as action items.

---

## Prompt Files

The review prompts live in the workspace `prompts/` directory:

| File | Purpose |
|------|---------|
| `prompts/reviewer-global-contract.md` | Shared rules and JSON output schema |
| `prompts/reviewer-correctness-pass.md` | Functional correctness review |
| `prompts/reviewer-security-pass.md` | Security risk review |
| `prompts/reviewer-reliability-performance-pass.md` | Reliability and performance review |
| `prompts/reviewer-test-adequacy-pass.md` | Test adequacy gap analysis |

---

## Examples

### Review Local Changes
```bash
/codereview
```
Auto-detects staged or recent changes. Runs all passes. Outputs findings to terminal.

### Review a Pull Request
```bash
/codereview 123
```
Fetches PR #123 diff via `gh`. Runs all passes with PR context (title, description). Optionally posts inline comments.

### Review an Entire Branch (Wave-End Review)
```bash
/codereview --base main
```
Reviews all commits on the current branch since it diverged from `main`. This is the primary mode for wave-end reviews — review everything done on a feature branch before merging.

### Review a Specific Commit Range
```bash
/codereview --range HEAD~5..HEAD
```
Reviews the last 5 commits. Useful for reviewing a specific wave of work within a longer-running branch.

### Review Against a Spec
```bash
/codereview --spec docs/plan.md --base main
```
Reviews changes against `main` and checks if the spec requirements are addressed.

---

## Architecture: Explorer-Judge Pattern

```
┌──────────────────────────────────────────────────────────────┐
│  Step 2: Context Gathering                                    │
│  - Diff analysis, callers/callees, dead code check            │
│  - Complexity analysis (radon/gocyclo)                        │
│  - Spec/plan loading                                          │
│  → Produces context packet                                    │
└──────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────┐
│  Step 3: Deterministic Scans                                  │
│  semgrep, trivy, osv-scanner, pre-commit                      │
│  → Produces deterministic findings                            │
└──────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│ Explorer:     │ │ Explorer:     │ │ Explorer:     │  ... (4 total)
│ Correctness   │ │ Security      │ │ Reliability   │
│               │ │               │ │               │
│ Uses Grep,    │ │ Uses Grep,    │ │ Uses Grep,    │
│ Read, Glob    │ │ Read, Glob    │ │ Read, Glob    │
│ to investigate│ │ to investigate│ │ to investigate│
│               │ │               │ │               │
│ Returns JSON  │ │ Returns JSON  │ │ Returns JSON  │
│ findings      │ │ findings      │ │ findings      │
└───────┬───────┘ └───────┬───────┘ └───────┬───────┘
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────────┐
│  Review Judge                                                 │
│  - Deduplicates explorer findings                             │
│  - Validates claims against codebase (Grep/Read)              │
│  - Assesses strengths                                         │
│  - Checks spec completeness                                   │
│  - Produces verdict: PASS / WARN / FAIL                       │
│  → Returns validated findings + verdict + strengths            │
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│  Step 5: Classify into tiers                                  │
│  Step 6: Format report with Next Steps                        │
│  Step 7: Save artifacts (.md + .json)                         │
└──────────────────────────────────────────────────────────────┘
```

**Why explorer-judge instead of independent passes:**
- Explorers use `sonnet` (fast, good at search) — cheaper and faster for investigation
- The judge uses the default model (thorough) — better for synthesis and verdict
- Explorers can use tools (Grep/Read/Glob) to deeply investigate their area
- The judge sees all findings together, resolving conflicts and removing duplicates
- Total context pressure is lower: each explorer only handles one specialty

---

## Design Rationale

| Decision | Why | Source |
|----------|-----|--------|
| Explorer-judge architecture | Specialized sub-agents investigate deeply, judge synthesizes and validates | Vibe/council explorer pattern, context window management |
| Complexity analysis in context | Feed radon/gocyclo scores to AI so it flags high-complexity functions | Vibe skill complexity step |
| Language standards (optional) | Give explorers concrete language-specific rules; graceful degradation if not installed | Vibe/standards skill two-tier system |
| Dead code / YAGNI check | Avoid reviewing and fixing unused code — waste of agent time | receiving-code-review YAGNI pattern |
| Spec/plan comparison | Check implementation completeness against requirements | requesting-code-review plan comparison |
| Merge verdict (PASS/WARN/FAIL) | Clear ship/no-ship signal for humans and downstream agents | Vibe/council verdict pattern, requesting-code-review assessment |
| Strengths section | Acknowledge good patterns — review isn't just finding faults | requesting-code-review strengths output |
| Configurable pushback level | fix-all for agent workflows, cautious for human review | User feedback: agents should fix most issues, but not rabbit-hole |
| Configurable review cadence | pre-commit for quality-critical, wave-end for throughput | User request: some projects need every-commit review |
| Next Steps with fix ordering | Guide downstream agents on what to fix and in what order | receiving-code-review implementation ordering |
| Deterministic scans before AI | Run semgrep/trivy/osv-scanner first so AI skips restating their findings | Previous plan, playbook 4-stage pipeline |
| Comprehensive findings (no hard cap) | Code agents fix fast — surface everything actionable, let tiers prioritize | Agent-assisted workflow reality |
| Action tiers (Must/Should/Consider) | Structured prioritization without losing lower-severity findings | Replaces rigid comment budget |
| Confidence floor (0.65) | Dramatically reduces false positives | Playbook signal controls |
| Structured JSON + Markdown output | JSON for machine consumption and validation; Markdown for humans | Global contract schema |
| Envelope metadata in artifacts | run_id/timestamp/scope/tool_status/verdict make reviews traceable | Previous plan |
| Best-effort degradation | Skip unavailable tools with explicit status rather than failing | Previous plan |
| Repo-level config file | Teams customize passes, cadence, pushback, paths, thresholds | CodeRabbit `.coderabbit.yaml`, Gemini `config.yaml` |

---

## Acceptance Criteria

### Functional

| Scenario | Expected Behavior |
|----------|-------------------|
| No-diff repo | Exits cleanly: "No changes found to review" |
| Branch diff (`--base`) | Computes merge-base, reviews all commits since divergence, produces findings |
| Commit range (`--range`) | Reviews specific commit range, scope=range |
| PR mode | Fetches PR diff via `gh`, includes PR title/body in context |
| Spec provided | Loads spec, checks requirements completeness, includes spec gaps in report |
| No spec | Skips spec check, report omits "Spec Gaps" section |
| Missing tools | Scans skipped with explicit status, AI passes still run, report notes gaps |
| Shell files in diff | shellcheck runs if installed, scoped to .sh files only |
| radon/gocyclo available | Complexity scores included in context, hotspots noted in tool status |
| Standards skill installed | Language-specific rules loaded and included in explorer context |
| All tools available | Deterministic + AI findings merged, deduplicated, tiered |
| Empty findings | Valid JSON with `"findings": []`, verdict PASS, report with "No issues found" |
| Dead code detected | YAGNI findings flagged in report |
| Cadence: pre-commit | Skill can be invoked before each commit in agent workflow |
| Cadence: wave-end | Skill invoked with `--base` or `--range` after batch of implementation steps |

### Output Validation

| Check | Requirement |
|-------|-------------|
| JSON structure | `findings.json` validates against `findings-schema.json` |
| Envelope fields | `run_id`, `timestamp`, `scope`, `base_ref`, `head_ref`, `tool_status`, `verdict` present |
| Finding fields | Every finding has `id`, `source`, `pass`, `severity`, `confidence`, `file`, `line`, `summary` |
| Confidence gating | No AI findings with `confidence < 0.65` in final output |
| Evidence gating | All `high`/`critical` findings have `failure_mode` populated |
| Action tiers | Every finding classified as Must Fix / Should Fix / Consider |
| Verdict | Report contains PASS/WARN/FAIL with reason |
| Strengths | Report contains at least 1 strength (or "No specific strengths noted") |
| Markdown report | Contains verdict, scope, tool status, strengths, tiered findings, next steps, summary |

### Policy

| Rule | Enforcement |
|------|-------------|
| Review-only | No code files modified by the skill |
| No external API calls | Uses only the active CLI model, no separate model runtime |
| Deterministic before AI | Deterministic scans always run first when tools are available |
| Comprehensive output | All findings above confidence floor are reported (no hard cap) |
| Graceful degradation | Missing tools never cause failure — always noted in tool_status |

Run the validation script to verify:
```bash
bash skills/codereview/scripts/validate_output.sh \
  --findings .agents/reviews/<latest>.json \
  --report .agents/reviews/<latest>.md
```

---

## Future: Multi-Model Consensus (v2)

Running the same review across multiple models (e.g., Claude + Codex) and comparing their findings could significantly improve review quality. When two models independently flag the same issue, confidence is very high. When they disagree, the disagreement itself is a signal worth human attention.

Adversarial debate (where models review each other's findings and must steel-man opposing views before revising) is another promising direction — the council/vibe skills demonstrate this pattern works well.

These are areas to explore once the core single-model review is battle-tested.

---

## See Also

- `findings-schema.json` — JSON Schema for the findings artifact
- `scripts/validate_output.sh` — Output validation script
- `docs/tooling-ai-code-review-playbook-2026-02-08.md` — Full playbook and reference architecture
- `templates/review-tooling-scorecard.csv` — Evaluation scorecard for comparing tools
