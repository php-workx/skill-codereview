# Changelog

## [Unreleased]

## [1.1.0] - 2026-02-09

### Added
- **3 new explorer passes** (extended, with adaptive skip signals):
  - Error handling: swallowed exceptions, missing error propagation, inconsistent patterns, missing rollback
  - API/contract: breaking changes, backward compatibility, convention consistency, documentation sync
  - Concurrency: shared mutable state, lock ordering, TOCTOU races, goroutine/thread/task leaks
- **Extracted judge prompt** (`reviewer-judge.md`) with adversarial validation protocol:
  - Existence check: verify cited code exists at stated file and line
  - Contradiction check: actively search for defenses that disprove findings
  - Severity calibration: downgrade theoretical findings without demonstrated call paths
  - Root cause grouping: merge related findings, eliminate causal chain duplicates
  - Cross-explorer synthesis: catch gaps no single explorer flagged
- **Chain-of-thought investigation protocol** in global contract — structured phased investigation for all explorers
- **Calibration examples** in every explorer prompt (3 per pass: high-confidence true positive, medium-confidence true positive, false positive to suppress)
- **False positive suppression lists** per pass (7-8 specific patterns each)
- **`pass_models` config** — override model per pass (e.g., use opus for security, sonnet for others)
- **`force_all_passes` config** — disable adaptive skip signals for extended passes
- **Adaptive pass selection** — extended passes auto-skip when irrelevant (e.g., concurrency pass skipped when no concurrency primitives in diff)

### Changed
- Explorer prompts expanded from ~16 lines to ~120-140 lines each with structured investigation phases
- Global contract updated with confidence calibration table and chain-of-thought protocol
- SKILL.md Step 4a updated to support up to 7 explorers with configurable models
- SKILL.md Step 4b now references external judge prompt file instead of inline prompt
- Tool status table expanded with keys for new passes and judge
- Default config now includes all 7 passes (4 core + 3 extended)

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
