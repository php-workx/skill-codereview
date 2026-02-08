# Changelog

## [Unreleased]

## [1.0.0] - 2026-02-08

### Added
- Initial release of codereview skill
- Explorer-judge architecture: 4 specialized explorers (correctness, security, reliability, test adequacy) + review judge
- Deterministic scans: semgrep, trivy, osv-scanner, shellcheck, pre-commit, sonarqube (best-effort)
- Multi-mode diff support: staged, commit, branch (`--base`), range (`--range`), PR, path
- Spec/plan comparison (`--spec`) for requirements completeness checking
- Language standards integration (optional, graceful degradation)
- Complexity analysis via radon/gocyclo
- Dead code / YAGNI detection
- Structured JSON output conforming to `findings-schema.json`
- Markdown review report with tiered findings (Must Fix / Should Fix / Consider)
- Configurable review cadence (manual, pre-commit, pre-push, wave-end)
- Configurable pushback level (fix-all, selective, cautious)
- Repo-level configuration via `.codereview.yaml`
- Output validation script (`validate_output.sh`)
- Install script for Claude and Codex
- Codex slash command prompt (`/codereview`)
- npm package distribution via GitHub Packages
- OpenSkills and skills.sh marketplace support
