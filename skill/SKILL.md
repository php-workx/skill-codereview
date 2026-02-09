---
name: codereview
description: 'Use when reviewing code changes before merge — PRs, staged files, branches, or commit ranges. Also for pre-merge checks, finding bugs, security audits, or checking implementation against a spec. Triggers: "code review", "review PR", "review this diff", "review my changes", "check for bugs", "security review", "/codereview".'
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

**Flags can be combined:** `/codereview --spec docs/plan.md --base main` applies both `--base` (for the diff target) and `--spec` (for requirements checking). Parse all flags before selecting the diff mode.

**If `--spec <path>` provided:** Read the spec/plan file. It will be included in the context packet so AI passes can check implementation completeness.

**Pre-flight check:** If the diff is empty, tell the user "No changes found to review" and stop.

**Store results for later steps:**
- `DIFF` — the full diff content
- `CHANGED_FILES` — list of changed file paths (one per line, extracted via `--name-only`)
- `SCOPE` — one of `branch`, `range`, `staged`, `commit`, `pr`, `path`
- `BASE_REF` / `HEAD_REF` — the base and head references
- `PR_NUMBER` — the PR number in PR mode, `null` otherwise

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

Before AI review, run available deterministic tools. Their output serves two purposes: (1) findings with `"source": "deterministic"` go directly into the final report, and (2) AI passes receive the scan results so they skip restating what tools already caught.

**Check tool availability and run each.** Scope scans to `CHANGED_FILES` where the tool supports it — this is faster and avoids noise from unrelated files.

**Permission strategy (sandboxed runners):** request elevated permissions **before** starting the Step 3 scan bundle (single escalation for the whole bundle), instead of waiting for fail-then-retry. This avoids repeated sandbox failures for tools that need host caches or Docker access.

Use a one-line justification like:
- `Run deterministic code-review scanners with host cache/docker access to avoid sandbox permission failures and collect complete tool_status output.`

**Note on file paths:** `CHANGED_FILES` is newline-delimited. When passing to tools, use `xargs` or quote-safe expansion to handle paths with spaces: `echo "$CHANGED_FILES" | xargs -I{} ...` or convert to an array first.

**Bash compatibility (macOS):** avoid `mapfile`/`readarray` because default macOS Bash is 3.2 and does not support them.

```bash
# Bash 3-safe way to convert CHANGED_FILES (newline-delimited) into an array
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && FILES+=("$f")
done <<EOF
$CHANGED_FILES
EOF

# Safe expansion preserves spaces in paths
semgrep scan --json --quiet "${FILES[@]}"
```

**Parallel policy:** when both are available, run `semgrep` and `sonarqube` in parallel, then wait for both before normalization/deduplication.

**Sandbox/cache setup:** initialize writable cache dirs before scans so tools do not fail on default cache locations in sandboxed runs.

```bash
export TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-/tmp/trivy-cache}"
export PRE_COMMIT_HOME="${PRE_COMMIT_HOME:-/tmp/pre-commit-cache}"
export SEMGREP_HOME="${SEMGREP_HOME:-/tmp/semgrep-home}"
export GOCACHE="${GOCACHE:-/tmp/go-build-cache}"
export GOMODCACHE="${GOMODCACHE:-/tmp/go-mod-cache}"
export GOPATH="${GOPATH:-/tmp/go}"
mkdir -p "$TRIVY_CACHE_DIR" "$PRE_COMMIT_HOME" "$SEMGREP_HOME" "$GOCACHE" "$GOMODCACHE" "$GOPATH"
```

**Execution safety:** If your runner shell is `zsh` (common in agent environments), do **not** embed a multiline script in a single-quoted `bash -lc '...'` string. Inner single-quoted jq/rg patterns (for example `jq '.results | length'`) will terminate the outer quote and trigger parse errors like `bad pattern` or `no matches found`. For multiline logic, write a temp script with a quoted heredoc and run it with bash.

```bash
cat > /tmp/codereview_tools.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# Safe: inner single quotes stay intact because this is a real script file.
count=$(jq '.results | length' /tmp/codereview/semgrep.json 2>/dev/null || echo 0)
echo "semgrep findings: $count"
EOF

chmod +x /tmp/codereview_tools.sh
/tmp/codereview_tools.sh
```

