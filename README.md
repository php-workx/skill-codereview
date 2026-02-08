# skill-codereview

AI-powered multi-pass code review skill for Claude, Codex, and Cursor.

This repository provides:

- `codereview` skill definitions for Claude and Codex
- `/codereview` prompt command for Codex
- Installer script to copy skill files into user skill directories

## Features

- **Explorer-judge architecture**: 4 specialized explorer sub-agents (correctness, security, reliability, test adequacy) investigate in parallel, then a review judge synthesizes findings
- **Deterministic scans first**: semgrep, trivy, osv-scanner, shellcheck, pre-commit, sonarqube run before AI review
- **Multiple review modes**: staged changes, last commit, full branch (`--base`), commit range (`--range`), PR, or specific path
- **Spec comparison**: check implementation against a plan/spec with `--spec`
- **Structured output**: JSON findings conforming to `findings-schema.json` + markdown report
- **Action tiers**: Must Fix / Should Fix / Consider with mechanical classification
- **Configurable**: review cadence, pushback level, confidence floor, focus/ignore paths via `.codereview.yaml`

## Prerequisites

- `git`
- `jq` (for output validation)
- Optional: `semgrep`, `trivy`, `osv-scanner`, `shellcheck`, `radon`, `gocyclo`, `skill-sonarqube`

All deterministic tools are optional — the skill degrades gracefully and notes missing tools in the report.

## Install

### Via OpenSkills

```bash
# project-local install
npx openskills install php-workx/skill-codereview

# global install
npx openskills install -g php-workx/skill-codereview
```

### Via skills.sh

```bash
# project-local install
npx skills add php-workx/skill-codereview

# global install
npx skills add -g php-workx/skill-codereview
```

### Via npm (GitHub Packages)

```bash
npm install @php-workx/skill-codereview --registry=https://npm.pkg.github.com
```

### Via installer script

```bash
git clone --depth 1 --branch v1.0.0 https://github.com/php-workx/skill-codereview.git
cd skill-codereview
bash scripts/install-codereview-skill.sh
```

After install, restart Claude/Codex so the new skill is loaded.

## Usage

Codex slash command:

```text
/codereview
/codereview 42
/codereview --base main
/codereview --range HEAD~5..HEAD
/codereview --spec docs/plan.md --base main
```

Natural language also works when the `codereview` skill is selected by intent.

## Configuration

Optional repo-level config via `.codereview.yaml`:

```yaml
cadence: manual          # manual | pre-commit | pre-push | wave-end
pushback_level: fix-all  # fix-all | selective | cautious
confidence_floor: 0.65
ignore_paths:
  - "vendor/"
  - "*.generated.*"
```

See `docs/CONFIGURATION.md` for details.

## Repository Layout

- `skill/` — canonical skill source (SKILL.md, prompts, scripts, schema, agents config)
- `prompts/codereview.md` — Codex slash command dispatcher
- `scripts/install-codereview-skill.sh` — copies skill to Claude and Codex directories

## Troubleshooting

- **No tools available**: The skill still works — AI passes run without deterministic scans. Install tools for better coverage.
- **Empty diff**: The skill exits cleanly with "No changes found to review."
- **`jq` not installed**: Output validation is skipped. Install with `brew install jq`.

## Release Process

See `docs/RELEASE.md`.
