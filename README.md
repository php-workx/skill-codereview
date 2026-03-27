# codereview

Local AI code review for Claude Code and Codex. Review your changes *before* they become a PR — catch bugs, security issues, and test gaps while you're still writing code, not after.

## Why Use This?

- **Faster feedback loop**: Review locally in seconds, not waiting for CI/PR review bots
- **Deeper than linters**: AI explorers trace call paths, check callers, verify test coverage — things no linter catches
- **Spec verification**: Pass a spec file and verify every requirement is actually implemented and behaviorally correct — not just that a matching function exists
- **Works offline**: Only needs your local git repo and whatever tools you have installed
- **Agent-friendly**: Findings include fix suggestions that Claude/Codex can execute immediately

## Features

### Pipeline (working)

The deterministic orchestrator (`scripts/orchestrate.py`) handles diff extraction, context gathering, scan execution, prompt assembly, and report rendering. These phases are fully implemented and tested.

- **Deterministic scans first**: semgrep, trivy, osv-scanner, shellcheck, pre-commit, sonarqube run before AI review
- **Local-first review modes**: staged changes, last commit, full branch (`--base`), commit range (`--range`), specific path — also works on PRs
- **Adaptive pass selection**: extended passes (error handling, API/contract, concurrency, spec verification) auto-skip when irrelevant to the diff
- **Coverage context collection**: loads existing coverage artifacts into review context without forcing a fresh test run
- **Structured output**: JSON findings conforming to `findings-schema.json` + markdown report
- **Action tiers**: Must Fix / Should Fix / Consider with mechanical classification
- **Configurable**: confidence floor, pass selection, model per pass, chunk thresholds, and prompt budgets via `.codereview.yaml`

### AI review (requires skill execution in Claude Code or Codex)

The AI phases run when you invoke `/codereview` — the skill wrapper launches explorer and judge sub-agents using the prompts and launch packets assembled by the orchestrator.

- **Explorer-judge architecture**: 3 core explorers (correctness, security, test adequacy) plus up to 6 adaptive passes investigate the diff, then a review judge synthesizes the result
- **Spec verification with scoped prompts**: pass a spec file with `--spec`, narrow prompt context to matching headings with `--spec-scope`, and feed that context into explorer selection and judging
- **Deep correctness analysis**: traces nil/partial objects on skip paths, detects type mismatches across serialization boundaries, catches panics from uninitialized fields
- **Deep security analysis**: traces inter-component data flows into dangerous sinks, detects environment variable namespace hijacking from dynamic sources

### Planned

- **Large-diff chunked review**: diffs exceeding 80 files or 8000 lines will be split into chunk waves with chunk metadata carried into the final report (not yet available — large diffs run in standard mode)

## Prerequisites

- `git`
- `jq` (for output validation)
- `PyYAML` if you want `.codereview.yaml` support
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
git clone --depth 1 https://github.com/php-workx/skill-codereview.git
cd skill-codereview
bash scripts/install-codereview-skill.sh
```

After install, restart Claude/Codex so the new skill is loaded.

## Usage

Review local changes before they leave your machine:

```text
/codereview                                                  # staged changes or HEAD~1
/codereview --base main                                      # entire feature branch
/codereview --range HEAD~5..HEAD                             # specific commits
/codereview src/auth/                                        # specific path
/codereview --spec docs/plan.md --base main                  # verify against spec
/codereview --spec docs/plan.md --spec-scope "Auth" --base main  # one section of spec
/codereview --base main --no-chunk                           # force standard mode on large diffs
/codereview --force-chunk                                    # force chunked mode for testing
/codereview 42                                               # PR #42 (also supported)
```

Natural language also works when the `codereview` skill is selected by intent.

## Configuration

Optional repo-level config via `.codereview.yaml`:

```yaml
cadence: manual          # manual | pre-commit | pre-push | wave-end
pushback_level: fix-all  # fix-all | selective | cautious
confidence_floor: 0.65
passes:
  - correctness
  - security
  - test-adequacy
  - reliability
pass_models:
  security: "opus"       # use stronger model where precision matters
large_diff:
  file_threshold: 80     # file count that triggers chunked mode
  line_threshold: 8000   # diff line count that triggers chunked mode
  max_chunk_files: 15
token_budget:
  explorer_prompt: 70000
```

See `docs/CONFIGURATION.md` for the full schema reference.

## Local Quality Gates

This repo now uses a `justfile` as the local quality-gate entry point, with git hooks delegating to `just` targets:

```bash
just pre-commit   # staged-file checks: whitespace, ruff, shellcheck, shfmt, gitleaks
just pre-push     # pre-commit gate + unit tests + script tests + integration test
just check        # full local gate entry point
just install-hooks
```

The pre-commit gate is intentionally file-targeted. It checks staged files only, while branch-wide tests and heavier validation run at pre-push time.

## Repository Layout

- `skills/codereview/` — canonical skill source for indexers and installers (SKILL.md, prompts, scripts, schema, references)
- `prompts/codereview.md` — Codex slash command dispatcher
- `scripts/install-codereview-skill.sh` — copies skill to Claude and Codex directories
- `docs/` — configuration reference, release process

## Troubleshooting

- **No tools available**: The skill still works — AI passes run without deterministic scans. Install `semgrep` and `shellcheck` at minimum for best results.
- **Empty diff**: The skill exits cleanly with "No changes found to review."
- **`jq` not installed**: Output validation is skipped. Install with `brew install jq`.
- **`.codereview.yaml` fails to load**: Install `pyyaml` or rerun with `--no-config`.
- **`gh` not authenticated**: PR mode requires `gh auth status` to work. Run `gh auth login` first.

## Release Process

See `docs/RELEASE.md`.