```bash
# Parallel semgrep + sonarqube pattern (preferred when both tools are present)
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
SONAR_OUT_DIR=".sonarqube/${RUN_ID}"
mkdir -p /tmp/codereview "$SONAR_OUT_DIR"
SONAR_SCRIPT="$HOME/.claude/skills/sonarqube/scripts/sonarqube.py"
[ ! -f "$SONAR_SCRIPT" ] && SONAR_SCRIPT="$HOME/.codex/skills/sonarqube/scripts/sonarqube.py"

SEM_PID=""
SONAR_PID=""
SEM_RC=0
SONAR_RC=0

if command -v semgrep &>/dev/null; then
  HOME="$SEMGREP_HOME" semgrep scan --json --quiet $CHANGED_FILES > "/tmp/codereview/semgrep-${RUN_ID}.json" 2>"/tmp/codereview/semgrep-${RUN_ID}.err" &
  SEM_PID=$!
fi

if [ -f "$SONAR_SCRIPT" ] && command -v python3 &>/dev/null; then
  python3 "$SONAR_SCRIPT" scan --mode local --severity medium --scope new \
    --base-ref "${BASE_REF:-HEAD~1}" --list-only --output-dir "$SONAR_OUT_DIR" \
    > "/tmp/codereview/sonarqube-${RUN_ID}.out" 2>"/tmp/codereview/sonarqube-${RUN_ID}.err" &
  SONAR_PID=$!
fi

if [ -n "$SEM_PID" ]; then
  wait "$SEM_PID"; SEM_RC=$?
fi
if [ -n "$SONAR_PID" ]; then
  wait "$SONAR_PID"; SONAR_RC=$?
fi
```

```bash
# semgrep — static analysis / custom rules (scoped to changed files)
if command -v semgrep &>/dev/null; then
  HOME="$SEMGREP_HOME" semgrep scan --json --quiet $CHANGED_FILES 2>/dev/null | head -500
else
  echo "semgrep not installed (pip install semgrep)"
fi

# trivy — vulnerability / misconfiguration scanner
# trivy fs scans the whole project for dependency vulns — scoping to individual
# files would miss manifest-level findings, so scan the repo root
if command -v trivy &>/dev/null; then
  trivy fs . --cache-dir "$TRIVY_CACHE_DIR" --format json --quiet 2>/dev/null | head -500
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
  PRE_COMMIT_HOME="$PRE_COMMIT_HOME" \
  GOCACHE="$GOCACHE" \
  GOMODCACHE="$GOMODCACHE" \
  GOPATH="$GOPATH" \
  pre-commit run --files $CHANGED_FILES 2>&1 | head -200
fi

# sonarqube — deep static analysis via the sonarqube skill's Python scanner
# Requires: python3 + sonarqube.py from skill-sonarqube (local mode needs Docker + sonar-scanner)
# The script auto-starts a local SonarQube container if none is running.
# Output: .sonarqube/findings.json with structured findings per file.
SONAR_SCRIPT="$HOME/.claude/skills/sonarqube/scripts/sonarqube.py"
if [ ! -f "$SONAR_SCRIPT" ]; then
  SONAR_SCRIPT="$HOME/.codex/skills/sonarqube/scripts/sonarqube.py"
fi
if [ -f "$SONAR_SCRIPT" ] && command -v python3 &>/dev/null; then
  python3 "$SONAR_SCRIPT" scan --mode local --severity medium --scope new \
    --base-ref "${BASE_REF:-HEAD~1}" --list-only --output-dir .sonarqube 2>/dev/null
  # Read findings if scan succeeded
  if [ -f .sonarqube/findings.json ]; then
    cat .sonarqube/findings.json | head -500
  fi
else
  echo "sonarqube skill not installed (see skill-sonarqube README)"
fi
```

**Normalize and deduplicate deterministic findings before AI merge:**
- Normalize each deterministic tool output into the standard finding shape (`pass`, `file`, `line`, `summary`, `severity`, `confidence=1.0`, `evidence`).
- Build a deterministic dedupe key: `file + ":" + line + ":" + normalize(summary)`.
- On key collision (e.g., semgrep + sonarqube same issue), keep one finding with:
  - highest severity,
  - unioned provenance in `sources` (e.g., `["semgrep","sonarqube"]`),
  - merged evidence/note text.
- Feed this deduplicated deterministic set to explorers and final merge.

