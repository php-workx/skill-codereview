#!/usr/bin/env bash
# validate_output.sh — Validate codereview skill output artifacts
# Usage: validate_output.sh --findings <json> [--report <md>]
#
# Checks:
#  1.  findings.json is valid JSON
#  2.  Required envelope fields (run_id, timestamp, scope, base_ref, head_ref, verdict, verdict_reason, strengths, files_reviewed, tool_status, findings, tier_summary)
#  2b. Verdict value is PASS/WARN/FAIL
#  2c. Strengths is an array (if present)
#  2d. Spec gaps is an array (if present)
#  3.  Each finding has required fields (id, source, pass, severity, confidence, file, line, summary)
#  3b. Optional sources field (if present) is an array
#  4.  Confidence gating: no AI findings below 0.65
#  5.  Evidence gating: high/critical findings have failure_mode populated
#  6.  Valid severity values
#  7.  Valid source values
#  7b. Valid pass values
#  8.  Tool status present and non-empty
#  8b. Tool status values valid (ran/skipped/failed/not_installed/sandbox_blocked)
#  9.  Action tier classification on every finding
#  9b. Valid action_tier values
#  10. Tier summary consistency
#  10b. review_mode field validation (standard/chunked)
#  10c. Chunked mode: chunk metadata validation (chunk_count, chunks array, required fields, risk_tier)
#  11. Report markdown: verdict, strengths, tier sections, summary
#  12. spec_requirements array validation (if present)
#  12a. Required fields (id, text, source_section, impl_status)
#  12b. Valid impl_status values
#  12c. Valid priority values (if present)
#  12d. Valid test_coverage.status (if present)
#  12e. Valid test category values (if present)

set -euo pipefail

FINDINGS=""
REPORT=""
ERRORS=0

usage() {
  echo "Usage: $0 --findings <json> [--report <md>]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --findings) FINDINGS="$2"; shift 2 ;;
    --report)   REPORT="$2";   shift 2 ;;
    *)          usage ;;
  esac
done

[ -z "$FINDINGS" ] && usage

if ! command -v jq &>/dev/null; then
  echo "FAIL: jq is required but not installed"
  exit 1
fi

echo "Validating: $FINDINGS"
echo "---"

# 1. Valid JSON
if ! jq empty "$FINDINGS" 2>/dev/null; then
  echo "FAIL: Not valid JSON"
  exit 1
fi
echo "PASS: Valid JSON"

# 2. Required envelope fields
ENVELOPE_ERRORS=0
for field in run_id timestamp scope base_ref head_ref verdict verdict_reason strengths files_reviewed tool_status findings tier_summary; do
  if [ "$(jq "has(\"$field\")" "$FINDINGS")" != "true" ]; then
    echo "FAIL: Missing required envelope field: $field"
    ENVELOPE_ERRORS=$((ENVELOPE_ERRORS + 1))
    ERRORS=$((ERRORS + 1))
  fi
done
if [ "$ENVELOPE_ERRORS" -eq 0 ]; then
  echo "PASS: Envelope fields present"
fi

# 2b. Verdict value validation
VERDICT=$(jq -r '.verdict // empty' "$FINDINGS" 2>/dev/null)
if [ -n "$VERDICT" ]; then
  case "$VERDICT" in
    PASS|WARN|FAIL) echo "PASS: Verdict value valid ($VERDICT)" ;;
    *) echo "FAIL: Invalid verdict value: $VERDICT (must be PASS/WARN/FAIL)"; ERRORS=$((ERRORS + 1)) ;;
  esac
fi

# 2c. Strengths is an array (if present)
if [ "$(jq 'has("strengths")' "$FINDINGS")" = "true" ]; then
  if [ "$(jq '.strengths | type' "$FINDINGS")" != '"array"' ]; then
    echo "FAIL: strengths must be an array"
    ERRORS=$((ERRORS + 1))
  else
    echo "PASS: Strengths is an array ($(jq '.strengths | length' "$FINDINGS") items)"
  fi
fi

