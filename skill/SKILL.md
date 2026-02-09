---
name: codereview
description: 'Use when reviewing local code changes — staged files, branches, commit ranges, or paths — before they become a PR. Runs deterministic scans (semgrep, trivy, shellcheck) and parallel AI explorer-judge passes to find bugs, security issues, missing tests, and spec gaps. Also works on PRs but optimized for pre-merge local review.'
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

## When to Use

- **Before creating a PR** — review your work locally first, catch issues before review roundtrips
- **After implementing a feature** — verify nothing was missed before sharing
- **Before merging a feature branch** — wave-end review with `--base main`
- **After writing code against a spec** — verify requirements with `--spec docs/plan.md`
- **When you want deeper analysis than linters** — AI explorers trace call paths, check callers, verify test coverage
- **Agent-assisted workflows** — findings include fix suggestions agents can execute immediately

### Which Mode to Use

| You want to review... | Command |
|----------------------|---------|
| Staged changes | `/codereview` (no args) |
| Last commit (nothing staged) | `/codereview` (no args) |
| Entire feature branch | `/codereview --base main` |
| Specific commits | `/codereview --range abc..def` |
| Specific files/paths | `/codereview src/auth/` |
| A pull request | `/codereview 42` |
| Changes against a spec | `/codereview --spec docs/plan.md --base main` |
| One section of a spec | `/codereview --spec docs/plan.md --spec-scope "Auth" --base main` |

---

## When NOT to Use

- **Single-line typo fixes** — just fix it, no review needed
- **Documentation-only changes** — markdown/comment edits don't need multi-pass review
- **Generated code** (protobuf, OpenAPI stubs) — review the generator config, not the output
- **Reverts** — if reverting a known-bad commit, skip the review
- **Empty diffs** — the skill detects this and exits, but don't invoke it if you already know there's nothing to review

---

## Execution Steps

### Step 1: Determine Review Target

Parse the argument to determine what to review:

**If PR number provided** (digits only):
```bash
gh pr diff <number> > /tmp/codereview-diff.patch
CHANGED_FILES=$(gh pr view <number> --json files --jq '.files[].path')
gh pr view <number> --json title,body,files --jq '{title: .title, body: .body, files: [.files[].path]}' 2>/dev/null
# SCOPE=pr, PR_NUMBER=<number>
```

**If `--base <branch>` provided** (branch review — all commits since divergence):
```bash
# Three-dot diff: everything committed on this branch since it diverged from <branch>
MERGE_BASE=$(git merge-base <branch> HEAD)
git diff $MERGE_BASE..HEAD
CHANGED_FILES=$(git diff $MERGE_BASE..HEAD --name-only)
# SCOPE=branch, BASE_REF=<branch>, HEAD_REF=HEAD
```
This is the primary mode for **wave-end reviews** — review all work done on a feature branch.

**If `--range <from>..<to>` provided** (specific commit range):
```bash
git diff <from>..<to>
CHANGED_FILES=$(git diff <from>..<to> --name-only)
# SCOPE=range, BASE_REF=<from>, HEAD_REF=<to>
```
Use this for reviewing a specific wave of commits (e.g., "review the last 5 commits").

**If path provided:**
```bash
git diff HEAD -- <path>
CHANGED_FILES=$(git diff HEAD --name-only -- <path>)
# SCOPE=path
```

**If no argument (auto-detect):**
```bash
# Try staged changes first
STAGED=$(git diff --cached --stat 2>/dev/null)
if [ -n "$STAGED" ]; then
  git diff --cached
  CHANGED_FILES=$(git diff --cached --name-only)
  # SCOPE=staged
else
  # Fall back to last commit
  git diff HEAD~1
  CHANGED_FILES=$(git diff HEAD~1 --name-only)
  # SCOPE=commit
fi
```

**Flags can be combined:** `/codereview --spec docs/plan.md --spec-scope "Authentication" --base main` applies `--base` (for the diff target), `--spec` (for requirements checking), and `--spec-scope` (to restrict to a section of the spec). Parse all flags before selecting the diff mode.

**If `--spec <path>` provided:** Read the spec/plan file. It will be included in the context packet so AI passes can check implementation completeness. This also enables the spec-verification explorer pass.

**If `--spec-scope <text>` provided:** Store the scope text for use by the spec-verification explorer. The explorer will filter requirements to the matching section/milestone of the spec. Requires `--spec` — if given without `--spec`, warn the user and ignore.

