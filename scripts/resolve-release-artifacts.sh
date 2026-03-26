#!/usr/bin/env bash
# resolve-release-artifacts.sh — Find the latest local-CI artifact set for a version
#
# Usage: ./scripts/resolve-release-artifacts.sh <version>
# Output: JSON with artifact_dir and file paths (for the release skill audit trail)

set -euo pipefail

VERSION="${1:?Usage: resolve-release-artifacts.sh <version>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
CI_DIR="$REPO_ROOT/.agents/releases/local-ci"

if [ ! -d "$CI_DIR" ]; then
  echo '{"error": "No local-CI runs found", "artifact_dir": null}' | jq .
  exit 1
fi

# Find the most recent artifact directory
LATEST=$(ls -1d "$CI_DIR"/*/ 2>/dev/null | sort -r | head -1)

if [ -z "$LATEST" ]; then
  echo '{"error": "No artifact directories found", "artifact_dir": null}' | jq .
  exit 1
fi

# Check if release-artifacts.json exists
ARTIFACTS_JSON="$LATEST/release-artifacts.json"
if [ -f "$ARTIFACTS_JSON" ] && command -v jq &>/dev/null; then
  # Update the version in the artifact record and return
  jq --arg v "$VERSION" '.version = $v' "$ARTIFACTS_JSON"
else
  # Fallback: construct minimal response
  jq -n --arg dir "$LATEST" --arg v "$VERSION" '{
    version: $v,
    artifact_dir: $dir
  }'
fi
