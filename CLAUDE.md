# CLAUDE.md

## Project overview

AI-powered multi-pass code review skill for Claude Code and Codex. The pipeline uses an explorer-judge architecture: deterministic scripts handle diff extraction, project discovery, and report rendering, while AI agents perform the actual review passes.

## Development

- **Python**: >=3.11, managed with `uv`. Run `uv sync` to set up the venv.
- **Task runner**: `just` (see `justfile` for all commands).
- **Tickets**: Use `tk` for issue tracking.

### Running tests

```bash
just test-unit          # Python unit tests (via uv run)
just test-scripts       # Shell script tests
just test-integration   # Integration tests
just check              # Full pre-push gate
```

### Project structure

- `scripts/orchestrate.py` — Main orchestration entry point
- `skills/codereview/` — Installable skill package (SKILL.md, prompts, scripts, references)
- `prompts/codereview.md` — Codex slash-command prompt
- `tests/` — Unit and integration tests
- `.eval/` — Benchmark infrastructure (gitignored)

### Key conventions

- `orchestrate.py` lives at repo root `scripts/` but gets installed into the skill's `scripts/` dir. Use `SKILL_DIR` (not `repo_root`) for any path that references skill-owned files (prompts, references, sub-scripts).
- All sub-scripts are invoked via `SKILL_DIR / "scripts"` paths in orchestrate.py.
- `yaml` is the only non-stdlib dependency and is optional (used for `.codereview.yaml` config).