**Pre-flight check:** If the diff is empty, tell the user "No changes found to review" and stop.

**Store results for later steps:**
- `DIFF` — the full diff content
- `CHANGED_FILES` — list of changed file paths (one per line, extracted via `--name-only`)
- `SCOPE` — one of `branch`, `range`, `staged`, `commit`, `pr`, `path`
- `BASE_REF` / `HEAD_REF` — the base and head references
- `PR_NUMBER` — the PR number in PR mode, `null` otherwise
- `SPEC_CONTENT` — the spec/plan file content, if `--spec` was provided
- `SPEC_SCOPE` — the scope filter text from `--spec-scope`, if provided

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
# JS/JSX maps to typescript — the standards skill uses a single typescript.md for both
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

Run available deterministic tools (semgrep, trivy, osv-scanner, shellcheck, pre-commit, sonarqube). Their output serves two purposes: (1) deterministic findings go directly into the final report, and (2) AI passes receive scan results so they skip restating what tools already caught.

**See `references/deterministic-scans.md`** for full tool scripts, cache setup, parallel execution patterns, zsh safety workarounds, and tool status keys.

**Summary of what to do:**
1. Request elevated permissions before the scan bundle (single escalation)
2. Initialize sandbox/cache dirs (TRIVY_CACHE_DIR, SEMGREP_HOME, etc.)
3. Run each tool scoped to `CHANGED_FILES` where supported; run semgrep + sonarqube in parallel when both available
4. Normalize findings into standard schema (`source: "deterministic"`, `confidence: 1.0`)
5. Deduplicate: on `file:line:summary` collision, keep highest severity, union provenance in `sources`
6. Record `tool_status` for every tool (`ran` / `skipped` / `not_installed` / `sandbox_blocked` / `failed`)

If no tools are available, continue — the AI passes still run. Log missing tools in the final report.

### Step 4: Run AI Review (Explorer Sub-Agents → Review Judge)

The AI review uses an **explorer-judge architecture**: specialized explorer sub-agents investigate specific aspects in parallel, then a single review judge synthesizes their findings into a coherent review. This approach is better than running 4 independent passes because:
- Each explorer can go deep on its specialty without context window pressure
- The judge sees all findings together, enabling cross-cutting analysis and deduplication
- The judge can assess overall quality and produce a coherent verdict

**4a. Launch explorer sub-agents in parallel** (single message, all at once):

```bash
# Paths to prompt files (relative to skill directory)
# When installed, these live alongside SKILL.md in the skill's prompts/ subdirectory.
# The executing agent should locate them relative to this SKILL.md file.
GLOBAL_CONTRACT="prompts/reviewer-global-contract.md"
JUDGE_PROMPT="prompts/reviewer-judge.md"
CORRECTNESS_PASS="prompts/reviewer-correctness-pass.md"
SECURITY_PASS="prompts/reviewer-security-pass.md"
RELIABILITY_PASS="prompts/reviewer-reliability-performance-pass.md"
TEST_PASS="prompts/reviewer-test-adequacy-pass.md"
ERROR_HANDLING_PASS="prompts/reviewer-error-handling-pass.md"
API_CONTRACT_PASS="prompts/reviewer-api-contract-pass.md"
CONCURRENCY_PASS="prompts/reviewer-concurrency-pass.md"
SPEC_VERIFICATION_PASS="prompts/reviewer-spec-verification-pass.md"
```

Read `prompts/reviewer-global-contract.md` first — it defines the shared rules, chain-of-thought protocol, and JSON output schema.

**Core explorers (always run):**

| # | Explorer | Prompt File | Model | `pass` value |
|---|----------|------------|-------|-------------|
| 1 | Correctness | `reviewer-correctness-pass.md` | sonnet (or `pass_models.correctness`) | `correctness` |
| 2 | Security | `reviewer-security-pass.md` | sonnet (or `pass_models.security`) | `security` |
| 3 | Reliability/Performance | `reviewer-reliability-performance-pass.md` | sonnet (or `pass_models.reliability`) | `reliability` / `performance` |
| 4 | Test Adequacy | `reviewer-test-adequacy-pass.md` | sonnet (or `pass_models.test-adequacy`) | `testing` |

**Extended explorers (run if configured in `passes` and not adaptively skipped):**