**Record `tool_status`** for each tool (`ran` / `skipped` / `not_installed` / `sandbox_blocked` / `failed`) with version and finding count. Use these standard keys:

| Key | Tool |
|-----|------|
| `semgrep` | semgrep |
| `trivy` | trivy |
| `osv_scanner` | osv-scanner |
| `shellcheck` | shellcheck (shell files only) |
| `pre_commit` | pre-commit |
| `sonarqube` | sonarqube (local or cloud, via skill-sonarqube) |
| `radon` | radon (Step 2d) |
| `gocyclo` | gocyclo (Step 2d) |
| `standards` | Language standards (Step 2f) |
| `ai_correctness` | Explorer: correctness |
| `ai_security` | Explorer: security |
| `ai_reliability` | Explorer: reliability/performance |
| `ai_test_adequacy` | Explorer: test adequacy |
| `ai_error_handling` | Explorer: error handling (extended) |
| `ai_api_contract` | Explorer: API/contract (extended) |
| `ai_concurrency` | Explorer: concurrency (extended) |
| `ai_judge` | Review judge |

This goes into the final JSON artifact envelope.

**Tool status classification rules:**
- `not_installed`: command not found on PATH.
- `sandbox_blocked`: stderr indicates permission/sandbox denial (for example `permission denied`, `operation not permitted`, read-only fs/cache path denied).
- `failed`: command executed but failed for non-sandbox reasons.
- `skipped`: intentionally not run (for example no matching files, missing config, optional mode).
- `ran`: completed successfully (with or without findings).

**Convert deterministic findings** into the standard finding schema with `"source": "deterministic"` and `"confidence": 1.0`.

If no tools are available, continue — the AI passes still run. Log which tools were missing in the final report.

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
5. **Check spec compliance** (if spec provided)
6. **Produce verdict** (PASS/WARN/FAIL with reason)

The judge returns a JSON object with `verdict`, `verdict_reason`, `strengths`, `spec_gaps`, and validated `findings` array.

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

Output the review in this format:

```markdown
# Code Review: <target description>

**Verdict: PASS / WARN / FAIL** — <verdict reason>
**Scope:** <branch|staged|commit|pr> | **Base:** <base_ref> | **Head:** <head_ref>
**Reviewed:** <N files> | **Total findings:** <N> (from <total pre-filter> raw)

### Tool Status
| Tool | Key | Status | Findings | Note |
|------|-----|--------|----------|------|
| semgrep | semgrep | ran | 3 | — |
| trivy | trivy | sandbox_blocked | 0 | cache path permission denied |
| osv-scanner | osv_scanner | not_installed | — | go install github.com/google/osv-scanner/cmd/osv-scanner@latest |
| shellcheck | shellcheck | ran | 1 | — |
| pre-commit | pre_commit | skipped | — | no .pre-commit-config.yaml |
| sonarqube | sonarqube | not_installed | — | see skill-sonarqube README |
| radon | radon | ran | 2 hotspots | functions with complexity C or worse |
| standards | standards | ran | 0 | loaded: python, go |
| AI: correctness | ai_correctness | ran | 4 | — |
| AI: security | ai_security | ran | 2 | — |
| AI: reliability | ai_reliability | ran | 1 | — |
| AI: test adequacy | ai_test_adequacy | ran | 3 | — |
| AI: error handling | ai_error_handling | ran | 2 | — |
| AI: API/contract | ai_api_contract | skipped | — | No public API surface changes in diff |
| AI: concurrency | ai_concurrency | skipped | — | No concurrency primitives in diff |
| AI: judge | ai_judge | ran | 8 | 4 findings removed by adversarial validation |

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

**Source column format:** The report's Source column combines the `source` and `pass` fields for readability. AI findings show as `AI:<pass>` (e.g., `AI:security` means `source=ai, pass=security`). Deterministic findings show the tool name (e.g., `semgrep`, `shellcheck`).

**Tool-status prose rule:** when summarizing deterministic tooling in prose, separate categories so readers can tell what happened:
- Missing tools: list only `not_installed` tools, and state explicitly that all listed tools were unavailable.
- Sandbox-blocked tools: list only `sandbox_blocked` tools with the denied path/action.
- Failed tools: list only `failed` tools (non-sandbox runtime errors).
- Never merge these into a single mixed sentence.

Example:
- `Missing tools (all unavailable): semgrep, osv-scanner, sonarqube, radon, gocyclo.`
- `Sandbox-blocked: trivy (cache path permission denied).`
- `Failed: pre-commit (hook runtime error).`

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
    "trivy": { "status": "sandbox_blocked", "version": "0.56.0", "finding_count": 0, "note": "cache path permission denied; set TRIVY_CACHE_DIR=/tmp/trivy-cache" },
    "osv_scanner": { "status": "not_installed", "version": null, "finding_count": 0, "note": "go install github.com/google/osv-scanner/cmd/osv-scanner@latest" },
    "shellcheck": { "status": "ran", "version": "0.10.0", "finding_count": 1, "note": null },
    "pre_commit": { "status": "skipped", "version": null, "finding_count": 0, "note": "no .pre-commit-config.yaml" },
    "sonarqube": { "status": "not_installed", "version": null, "finding_count": 0, "note": "see skill-sonarqube README" },
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
bash scripts/validate_output.sh \
  --findings .agents/reviews/YYYY-MM-DD-<target>.json \
  --report .agents/reviews/YYYY-MM-DD-<target>.md
```

