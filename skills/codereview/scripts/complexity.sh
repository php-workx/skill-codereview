#!/usr/bin/env bash
# complexity.sh — Run cyclomatic complexity analysis on changed files
# Usage: echo "$CHANGED_FILES" | bash scripts/complexity.sh
#
# Input:  CHANGED_FILES on stdin (newline-delimited file paths)
# Output: JSON to stdout:
#   {
#     "hotspots": [
#       { "file": "src/auth.py", "function": "validate_token", "score": 15, "rating": "C" }
#     ],
#     "tool_status": {
#       "radon":   { "status": "ran", "version": "5.1.0", "finding_count": 2, "note": null },
#       "gocyclo": { "status": "not_installed", "version": null, "finding_count": 0, "note": "..." }
#     }
#   }
#
# Only reports functions rated C or worse (complexity >= 11 for radon, > 10 for gocyclo).
# Best-effort: exits 0 even when no tools are installed.
# Bash 3 compatible (macOS default).

set -uo pipefail

# --- Require jq ---
if ! command -v jq &>/dev/null; then
  echo '{"hotspots":[],"tool_status":{"radon":{"status":"skipped","version":null,"finding_count":0,"note":"jq not installed"},"gocyclo":{"status":"skipped","version":null,"finding_count":0,"note":"jq not installed"}}}'
  exit 0
fi

# --- Read file list from stdin (safe file-path handling) ---
PY_FILES=()
GO_FILES=()
RUBY_FILES=()
JAVA_FILES=()