| # | Explorer | Prompt File | Model | `pass` value | Adaptive skip signal |
|---|----------|------------|-------|-------------|---------------------|
| 5 | Error Handling | `reviewer-error-handling-pass.md` | sonnet (or `pass_models.error-handling`) | `reliability` | Skip if diff is test/docs/config only |
| 6 | API/Contract | `reviewer-api-contract-pass.md` | sonnet (or `pass_models.api-contract`) | `correctness` | Skip if no public API surface changes in diff |
| 7 | Concurrency | `reviewer-concurrency-pass.md` | sonnet (or `pass_models.concurrency`) | `reliability` | Skip if no concurrency primitives in diff |
| 8 | Spec Verification | `reviewer-spec-verification-pass.md` | sonnet (or `pass_models.spec-verification`) | `spec_verification` | Skip if no spec loaded |

**Adaptive pass selection:** Before launching extended explorers, check skip signals (see Step 3.5 below). If `force_all_passes: true` in config, skip this check and launch all configured passes.

**Explorer prompt template (same for all explorers):**
```
Tool: Task
Parameters:
  subagent_type: "general-purpose"
  model: <pass_models[pass_name] from config, default "sonnet">
  description: "Review explorer: <pass name>"
  prompt: |
    <global contract content>
    <pass-specific prompt>

    ## Diff to Review
    <diff content>

    ## Context
    <context packet from Step 2 — callers, types, complexity scores>

    ## Deterministic Scan Results (already reported — do not restate)
    <summary of findings from Step 3, if any>

    ## Spec/Plan (if available)
    <spec content, or "No spec provided">

    ## Spec Scope (if provided)
    Restrict spec analysis to section matching: "<SPEC_SCOPE value, or omit if not provided>"

    You are an explorer sub-agent. Investigate thoroughly using
    Grep, Glob, and Read to trace code paths and verify your findings.
    Return ALL findings as a JSON array per the global contract schema.
    Do not self-censor low or medium issues. If no issues found, return [].
```

Launch all applicable explorers in a **single message** (parallel execution). Collect all explorer result sets before proceeding to the judge.

**Step 3.5: Adaptive Pass Selection**

Before launching extended explorers, evaluate skip signals. Core passes (correctness, security, reliability, test-adequacy) are never skipped. Extended passes are skipped when their skip signal triggers:

```bash
# Error handling: skip if diff is test-only, docs-only, or config-only
ERROR_HANDLING_SKIP=false
if echo "$CHANGED_FILES" | grep -qvE '\.(test|spec)\.|_test\.|test_|\.md$|\.ya?ml$|\.json$|\.toml$'; then
  ERROR_HANDLING_SKIP=false  # has non-test/doc/config files
else
  ERROR_HANDLING_SKIP=true
fi

# API/Contract: skip if no public API surface changes
API_CONTRACT_SKIP=true
if echo "$DIFF" | grep -qE 'route|endpoint|handler|@app\.|@api\.|@router\.|export (function|class|interface)|func [A-Z]|pub fn|public .* class|\.proto|\.graphql|openapi|swagger'; then
  API_CONTRACT_SKIP=false
fi

# Concurrency: skip if no concurrency primitives
CONCURRENCY_SKIP=true
if echo "$DIFF" | grep -qiE 'goroutine|go func|threading|Thread|async def|asyncio|\.lock\(|mutex|chan |channel|atomic|sync\.|Promise\.all|Worker\(|spawn|tokio'; then
  CONCURRENCY_SKIP=false
fi

# Spec verification: skip if no spec was loaded
SPEC_VERIFICATION_SKIP=true
if [ -n "$SPEC_CONTENT" ]; then
  SPEC_VERIFICATION_SKIP=false
fi
```

Skipped passes get `tool_status` with `status: "skipped"` and a note explaining why (e.g., "No concurrency primitives detected in diff"). If `force_all_passes: true` in config, all configured passes run regardless of skip signals.

**4b. Review Judge — synthesize and verdict:**

Read `prompts/reviewer-judge.md` for the full judge protocol. Launch a single review judge that receives all explorer findings plus the context:

```
Tool: Task
Parameters:
  subagent_type: "general-purpose"
  description: "Review judge: synthesize findings"
  prompt: |
    <judge prompt from prompts/reviewer-judge.md>

    ## Explorer Findings
    <JSON arrays from all explorers>

    ## Context
    <context packet from Step 2>

    ## Deterministic Scan Results
    <findings from Step 3>

    ## Spec/Plan (if available)
    <spec content>
```