---

## Configuration

The skill respects optional repo-level configuration. If a `.codereview.yaml` file exists in the repo root, read it for:

```yaml
# .codereview.yaml (optional)
passes:
  # Core passes (always run unless removed from this list)
  - correctness
  - security
  - reliability
  - test-adequacy
  # Extended passes (subject to adaptive skip signals)
  - error-handling
  - api-contract
  - concurrency

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

# Model override per pass (optional)
# Default: all explorers use "sonnet", judge uses session default model
# Use stronger models for passes where precision matters most
pass_models:
  # security: "opus"       # use stronger model for security analysis
  # concurrency: "opus"    # use stronger model for concurrency analysis
  # judge: null            # null means use session default model

# Force all configured passes to run even if adaptive skip signals trigger
# Default: false (adaptive skip is enabled)
force_all_passes: false

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

If no config file exists, use defaults (4 core passes + 3 extended passes, 0.65 confidence, manual cadence, fix-all pushback, sonnet model for explorers, adaptive skip enabled).

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

## Common Mistakes

| Mistake | What Goes Wrong | Fix |
|---------|----------------|-----|
| Launching explorers without reading prompt files first | Explorers get no global contract or pass-specific instructions, produce unstructured output | Always `Read` the prompt files in Step 4 before constructing explorer prompts |
| Launching judge before all 4 explorers finish | Judge has incomplete findings, misses cross-cutting issues | Wait for all 4 Task results before launching the judge |
| Posting PR comments without user confirmation | Unwanted noise on the PR, user didn't consent | Always ask "Would you like me to post these as inline PR comments?" first |
| Forgetting to include deterministic scan results in explorer prompts | Explorers restate what semgrep/trivy/sonarqube already found, creating duplicates | Pass scan summaries to each explorer with "do not restate" instruction |
| Skipping context gathering (Step 2) | Explorers can't trace callers/callees, miss integration bugs | Step 2 is critical — don't jump straight to Step 3/4 |
| Not extracting `CHANGED_FILES` in Step 1 | Steps 2d, 2f, and 3 can't scope to changed files, run on entire repo | Every diff mode must set `CHANGED_FILES` via `--name-only` |

---

## Architecture & Design Rationale

See `references/design.md` for the full architecture diagram, explorer-judge rationale, design decision table, and future v2 plans. Not needed at runtime.

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
| sonarqube skill installed | Runs `sonarqube.py scan --list-only`, findings merged as deterministic source |
| sonarqube not installed | Skipped with tool_status note, other scans and AI passes still run |
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
| Envelope fields | `run_id`, `timestamp`, `scope`, `base_ref`, `head_ref`, `verdict`, `verdict_reason`, `strengths`, `files_reviewed`, `tool_status`, `findings`, `tier_summary` present |
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
bash scripts/validate_output.sh \
  --findings .agents/reviews/<latest>.json \
  --report .agents/reviews/<latest>.md
```

---

## See Also

- `findings-schema.json` — JSON Schema for the findings artifact
- `scripts/validate_output.sh` — Output validation script
- `references/design.md` — Architecture diagram, design rationale, and future plans
- `prompts/` — Explorer sub-agent prompt files (global contract + 4 passes)
