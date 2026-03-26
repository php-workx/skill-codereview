#!/usr/bin/env bash
# test-scripts.sh — Unit tests for codereview pipeline scripts
#
# Runs with fixture data only — no external tools required.
# Exit 0 = all tests pass, exit 1 = failures.
#
# Usage: bash tests/test-scripts.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES="$SCRIPT_DIR/fixtures"
SCRIPTS="$REPO_ROOT/skills/codereview/scripts"

PASS=0
FAIL=0
TOTAL=0

pass() {
  PASS=$((PASS + 1))
  TOTAL=$((TOTAL + 1))
  echo "  PASS: $1"
}

fail() {
  FAIL=$((FAIL + 1))
  TOTAL=$((TOTAL + 1))
  echo "  FAIL: $1"
  if [ -n "${2:-}" ]; then
    echo "        $2"
  fi
}

assert_json_valid() {
  local label="$1"
  local input="$2"
  if echo "$input" | python3 -m json.tool > /dev/null 2>&1; then
    pass "$label"
  else
    fail "$label" "Invalid JSON output"
  fi
}

assert_json_field() {
  local label="$1"
  local json="$2"
  local field="$3"
  if echo "$json" | python3 -c "import json,sys; d=json.load(sys.stdin); assert $field" 2>/dev/null; then
    pass "$label"
  else
    fail "$label" "Assertion failed: $field"
  fi
}

# ============================================================
echo ""
echo "=== 1. Script Syntax Validation ==="
echo ""

# 1a. Bash scripts parse cleanly
for script in run-scans.sh complexity.sh validate_output.sh git-risk.sh; do
  if bash -n "$SCRIPTS/$script" 2>/dev/null; then
    pass "$script syntax valid"
  else
    fail "$script syntax valid" "bash -n failed"
  fi
done

# 1b. Python scripts parse cleanly
for script in enrich-findings.py discover-project.py coverage-collect.py; do
  if python3 -c "import ast; ast.parse(open('$SCRIPTS/$script').read())" 2>/dev/null; then
    pass "$script syntax valid"
  else
    fail "$script syntax valid" "ast.parse failed"
  fi
done

# ============================================================
echo ""
echo "=== 2. enrich-findings.py ==="
echo ""

# 2a. Basic enrichment with judge + scan findings
ENRICH_OUT=$(python3 "$SCRIPTS/enrich-findings.py" \
  --judge-findings "$FIXTURES/judge-output.json" \
  --scan-findings "$FIXTURES/scan-findings.json" \
  2>/dev/null)

assert_json_valid "enrichment produces valid JSON" "$ENRICH_OUT"
assert_json_field "has findings array" "$ENRICH_OUT" "'findings' in d"
assert_json_field "has tier_summary" "$ENRICH_OUT" "'tier_summary' in d"

# 2b. Confidence floor filters low-confidence findings
assert_json_field "low-confidence finding filtered (0.50 < 0.65)" "$ENRICH_OUT" \
  "not any(f.get('confidence',1.0) < 0.65 for f in d['findings'])"

# 2c. All findings have required enrichment fields
assert_json_field "all findings have 'source' field" "$ENRICH_OUT" \
  "all('source' in f for f in d['findings'])"
assert_json_field "all findings have 'id' field" "$ENRICH_OUT" \
  "all('id' in f for f in d['findings'])"
assert_json_field "all findings have 'action_tier' field" "$ENRICH_OUT" \
  "all('action_tier' in f for f in d['findings'])"

# 2d. Source assignment correct
assert_json_field "scan findings have source=deterministic" "$ENRICH_OUT" \
  "all(f['source']=='deterministic' for f in d['findings'] if f.get('tool'))"
assert_json_field "judge findings have source=ai" "$ENRICH_OUT" \
  "all(f['source']=='ai' for f in d['findings'] if not f.get('tool'))"

# 2e. Tier assignment rules
assert_json_field "high/0.92 → must_fix" "$ENRICH_OUT" \
  "any(f['action_tier']=='must_fix' and f['severity']=='high' and f.get('confidence',0)>=0.80 for f in d['findings'])"
assert_json_field "medium findings → should_fix" "$ENRICH_OUT" \
  "any(f['action_tier']=='should_fix' and f['severity']=='medium' for f in d['findings'])"

