# Plan: Code Review Skill v1.2

Five features to improve review quality and operational workflow. Feature 0 (script extraction) should be done first as it establishes the scripting pattern that Features 1-3 build on. Features 1-4 are independent of each other.

### Design Principle: Scripts Over Prompts

Wherever a step in the pipeline is **mechanical** (deterministic rules, data transformation, tool invocation, arithmetic), it should be implemented as a **script** (bash or Python) rather than described as instructions for an AI agent. This eliminates the agent's ability to diverge from the intended process and ensures reproducible results.

**Use scripts for:**
- Tool detection and invocation (coverage tools, git commands, linters)
- Data transformation (JSON manipulation, normalization, filtering)
- Rule-based classification (tier assignment, confidence floor, severity weighting)
- Hash computation and comparison (fingerprinting, fuzzy matching)
- File I/O and artifact management (loading previous reviews, suppressions)

**Use AI (prompts/agents) for:**
- Understanding code semantics and intent
- Investigating call paths, data flow, and integration points
- Assessing severity and writing evidence descriptions
- Cross-cutting synthesis (judge, cross-chunk, cross-model)
- Report narration and verdict reasoning

The boundary is judgment. If a step requires reading code and reasoning about behavior, it's an AI task. If it's applying a formula or running a tool, it's a script.

---

## Feature 0: Extract Existing Pipeline Steps into Scripts

**Goal:** Move mechanical steps that are currently described as agent instructions in SKILL.md into executable scripts. This reduces agent interpretation variance, makes the pipeline testable, and establishes the scripting pattern for Features 1-3.

### Scripts to extract

#### 0a-0. Project tooling discovery — script + agent (NEW)

Most projects already have configured quality gates (make lint, npm run test, cargo clippy) with project-specific thresholds, exclusions, and rules. The codereview skill should discover and use these rather than running only its own defaults.

**This is a two-layer process:**

| Layer | Tool | Does what | Why this layer |
|-------|------|-----------|----------------|
| **Script** | `scripts/discover-project.py` | Finds build files, extracts raw data (file lists, target names, script names, CI step names, config sections). Deterministic scan. | Fast, no tokens, reproducible. Finds the facts. |
| **Agent** | Step 2 context gathering (new sub-step) | Reads the build files the script identified. Understands intent: resolves non-obvious target names (`preflight`, `verify`, `checks`), follows target dependencies (`make ci` calls `make lint && make test`), identifies what each command actually does. | Judgment: a Makefile target called `verify` that chains `ruff + mypy + pytest` is only understandable by reading the Makefile. |

**Why both layers:** A script can find that a `Makefile` has targets `lint`, `verify`, and `preflight` — but only an LLM can read the Makefile and understand that `preflight` runs `make lint && make test`, that `verify` adds `mypy --strict` on top, and that `lint` uses a non-standard tool the script doesn't know about. Similarly, a `package.json` script called `checks` that runs `eslint . && tsc --noEmit && jest --ci` is opaque to a script but transparent to an agent.

##### `scripts/discover-project.py` — raw project scanner

**Extract into a Python script that:**

1. Accepts `CHANGED_FILES` (newline-delimited, via stdin or file arg)
2. For each changed file, finds the nearest project root (closest ancestor with `package.json`, `go.mod`, `Cargo.toml`, `pyproject.toml`, `Makefile`, etc.)
3. Groups changed files by project context (critical for monorepos)
4. For each project context, extracts **raw data** (not interpreted):
   - Which build files exist and their paths
   - Target/script names from build files (Makefile targets, package.json scripts, Justfile recipes)
   - Config section names from tool configs (pyproject.toml `[tool.*]`, `.golangci.yml` linters)
   - CI workflow file paths and step names
   - Monorepo orchestrator files
5. Outputs raw project profile JSON to stdout

**Project root detection order** (first match wins per directory, walking up from each changed file):

| File | Language/System | What to extract (raw) |
|------|----------------|----------------------|
| `package.json` | Node.js/TypeScript | All `scripts.*` keys (names only, not values — agent reads values) |
| `go.mod` | Go | Module path |
| `Cargo.toml` | Rust | `[workspace]` presence, `[dev-dependencies]` keys |
| `pyproject.toml` | Python | `[tool.*]` section names, `[project.scripts]` keys |
| `setup.cfg` / `setup.py` | Python (legacy) | `[tool:*]` section names |
| `Makefile` / `Justfile` / `Taskfile.yml` | Any | Target/recipe names (extracted via `make -qp`, `just --list`, or regex) |
| `go.work` | Go workspace | Workspace `use` entries |

**Monorepo orchestrator detection:**

| File | Orchestrator | What to extract (raw) |
|------|-------------|----------------------|
| `turbo.json` | Turborepo | Pipeline task names |
| `nx.json` | Nx | Target names |
| `pnpm-workspace.yaml` | pnpm workspaces | Package glob patterns |
| `lerna.json` | Lerna | Package directories |
| Cargo `[workspace]` | Cargo workspaces | Workspace members list |
| `go.work` | Go workspaces | Module paths |

**Output format** (raw data — agent interprets this):

```json
{
  "monorepo": true,
  "orchestrator": { "type": "turborepo", "config_file": "turbo.json" },
  "contexts": [
    {
      "root": "packages/api",
      "language": "go",
      "build_files": [
        { "path": "packages/api/Makefile", "type": "makefile", "targets": ["lint", "test", "verify", "ci"] },
        { "path": "packages/api/go.mod", "type": "go_mod" }
      ],
      "tool_configs": [],
      "ci_files": [".github/workflows/api-ci.yml"],
      "changed_files": ["packages/api/src/handler.go"]
    },
    {
      "root": "packages/web",
      "language": "typescript",
      "build_files": [
        { "path": "packages/web/package.json", "type": "package_json", "scripts": ["lint", "test", "test:coverage", "typecheck", "checks", "preflight"] }
      ],
      "tool_configs": [
        { "path": "packages/web/.eslintrc.json", "type": "eslint" },
        { "path": "packages/web/tsconfig.json", "type": "typescript" }
      ],
      "ci_files": [".github/workflows/web-ci.yml"],
      "changed_files": ["packages/web/src/App.tsx"]
    },
    {
      "root": "packages/ml",
      "language": "python",
      "build_files": [
        { "path": "packages/ml/pyproject.toml", "type": "pyproject", "tool_sections": ["tool.ruff", "tool.pytest.ini_options", "tool.mypy"] },
        { "path": "packages/ml/Makefile", "type": "makefile", "targets": ["lint", "test", "coverage", "preflight"] }
      ],
      "tool_configs": [],
      "ci_files": [],
      "changed_files": ["packages/ml/pipeline.py"]
    }
  ]
}
```

For single-project repos, `contexts` has one entry with `root: "."`.

**Interface:**
```bash
echo "$CHANGED_FILES" | python3 scripts/discover-project.py > /tmp/codereview-project.json
```

Python 3 stdlib only (extracts Makefile targets via `make -qp 2>/dev/null | grep -E '^[a-zA-Z]'` or regex fallback, reads TOML sections via regex, reads JSON natively).

##### Agent interpretation (Step 2, new sub-step)

After the script runs, the agent reads the raw profile and the build files it references:

1. **Read each build file** identified in the profile (Makefile, package.json, Justfile, etc.) using the Read tool
2. **Resolve non-obvious commands**: For targets/scripts with unclear names (`preflight`, `verify`, `checks`, `ci`), read their definitions to understand what they actually run
3. **Identify quality-relevant commands**: From the full list of targets/scripts, select those that run linting, testing, coverage, type checking, or security scanning
4. **Map to standard categories**: Assign each relevant command to one of: `lint`, `test`, `coverage`, `typecheck`, `security`
5. **Determine invocation**: For monorepos, decide whether to run commands via the orchestrator (`turbo run lint --filter=...`) or directly in the project root (`cd packages/api && make lint`)
6. **Produce the interpreted project profile**: The final profile that `run-scans.sh` consumes via `--project-profile`

**The agent outputs an interpreted profile** (written to `/tmp/codereview-project-interpreted.json`):

```json
{
  "monorepo": true,
  "contexts": [
    {
      "root": "packages/api",
      "language": "go",
      "commands": {
        "lint": { "cmd": "cd packages/api && make lint", "note": "runs golangci-lint with project config" },
        "test": { "cmd": "cd packages/api && make test", "note": "runs go test -race ./..." },
        "coverage": { "cmd": "cd packages/api && make coverage", "note": "generates cover.out" },
        "typecheck": null
      },
      "changed_files": ["packages/api/src/handler.go"]
    }
  ]
}
```

**Why the agent, not the script, does interpretation:**
- A `Makefile` target `preflight: lint test typecheck` requires reading the Makefile to understand it chains three commands
- A `package.json` script `"checks": "eslint . && tsc --noEmit && jest --ci"` requires parsing the shell command string
- A CI workflow that conditionally runs `cargo clippy` only on `*.rs` changes requires understanding YAML + GitHub Actions path filter syntax
- A `Justfile` recipe that sources a `.env` file before running tests requires understanding the recipe body

None of these are tractable for a regex-based script. All are trivial for an LLM reading the file.

#### 0a. `scripts/run-scans.sh` — Deterministic scan orchestration (from Step 3)

Currently, `references/deterministic-scans.md` is a reference document that the agent reads and reimplements each time. Different runs may execute tools in different order, handle errors differently, or miss tools entirely.

**Tool inventory — three tiers:**

The skill runs a layered set of tools. The principle: **never skip a quality tool just because the project wasn't using it before.** Project-specific tools add coverage; our baseline tools add security guarantees the project may lack.

**Tier 1 — Baseline tools (always run with our defaults if installed):**

These catch security and vulnerability issues. Run regardless of whether the project uses them.