# 2d. Spec gaps is an array (if present)
if [ "$(jq 'has("spec_gaps")' "$FINDINGS")" = "true" ]; then
  if [ "$(jq '.spec_gaps | type' "$FINDINGS")" != '"array"' ]; then
    echo "FAIL: spec_gaps must be an array"
    ERRORS=$((ERRORS + 1))
  else
    echo "PASS: spec_gaps is an array ($(jq '.spec_gaps | length' "$FINDINGS") items)"
  fi
fi

# 3. Required finding fields
FINDING_COUNT=$(jq '.findings | length' "$FINDINGS")
echo "INFO: $FINDING_COUNT findings"

REQUIRED_FIELDS='["id","source","pass","severity","confidence","file","line","summary"]'
# Count findings that are missing at least one required field
BAD_FINDING_COUNT=$(jq --argjson req "$REQUIRED_FIELDS" \
  '[.findings[] | . as $f | select([$req[] | select($f[.] == null)] | length > 0)] | length' \
  "$FINDINGS")
if [ "$BAD_FINDING_COUNT" -gt 0 ]; then
  # Show which fields are missing for debugging
  MISSING_DETAIL=$(jq -r --argjson req "$REQUIRED_FIELDS" \
    '.findings[] | . as $f | [$req[] | select($f[.] == null)] | select(length > 0) | "  finding \($f.summary // $f.file // "unknown"): missing \(join(", "))"' \
    "$FINDINGS" 2>/dev/null | head -5)
  echo "FAIL: $BAD_FINDING_COUNT findings missing required fields"
  echo "$MISSING_DETAIL"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: All findings have required fields"
fi

# 3b. Optional sources field type validation
BAD_SOURCES=$(jq '[.findings[] | select(has("sources") and (.sources | type != "array"))] | length' "$FINDINGS")
if [ "$BAD_SOURCES" -gt 0 ]; then
  echo "FAIL: $BAD_SOURCES findings have non-array sources field"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Optional sources field type valid"
fi

# 4. Confidence gating — no AI findings below 0.65
LOW_CONF=$(jq '[.findings[] | select(.source == "ai" and .confidence < 0.65)] | length' "$FINDINGS")
if [ "$LOW_CONF" -gt 0 ]; then
  echo "FAIL: $LOW_CONF AI findings below 0.65 confidence (should have been filtered)"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Confidence gating OK"
fi

# 5. Evidence gating — high/critical must have failure_mode
MISSING_EVIDENCE=$(jq '[.findings[] | select((.severity == "high" or .severity == "critical") and (.failure_mode == null or .failure_mode == ""))] | length' "$FINDINGS")
if [ "$MISSING_EVIDENCE" -gt 0 ]; then
  echo "FAIL: $MISSING_EVIDENCE high/critical findings missing failure_mode"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Evidence gating OK"
fi

# 6. Valid severity values
BAD_SEV=$(jq '[.findings[] | select(.severity | IN("low","medium","high","critical") | not)] | length' "$FINDINGS")
if [ "$BAD_SEV" -gt 0 ]; then
  echo "FAIL: $BAD_SEV findings with invalid severity"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Severity values valid"
fi

# 7. Valid source values
BAD_SRC=$(jq '[.findings[] | select(.source | IN("deterministic","ai") | not)] | length' "$FINDINGS")
if [ "$BAD_SRC" -gt 0 ]; then
  echo "FAIL: $BAD_SRC findings with invalid source"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Source values valid"
fi

# 7b. Valid pass values
BAD_PASS=$(jq '[.findings[] | select(.pass | IN("correctness","security","reliability","performance","testing","maintainability","spec_verification") | not)] | length' "$FINDINGS")
if [ "$BAD_PASS" -gt 0 ]; then
  echo "FAIL: $BAD_PASS findings with invalid pass value"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Pass values valid"
fi

# 8. Tool status present and non-empty
TOOL_COUNT=$(jq '.tool_status | keys | length' "$FINDINGS")
if [ "$TOOL_COUNT" -eq 0 ]; then
  echo "FAIL: tool_status is empty"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: tool_status has $TOOL_COUNT entries"
fi

# 8b. Tool status values valid
BAD_TOOL_STATUS=$(jq '[.tool_status[]? | select((.status // "") | IN("ran","skipped","failed","not_installed","sandbox_blocked") | not)] | length' "$FINDINGS")
if [ "$BAD_TOOL_STATUS" -gt 0 ]; then
  echo "FAIL: $BAD_TOOL_STATUS tool_status entries with invalid status value"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Tool status values valid"