# 2f. Evidence check: critical without failure_mode → downgrade to medium
assert_json_field "critical without failure_mode downgraded" "$ENRICH_OUT" \
  "not any(f['severity']=='critical' and not f.get('failure_mode') for f in d['findings'] if f['source']=='ai')"

# 2g. Tier summary counts are consistent
assert_json_field "tier_summary counts match findings" "$ENRICH_OUT" \
  "d['tier_summary']['must_fix'] + d['tier_summary']['should_fix'] + d['tier_summary']['consider'] == len(d['findings'])"

# 2h. Deterministic findings get confidence 1.0
assert_json_field "deterministic findings have confidence 1.0" "$ENRICH_OUT" \
  "all(f.get('confidence')==1.0 for f in d['findings'] if f['source']=='deterministic')"

# 2i. Empty input produces valid output
ENRICH_EMPTY=$(echo '{"findings":[]}' > /tmp/test-empty.json && \
  python3 "$SCRIPTS/enrich-findings.py" --judge-findings /tmp/test-empty.json 2>/dev/null)
assert_json_valid "empty input produces valid JSON" "$ENRICH_EMPTY"
assert_json_field "empty input has zero findings" "$ENRICH_EMPTY" \
  "len(d['findings']) == 0"
assert_json_field "empty input has zero tier_summary" "$ENRICH_EMPTY" \
  "d['tier_summary'] == {'must_fix': 0, 'should_fix': 0, 'consider': 0}"

# ============================================================
echo ""
echo "=== 3. complexity.sh ==="
echo ""

# 3a. No matching files → skipped status
COMPLEXITY_OUT=$(echo "README.md" | bash "$SCRIPTS/complexity.sh" 2>/dev/null)
assert_json_valid "complexity with non-code file produces valid JSON" "$COMPLEXITY_OUT"
assert_json_field "radon skipped for non-.py file" "$COMPLEXITY_OUT" \
  "d['tool_status']['radon']['status'] == 'skipped'"
assert_json_field "gocyclo skipped for non-.go file" "$COMPLEXITY_OUT" \
  "d['tool_status']['gocyclo']['status'] == 'skipped'"
assert_json_field "no hotspots for non-code file" "$COMPLEXITY_OUT" \
  "len(d['hotspots']) == 0"

# 3b. Python file detected
COMPLEXITY_PY=$(echo "src/auth/login.py" | bash "$SCRIPTS/complexity.sh" 2>/dev/null)
assert_json_valid "complexity with .py file produces valid JSON" "$COMPLEXITY_PY"
assert_json_field "radon not skipped for .py file" "$COMPLEXITY_PY" \
  "d['tool_status']['radon']['status'] != 'skipped'"

# 3c. Go file detected
COMPLEXITY_GO=$(echo "src/api/handler.go" | bash "$SCRIPTS/complexity.sh" 2>/dev/null)
assert_json_valid "complexity with .go file produces valid JSON" "$COMPLEXITY_GO"
assert_json_field "gocyclo not skipped for .go file" "$COMPLEXITY_GO" \
  "d['tool_status']['gocyclo']['status'] != 'skipped'"

# 3d. Empty input
COMPLEXITY_EMPTY=$(echo "" | bash "$SCRIPTS/complexity.sh" 2>/dev/null)
assert_json_valid "complexity with empty input produces valid JSON" "$COMPLEXITY_EMPTY"

# 3e. Output structure
assert_json_field "has hotspots array" "$COMPLEXITY_OUT" "'hotspots' in d"
assert_json_field "has tool_status object" "$COMPLEXITY_OUT" "'tool_status' in d"
assert_json_field "tool_status has radon" "$COMPLEXITY_OUT" "'radon' in d['tool_status']"
assert_json_field "tool_status has gocyclo" "$COMPLEXITY_OUT" "'gocyclo' in d['tool_status']"
assert_json_field "radon status has required fields" "$COMPLEXITY_OUT" \
  "all(k in d['tool_status']['radon'] for k in ['status','version','finding_count','note'])"

# ============================================================
echo ""
echo "=== 4. discover-project.py ==="
echo ""

