#!/usr/bin/env bash
# ci-local-release.sh — Local CI parity gate for the codereview skill
#
# Validates structural integrity, schema-prompt consistency, shell script
# quality, and absence of secrets before a release. Produces a manifest
# (SBOM-equivalent) and a security scan report as release artifacts.
#
# Usage:
#   ./scripts/ci-local-release.sh [--release-version <version>]
#
# Exit codes:
#   0  All checks passed
#   1  One or more blocking checks failed

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SKILL_DIR="$REPO_ROOT/skill"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="$REPO_ROOT/.agents/releases/local-ci/$TIMESTAMP"
RELEASE_VERSION="${2:-unreleased}"
ERRORS=0
WARNINGS=0

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-version) RELEASE_VERSION="$2"; shift 2 ;;
    *) shift ;;
  esac
done

mkdir -p "$ARTIFACT_DIR"

pass()  { echo "  [PASS] $1"; }
warn()  { echo "  [WARN] $1"; WARNINGS=$((WARNINGS + 1)); }
fail()  { echo "  [FAIL] $1"; ERRORS=$((ERRORS + 1)); }

echo "Local CI Gate — codereview skill"
echo "================================"
echo "Version:    $RELEASE_VERSION"
echo "Timestamp:  $TIMESTAMP"
echo "Artifacts:  $ARTIFACT_DIR"
echo ""

# ---------------------------------------------------------------------------
# 1. SKILL.md exists and has valid frontmatter
# ---------------------------------------------------------------------------
echo "--- Structural Integrity ---"

if [ ! -f "$SKILL_DIR/SKILL.md" ]; then
  fail "SKILL.md not found at $SKILL_DIR/SKILL.md"
else
  # Check for YAML frontmatter (--- ... ---)
  if head -1 "$SKILL_DIR/SKILL.md" | grep -q '^---$'; then
    # Check required frontmatter fields
    FRONTMATTER=$(sed -n '2,/^---$/p' "$SKILL_DIR/SKILL.md" | sed '$d')
    if echo "$FRONTMATTER" | grep -q '^name:'; then
      pass "SKILL.md has 'name' in frontmatter"
    else
      fail "SKILL.md frontmatter missing 'name' field"
    fi
    if echo "$FRONTMATTER" | grep -q '^description:'; then
      pass "SKILL.md has 'description' in frontmatter"
    else
      fail "SKILL.md frontmatter missing 'description' field"
    fi
  else
    fail "SKILL.md missing YAML frontmatter (must start with ---)"
  fi
fi

# ---------------------------------------------------------------------------
# 2. All prompt files referenced in SKILL.md exist
# ---------------------------------------------------------------------------
PROMPT_FILES=(
  "prompts/reviewer-global-contract.md"
  "prompts/reviewer-judge.md"
  "prompts/reviewer-correctness-pass.md"
  "prompts/reviewer-security-pass.md"
  "prompts/reviewer-reliability-performance-pass.md"
  "prompts/reviewer-test-adequacy-pass.md"
  "prompts/reviewer-error-handling-pass.md"
  "prompts/reviewer-api-contract-pass.md"
  "prompts/reviewer-concurrency-pass.md"
  "prompts/reviewer-spec-verification-pass.md"
)

MISSING_PROMPTS=0
for pf in "${PROMPT_FILES[@]}"; do
  if [ ! -f "$SKILL_DIR/$pf" ]; then
    fail "Missing prompt: $pf"
    MISSING_PROMPTS=$((MISSING_PROMPTS + 1))
  fi
done
if [ "$MISSING_PROMPTS" -eq 0 ]; then
  pass "All ${#PROMPT_FILES[@]} prompt files exist"
fi

# ---------------------------------------------------------------------------
# 3. All reference files exist
# ---------------------------------------------------------------------------
REFERENCE_FILES=(
  "references/acceptance-criteria.md"
  "references/design.md"
  "references/deterministic-scans.md"
  "references/report-template.md"
)

MISSING_REFS=0
for rf in "${REFERENCE_FILES[@]}"; do
  if [ ! -f "$SKILL_DIR/$rf" ]; then
    fail "Missing reference: $rf"
    MISSING_REFS=$((MISSING_REFS + 1))
  fi
