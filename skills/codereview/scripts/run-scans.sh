#!/usr/bin/env bash
# run-scans.sh — Deterministic scan orchestration for the codereview skill
#
# Runs a 3-tier set of code review tools, normalizes output into a standard
# finding shape, deduplicates, and emits structured JSON on stdout.
#
# Usage:
#   echo "$CHANGED_FILES" | bash run-scans.sh --base-ref <ref> [--project-profile <json>]
#
# Input:  CHANGED_FILES on stdin (newline-delimited file paths)
# Output: stdout = { "findings": [...], "tool_status": {...} }
#         stderr = human-readable progress log
#
# Exit code: always 0 (best-effort — missing tools are recorded, not fatal)
#
# Bash 3 compatible (macOS default). Requires: jq

set -uo pipefail

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
  echo "FATAL: jq is required but not installed. Install it with: brew install jq (macOS) or apt-get install jq (Linux)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BASE_REF=""
PROJECT_PROFILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --base-ref)        BASE_REF="$2";        shift 2 ;;
    --project-profile) PROJECT_PROFILE="$2";  shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$BASE_REF" ]; then
  echo "ERROR: --base-ref is required" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Read CHANGED_FILES from stdin into array (Bash 3 safe — no mapfile)
# ---------------------------------------------------------------------------
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && FILES+=("$f")
done

if [ ${#FILES[@]} -eq 0 ]; then
  echo "WARNING: no changed files received on stdin" >&2
  jq -n '{"findings":[],"tool_status":{}}'
  exit 0
fi

echo "run-scans: ${#FILES[@]} changed files, base-ref=${BASE_REF}" >&2

# ---------------------------------------------------------------------------
# Sandbox / cache setup
# ---------------------------------------------------------------------------
export TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-/tmp/trivy-cache}"
export PRE_COMMIT_HOME="${PRE_COMMIT_HOME:-/tmp/pre-commit-cache}"
export SEMGREP_HOME="${SEMGREP_HOME:-/tmp/semgrep-home}"
export GOCACHE="${GOCACHE:-/tmp/go-build-cache}"
export GOMODCACHE="${GOMODCACHE:-/tmp/go-mod-cache}"
export GOPATH="${GOPATH:-/tmp/go}"
mkdir -p "$TRIVY_CACHE_DIR" "$PRE_COMMIT_HOME" "$SEMGREP_HOME" \
         "$GOCACHE" "$GOMODCACHE" "$GOPATH" 2>/dev/null || true

# Scratch directory for raw tool output
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SCRATCH="$(mktemp -d /tmp/codereview-scans-XXXXXX)"
trap 'rm -rf "$SCRATCH" "${SONAR_OUT_DIR:-}"' EXIT INT TERM


# ---------------------------------------------------------------------------
# Language detection from CHANGED_FILES
# ---------------------------------------------------------------------------
HAS_RUST=false
HAS_PYTHON=false
HAS_GO=false
HAS_JS=false
HAS_RUBY=false
HAS_JAVA=false
SH_FILES=()

for f in "${FILES[@]}"; do
  case "$f" in
    *.rs)                    HAS_RUST=true ;;
    *.py)                    HAS_PYTHON=true ;;
    *.go)                    HAS_GO=true ;;
    *.ts|*.tsx|*.js|*.jsx)   HAS_JS=true ;;
    *.rb|*.rake|*.gemspec)   HAS_RUBY=true ;;
    *.java|*.kt|*.scala)     HAS_JAVA=true ;;
    *.sh)                    SH_FILES+=("$f") ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# log: write timestamped message to stderr
log() {
  echo "[run-scans] $*" >&2
}

# check_normalized: verify a normalization output file is valid JSON array.
# If empty or invalid, write '[]' and log a warning.
# Args: $1=file_path, $2=tool_name
check_normalized() {
  local fpath="$1"
  local tool="$2"
  if [ ! -s "$fpath" ] || ! jq -e 'type == "array"' "$fpath" >/dev/null 2>&1; then
    log "WARNING: normalization of $tool output produced invalid JSON — replacing with []"
    echo '[]' > "$fpath"
  fi
}

# filter_to_changed_files: remove findings whose .file is not in changed files.
# This is needed for repo-wide tools (clippy, golangci-lint) that scan the
# entire workspace and may report stale findings from untouched files.
# Args: $1=findings_json_file
filter_to_changed_files() {
  local fpath="$1"
  if [ ! -f "$fpath" ] || [ ! -s "$fpath" ]; then
    return
  fi
  # Build a JSON array of changed file paths for jq
  local changed_json
  changed_json=$(printf '%s\n' "${FILES[@]}" | jq -R . | jq -s .)
  # Filter: keep only findings where .file is in the changed files list
  local filtered
  filtered=$(jq --argjson changed "$changed_json" \
    '[.[] | select(.file as $f | $changed | any(. == $f or (. | endswith("/" + $f))))]' \
    "$fpath" 2>/dev/null) || return
  # Validate output before overwriting
  if [ -n "$filtered" ] && [ "$filtered" != "null" ]; then
    echo "$filtered" > "$fpath"
  fi
}

# get_version: attempt to get version string for a tool
get_version() {
  local tool="$1"
  case "$tool" in
    semgrep)       semgrep --version 2>/dev/null | head -1 ;;
    trivy)         trivy --version 2>/dev/null | head -1 ;;
    osv-scanner)   osv-scanner --version 2>/dev/null | head -1 ;;
    gitleaks)      gitleaks version 2>/dev/null | head -1 ;;
    shellcheck)    shellcheck --version 2>/dev/null | sed -n 's/^version: //p' | head -1 ;;
    clippy)        cargo clippy --version 2>/dev/null | head -1 ;;
    ruff)          ruff --version 2>/dev/null | head -1 ;;
    golangci-lint) golangci-lint --version 2>/dev/null | head -1 ;;
    eslint)        eslint --version 2>/dev/null | head -1 ;;
    rubocop)       rubocop --version 2>/dev/null | head -1 ;;
    brakeman)      brakeman --version 2>/dev/null | head -1 ;;
    pmd)           pmd --version 2>/dev/null | head -1 ;;
    pre-commit)    pre-commit --version 2>/dev/null | head -1 ;;
    sonarqube)     echo "skill-based" ;;
    *)             echo "unknown" ;;
  esac
}

# classify_status: determine tool_status from exit code and stderr
# Args: $1=exit_code, $2=stderr_file
# Prints one of: ran, timeout, failed, sandbox_blocked
classify_status() {
  local rc="$1"
  local errfile="$2"

  if [ "$rc" -eq 124 ] 2>/dev/null; then
    echo "timeout"
    return
  fi

  if [ "$rc" -eq 0 ]; then
    echo "ran"
    return
  fi

  # Check stderr for sandbox/permission indicators
  if [ -f "$errfile" ]; then
    if grep -qiE 'permission denied|operation not permitted|read-only file|sandbox' "$errfile" 2>/dev/null; then
      echo "sandbox_blocked"
      return
    fi
  fi

  echo "failed"
}