# 4a. Single-project repo (this repo itself)
DISCOVER_SINGLE=$(echo "skills/codereview/SKILL.md" | python3 "$SCRIPTS/discover-project.py" 2>/dev/null)
assert_json_valid "discover single-project produces valid JSON" "$DISCOVER_SINGLE"
assert_json_field "has contexts array" "$DISCOVER_SINGLE" "'contexts' in d"
assert_json_field "has monorepo flag" "$DISCOVER_SINGLE" "'monorepo' in d"
assert_json_field "has orchestrator field" "$DISCOVER_SINGLE" "'orchestrator' in d"
assert_json_field "single project has 1 context" "$DISCOVER_SINGLE" \
  "len(d['contexts']) == 1"

# 4b. Context has required fields
assert_json_field "context has root" "$DISCOVER_SINGLE" "'root' in d['contexts'][0]"
assert_json_field "context has language" "$DISCOVER_SINGLE" "'language' in d['contexts'][0]"
assert_json_field "context has build_files" "$DISCOVER_SINGLE" "'build_files' in d['contexts'][0]"
assert_json_field "context has tool_configs" "$DISCOVER_SINGLE" "'tool_configs' in d['contexts'][0]"
assert_json_field "context has ci_files" "$DISCOVER_SINGLE" "'ci_files' in d['contexts'][0]"
assert_json_field "context has changed_files" "$DISCOVER_SINGLE" "'changed_files' in d['contexts'][0]"

# 4c. Monorepo detection with fixtures
DISCOVER_MONO=$(printf "packages/api/handler.go\npackages/web/App.tsx" | \
  (cd "$FIXTURES/project-monorepo" && python3 "$SCRIPTS/discover-project.py") 2>/dev/null)
assert_json_valid "discover monorepo produces valid JSON" "$DISCOVER_MONO"
assert_json_field "detects monorepo=true" "$DISCOVER_MONO" "d['monorepo'] == True"
assert_json_field "detects turborepo orchestrator" "$DISCOVER_MONO" \
  "d['orchestrator'] is not None and d['orchestrator'].get('type') == 'turborepo'"
assert_json_field "has 2 contexts (api + web)" "$DISCOVER_MONO" \
  "len(d['contexts']) == 2"

# 4d. Language detection from project markers
assert_json_field "api context detected as go" "$DISCOVER_MONO" \
  "any(c['language']=='go' for c in d['contexts'])"
assert_json_field "web context detected as typescript" "$DISCOVER_MONO" \
  "any(c['language']=='typescript' for c in d['contexts'])"

# 4e. Build file extraction
assert_json_field "api has makefile with targets" "$DISCOVER_MONO" \
  "any(bf['type']=='makefile' and len(bf.get('targets',[])) > 0 for c in d['contexts'] for bf in c['build_files'] if c['language']=='go')"
assert_json_field "web has package.json with scripts" "$DISCOVER_MONO" \
  "any(bf['type']=='package_json' and len(bf.get('scripts',[])) > 0 for c in d['contexts'] for bf in c['build_files'] if c['language']=='typescript')"

# 4f. Empty input
DISCOVER_EMPTY=$(echo "" | python3 "$SCRIPTS/discover-project.py" 2>/dev/null)
assert_json_valid "discover with empty input produces valid JSON" "$DISCOVER_EMPTY"
assert_json_field "empty input has 0 contexts" "$DISCOVER_EMPTY" \
  "len(d['contexts']) == 0"

# ============================================================
echo ""
echo "=== 5. run-scans.sh ==="
echo ""

# 5a. Empty input produces valid output
SCANS_EMPTY=$(echo "" | bash "$SCRIPTS/run-scans.sh" --base-ref HEAD~1 2>/dev/null)
assert_json_valid "run-scans empty input produces valid JSON" "$SCANS_EMPTY"
assert_json_field "has findings array" "$SCANS_EMPTY" "'findings' in d"
assert_json_field "has tool_status object" "$SCANS_EMPTY" "'tool_status' in d"
assert_json_field "empty input has zero findings" "$SCANS_EMPTY" \
  "len(d['findings']) == 0"

# 5b. Tool status has expected structure
assert_json_field "tool_status entries have status field" "$SCANS_EMPTY" \
  "all('status' in v for v in d['tool_status'].values())"