| Tool | What it does | Scoped to | Default timeout |
|------|-------------|-----------|-----------------|
| `semgrep` | Static analysis, security patterns | CHANGED_FILES | 60s |
| `trivy` | Dependency vulnerability scanner | Repo root | 120s |
| `osv-scanner` | Dependency vulnerability scanner (OSV database) | Repo root | 120s |
| `gitleaks` | Secret detection (API keys, tokens, passwords) | CHANGED_FILES | 60s |
| `shellcheck` | Shell script linter | `.sh` files in diff | 60s |

**Tier 2 — Language-detected tools (run if language detected in CHANGED_FILES):**

These provide language-specific quality checks. Run with project config if found, otherwise with sensible defaults.

| Tool | Language | Detection | With project config | Without project config |
|------|----------|-----------|--------------------|-----------------------|
| `clippy` | Rust | `.rs` in CHANGED_FILES + `Cargo.toml` exists | `cargo clippy` (uses project's `clippy.toml` / `Cargo.toml` config) | `cargo clippy -- -W clippy::all` (default warnings) |
| `ruff` | Python | `.py` in CHANGED_FILES | `ruff check` (uses project's `pyproject.toml` / `ruff.toml` config) | `ruff check --select=E,F,W` (pycodestyle + pyflakes, conservative) |
| `golangci-lint` | Go | `.go` in CHANGED_FILES | `golangci-lint run` (uses project's `.golangci.yml` config) | `golangci-lint run --enable=govet,errcheck,staticcheck` (safe defaults) |
| `eslint` | TypeScript/JS | `.ts`/`.js`/`.tsx`/`.jsx` in CHANGED_FILES + config exists | `eslint` (uses project's `.eslintrc.*` / `eslint.config.*`) | **Skip** (eslint without config produces too much noise; project must opt-in) |

**Tier 3 — Project-configured tools (run only if project has them set up):**

| Tool | When to run |
|------|-------------|
| `pre-commit` | `.pre-commit-config.yaml` exists |
| `sonarqube` | sonarqube skill installed |
| Project's own `make lint` / `npm run lint` / etc. | Discovered by `discover-project.py` |

**Deduplication across tiers:** If a project's `make lint` runs semgrep internally (e.g., via pre-commit), we'd get duplicate findings. Deduplicate on `file:line:summary` key after normalization — same as before. The dedup step handles this automatically.

**Extract into a single script that:**

1. Accepts `CHANGED_FILES` (newline-delimited, via stdin or file arg), `BASE_REF` (env var or flag), and optionally `--project-profile <path>` (output of `discover-project.py`)
2. Runs Tier 1 baseline tools (always, if installed)
3. Detects languages in CHANGED_FILES and runs Tier 2 tools (if installed), preferring project config when present
4. If `--project-profile` provided, runs Tier 3 project-configured commands (scoped per project context for monorepos)
5. Sets up sandbox/cache dirs (TRIVY_CACHE_DIR, SEMGREP_HOME, etc.)
6. Runs tools in parallel where possible (Tier 1 tools in parallel, then Tier 2, then Tier 3)
7. Applies per-tool timeouts to prevent hangs (see table above). If a tool times out, record `tool_status: "timeout"` and continue. (See pm-20260325-005.)
8. Normalizes each tool's output into the standard finding shape (JSON array) per the mapping table below
9. Deduplicates on `file:line:summary` key — handles cross-tier duplicates automatically
10. Outputs two things:
    - `stdout`: JSON object with `{ "findings": [...], "tool_status": {...} }`
    - `stderr`: human-readable progress log

**Per-tool normalization mapping** (how each tool's native output maps to the standard finding shape):

| Tool | `file` | `line` | `summary` | `severity` mapping | `evidence` | `pass` |
|------|--------|--------|-----------|-------------------|------------|--------|
| semgrep | `.results[].path` | `.results[].start.line` | `.results[].message` | error→high, warning→medium, info→low | `.results[].extra.lines` (code snippet) | Derive from `.results[].check_id`: `security.*`→security, `correctness.*`→correctness, default→maintainability |
| trivy | `.Results[].Target` or `.Vulnerabilities[].PkgName` | `0` (no line) | `.Results[].Vulnerabilities[].Title` | CRITICAL→critical, HIGH→high, MEDIUM→medium, LOW→low | `.VulnerabilityID` + `.InstalledVersion` + `.FixedVersion` | security |
| osv-scanner | `.results[].packages[].package.name` | `0` (no line) | `.vulnerabilities[].summary` | `.database_specific.severity` if present; default→medium | Vulnerability ID (CVE/GHSA) | security |
| gitleaks | `.[]?.File` | `.[]?.StartLine` | `"Secret detected: " + .[]?.Description` | All→critical (leaked secrets are always critical) | `.[]?.Match` (redacted to first/last 4 chars) + `.[]?.RuleID` | security |
| shellcheck | `.[].file` | `.[].line` | `.[].message` | error→high, warning→medium, info→low, style→low | `.[].code` (SC number) + `.[].fix` if present | reliability |
| clippy | Parse from stderr: file path | Parse from stderr: line number | Lint message | error→high, warning→medium, note→low | Clippy lint name (e.g., `clippy::unwrap_used`) | Derive: `clippy::correctness`→correctness, `clippy::suspicious`→correctness, `clippy::security`→security, default→maintainability |
| ruff | `.[]?.filename` | `.[]?.location.row` | `.[]?.message` | `E`/`F`→high, `W`→medium, `C`/`I`→low | Rule code (e.g., `E501`, `F841`) | maintainability |
| golangci-lint | `.Issues[].Pos.Filename` | `.Issues[].Pos.Line` | `.Issues[].Text` | Parse from linter: `errcheck`/`govet`→high, `staticcheck`→medium, style linters→low | `.Issues[].FromLinter` + `.Issues[].SourceLines` | Derive: `errcheck`/`govet`→correctness, `gosec`→security, default→maintainability |
| pre-commit | Parse from stderr | Parse if available, else `0` | First line of hook output | All→medium | Full hook output (truncated to 500 chars) | maintainability |
| sonarqube | `.issues[].component` (strip prefix) | `.issues[].line` | `.issues[].message` | BLOCKER→critical, CRITICAL→high, MAJOR→medium, MINOR→low, INFO→low | `.issues[].rule` + `.issues[].effort` | Derive from `.issues[].type`: BUG→correctness, VULNERABILITY→security, CODE_SMELL→maintainability |
| Project commands | Parse from stdout/stderr (best-effort) | Parse if available, else `0` | First meaningful line | All→medium (unknown tool severity) | Full output (truncated to 500 chars) | maintainability |

Each normalized finding has `source: "deterministic"`, `confidence: 1.0`, and the tool name stored in a `tool` field for provenance.

**Interface:**
```bash
# Without project discovery (baseline + language-detected tools only)
echo "$CHANGED_FILES" | bash scripts/run-scans.sh --base-ref "$BASE_REF" > /tmp/codereview-scans.json

# With project discovery (adds project-configured tools)
echo "$CHANGED_FILES" | bash scripts/run-scans.sh --base-ref "$BASE_REF" \
  --project-profile /tmp/codereview-project.json > /tmp/codereview-scans.json
```

**What stays in the agent's hands:** The agent runs `discover-project.py` first (Step 2, new sub-step), then passes the profile to `run-scans.sh`. The agent does NOT re-interpret `deterministic-scans.md` — it runs the scripts and consumes the JSON.

**`references/deterministic-scans.md` becomes:** Reference documentation only (explaining what each tool does, when to install them, etc.). No longer the source of executable logic.

#### 0b. `scripts/enrich-findings.py` — Finding enrichment and classification (from Step 5)

Currently, the agent performs Step 5 by reading SKILL.md rules and applying them. This is the most divergence-prone step — agents make different tier assignment choices, skip deduplication steps, or miscalculate severity weights.

**Extract the mechanical parts into a Python script that:**

1. Accepts raw findings JSON from the judge (stdin or file arg) and deterministic findings JSON from `run-scans.sh`
2. Combines both into one list
3. Assigns `source` field ("deterministic" or "ai")
4. Generates stable `id` for each finding: `<pass>-<file-hash>-<line>`
5. Applies confidence floor (drops AI findings < 0.65)
6. Applies evidence check (high/critical without `failure_mode` → downgrade to medium)
7. Assigns `action_tier` mechanically: Must Fix / Should Fix / Consider per the rules table
8. Ranks within each tier by `severity_weight * confidence`
9. Computes `tier_summary` counts
10. Outputs enriched findings JSON to stdout

**Interface:**
```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/codereview-judge.json \
  --scan-findings /tmp/codereview-scans.json \
  --confidence-floor 0.65 \
  > /tmp/codereview-enriched.json
```

**What stays in the agent's hands:**
- Step 5a item 5: Deduplication by "same root cause" — this requires judgment about whether two findings describe the same underlying issue with different wording. The agent does this BEFORE passing findings to the script.
- Step 5a item 6: "No linter restatement" — detecting that a finding restates what a linter already catches requires understanding the finding's content. The agent does this BEFORE passing findings to the script.

The agent runs dedup and linter-restatement removal first (using AI judgment), then pipes the clean list to `enrich-findings.py` for mechanical enrichment.

#### 0c. `scripts/complexity.sh` — Complexity analysis (from Step 2d)

Currently inline bash snippets in SKILL.md. Extract into a script that:

1. Accepts `CHANGED_FILES` (newline-delimited, via stdin or file arg)
2. Detects language from file extensions
3. Runs available tools: `radon` for Python, `gocyclo` for Go
4. Outputs JSON: `{ "hotspots": [...], "tool_status": {...} }` where each hotspot has `file`, `function`, `score`, `rating`
5. Only reports functions rated C or worse (complexity >= 11)

**Interface:**
```bash
echo "$CHANGED_FILES" | bash scripts/complexity.sh > /tmp/codereview-complexity.json
```

### Edge cases

- **Scripts not executable**: The agent should `chmod +x` scripts before first run, or invoke via `bash scripts/...` / `python3 scripts/...` explicitly. Use the explicit interpreter approach for portability.
- **Python not available**: `enrich-findings.py` requires Python 3. If `python3` is not available, the agent falls back to performing Step 5 manually (as it does today). Log a warning: "python3 not found — falling back to agent-based enrichment."
- **Script fails**: If a script exits non-zero, the agent logs the stderr output and falls back to manual execution for that step. Scripts never block the review — they degrade gracefully.
- **Script output is invalid JSON**: The agent validates script output with `jq . < output.json` before consuming. If invalid, fall back to manual execution.

### Interaction with existing pipeline

- **SKILL.md Steps 3, 5**: Rewritten to say "Run `scripts/run-scans.sh`" and "Run `scripts/enrich-findings.py`" instead of describing the logic inline. The inline logic remains in SKILL.md as comments/documentation for understanding, but is clearly marked as "implemented by script — do not reimplement."
- **`references/deterministic-scans.md`**: Retains all documentation (tool descriptions, install instructions, cache setup rationale) but the executable snippets are moved to `scripts/run-scans.sh`. Add a note: "The executable logic from this document has been extracted to `scripts/run-scans.sh`. This file is now reference-only."
- **`validate_output.sh`**: Unchanged — it validates the final output regardless of how it was produced (script or agent).

### Testing

Each script can be tested independently with fixture input:

```bash
# Test discover-project.py with a known file list
echo "packages/api/src/handler.go" | python3 scripts/discover-project.py | jq .

# Test run-scans.sh with a known file list (baseline tools only)
echo "src/auth/login.py" | bash scripts/run-scans.sh --base-ref HEAD~1 | jq .

# Test run-scans.sh with project profile (adds project-configured tools)
echo "src/auth/login.py" | bash scripts/run-scans.sh --base-ref HEAD~1 \
  --project-profile /tmp/codereview-project.json | jq .

# Test enrich-findings.py with fixture JSON
python3 scripts/enrich-findings.py \
  --judge-findings tests/fixtures/judge-output.json \
  --scan-findings tests/fixtures/scan-output.json \
  | jq '.findings | length'

# Test complexity.sh
echo "src/auth/login.py" | bash scripts/complexity.sh | jq .
```

### Files to create

- `skills/codereview/scripts/discover-project.py` — Project tooling discovery (monorepo-aware)
- `skills/codereview/scripts/run-scans.sh` — Deterministic scan orchestration (3-tier: baseline + language + project)
- `skills/codereview/scripts/enrich-findings.py` — Finding enrichment and classification
- `skills/codereview/scripts/complexity.sh` — Complexity analysis

### Files to modify

- `skills/codereview/SKILL.md` — Update Steps 2d, 3, and 5 to invoke scripts. Keep logic descriptions as documentation, clearly marked "implemented by script."
- `skills/codereview/references/deterministic-scans.md` — Mark as reference-only, add pointer to `scripts/run-scans.sh`
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: script available, python3 missing fallback, script failure fallback, script invalid output

### Effort: Medium

---

## Feature 1: Git History Risk Scoring

**Goal:** Give explorers a per-file risk signal based on historical bug frequency and churn, so they pay more attention to code that has been problematic before.

### Where it fits

New sub-step **2i** in Step 2 (Gather Context), after complexity analysis (2d) and before building the context packet (2h). Also integrates into Step 2-L Phase A (large-diff mode) as a lightweight global context component.

### Implementation: `scripts/git-risk.sh`

This feature is **entirely deterministic** — no AI judgment needed. Implemented as a bash script.

**Interface:**
```bash
echo "$CHANGED_FILES" | bash scripts/git-risk.sh [--months 6] > /tmp/codereview-git-risk.json
```

**Input:** Newline-delimited file paths on stdin.

**Output:** JSON object on stdout:
```json
{
  "shallow_clone": false,
  "lookback_months": 6,
  "files": [
    { "file": "src/auth/session.py", "churn": 14, "bug_commits": 3, "last_bug": "2026-03-13", "risk": "high" },
    { "file": "src/api/orders.py", "churn": 6, "bug_commits": 1, "last_bug": "2026-02-08", "risk": "medium" }
  ],
  "summary": { "high": 1, "medium": 1, "low": 4 }
}
```

Low-risk files are included in the JSON (for completeness) but omitted from the context packet table (to save tokens).

**Script logic:**

For each file, compute three signals:

```bash
for file in "${FILES[@]}"; do
  # 1. Churn: total commits touching this file in lookback period
  CHURN=$(git log --oneline --follow --since="${MONTHS} months ago" -- "$file" 2>/dev/null | wc -l | tr -d ' ')

  # 2. Bug signal: commits with fix/bug/revert/hotfix in message
  BUG_COMMITS=$(git log --oneline --follow --since="${MONTHS} months ago" --grep='fix\|bug\|revert\|hotfix' -i -- "$file" 2>/dev/null | wc -l | tr -d ' ')

  # 3. Recency: date of last bug-related commit
  LAST_BUG=$(git log -1 --format='%as' --follow --since="${MONTHS} months ago" --grep='fix\|bug\|revert\|hotfix' -i -- "$file" 2>/dev/null)
done
```

Risk tier assignment (deterministic rules):

| Condition | Risk | Signal |
|-----------|------|--------|
| BUG_COMMITS >= 3 OR (BUG_COMMITS >= 2 AND CHURN >= 10) | **high** | Frequent bugs or high churn + bugs |
| BUG_COMMITS >= 1 OR CHURN >= 8 | **medium** | Some bug history or notable churn |
| Otherwise | **low** | Stable file |

**The agent's role:** Run the script, read the JSON, format the context packet table. No interpretation of git history — that's the script's job.

### Edge cases

- **Shallow clones** (common in CI): `git log` may have limited history. If `git rev-list --count HEAD` < 50, emit a warning: "Shallow clone detected — git history risk scores may be incomplete" and still compute with available history.
- **Renamed files**: `git log --follow` tracks renames. Use `--follow` for single-file queries to capture pre-rename history. Note: `--follow` only works with single files, not bulk — the per-file loop already handles this.
- **New files**: Files not previously in the repo will have 0 churn and 0 bug commits → risk "low". This is correct — no history means no historical risk signal.
- **Monorepos with very high churn**: Cap the table output to files with medium or high risk only. Low-risk files are the default — listing them all adds noise without signal. If all files are low-risk, output: "All changed files have low historical risk (no recent bug-related commits)."

### Performance

One `git log` call per file. For typical diffs (< 50 files), this adds < 2 seconds. For large-diff chunked mode, this runs once in Phase A (global context), not per chunk.

### Output

Include in context packet as a "Historical Risk" section:

```
## Historical Risk (git history, last 6 months)
| File | Churn | Bug Commits | Last Bug | Risk |
|------|-------|-------------|----------|------|
| src/auth/session.py | 14 | 3 | 12 days ago | high |
| src/api/orders.py | 6 | 1 | 45 days ago | medium |

Low-risk files (4) omitted — no recent bug-related commits.
```

### Interaction with existing pipeline

- **Step 1.5 (large-diff mode)**: Historical risk should feed into file risk tiering. A file classified as Tier 2 (standard) by path heuristics but with high historical risk should be promoted to Tier 1 (critical). Add to the Tier 1 criteria: "Historical risk = high".
- **Step 2h (context packet)**: Add "Historical Risk" as a new section between complexity scores and language standards.
- **Step 2-L Phase A**: Include git history risk as part of the lightweight global context (~1-2k tokens). It's per-file, not per-chunk, so it runs once globally.
- **Explorer prompts**: No changes needed — explorers receive the context packet which will now include historical risk. The data speaks for itself.

### Files to create

- `skills/codereview/scripts/git-risk.sh` — Git history risk scoring script

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2i: "Run `scripts/git-risk.sh`, include output in context packet." Add to Step 2h context packet, Step 2-L Phase A, and Step 1.5c Tier 1 promotion criteria.
- `skills/codereview/references/design.md` — Add rationale entry to design decisions table
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: git history available, shallow clone, all files low-risk, file promotion from Tier 2 to Tier 1, script failure fallback

### Effort: Small

---

## Feature 2: Test Coverage Data Integration

**Goal:** Collect actual test coverage data (line/function coverage) for changed files using language-native tools and feed it as deterministic context to explorers. This replaces inference-based coverage guessing with measured data.

### Where it fits

New sub-step **2j** in Step 2 (Gather Context), after git history risk (2i). Runs coverage tools as a context-gathering step (not a deterministic scan in Step 3, because coverage data is context for AI passes, not a finding source itself).

### Language detection and tools

Detect from `CHANGED_FILES` extensions — same pattern as Step 2f (standards loading):

| Language | Detection | Coverage Tool | Existing Data Check | Generate Command | Output Format |
|----------|-----------|---------------|---------------------|-----------------|---------------|
| Go | `.go` files | `go tool cover` | `cover.out`, `coverage.out` | `go test -coverprofile=/tmp/codereview-cover.out ./...` | Per-function coverage % |
| Python | `.py` files | `coverage` | `.coverage`, `coverage.json`, `htmlcov/` | `coverage run -m pytest && coverage json -o /tmp/codereview-cover.json` | Per-file line coverage % |
| Rust | `.rs` files | `cargo-tarpaulin` or `cargo-llvm-cov` | `tarpaulin-report.json`, `lcov.info` | `cargo tarpaulin --out json --output-dir /tmp/codereview-cover/` | Per-file line coverage % |
| TypeScript/JS | `.ts`/`.js`/`.tsx`/`.jsx` files | `c8`, `nyc`, or `jest` | `coverage/`, `.nyc_output/`, `coverage-final.json` | `npx c8 report --reporter=json --reports-dir=/tmp/codereview-cover/` | Per-file line/branch coverage % |

### Tool detection order

For each language, try tools in preference order (first available wins):

- **Go**: `go tool cover` (stdlib, always available with Go)
- **Python**: `coverage` (most common) → `pytest` with `--cov` flag (pytest-cov plugin)
- **Rust**: `cargo-tarpaulin` → `cargo-llvm-cov` (either may be installed)
- **TypeScript/JS**: `c8` → `nyc` → `jest --coverage` (check `package.json` scripts/devDependencies for hints)

### Design decisions

- **Best-effort**: If no coverage tool is available or no existing coverage data exists, skip with `tool_status` note. Never fail the review because of missing coverage.
- **Use existing coverage data first**: Check for pre-computed coverage artifacts before running tests. Running the full test suite is expensive and may not be appropriate during a review. The check order for each language is listed in the "Existing Data Check" column above.
- **Run tests only if configured**: Add optional `coverage.run_tests: true` config flag (default `false`). When false, only parse existing coverage data. When true, run the test suite to generate fresh coverage. When true, apply a timeout (default 5 minutes, configurable via `coverage.test_timeout`) to prevent the review from hanging on slow test suites.
- **Scope output to changed files**: Parse full coverage report but only include data for files in `CHANGED_FILES` — keeps context compact.
- **Coverage ≠ correctness**: Include a reminder in the explorer context: "Coverage data shows which lines are executed by tests. It does NOT indicate correctness — 100% coverage can still have bugs. Use as additional context, not as a quality verdict."
- **Multi-language repos**: A project may have both Go and Python files. Run coverage detection for each detected language independently. Each gets its own `tool_status` entry.
- **Test file exclusion**: Coverage for test files themselves is not useful. Filter out files matching test patterns (`test_*.py`, `*_test.go`, `*.test.ts`, etc.) from the coverage output.

### Edge cases

- **No coverage data and `run_tests: false` (default)**: Skip silently with `tool_status: "skipped"` and note: "No existing coverage data found. Set `coverage.run_tests: true` to generate."
- **Coverage tool available but tests fail**: If `run_tests: true` and the test suite fails, report partial coverage from whatever ran. Set `tool_status: "partial"` with note about test failures. Do not block the review.
- **Generated files in CHANGED_FILES**: Coverage for generated files (`.pb.go`, `.generated.ts`) is meaningless. Exclude files matching patterns from `ignore_paths` config.
- **Monorepo with multiple languages**: Each language's coverage runs independently. A single file can only be one language, so there's no overlap.
- **Stale coverage data**: Existing coverage artifacts may be from a previous branch or old commit. Check the artifact's mtime against HEAD's commit time. If the artifact is older than the most recent commit in CHANGED_FILES, add a warning: "Coverage data may be stale (predates recent changes)."

### Output

Include in context packet as a "Test Coverage" section:

```
## Test Coverage (measured, from coverage tool)
| File | Line Coverage | Uncovered Functions | Tool |
|------|--------------|---------------------|------|
| src/auth/session.py | 72% | validate_token, refresh_session | coverage.py |
| src/api/orders.py | 45% | cancel_order, bulk_update | coverage.py |
| src/utils/format.py | 91% | — | coverage.py |

Note: Coverage shows test execution, not correctness. Review all code thoroughly regardless of coverage.
```

### Interaction with existing pipeline

- **Step 2h (context packet)**: Add "Test Coverage" as a new section after historical risk. Include the coverage table and the "coverage ≠ correctness" reminder.
- **Step 2-L Phase A (large-diff mode)**: Include coverage data in the lightweight global context. Only output files with < 50% coverage to keep token budget compact. Full coverage table goes to Phase B (per-chunk context) for files in that chunk.
- **Test-adequacy explorer**: Receives measured coverage data alongside its own inference-based analysis. The prompt should instruct: "When measured coverage data is available, use it as the primary signal for which functions are untested. Your investigation should focus on *what kind* of tests are missing (unit vs integration vs e2e) and *what behaviors* are untested, not just *which files* lack coverage."
- **Spec-verification pass**: If a requirement maps to a file with 0% coverage, the `test_coverage.status` for that requirement should be set to `missing` with higher confidence.
- **`tool_status` keys**: `coverage_go`, `coverage_python`, `coverage_rust`, `coverage_typescript` — each with `status`, `version` (tool version), `finding_count` (number of files with data), and `note`.

### Implementation: `scripts/coverage-collect.py`

This feature is **entirely deterministic** — tool detection, invocation, output parsing, and filtering are all mechanical. Implemented as a Python script (JSON parsing is native, subprocess management is clean, and parsing 4 different tool output formats is far more maintainable in Python than bash+jq). Python 3 stdlib only, no external dependencies.

**Interface:**
```bash
echo "$CHANGED_FILES" | python3 scripts/coverage-collect.py \
  [--run-tests] [--timeout 300] > /tmp/codereview-coverage.json
```

**Input:** Newline-delimited file paths on stdin. Optional flags for test execution.

**Output:** JSON object on stdout:
```json
{
  "languages_detected": ["python", "go"],
  "coverage_data": [
    { "file": "src/auth/session.py", "line_coverage": 72, "uncovered_functions": ["validate_token", "refresh_session"], "tool": "coverage.py" },
    { "file": "src/api/orders.py", "line_coverage": 45, "uncovered_functions": ["cancel_order", "bulk_update"], "tool": "coverage.py" }
  ],
  "tool_status": {
    "coverage_python": { "status": "ran", "version": "7.4.0", "finding_count": 3, "note": null },
    "coverage_go": { "status": "not_installed", "version": null, "finding_count": 0, "note": "go not found on PATH" }
  },
  "warnings": ["Coverage data may be stale (predates recent changes by 3 days)"]
}
```

**Script logic:**

1. Detect languages from file extensions in CHANGED_FILES
2. For each detected language, check for existing coverage artifacts (in order listed in "Existing Data Check" column)
3. If `--run-tests` is set and no existing data found, run the test suite with the coverage tool (with `--timeout` enforcement via `timeout` command)
4. Parse coverage output (native JSON parsing for Python/Rust/TS tools, text parsing for Go's `cover -func` output)
5. Filter to CHANGED_FILES only, exclude test files
6. Check staleness: compare artifact mtime against most recent commit in CHANGED_FILES
7. Output JSON to stdout

**The agent's role:** Run the script, read the JSON, format the context packet table, add the "coverage ≠ correctness" reminder. The agent does NOT detect tools, parse coverage output, or compute staleness — that's the script's job. The agent also updates the test-adequacy explorer prompt to reference the measured data.

### Files to create

- `skills/codereview/scripts/coverage-collect.py` — Coverage data collection and parsing script

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2j: "Run `scripts/coverage-collect.py`, include output in context packet." Add to Step 2h and Step 2-L Phase A.
- `skills/codereview/prompts/reviewer-test-adequacy-pass.md` — Add instructions for using measured coverage data when available
- `docs/CONFIGURATION.md` — Add `coverage` config section (`run_tests`, `test_timeout`)
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: each language, no tool, stale data, run_tests true + test failure, multi-language, script failure fallback
- `skills/codereview/findings-schema.json` — Add `tool_status` keys for coverage tools

### Effort: Medium

---

## Feature 3: Finding Lifecycle & Fingerprinting

**Goal:** Track findings across review runs so users can see what's new vs recurring, and dismiss findings that are intentionally not fixed. Prevents noise on repeated reviews.

### Fingerprinting

Generate a stable fingerprint for each finding that survives minor code changes:

```
fingerprint = sha256(file_path + ":" + pass + ":" + severity + ":" + normalize(summary_key_terms))[:12]
```

Use SHA-256 truncated to 12 hex characters (48 bits). This gives ~281 trillion possible values — collision probability is negligible for review artifact sizes (< 1000 findings per run).

**What's in the fingerprint:**
- `file_path` — the file the finding is in
- `pass` — the explorer pass that found it (correctness, security, etc.)
- `severity` — low/medium/high/critical
- Key terms extracted from `summary`, normalized in this order:
  1. Lowercase everything
  2. Strip punctuation
  3. Strip stop words (a, the, is, in, of, to, for, and, or, but, not, with, this, that)
  4. Stem common suffixes: `-ing`, `-ed`, `-tion`, `-ment`, `-ness`, `-ly`, `-ble`, `-er`, `-est` (simple regex `re.sub(r'(ing|ed|tion|ment|ness|ly|ble|er|est)$', '', word)` — no NLP library needed)
  5. Collapse whitespace
  6. Sort tokens alphabetically
  7. Join with spaces

  The suffix stemming bumps match rate from ~70-80% to ~85-90%. Example: "missing" → "miss", "limiting" → "limit", "validation" → "valida" — morphological variants converge.

**What's NOT in the fingerprint:**
- Line number (shifts when code changes above the finding)
- Exact summary text (wording varies between runs)
- Confidence (may change between runs)
- `source` (the same issue found by deterministic tool and AI should match)

**Normalization example:**
- Summary: "Missing rate limit on the token refresh endpoint" → lowercase → strip stop → stem → sort → `endpoint limit miss rate refresh token` → fingerprint input: `src/auth/session.py:security:medium:endpoint limit miss rate refresh token`
- Next run summary: "Token refresh endpoint lacks rate limiting" → lowercase → strip stop → stem → sort → `endpoint lack limit rate refresh token` → different fingerprint (because "miss" ≠ "lack"), but the fuzzy match catches it (5/6 = 83% overlap)
- Third run summary: "No rate-limiting on token refresh" → lowercase → strip stop → stem → sort → `limit no rate refresh token` → exact fingerprint differs, fuzzy match: 4/5 = 80% overlap with run 1 → matched as recurring

**Fingerprint stability concern:** Since AI-generated summaries vary between runs, fingerprints based on summary key terms will have imperfect matching even with suffix stemming. This is a known tradeoff — we expect ~85-90% exact match rate with stemming. To catch the remaining ~10-15%, Step 5 also does a secondary fuzzy match: if two findings share the same `file_path + pass + severity` and their stemmed key term sets overlap by >= 60%, treat them as the same finding. The fuzzy match is a fallback — the exact fingerprint is the primary match.

**The 60% threshold should be data-driven.** See "Test fixtures" section below for the fixture file used to tune this value.

### Lifecycle statuses

| Status | Meaning | Surfaced in report? | In JSON output? |
|--------|---------|---------------------|-----------------|
| `new` | First time this finding appears | Yes | Yes |
| `recurring` | Found in a previous review and still present | Yes | Yes |
| `rejected` | User dismissed — intentionally not fixing | No (filtered from report) | Yes (in `suppressed_findings` array) |
| `deferred` | Acknowledged, not fixing now — resurfaces when file is modified | No (filtered, unless file touched) | Yes (in `suppressed_findings` array) |

Suppressed findings are excluded from the report and from `findings[]` but included in a separate `suppressed_findings[]` array in the JSON envelope for auditability.

### Suppressions file

`.codereview-suppressions.json` in repo root (committed to git so the team shares suppressions):

```json
{
  "version": 1,
  "suppressions": [
    {
      "fingerprint": "a1b2c3d4e5f6",
      "status": "rejected",
      "reason": "Intentional design choice — documented in ADR-007",
      "created_at": "2026-03-25T14:00:00Z",
      "created_by": "runger",
      "file": "src/auth/session.py",
      "pass": "security",
      "severity": "medium",
      "summary_snippet": "Missing rate limit on token refresh"
    },
    {
      "fingerprint": "d4e5f6a7b8c9",
      "status": "deferred",
      "reason": "Will address in v1.3 — tracked in JIRA-456",
      "created_at": "2026-03-25T14:00:00Z",
      "created_by": "runger",
      "file": "src/api/orders.py",
      "pass": "testing",
      "severity": "medium",
      "summary_snippet": "Missing integration test for bulk update",
      "deferred_scope": "pass",
      "expires_at": "2026-04-25T00:00:00Z"
    }
  ]
}
```

The `pass` and `severity` fields enable the fuzzy match fallback when the fingerprint doesn't match exactly (e.g., summary wording changed between runs).

**Suppression-specific fields:**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `fingerprint` | Yes | — | SHA-256 fingerprint (12 hex chars) |
| `status` | Yes | — | `rejected` or `deferred` |
| `reason` | Yes | — | Human-readable explanation |
| `created_at` | Yes | — | ISO 8601 timestamp |
| `created_by` | No | — | Who created the suppression |
| `file` | Yes | — | File path the finding was in |
| `pass` | Yes | — | Explorer pass (for fuzzy matching) |
| `severity` | Yes | — | Finding severity (for fuzzy matching) |
| `summary_snippet` | Yes | — | First ~80 chars of finding summary (human reference) |
| `deferred_scope` | No | `"file"` | **Deferred findings only.** Controls when the finding resurfaces: `"file"` = when file is in CHANGED_FILES, `"pass"` = when file is changed AND the same explorer pass fires on it, `"exact"` = only on exact fingerprint match (effectively permanent deferral until the exact same finding reappears) |
| `expires_at` | No | `null` | ISO 8601 date string. If present and in the past, the suppression is ignored and the finding resurfaces. Enables "defer for N days" without permanent dismissal. |

### How suppressions are created

Two methods:

**1. CLI subcommand** — suppress a finding by its ID from the most recent review:
```bash
/codereview suppress <finding-id> --status rejected --reason "explanation"
/codereview suppress <finding-id> --status deferred --reason "tracked in JIRA-456"
/codereview suppress <finding-id> --status deferred --defer-days 30 --reason "revisit next sprint"
/codereview suppress <finding-id> --status deferred --defer-scope pass --reason "only relevant if auth pass fires"
```

Flags:
- `--defer-days N` — convenience flag that auto-computes `expires_at` as now + N days. Only valid with `--status deferred`.
- `--defer-scope file|pass|exact` — sets `deferred_scope`. Default `file`. Only valid with `--status deferred`.

This reads the finding from the most recent `.agents/reviews/*.json`, extracts its fingerprint, and appends to `.codereview-suppressions.json`.

**2. Interactive post-review** — after presenting the review, the skill asks: "Would you like to suppress any findings? Provide finding IDs (e.g., `suppress correctness-a3f1-42 --status deferred --reason 'reason'`)."

### Where it fits in the pipeline

**Step 5 (Merge, Deduplicate, and Classify)** — new sub-step 5a.5 between enrichment (5a) and tier classification (5b):

1. Generate fingerprint for each finding in current run
2. Load previous review artifact: most recent `.agents/reviews/*.json` matching the same `scope` and `base_ref` (e.g., if current review is `--base main`, load the most recent `scope: "branch"` review). If no matching previous review exists, all findings are `new`.
3. Load `.codereview-suppressions.json` (if exists, fail-open: malformed file → warn and skip suppressions)
4. For each current finding:
   a. **Exact fingerprint match** against previous findings → `recurring`
   b. **Fuzzy match** (same file + pass + severity + >= 60% key term overlap) against previous findings → `recurring`
   c. No match → `new`
5. For each current finding:
   a. **Exact fingerprint match** against suppressions → candidate suppression
   b. **Fuzzy match** against suppressions (same file + pass + severity + >= 60% overlap) → candidate suppression
   c. **Expiry check**: If the matched suppression has `expires_at` and it's in the past, ignore the suppression — finding resurfaces as `new` or `recurring`.
   d. If suppressed as `rejected`: always suppress regardless of file changes.
   e. If suppressed as `deferred`: apply `deferred_scope` rules:
      - `"file"` (default): resurface if `file` is in current `CHANGED_FILES`. Otherwise keep suppressed.
      - `"pass"`: resurface only if `file` is in `CHANGED_FILES` AND the current finding's `pass` matches the suppression's `pass`. Otherwise keep suppressed.
      - `"exact"`: resurface only on exact fingerprint match. If matched via fuzzy match only, keep suppressed.
6. Move suppressed findings to `suppressed_findings[]` in the JSON envelope. Keep `findings[]` clean for report generation.
7. Include `lifecycle_status` and `fingerprint` in each finding's output (both in `findings[]` and `suppressed_findings[]`).

### Report changes

- Add lifecycle badge next to each finding in tiered sections: `[NEW]` or `[RECURRING]`
- Add a "Suppressed Findings" section at the bottom:
  ```
  ## Suppressed Findings (3)
  - 2 rejected, 1 deferred
  - To review suppressions, see `.codereview-suppressions.json`
  - To un-suppress: remove the entry from `.codereview-suppressions.json`
  ```
- Add post-review action hint: "To suppress a finding: `/codereview suppress <finding-id> --status rejected --reason 'reason'`"

### Interaction with existing pipeline

- **Step 5a item 2 (Assign `id`)**: The existing finding ID (`<pass>-<file-hash>-<line>`) is separate from the fingerprint. The `id` is for human reference within a single review. The `fingerprint` is for cross-review tracking. Both fields coexist.
- **Step 5a item 5 (Deduplicate)**: Deduplication happens BEFORE fingerprinting. Fingerprints are assigned to the deduplicated findings.
- **`findings-schema.json`**: Add `fingerprint` (string, 12 hex chars), `lifecycle_status` (enum: new/recurring), and `suppressed_findings` (array, same shape as `findings` but with `lifecycle_status` of rejected/deferred) to the schema. `lifecycle_status` is optional for backward compatibility — existing reviews without it are valid.
- **`validate_output.sh`**: Add check: if `lifecycle_status` is present, it must be one of `new`, `recurring`. If `suppressed_findings` is present, each entry must have `lifecycle_status` of `rejected` or `deferred`.

### Edge cases

- **First review (no previous artifact)**: All findings are `new`. No suppressions file → no suppressions. This is the default experience — lifecycle features are invisible until the second review.
- **Suppressions file is malformed JSON**: Warn and skip suppressions entirely. Do not block the review. (Note: if we ever move to JSONL format, adopt per-line resilience — skip malformed lines, not the entire file.)
- **Finding changes severity between runs**: The fingerprint includes severity, so a finding that was `medium` in run 1 and `high` in run 2 will be `new` (different fingerprint). The fuzzy match (same file + pass + >= 60% key term overlap) may still catch it. This is acceptable — a severity change is worth re-evaluating.
- **File renamed**: Finding's `file_path` changes → new fingerprint → `new`. This is correct — the reviewer should re-evaluate in the new context.
- **Suppression for a file that no longer exists**: Suppression stays in `.codereview-suppressions.json` but never matches. Harmless — no cleanup needed. Users can periodically prune stale suppressions manually.
- **Expired suppression**: `expires_at` is in the past → suppression is ignored, finding resurfaces as `new` or `recurring` (depending on whether it was in the previous review). The expired suppression entry stays in the file — it's inert but harmless.
- **Deferred with `deferred_scope: "pass"` but pass doesn't fire**: The finding stays suppressed even though the file is in CHANGED_FILES. This is intentional — e.g., a deferred security finding shouldn't resurface when only the correctness pass reviews the file for a typo fix.
- **Deferred with `deferred_scope: "exact"` and summary wording changed**: The finding stays suppressed (exact fingerprint didn't match, fuzzy match is not sufficient for `exact` scope). This is the strictest deferral — use only when you want near-permanent suppression.
- **`--raw` flag with enriched input**: Harmless — `--raw` skips validation of enrichment fields, so enriched input still works (the fields are present but not required).

### Implementation: `scripts/lifecycle.py`

This feature is **entirely deterministic** — fingerprint computation, normalization, comparison, suppression lookup, and lifecycle tagging are all mechanical. Implemented as a Python script (SHA-256 hashing, JSON manipulation, set operations for fuzzy matching).

**Interface:**
```bash
python3 scripts/lifecycle.py \
  --findings /tmp/codereview-enriched.json \
  --previous-review .agents/reviews/2026-03-24-branch-main.json \
  --suppressions .codereview-suppressions.json \
  --changed-files /tmp/codereview-changed-files.txt \
  --scope branch --base-ref main \
  > /tmp/codereview-lifecycle.json

# If enrich-findings.py hasn't run yet (Feature 0 not implemented):
python3 scripts/lifecycle.py \
  --findings /tmp/codereview-judge-raw.json \
  --raw \
  > /tmp/codereview-lifecycle.json
```

**Input:**
- `--findings`: Current findings JSON. By default, expects enriched output from `enrich-findings.py` (with `action_tier`, `source`, `id` fields). With `--raw`, accepts raw judge output directly (skips expecting enrichment fields). The `--raw` flag enables Feature 3 to be built and tested independently of Feature 0. Remove `--raw` once Feature 0 lands.
- `--previous-review`: Path to most recent matching review artifact (optional — if absent, all findings are `new`). The script can also auto-discover this by scanning `.agents/reviews/` for the most recent file matching the given `--scope` and `--base-ref`.
- `--suppressions`: Path to suppressions file (optional — if absent or malformed, no suppressions applied)
- `--changed-files`: Newline-delimited file list (for deferred-finding resurfacing logic)
- `--scope` and `--base-ref`: Used to auto-discover previous review if `--previous-review` not provided

**Atomic writes:** When `lifecycle.py` writes output files (review artifacts, suppressions updates), it uses the temp-file-plus-rename pattern: write to a temporary file in the same directory, then `os.rename()` to the final path. This prevents corrupted artifacts if the process is killed mid-write.

**Output:** JSON object on stdout — the same structure as the input findings, with three additions:
```json
{
  "findings": [
    { "...all existing fields...", "fingerprint": "a1b2c3d4e5f6", "lifecycle_status": "new" },
    { "...all existing fields...", "fingerprint": "d4e5f6a7b8c9", "lifecycle_status": "recurring" }
  ],
  "suppressed_findings": [
    { "...all existing fields...", "fingerprint": "x1y2z3w4v5u6", "lifecycle_status": "rejected", "suppression_reason": "Intentional design choice" }
  ],
  "lifecycle_summary": { "new": 5, "recurring": 2, "rejected": 1, "deferred": 0, "deferred_resurfaced": 0 }
}
```

**Script logic:**

1. Load current findings, previous review (if exists), suppressions (if exists). If `--raw`, skip validation of enrichment fields (`action_tier`, `source`, `id`).
2. For each current finding, compute fingerprint: `sha256(file + ":" + pass + ":" + severity + ":" + normalize(summary))[:12]`
3. Normalize summary: lowercase → strip punctuation → strip stop words → stem suffixes (-ing, -ed, -tion, -ment, -ness, -ly, -ble, -er, -est) → collapse whitespace → sort tokens → join
4. Match against previous findings:
   a. Exact fingerprint match → `recurring`
   b. Fuzzy match (same file + pass + severity + >= 60% stemmed word overlap) → `recurring`
   c. No match → `new`
5. Match against suppressions:
   a. Exact fingerprint match → candidate suppression
   b. Fuzzy match (same file + pass + severity + >= 60% stemmed word overlap) → candidate suppression
   c. Expiry check: if `expires_at` is set and in the past → ignore suppression, finding resurfaces
   d. `rejected` → always suppress
   e. `deferred` → apply `deferred_scope` rules (`file`/`pass`/`exact`)
6. Partition into `findings[]` and `suppressed_findings[]`
7. Compute `lifecycle_summary` counts (including `deferred_resurfaced` for deferred findings that re-entered `findings[]`)
8. Output JSON to stdout (atomic write if writing to file via `--output`)

**The agent's role:** Run the script after `enrich-findings.py`. The agent does NOT compute fingerprints, match findings, or evaluate suppressions — that's the script's job. The agent formats the lifecycle badges in the report and handles the interactive `suppress` subcommand (reading user input and appending to the suppressions file).

**The `suppress` subcommand** (`/codereview suppress <id> ...`) is also implemented as a script operation:

```bash
# Reject permanently
python3 scripts/lifecycle.py suppress \
  --review .agents/reviews/2026-03-25-branch-main.json \
  --finding-id "correctness-a3f1-42" \
  --status rejected \
  --reason "Intentional design choice" \
  --suppressions .codereview-suppressions.json

# Defer for 30 days, resurface only if same pass fires
python3 scripts/lifecycle.py suppress \
  --review .agents/reviews/2026-03-25-branch-main.json \
  --finding-id "testing-b2c3-18" \
  --status deferred \
  --defer-days 30 \
  --defer-scope pass \
  --reason "Will address in next sprint" \
  --suppressions .codereview-suppressions.json
```

This reads the finding from the review artifact, computes its fingerprint, and appends to the suppressions file using the atomic temp-file-plus-rename pattern. Pure data operation — no AI needed.

**The `--test-fixtures` subcommand** runs the fuzzy matching logic against the test fixture file for threshold tuning:

```bash
python3 scripts/lifecycle.py --test-fixtures tests/fixtures/fuzzy-match-pairs.json
```

Reports match/no-match accuracy and false positive/negative counts. Use this to tune the 60% overlap threshold — if it produces false positives, raise to 70%; if it misses obvious matches, lower to 50%.

### Test fixtures

`tests/fixtures/fuzzy-match-pairs.json` — 20+ pairs of findings for tuning the fuzzy match threshold. Must include:

| Category | Examples | Expected |
|----------|----------|----------|
| Same issue, different wording | "Missing rate limit" vs "No rate limiting" | MATCH |
| Same issue, different wording (stemming helps) | "Missing validation" vs "Validation is missing" | MATCH |
| Same issue, severity changed | medium "SQL injection" vs high "SQL injection" | NO MATCH (different fingerprint), but fuzzy MATCH (same file+pass) |
| Similar but different issues | "Missing null check in parse()" vs "Missing null check in render()" | NO MATCH (different function — key terms differ) |
| Same file, same pass, genuinely different | "Race condition in connect()" vs "Buffer overflow in connect()" | NO MATCH (different key terms despite same file+pass) |
| Near-duplicate with extra context | "Hardcoded secret" vs "Hardcoded secret in config file" | MATCH (subset overlap > 60%) |

Format:
```json
[
  {
    "finding_a": { "file": "...", "pass": "...", "severity": "...", "summary": "..." },
    "finding_b": { "file": "...", "pass": "...", "severity": "...", "summary": "..." },
    "expected_exact_match": false,
    "expected_fuzzy_match": true,
    "note": "Same issue, different wording"
  }
]
```

### Files to create

- `skills/codereview/scripts/lifecycle.py` — Fingerprinting, lifecycle tagging, suppression management, suppress subcommand, test-fixtures runner
- `tests/fixtures/fuzzy-match-pairs.json` — Fuzzy match test fixture (20+ pairs)

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 5a.5: "Run `scripts/lifecycle.py`, include output in report." Add `suppress` subcommand to Quick Start and Step 1.
- `skills/codereview/findings-schema.json` — Add `fingerprint`, `lifecycle_status` to finding schema, add `suppressed_findings` array to envelope
- `skills/codereview/references/report-template.md` — Add lifecycle badges, suppressed section, post-review action hint
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: first review, recurring, rejected, deferred, deferred-but-file-touched, deferred-scope (file/pass/exact), expired suppression, malformed suppressions file, fuzzy match, --raw mode, script failure fallback
- `skills/codereview/references/design.md` — Add rationale for fingerprinting approach, suffix stemming, fuzzy match tradeoff, deferred_scope design, atomic writes
- `docs/CONFIGURATION.md` — Document `.codereview-suppressions.json` format and `suppress` subcommand
- `skills/codereview/scripts/validate_output.sh` — Validate `lifecycle_status` values, validate `suppressed_findings` array

### Effort: Medium

---

## Feature 4 (Replacement): Named Expert Panel for Judge

**Goal:** Restructure the judge prompt as a panel of named experts who analyze findings sequentially, each with a distinct role. This improves reasoning quality at zero additional cost by forcing the judge through distinct analytical phases that can't be silently skipped.

**Replaces:** Multi-Model Council Review (deferred to research — same-family model variants don't provide true diversity, cross-provider requires mechanisms not yet available).

**Inspired by:** Kodus-AI's panel-of-experts pattern (see `docs/research-multi-model-council.md` Section 6). Their production code review platform uses named expert role-play within a single prompt to structure analysis, with a gatekeeper that pre-filters false positives.

### The Expert Panel

The judge's existing analysis steps (Steps 1-6 in `reviewer-judge.md`) are restructured as four named experts. The experts execute sequentially within a single prompt — each builds on the previous expert's output.

| Expert | Current Step(s) | Role | Key additions from Kodus-AI pattern |
|--------|----------------|------|-------------------------------------|
| **Gatekeeper** | Step 1 (existence check) | Pre-filter triage — eliminate obvious false positives before expensive analysis | Explicit auto-discard rules for the top false positive categories: phantom knowledge claims (finding references code that doesn't exist), speculative concerns ("might cause issues" without evidence), framework-guaranteed behavior, findings about code outside the diff scope |
| **Verifier** | Steps 2-3 (existence, contradiction, severity) | Evidence verification — for each finding that survived the gatekeeper, verify with Read/Grep | Must produce a verification annotation per finding: `verified` (code exists, evidence is real), `unverified` (couldn't confirm — downgrade confidence by 0.15), `disproven` (evidence contradicts the finding — drop it) |
| **Calibrator** | Step 4 (cross-explorer synthesis) | Severity and confidence calibration — apply calibration rules, resolve contradictions between explorers | Cross-explorer root cause grouping (multiple findings about the same underlying issue → merge), contradiction resolution (two explorers disagree → surface the disagreement as a finding), severity adjustment based on verification annotations |
| **Synthesizer** | Steps 5-6 (spec gaps, verdict) | Final synthesis — produce verdict, strengths, spec_gaps, and the final findings list | Produces the JSON output. Cannot add new findings — only merge, re-rank, and annotate. The synthesizer's job is to produce a coherent report, not to re-investigate. |

### What changes in the judge prompt

The existing `reviewer-judge.md` prompt is restructured but the *content* stays the same. The change is organizational:

**Before (flat instruction list):**
```
Step 1: Check existence...
Step 2: Check contradiction...
Step 3: Calibrate severity...
Step 4: Cross-explorer synthesis...
Step 5: Spec gaps...
Step 6: Verdict...
```

**After (named expert sequence):**
```
## Expert Panel

You will analyze the explorer findings as a sequence of four experts.
Each expert produces an annotated output that the next expert receives.

### Gatekeeper (Pre-Filter)
<existence check instructions + auto-discard rules>
Output: findings[] with gatekeeper_action: "keep" | "discard" + reason

### Verifier (Evidence Check)
<Read/Grep verification instructions>
Output: findings[] with verification: "verified" | "unverified" | "disproven"

### Calibrator (Severity + Synthesis)
<calibration rules, cross-explorer grouping, contradiction resolution>
Output: findings[] with final severity, confidence, root_cause_group

### Synthesizer (Verdict)
<spec gaps, strengths, verdict reasoning>
Output: final JSON response
```

### Gatekeeper auto-discard rules

These are the top false positive categories observed in code review (adapted from Kodus-AI's Edward expert):

1. **Phantom knowledge** — Finding references code, functions, or variables that don't exist in the diff or codebase. Discard with reason: "References non-existent code."
2. **Speculative concern** — Finding says "might cause issues" or "could lead to problems" without concrete evidence of what breaks and when. Discard with reason: "Speculative — no concrete failure mode."
3. **Framework-guaranteed** — Finding flags a concern that the framework handles by default (e.g., JSON response format in FastAPI, CSRF protection in Django, auto-escaping in React). Discard with reason: "Framework handles this."
4. **Outside diff scope** — Finding is about code that was not changed in this diff and has no interaction with changed code. Discard with reason: "Outside diff scope."
5. **Style/formatting only** — Finding is about code style, naming conventions, or formatting that a linter should handle. Discard with reason: "Style concern — defer to linter."
6. **Duplicate of deterministic** — Finding restates what a deterministic tool (semgrep, shellcheck, etc.) already caught. Discard with reason: "Already caught by [tool]."

### What stays the same

- The judge still receives all explorer findings and deterministic scan results
- The judge still uses Read/Grep/Glob to verify findings
- The output JSON shape is unchanged
- The adversarial validation protocol is unchanged (existence check, contradiction check, severity calibration)
- Spec verification synthesis (Steps 5a-5e) is unchanged

### Files to modify

- `skills/codereview/prompts/reviewer-judge.md` — Restructure as named expert panel. Add gatekeeper auto-discard rules. Add verification annotations. No new content — reorganization of existing steps.
- `skills/codereview/references/design.md` — Add rationale for named expert panel pattern
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: gatekeeper discards phantom finding, verifier downgrades unverified finding, calibrator merges root cause group

### Effort: Small

---

## Feature 4 (Original — DEFERRED): Multi-Model Council Review

> **STATUS: DEFERRED TO RESEARCH.** Same-family model variants (Sonnet↔Opus) don't provide genuine diversity — they share training data. True cross-provider council (Claude + GPT + Gemini) requires spawning mechanisms not available in the current Task tool. See `docs/research-multi-model-council.md` for the full decision rationale. The design below is preserved for future reference.

**Goal:** Run the review across multiple models and compare findings. When two models independently flag the same issue, confidence is very high. When they disagree, the disagreement itself is a signal worth attention. This is the single biggest quality improvement available.

### Design approach

**Options considered:**

| Option | Approach | Cost | Pros | Cons |
|--------|----------|------|------|------|
| A | Full pipeline × 2 models | 2x everything | Maximum independence | Doubles cost AND latency; both pipelines catch same obvious issues |
| B | Explorers × 2 models, single judge | 2x explorers, 1x judge | Diversity where it matters (exploration), centralized synthesis | Still doubles explorer cost |
| C | Standard pipeline, then council validation on findings | 1x pipeline + 1 council call | Cheapest; leverages existing `/council` skill | Second model doesn't investigate code, only reviews findings |

**Chosen approach: Option B** — Run each core explorer pass with two models in parallel, then feed all findings to a single cross-model judge. This gives diversity at the exploration stage (where different models notice different things) while keeping synthesis centralized. The judge already performs adversarial validation — extending it to cross-model synthesis is a natural fit.

Option C was tempting (cheapest) but the second model needs to *investigate the code itself*, not just review someone else's findings. An LLM reviewing findings without reading the code can only assess plausibility, not correctness.

### Architecture

```
Step 4 (council mode):

  Explorer Wave (all launched in parallel, single message):
  ├── correctness (model A)    correctness (model B)
  ├── security (model A)       security (model B)
  ├── reliability (model A)    reliability (model B)
  ├── test-adequacy (model A)  test-adequacy (model B)
  ├── (extended passes: model A only — not doubled)
  └── (spec-verification: model A only — not doubled)
           │
           ▼
  Cross-Model Judge (single agent, model A):
  ├── Match findings across models (file + pass + summary similarity)
  ├── Corroborated findings (both models) → confidence boost (+0.15, cap 0.98)
  ├── Model-A-only findings → "single-source", standard adversarial validation
  ├── Model-B-only findings → "single-source", standard adversarial validation
  ├── Contradictions → severity floor "medium", flag for human attention
  └── All standard judge duties: verdict, strengths, spec_gaps, dedup, tier
```

### Model selection

Default pairing: the session's default model (model A) + a configurable second model (model B).

| Config value | Model B | Notes |
|-------------|---------|-------|
| `opus` | Claude Opus | Same vendor, higher capability — catches subtler issues |
| `sonnet` | Claude Sonnet | Use when session default is Opus — cheaper second opinion |
| `haiku` | Claude Haiku | Same vendor, fast/cheap — diversity through capability difference |

**Cross-vendor models** (e.g., OpenAI) are out of scope for v1.2. The Task tool's `model` parameter only supports Claude models. Cross-vendor would require a different spawning mechanism (e.g., API calls or the Codex CLI). This can be added in v1.3 if needed.

Configurable via `.codereview.yaml`:

```yaml
council:
  enabled: false          # opt-in (default off — doubles core explorer cost)
  model_b: "opus"         # second model for council mode
  passes: "core"          # "core" (4 passes doubled) or "all" (8 passes doubled)
```

Or CLI flag: `--council` enables with defaults, `--council-model opus` overrides model B.

### How findings are matched across models

The judge needs to determine which findings from model A and model B refer to the same issue. This uses the same logic as Feature 3's fingerprinting (if implemented), or a simpler inline approach:

**Matching criteria (ordered by strength):**
1. **Exact file + line + pass** — same file, same line (±5 lines tolerance), same pass → corroborated
2. **Same file + pass + similar summary** — same file, same pass, summary key terms overlap >= 50% → likely corroborated (judge verifies)
3. **Same file, different pass** — e.g., model A flags as correctness, model B flags as reliability for the same code → judge merges into one finding with the more appropriate pass
4. **No match** — single-source finding

**Important:** The judge performs this matching, not the orchestrator. The orchestrator labels each finding with `model_source: "A"` or `model_source: "B"` and passes all findings to the judge. The judge uses its understanding of the code to determine which findings are about the same issue.

### Extended passes

Extended passes (error-handling, api-contract, concurrency, spec-verification) run with **model A only** in council mode. Reasons:
- They're adaptive-skip and often don't run at all
- Doubling them adds cost without proportional benefit
- Core passes (correctness, security, reliability, test-adequacy) are where model diversity matters most — these run on every review and cover the broadest surface area

If `council.passes: "all"` is configured, ALL passes including extended are doubled. This is the expensive option for maximum coverage.

### Cross-model judge additions

The judge prompt gets an additional section when council mode is active:

```
## Cross-Model Synthesis (Council Mode)

You are receiving findings from two models (A and B) for each core pass.
Each finding includes a `model_source` field ("A" or "B").

For each finding, determine its cross-model status:

1. **Corroborated** — Both models flagged a similar issue (same file, same or
   adjacent line ±5, same pass, similar description).
   Boost confidence by +0.15 (cap at 0.98).
   In evidence, note: "Corroborated by both models."

2. **Single-source** — Only one model flagged this issue.
   Keep original confidence. In evidence, note: "Single-model finding (A/B only)."
   Apply EXTRA scrutiny in adversarial validation — single-source findings are
   more likely to be false positives. Verify with Read/Grep before keeping.

3. **Contradiction** — Models reached opposite conclusions about the same code
   (e.g., A says "this is safe", B says "this is vulnerable").
   Set severity floor to "medium". Include BOTH perspectives in evidence.
   In evidence, note: "Models disagree — requires human review."
   The contradiction itself is the finding — do not resolve it, surface it.

After cross-model synthesis, proceed with standard judge duties (dedup, verdict,
strengths, spec_gaps). The `model_source` field should be preserved in the
output for auditability but is not shown in the report unless contradicted.
```

### Interaction with existing pipeline

- **Step 3.5 (Adaptive skip)**: Adaptive skip signals are evaluated ONCE (not per model). If a pass is skipped, it's skipped for both models.
- **Step 4a (Explorer launch)**: In council mode, the orchestrator launches 8 core explorer Tasks instead of 4, all in a single message (parallel). Each Task's `model` parameter is set to either the default (model A) or the configured `model_b`. The prompt content is identical — only the model differs.
- **Step 4b (Judge)**: The judge receives all findings from both models, labeled with `model_source`. The judge prompt includes the cross-model synthesis section above. The judge model is always the session default (model A) — it needs to be the strongest model to synthesize correctly.
- **Step 5 (Merge)**: The orchestrator adds `council_mode: true` to the JSON envelope. The `model_source` field on each finding is preserved through merge.
- **Large-diff chunked mode (Step 4-L)**: In council mode + chunked mode, each chunk gets 2x core explorer Tasks. This compounds: a 6-chunk review with council mode generates 6 × 8 = 48 core explorer Tasks plus extended passes. This may exceed practical limits. **Safeguard:** In chunked mode, cap total Task calls at 36. If the execution matrix exceeds this, fall back to council on Tier 1 chunks only (Tier 2/3 get single-model review).
- **Feature 3 interaction (fingerprinting)**: If Feature 3 is implemented, the judge's cross-model matching can use fingerprints for more stable matching. If Feature 3 is not yet implemented, the judge uses the inline matching criteria described above.

### Token budget

| Component | Standard mode | Council mode (core) | Council mode (all) |
|-----------|--------------|--------------------|--------------------|
| Core explorer Tasks | 4 | 8 | 8 |
| Extended explorer Tasks | 0-4 | 0-4 (unchanged) | 0-8 (doubled) |
| Judge Tasks | 1 | 1 | 1 |
| **Total Task calls** | **5-9** | **9-13** | **9-17** |
| **Approx. token cost multiplier** | **1x** | **~1.8x** | **~2.5x** |

The token cost multiplier is less than 2x because the judge, context gathering, and deterministic scans are unchanged.

### Edge cases

- **Model B unavailable or rate-limited**: If a model B explorer Task fails, log a warning and proceed with model A findings only for that pass. The review doesn't fail — it degrades to single-model for that pass. Set `tool_status` for the affected pass to note the degradation.
- **Model B produces invalid output**: The judge should handle this the same way it handles any malformed explorer output — skip the findings, note it in evidence.
- **All model B explorers fail**: The review becomes a standard single-model review. Set `council_mode: "degraded"` in the envelope instead of `true`.
- **Council + spec verification**: Spec verification runs as a single global pass (model A only). It produces structured `requirements` data that can't be meaningfully compared across models — the requirement extraction is deterministic once the spec is parsed.

### Files to modify

- `skills/codereview/SKILL.md` — Add `--council` and `--council-model` flags to Quick Start and Step 1 argument parsing. Add council mode to Step 4a (doubled core explorers), Step 4b (cross-model judge synthesis). Add Task cap safeguard for chunked + council.
- `skills/codereview/prompts/reviewer-judge.md` — Add "Cross-Model Synthesis" section (conditional on council mode)
- `skills/codereview/findings-schema.json` — Add `model_source` field (string, "A"/"B") to findings, add `council_mode` (boolean or string "true"/"degraded"/"false") to envelope
- `docs/CONFIGURATION.md` — Add `council` config section with `enabled`, `model_b`, `passes`
- `skills/codereview/references/design.md` — Replace "Future: Multi-Model Consensus (v2)" with full design section including architecture, options considered, tradeoffs
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: council standard, council + chunked, model B failure, contradiction detection, corroboration boost, all passes mode
- `skills/codereview/references/report-template.md` — Add model attribution format, corroboration badge `[CORROBORATED]`, contradiction badge `[MODELS DISAGREE]`
- `skills/codereview/scripts/validate_output.sh` — Validate `council_mode` field, validate `model_source` values when present

### Effort: Large

---

## Implementation Order

| Order | Feature | Effort | Why this order |
|-------|---------|--------|----------------|
| 0 | Extract Existing Pipeline into Scripts | Medium | Establishes the scripting pattern (`scripts/` directory, interface conventions, fallback behavior). Features 1-3 follow the same pattern. Also delivers immediate value by making Steps 3 and 5 deterministic. |
| 1 | Git History Risk Scoring | Small | Quick win, follows the `scripts/` pattern established by Feature 0 |
| 2 | Test Coverage Data Integration | Medium | Deterministic signal, multi-language support, follows `scripts/` pattern |
| 3 | Finding Lifecycle & Fingerprinting | Medium | Depends on `enrich-findings.py` from Feature 0 (lifecycle runs after enrichment). Also follows `scripts/` pattern. |
| ~~4~~ | ~~Multi-Model Council Review~~ | ~~Large~~ | **DEFERRED to research.** Same-family model variants (Sonnet↔Opus) don't provide true diversity — they share training data. True multi-model (cross-provider) requires spawning mechanisms not yet available. Full design preserved in Feature 4 section below for future reference. See `docs/research-multi-model-council.md` for the decision rationale. |
| 4 (replacement) | Named Expert Panel for Judge | Small | Free quality improvement from Kodus-AI pattern — restructures judge prompt as sequential expert passes (Gatekeeper → Verifier → Calibrator → Synthesizer). Zero cost, better reasoning structure. |

Feature 0 should be done first — it establishes conventions that Features 1-3 build on. Features 1-3 are independent of each other.

### Inter-feature interactions

| Feature pair | Interaction |
|-------------|------------|
| 0 → 1, 2, 3 | Feature 0 establishes the `scripts/` directory, interface conventions (stdin/stdout JSON, `--flag` args, graceful fallback), and the pattern of "agent runs script, consumes JSON." Features 1-3 follow this pattern. |
| 0 → existing pipeline | `run-scans.sh` replaces agent interpretation of `deterministic-scans.md`. `enrich-findings.py` replaces agent interpretation of Step 5 rules. `complexity.sh` replaces inline snippets in Step 2d. |
| 1 → 1.5 (large-diff) | Git history risk promotes files from Tier 2 to Tier 1 in chunked mode. Must update Step 1.5c tier criteria. |
| 2 → test-adequacy pass | Coverage data is consumed by the test-adequacy explorer. Must update that pass's prompt to reference measured data. |
| 2 → spec-verification | Coverage data enriches spec requirement `test_coverage` with measured data. Interaction is natural — no special wiring. |
| 3 → 0 | Feature 3's `lifecycle.py` runs AFTER Feature 0's `enrich-findings.py`. The pipeline is: judge output → `enrich-findings.py` (IDs, tiers, confidence) → `lifecycle.py` (fingerprints, suppressions). |
| 3 → 4 | Feature 4's cross-model finding matching uses the same normalization logic as Feature 3's fingerprinting. If Feature 3 ships first, Feature 4 reuses the fingerprint. If Feature 4 ships first, it implements a simpler inline matching that Feature 3 later supersedes. |
| 1 + 2 → context packet | Both add sections to Step 2h. They're additive — no conflict. Token budget for context packet grows by ~2-4k tokens (historical risk ~1k, coverage ~1-3k). |

### Script pipeline (full flow after all features)

```
Step 2:   python3 scripts/discover-project.py < CHANGED_FILES → project.json
Step 2d:  bash scripts/complexity.sh < CHANGED_FILES          → complexity.json
Step 2i:  bash scripts/git-risk.sh < CHANGED_FILES            → git-risk.json
Step 2j:  python3 scripts/coverage-collect.py < CHANGED_FILES  → coverage.json
Step 3:   bash scripts/run-scans.sh --project-profile project.json < CHANGED_FILES → scans.json
Step 4:   Agent launches explorers + judge (AI)               → judge.json
Step 5a:  Agent dedup + linter-removal (AI judgment)          → cleaned-judge.json
Step 5a:  python3 scripts/enrich-findings.py                  → enriched.json
Step 5a5: python3 scripts/lifecycle.py                        → lifecycle.json (final findings)
Step 6:   Agent formats report (AI)                           → report.md
Step 7:   Agent saves artifacts                               → .agents/reviews/
Step 7:   bash scripts/validate_output.sh                     → validation result
```

Scripts handle the deterministic plumbing (boxes). The agent handles understanding, judgment, and narration (arrows between boxes).

**Tool execution order within Step 3:**
```
Tier 1 (parallel):  semgrep | trivy | osv-scanner | gitleaks | shellcheck
                              ↓
Tier 2 (parallel):  clippy | ruff | golangci-lint | eslint (if config exists)
                              ↓
Tier 3 (sequential): project commands from discover-project.py (scoped per project context)
                              ↓
Normalize → Deduplicate → Output JSON
```

### Backward compatibility

All four features are additive — existing reviews without these features remain valid:
- `lifecycle_status`, `fingerprint`, `suppressed_findings` are optional fields (not in `required` array)
- `council_mode` and `model_source` are optional
- `tool_status` keys for coverage and git-history are new entries, not changes to existing ones
- The `.codereview-suppressions.json` file is only read if it exists

Existing `.agents/reviews/*.json` files from v1.1 will validate against the v1.2 schema without modification.

---

## Out of Scope

- **SKILL.md description fix** — Deferred per user decision
- **False positive feedback loop** — No simple implementation path identified; Feature 3 (suppressions) provides a manual version
- **Cross-repo dependency awareness** — Requires external tooling, not in scope for v1.2
- **Multi-model council (same-family)** — Deferred to research. Same-family variants (Sonnet↔Opus) share training data; quality improvement is uncertain. See `docs/research-multi-model-council.md`.
- **Multi-model council (cross-vendor)** — Deferred to research. Requires cross-vendor spawning mechanism (API calls to OpenAI/Google). See `docs/research-multi-model-council.md`.
- **Self-reflection scoring** — PR-Agent's 0-10 scoring bands are promising but require calibration data we don't have yet. Candidate for v1.3 after named expert panel is validated.
- **Automated suppression cleanup** — Stale suppressions (for deleted files) are harmless; manual pruning is sufficient for v1.2