The judge will:
1. **Adversarially validate** each finding (existence check, contradiction check, severity calibration)
2. **Group root causes** (merge related findings, eliminate causal chains)
3. **Cross-synthesize** (catch gaps no single explorer flagged)
4. **Assess strengths** (2-3 specific observations)
5. **Check spec compliance** (if spec provided) — merge spec-verification explorer's `requirements` array with other explorers' findings, validate implementation/test claims, produce final `spec_requirements` and derive `spec_gaps`
6. **Produce verdict** (PASS/WARN/FAIL with reason)

The judge returns a JSON object with `verdict`, `verdict_reason`, `strengths`, `spec_gaps`, `spec_requirements`, and validated `findings` array.

**Note on spec-verification explorer output:** The spec-verification explorer returns a JSON object with two keys: `requirements` (the traceability data) and `findings` (standard findings with `pass: "spec_verification"`). Pass both the `requirements` array and the `findings` array to the judge. The judge merges `requirements` into its `spec_requirements` output and validates the `findings` alongside other explorers' findings.

### Step 5: Merge, Deduplicate, and Classify

Combine deterministic findings (Step 3) with the judge's validated findings (Step 4b), then apply signal controls and classify into action tiers.

**5a. Enrich and combine:**

The orchestrator (the agent executing this skill) adds fields that explorers and the judge don't produce:

1. **Assign `source`** — set `"source": "deterministic"` for tool findings, `"source": "ai"` for judge findings
2. **Assign `id`** — generate a stable ID for each finding: `<pass>-<file-hash>-<line>` (e.g., `security-a3f1-42`)
3. **Combine** deterministic findings (confidence 1.0) with judge's findings into one list
4. **Confidence floor** — drop any AI finding with `confidence < 0.65`
5. **Deduplicate** — merge findings that share the same dedupe key (`file + line + normalized summary`) or clearly describe the same root cause; keep the higher-severity version and preserve provenance in `sources` (for example `["semgrep","sonarqube"]`)
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

**See `references/report-template.md`** for the full markdown template and JSON envelope format.

Output the review with these sections (in order): verdict header, tool status table, strengths, Must Fix / Should Fix / Consider tiered findings, spec verification (if spec provided), test gaps, next steps, summary.

**Key formatting rules:**
- Source column: AI findings show as `AI:<pass>` (e.g., `AI:security`). Deterministic findings show the tool name.
- Tool-status prose: separate `not_installed`, `sandbox_blocked`, and `failed` into distinct sentences — never merge into one mixed sentence.
- For PR mode: ask user "Would you like me to post these as inline PR comments?" before posting via `gh api`.

### Step 7: Save Review Artifacts

```bash
mkdir -p .agents/reviews
```

**7a. Markdown report** — `.agents/reviews/YYYY-MM-DD-<target>.md` (formatted review + raw findings appendix)

**7b. JSON findings** — `.agents/reviews/YYYY-MM-DD-<target>.json` (must conform to `findings-schema.json`; see `references/report-template.md` for envelope format)

**7c. Validate output** (optional, if `jq` available):
```bash
bash scripts/validate_output.sh \
  --findings .agents/reviews/YYYY-MM-DD-<target>.json \
  --report .agents/reviews/YYYY-MM-DD-<target>.md
```

---

## Configuration

Optional repo-level config via `.codereview.yaml`. See `docs/CONFIGURATION.md` for the full schema reference.

**Defaults** (no config file needed): 8 passes (4 core + 4 extended), 0.65 confidence floor, manual cadence, fix-all pushback, sonnet model for explorers, adaptive skip enabled. The spec-verification pass only runs when `--spec` is provided.

**Key settings:**

| Setting | Default | Options |
|---------|---------|---------|
| `passes` | All 8 | List of pass names to enable |
| `cadence` | `manual` | `manual`, `pre-commit`, `pre-push`, `wave-end` |
| `pushback_level` | `fix-all` | `fix-all`, `selective`, `cautious` |
| `confidence_floor` | `0.65` | `0.0` – `1.0` |
| `pass_models` | sonnet for all | Override model per pass (e.g., `security: "opus"`) |
| `force_all_passes` | `false` | Disable adaptive skip for extended passes |
| `ignore_paths` | none | Glob patterns to exclude |
| `focus_paths` | none | Glob patterns to prioritize |
| `custom_instructions` | none | Free-text repo-specific rules |

