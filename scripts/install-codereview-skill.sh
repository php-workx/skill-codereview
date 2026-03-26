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

chmod +x "$DEST_CLAUDE/scripts/validate_output.sh" \
  "$DEST_CODEX/scripts/validate_output.sh"

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
