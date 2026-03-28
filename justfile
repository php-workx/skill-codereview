set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
test_modules := "tests.test_orchestrate tests.test_orchestrate_prepare tests.test_orchestrate_json tests.test_orchestrate_phases tests.test_orchestrate_alignment tests.test_eval_store tests.test_eval_owasp"

default:
  @just --list

# Fast local gate for staged files. Used by the git pre-commit hook.
pre-commit:
  bash scripts/check-pre-commit.sh

# Broader local gate before pushing.
pre-push:
  bash scripts/check-pre-push.sh

# Full local check entry point.
check: pre-push
  @echo "All checks passed."

test-scripts:
  bash tests/test-scripts.sh

test-unit:
  uv run python -m unittest {{test_modules}}

test-integration:
  bash tests/test-orchestrate-integration.sh

install-hooks:
  bash scripts/install-hooks.sh

setup: install-hooks
  @echo "Git hooks installed."