done
if [ "$MISSING_REFS" -eq 0 ]; then
  pass "All ${#REFERENCE_FILES[@]} reference files exist"
fi

# ---------------------------------------------------------------------------
# 4. findings-schema.json is valid JSON
# ---------------------------------------------------------------------------
if [ ! -f "$SKILL_DIR/findings-schema.json" ]; then
  fail "findings-schema.json not found"
else
  if command -v jq &>/dev/null; then
    if jq empty "$SKILL_DIR/findings-schema.json" 2>/dev/null; then
      pass "findings-schema.json is valid JSON"
    else
      fail "findings-schema.json is invalid JSON"
    fi
  else
    warn "jq not installed — cannot validate findings-schema.json"
  fi
fi

# ---------------------------------------------------------------------------
# 5. validate_output.sh exists and is executable
# ---------------------------------------------------------------------------
if [ ! -f "$SKILL_DIR/scripts/validate_output.sh" ]; then
  fail "validate_output.sh not found"
elif [ ! -x "$SKILL_DIR/scripts/validate_output.sh" ]; then
  warn "validate_output.sh is not executable (chmod +x needed)"
else
  pass "validate_output.sh exists and is executable"
fi

# ---------------------------------------------------------------------------
# 6. Install script references files that exist
# ---------------------------------------------------------------------------
INSTALL_SCRIPT="$REPO_ROOT/scripts/install-codereview-skill.sh"
if [ -f "$INSTALL_SCRIPT" ]; then
  # The install script copies $REPO_ROOT/skill and $REPO_ROOT/prompts/codereview.md
  if [ -d "$SKILL_DIR" ] && [ -f "$REPO_ROOT/prompts/codereview.md" ]; then
    pass "Install script source files exist (skill/, prompts/codereview.md)"
  else
    [ ! -d "$SKILL_DIR" ] && fail "Install script references skill/ but it doesn't exist"
    [ ! -f "$REPO_ROOT/prompts/codereview.md" ] && fail "Install script references prompts/codereview.md but it doesn't exist"
  fi
else
  warn "Install script not found at scripts/install-codereview-skill.sh"
fi

echo ""
echo "--- Schema-Prompt Consistency ---"