# 5c. With real files (tools may not be installed — that's fine)
SCANS_REAL=$(echo "skills/codereview/scripts/run-scans.sh" | \
  bash "$SCRIPTS/run-scans.sh" --base-ref HEAD~1 2>/dev/null)
assert_json_valid "run-scans with real file produces valid JSON" "$SCANS_REAL"
assert_json_field "tool_status has entries" "$SCANS_REAL" "len(d['tool_status']) > 0"

# 5d. Each tool status value is from allowed enum
assert_json_field "tool_status values are valid enum" "$SCANS_REAL" \
  "all(v['status'] in ('ran','skipped','failed','timeout','not_installed','sandbox_blocked') for v in d['tool_status'].values())"

# 5e. Findings (if any) have required fields
assert_json_field "all findings have file field" "$SCANS_REAL" \
  "all('file' in f for f in d['findings'])"
assert_json_field "all findings have summary field" "$SCANS_REAL" \
  "all('summary' in f for f in d['findings'])"
assert_json_field "all findings have source=deterministic" "$SCANS_REAL" \
  "all(f.get('source')=='deterministic' for f in d['findings'])"

# ============================================================
echo ""
echo "=== 6. git-risk.sh ==="
echo ""

# 6a. Empty input produces valid output
GITRISK_EMPTY=$(echo "" | bash "$SCRIPTS/git-risk.sh" 2>/dev/null)
assert_json_valid "git-risk empty input produces valid JSON" "$GITRISK_EMPTY"
assert_json_field "has files array" "$GITRISK_EMPTY" "'files' in d"
assert_json_field "has summary object" "$GITRISK_EMPTY" "'summary' in d"
assert_json_field "has shallow_clone field" "$GITRISK_EMPTY" "'shallow_clone' in d"
assert_json_field "has lookback_months field" "$GITRISK_EMPTY" "'lookback_months' in d"
assert_json_field "empty input has zero files" "$GITRISK_EMPTY" "len(d['files']) == 0"

# 6b. Default lookback is 6 months
assert_json_field "default lookback is 6" "$GITRISK_EMPTY" "d['lookback_months'] == 6"

# 6c. Custom --months flag
GITRISK_MONTHS=$(echo "" | bash "$SCRIPTS/git-risk.sh" --months 12 2>/dev/null)
assert_json_valid "git-risk --months 12 produces valid JSON" "$GITRISK_MONTHS"
assert_json_field "lookback_months is 12" "$GITRISK_MONTHS" "d['lookback_months'] == 12"

# 6d. With real file produces valid output
GITRISK_REAL=$(echo "skills/codereview/SKILL.md" | bash "$SCRIPTS/git-risk.sh" 2>/dev/null)
assert_json_valid "git-risk with real file produces valid JSON" "$GITRISK_REAL"
assert_json_field "has 1 file entry" "$GITRISK_REAL" "len(d['files']) == 1"
assert_json_field "file entry has all fields" "$GITRISK_REAL" \
  "all(k in d['files'][0] for k in ['file','churn','bug_commits','last_bug','risk'])"
assert_json_field "risk is valid tier" "$GITRISK_REAL" \
  "d['files'][0]['risk'] in ('high','medium','low')"

# 6e. Summary counts are consistent
assert_json_field "summary counts match file count" "$GITRISK_REAL" \
  "d['summary']['high'] + d['summary']['medium'] + d['summary']['low'] == len(d['files'])"

# 6f. Churn and bug_commits are non-negative integers
assert_json_field "churn is non-negative" "$GITRISK_REAL" \
  "all(f['churn'] >= 0 for f in d['files'])"
assert_json_field "bug_commits is non-negative" "$GITRISK_REAL" \
  "all(f['bug_commits'] >= 0 for f in d['files'])"

# 6g. Multiple files
GITRISK_MULTI=$(printf "skills/codereview/SKILL.md\nskills/codereview/scripts/complexity.sh" | bash "$SCRIPTS/git-risk.sh" 2>/dev/null)
assert_json_valid "git-risk with multiple files produces valid JSON" "$GITRISK_MULTI"
assert_json_field "has 2 file entries" "$GITRISK_MULTI" "len(d['files']) == 2"
assert_json_field "multi-file summary counts match" "$GITRISK_MULTI" \
  "d['summary']['high'] + d['summary']['medium'] + d['summary']['low'] == len(d['files'])"

