# codereview

Local AI code review for Claude Code and Codex. Review your changes *before* they become a PR — catch bugs, security issues, and test gaps while you're still writing code, not after.

## Why Use This?

- **Faster feedback loop**: Review locally in seconds, not waiting for CI/PR review bots
- **Deeper than linters**: AI explorers trace call paths, check callers, verify test coverage — things no linter catches
- **Spec verification**: Pass a spec file and verify every requirement is actually implemented and behaviorally correct — not just that a matching function exists
- **Works offline**: Only needs your local git repo and whatever tools you have installed
- **Agent-friendly**: Findings include fix suggestions that Claude/Codex can execute immediately

## Features

- **Explorer-judge architecture**: 8 specialized explorer sub-agents (correctness, security, reliability, test adequacy + 4 extended passes) investigate in parallel, then a review judge synthesizes findings with adversarial validation
- **Large-diff chunked review**: diffs exceeding 80 files or 8000 lines are automatically split into risk-tiered chunks with cross-chunk analysis, keeping reviews within context limits without sacrificing quality
- **Deterministic scans first**: semgrep, trivy, osv-scanner, shellcheck, pre-commit, sonarqube run before AI review
- **Local-first review modes**: staged changes, last commit, full branch (`--base`), commit range (`--range`), specific path — also works on PRs
- **Spec verification with traceability**: check implementation against a plan/spec with `--spec`, scope to sections with `--spec-scope`, get per-requirement behavioral verification and test category coverage
- **Deep correctness analysis**: traces nil/partial objects on skip paths, detects type mismatches across serialization boundaries, catches panics from uninitialized fields
- **Deep security analysis**: traces inter-component data flows into dangerous sinks, detects environment variable namespace hijacking from dynamic sources
- **Test category classification**: classifies tests as unit/integration/e2e and flags when the wrong category is used
- **Adaptive pass selection**: extended passes (error handling, API/contract, concurrency, spec verification) auto-skip when irrelevant to the diff
- **Structured output**: JSON findings conforming to `findings-schema.json` + markdown report
- **Action tiers**: Must Fix / Should Fix / Consider with mechanical classification
- **Configurable**: review cadence, pushback level, confidence floor, model per pass, chunked mode thresholds, focus/ignore paths via `.codereview.yaml`

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
pass_models:
  security: "opus"       # use stronger model where precision matters
ignore_paths:
  - "vendor/"
  - "*.generated.*"
large_diff:
  file_threshold: 80     # file count that triggers chunked mode
  line_threshold: 8000   # diff line count that triggers chunked mode
```

See `docs/CONFIGURATION.md` for the full schema reference.

## Repository Layout

- `skills/codereview/` — canonical skill source for indexers and installers (SKILL.md, prompts, scripts, schema, references)
- `prompts/codereview.md` — Codex slash command dispatcher
- `scripts/install-codereview-skill.sh` — copies skill to Claude and Codex directories
- `docs/` — configuration reference, release process

## Troubleshooting

- **No tools available**: The skill still works — AI passes run without deterministic scans. Install `semgrep` and `shellcheck` at minimum for best results.
- **Empty diff**: The skill exits cleanly with "No changes found to review."
- **`jq` not installed**: Output validation is skipped. Install with `brew install jq`.
- **`gh` not authenticated**: PR mode requires `gh auth status` to work. Run `gh auth login` first.

## Release Process

See `docs/RELEASE.md`.
