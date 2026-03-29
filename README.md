# codereview

Local AI code review for Claude Code and Codex. Review your changes *before* they become a PR — catch bugs, security issues, and test gaps while you're still writing code, not after.

## Why Use This?

- **Faster feedback loop**: Review locally in seconds, not waiting for CI/PR review bots
- **Deeper than linters**: AI explorers trace call paths, build dependency graphs, and verify test coverage — things no linter catches
- **Code intelligence**: LLM-optimized diffs, cross-file context, caller analysis, and prescan signals give reviewers the context they need
- **Spec verification**: Pass `--spec docs/plan.md` and verify every requirement is actually implemented — not just that a matching function exists
- **Works offline**: Only needs your local git repo and whatever tools you have installed
- **Agent-friendly**: Findings include fix suggestions that Claude/Codex can execute immediately

## Quick Start

```text
/codereview                                    # all uncommitted changes
/codereview --base main                        # entire feature branch
/codereview --spec docs/plan.md --base main    # verify against spec
```

When no flags are given, the skill asks what to review — offering "since last review", "all uncommitted changes", or "compared to main".

## Install

```bash
# Via installer script
git clone --depth 1 https://github.com/php-workx/skill-codereview.git
cd skill-codereview && bash scripts/install-codereview-skill.sh

# Via OpenSkills
npx openskills install php-workx/skill-codereview
```

After install, restart Claude/Codex so the new skill is loaded. Run `/codereview --setup` to install optional analysis tools.

## What It Does

1. **Scans**: ruff, semgrep, trivy, shellcheck, gitleaks run first (per-language filtering, all optional)
2. **Code intelligence**: Dependency graphs, caller analysis, function extraction, prescan signals
3. **AI explorers**: 3 core passes (correctness, security, test adequacy) + up to 6 adaptive passes investigate the diff
4. **Judge**: Adversarial validation deduplicates, verifies, and calibrates all findings
5. **Report**: JSON + markdown with action tiers (Must Fix / Should Fix / Consider)

## Configuration

Optional `.codereview.yaml` in your repo root:

```yaml
confidence_floor: 0.65          # drop findings below this confidence
minimum_severity: "low"         # drop below this severity (low/medium/high/critical)
suggest_missing_tests: false    # true to suggest new tests for untested code
pass_models:
  security: "opus"              # stronger model where precision matters
```

See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the full schema.

## Documentation

- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — Full config schema and CLI flags
- [`docs/design.md`](skills/codereview/references/design.md) — Architecture and design decisions
- [`docs/RELEASE.md`](docs/RELEASE.md) — Release process

## Prerequisites

- `git`, `jq`
- Optional: `semgrep`, `trivy`, `shellcheck`, `radon`, `gocyclo`, `ruff`, `ast-grep`
- Optional (semantic search): `model2vec` for vector-based similarity in dependency graphs

All tools are optional — the skill degrades gracefully and notes missing tools in the report.

## Development

```bash
uv sync                    # set up Python venv
just check                 # full local quality gate
just test-unit             # Python unit tests (432)
just test-scripts          # shell script tests (321)
```

## License

See [LICENSE](LICENSE).
