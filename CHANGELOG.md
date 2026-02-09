# Changelog

## [Unreleased]

### Added
- **Spec verification explorer pass** (`reviewer-spec-verification-pass.md`):
  - Per-requirement traceability: extracts requirements from spec, traces to implementation, maps to tests
  - Test category classification: classifies tests as unit/integration/e2e with evidence
  - Category adequacy assessment: flags when requirements need integration/e2e tests but only have unit
  - Scoped verification via `--spec-scope` flag (filter to section/milestone)
  - Full `spec_requirements` output with impl_status, test_coverage, needed_categories per requirement
- **Test category classification** in test-adequacy pass (Phase 6):
  - Classifies discovered tests as unit/integration/e2e/unknown using directory, mock-density, and infrastructure heuristics
  - `test_category_needed` field on findings specifying which test category is missing
- **`--spec-scope <text>` flag** — restrict spec verification to a specific section or milestone
- **`spec_requirements` in findings schema** — structured per-requirement traceability replacing flat `spec_gaps` list (backward-compatible: `spec_gaps` still populated)
- **`spec_verification` pass enum value** — findings from spec verification are tagged distinctly
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
- Global contract updated with confidence calibration table, chain-of-thought protocol, and `spec_verification` pass value
- SKILL.md Step 4a updated to support up to 8 explorers with configurable models
- SKILL.md Step 4b now references external judge prompt file instead of inline prompt
- SKILL.md report format: "Spec Gaps" section replaced with rich "Spec Verification" section (requirement table + details) when spec is provided
- Judge Step 5 enhanced: merges spec-verification explorer data, validates impl/test claims, produces structured `spec_requirements`
- Test-adequacy pass expanded with Phase 6 (test category classification)
- Tool status table expanded with keys for new passes including `ai_spec_verification`
- Default config now includes all 8 passes (4 core + 4 extended)
- Validation script updated with spec_requirements checks (12a-12e) and `spec_verification` pass value
- **SKILL.md description** rewritten with local-first positioning — leads with local review, removes redundant trigger keywords
- **README** rewritten with "Why Use This?" section, local-first framing, updated feature list (8 explorers, spec verification, test categories)
- **SKILL.md token efficiency**: extracted heavy reference material into 3 new files (6,641→4,086 words, -38%)
  - `references/deterministic-scans.md` — full tool scripts, cache setup, parallel patterns, zsh workarounds
  - `references/report-template.md` — full markdown report template and JSON envelope format
  - `references/acceptance-criteria.md` — functional scenarios and output validation checks
- **"When to Use" section** added with user-facing symptoms and diff mode decision table
- **User-facing common mistakes** added (huge diffs, missing tools, `gh` auth, vague specs)
- Fixed stale "4 passes" references → 8 passes throughout
- Configuration section condensed to summary table referencing `docs/CONFIGURATION.md`

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