# ---------------------------------------------------------------------------
# 7. Pass names in schema match explorer prompt filenames
# ---------------------------------------------------------------------------
if command -v jq &>/dev/null && [ -f "$SKILL_DIR/findings-schema.json" ]; then
  SCHEMA_PASSES=$(jq -r '
    .properties.findings.items.properties.pass.enum // empty | .[]
  ' "$SKILL_DIR/findings-schema.json" 2>/dev/null | sort)

  CONTRACT_PASSES=$(sed -n 's/^| `\([a-z_]*\)` |.*/\1/p' "$SKILL_DIR/prompts/reviewer-global-contract.md" 2>/dev/null | sort)

  if [ -n "$SCHEMA_PASSES" ] && [ -n "$CONTRACT_PASSES" ]; then
    SCHEMA_ONLY=$(comm -23 <(echo "$SCHEMA_PASSES") <(echo "$CONTRACT_PASSES"))
    CONTRACT_ONLY=$(comm -13 <(echo "$SCHEMA_PASSES") <(echo "$CONTRACT_PASSES"))

    if [ -n "$SCHEMA_ONLY" ]; then
      warn "Pass values in schema but not in global contract: $SCHEMA_ONLY"
    fi
    if [ -n "$CONTRACT_ONLY" ]; then
      warn "Pass values in global contract but not in schema: $CONTRACT_ONLY"
    fi
    if [ -z "$SCHEMA_ONLY" ] && [ -z "$CONTRACT_ONLY" ]; then
      pass "Schema pass enum matches global contract pass table"
    fi
  else
    warn "Could not extract pass values from schema or contract for comparison"
  fi
else
  warn "Skipping schema-prompt consistency (jq not installed or schema missing)"
fi

# ---------------------------------------------------------------------------
# 8. Required finding fields in schema are documented in global contract
# ---------------------------------------------------------------------------
if command -v jq &>/dev/null && [ -f "$SKILL_DIR/findings-schema.json" ]; then
  REQUIRED_FIELDS=$(jq -r '
    .properties.findings.items.required // empty | .[]
  ' "$SKILL_DIR/findings-schema.json" 2>/dev/null)

  if [ -n "$REQUIRED_FIELDS" ]; then
    MISSING_IN_CONTRACT=0
    for field in $REQUIRED_FIELDS; do
      # id, source, action_tier are orchestrator-assigned (documented at line 81)
      if [[ "$field" == "id" || "$field" == "source" || "$field" == "action_tier" ]]; then
        continue
      fi
      if ! grep -q "\"$field\"" "$SKILL_DIR/prompts/reviewer-global-contract.md" 2>/dev/null; then
        warn "Required finding field '$field' not found in global contract output schema"
        MISSING_IN_CONTRACT=$((MISSING_IN_CONTRACT + 1))
      fi
    done
    if [ "$MISSING_IN_CONTRACT" -eq 0 ]; then
      pass "All required finding fields documented in global contract"
    fi
  fi
fi

echo ""
echo "--- Shell Script Quality ---"

# ---------------------------------------------------------------------------
# 9. shellcheck on all .sh files
# ---------------------------------------------------------------------------
SH_FILES=()
while IFS= read -r -d '' f; do
  SH_FILES+=("$f")
done < <(find "$REPO_ROOT/scripts" "$SKILL_DIR/scripts" -name '*.sh' -print0 2>/dev/null)

if [ ${#SH_FILES[@]} -eq 0 ]; then
  warn "No shell scripts found to check"
elif command -v shellcheck &>/dev/null; then
  SC_REPORT="$ARTIFACT_DIR/shellcheck-report.json"
  shellcheck --format=json "${SH_FILES[@]}" > "$SC_REPORT" 2>/dev/null || true

  # Count errors (level "error") vs warnings
  if command -v jq &>/dev/null; then
    SC_ERROR_COUNT=$(jq '[.[] | select(.level == "error")] | length' "$SC_REPORT" 2>/dev/null || echo 0)
    SC_WARN_COUNT=$(jq '[.[] | select(.level == "warning")] | length' "$SC_REPORT" 2>/dev/null || echo 0)

    if [ "$SC_ERROR_COUNT" -gt 0 ]; then
      fail "shellcheck: $SC_ERROR_COUNT error(s) in ${#SH_FILES[@]} script(s) (see $SC_REPORT)"
    elif [ "$SC_WARN_COUNT" -gt 0 ]; then
      warn "shellcheck: $SC_WARN_COUNT warning(s) in ${#SH_FILES[@]} script(s) (see $SC_REPORT)"
    else
      pass "shellcheck: ${#SH_FILES[@]} script(s) clean"
    fi
  else
    # Without jq, just check exit code
    if shellcheck "${SH_FILES[@]}" >/dev/null 2>&1; then
      pass "shellcheck: ${#SH_FILES[@]} script(s) clean"
    else
      warn "shellcheck: issues found in ${#SH_FILES[@]} script(s) (install jq for details)"
    fi
  fi
else
  warn "shellcheck not installed — skipping shell lint (brew install shellcheck)"
fi

echo ""
echo "--- Security Scan ---"

# ---------------------------------------------------------------------------
# 10. Secret scanning — grep tracked files for common secret patterns
# ---------------------------------------------------------------------------
SECRET_PATTERNS=(
  'AKIA[0-9A-Z]{16}'                          # AWS access key
  'sk-[a-zA-Z0-9]{20,}'                       # OpenAI / Stripe secret key
  'ghp_[a-zA-Z0-9]{36}'                       # GitHub personal access token
  'gho_[a-zA-Z0-9]{36}'                       # GitHub OAuth token
  'github_pat_[a-zA-Z0-9_]{82}'               # GitHub fine-grained PAT
  'glpat-[a-zA-Z0-9\-]{20}'                   # GitLab PAT
  'xox[bpors]-[a-zA-Z0-9\-]+'                 # Slack token
  '-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----' # Private key
  'password\s*[:=]\s*["\x27][^"\x27]{8,}'     # Hardcoded password
)

SECRETS_FOUND=0
SECRETS_REPORT="$ARTIFACT_DIR/secret-scan-report.txt"
: > "$SECRETS_REPORT"

# Only scan tracked files (not .agents/, not .git/)
TRACKED_FILES=$(git -C "$REPO_ROOT" ls-files 2>/dev/null)

for pattern in "${SECRET_PATTERNS[@]}"; do
  MATCHES=$(echo "$TRACKED_FILES" | xargs grep -rlE "$pattern" 2>/dev/null || true)
  if [ -n "$MATCHES" ]; then
    echo "Pattern: $pattern" >> "$SECRETS_REPORT"
    echo "$MATCHES" >> "$SECRETS_REPORT"
    echo "" >> "$SECRETS_REPORT"
    SECRETS_FOUND=$((SECRETS_FOUND + 1))
  fi
done

if [ "$SECRETS_FOUND" -gt 0 ]; then
  fail "Potential secrets found in $SECRETS_FOUND pattern(s) (see $SECRETS_REPORT)"
else
  pass "No secrets detected in tracked files"
  echo "Scanned $(echo "$TRACKED_FILES" | wc -l | tr -d ' ') tracked files against ${#SECRET_PATTERNS[@]} patterns" >> "$SECRETS_REPORT"
fi

echo ""
echo "--- Manifest ---"

# ---------------------------------------------------------------------------
# 11. Generate skill file manifest (SBOM-equivalent for a prompt skill)
# ---------------------------------------------------------------------------
MANIFEST="$ARTIFACT_DIR/skill-manifest.json"

# Build manifest of the distributable skill directory
if command -v jq &>/dev/null; then
  (
    cd "$SKILL_DIR"
    find . -type f | sort | while IFS= read -r f; do
      SIZE=$(wc -c < "$f" | tr -d ' ')
      SHA=$(shasum -a 256 "$f" | cut -d' ' -f1)
      printf '{"path":"%s","size":%s,"sha256":"%s"}\n' "$f" "$SIZE" "$SHA"
    done | jq -s '{
      "manifest_version": "1.0",
      "skill_name": "codereview",
      "release_version": "'"$RELEASE_VERSION"'",
      "timestamp": "'"$TIMESTAMP"'",
      "files": .,
      "file_count": length,
      "total_bytes": (map(.size) | add)
    }'
  ) > "$MANIFEST"
  FILE_COUNT=$(jq '.file_count' "$MANIFEST")
  TOTAL_KB=$(jq '.total_bytes / 1024 | floor' "$MANIFEST")
  pass "Manifest: $FILE_COUNT files, ${TOTAL_KB}KB total (see $MANIFEST)"
else
  # Fallback without jq
  (
    cd "$SKILL_DIR"
    echo "# Skill Manifest — codereview $RELEASE_VERSION"
    echo "# Generated: $TIMESTAMP"
    echo ""
    find . -type f | sort | while IFS= read -r f; do
      SIZE=$(wc -c < "$f" | tr -d ' ')
      SHA=$(shasum -a 256 "$f" | cut -d' ' -f1)
      printf '%s  %s  %s\n' "$SHA" "$SIZE" "$f"
    done
  ) > "$MANIFEST"
  pass "Manifest written (plain text, jq not available for JSON format)"
fi

# ---------------------------------------------------------------------------
# 12. Write release-artifacts.json
# ---------------------------------------------------------------------------
ARTIFACTS_JSON="$ARTIFACT_DIR/release-artifacts.json"
if command -v jq &>/dev/null; then
  jq -n \
    --arg version "$RELEASE_VERSION" \
    --arg timestamp "$TIMESTAMP" \
    --arg artifact_dir "$ARTIFACT_DIR" \
    --arg manifest "$MANIFEST" \
    --arg secret_scan "$SECRETS_REPORT" \
    --arg shellcheck_report "$ARTIFACT_DIR/shellcheck-report.json" \
    '{
      version: $version,
      timestamp: $timestamp,
      artifact_dir: $artifact_dir,
      artifacts: {
        manifest: $manifest,
        secret_scan: $secret_scan,
        shellcheck_report: $shellcheck_report
      }
    }' > "$ARTIFACTS_JSON"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "================================"
if [ "$ERRORS" -gt 0 ]; then
  echo "Result: FAIL ($ERRORS error(s), $WARNINGS warning(s))"
  echo "Artifacts: $ARTIFACT_DIR"
  exit 1
else
  echo "Result: PASS ($WARNINGS warning(s))"
  echo "Artifacts: $ARTIFACT_DIR"
  exit 0
fi
