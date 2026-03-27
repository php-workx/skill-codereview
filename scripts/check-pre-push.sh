#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

just pre-commit
python3 -m unittest \
	tests.test_orchestrate \
	tests.test_orchestrate_prepare \
	tests.test_orchestrate_json \
	tests.test_orchestrate_phases \
	tests.test_orchestrate_alignment
bash tests/test-scripts.sh
bash tests/test-orchestrate-integration.sh