Cadence is advisory — it tells the agent *when* to invoke the skill, not a git hook. For `wave-end`, invoke with `--base main` after completing a batch of tasks.

---

## Prompt Files

The review prompts live in the `prompts/` directory alongside this SKILL.md:

| File | Purpose |
|------|---------|
| `prompts/reviewer-global-contract.md` | Shared rules, chain-of-thought protocol, and JSON output schema |
| `prompts/reviewer-judge.md` | Review judge: adversarial validation, deduplication, verdict |
| `prompts/reviewer-correctness-pass.md` | Functional correctness review (core) |
| `prompts/reviewer-security-pass.md` | Security risk review (core) |
| `prompts/reviewer-reliability-performance-pass.md` | Reliability and performance review (core) |
| `prompts/reviewer-test-adequacy-pass.md` | Test adequacy gap analysis (core) |
| `prompts/reviewer-error-handling-pass.md` | Error handling quality review (extended) |
| `prompts/reviewer-api-contract-pass.md` | API/contract breaking changes (extended) |
| `prompts/reviewer-concurrency-pass.md` | Concurrency issues and race conditions (extended) |
| `prompts/reviewer-spec-verification-pass.md` | Spec requirement tracing and test category adequacy (extended) |

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
Reviews changes against `main` and checks if the spec requirements are implemented and tested. Produces a per-requirement traceability report with implementation status and test category coverage.

### Review a Specific Section of a Spec
```bash
/codereview --spec docs/plan.md --spec-scope "Authentication" --base main
```
Same as above, but restricts spec verification to the "Authentication" section of the spec. Use `--spec-scope` with any heading text, milestone label, or keyword to scope verification.

---

## Common Mistakes

**Execution mistakes (for the agent running the skill):**

| Mistake | What Goes Wrong | Fix |
|---------|----------------|-----|
| Launching explorers without reading prompt files first | Explorers get no global contract or pass-specific instructions, produce unstructured output | Always `Read` the prompt files in Step 4 before constructing explorer prompts |
| Launching judge before all explorers finish | Judge has incomplete findings, misses cross-cutting issues | Wait for all Task results before launching the judge |
| Posting PR comments without user confirmation | Unwanted noise on the PR, user didn't consent | Always ask "Would you like me to post these as inline PR comments?" first |
| Forgetting to include deterministic scan results in explorer prompts | Explorers restate what semgrep/trivy/sonarqube already found, creating duplicates | Pass scan summaries to each explorer with "do not restate" instruction |
| Skipping context gathering (Step 2) | Explorers can't trace callers/callees, miss integration bugs | Step 2 is critical — don't jump straight to Step 3/4 |
| Not extracting `CHANGED_FILES` in Step 1 | Steps 2d, 2f, and 3 can't scope to changed files, run on entire repo | Every diff mode must set `CHANGED_FILES` via `--name-only` |

**User-facing mistakes:**

| Mistake | Fix |
|---------|-----|
| Running on a huge diff (1000+ files) | Use `--base` with a recent merge-base, or scope to specific paths |
| No deterministic tools installed | Install `semgrep` and `shellcheck` at minimum for best results |
| Expecting PR comment posting without `gh` auth | Ensure `gh auth status` works before using PR mode |
| Using `--spec` with a vague spec | Spec works best with concrete, testable requirements — acceptance criteria format |
| Using `--spec-scope` without `--spec` | `--spec-scope` requires `--spec`; it's ignored without it |

---

## Architecture & Design Rationale

See `references/design.md` for the full architecture diagram, explorer-judge rationale, design decision table, and future v2 plans. Not needed at runtime.

---

## Acceptance Criteria

See `references/acceptance-criteria.md` for full functional scenarios, output validation checks, and policy rules. Not needed at runtime.

---

## See Also

- `findings-schema.json` — JSON Schema for the findings artifact
- `scripts/validate_output.sh` — Output validation script
- `references/design.md` — Architecture diagram, design rationale, and future plans
- `references/deterministic-scans.md` — Full tool scripts, cache setup, parallel patterns
- `references/report-template.md` — Full markdown report template and JSON envelope format
- `references/acceptance-criteria.md` — Functional scenarios and output validation checks
- `prompts/` — Explorer sub-agent prompt files (global contract + judge + 8 explorer passes)
