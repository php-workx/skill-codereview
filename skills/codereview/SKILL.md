---
name: codereview
description: "Use when reviewing local code changes before they become a PR. Deterministic work lives in scripts/orchestrate.py; this skill is the thin wrapper that drives the flow."
---

# Code Review Skill

Use this skill to run a local review end to end. Do not reimplement the pipeline in the skill; delegate deterministic work to `scripts/orchestrate.py`.

## Quick Start

```text
/codereview                                                  # staged changes or HEAD~1
/codereview --base main                                      # entire feature branch
/codereview --range HEAD~5..HEAD                             # specific commits
/codereview src/auth/                                        # specific path
/codereview --spec docs/plan.md --base main                  # verify against spec
/codereview --spec docs/plan.md --spec-scope "Auth" --base main  # one section of spec
/codereview --base main --no-chunk                           # force standard mode on large diffs
/codereview 42                                               # PR #42
```

## When to Use / When NOT to Use

**Use** when you have local changes that would benefit from a structured review before they become a PR. Works on staged changes, commits, branches, ranges, paths, and open PRs.

**Do NOT use** for reviewing merged code, auditing entire repositories, or as a replacement for CI checks. This skill reviews diffs, not entire codebases.

## Execution

### Error handling (applies to all steps)

After each script phase, check the `status` field in the output JSON. If `"error"`: report the message to the user and stop. After each agent step, if the agent fails or returns no output, report the failure and offer to retry or skip.

### Step 1: Prepare

Create a session directory and run the orchestrator:

```bash
SESSION_DIR=$(mktemp -d /tmp/codereview-XXXXXXXX)
python3 scripts/orchestrate.py prepare --session-dir "$SESSION_DIR" [flags from user]
```

Read `$SESSION_DIR/launch.json`. Check the `status` field:
- `"empty"`: tell user "No changes found to review" and stop.
- `"error"`: report the error message and stop.
- `"ready"`: proceed.

If `mode` is `"chunked"`: tell user "Chunked review mode is not yet available. Proceeding in standard mode." and continue with the standard flow below.

### Step 2: Launch Explorers

Read the launch packet. For each wave in `waves[]`:

Launch ALL tasks in the wave in parallel (single message, multiple Agent tool calls). For each task:
- Read the assembled prompt from `task.assembled_prompt_file`
- Set `model` from `task.model` (if present; omit for default)
- Set `description` to `"Review explorer: <task.name>"`
- Set `run_in_background: true` for parallel execution

Wait for all tasks in this wave to complete before starting the next wave.

**Explorer failure handling:**
- If a task with `core: true` fails, retry it once. If it fails again, warn the user: "`<name>` analysis unavailable — review may miss issues in this area."
- If a non-core (activated) task fails, skip it and log a warning. Do not retry.
- If zero core explorers succeed after retries, warn the user prominently: "No core analysis completed. Consider retrying the full review." Continue only if the user confirms.

After each explorer completes, immediately:
1. Extract the JSON array or object from its response (find the `[...]` or `{...}`).
2. Write it to `task.output_file`.
3. Process one result at a time — do not accumulate all results in context.

If `post_wave_task` exists in the launch packet, launch it after all waves complete.

Relay progress: `[AI] Launching N explorers in parallel...`, `[AI] M/N complete...`, `[AI] All explorers complete.`

### Step 3: Post-Explorer Processing

```bash
python3 scripts/orchestrate.py post-explorers --session-dir "$SESSION_DIR"
```

Read `$SESSION_DIR/judge-input.json`. Check for errors.

### Step 4: Launch Judge

Launch a single sub-agent:
- Read the prompt from `judge_prompt_file` in the judge input JSON
- Set `model` from `judge_model`
- Set `description` to `"Review judge: synthesize findings"`

The judge needs Read, Grep, and Glob tools for adversarial verification — use a general-purpose sub-agent, not a limited one.

After the judge completes, extract JSON from its response and write it to `judge_output_file`.

### Step 5: Finalize

```bash
python3 scripts/orchestrate.py finalize --session-dir "$SESSION_DIR"
```

Read `$SESSION_DIR/finalize.json`. Present the report to the user:
- Show the verdict, tier summary, and `report_preview`
- Tell user where full artifacts are saved (`json_artifact`, `markdown_artifact` paths)

### Step 6: PR Comments (optional)

If in PR mode and the user asks, post findings as inline PR comments using `gh api`. Always ask before posting — never auto-post.

### Step 7: Cleanup

After the review is fully complete (including any PR comments):

```bash
rm -rf "$SESSION_DIR"
```

## Suppress a Finding

```bash
python3 scripts/lifecycle.py suppress \
  --review <latest review JSON path> \
  --finding-id <id> \
  --status rejected --reason "explanation" \
  --suppressions .codereview-suppressions.json
```

The suppress subcommand goes directly to `lifecycle.py`, not through `orchestrate.py`.

## What The Script Owns

- target detection, diff extraction, and config loading
- launch packet assembly and prompt shaping
- explorer/judge packet generation
- report rendering, artifact writing, and lifecycle state

## Configuration

Optional repo-level config via `.codereview.yaml`. See `docs/CONFIGURATION.md` for the full schema reference.

## Prompt Files

| File | Role |
|------|------|
| `prompts/reviewer-global-contract.md` | Shared contract prepended to all explorer prompts |
| `prompts/reviewer-correctness-pass.md` | Correctness explorer (core) |
| `prompts/reviewer-security-pass.md` | Security explorer (core) |
| `prompts/reviewer-test-adequacy-pass.md` | Test adequacy explorer (core) |
| `prompts/reviewer-reliability-performance-pass.md` | Reliability explorer (core) |
| `prompts/reviewer-error-handling-pass.md` | Error handling explorer (activated) |
| `prompts/reviewer-api-contract-pass.md` | API/contract explorer (activated) |
| `prompts/reviewer-concurrency-pass.md` | Concurrency explorer (activated) |
| `prompts/reviewer-spec-verification-pass.md` | Spec verification explorer (activated) |
| `prompts/reviewer-shell-script-pass.md` | Shell script explorer (activated) |
| `prompts/reviewer-security-config-pass.md` | Security config explorer (activated) |
| `prompts/reviewer-security-dataflow-pass.md` | Security dataflow explorer (activated) |
| `prompts/reviewer-judge.md` | Review judge (adversarial validation) |

If you need to understand the review internals, inspect `scripts/orchestrate.py` instead of expanding this skill.
