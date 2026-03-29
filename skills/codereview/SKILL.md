---
name: codereview
description: "Use when reviewing local code changes before they become a PR. Deterministic work lives in scripts/orchestrate.py; this skill is the thin wrapper that drives the flow."
---

# Code Review Skill

Use this skill to run a local review end to end. Do not reimplement the pipeline in the skill; delegate deterministic work to `scripts/orchestrate.py`.

## Quick Start

```text
/codereview                                                  # all uncommitted changes (staged + unstaged) or HEAD~1
/codereview --base main                                      # entire feature branch
/codereview --range HEAD~5..HEAD                             # specific commits
/codereview src/auth/                                        # specific path
/codereview --spec docs/plan.md --base main                  # verify against spec
/codereview --spec docs/plan.md --spec-scope "Auth" --base main  # one section of spec
/codereview --base main --no-chunk                           # force standard mode on large diffs
/codereview 42                                               # PR #42
/codereview --setup                                          # re-run dependency setup
```

## When to Use / When NOT to Use

**Use** when you have local changes that would benefit from a structured review before they become a PR. Works on uncommitted changes, commits, branches, ranges, paths, and open PRs.

**Do NOT use** for reviewing merged code, auditing entire repositories, or as a replacement for CI checks. This skill reviews diffs, not entire codebases.

## Execution

**Script location:** All scripts live in this skill's `scripts/` directory. Since bash blocks run in the user's current working directory (not the skill directory), you must use the full absolute path. The skill framework injects `Base directory for this skill: <path>` at the top of the skill context — extract that path and append `/scripts` to get the scripts directory. Set this variable at the start of every bash block, replacing `<SKILL_BASE>` with the actual base directory path from the injected header:

```
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
```

### Error handling (applies to all steps)

After each script phase, check the `status` field in the output JSON. If `"error"`: report the message to the user and stop. After each agent step, if the agent fails or returns no output, report the failure and offer to retry or skip.

### Step 0: Dependency Setup (first review only)

If `.agents/codereview/setup-complete` does NOT exist:

1. Check dependencies:

```bash
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
python3 "$SKILL_SCRIPTS/code_intel.py" setup --check --json
```

2. Parse the JSON output.
3. If `summary.missing_by_tier.full > 0`:
   Show the user the human-readable check output, then use `AskUserQuestion` to prompt:

   "I recommend installing the full dependency set for the best review quality. This includes semantic code search, AST security rules, and language-specific linters. One-time install, ~250MB. Install?"

   With options: `["yes", "skip"]`

   Do NOT proceed until the user responds. Do NOT auto-select.

   If user says yes:

```bash
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
python3 "$SKILL_SCRIPTS/code_intel.py" setup --install --tier full
```

   If skip: Note in report footer: "Some optional tools are missing.
            Run `code_intel.py setup --check` for details."

4. Write `.agents/codereview/setup-complete` with timestamp.
5. Proceed to Step 1.

If `.agents/codereview/setup-complete` EXISTS: skip to Step 1.

To re-run setup: `/codereview --setup` (deletes marker and re-runs Step 0)

### Step 0.5: Determine Review Scope (when no --base/--range/--pr/--path given)

If the user provided an explicit `--base`, `--range`, `--pr`, or path argument, skip this step — use their flags directly.

If NO scope flags were provided, check for the last-review marker:

```bash
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
cat .agents/codereview/last-review.json 2>/dev/null
```

**If the marker exists**, parse it and use `AskUserQuestion` to offer scope options:
- **"Since last review ({head_sha:.7} — {timestamp})"** (Recommended) — uses `--base {head_sha}` to review only changes since the last review
- **"All uncommitted changes"** — no `--base` flag (reviews all staged + unstaged changes against HEAD)
- **"Compared to main"** — uses `--base main`

Present the timestamp in relative terms when possible (e.g., "2 hours ago", "yesterday"). Include the file count and verdict from the last review for context: "Last review: {file_count} files, verdict {verdict}".

Do NOT proceed until the user responds. Use the selected option to set the appropriate `--base` flag for Step 1.

**If no marker exists** (first review ever), use `AskUserQuestion`:
- **"All uncommitted changes (staged + unstaged)"** (Recommended) — no `--base` flag
- **"Compared to main"** — uses `--base main`
- **"Specify a base ref"** — prompt for the ref