# ============================================================
echo ""
echo "=== 7. validate_output.sh ==="
echo ""

# 7a. Valid review passes validation
VALIDATE_VALID=$(bash "$SCRIPTS/validate_output.sh" --findings "$FIXTURES/valid-review.json" 2>&1)
VALIDATE_VALID_RC=$?
if echo "$VALIDATE_VALID" | grep -q "ERRORS: 0\|0 errors"; then
  pass "valid review passes validation"
elif [ $VALIDATE_VALID_RC -eq 0 ]; then
  pass "valid review passes validation (exit 0)"
else
  fail "valid review passes validation" "Exit code: $VALIDATE_VALID_RC"
fi

# 7b. Invalid review catches errors
VALIDATE_INVALID=$(bash "$SCRIPTS/validate_output.sh" --findings "$FIXTURES/invalid-review.json" 2>&1) || VALIDATE_INVALID_RC=$?
VALIDATE_INVALID_RC=${VALIDATE_INVALID_RC:-0}
if echo "$VALIDATE_INVALID" | grep -q "FAIL"; then
  pass "invalid review has FAIL entries"
else
  fail "invalid review has FAIL entries" "No FAIL found in output"
fi

# ============================================================
echo ""
echo "=== 8. coverage-collect.py ==="
echo ""

