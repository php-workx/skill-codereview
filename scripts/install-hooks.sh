#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

HOOKS_DIR="$(cd "$REPO_ROOT" && git rev-parse --path-format=absolute --git-path hooks 2>/dev/null)" ||
	HOOKS_DIR="$REPO_ROOT/.git/hooks"
mkdir -p "$HOOKS_DIR"

for hook in pre-commit pre-push; do
	src="$SCRIPT_DIR/$hook"
	dst="$HOOKS_DIR/$hook"
	cp "$src" "$dst"
	chmod +x "$dst"
	echo "Installed $hook hook."
done

echo "Done. Git hooks installed."
