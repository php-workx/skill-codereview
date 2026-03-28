# Deterministic Scans Reference

> **Note:** The executable logic from this document has been extracted to `scripts/run-scans.sh`. This file is now **reference documentation only** — it explains what each tool does, when to install them, and how normalization works. The agent should run the script, not re-implement this document. Fall back to manual execution only if the script is unavailable.

This file contains tool documentation, cache setup rationale, and operational details for Step 3 of the codereview skill.

---

## Permission Strategy (Sandboxed Runners)

Request elevated permissions **before** starting the scan bundle (single escalation), instead of waiting for fail-then-retry. This avoids repeated sandbox failures for tools that need host caches or Docker access.

Use a one-line justification like:
- `Run deterministic code-review scanners with host cache/docker access to avoid sandbox permission failures and collect complete tool_status output.`

## File Path Handling

`CHANGED_FILES` is newline-delimited. When passing to tools, use `xargs` or quote-safe expansion to handle paths with spaces.

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

## Sandbox/Cache Setup

Initialize writable cache dirs before scans so tools do not fail on default cache locations in sandboxed runs.

```bash
export TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-/tmp/trivy-cache}"
export PRE_COMMIT_HOME="${PRE_COMMIT_HOME:-/tmp/pre-commit-cache}"
export SEMGREP_HOME="${SEMGREP_HOME:-/tmp/semgrep-home}"
export GOCACHE="${GOCACHE:-/tmp/go-build-cache}"
export GOMODCACHE="${GOMODCACHE:-/tmp/go-mod-cache}"
export GOPATH="${GOPATH:-/tmp/go}"
mkdir -p "$TRIVY_CACHE_DIR" "$PRE_COMMIT_HOME" "$SEMGREP_HOME" "$GOCACHE" "$GOMODCACHE" "$GOPATH"
```

## Execution Safety (zsh Environments)

If your runner shell is `zsh` (common in agent environments), do **not** embed a multiline script in a single-quoted `bash -lc '...'` string. Inner single-quoted jq/rg patterns (for example `jq '.results | length'`) will terminate the outer quote and trigger parse errors like `bad pattern` or `no matches found`. For multiline logic, write a temp script with a quoted heredoc and run it with bash.

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

## Parallel Execution Pattern

When both semgrep and sonarqube are available, run them in parallel:

```bash
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
  HOME="$SEMGREP_HOME" semgrep scan --json --quiet "${FILES[@]}" > "/tmp/codereview/semgrep-${RUN_ID}.json" 2>"/tmp/codereview/semgrep-${RUN_ID}.err" &
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

## Individual Tool Scripts

```bash
# NOTE: These snippets assume FILES array from "File Path Handling" section above.
# Build it first: while IFS= read -r f; do [ -n "$f" ] && FILES+=("$f"); done <<< "$CHANGED_FILES"