while IFS= read -r file; do
  # Skip empty lines
  [ -z "$file" ] && continue
  case "$file" in
    *.py)    PY_FILES[${#PY_FILES[@]}]="$file" ;;
    *.go)    GO_FILES[${#GO_FILES[@]}]="$file" ;;
    *.rb)    RUBY_FILES[${#RUBY_FILES[@]}]="$file" ;;
    *.java)  JAVA_FILES[${#JAVA_FILES[@]}]="$file" ;;
  esac
done

# --- Accumulators ---
# We build hotspots as newline-delimited JSON objects, then assemble at the end
HOTSPOT_LINES=""

RADON_STATUS="skipped"
RADON_VERSION="null"
RADON_COUNT=0
RADON_NOTE="null"

GOCYCLO_STATUS="skipped"
GOCYCLO_VERSION="null"
GOCYCLO_COUNT=0
GOCYCLO_NOTE="null"

FLOG_STATUS="skipped"
FLOG_VERSION="null"
FLOG_COUNT=0
FLOG_NOTE="null"

PMD_CPX_STATUS="skipped"
PMD_CPX_VERSION="null"
PMD_CPX_COUNT=0
PMD_CPX_NOTE="null"

# --- Python: radon ---
if [ ${#PY_FILES[@]} -eq 0 ]; then
  RADON_STATUS="skipped"
  RADON_NOTE="\"no .py files in changeset\""
elif ! command -v radon &>/dev/null; then
  RADON_STATUS="not_installed"
  RADON_NOTE="\"pip install radon\""
else
  RADON_VERSION=$(radon --version 2>/dev/null | head -1 || echo "unknown")
  # Strip to just version number if possible
  RADON_VERSION=$(echo "$RADON_VERSION" | sed 's/[^0-9.]//g')
  if [ -z "$RADON_VERSION" ]; then
    RADON_VERSION="unknown"
  fi
  RADON_VERSION="\"$RADON_VERSION\""

  # Run radon cc with -s (show score) on each file
  # radon cc output format (per file):
  #   path/to/file.py
  #       F 42:0 validate_token - C (15)
  #       M 10:4 ClassName.method - B (8)
  # We want lines with rating C or worse (C, D, F) — score >= 11
  CURRENT_FILE=""
  while IFS= read -r line; do
    # File header line: no leading whitespace, ends with .py
    case "$line" in
      *".py")
        # Trim whitespace
        CURRENT_FILE=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        continue
        ;;
    esac

    # Function/method line: leading whitespace, contains " - " and a rating
    # Format: "    F 42:0 function_name - C (15)"
    # or:     "    M 10:4 ClassName.method_name - D (25)"
    # Extract: function_name, rating letter, score
    if echo "$line" | grep -qE '^[[:space:]]+[FMCG][[:space:]]+[0-9]+:[0-9]+[[:space:]]+.+ - [A-F] \([0-9]+\)'; then
      # Parse the components
      FUNC_NAME=$(echo "$line" | sed -E 's/^[[:space:]]+[FMCG][[:space:]]+[0-9]+:[0-9]+[[:space:]]+(.+)[[:space:]]+-[[:space:]]+[A-F][[:space:]]+\([0-9]+\).*/\1/')
      RATING=$(echo "$line" | sed -E 's/.*[[:space:]]-[[:space:]]+([A-F])[[:space:]]+\([0-9]+\).*/\1/')
      SCORE=$(echo "$line" | sed -E 's/.*\(([0-9]+)\).*/\1/')

      # Only report C or worse (score >= 11)
      if [ "$SCORE" -ge 11 ] 2>/dev/null; then
        HOTSPOT=$(jq -n \
          --arg file "$CURRENT_FILE" \
          --arg func "$FUNC_NAME" \
          --argjson score "$SCORE" \
          --arg rating "$RATING" \
          '{file: $file, function: $func, score: $score, rating: $rating}')
        if [ -n "$HOTSPOT_LINES" ]; then
          HOTSPOT_LINES="$HOTSPOT_LINES
$HOTSPOT"
        else
          HOTSPOT_LINES="$HOTSPOT"
        fi
        RADON_COUNT=$((RADON_COUNT + 1))
      fi
    fi
  done < <(radon cc -s "${PY_FILES[@]}" 2>/dev/null || true)

  RADON_STATUS="ran"
fi

# --- Go: gocyclo ---
if [ ${#GO_FILES[@]} -eq 0 ]; then
  GOCYCLO_STATUS="skipped"
  GOCYCLO_NOTE="\"no .go files in changeset\""
elif ! command -v gocyclo &>/dev/null; then
  GOCYCLO_STATUS="not_installed"
  GOCYCLO_NOTE="\"go install github.com/fzipp/gocyclo/cmd/gocyclo@latest\""
else
  GOCYCLO_VERSION=$(gocyclo --version 2>/dev/null || echo "unknown")
  GOCYCLO_VERSION=$(echo "$GOCYCLO_VERSION" | sed 's/[^0-9.]//g')
  if [ -z "$GOCYCLO_VERSION" ]; then
    GOCYCLO_VERSION="unknown"
  fi
  GOCYCLO_VERSION="\"$GOCYCLO_VERSION\""

  # Run gocyclo -over 10 on each file
  # gocyclo output format: "score package function file.go:line:col"
  # e.g.: "12 main complex /tmp/test_complex.go:3:1"
  while IFS= read -r line; do
    [ -z "$line" ] && continue

    # Parse: score package function file:line:col
    SCORE=$(echo "$line" | awk '{print $1}')
    PKG_NAME=$(echo "$line" | awk '{print $2}')
    FUNC_BARE=$(echo "$line" | awk '{print $3}')
    FILE_LOC=$(echo "$line" | awk '{print $4}')
    FILE_PATH=$(echo "$FILE_LOC" | cut -d: -f1)
    # Combine package.function for display
    FUNC_NAME="${PKG_NAME}.${FUNC_BARE}"

    # gocyclo -over 10 already filters, but validate score > 10
    if [ "$SCORE" -gt 10 ] 2>/dev/null; then
      # Map score to radon-style rating
      if [ "$SCORE" -le 20 ]; then
        RATING="C"
      elif [ "$SCORE" -le 30 ]; then
        RATING="D"
      else
        RATING="F"
      fi

      HOTSPOT=$(jq -n \
        --arg file "$FILE_PATH" \
        --arg func "$FUNC_NAME" \
        --argjson score "$SCORE" \
        --arg rating "$RATING" \
        '{file: $file, function: $func, score: $score, rating: $rating}')
      if [ -n "$HOTSPOT_LINES" ]; then
        HOTSPOT_LINES="$HOTSPOT_LINES
$HOTSPOT"
      else
        HOTSPOT_LINES="$HOTSPOT"
      fi
      GOCYCLO_COUNT=$((GOCYCLO_COUNT + 1))
    fi
  done < <(gocyclo -over 10 "${GO_FILES[@]}" 2>/dev/null || true)

  GOCYCLO_STATUS="ran"
fi

# --- Ruby: flog ---
if [ ${#RUBY_FILES[@]} -eq 0 ]; then
  FLOG_STATUS="skipped"
  FLOG_NOTE="\"no .rb files in changeset\""
elif ! command -v flog &>/dev/null; then
  FLOG_STATUS="not_installed"
  FLOG_NOTE="\"gem install flog\""
else
  FLOG_VERSION=$(flog --version 2>/dev/null | head -1 || echo "unknown")
  FLOG_VERSION=$(echo "$FLOG_VERSION" | sed 's/[^0-9.]//g')
  if [ -z "$FLOG_VERSION" ]; then FLOG_VERSION="unknown"; fi
  FLOG_VERSION="\"$FLOG_VERSION\""

  # flog output: "  score: Class#method path/file.rb:line"
  # Parse methods with score >= 11 (C or worse)
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # Match lines like "    13.2: SomeClass#method_name"
    SCORE=$(echo "$line" | sed -nE 's/^[[:space:]]+([0-9]+)\.[0-9]+:.*/\1/p')
    [ -z "$SCORE" ] && continue
    if [ "$SCORE" -ge 11 ] 2>/dev/null; then
      FUNC_NAME=$(echo "$line" | sed -E 's/^[[:space:]]+[0-9.]+:[[:space:]]*//')
      if [ "$SCORE" -ge 31 ]; then RATING="F"
      elif [ "$SCORE" -ge 21 ]; then RATING="D"
      else RATING="C"
      fi
      # Try to extract file from the function name (flog may include file:line)
      FILE_PART=$(echo "$FUNC_NAME" | grep -oE '[^ ]+\.rb(:[0-9]+)?' | head -1 || echo "")
      if [ -z "$FILE_PART" ]; then FILE_PART="unknown"; fi
      HOTSPOT=$(jq -n \
        --arg file "$FILE_PART" \
        --arg func "$FUNC_NAME" \
        --argjson score "$SCORE" \
        --arg rating "$RATING" \
        '{file: $file, function: $func, score: $score, rating: $rating}')
      if [ -n "$HOTSPOT_LINES" ]; then
        HOTSPOT_LINES="$HOTSPOT_LINES
$HOTSPOT"
      else
        HOTSPOT_LINES="$HOTSPOT"
      fi
      FLOG_COUNT=$((FLOG_COUNT + 1))
    fi
  done < <(flog --all "${RUBY_FILES[@]}" 2>/dev/null || true)

  FLOG_STATUS="ran"
fi

# --- Java: PMD cyclomatic complexity ---
if [ ${#JAVA_FILES[@]} -eq 0 ]; then
  PMD_CPX_STATUS="skipped"
  PMD_CPX_NOTE="\"no .java files in changeset\""
elif ! command -v pmd &>/dev/null; then
  PMD_CPX_STATUS="not_installed"
  PMD_CPX_NOTE="\"https://pmd.github.io/\""
else
  PMD_CPX_VERSION=$(pmd --version 2>/dev/null | head -1 || echo "unknown")
  PMD_CPX_VERSION=$(echo "$PMD_CPX_VERSION" | sed 's/[^0-9.]//g')
  if [ -z "$PMD_CPX_VERSION" ]; then PMD_CPX_VERSION="unknown"; fi
  PMD_CPX_VERSION="\"$PMD_CPX_VERSION\""

  # Run PMD design rules (includes CyclomaticComplexity)
  PMD_CPX_OUT=""
  if pmd check --help >/dev/null 2>&1; then
    PMD_CPX_OUT=$(pmd check -d . -f json -R category/java/design.xml --no-progress 2>/dev/null || true)
  else
    PMD_CPX_OUT=$(pmd -d . -f json -R category/java/design.xml 2>/dev/null || true)
  fi

  if [ -n "$PMD_CPX_OUT" ]; then
    # Extract CyclomaticComplexity violations and map to hotspots
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      FILE=$(echo "$line" | jq -r '.file // "unknown"')
      FUNC=$(echo "$line" | jq -r '.desc // "unknown"')
      SCORE=$(echo "$line" | jq -r '.score // 0')
      if [ "$SCORE" -ge 11 ] 2>/dev/null; then
        if [ "$SCORE" -ge 31 ]; then RATING="F"
        elif [ "$SCORE" -ge 21 ]; then RATING="D"
        else RATING="C"
        fi
        HOTSPOT=$(jq -n \
          --arg file "$FILE" \
          --arg func "$FUNC" \
          --argjson score "$SCORE" \
          --arg rating "$RATING" \
          '{file: $file, function: $func, score: $score, rating: $rating}')
        if [ -n "$HOTSPOT_LINES" ]; then
          HOTSPOT_LINES="$HOTSPOT_LINES
$HOTSPOT"
        else
          HOTSPOT_LINES="$HOTSPOT"
        fi
        PMD_CPX_COUNT=$((PMD_CPX_COUNT + 1))
      fi
    done < <(echo "$PMD_CPX_OUT" | jq -c '
      [(.files // [])[] |
        .filename as $fp |
        (.violations // [])[] |
        select(.rule | test("Cyclomatic|NPath")) |
        {
          file: $fp,
          desc: .description,
          score: (.description | capture("of (?<n>[0-9]+)") | .n | tonumber)
        }
      ] | .[]
    ' 2>/dev/null || true)
  fi

  PMD_CPX_STATUS="ran"
fi

# --- Assemble JSON output ---

# Build hotspots array from accumulated lines
if [ -n "$HOTSPOT_LINES" ]; then
  HOTSPOTS_JSON=$(echo "$HOTSPOT_LINES" | jq -s '.')
else
  HOTSPOTS_JSON="[]"
fi

# Build tool_status object
TOOL_STATUS=$(jq -n \
  --arg rs "$RADON_STATUS" \
  --argjson rv "$RADON_VERSION" \
  --argjson rc "$RADON_COUNT" \
  --argjson rn "$RADON_NOTE" \
  --arg gs "$GOCYCLO_STATUS" \
  --argjson gv "$GOCYCLO_VERSION" \
  --argjson gc "$GOCYCLO_COUNT" \
  --argjson gn "$GOCYCLO_NOTE" \
  --arg fs "$FLOG_STATUS" \
  --argjson fv "$FLOG_VERSION" \
  --argjson fc "$FLOG_COUNT" \
  --argjson fn "$FLOG_NOTE" \
  --arg ps "$PMD_CPX_STATUS" \
  --argjson pv "$PMD_CPX_VERSION" \
  --argjson pc "$PMD_CPX_COUNT" \
  --argjson pn "$PMD_CPX_NOTE" \
  '{
    radon: { status: $rs, version: $rv, finding_count: $rc, note: $rn },
    gocyclo: { status: $gs, version: $gv, finding_count: $gc, note: $gn },
    flog: { status: $fs, version: $fv, finding_count: $fc, note: $fn },
    pmd_complexity: { status: $ps, version: $pv, finding_count: $pc, note: $pn }
  }')

# Combine into final output
jq -n \
  --argjson hotspots "$HOTSPOTS_JSON" \
  --argjson tool_status "$TOOL_STATUS" \
  '{ hotspots: $hotspots, tool_status: $tool_status }'

exit 0