# ---------------------------------------------------------------------------
# Tool status tracking
# ---------------------------------------------------------------------------
# We accumulate tool_status entries as individual JSON files in $SCRATCH/status/
mkdir -p "$SCRATCH/status" "$SCRATCH/findings"

# record_status: write tool status JSON
# Args: $1=tool_key, $2=status, $3=version (optional), $4=finding_count (optional), $5=note (optional)
record_status() {
  local key="$1"
  local status="$2"
  local version="${3:-null}"
  local count="${4:-0}"
  local note="${5:-null}"

  jq -n \
    --arg s "$status" \
    --arg v "$version" \
    --argjson c "$count" \
    --arg n "$note" \
    '{
      status: $s,
      version: (if $v == "null" then null else $v end),
      finding_count: $c,
      note: (if $n == "null" then null else $n end)
    }' > "$SCRATCH/status/${key}.json"
}

# ---------------------------------------------------------------------------
# Normalization functions — one per tool
# Each reads tool-specific JSON from stdin, writes normalized findings JSON
# to stdout. Each finding has: file, line, summary, severity, confidence,
# evidence, pass, source, tool
# ---------------------------------------------------------------------------

normalize_semgrep() {
  jq '
    [(.results // [])[] |
      {
        file: .path,
        line: (.start.line // 0),
        summary: .message,
        severity: (
          if .severity == "ERROR" then "high"
          elif .severity == "WARNING" then "medium"
          elif .severity == "INFO" then "low"
          else "medium"
          end
        ),
        confidence: 1.0,
        evidence: ((.extra.lines // "") | tostring),
        pass: (
          if (.check_id // "" | test("security|vuln|injection|crypto|auth|xss|xxe|ssrf|idor"; "i")) then "security"
          elif (.check_id // "" | test("correctness|bug|error|type-error|null-deref"; "i")) then "correctness"
          elif (.check_id // "" | test("performance|complexity|n-plus-one"; "i")) then "performance"
          elif (.check_id // "" | test("reliability|timeout|retry|resource-leak"; "i")) then "reliability"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "semgrep"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_trivy() {
  jq '
    [(.Results // [])[] |
      (.Target // "unknown") as $target |
      (.Vulnerabilities // [])[] |
      {
        file: $target,
        line: 0,
        summary: (.Title // .VulnerabilityID // "Unknown vulnerability"),
        severity: (
          if .Severity == "CRITICAL" then "critical"
          elif .Severity == "HIGH" then "high"
          elif .Severity == "MEDIUM" then "medium"
          elif .Severity == "LOW" then "low"
          else "medium"
          end
        ),
        confidence: 1.0,
        evidence: (
          [.VulnerabilityID // "", "installed=" + (.InstalledVersion // "?"), "fixed=" + (.FixedVersion // "?")] | join(" ")
        ),
        pass: "security",
        source: "deterministic",
        tool: "trivy"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_osv() {
  jq '
    [(.results // [])[] |
      (.packages // [])[] |
      (.package.name // "unknown") as $pkg |
      (.vulnerabilities // [])[] |
      {
        file: $pkg,
        line: 0,
        summary: (.summary // .id // "Unknown vulnerability"),
        severity: (
          if (.database_specific.severity // "" | test("CRITICAL"; "i")) then "critical"
          elif (.database_specific.severity // "" | test("HIGH"; "i")) then "high"
          elif (.database_specific.severity // "" | test("LOW"; "i")) then "low"
          else "medium"
          end
        ),
        confidence: 1.0,
        evidence: (.id // ""),
        pass: "security",
        source: "deterministic",
        tool: "osv-scanner"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_gitleaks() {
  jq '
    [(.[] // empty) |
      {
        file: (.File // "unknown"),
        line: (.StartLine // 0),
        summary: ("Secret detected: " + (.Description // "unknown type")),
        severity: "critical",
        confidence: 1.0,
        evidence: (
          ((.Match // "")[0:4] + "..." + (.Match // "")[-4:]) + " rule=" + (.RuleID // "unknown")
        ),
        pass: "security",
        source: "deterministic",
        tool: "gitleaks"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_shellcheck() {
  jq '
    [(.[] // empty) |
      {
        file: (.file // "unknown"),
        line: (.line // 0),
        summary: (.message // "shellcheck issue"),
        severity: (
          if .level == "error" then "high"
          elif .level == "warning" then "medium"
          elif .level == "info" then "low"
          elif .level == "style" then "low"
          else "medium"
          end
        ),
        confidence: 1.0,
        evidence: ("SC" + ((.code // 0) | tostring) + (if .fix then " fix=" + (.fix | tostring) else "" end)),
        pass: "reliability",
        source: "deterministic",
        tool: "shellcheck"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_ruff() {
  jq '
    [(.[] // empty) |
      {
        file: (.filename // "unknown"),
        line: (.location.row // 0),
        summary: (.message // "ruff issue"),
        severity: (
          if (.code // "" | test("^[EF]")) then "high"
          elif (.code // "" | test("^W")) then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: (.code // ""),
        pass: "maintainability",
        source: "deterministic",
        tool: "ruff"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_golangci() {
  jq '
    [(.Issues // [])[] |
      {
        file: (.Pos.Filename // "unknown"),
        line: (.Pos.Line // 0),
        summary: (.Text // "golangci-lint issue"),
        severity: (
          if (.FromLinter // "" | test("errcheck|govet")) then "high"
          elif (.FromLinter // "" | test("staticcheck")) then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: ((.FromLinter // "") + " " + ((.SourceLines // []) | join("\n"))),
        pass: (
          if (.FromLinter // "" | test("errcheck|govet")) then "correctness"
          elif (.FromLinter // "" | test("gosec")) then "security"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "golangci-lint"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_clippy() {
  # clippy outputs JSON messages on stderr when invoked with --message-format=json
  # Each line is a JSON object; we filter for compiler-message kind
  jq -s '
    [.[] | select(.reason == "compiler-message") | .message |
      select(.level != "note" or (.level == "note" and (.code.code // "" | test("clippy::")))) |
      {
        file: ((.spans // [{}])[0].file_name // "unknown"),
        line: ((.spans // [{}])[0].line_start // 0),
        summary: (.message // "clippy issue"),
        severity: (
          if .level == "error" then "high"
          elif .level == "warning" then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: (.code.code // ""),
        pass: (
          if (.code.code // "" | test("clippy::correctness|clippy::suspicious")) then "correctness"
          elif (.code.code // "" | test("clippy::security")) then "security"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "clippy"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_eslint() {
  jq '
    [(.[] // empty) |
      .filePath as $fp |
      (.messages // [])[] |
      {
        file: $fp,
        line: (.line // 0),
        summary: (.message // "eslint issue"),
        severity: (
          if .severity == 2 then "high"
          elif .severity == 1 then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: (.ruleId // ""),
        pass: "maintainability",
        source: "deterministic",
        tool: "eslint"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_rubocop() {
  # RuboCop JSON: {files: [{path, offenses: [{cop_name, severity, message, location: {start_line}}]}]}
  jq '
    [(.files // [])[] |
      .path as $fp |
      (.offenses // [])[] |
      {
        file: $fp,
        line: (.location.start_line // 0),
        summary: (.message // "rubocop issue"),
        severity: (
          if .severity == "fatal" or .severity == "error" then "high"
          elif .severity == "warning" then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: (.cop_name // ""),
        pass: (
          if (.cop_name // "" | test("Security")) then "security"
          elif (.cop_name // "" | test("^Lint")) then "correctness"
          elif (.cop_name // "" | test("Performance")) then "performance"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "rubocop"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_brakeman() {
  # Brakeman JSON: {warnings: [{warning_type, confidence, file, line, message, code}]}
  jq '
    [(.warnings // [])[] |
      {
        file: (.file // "unknown"),
        line: (.line // 0),
        summary: ((.warning_type // "") + ": " + (.message // "brakeman issue")),
        severity: (
          if .confidence == "High" then "high"
          elif .confidence == "Medium" then "medium"
          else "low"
          end
        ),
        confidence: (
          if .confidence == "High" then 0.95
          elif .confidence == "Medium" then 0.80
          else 0.65
          end
        ),
        evidence: (.code // ""),
        pass: "security",
        source: "deterministic",
        tool: "brakeman"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_pmd() {
  # PMD JSON: {files: [{filename, violations: [{beginline, rule, ruleset, priority, description}]}]}
  jq '
    [(.files // [])[] |
      .filename as $fp |
      (.violations // [])[] |
      {
        file: $fp,
        line: (.beginline // 0),
        summary: (.description // "PMD violation"),
        severity: (
          if .priority <= 2 then "high"
          elif .priority <= 3 then "medium"
          else "low"
          end
        ),
        confidence: 1.0,
        evidence: ((.rule // "") + " [" + (.ruleset // "") + "]"),
        pass: (
          if (.ruleset // "" | test("[Ss]ecurity")) then "security"
          elif (.ruleset // "" | test("Error Prone|Design")) then "correctness"
          elif (.ruleset // "" | test("Performance")) then "performance"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "pmd"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_sonarqube() {
  jq '
    [(.issues // [])[] |
      {
        file: (.component // "unknown" | sub("^[^:]*:"; "")),
        line: (.line // 0),
        summary: (.message // "sonarqube issue"),
        severity: (
          if .severity == "BLOCKER" then "critical"
          elif .severity == "CRITICAL" then "high"
          elif .severity == "MAJOR" then "medium"
          elif .severity == "MINOR" then "low"
          elif .severity == "INFO" then "low"
          else "medium"
          end
        ),
        confidence: 1.0,
        evidence: ((.rule // "") + (if .effort then " effort=" + .effort else "" end)),
        pass: (
          if (.type // "" | test("BUG")) then "correctness"
          elif (.type // "" | test("VULNERABILITY")) then "security"
          else "maintainability"
          end
        ),
        source: "deterministic",
        tool: "sonarqube"
      }
    ]
  ' 2>/dev/null || echo '[]'
}

normalize_precommit() {
  # pre-commit outputs plain text, not JSON. We create a single finding
  # from the output if non-empty.
  local output
  output="$(cat)"
  if [ -z "$output" ]; then
    echo '[]'
    return
  fi
  local first_line
  first_line="$(echo "$output" | head -1)"
  local truncated
  truncated="$(echo "$output" | head -50 | cut -c1-500)"
  # Escape for JSON
  jq -n --arg summary "$first_line" --arg evidence "$truncated" \
    '[{
      file: ".",
      line: 0,
      summary: $summary,
      severity: "medium",
      confidence: 1.0,
      evidence: $evidence,
      pass: "maintainability",
      source: "deterministic",
      tool: "pre-commit"
    }]'
}

normalize_project_cmd() {
  # Best-effort normalization of arbitrary project command output (plain text)
  local tool_name="$1"
  local output
  output="$(cat)"
  if [ -z "$output" ]; then
    echo '[]'
    return
  fi
  local first_line
  first_line="$(echo "$output" | head -1)"
  local truncated
  truncated="$(echo "$output" | head -50 | cut -c1-500)"
  jq -n --arg summary "$first_line" --arg evidence "$truncated" --arg tool "$tool_name" \
    '[{
      file: ".",
      line: 0,
      summary: $summary,
      severity: "medium",
      confidence: 1.0,
      evidence: $evidence,
      pass: "maintainability",
      source: "deterministic",
      tool: $tool
    }]'
}

# ---------------------------------------------------------------------------
# Tool runner: invokes a tool with timeout, captures output and status
# Args: $1=tool_key, $2=timeout_seconds, $3...=command and args
# Writes raw output to $SCRATCH/<tool_key>.out, stderr to $SCRATCH/<tool_key>.err
# Also records elapsed time in $SCRATCH/<tool_key>.ms
# Returns the exit code
# ---------------------------------------------------------------------------

# _get_epoch_ms: epoch time in milliseconds (for tool timing)
_get_epoch_ms() {
  local ms
  ms="$(date +%s%3N 2>/dev/null)"
  # On macOS, %3N is literal "3N" — detect and fall back
  case "$ms" in
    *[!0-9]*) ms="$(python3 -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)" ;;
  esac
  echo "$ms"
}

run_tool() {
  local key="$1"
  local tout="$2"
  shift 2

  log "${key}: running (timeout=${tout}s)..."

  # Use the timeout command. On macOS, GNU coreutils 'gtimeout' may be available;
  # fall back to the command's own execution if neither is found.
  local timeout_cmd=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_cmd="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_cmd="gtimeout"
  fi

  local t_start t_end elapsed_ms
  t_start="$(_get_epoch_ms)"

  local rc=0
  if [ -n "$timeout_cmd" ]; then
    $timeout_cmd "$tout" "$@" >"$SCRATCH/${key}.out" 2>"$SCRATCH/${key}.err" || rc=$?
  else
    # No timeout command available — run directly
    "$@" >"$SCRATCH/${key}.out" 2>"$SCRATCH/${key}.err" || rc=$?
  fi

  t_end="$(_get_epoch_ms)"
  elapsed_ms=$((t_end - t_start))
  echo "$elapsed_ms" > "$SCRATCH/${key}.ms"

  local status
  status="$(classify_status "$rc" "$SCRATCH/${key}.err")"
  log "${key}: finished with status=${status} (rc=${rc}, ${elapsed_ms}ms)"

  echo "$status" > "$SCRATCH/${key}.status"
  return 0  # Always succeed — best-effort
}

# Record overall scan start time for _timing.total_ms
SCAN_START_MS="$(_get_epoch_ms)"

# ---------------------------------------------------------------------------
# TIER 1: Baseline tools (always run if installed, in parallel)
# ---------------------------------------------------------------------------
log "=== Tier 1: Baseline tools ==="

TIER1_PIDS=""

# --- semgrep ---
if command -v semgrep >/dev/null 2>&1; then
  (
    HOME="$SEMGREP_HOME" run_tool semgrep 60 semgrep scan --json --quiet "${FILES[@]}"
  ) &
  TIER1_PIDS="$TIER1_PIDS $!"
else
  log "semgrep: not installed"
  record_status "semgrep" "not_installed" "null" 0 "pip install semgrep"
fi

# --- trivy ---
# Scope trivy to manifest/lockfiles in the changeset. Scanning the full repo
# (trivy fs .) takes 18s+ even on small repos. Trivy only needs package manifests
# (package.json, go.mod, Cargo.toml, etc.) to find dependency vulnerabilities.
# Also skip DB update — use cached DB (stale data is acceptable for code review;
# full DB refresh is a separate security audit concern).
if command -v trivy >/dev/null 2>&1; then
  TRIVY_TARGETS=()
  MANIFEST_PATTERNS="package.json|package-lock.json|yarn.lock|pnpm-lock.yaml|go.mod|go.sum|Cargo.toml|Cargo.lock|pyproject.toml|poetry.lock|requirements.txt|Pipfile.lock|Gemfile.lock|composer.lock"
  for tf in "${FILES[@]}"; do
    if echo "$tf" | grep -qE "(${MANIFEST_PATTERNS})$"; then
      TRIVY_TARGETS+=("$tf")
    fi
  done
  if [ ${#TRIVY_TARGETS[@]} -gt 0 ]; then
    (
      # Pass manifest files directly to trivy — scanning individual files is ~100ms
      # vs 18s+ for directories. Trivy auto-discovers related lockfiles.
      run_tool trivy 60 trivy fs "${TRIVY_TARGETS[@]}" --cache-dir "$TRIVY_CACHE_DIR" --skip-db-update --format json --quiet
    ) &
    TIER1_PIDS="$TIER1_PIDS $!"
  else
    log "trivy: skipped (no manifest/lockfiles in changed files)"
    record_status "trivy" "skipped" "null" 0 "no manifest/lockfiles in changed files"
  fi
else
  log "trivy: not installed"
  record_status "trivy" "not_installed" "null" 0 "brew install trivy"
fi

# --- osv-scanner ---
if command -v osv-scanner >/dev/null 2>&1; then
  (
    run_tool osv_scanner 120 osv-scanner scan -r . --format json
  ) &
  TIER1_PIDS="$TIER1_PIDS $!"
else
  log "osv-scanner: not installed"
  record_status "osv_scanner" "not_installed" "null" 0 "go install github.com/google/osv-scanner/cmd/osv-scanner@latest"
fi

# --- gitleaks ---
if command -v gitleaks >/dev/null 2>&1; then
  (
    # Scope gitleaks to changed files only — scanning the full repo (--source .)
    # is too slow (40s+ CPU on medium repos). Create a temp dir with symlinks to
    # changed files, and scan that instead.
    GITLEAKS_SRC="$SCRATCH/gitleaks_src"
    for gf in "${FILES[@]}"; do
      gf_dir="$GITLEAKS_SRC/$(dirname "$gf")"
      mkdir -p "$gf_dir" 2>/dev/null
      ln -sf "$(pwd)/$gf" "$GITLEAKS_SRC/$gf" 2>/dev/null
    done
    run_tool gitleaks 60 gitleaks detect --source "$GITLEAKS_SRC" --report-format json --report-path "$SCRATCH/gitleaks_report.json" --no-git --follow-symlinks --exit-code 0
  ) &
  TIER1_PIDS="$TIER1_PIDS $!"
else
  log "gitleaks: not installed"
  record_status "gitleaks" "not_installed" "null" 0 "brew install gitleaks"
fi

# --- shellcheck ---
if [ ${#SH_FILES[@]} -gt 0 ]; then
  if command -v shellcheck >/dev/null 2>&1; then
    (
      run_tool shellcheck 60 shellcheck --format=json "${SH_FILES[@]}"
    ) &
    TIER1_PIDS="$TIER1_PIDS $!"
  else
    log "shellcheck: not installed (${#SH_FILES[@]} .sh files in diff)"
    record_status "shellcheck" "not_installed" "null" 0 "brew install shellcheck"
  fi
else
  log "shellcheck: skipped (no .sh files in diff)"
  record_status "shellcheck" "skipped" "null" 0 "no .sh files in diff"
fi

# Wait for all Tier 1 tools
for pid in $TIER1_PIDS; do
  wait "$pid" 2>/dev/null || true
done
log "=== Tier 1 complete ==="

# Record status and normalize Tier 1 results
for tool_key in semgrep trivy osv_scanner gitleaks shellcheck; do
  if [ -f "$SCRATCH/${tool_key}.status" ]; then
    status="$(cat "$SCRATCH/${tool_key}.status")"
    # Get version
    case "$tool_key" in
      osv_scanner) version="$(get_version osv-scanner)" ;;
      *)           version="$(get_version "$tool_key")" ;;
    esac

    # Normalize output
    case "$tool_key" in
      semgrep)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          normalize_semgrep < "$SCRATCH/semgrep.out" > "$SCRATCH/findings/semgrep.json"
          check_normalized "$SCRATCH/findings/semgrep.json" "semgrep"
        fi
        ;;
      trivy)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          normalize_trivy < "$SCRATCH/trivy.out" > "$SCRATCH/findings/trivy.json"
          check_normalized "$SCRATCH/findings/trivy.json" "trivy"
        fi
        ;;
      osv_scanner)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          normalize_osv < "$SCRATCH/osv_scanner.out" > "$SCRATCH/findings/osv_scanner.json"
          check_normalized "$SCRATCH/findings/osv_scanner.json" "osv-scanner"
        fi
        ;;
      gitleaks)
        # gitleaks writes its report to --report-path, not stdout
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -f "$SCRATCH/gitleaks_report.json" ] && [ -s "$SCRATCH/gitleaks_report.json" ]; then
            normalize_gitleaks < "$SCRATCH/gitleaks_report.json" > "$SCRATCH/findings/gitleaks.json"
          else
            echo '[]' > "$SCRATCH/findings/gitleaks.json"
          fi
        fi
        ;;
      shellcheck)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          # SC tool exits non-zero when it finds issues — that is normal "ran"
          if [ -s "$SCRATCH/shellcheck.out" ]; then
            normalize_shellcheck < "$SCRATCH/shellcheck.out" > "$SCRATCH/findings/shellcheck.json"
          else
            echo '[]' > "$SCRATCH/findings/shellcheck.json"
          fi
        fi
        ;;
    esac

    # Count findings (ensure numeric, default 0)
    local_count=0
    if [ -f "$SCRATCH/findings/${tool_key}.json" ]; then
      local_count="$(jq 'length // 0' "$SCRATCH/findings/${tool_key}.json" 2>/dev/null || echo 0)"
      # Guard against empty/non-numeric result
      case "$local_count" in
        ''|*[!0-9]*) local_count=0 ;;
      esac
    fi

    # For tools that exit non-zero when findings exist (shellcheck, gitleaks), treat as "ran"
    if [ "$status" = "failed" ]; then
      case "$tool_key" in
        shellcheck|gitleaks|osv_scanner|semgrep)
          if [ "$local_count" -gt 0 ] 2>/dev/null; then
            status="ran"
          fi
          ;;
      esac
    fi

    record_status "$tool_key" "$status" "$version" "$local_count"
    log "${tool_key}: ${local_count} findings (status=${status})"
  fi
done

# ---------------------------------------------------------------------------
# TIER 2: Language-detected tools (run if language detected, in parallel)
# ---------------------------------------------------------------------------
log "=== Tier 2: Language-detected tools ==="

TIER2_PIDS=""

# --- clippy (Rust) ---
if $HAS_RUST; then
  if command -v cargo >/dev/null 2>&1; then
    (
      run_tool clippy 120 cargo clippy --message-format=json --quiet -- -W clippy::all
    ) &
    TIER2_PIDS="$TIER2_PIDS $!"
  else
    log "clippy: cargo not installed (Rust files detected)"
    record_status "clippy" "not_installed" "null" 0 "install Rust toolchain"
  fi
else
  record_status "clippy" "skipped" "null" 0 "no .rs files in diff"
fi

# --- ruff (Python) ---
if $HAS_PYTHON; then
  if command -v ruff >/dev/null 2>&1; then
    (
      # Check if project has ruff config
      if [ -f "ruff.toml" ] || [ -f ".ruff.toml" ] || grep -q '\[tool\.ruff\]' pyproject.toml 2>/dev/null; then
        run_tool ruff 60 ruff check --output-format=json "${FILES[@]}"
      else
        run_tool ruff 60 ruff check --select=E,F,W --output-format=json "${FILES[@]}"
      fi
    ) &
    TIER2_PIDS="$TIER2_PIDS $!"
  else
    log "ruff: not installed (Python files detected)"
    record_status "ruff" "not_installed" "null" 0 "pip install ruff"
  fi
else
  record_status "ruff" "skipped" "null" 0 "no .py files in diff"
fi

# --- golangci-lint (Go) ---
if $HAS_GO; then
  if command -v golangci-lint >/dev/null 2>&1; then
    (
      if [ -f ".golangci.yml" ] || [ -f ".golangci.yaml" ] || [ -f ".golangci.json" ] || [ -f ".golangci.toml" ]; then
        run_tool golangci_lint 120 golangci-lint run --out-format=json ./...
      else
        run_tool golangci_lint 120 golangci-lint run --enable=govet,errcheck,staticcheck --out-format=json ./...
      fi
    ) &
    TIER2_PIDS="$TIER2_PIDS $!"
  else
    log "golangci-lint: not installed (Go files detected)"
    record_status "golangci_lint" "not_installed" "null" 0 "brew install golangci-lint"
  fi
else
  record_status "golangci_lint" "skipped" "null" 0 "no .go files in diff"
fi

# --- eslint (JS/TS — only if config exists) ---
if $HAS_JS; then
  # Check for eslint config
  ESLINT_CONFIG_EXISTS=false
  for cfg in .eslintrc .eslintrc.js .eslintrc.cjs .eslintrc.json .eslintrc.yml .eslintrc.yaml eslint.config.js eslint.config.mjs eslint.config.cjs eslint.config.ts; do
    if [ -f "$cfg" ]; then
      ESLINT_CONFIG_EXISTS=true
      break
    fi
  done

  if $ESLINT_CONFIG_EXISTS; then
    if command -v eslint >/dev/null 2>&1; then
      # Filter to only JS/TS files
      JS_FILES=()
      for f in "${FILES[@]}"; do
        case "$f" in
          *.ts|*.tsx|*.js|*.jsx) JS_FILES+=("$f") ;;
        esac
      done
      if [ ${#JS_FILES[@]} -gt 0 ]; then
        (
          run_tool eslint 60 eslint --format=json "${JS_FILES[@]}"
        ) &
        TIER2_PIDS="$TIER2_PIDS $!"
      else
        record_status "eslint" "skipped" "null" 0 "no JS/TS files after filtering"
      fi
    else
      log "eslint: not installed (JS/TS files + config detected)"
      record_status "eslint" "not_installed" "null" 0 "npm install eslint"
    fi
  else
    log "eslint: skipped (no eslint config found)"
    record_status "eslint" "skipped" "null" 0 "no eslint config found — project must opt-in"
  fi
else
  record_status "eslint" "skipped" "null" 0 "no JS/TS files in diff"
fi

# --- rubocop (Ruby) ---
if $HAS_RUBY; then
  if command -v rubocop >/dev/null 2>&1; then
    RUBY_FILES=()
    for f in "${FILES[@]}"; do
      case "$f" in *.rb|*.rake|*.gemspec) RUBY_FILES+=("$f") ;; esac
    done
    if [ ${#RUBY_FILES[@]} -gt 0 ]; then
      (run_tool rubocop 120 rubocop --format=json "${RUBY_FILES[@]}") &
      TIER2_PIDS="$TIER2_PIDS $!"
    else
      record_status "rubocop" "skipped" "null" 0 "no Ruby files after filtering"
    fi
  else
    log "rubocop: not installed (Ruby files detected)"
    record_status "rubocop" "not_installed" "null" 0 "gem install rubocop"
  fi
  # brakeman (Rails security — only if Gemfile contains gem 'rails')
  if command -v brakeman >/dev/null 2>&1 && [ -f "Gemfile" ] && grep -qE "gem ['\"]rails['\"]" Gemfile 2>/dev/null; then
    (run_tool brakeman 180 brakeman --format json --no-pager --quiet) &
    TIER2_PIDS="$TIER2_PIDS $!"
  else
    record_status "brakeman" "skipped" "null" 0 "no Rails project or brakeman not installed"
  fi
else
  record_status "rubocop" "skipped" "null" 0 "no .rb files in diff"
  record_status "brakeman" "skipped" "null" 0 "no .rb files in diff"
fi

# --- pmd (Java/Kotlin/Scala) ---
if $HAS_JAVA; then
  if command -v pmd >/dev/null 2>&1; then
    (
      # PMD 7+ uses 'pmd check'; PMD 6.x uses 'pmd' directly
      if pmd check --help >/dev/null 2>&1; then
        run_tool pmd 120 pmd check -d . -f json -R rulesets/java/quickstart.xml --no-progress
      else
        run_tool pmd 120 pmd -d . -f json -R rulesets/java/quickstart.xml
      fi
    ) &
    TIER2_PIDS="$TIER2_PIDS $!"
  else
    log "pmd: not installed (Java files detected)"
    record_status "pmd" "not_installed" "null" 0 "https://pmd.github.io/"
  fi
else
  record_status "pmd" "skipped" "null" 0 "no .java/.kt/.scala files in diff"
fi

# Wait for all Tier 2 tools
for pid in $TIER2_PIDS; do
  wait "$pid" 2>/dev/null || true
done
log "=== Tier 2 complete ==="

# Record status and normalize Tier 2 results
for tool_key in clippy ruff golangci_lint eslint rubocop brakeman pmd; do
  if [ -f "$SCRATCH/${tool_key}.status" ]; then
    status="$(cat "$SCRATCH/${tool_key}.status")"
    case "$tool_key" in
      golangci_lint) version="$(get_version golangci-lint)" ;;
      *)             version="$(get_version "$tool_key")" ;;
    esac

    case "$tool_key" in
      clippy)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/clippy.out" ]; then
            normalize_clippy < "$SCRATCH/clippy.out" > "$SCRATCH/findings/clippy.json"
            filter_to_changed_files "$SCRATCH/findings/clippy.json"
          else
            echo '[]' > "$SCRATCH/findings/clippy.json"
          fi
        fi
        ;;
      ruff)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/ruff.out" ]; then
            normalize_ruff < "$SCRATCH/ruff.out" > "$SCRATCH/findings/ruff.json"
          else
            echo '[]' > "$SCRATCH/findings/ruff.json"
          fi
        fi
        ;;
      golangci_lint)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/golangci_lint.out" ]; then
            normalize_golangci < "$SCRATCH/golangci_lint.out" > "$SCRATCH/findings/golangci_lint.json"
            filter_to_changed_files "$SCRATCH/findings/golangci_lint.json"
          else
            echo '[]' > "$SCRATCH/findings/golangci_lint.json"
          fi
        fi
        ;;
      eslint)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/eslint.out" ]; then
            normalize_eslint < "$SCRATCH/eslint.out" > "$SCRATCH/findings/eslint.json"
          else
            echo '[]' > "$SCRATCH/findings/eslint.json"
          fi
        fi
        ;;
      rubocop)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/rubocop.out" ]; then
            normalize_rubocop < "$SCRATCH/rubocop.out" > "$SCRATCH/findings/rubocop.json"
          else
            echo '[]' > "$SCRATCH/findings/rubocop.json"
          fi
        fi
        ;;
      brakeman)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/brakeman.out" ]; then
            normalize_brakeman < "$SCRATCH/brakeman.out" > "$SCRATCH/findings/brakeman.json"
          else
            echo '[]' > "$SCRATCH/findings/brakeman.json"
          fi
        fi
        ;;
      pmd)
        if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
          if [ -s "$SCRATCH/pmd.out" ]; then
            normalize_pmd < "$SCRATCH/pmd.out" > "$SCRATCH/findings/pmd.json"
            filter_to_changed_files "$SCRATCH/findings/pmd.json"
          else
            echo '[]' > "$SCRATCH/findings/pmd.json"
          fi
        fi
        ;;
    esac

    # Count and adjust status for tools that exit non-zero on findings
    local_count=0
    if [ -f "$SCRATCH/findings/${tool_key}.json" ]; then
      local_count="$(jq 'length // 0' "$SCRATCH/findings/${tool_key}.json" 2>/dev/null || echo 0)"
      case "$local_count" in
        ''|*[!0-9]*) local_count=0 ;;
      esac
    fi
    if [ "$status" = "failed" ]; then
      case "$tool_key" in
        ruff|eslint|golangci_lint|clippy|rubocop|brakeman|pmd)
          if [ "$local_count" -gt 0 ] 2>/dev/null; then
            status="ran"
          fi
          ;;
      esac
    fi

    record_status "$tool_key" "$status" "$version" "$local_count"
    log "${tool_key}: ${local_count} findings (status=${status})"
  fi
done

# ---------------------------------------------------------------------------
# TIER 3: Project-configured tools (pre-commit, sonarqube, project commands)
# ---------------------------------------------------------------------------
log "=== Tier 3: Project-configured tools ==="

TIER3_PIDS=""

# --- pre-commit ---
if [ -f .pre-commit-config.yaml ] && command -v pre-commit >/dev/null 2>&1; then
  (
    PRE_COMMIT_HOME="$PRE_COMMIT_HOME" \
    GOCACHE="$GOCACHE" \
    GOMODCACHE="$GOMODCACHE" \
    GOPATH="$GOPATH" \
    run_tool pre_commit 120 pre-commit run --files "${FILES[@]}"
  ) &
  TIER3_PIDS="$TIER3_PIDS $!"
elif [ -f .pre-commit-config.yaml ]; then
  log "pre-commit: not installed (.pre-commit-config.yaml exists)"
  record_status "pre_commit" "not_installed" "null" 0 "pip install pre-commit"
else
  record_status "pre_commit" "skipped" "null" 0 "no .pre-commit-config.yaml"
fi

# --- sonarqube ---
SONAR_SCRIPT="${HOME}/.claude/skills/sonarqube/scripts/sonarqube.py"
if [ ! -f "$SONAR_SCRIPT" ]; then
  SONAR_SCRIPT="${HOME}/.codex/skills/sonarqube/scripts/sonarqube.py"
fi
SONAR_OUT_DIR="$SCRATCH/sonar-out"
mkdir -p "$SONAR_OUT_DIR" 2>/dev/null || true

if [ -f "$SONAR_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  (
    run_tool sonarqube 180 python3 "$SONAR_SCRIPT" scan --mode local --severity medium --scope new \
      --base-ref "$BASE_REF" --list-only --output-dir "$SONAR_OUT_DIR"
  ) &
  TIER3_PIDS="$TIER3_PIDS $!"
else
  log "sonarqube: skipped (skill not installed)"
  record_status "sonarqube" "skipped" "null" 0 "sonarqube skill not installed"
fi

# --- Project commands from --project-profile ---
if [ -n "$PROJECT_PROFILE" ] && [ -f "$PROJECT_PROFILE" ]; then
  log "Processing project profile: $PROJECT_PROFILE"

  # Extract lint/check commands from the interpreted project profile.
  # The orchestrating agent writes this file after interpreting discover-project.py output.
  # Expected format: { "contexts": [{ "lint_commands": ["cmd1", "cmd2"] }] }
  # Also supports: { "commands": [{ "cmd": "cmd1" }] } for backwards compatibility.
  PROJ_CMD_INDEX=0
  PROJ_CMDS_JSON="$(jq -r '
    [
      # Format 1: contexts[].lint_commands[] (array of strings)
      ((.contexts // [])[] | (.lint_commands // [])[] | select(. != null and . != "")),
      # Format 2: commands[].cmd (array of objects with cmd field)
      ((.commands // [])[] | .cmd // empty | select(. != null and . != ""))
    ] | unique | .[]
  ' "$PROJECT_PROFILE" 2>/dev/null || true)"

  if [ -n "$PROJ_CMDS_JSON" ]; then
    while IFS= read -r cmd; do
      [ -z "$cmd" ] && continue
      PROJ_CMD_INDEX=$((PROJ_CMD_INDEX + 1))
      proj_key="project_cmd_${PROJ_CMD_INDEX}"
      log "project command ${PROJ_CMD_INDEX}: ${cmd}"
      # Validate command against allowlist before execution
      proj_tool=$(echo "$cmd" | awk '{print $1}')
      case "$proj_tool" in
        npm|npx|yarn|pnpm|bundle|rake|make|just|task|gradle|mvn|cargo|go|ruff|eslint|rubocop|pmd|checkstyle|prettier|black|isort|mypy|pylint|flake8)
          (
            # Run allowed project command with a 120s timeout
            # shellcheck disable=SC2086
            run_tool "$proj_key" 120 $cmd
          ) &
          ;;
        *)
          log "project command ${PROJ_CMD_INDEX}: BLOCKED (tool '$proj_tool' not in allowlist)"
          record_status "$proj_key" "sandbox_blocked" "null" 0 "tool '$proj_tool' not in allowlist"
          continue
          ;;
      esac
      TIER3_PIDS="$TIER3_PIDS $!"
    done <<PROJEOF
$PROJ_CMDS_JSON
PROJEOF
  fi
fi

# Wait for all Tier 3 tools
for pid in $TIER3_PIDS; do
  wait "$pid" 2>/dev/null || true
done
log "=== Tier 3 complete ==="

# Normalize Tier 3 results
# pre-commit
if [ -f "$SCRATCH/pre_commit.status" ]; then
  status="$(cat "$SCRATCH/pre_commit.status")"
  version="$(get_version pre-commit)"
  if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
    if [ -s "$SCRATCH/pre_commit.out" ]; then
      normalize_precommit < "$SCRATCH/pre_commit.out" > "$SCRATCH/findings/pre_commit.json"
    else
      echo '[]' > "$SCRATCH/findings/pre_commit.json"
    fi
    # pre-commit exits non-zero when hooks fail — that's normal
    local_count=0
    if [ -f "$SCRATCH/findings/pre_commit.json" ]; then
      local_count="$(jq 'length // 0' "$SCRATCH/findings/pre_commit.json" 2>/dev/null || echo 0)"
      case "$local_count" in ''|*[!0-9]*) local_count=0 ;; esac
    fi
    if [ "$status" = "failed" ] && [ "$local_count" -gt 0 ] 2>/dev/null; then
      status="ran"
    fi
    record_status "pre_commit" "$status" "$version" "$local_count"
    log "pre_commit: ${local_count} findings (status=${status})"
  else
    record_status "pre_commit" "$status" "$version" 0
  fi
fi

# sonarqube
if [ -f "$SCRATCH/sonarqube.status" ]; then
  status="$(cat "$SCRATCH/sonarqube.status")"
  version="$(get_version sonarqube)"
  if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
    # sonarqube output may be in the output dir or in the tool's stdout
    SONAR_FINDINGS=""
    if [ -f "$SONAR_OUT_DIR/findings.json" ]; then
      SONAR_FINDINGS="$SONAR_OUT_DIR/findings.json"
    elif [ -s "$SCRATCH/sonarqube.out" ]; then
      SONAR_FINDINGS="$SCRATCH/sonarqube.out"
    fi
    if [ -n "$SONAR_FINDINGS" ]; then
      normalize_sonarqube < "$SONAR_FINDINGS" > "$SCRATCH/findings/sonarqube.json"
    else
      echo '[]' > "$SCRATCH/findings/sonarqube.json"
    fi
    local_count=0
    if [ -f "$SCRATCH/findings/sonarqube.json" ]; then
      local_count="$(jq 'length // 0' "$SCRATCH/findings/sonarqube.json" 2>/dev/null || echo 0)"
      case "$local_count" in ''|*[!0-9]*) local_count=0 ;; esac
    fi
    record_status "sonarqube" "$status" "$version" "$local_count"
    log "sonarqube: ${local_count} findings (status=${status})"
  else
    record_status "sonarqube" "$status" "$version" 0
  fi
fi

# Project commands
if [ -n "${PROJ_CMD_INDEX:-}" ] && [ "$PROJ_CMD_INDEX" -gt 0 ] 2>/dev/null; then
  i=1
  while [ "$i" -le "$PROJ_CMD_INDEX" ]; do
    proj_key="project_cmd_${i}"
    if [ -f "$SCRATCH/${proj_key}.status" ]; then
      status="$(cat "$SCRATCH/${proj_key}.status")"
      if [ "$status" = "ran" ] || [ "$status" = "failed" ]; then
        if [ -s "$SCRATCH/${proj_key}.out" ]; then
          normalize_project_cmd "$proj_key" < "$SCRATCH/${proj_key}.out" > "$SCRATCH/findings/${proj_key}.json"
        else
          echo '[]' > "$SCRATCH/findings/${proj_key}.json"
        fi
        local_count=0
        if [ -f "$SCRATCH/findings/${proj_key}.json" ]; then
          local_count="$(jq 'length // 0' "$SCRATCH/findings/${proj_key}.json" 2>/dev/null || echo 0)"
          case "$local_count" in ''|*[!0-9]*) local_count=0 ;; esac
        fi
        record_status "$proj_key" "$status" "unknown" "$local_count"
      else
        record_status "$proj_key" "$status" "unknown" 0
      fi
    fi
    i=$((i + 1))
  done
fi

# ---------------------------------------------------------------------------
# Merge all findings, add IDs, deduplicate
# ---------------------------------------------------------------------------
log "=== Merging and deduplicating findings ==="

# Concatenate all finding arrays into one
ALL_FINDINGS="$SCRATCH/all_findings.json"
echo '[]' > "$ALL_FINDINGS"

for ffile in "$SCRATCH"/findings/*.json; do
  [ -f "$ffile" ] || continue
  # Merge into accumulated array
  jq -s '.[0] + .[1]' "$ALL_FINDINGS" "$ffile" > "$SCRATCH/merged_tmp.json" 2>/dev/null \
    && mv "$SCRATCH/merged_tmp.json" "$ALL_FINDINGS"
done

# Add IDs and deduplicate on file:line:summary key
# On collision: keep highest severity, merge tool names into sources array
DEDUPED="$SCRATCH/deduped_findings.json"
jq '
  # Severity ordering for comparison
  def sev_rank:
    if . == "critical" then 4
    elif . == "high" then 3
    elif . == "medium" then 2
    elif . == "low" then 1
    else 0
    end;

  # Normalize summary for dedup key (lowercase, trim whitespace)
  def norm_summary: ascii_downcase | gsub("^\\s+|\\s+$"; "") | gsub("\\s+"; " ");

  # Group by dedup key
  group_by(.file + ":" + (.line | tostring) + ":" + (.summary | norm_summary))
  | map(
    sort_by(-(.severity | sev_rank))
    | .[0] as $best
    | {
        file: $best.file,
        line: $best.line,
        summary: $best.summary,
        severity: $best.severity,
        confidence: $best.confidence,
        evidence: (map(.evidence // "") | map(select(. != "")) | unique | join("; ")),
        pass: $best.pass,
        source: "deterministic",
        tool: $best.tool,
        sources: (map(.tool) | unique)
      }
  )
  | to_entries
  | map(
    .value + {
      id: (.value.pass + "-" + (.value.file | gsub("[^a-zA-Z0-9]"; "") | .[0:12]) + "-" + (.value.line | tostring) + "-" + (.key | tostring))
    }
  )
  | map(del(.tool))
' "$ALL_FINDINGS" > "$DEDUPED" 2>/dev/null || echo '[]' > "$DEDUPED"

TOTAL_FINDINGS="$(jq 'length' "$DEDUPED" 2>/dev/null || echo 0)"
log "Total deduplicated findings: ${TOTAL_FINDINGS}"

# ---------------------------------------------------------------------------
# Assemble tool_status object
# ---------------------------------------------------------------------------
TOOL_STATUS="$SCRATCH/tool_status.json"
echo '{}' > "$TOOL_STATUS"

for sfile in "$SCRATCH"/status/*.json; do
  [ -f "$sfile" ] || continue
  key="$(basename "$sfile" .json)"
  jq --arg k "$key" --slurpfile v "$sfile" '. + {($k): $v[0]}' "$TOOL_STATUS" \
    > "$SCRATCH/status_tmp.json" 2>/dev/null \
    && mv "$SCRATCH/status_tmp.json" "$TOOL_STATUS"
done

# ---------------------------------------------------------------------------
# Build _timing object from per-tool .ms and .status files
# ---------------------------------------------------------------------------
SCAN_END_MS="$(_get_epoch_ms)"
TIMING_JSON="$SCRATCH/timing.json"

# Collect per-tool timing into a JSON object
echo '{}' > "$TIMING_JSON"
for ms_file in "$SCRATCH"/*.ms; do
  [ -f "$ms_file" ] || continue
  tkey="$(basename "$ms_file" .ms)"
  tms="$(cat "$ms_file" 2>/dev/null || echo 0)"
  case "$tms" in ''|*[!0-9]*) tms=0 ;; esac
  # Determine status for this tool
  tstatus="not_installed"
  if [ -f "$SCRATCH/${tkey}.status" ]; then
    tstatus="$(cat "$SCRATCH/${tkey}.status")"
  elif [ -f "$SCRATCH/status/${tkey}.json" ]; then
    tstatus="$(jq -r '.status // "unknown"' "$SCRATCH/status/${tkey}.json" 2>/dev/null || echo "unknown")"
  fi
  jq --arg k "$tkey" --argjson ms "$tms" --arg st "$tstatus" \
    '. + {($k): {ms: $ms, status: $st}}' "$TIMING_JSON" \
    > "$SCRATCH/timing_tmp.json" 2>/dev/null \
    && mv "$SCRATCH/timing_tmp.json" "$TIMING_JSON"
done

# Also add tools that were not_installed/skipped (no .ms file) with ms=0
for sfile in "$SCRATCH"/status/*.json; do
  [ -f "$sfile" ] || continue
  skey="$(basename "$sfile" .json)"
  if [ ! -f "$SCRATCH/${skey}.ms" ]; then
    sstatus="$(jq -r '.status // "unknown"' "$sfile" 2>/dev/null || echo "unknown")"
    jq --arg k "$skey" --arg st "$sstatus" \
      'if .[$k] then . else . + {($k): {ms: 0, status: $st}} end' "$TIMING_JSON" \
      > "$SCRATCH/timing_tmp.json" 2>/dev/null \
      && mv "$SCRATCH/timing_tmp.json" "$TIMING_JSON"
  fi
done

TOTAL_SCAN_MS=$((SCAN_END_MS - SCAN_START_MS))
case "$TOTAL_SCAN_MS" in ''|*[!0-9]*) TOTAL_SCAN_MS=0 ;; esac

# ---------------------------------------------------------------------------
# Final output: JSON to stdout
# ---------------------------------------------------------------------------
jq -n --slurpfile findings "$DEDUPED" --slurpfile status "$TOOL_STATUS" \
  --slurpfile timing "$TIMING_JSON" --argjson total_ms "$TOTAL_SCAN_MS" \
  '{
    findings: $findings[0],
    tool_status: $status[0],
    _timing: {
      total_ms: $total_ms,
      steps: ($timing[0] | to_entries | map({name: .key, duration_ms: (.value.ms // .value // 0)})),
      marks: []
    }
  }'

log "=== run-scans.sh complete ==="

# Cleanup scratch (keep for debugging if CODEREVIEW_KEEP_SCRATCH is set)
if [ -z "${CODEREVIEW_KEEP_SCRATCH:-}" ]; then
  rm -rf "$SCRATCH"
fi

exit 0
