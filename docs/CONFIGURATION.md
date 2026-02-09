# Configuration

The codereview skill supports optional repo-level configuration via `.codereview.yaml` in the repository root. All settings have sensible defaults — no configuration is required.

## Config File Schema

```yaml
# .codereview.yaml (optional)

# Which review passes to run (default: all 7)
passes:
  # Core passes (always run unless removed)
  - correctness
  - security
  - reliability
  - test-adequacy
  # Extended passes (subject to adaptive skip signals)
  - error-handling
  - api-contract
  - concurrency

# Minimum confidence for AI findings (default: 0.65)
confidence_floor: 0.65

# Review cadence — controls when /codereview runs automatically
# Options: manual (default), pre-commit, pre-push, wave-end
cadence: manual

# Pushback level — controls how aggressively findings are surfaced
# Options: fix-all (default), selective, cautious
pushback_level: fix-all

# Model override per pass (optional)
# Default: "sonnet" for all explorers, session default for judge
pass_models:
  # security: "opus"
  # concurrency: "opus"
  # judge: null

# Force all configured passes to run (disable adaptive skip)
# Default: false
force_all_passes: false

# Paths to ignore (glob patterns)
ignore_paths:
  - "*.generated.*"
  - "vendor/"
  - "node_modules/"

# Paths to focus on (higher priority for findings in these paths)
focus_paths:
  - "src/auth/"
  - "src/payments/"

# Custom instructions included in all review passes
custom_instructions: |
  This repo uses Django ORM. Flag any raw SQL queries.
  All API endpoints must have rate limiting.
```

## Settings Reference

### `passes`

Which AI review passes to run. Default: all 7 (4 core + 3 extended).

**Core passes** — always run when listed:

| Pass | Focus |
|------|-------|
| `correctness` | Functional bugs, regressions, logic errors |
| `security` | Auth, injection, secrets, trust boundaries |
| `reliability` | Timeouts, retries, resource leaks, performance |
| `test-adequacy` | Missing tests, stale tests, mock-heavy tests |

**Extended passes** — run when listed, subject to adaptive skip signals:

| Pass | Focus | Skip Signal |
|------|-------|-------------|
| `error-handling` | Swallowed exceptions, missing error propagation, inconsistent patterns | Diff is test/docs/config only |
| `api-contract` | Breaking API changes, missing backward compatibility, contract violations | No public API surface changes in diff |
| `concurrency` | Race conditions, deadlocks, shared mutable state, goroutine/thread leaks | No concurrency primitives in diff |

Extended passes are automatically skipped when their skip signal triggers. Use `force_all_passes: true` to override.

### `confidence_floor`

Minimum confidence score for AI findings to appear in the report. Default: `0.65`. Range: `0.0` to `1.0`.

Higher values reduce false positives but may miss some real issues.

### `cadence`

When the skill should be invoked in agent workflows. Default: `manual`.

| Mode | When It Runs |
|------|-------------|
| `manual` | Only when user invokes `/codereview` |
| `pre-commit` | Before every commit in agent workflows |
| `pre-push` | Before push (agent checks before sharing) |
| `wave-end` | After a batch of tasks completes |

The cadence setting is advisory — it tells the agent *when* to call the skill, not a git hook.

### `pushback_level`

Controls how aggressively findings are surfaced. Default: `fix-all`.

| Level | Must Fix | Should Fix | Consider |
|-------|----------|------------|----------|
| `fix-all` | Fix immediately | Fix in this PR | Fix if time permits |
| `selective` | Fix immediately | Fix in this PR | Informational only |
| `cautious` | Fix immediately | Informational | Informational only |

### `pass_models`

Override the model used for specific explorer passes or the judge. Default: `"sonnet"` for all explorers, session default model for the judge.

```yaml
pass_models:
  security: "opus"       # use stronger model for security analysis
  concurrency: "opus"    # use stronger model for concurrency
  judge: null            # null = session default model
```

Valid model values: `"sonnet"`, `"opus"`, `"haiku"`, or `null` (session default).

Use stronger models for passes where precision matters most (security, concurrency). Use faster models for passes where recall is more important than precision (test-adequacy).

### `force_all_passes`

Disable adaptive skip signals for extended passes. Default: `false`.

When `true`, all passes listed in `passes` will run regardless of skip signals. When `false`, extended passes are automatically skipped when their skip signal triggers (e.g., concurrency pass skipped when no concurrency primitives detected in the diff).

### `ignore_paths`

Glob patterns for files to exclude from review. These files are filtered out of `CHANGED_FILES` before deterministic scans and AI review.

### `focus_paths`

Glob patterns for high-priority paths. Findings in these paths get slightly boosted in the ranking within each tier.

### `custom_instructions`

Free-text instructions included in the context packet for all review passes. Use this for repo-specific conventions that the AI should enforce.

## Precedence

If multiple configuration sources exist:

1. CLI flags (highest priority)
2. `.codereview.yaml` in repo root
3. Built-in defaults (lowest priority)

## No Config Required

If no `.codereview.yaml` exists, the skill uses defaults:
- All 7 passes enabled (4 core + 3 extended)
- 0.65 confidence floor
- Manual cadence
- fix-all pushback
- sonnet model for all explorers
- Adaptive skip enabled
- No ignored or focused paths