fi

# 9. Action tier classification — every finding should have an action_tier
MISSING_TIER=$(jq '[.findings[] | select(.action_tier == null)] | length' "$FINDINGS")
if [ "$MISSING_TIER" -gt 0 ]; then
  echo "FAIL: $MISSING_TIER findings missing action_tier classification"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: All findings have action_tier"
fi

# 9b. Valid action_tier values
BAD_TIER=$(jq '[.findings[] | select(.action_tier != null and (.action_tier | IN("must_fix","should_fix","consider") | not))] | length' "$FINDINGS")
if [ "$BAD_TIER" -gt 0 ]; then
  echo "FAIL: $BAD_TIER findings with invalid action_tier value"
  ERRORS=$((ERRORS + 1))
else
  echo "PASS: Action tier values valid"
fi

# 10. Tier summary consistency (if present)
if [ "$(jq 'has("tier_summary")' "$FINDINGS")" = "true" ]; then
  MUST=$(jq '.tier_summary.must_fix // 0' "$FINDINGS")
  SHOULD=$(jq '.tier_summary.should_fix // 0' "$FINDINGS")
  CONSIDER=$(jq '.tier_summary.consider // 0' "$FINDINGS")
  TOTAL=$((MUST + SHOULD + CONSIDER))
  if [ "$TOTAL" -ne "$FINDING_COUNT" ]; then
    echo "WARN: tier_summary counts ($TOTAL) don't match findings count ($FINDING_COUNT)"
  else
    echo "PASS: tier_summary consistent (must_fix=$MUST, should_fix=$SHOULD, consider=$CONSIDER)"
  fi
fi

# 10b. review_mode field validation
if [ "$(jq 'has("review_mode")' "$FINDINGS")" = "true" ]; then
  REVIEW_MODE=$(jq -r '.review_mode' "$FINDINGS")
  case "$REVIEW_MODE" in
    standard|chunked) echo "PASS: review_mode valid ($REVIEW_MODE)" ;;
    *) echo "FAIL: Invalid review_mode: $REVIEW_MODE (must be standard/chunked)"; ERRORS=$((ERRORS + 1)) ;;
  esac

  # 10c. Chunked mode: validate chunk metadata
  if [ "$REVIEW_MODE" = "chunked" ]; then
    if [ "$(jq 'has("chunk_count")' "$FINDINGS")" != "true" ]; then
      echo "FAIL: chunked mode requires chunk_count field"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: chunk_count present ($(jq '.chunk_count' "$FINDINGS"))"
    fi

    if [ "$(jq 'has("chunks")' "$FINDINGS")" != "true" ]; then
      echo "FAIL: chunked mode requires chunks array"
      ERRORS=$((ERRORS + 1))
    elif [ "$(jq '.chunks | type' "$FINDINGS")" != '"array"' ]; then
      echo "FAIL: chunks must be an array"
      ERRORS=$((ERRORS + 1))
    else
      CHUNK_COUNT=$(jq '.chunks | length' "$FINDINGS")
      echo "INFO: $CHUNK_COUNT chunks"

      # Validate required fields in each chunk
      BAD_CHUNKS=$(jq '[.chunks[] | select(.id == null or .description == null or .files == null or .file_count == null or .diff_lines == null or .risk_tier == null)] | length' "$FINDINGS")
      if [ "$BAD_CHUNKS" -gt 0 ]; then
        echo "FAIL: $BAD_CHUNKS chunks missing required fields (id, description, files, file_count, diff_lines, risk_tier)"
        ERRORS=$((ERRORS + 1))
      else
        echo "PASS: All chunks have required fields"
      fi

      # Validate risk_tier values
      BAD_RISK=$(jq '[.chunks[] | select(.risk_tier | IN("critical","standard","low-risk") | not)] | length' "$FINDINGS")
      if [ "$BAD_RISK" -gt 0 ]; then
        echo "FAIL: $BAD_RISK chunks with invalid risk_tier"
        ERRORS=$((ERRORS + 1))
      else
        echo "PASS: Chunk risk_tier values valid"
      fi
    fi
  fi
