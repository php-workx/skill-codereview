# Configuration

The orchestrator reads optional repo-level configuration from `.codereview.yaml` at the repository root.

`PyYAML` is required when this file exists. If you want defaults only, run with `--no-config`.

## Implemented Fields

```yaml
confidence_floor: 0.65

passes:
  - correctness
  - security
  - test-adequacy
  - reliability
  - error-handling
  - api-contract
  - concurrency
  - shell-script
  - spec-verification

force_all_passes: false

experts:
  force_all: false
  concurrency: true
  reliability: false

pass_models:
  security: "opus"
  concurrency: "opus"

judge_model: "sonnet"

large_diff:
  file_threshold: 80
  line_threshold: 8000
  max_chunk_files: 15

token_budget:
  explorer_prompt: 70000
  judge_prompt: 80000

custom_instructions: |
  Flag raw SQL usage.
  All auth endpoints must enforce rate limits.
```

## Behavior

### `confidence_floor`

Minimum confidence required for explorer findings to survive into `judge-input.json`.

### `passes`

Optional allowlist of expert passes. When unset, the orchestrator keeps the full adaptive panel.

Supported pass names:

- `correctness`
- `security`
- `test-adequacy`
- `shell-script`
- `api-contract`
- `concurrency`
- `error-handling`
- `reliability`
- `spec-verification`

### `force_all_passes`

Forces all adaptive passes on, subject to `passes` filtering and explicit per-expert disables.

### `experts`

Supports:

- `force_all: true` to force the adaptive panel on
- `name: false` to disable a specific pass

Example:

```yaml
experts:
  force_all: true
  reliability: false
```

Legacy `expert_panel.force_all` and `expert_panel.experts.<name>` are still accepted.

### `pass_models`

Overrides the model recorded for specific explorer passes.

### `judge_model`

Overrides the model recorded for the judge packet.

### `large_diff`

Controls chunked mode:

- `file_threshold`: switch to chunked mode when changed file count meets or exceeds this value
- `line_threshold`: switch to chunked mode when diff line count meets or exceeds this value
- `max_chunk_files`: maximum files grouped into one chunk

### `token_budget`

Currently enforced for:

- `explorer_prompt`

`judge_prompt` is accepted and persisted but not currently enforced by `scripts/orchestrate.py`.

### `custom_instructions`

Included in prompt context via `.codereview.yaml`.

## CLI Overrides

`scripts/orchestrate.py prepare` supports:

```bash
python3 scripts/orchestrate.py prepare \
  --session-dir /tmp/codereview \
  --base main \
  --range HEAD~5..HEAD \
  --pr 42 \
  --path src/auth \
  --spec docs/plan.md \
  --spec-scope "Billing" \
  --no-chunk \
  --force-chunk \
  --force-all-experts \
  --confidence-floor 0.8 \
  --no-config
```

Semantics:

- `--path` is a hard error when the target path does not exist.
- `--spec-scope` matches markdown headings by case-insensitive substring. If no heading matches, the full spec is kept.
- `--no-chunk` forces standard mode.
- `--force-chunk` forces chunked mode.
- `--force-all-experts` overrides config and enables the adaptive passes.

## Accepted But Not Enforced

These fields may exist in config and merge successfully, but the current orchestrator does not apply behavior for them directly:

- `cadence`
- `pushback_level`
- `ignore_paths`

If you document or depend on them elsewhere, treat them as wrapper-level policy, not current `scripts/orchestrate.py` behavior.
