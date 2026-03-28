#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SKILL_NAME="codereview"

SRC_SKILL="$REPO_ROOT/skills/codereview"
SRC_PROMPT="$REPO_ROOT/prompts/codereview.md"

if [[ ! -d "$SRC_SKILL" ]]; then
	echo "error: missing source skill directory at $SRC_SKILL" >&2
	exit 1
fi

if [[ -d "$HOME/.claude/skills" ]]; then
	DEST_CLAUDE_BASE="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
else
	DEST_CLAUDE_BASE="${CLAUDE_SKILLS_DIR:-$HOME/.agents/skills}"
fi
DEST_CODEX_BASE="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
DEST_CODEX_PROMPTS_BASE="${CODEX_PROMPTS_DIR:-$HOME/.codex/prompts}"

DEST_CLAUDE="$DEST_CLAUDE_BASE/$SKILL_NAME"
DEST_CODEX="$DEST_CODEX_BASE/$SKILL_NAME"

mkdir -p "$DEST_CLAUDE_BASE" "$DEST_CODEX_BASE"
mkdir -p "$DEST_CODEX_PROMPTS_BASE"

# Atomic install: copy to temp dirs first, then move into place so a failed
# copy never leaves the destination in a broken state.
DEST_CLAUDE_TMP="${DEST_CLAUDE}.installing"
DEST_CODEX_TMP="${DEST_CODEX}.installing"
rm -rf "$DEST_CLAUDE_TMP" "$DEST_CODEX_TMP"

cp -R "$SRC_SKILL" "$DEST_CLAUDE_TMP" || {
	echo "error: failed to copy skill to Claude destination" >&2
	rm -rf "$DEST_CLAUDE_TMP"
	exit 1
}
cp -R "$SRC_SKILL" "$DEST_CODEX_TMP" || {
	echo "error: failed to copy skill to Codex destination" >&2
	rm -rf "$DEST_CLAUDE_TMP" "$DEST_CODEX_TMP"
	exit 1
}

rm -rf "$DEST_CLAUDE" "$DEST_CODEX"
mv "$DEST_CLAUDE_TMP" "$DEST_CLAUDE"
mv "$DEST_CODEX_TMP" "$DEST_CODEX"

# Copy repo-root scripts (live at repo root scripts/, not inside the skill dir)
for script_name in orchestrate.py code_intel.py prescan.py; do
	SRC_SCRIPT="$REPO_ROOT/scripts/$script_name"
	if [[ -f "$SRC_SCRIPT" ]]; then
		for dest in "$DEST_CLAUDE" "$DEST_CODEX"; do
			cp "$SRC_SCRIPT" "$dest/scripts/$script_name" || {
				echo "error: failed to copy $script_name to $dest/scripts/" >&2
				exit 1
			}
			chmod +x "$dest/scripts/$script_name"
		done
	else
		echo "warning: $script_name not found at $SRC_SCRIPT" >&2
	fi
done

# Make all scripts executable
for dest in "$DEST_CLAUDE" "$DEST_CODEX"; do
	# Use compgen to check for matching files before chmod (avoids glob expansion errors)
	for pat in "$dest/scripts/"*.sh "$dest/scripts/"*.py; do
		if compgen -G "$pat" >/dev/null 2>&1; then
			chmod +x "$pat" || {
				echo "error: failed to chmod +x $pat" >&2
				exit 1
			}
		fi
	done
done

if [[ -f "$SRC_PROMPT" ]]; then
	cp "$SRC_PROMPT" "$DEST_CODEX_PROMPTS_BASE/codereview.md" || {
		echo "error: failed to copy Codex prompt from $SRC_PROMPT" >&2
		exit 1
	}
fi

cat <<EOF
Installed $SKILL_NAME:
- Claude: $DEST_CLAUDE
- Codex:  $DEST_CODEX
Codex slash command prompt:
- $DEST_CODEX_PROMPTS_BASE/codereview.md
EOF