else
  echo "WARN: Missing review_mode field (should be 'standard' or 'chunked')"
fi

# 12. spec_requirements validation (if present)
if [ "$(jq 'has("spec_requirements")' "$FINDINGS")" = "true" ]; then
  if [ "$(jq '.spec_requirements | type' "$FINDINGS")" != '"array"' ]; then
    echo "FAIL: spec_requirements must be an array"
    ERRORS=$((ERRORS + 1))
  else
    SPEC_REQ_COUNT=$(jq '.spec_requirements | length' "$FINDINGS")
    echo "INFO: $SPEC_REQ_COUNT spec requirements"

    # 12a. Required fields in each spec requirement
    BAD_SPEC_REQ=$(jq '[.spec_requirements[] | select(.id == null or .text == null or .source_section == null or .impl_status == null)] | length' "$FINDINGS")
    if [ "$BAD_SPEC_REQ" -gt 0 ]; then
      echo "FAIL: $BAD_SPEC_REQ spec requirements missing required fields (id, text, source_section, impl_status)"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: All spec requirements have required fields"
    fi

    # 12b. Valid impl_status values
    BAD_IMPL=$(jq '[.spec_requirements[] | select(.impl_status | IN("implemented","partial","not_implemented","cannot_determine") | not)] | length' "$FINDINGS")
    if [ "$BAD_IMPL" -gt 0 ]; then
      echo "FAIL: $BAD_IMPL spec requirements with invalid impl_status"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: impl_status values valid"
    fi

    # 12c. Valid priority values (if present)
    BAD_PRIORITY=$(jq '[.spec_requirements[] | select(has("priority") and (.priority | IN("must","should","could","informational") | not))] | length' "$FINDINGS")
    if [ "$BAD_PRIORITY" -gt 0 ]; then
      echo "FAIL: $BAD_PRIORITY spec requirements with invalid priority"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: Priority values valid"
    fi

    # 12d. Valid test_coverage.status (if present)
    BAD_COV=$(jq '[.spec_requirements[] | select(has("test_coverage") and (.test_coverage.status | IN("covered","partial","missing","not_applicable") | not))] | length' "$FINDINGS")
    if [ "$BAD_COV" -gt 0 ]; then
      echo "FAIL: $BAD_COV spec requirements with invalid test_coverage.status"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: test_coverage.status values valid"
    fi

    # 12e. Valid test category values (if present)
    BAD_CAT=$(jq '[.spec_requirements[] | select(has("test_coverage")) | .test_coverage.tests[]? | select(.category | IN("unit","integration","e2e","unknown") | not)] | length' "$FINDINGS")
    if [ "$BAD_CAT" -gt 0 ]; then
      echo "FAIL: $BAD_CAT tests with invalid category"
      ERRORS=$((ERRORS + 1))
    else
      echo "PASS: Test category values valid"
    fi
  fi
fi

# 11. Validate report markdown if provided
if [ -n "$REPORT" ]; then
  echo "---"
  echo "Validating report: $REPORT"
  if [ ! -f "$REPORT" ]; then
    echo "FAIL: Report file not found"
    ERRORS=$((ERRORS + 1))
  else
    # Required sections
    for section in "Code Review:" "Verdict:" "Strengths" "Summary"; do
      if ! grep -q "$section" "$REPORT" 2>/dev/null; then
        echo "WARN: Report missing expected section containing '$section'"
      fi
    done
    # Tier sections (at least one should be present unless zero findings)
    TIER_FOUND=0
    for tier in "Must Fix" "Should Fix" "Consider"; do
      if grep -q "$tier" "$REPORT" 2>/dev/null; then
        TIER_FOUND=$((TIER_FOUND + 1))
      fi
    done
    if [ "$TIER_FOUND" -eq 0 ] && [ "$FINDING_COUNT" -gt 0 ]; then
      echo "WARN: Report has findings but no tier sections (Must Fix / Should Fix / Consider)"
    fi
    echo "PASS: Report file exists and is non-empty"
  fi
fi

echo "---"
if [ "$ERRORS" -gt 0 ]; then
  echo "RESULT: FAIL ($ERRORS errors)"
  exit 1
else
  echo "RESULT: PASS (all checks passed)"
  exit 0
fi
