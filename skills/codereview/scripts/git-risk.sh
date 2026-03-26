#!/usr/bin/env bash
# git-risk.sh — Compute per-file risk scores from git history
# Usage: echo "$CHANGED_FILES" | bash scripts/git-risk.sh [--months N]
#
# Input:  CHANGED_FILES on stdin (newline-delimited file paths)
# Output: JSON to stdout:
#   {
#     "shallow_clone": false,
#     "lookback_months": 6,
#     "files": [
#       { "file": "src/auth.py", "churn": 14, "bug_commits": 3, "last_bug": "2026-03-13", "risk": "high" }
#     ],
#     "summary": { "high": 1, "medium": 2, "low": 5 }
#   }
#
# Risk tier rules:
#   high:   BUG_COMMITS >= 3 OR (BUG_COMMITS >= 2 AND CHURN >= 10)
#   medium: BUG_COMMITS >= 1 OR CHURN >= 8
#   low:    otherwise
#
# Best-effort: exits 0 even on errors.
# Bash 3 compatible (macOS default).

set -uo pipefail

# --- Parse arguments ---
MONTHS=6

while [ $# -gt 0 ]; do
  case "$1" in
    --months)
      shift
      MONTHS="${1:-6}"
      ;;
  esac
  shift
done

# --- Require jq ---
if ! command -v jq &>/dev/null; then
  cat <<'NOJQ'
{"shallow_clone":false,"lookback_months":6,"files":[],"summary":{"high":0,"medium":0,"low":0},"warning":"jq not installed — git history risk scoring skipped"}
NOJQ
  exit 0
fi

# --- Shallow clone detection ---
SHALLOW_CLONE="false"
WARNING=""

COMMIT_COUNT=$(git rev-list --count HEAD 2>/dev/null || echo "0")
if [ "$COMMIT_COUNT" -lt 50 ] 2>/dev/null; then
  SHALLOW_CLONE="true"
  WARNING="Shallow clone detected — git history risk scores may be incomplete"
fi

# --- Read file list from stdin (safe file-path handling) ---
FILES=()

while IFS= read -r file || [ -n "$file" ]; do
  # Skip empty lines
  [ -z "$file" ] && continue
  FILES[${#FILES[@]}]="$file"
done

# --- Compute per-file risk scores ---
FILE_LINES=""
HIGH=0
MEDIUM=0
LOW=0

for file in "${FILES[@]+${FILES[@]}}"; do
  [ -z "$file" ] && continue

  # 1. Churn: total commits touching this file in lookback period
  CHURN=$(git log --oneline --follow --since="${MONTHS} months ago" -- "$file" 2>/dev/null | wc -l | tr -d ' ')

  # 2. Bug signal: commits with fix/bug/revert/hotfix in message
  BUG_COMMITS=$(git log --oneline --follow --since="${MONTHS} months ago" --grep='fix\|bug\|revert\|hotfix' -i -- "$file" 2>/dev/null | wc -l | tr -d ' ')

  # 3. Recency: date of last bug-related commit
  LAST_BUG=$(git log -1 --format='%as' --follow --since="${MONTHS} months ago" --grep='fix\|bug\|revert\|hotfix' -i -- "$file" 2>/dev/null)

  # Default to empty string if no bug commit found
  if [ -z "$LAST_BUG" ]; then
    LAST_BUG_JSON="null"
  else
    LAST_BUG_JSON="\"$LAST_BUG\""
  fi

  # Ensure numeric values
  CHURN=${CHURN:-0}
  BUG_COMMITS=${BUG_COMMITS:-0}

  # Risk tier assignment (deterministic rules)
  if [ "$BUG_COMMITS" -ge 3 ] 2>/dev/null || { [ "$BUG_COMMITS" -ge 2 ] 2>/dev/null && [ "$CHURN" -ge 10 ] 2>/dev/null; }; then
    RISK="high"
    HIGH=$((HIGH + 1))
  elif [ "$BUG_COMMITS" -ge 1 ] 2>/dev/null || [ "$CHURN" -ge 8 ] 2>/dev/null; then
    RISK="medium"
    MEDIUM=$((MEDIUM + 1))
  else
    RISK="low"
    LOW=$((LOW + 1))
  fi

  FILE_ENTRY=$(jq -n \
    --arg file "$file" \
    --argjson churn "$CHURN" \
    --argjson bug_commits "$BUG_COMMITS" \
    --argjson last_bug "$LAST_BUG_JSON" \
    --arg risk "$RISK" \
    '{file: $file, churn: $churn, bug_commits: $bug_commits, last_bug: $last_bug, risk: $risk}')

  if [ -n "$FILE_LINES" ]; then
    FILE_LINES="$FILE_LINES
$FILE_ENTRY"
  else
    FILE_LINES="$FILE_ENTRY"
  fi
done

# --- Assemble JSON output ---

# Build files array from accumulated lines
if [ -n "$FILE_LINES" ]; then
  FILES_JSON=$(echo "$FILE_LINES" | jq -s '.')
else
  FILES_JSON="[]"
fi

# Build output
if [ -n "$WARNING" ]; then
  jq -n \
    --argjson shallow_clone "$SHALLOW_CLONE" \
    --argjson lookback_months "$MONTHS" \
    --argjson files "$FILES_JSON" \
    --argjson high "$HIGH" \
    --argjson medium "$MEDIUM" \
    --argjson low "$LOW" \
    --arg warning "$WARNING" \
    '{
      shallow_clone: $shallow_clone,
      lookback_months: $lookback_months,
      files: $files,
      summary: { high: $high, medium: $medium, low: $low },
      warning: $warning
    }'
else
  jq -n \
    --argjson shallow_clone "$SHALLOW_CLONE" \
    --argjson lookback_months "$MONTHS" \
    --argjson files "$FILES_JSON" \
    --argjson high "$HIGH" \
    --argjson medium "$MEDIUM" \
    --argjson low "$LOW" \
    '{
      shallow_clone: $shallow_clone,
      lookback_months: $lookback_months,
      files: $files,
      summary: { high: $high, medium: $medium, low: $low }
    }'
fi

exit 0