# 8a. Empty input produces valid output
COVERAGE_EMPTY=$(echo "" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect empty input produces valid JSON" "$COVERAGE_EMPTY"
assert_json_field "empty input has empty languages_detected" "$COVERAGE_EMPTY" \
  "d['languages_detected'] == []"
assert_json_field "empty input has empty coverage_data" "$COVERAGE_EMPTY" \
  "d['coverage_data'] == []"
assert_json_field "empty input has empty tool_status" "$COVERAGE_EMPTY" \
  "d['tool_status'] == {}"
assert_json_field "empty input has empty warnings" "$COVERAGE_EMPTY" \
  "d['warnings'] == []"

# 8b. Output structure with non-code files
COVERAGE_NOCODE=$(echo "README.md" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect non-code file produces valid JSON" "$COVERAGE_NOCODE"
assert_json_field "non-code file has empty languages_detected" "$COVERAGE_NOCODE" \
  "d['languages_detected'] == []"

# 8c. Python file detected
COVERAGE_PY=$(echo "src/auth/login.py" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect with .py file produces valid JSON" "$COVERAGE_PY"
assert_json_field "python detected in languages_detected" "$COVERAGE_PY" \
  "'python' in d['languages_detected']"
assert_json_field "has coverage_data array" "$COVERAGE_PY" "'coverage_data' in d"
assert_json_field "has tool_status object" "$COVERAGE_PY" "'tool_status' in d"
assert_json_field "has warnings array" "$COVERAGE_PY" "'warnings' in d"

# 8d. Go file detected
COVERAGE_GO=$(echo "src/api/handler.go" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect with .go file produces valid JSON" "$COVERAGE_GO"
assert_json_field "go detected in languages_detected" "$COVERAGE_GO" \
  "'go' in d['languages_detected']"

# 8e. TypeScript file detected
COVERAGE_TS=$(echo "src/app/index.ts" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect with .ts file produces valid JSON" "$COVERAGE_TS"
assert_json_field "typescript detected in languages_detected" "$COVERAGE_TS" \
  "'typescript' in d['languages_detected']"

# 8f. Rust file detected
COVERAGE_RS=$(echo "src/main.rs" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect with .rs file produces valid JSON" "$COVERAGE_RS"
assert_json_field "rust detected in languages_detected" "$COVERAGE_RS" \
  "'rust' in d['languages_detected']"

# 8g. Multi-language detection
COVERAGE_MULTI=$(printf "src/auth/login.py\nsrc/api/handler.go\nsrc/app/index.ts" | \
  python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect multi-language produces valid JSON" "$COVERAGE_MULTI"
assert_json_field "detects all 3 languages" "$COVERAGE_MULTI" \
  "len(d['languages_detected']) == 3"
assert_json_field "multi-language has go" "$COVERAGE_MULTI" \
  "'go' in d['languages_detected']"
assert_json_field "multi-language has python" "$COVERAGE_MULTI" \
  "'python' in d['languages_detected']"
assert_json_field "multi-language has typescript" "$COVERAGE_MULTI" \
  "'typescript' in d['languages_detected']"

# 8h. Test files excluded from coverage data (only test files → no coverage entries)
COVERAGE_TESTONLY=$(printf "test_auth.py\nhandler_test.go\napp.test.ts" | \
  python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage-collect test-only files produces valid JSON" "$COVERAGE_TESTONLY"
assert_json_field "test-only files has empty coverage_data" "$COVERAGE_TESTONLY" \
  "d['coverage_data'] == []"

# 8i. tool_status keys use correct naming convention
COVERAGE_KEYS=$(printf "src/auth.py\nsrc/handler.go" | python3 "$SCRIPTS/coverage-collect.py" 2>/dev/null)
assert_json_valid "coverage tool_status key check produces valid JSON" "$COVERAGE_KEYS"
assert_json_field "tool_status keys follow coverage_<lang> pattern" "$COVERAGE_KEYS" \
  "all(k.startswith('coverage_') for k in d['tool_status'].keys())"

# ============================================================
echo ""
echo "=== 9. Integration: enrich-findings.py → validate_output.sh pipeline ==="
echo ""

# Build a complete review envelope from enriched findings
ENRICHED=$(python3 "$SCRIPTS/enrich-findings.py" \
  --judge-findings "$FIXTURES/judge-output.json" \
  --scan-findings "$FIXTURES/scan-findings.json" \
  2>/dev/null)

# Wrap enriched output into a full review envelope
REVIEW_ENVELOPE=$(echo "$ENRICHED" | python3 -c "
import json, sys
enriched = json.load(sys.stdin)
envelope = {
  'run_id': '20260326T090000Z-test',
  'timestamp': '2026-03-26T09:00:00Z',
  'review_mode': 'standard',
  'scope': 'branch',
  'base_ref': 'main',
  'head_ref': 'feat/test',
  'pr_number': None,
  'files_reviewed': list(set(f['file'] for f in enriched['findings'])),
  'verdict': 'WARN',
  'verdict_reason': 'Has findings',
  'strengths': ['Good test coverage'],
  'spec_gaps': [],
  'spec_requirements': [],
  'tool_status': {
    'semgrep': {'status': 'ran', 'version': '1.56.0', 'finding_count': 1, 'note': None},
    'ai_correctness': {'status': 'ran', 'version': None, 'finding_count': 1, 'note': None}
  },
  'findings': enriched['findings'],
  'tier_summary': enriched['tier_summary']
}
json.dump(envelope, sys.stdout, indent=2)
" 2>/dev/null)

# Write to temp file and validate
echo "$REVIEW_ENVELOPE" > /tmp/test-pipeline-review.json
PIPELINE_VALID=$(bash "$SCRIPTS/validate_output.sh" --findings /tmp/test-pipeline-review.json 2>&1) || PIPELINE_RC=$?
PIPELINE_RC=${PIPELINE_RC:-0}

if echo "$PIPELINE_VALID" | grep -q "ERRORS: 0\|0 errors"; then
  pass "enriched findings → full envelope validates"
elif [ $PIPELINE_RC -eq 0 ]; then
  pass "enriched findings → full envelope validates (exit 0)"
else
  # Count actual failures (not warnings)
  FAIL_COUNT=$(echo "$PIPELINE_VALID" | grep -c "^FAIL:" || true)
  if [ "$FAIL_COUNT" -eq 0 ]; then
    pass "enriched findings → full envelope validates (no FAIL lines)"
  else
    fail "enriched findings → full envelope validates" "$FAIL_COUNT FAIL entries found"
    echo "$PIPELINE_VALID" | grep "^FAIL:" | head -5
  fi
fi

# ============================================================
echo ""
echo "=========================================="
echo "  Results: $PASS passed, $FAIL failed (of $TOTAL tests)"
echo "=========================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