# semgrep — static analysis / custom rules (scoped to changed files)
if command -v semgrep &>/dev/null; then
  HOME="$SEMGREP_HOME" semgrep scan --json --quiet "${FILES[@]}" 2>/dev/null | head -500
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
SH_FILES=()
for f in "${FILES[@]}"; do [[ "$f" == *.sh ]] && SH_FILES+=("$f"); done
if [ ${#SH_FILES[@]} -gt 0 ] && command -v shellcheck &>/dev/null; then
  shellcheck --format=json "${SH_FILES[@]}" 2>/dev/null | head -500
else
  if [ ${#SH_FILES[@]} -gt 0 ]; then
    echo "shellcheck not installed (brew install shellcheck)"
  fi
fi

# pre-commit — repo-configured hooks (scoped to changed files)
if [ -f .pre-commit-config.yaml ] && command -v pre-commit &>/dev/null; then
  PRE_COMMIT_HOME="$PRE_COMMIT_HOME" \
  GOCACHE="$GOCACHE" \
  GOMODCACHE="$GOMODCACHE" \
  GOPATH="$GOPATH" \
  pre-commit run --files "${FILES[@]}" 2>&1 | head -200
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
    head -500 .sonarqube/findings.json
  fi
else
  echo "sonarqube skill not installed (see skill-sonarqube README)"
fi
```

## Normalization and Deduplication

After all scans complete:

- Normalize each deterministic tool output into the standard finding shape (`pass`, `file`, `line`, `summary`, `severity`, `confidence=1.0`, `evidence`).
- Build a deterministic dedupe key: `file + ":" + line + ":" + normalize(summary)`.
- On key collision (e.g., semgrep + sonarqube same issue), keep one finding with:
  - highest severity,
  - unioned provenance in `sources` (e.g., `["semgrep","sonarqube"]`),
  - merged evidence/note text.
- Feed this deduplicated deterministic set to explorers and final merge.

## Tool Status Keys

Record `tool_status` for each tool (`ran` / `skipped` / `not_installed` / `sandbox_blocked` / `failed`) with version and finding count:

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
| `ai_spec_verification` | Explorer: spec verification (extended) |
| `ai_judge` | Review judge |

## `code_intel.py patterns` — Lightweight Semgrep Fallback

When semgrep is not installed, `scripts/code_intel.py patterns` provides a regex-based fallback for the most common static analysis checks. It activates automatically — the orchestrator calls it as part of deterministic scan collection when semgrep is absent.

### Patterns checked (6)

| Pattern | Severity | Requires tree-sitter | What it detects |
|---------|----------|---------------------|-----------------|
| `sql-injection` | high | No | String concatenation or f-strings in SQL execution calls (`execute`, `query`, `cursor.execute`) |
| `command-injection` | high | No | String concatenation or f-strings in command execution calls (`subprocess.run`, `os.system`, `popen`) |
| `empty-error-handler` | medium | No | Exception/catch blocks with empty bodies (`except ...: pass`, `catch(...) {}`) |
| `unused-import` | low | Yes | Imported symbol not referenced anywhere in the file |
| `unreachable-code` | low | Yes | Code after `return`/`raise`/`throw` statements |
| `resource-leak` | medium | Yes | `open()`/`connect()` without matching `close()` |

Without tree-sitter, only the first 3 patterns (regex-matchable) are checked. The remaining 3 require AST analysis and are silently skipped.

### Output format

Findings use the same shape as `run-scans.sh` normalized output:

```json
{
  "pattern": "sql-injection",
  "severity": "high",
  "file": "src/db.py",
  "line": 42,
  "summary": "String concatenation/f-string in SQL execution call",
  "evidence": "cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")",
  "source": "deterministic",
  "confidence": 1.0
}
```

The top-level response includes `"analyzer": "tree-sitter"` or `"analyzer": "regex-only"` so the orchestrator knows which mode was used.

### Limitations vs semgrep

- **Far fewer rules:** 6 patterns vs semgrep's thousands of community rules. Only covers the highest-signal checks.
- **Regex-based matching:** Without tree-sitter, patterns match syntactically — they cannot distinguish string interpolation inside a parameterized query wrapper from actual injection risks. Higher false-positive rate than semgrep's semantic analysis.
- **No custom rules:** Semgrep supports project-specific `.semgrep.yml` rules; `code_intel.py patterns` has a fixed pattern set.
- **No cross-function analysis:** Each pattern matches within a single line or small block. Semgrep's taint tracking and dataflow analysis are not replicated.

## Tool Status Classification Rules

- `not_installed`: command not found on PATH.
- `sandbox_blocked`: stderr indicates permission/sandbox denial (for example `permission denied`, `operation not permitted`, read-only fs/cache path denied).
- `failed`: command executed but failed for non-sandbox reasons.
- `skipped`: intentionally not run (for example no matching files, missing config, optional mode).
- `ran`: completed successfully (with or without findings).