**Note:** The default mode (no `--base`) captures all uncommitted changes — both staged and unstaged — against HEAD. If there are no uncommitted changes, it falls back to reviewing the last commit (HEAD~1..HEAD).

### Step 1: Prepare

Create a session directory and run the orchestrator:

```bash
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
SESSION_DIR=$(mktemp -d /tmp/codereview-XXXXXXXX)
python3 "$SKILL_SCRIPTS/orchestrate.py" prepare --session-dir "$SESSION_DIR" [flags from user]
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
  - The assembled prompt includes prescan signals (when available) as a "Prescan Signals" section. Explorers should investigate flagged areas but prescan signals are not findings themselves.
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

If `post_wave_task` exists in the launch packet, wait until all waves complete, read `post_wave_task.assembled_prompt_file`, launch the task, wait for it to finish, capture its result, and write that result to `post_wave_task.output_file`. Only proceed to Step 3 after that artifact exists.

Relay progress: `[AI] Launching N explorers in parallel...`, `[AI] M/N complete...`, `[AI] All explorers complete.`

### Step 3: Post-Explorer Processing

```bash
python3 "$SKILL_SCRIPTS/orchestrate.py" post-explorers --session-dir "$SESSION_DIR"
```

Read `$SESSION_DIR/judge-input.json`. Check for errors.

### Step 4: Launch Judge

Launch a single sub-agent:
- Read the prompt from `judge_prompt_file` in the judge input JSON
- Set `model` from `judge_model`
- Set `description` to `"Review judge: synthesize findings"`

The judge needs Read, Grep, and Glob tools for adversarial verification — use a general-purpose sub-agent, not a limited one.

After the judge completes, write its full response to `judge_output_file` (the path from judge input JSON). The finalize step uses `extract_json_from_text()` which handles JSON embedded in markdown, fenced code blocks, and text with extra data after the JSON. Do NOT attempt to parse or extract JSON yourself — just write the raw response.

### Step 5: Finalize

```bash
python3 "$SKILL_SCRIPTS/orchestrate.py" finalize --session-dir "$SESSION_DIR"
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
SKILL_SCRIPTS="<SKILL_BASE>/scripts"
python3 "$SKILL_SCRIPTS/lifecycle.py" suppress \
  --review <latest review JSON path> \
  --finding-id <id> \
  --status rejected --reason "explanation" \
  --suppressions .codereview-suppressions.json
```

The suppress subcommand goes directly to `lifecycle.py`, not through `orchestrate.py`.

## What The Script Owns

- target detection, diff extraction, and config loading
- launch packet assembly and prompt shaping (includes prescan signals when available)
- explorer/judge packet generation
- code intelligence via `code_intel.py` (replaces `complexity.sh` for complexity analysis; provides functions/callers context and LLM-optimized diffs)
- report rendering, artifact writing, and lifecycle state

## Configuration

Optional repo-level config via `.codereview.yaml`. See `docs/CONFIGURATION.md` for the full schema reference.

## Prompt Files

| File | Role |
|------|------|
| `prompts/reviewer-global-contract.md` | Shared contract prepended to all explorer prompts |
| `prompts/reviewer-correctness-pass.md` | Correctness explorer (core) |
| `prompts/reviewer-security-config-pass.md` | Security config explorer (core) |
| `prompts/reviewer-test-adequacy-pass.md` | Test adequacy explorer (core) |
| `prompts/reviewer-security-dataflow-pass.md` | Security dataflow explorer (activated) |
| `prompts/reviewer-reliability-performance-pass.md` | Reliability explorer (activated; also reused for shell-script reviews) |
| `prompts/reviewer-error-handling-pass.md` | Error handling explorer (activated) |
| `prompts/reviewer-api-contract-pass.md` | API/contract explorer (activated) |
| `prompts/reviewer-concurrency-pass.md` | Concurrency explorer (activated) |
| `prompts/reviewer-spec-verification-pass.md` | Spec verification explorer (activated) |
| `prompts/reviewer-reliability-performance-pass.md` | Shell script explorer (activated; shared prompt) |
| `prompts/reviewer-judge.md` | Review judge (adversarial validation) |

If you need to understand the review internals, inspect `scripts/orchestrate.py` instead of expanding this skill.
