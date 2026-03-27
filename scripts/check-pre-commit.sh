#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

FILES=()
while IFS= read -r file; do
	[ -n "$file" ] && FILES+=("$file")
done < <(git diff --cached --name-only --diff-filter=ACMR)

if [ ${#FILES[@]} -eq 0 ]; then
	echo "No staged files. Skipping pre-commit checks."
	exit 0
fi

git diff --cached --check

PY_FILES=()
SH_FILES=()
WORKFLOW_FILES=()

for file in "${FILES[@]}"; do
	case "$file" in
	*.py)
		PY_FILES+=("$file")
		;;
	*.sh | scripts/pre-commit | scripts/pre-push)
		SH_FILES+=("$file")
		;;
	.github/workflows/*.yml | .github/workflows/*.yaml)
		WORKFLOW_FILES+=("$file")
		;;
	esac
done

if [ ${#PY_FILES[@]} -gt 0 ]; then
	command -v ruff >/dev/null 2>&1 || {
		echo "ruff not found. Install: pip install ruff" >&2
		exit 1
	}
	ruff check "${PY_FILES[@]}"
	ruff format --check "${PY_FILES[@]}"
fi

if [ ${#SH_FILES[@]} -gt 0 ]; then
	bash -n "${SH_FILES[@]}"

	command -v shellcheck >/dev/null 2>&1 || {
		echo "shellcheck not found. Install: brew install shellcheck" >&2
		exit 1
	}
	shellcheck "${SH_FILES[@]}"

	command -v shfmt >/dev/null 2>&1 || {
		echo "shfmt not found. Install: brew install shfmt" >&2
		exit 1
	}
	shfmt -d "${SH_FILES[@]}"
fi

if [ ${#WORKFLOW_FILES[@]} -gt 0 ]; then
	command -v actionlint >/dev/null 2>&1 || {
		echo "actionlint not found. Install: brew install actionlint" >&2
		exit 1
	}
	actionlint "${WORKFLOW_FILES[@]}"
fi

command -v gitleaks >/dev/null 2>&1 || {
	echo "gitleaks not found. Install: brew install gitleaks" >&2
	exit 1
}

tmpdir="$(mktemp -d /tmp/skill-codereview-gitleaks-XXXXXX)"
cleanup() {
	rm -rf "$tmpdir"
}
trap cleanup EXIT INT TERM

for file in "${FILES[@]}"; do
	if ! git cat-file -e ":$file" 2>/dev/null; then
		continue
	fi
	mkdir -p "$tmpdir/$(dirname "$file")"
	git show ":$file" >"$tmpdir/$file"
done

gitleaks detect \
	--source "$tmpdir" \
	--report-format json \
	--report-path "$tmpdir/gitleaks.json" \
	--no-git \
	--follow-symlinks \
	--exit-code 1 \
	--no-banner
