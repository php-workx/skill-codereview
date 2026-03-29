# Plan: Python Orchestrator

Replace the agent-interpreted SKILL.md workflow with a Python orchestration script that drives the entire review pipeline deterministically, invoking LLM agents only for judgment steps.

**Motivation:** Every production code review system we studied (CodeRabbit, Kodus-AI, PR-Agent, Claude Octopus) uses code-driven orchestration. Our current approach — a 500+ line SKILL.md that the agent interprets step by step — is the outlier. This causes:
- Instruction drift (agent skims later steps)
- Context pressure (workflow instructions compete with diff/context for tokens)
- Step skipping (conditional logic in natural language is fragile)
- Non-determinism (different runs interpret the same instructions differently)

**Design principle:** Scripts for everything deterministic. LLM for everything that requires judgment. Quality of findings is most important, then process strictness and speed.

**Provenance:** CodeRabbit gap analysis (Round 1: 30 findings, Round 2: 11 findings on our own codebase) confirmed that the same 5 structural detection root causes persist across fix cycles. A deterministic orchestrator is the structural fix.

---

## Architecture

The pipeline alternates between **script phases** (deterministic, driven by orchestrate.py) and **agent steps** (LLM judgment). orchestrate.py controls the flow — it decides what to run, prepares inputs, and processes outputs. The agent provides judgment at defined points.

New phases can be added freely. Each script phase takes JSON on stdin or via file, writes its output JSON to a known file path in the session directory, and writes progress to stderr. Each agent step reads a packet from the previous script phase, performs one LLM task, and writes output to a designated file.

```
User invokes /codereview --base main
    │
    ▼
SKILL.md (thin — ~80 lines, see "Thin SKILL.md" section)
    │
    ▼
┌─ SCRIPT ─────────────────────────────────────────────────────┐
│  orchestrate.py --prepare                                     │
│  Parse args, extract diff, mode selection, deterministic       │
│  context gathering, scans, expert panel assembly, prompt       │
│  construction.                                                 │
│  Output: launch packet JSON + assembled prompts                │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT (optional, when v1.3 F19 is implemented) ─────────────┐
│  Cross-file context planner: lightweight LLM call              │
│  Input: diff summary from launch packet                        │
│  Output: search queries JSON → file in session dir             │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ SCRIPT (optional, when F19 queries exist) ──────────────────┐
│  orchestrate.py --enrich-context                               │
│  Execute planner's grep queries, collect results.              │
│  Optionally: sufficiency check (VP F6) → agent LLM call       │
│  → second query round → script executes.                       │
│  Update assembled prompts with cross-file context.             │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT ──────────────────────────────────────────────────────┐
│  Launch explorer sub-agents (parallel)                         │
│  Each explorer gets a fully assembled prompt.                  │
│  Wait for all. Handle failures.                                │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ SCRIPT ─────────────────────────────────────────────────────┐
│  orchestrate.py --post-explorers                               │
│  Read explorer outputs, validate/repair, merge findings.       │
│  Output: judge input packet JSON + assembled judge prompt      │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT (optional, when VP F0 is implemented) ────────────────┐
│  VP F0 Stage 1: Feature extraction (batch LLM call)            │
│  Input: all findings from post-explorers                       │
│  Output: per-finding boolean features → file in session dir    │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ SCRIPT (optional, when VP F0 is implemented) ───────────────┐
│  orchestrate.py --triage                                       │
│  Stage 2: deterministic triage rules on extracted features.    │
│  Discard obvious false positives.                              │
│  Output: filtered findings for judge                           │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT (optional, when VP F0 Stage 3 is implemented) ────────┐
│  VP F0 Stage 3: Verification agent                             │
│  Per-finding deep verification with tool access.               │
│  Output: verification verdicts → file in session dir           │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT ──────────────────────────────────────────────────────┐
│  Launch judge sub-agent                                        │
│  Receives pre-assembled prompt with filtered/verified findings │
│  Output: judge verdict JSON → file in session dir              │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ SCRIPT ─────────────────────────────────────────────────────┐
│  orchestrate.py --finalize                                     │
│  Run enrich-findings.py, lifecycle.py, validate_output.sh      │
│  Assemble markdown report + JSON artifact                      │
│  Output: finalize result JSON (verdict, paths, preview)        │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌─ AGENT ──────────────────────────────────────────────────────┐
│  Present report to user. Optionally post PR comments.          │
└───────────────────────────────────────────────────────────────┘
```

### The alternating pattern

The pipeline is a sequence of `SCRIPT → AGENT → SCRIPT → AGENT → ...` steps. The key rules:

1. **Script phases are deterministic.** Same input → same output. No LLM calls. Testable with fixtures.
2. **Agent steps provide judgment.** Each agent step does exactly one LLM task: run explorers, run planner, run judge, etc.
3. **Each script phase prepares the next agent step.** It assembles the prompt, gathers context, and writes a packet the agent reads.
4. **Each agent step produces output for the next script phase.** It writes JSON to a designated file that the script reads.
5. **Optional steps are skipped cleanly.** If F19 isn't implemented, `--enrich-context` isn't called. If VP F0 isn't implemented, `--triage` isn't called. The thin SKILL.md has conditional logic only for "is this phase available?" — not for what happens inside each phase.
6. **New phases can be added without changing existing ones.** Adding VP F0 means adding 2-3 new steps in the middle. The `--prepare` and `--finalize` phases don't change.

### MVP vs full pipeline

**MVP (Phase 1 of migration):** Three steps only.

```
orchestrate.py --prepare → Agent: explorers → orchestrate.py --post-explorers → Agent: judge → orchestrate.py --finalize
```

**Full pipeline (Phase 3 of migration):** All optional steps enabled.

```
--prepare → Agent: planner → --enrich-context → Agent: explorers → --post-explorers → Agent: feature extraction → --triage → Agent: verification → Agent: judge → --finalize
```

The thin SKILL.md handles both by checking which phases are available (the launch packet declares what's next).

### Output contract: files, not stdout

Each script phase writes its output JSON to a **known file path** in the session directory. Phases produce **no stdout output** — this avoids fragility from stray print statements, library warnings, or Python deprecation notices corrupting a stdout JSON contract.

| Phase | Output file | Content |
|-------|-------------|---------|
| `--prepare` | `<session_dir>/launch.json` | Launch packet |
| `--post-explorers` | `<session_dir>/judge-input.json` | Judge input packet |
| `--finalize` | `<session_dir>/finalize.json` | Finalize result (verdict, artifact paths) |

**Exit codes:** 0 = success (output file written). 1 = error (error written to stderr). The agent checks the exit code, then reads the output file.

**How the agent controls session_dir:** The agent creates the session directory before calling `--prepare` and passes it via `--session-dir`. All subsequent phases use the same `--session-dir`:

```bash
SESSION_DIR=$(mktemp -d /tmp/codereview-XXXXXXXX)
python3 scripts/orchestrate.py prepare --session-dir "$SESSION_DIR" [flags]
python3 scripts/orchestrate.py post-explorers --session-dir "$SESSION_DIR"
python3 scripts/orchestrate.py finalize --session-dir "$SESSION_DIR"
```

Each phase derives its input from convention: `--post-explorers` reads `$SESSION_DIR/launch.json`, `--finalize` reads `$SESSION_DIR/launch.json` + `$SESSION_DIR/judge.json`. No `--launch-packet` argument — the session directory IS the shared state.

**If `--session-dir` is omitted:** orchestrate.py creates one internally via `tempfile.mkdtemp()` and prints the path to stderr (backward compat for CLI use). But the thin SKILL.md always provides it.

**Session dir path handling:** The session dir path is stored as-provided by the agent (no `Path.resolve()` applied). This avoids `/tmp` vs `/private/tmp` mismatches on macOS where `/tmp` is a symlink.

**Sub-script invocations** use `subprocess.run(capture_output=True, text=True, encoding='utf-8')`. Their stdout is captured into Python variables, never inherited. Their stderr is captured and logged to orchestrate.py's stderr on failure. For long-running scripts (run-scans.sh), stderr progress is captured but not streamed — the user sees progress only between phases, not during a single script's execution.

**Progress messages** go to stderr only, as structured JSONL (one JSON object per line).

### Phase extensibility

Adding a new phase is mechanical:
1. Add a new subcommand to orchestrate.py that reads from session_dir and writes to session_dir
2. Add a corresponding agent step in the thin SKILL.md (if LLM judgment needed)
3. The thin SKILL.md gains one more step in its linear sequence

This means v1.3 and VP features that require LLM calls during the pipeline (F19 cross-file planner, VP F0 verification, VP F6 sufficiency check) simply become new phase pairs in the alternating sequence. No architectural change needed — the pattern is already designed for extension.

### Path resolution

orchestrate.py resolves all paths relative to the **skill root directory** — the directory containing `SKILL.md`. It finds this by walking up from its own location (`__file__`):

```python
SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/
SKILL_ROOT = SCRIPT_DIR.parent                        # skills/codereview/
PROMPTS_DIR = SKILL_ROOT / "prompts"
REFERENCES_DIR = SKILL_ROOT / "references"
SCRIPTS_DIR = SKILL_ROOT / "scripts"
```

All paths in the launch packet are **absolute paths**. The agent or CLI consumer never needs to resolve relative paths.

### Session directory

Each invocation creates a session directory for all temp files:

```python
import tempfile
SESSION_DIR = Path(tempfile.mkdtemp(prefix="codereview-"))
```

All temp files (assembled prompts, explorer outputs, scan results, etc.) live under `SESSION_DIR`. The launch packet records `session_dir` so all phases can find artifacts.

**Cleanup is agent-initiated, not script-initiated.** The agent calls `rm -rf "$SESSION_DIR"` after the review is fully complete (including PR comments in Step 6). orchestrate.py does NOT register atexit cleanup — in a multi-invocation model (3 separate subprocess calls), atexit in `--prepare` would destroy the session dir before Phase 2 runs. A `--cleanup --session-dir <path>` subcommand is provided for explicit cleanup.

**Stale session cleanup:** On startup, `--prepare` scans `/tmp/codereview-*` directories and deletes any older than 2 hours. This handles sessions left behind by crashed reviews.

**Estimated temp disk usage:** ~5MB standard mode, ~30MB chunked mode (40 prompt files at 100-300KB each). CI runners with small tmpfs should set `TMPDIR` to persistent storage.

### Configuration loading

orchestrate.py loads `.codereview.yaml` from the git repository root (detected via `git rev-parse --show-toplevel`). If the file doesn't exist, all settings use defaults. CLI arguments override config file values. Precedence: CLI flag > `.codereview.yaml` > built-in default.

```python
def load_config(repo_root: Path, cli_args) -> dict:
    config = DEFAULT_CONFIG.copy()
    yaml_path = repo_root / ".codereview.yaml"
    if yaml_path.exists():
        with open(yaml_path) as f:
            import yaml  # optional dependency; fall back to basic parsing if absent
            file_config = yaml.safe_load(f) or {}
            config = deep_merge(config, file_config)
    # CLI overrides
    if cli_args.confidence_floor is not None:
        config["confidence_floor"] = cli_args.confidence_floor
    if cli_args.force_all_experts:
        config.setdefault("experts", {})["force_all"] = True
    return config
```

**PyYAML is a required dependency.** `.codereview.yaml` uses nested structures (`pass_models`, `large_diff`, `token_budget`, `experts`) that a flat key:value parser cannot handle. Without PyYAML, nested config values are silently lost — users set `pass_models.correctness: opus` and it's ignored with no warning. This is worse than failing clearly.

If PyYAML is not installed, orchestrate.py prints a clear error: `"PyYAML is required for .codereview.yaml support. Install: pip install pyyaml. To use defaults only, pass --no-config."` The `--no-config` flag skips config loading entirely (all defaults).

### Sub-script invocation: `run_subprocess_json()`

All sub-script calls (complexity.sh, git-risk.sh, discover-project.py, run-scans.sh, coverage-collect.py, enrich-findings.py, lifecycle.py) go through a single helper that handles errors uniformly:

```python
def run_subprocess_json(
    cmd: list[str],
    stdin_text: str | None = None,
    timeout: int = 120,
    required: bool = False,
    label: str | None = None,
) -> dict | list:
    """Run a subprocess and parse its stdout as JSON.

    Args:
        cmd: command + arguments
        stdin_text: text to pipe via subprocess input= param (e.g., changed files list).
                    Scripts that take file path arguments (enrich-findings.py, lifecycle.py)
                    do NOT use this — their paths are in the cmd list.
        timeout: seconds before killing the process (SIGTERM → SIGKILL after 5s)
        required: if True, raise on failure; if False, return {} and log warning
        label: human-readable name for progress messages

    Returns:
        Parsed JSON from stdout (dict or list). Returns {} on failure if not required.

    Subprocess contract:
        - capture_output=True, text=True, encoding='utf-8'
        - stdout: JSON output (captured, parsed)
        - stderr: progress/warnings (captured, logged to orchestrate.py stderr on failure)
        - stdin: from stdin_text via input= param, or None
    """
    label = label or Path(cmd[-1]).name if cmd else "unknown"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            input=stdin_text, timeout=timeout,
        )
    except FileNotFoundError:
        if required:
            raise
        progress(message=f"{label}: command not found — skipped")
        return {}
    except subprocess.TimeoutExpired:
        if required:
            raise
        progress(message=f"{label}: timed out after {timeout}s — skipped")
        return {}

    if result.returncode != 0:
        # Log stderr for debugging (first 500 chars)
        if result.stderr:
            progress(message=f"{label}: stderr: {result.stderr[:500]}")
        if required:
            raise SubprocessError(cmd, result.returncode, result.stderr[:500])
        progress(message=f"{label}: exit code {result.returncode} — skipped")
        return {}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        if required:
            raise
        progress(message=f"{label}: non-JSON output ({len(result.stdout)} chars) — skipped")
        return {}
```

**Script invocation table (required/optional, timeouts, stdin method):**

| Script | Required? | Timeout | Stdin | Failure recovery |
|--------|-----------|---------|-------|-----------------|
| `git diff` (diff extraction) | **Yes** | 30s | — | FATAL: abort `--prepare` |
| `discover-project.py` | No | 30s | changed files via `input=` | Proceed with empty profile |
| `complexity.sh` | No | 60s | changed files via `input=` | Proceed without complexity context |
| `git-risk.sh` | No | 60s | changed files via `input=` | Proceed without risk context |
| `run-scans.sh` | No | 300s | changed files via `input=` | Proceed without scan results (AI-only review) |
| `coverage-collect.py` | No | 120s | changed files via `input=` | Proceed without coverage data |
| `enrich-findings.py` | **Yes** | 60s | file path args | FATAL: abort `--finalize` |
| `lifecycle.py` | No | 60s | file path args | Proceed with unenriched findings |
| `validate_output.sh` | No | 30s | file path args | Save artifacts anyway; mark `validation_status: "skipped"` |

**Global timeout:** orchestrate.py accepts `--timeout <seconds>` (default: 1200 = 20 minutes). Each phase checks remaining time before starting a subprocess and reduces the subprocess timeout accordingly. If the global deadline is exceeded, the current phase writes partial results and exits with a clear error: `"Global timeout exceeded after {elapsed}s. Partial results may be available."`

---

## What moves to orchestrate.py vs what stays with the agent

### Deterministic (orchestrate.py)

| Current SKILL.md Step | Phase | Why deterministic |
|----------------------|-------|-------------------|
| Step 1: Determine review target | 1 | Argument parsing, git commands |
| Step 1.5: Mode selection & clustering | 1 | File counting, thresholds, directory grouping |
| Step 2a: Scope identification | 1 | Diff parsing, function/class extraction |
| Step 2a-1: Project discovery | 1 | Runs discover-project.py |
| Step 2d: Complexity analysis | 1 | Runs complexity.sh |
| Step 2e: Review instructions | 1 | File existence checks |
| Step 2f: Language standards | 1 | File detection and loading |
| Step 2g: Spec/plan loading | 1 | File reading |
| Step 2i: Git risk scoring | 1 | Runs git-risk.sh |
| Step 2j: Coverage collection | 1 | Runs coverage-collect.py |
| Step 3: Deterministic scans | 1 | Runs run-scans.sh |
| Step 3.5: Expert panel assembly | 1 | Diff content analysis, activation signals |
| Step 4 prompt construction | 1 | Template assembly |
| Step 5: Enrich/classify | 3 | Runs enrich-findings.py |
| Step 5c: Lifecycle | 3 | Runs lifecycle.py |
| Step 6: Report formatting | 3 | Template rendering (Python string formatting, follows `references/report-template.md`) |
| Step 7: Save artifacts | 3 | File writes |
| Timing instrumentation | 1,2,3 | Each phase records timing data; Phase 3 assembles `_timing` |

### LLM judgment (agent)

| Step | Why it needs LLM |
|------|-----------------|
| Explorer sub-agents (Step 4a) | Each explorer investigates with Read/Grep/Glob — core LLM judgment |
| Judge synthesis (Step 4b) | Adversarial validation, deduplication, verdict — core LLM judgment |
| Report presentation + PR comments | Natural language output, user interaction |

### Moved from agent to judge prompt (Step 5a dedup)

Currently Step 5a says "agent pre-processing: deduplicate by root cause, remove linter restatements." This semantic dedup moves into the **judge prompt** — the judge already does root-cause grouping and cross-source dedup (Calibrator expert, Steps 3b-3d in reviewer-judge.md). The thin SKILL.md has no agent dedup step. The judge is the single authority for semantic dedup; enrich-findings.py handles only mechanical classification.

### The gray area: context gathering (Step 2b)

Currently the agent uses Grep/Read/Glob to explore callers, callees, types, and related code.

With v1.3 F3-F5 (code_intel.py — **not yet implemented**), much of this becomes structural:
- `code_intel.py functions` → list of functions with signatures, line spans
- `code_intel.py callers --target X` → callers of function X
- `code_intel.py imports` → import graph
- `code_intel.py exports` → public API surface

**Phase 1 MVP (without code_intel.py):** orchestrate.py includes basic context: diff stats, file list, scan results, complexity, git risk. Explorers do their own semantic investigation via tools.

**Phase 2 (with code_intel.py):** orchestrate.py runs code_intel.py and includes structural context in assembled prompts. Explorers still investigate semantically but start with richer pre-computed context.

---

## Main Entry Point and Plumbing Layer

The main entry point is where argparse lives, where repo_root is determined, where phase functions are called, and where output files are written to disk. This is the connective tissue of the script.

```python
import argparse, json, os, subprocess, sys
from pathlib import Path
from datetime import datetime

# --- Constants ---

CORE_EXPERTS = {"correctness", "security", "test-adequacy"}

class SubprocessError(Exception):
    """Raised when a required subprocess fails."""
    def __init__(self, cmd, returncode, stderr):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{cmd[0]} exited {returncode}: {stderr[:200]}")

class PromptBudgetExceeded(Exception):
    """Raised when prompt exceeds token budget after all truncation attempts."""
    def __init__(self, tokens, budget):
        super().__init__(f"Prompt is {tokens} tokens, budget is {budget}. "
                         "Consider using chunked mode (--force-chunk).")

DEFAULT_CONFIG = {
    "confidence_floor": 0.65,
    "pass_models": {},
    "judge_model": "sonnet",
    "experts": {},
    "force_all_passes": False,
    "ignore_paths": [],
    "focus_paths": [],
    "custom_instructions": "",
    "large_diff": {
        "file_threshold": 80,
        "line_threshold": 8000,
        "max_chunk_files": 15,
        "max_chunk_lines": 2000,
        "max_parallel_explorers": 12,
    },
    "token_budget": {
        "explorer_prompt": 70000,
        "judge_prompt": 80000,
    },
}

# --- Helper functions ---

def detect_repo_root() -> Path:
    """Detect git repository root via git rev-parse."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("ERROR: Not in a git repository", file=sys.stderr)
        sys.exit(1)
    return Path(result.stdout.strip())

def expert_to_task(expert: dict) -> dict:
    """Convert an expert dict (from assemble_expert_panel) to a task dict (for waves).
    Adds the `core` field and drops `prompt_file` (tasks carry assembled_prompt_file only)."""
    return {
        "name": expert["name"],
        "model": expert["model"],
        "assembled_prompt_file": expert["assembled_prompt_file"],
        "output_file": expert["output_file"],
        "activation_reason": expert["activation_reason"],
        "core": expert["name"] in CORE_EXPERTS,
    }

def filter_config_allowlist(config: dict) -> dict:
    """Return only the config keys that Phase 2/3 need. No secrets."""
    ALLOWED = {"confidence_floor", "experts", "token_budget", "large_diff",
               "pushback_level", "judge_model", "pass_models"}
    return {k: v for k, v in config.items() if k in ALLOWED}

def build_launch_packet(*, session_dir, diff_result, review_mode, waves, judge,
                        scan_results, spec_file, config, chunks, timing) -> dict:
    """Assemble the launch packet from all Phase 1 outputs."""
    return {
        "status": "ready",
        "review_id": f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}",
        "session_dir": session_dir,
        "mode": review_mode,
        "scope": diff_result.scope,
        "base_ref": diff_result.base_ref,
        "head_ref": diff_result.head_ref,
        "pr_number": diff_result.pr_number,
        "changed_files": diff_result.changed_files,
        "file_count": diff_result.file_count,
        "diff_lines": diff_result.line_count,
        "waves": waves,
        "judge": judge,
        "scan_results_file": str(Path(session_dir) / "scans.json"),
        "tool_status": scan_results.get("tool_status", {}),
        "spec_file": spec_file,
        "context_summary": (f"{diff_result.file_count} files, {diff_result.line_count} lines, "
                           f"{sum(len(w['tasks']) for w in waves)} experts"),
        "_config": filter_config_allowlist(config),
        "chunks": chunks,
        "_timing": timing,
    }

def get_all_tasks(launch_packet: dict) -> list:
    """Flatten all tasks from all waves into a single list.
    This is the canonical way to iterate over explorer tasks."""
    return [task for wave in launch_packet["waves"] for task in wave["tasks"]]

# --- Main entry point ---

def main():
    parser = argparse.ArgumentParser(description="Code review pipeline orchestrator")
    sub = parser.add_subparsers(dest="phase", required=True)

    # Phase: prepare
    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("--session-dir", required=True, help="Session directory (created by agent via mktemp)")
    p_prep.add_argument("--base", help="Base branch for diff (branch mode)")
    p_prep.add_argument("--pr", type=int, help="PR number (PR mode)")
    p_prep.add_argument("--range", help="Commit range FROM..TO (range mode)")
    p_prep.add_argument("--path", help="Path to scope review to")
    p_prep.add_argument("--spec", help="Path to spec/plan file")
    p_prep.add_argument("--spec-scope", help="Section of spec to focus on")
    p_prep.add_argument("--no-chunk", action="store_true")
    p_prep.add_argument("--force-chunk", action="store_true")
    p_prep.add_argument("--force-all-experts", action="store_true")
    p_prep.add_argument("--confidence-floor", type=float)
    p_prep.add_argument("--no-config", action="store_true", help="Skip .codereview.yaml loading")
    p_prep.add_argument("--timeout", type=int, default=1200, help="Global timeout in seconds")

    # Phase: post-explorers
    p_post = sub.add_parser("post-explorers")
    p_post.add_argument("--session-dir", required=True)

    # Phase: finalize
    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("--session-dir", required=True)

    # Phase: cleanup
    p_clean = sub.add_parser("cleanup")
    p_clean.add_argument("--session-dir", required=True)

    args = parser.parse_args()
    session = Path(args.session_dir)

    try:
        if args.phase == "prepare":
            repo_root = detect_repo_root()
            config = load_config(repo_root, args) if not args.no_config else DEFAULT_CONFIG.copy()
            prepare(args, config, session, repo_root)
            # prepare() writes launch.json to session dir internally

        elif args.phase == "post-explorers":
            launch_packet = json.loads((session / "launch.json").read_text())
            result = post_explorers(launch_packet, session)
            (session / "judge-input.json").write_text(json.dumps(result, indent=2))

        elif args.phase == "finalize":
            launch_packet = json.loads((session / "launch.json").read_text())
            judge_output = extract_json_from_text((session / "judge.json").read_text())
            result = finalize(launch_packet, judge_output, session)
            (session / "finalize.json").write_text(json.dumps(result, indent=2))

        elif args.phase == "cleanup":
            import shutil
            if session.exists():
                shutil.rmtree(session, ignore_errors=True)

    except (SubprocessError, PromptBudgetExceeded) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        # Write error status to session dir if it exists
        if session.exists():
            error = {"status": "error", "message": str(e)}
            (session / f"{args.phase}-error.json").write_text(json.dumps(error))
        sys.exit(1)

if __name__ == "__main__":
    main()
```

**Key plumbing decisions visible in main():**
- **repo_root** is detected once in main() via `git rev-parse --show-toplevel` and passed to prepare()
- **Output files** are written by main(), not by the phase functions. Phase functions return dicts; main() writes them. Exception: prepare() writes launch.json internally because it has the full packet.
- **Error handling**: SubprocessError and PromptBudgetExceeded are caught, logged to stderr, and written to session dir.
- **argparse** uses subcommands (prepare, post-explorers, finalize, cleanup) not --flags.

**Note on CLI syntax:** argparse subcommands use positional names (prepare, not --prepare). All CLI examples in the plan should use: `python3 scripts/orchestrate.py prepare --session-dir ...` not `python3 scripts/orchestrate.py --prepare --session-dir ...`.

---

## Phase 1: `prepare`

### Input

```bash
python3 scripts/orchestrate.py prepare \
  --session-dir "$SESSION_DIR" \
  --base main \
  [--range abc..def] \
  [--pr 42] \
  [--path src/auth/] \
  [--spec docs/plan.md] \
  [--spec-scope "Authentication"] \
  [--no-chunk] \
  [--force-chunk] \
  [--force-all-experts] \
  [--confidence-floor 0.65] \
  [--no-config] \
  [--timeout 1200]
```

**Output:** `<session_dir>/launch.json` (see "Launch Packet Schema" section). No stdout output.

**Exit codes:** 0 = success (launch.json written), 1 = error (message to stderr). Empty diff: launch.json written with `{"status": "empty"}`.

### Diff extraction: `extract_diff()`

This is the most complex function in the script — it handles 6 review modes with different git commands and edge cases.

```python
@dataclass
class DiffResult:
    diff: str               # full unified diff text (empty string if no changes)
    changed_files: list     # list of changed file paths (from --name-only)
    file_count: int         # len(changed_files)
    line_count: int         # total lines in diff
    scope: str              # "branch" | "range" | "staged" | "commit" | "pr" | "path"
    base_ref: str           # git ref for base (e.g., merge-base hash, HEAD~1)
    head_ref: str           # git ref for head (e.g., HEAD, branch name)
    pr_number: int | None   # PR number if scope=pr, else None
```

**Per-mode git commands and error handling:**

| Mode | Trigger | Git commands | Error handling |
|------|---------|-------------|----------------|
| **branch** | `--base <branch>` | `MERGE_BASE=$(git merge-base <branch> HEAD)`; `git diff $MERGE_BASE..HEAD` | merge-base fails (branch doesn't exist): abort with "Branch '<branch>' not found." Same branch as HEAD: abort with "Already on <branch>, nothing to diff." |
| **pr** | positional digit arg | `gh pr diff <number>` (diff); `gh pr view <number> --json files,title,body` (metadata) | gh not installed: abort with "gh CLI required for PR mode." PR not found / auth failure: abort with gh's error message. |
| **range** | `--range <from>..<to>` | `git diff <from>..<to>` | Invalid refs: abort with git's error message. |
| **staged** | no args, staged changes exist | `git diff --cached` | No staged changes: fall through to commit mode. |
| **commit** | no args, nothing staged | `git diff HEAD~1` | Initial commit (no HEAD~1): use `git diff --cached HEAD` (diff of first commit). Merge commit: use `git diff HEAD~1..HEAD` (first parent only). |
| **path** | positional path arg | `git diff HEAD -- <path>` | Path doesn't exist: abort with "Path '<path>' not found." |

**Common post-processing (all modes):**
- **Changed files:** `git diff <same args> --name-only` to get the file list.
- **Binary files:** Filter out lines matching `^Binary files .* differ$` from the diff. Binary files are listed in `changed_files` but their diff content is omitted.
- **Renamed files:** Use `--find-renames` for accurate rename detection in `--name-only`.
- **Max diff size:** If `len(diff) > 5_000_000` chars (~1.25M tokens), abort with: "Diff is very large ({size}MB). Use --base with a closer branch, or scope to specific paths."
- **Encoding:** Use `subprocess.run(..., encoding='utf-8', errors='replace')`. If replacement characters appear, log warning: "Non-UTF8 content detected in diff — replacement characters used."

**All git subprocess calls** use `run_subprocess_json()` for error handling (non-zero exit → clear error message). `git diff` returns text, not JSON, so use a `run_subprocess_text()` variant that returns `result.stdout` as a string instead of parsing JSON.

### Processing steps

```python
def prepare(args, config, session: Path, repo_root: Path):

    # 0. Stale session cleanup: delete /tmp/codereview-session-* dirs older than 2 hours
    #    Only scans dirs matching codereview-session-* AND containing launch.json
    #    to avoid deleting unrelated user dirs.
    cleanup_stale_sessions(prefix="codereview-session-", max_age_hours=2)

    # 0b. Validate prompt files exist before doing any work
    validate_prompt_files()  # checks global contract, all pass prompts, judge prompt

    # 1. Determine review target
    diff_result = extract_diff(args)  # see extract_diff() section above
    if not diff_result.diff:
        empty = {"status": "empty", "message": "No changes found to review"}
        (session / "launch.json").write_text(json.dumps(empty, indent=2))
        return
    progress(step=1, total=8, message="Diff extracted",
             detail=f"{diff_result.file_count} files, {diff_result.line_count} lines")

    # Write diff to session dir (not stdout — captured from git subprocess)
    diff_path = session / "diff.patch"
    diff_path.write_text(diff_result.diff)

    # Write changed files list (consumed by sub-scripts via stdin)
    changed_files_path = session / "changed-files.txt"
    changed_files_path.write_text("\n".join(diff_result.changed_files))

    # 2. Mode selection (current Step 1.5)
    mode = select_mode(diff_result, config, args)
    chunks = None
    waves = None
    if mode == "chunked":
        manifest = build_manifest(diff_result, config)
        chunks = cluster_files(manifest, diff_result, config)
        waves = plan_waves(chunks, config)
        progress(step=2, total=8, message="Chunked mode",
                 detail=f"{len(chunks)} chunks, {len(waves)} waves")
    else:
        progress(step=2, total=8, message="Standard mode")

    # 3. Context gathering — deterministic parts (parallelized)
    #    Independent scripts run concurrently via ThreadPoolExecutor.
    #    run-scans.sh is the slowest (30-300s); running others in parallel
    #    hides their latency behind it.
    changed_files_text = "\n".join(diff_result.changed_files)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    context_tasks = {
        "discover":   lambda: run_subprocess_json(
            ["python3", str(SCRIPTS_DIR / "discover-project.py")],
            stdin_text=changed_files_text, timeout=30, label="discover-project"),
        "complexity": lambda: run_subprocess_json(
            ["bash", str(SCRIPTS_DIR / "complexity.sh")],
            stdin_text=changed_files_text, timeout=60, label="complexity"),
        "git_risk":   lambda: run_subprocess_json(
            ["bash", str(SCRIPTS_DIR / "git-risk.sh")],
            stdin_text=changed_files_text, timeout=60, label="git-risk"),
        "scans":      lambda: run_subprocess_json(
            ["bash", str(SCRIPTS_DIR / "run-scans.sh"), "--base-ref", diff_result.base_ref],
            stdin_text=changed_files_text, timeout=300, label="run-scans"),
        "coverage":   lambda: run_subprocess_json(
            ["python3", str(SCRIPTS_DIR / "coverage-collect.py")],
            stdin_text=changed_files_text, timeout=120, label="coverage-collect"),
    }
    context_results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in context_tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                context_results[name] = future.result()
            except Exception as e:
                progress(message=f"{name}: failed ({e}) — skipped")
                context_results[name] = {}

    project_profile = context_results["discover"]
    complexity = context_results["complexity"]
    git_risk = context_results["git_risk"]
    scan_results = context_results["scans"]
    coverage = context_results.get("coverage", {})
    scan_results_path = session / "scans.json"
    scan_results_path.write_text(json.dumps(scan_results))

    review_instructions = load_review_instructions(repo_root)
    language_standards = load_language_standards(diff_result.changed_files)
    spec_content = load_spec(args.spec) if args.spec else None

    progress(step=3, total=8, message="Context gathered",
             detail=f"scans: {len(scan_results.get('findings', []))} findings")

    # 4. Structural context (v1.3 F3-F5, when available)
    #    Phase 1: check_code_intel_available() returns False (stub).
    #    Phase 2: checks for SCRIPTS_DIR / 'code_intel.py' and required grammars.
    structural_context = None
    if check_code_intel_available():
        structural_context = run_code_intel(diff_result.changed_files)
        progress(step=4, total=8, message="Structural context (code_intel.py)")
    else:
        progress(step=4, total=8, message="Structural context skipped (code_intel.py not available)")

    # 5. Expert panel assembly (see "Expert panel assembly rules" section below)
    experts = assemble_expert_panel(diff_result, config, spec_content)
    progress(step=5, total=8, message="Expert panel",
             detail=", ".join(e["name"] for e in experts))

    # 6. Build per-explorer assembled prompts (MUST run before waves construction)
    #    This mutates each expert dict to add assembled_prompt_file and output_file.
    for expert in experts:
        assembled = assemble_explorer_prompt(
            expert_name=expert["name"],
            prompt_file=PROMPTS_DIR / expert["prompt_file"],
            global_contract=PROMPTS_DIR / "reviewer-global-contract.md",
            diff=diff_result.diff,
            changed_files=diff_result.changed_files,
            scan_results=scan_results,
            complexity=complexity,
            git_risk=git_risk,
            coverage=coverage,
            structural_context=structural_context,
            review_instructions=review_instructions,
            language_standards=language_standards,
            spec_content=spec_content,
            spec_scope=args.spec_scope,
            chunk=expert.get("_chunk"),
        )
        prompt_path = session / f"explorer-{expert['name']}-prompt.md"
        prompt_path.write_text(assembled)
        expert["assembled_prompt_file"] = str(prompt_path)

        output_path = session / f"explorer-{expert['name']}.json"
        expert["output_file"] = str(output_path)

    progress(step=6, total=8, message="Explorer prompts assembled",
             detail=f"{len(experts)} prompts")

    # 6b. Build waves AFTER prompt assembly (experts now have assembled_prompt_file + output_file)
    if mode != "chunked":
        waves = [{"wave": 1, "tasks": [expert_to_task(e) for e in experts]}]

    # 7. Build judge prompt template path (assembled in Phase 2 after explorers)
    judge_config = {
        "prompt_file": str(PROMPTS_DIR / "reviewer-judge.md"),
        "model": config.get("judge_model", config.get("pass_models", {}).get("judge", "sonnet")),
        "output_file": str(session / "judge.json"),
    }

    # 8. Timing
    timing = record_phase_timing("prepare", phase_start)
    progress(step=8, total=8, message="Launch packet ready")

    # Build launch packet and write to session dir
    #  Note: experts is NOT passed — experts are already represented as tasks inside waves.
    #  Chunks metadata (if any) is passed for the report, not for execution.
    packet = build_launch_packet(
        session_dir=str(session),
        diff_result=diff_result,
        review_mode=review_mode,
        waves=waves,
        judge=judge_config,
        scan_results=scan_results,  # full dict — build_launch_packet extracts tool_status
        spec_file=args.spec,
        config=config,
        chunks=chunks,
        timing=timing,
    )
    (session / "launch.json").write_text(json.dumps(packet, indent=2))
```

### Key design decision: assembled prompts

orchestrate.py constructs the **complete prompt** for each explorer — a single, self-contained markdown file that the agent sends verbatim. This eliminates the agent's role in prompt construction entirely.

```markdown
# Assembled prompt: /tmp/codereview-XXXXXXXX/explorer-correctness-prompt.md
# (generated by orchestrate.py — send verbatim to the LLM)

## Global Contract
<content of prompts/reviewer-global-contract.md>

## Your Focus: Correctness
<content of prompts/reviewer-correctness-pass.md>

## Diff to Review
<full diff, or chunk-specific diff in chunked mode>

## Context

### Changed Files
<file list with change stats: path | +lines/-lines | language>

### Complexity Hotspots
<from complexity.sh — only functions rated C or worse>

### Git Risk Scores
<from git-risk.sh — per-file risk tiers>

### Callers and Callees
<from code_intel.py if available, otherwise: "Use Grep/Read to investigate callers">

### Deterministic Scan Results (already reported — do not restate)
<summary of findings from run-scans.sh>

### Language Standards
<loaded standards for detected languages, if available>

### Review Instructions
<from .codereview.yaml custom_instructions, .github/codereview.md, REVIEW.md>

## Spec/Plan
<spec content if --spec provided, or "No spec provided">

You are an explorer sub-agent. Investigate thoroughly using Grep, Glob, and Read
to trace code paths and verify your findings. Return ALL findings as a JSON array
per the global contract schema.
```

**Explorer output format:** Explorers return a JSON array of findings: `[{...}, {...}]`. Exception: the spec-verification explorer returns `{"requirements": [...], "findings": [...]}`. Phase 2 handles both formats via `parse_explorer_output()` which normalizes to a flat findings list (preserving requirements separately for the judge).

### Token budget and progressive truncation

Assembled prompts can range from 24k to 68k tokens depending on diff size and context richness. On Sonnet (200k context), this fits — but explorers are agents with tool access, and each tool call consumes additional context. An explorer making 15 tool calls can burn 30-50k tokens on tool results alone.

**`estimate_tokens()` step:** After assembling each prompt, orchestrate.py estimates the token count (~1 token per 4 characters). If any assembled prompt exceeds the **prompt budget** (configurable, default: 70k tokens — leaving 130k for tools, working memory, and output), apply **progressive truncation** in this order:

1. Summarize scan results to counts-only (save ~2-5k tokens)
2. Drop language standards section (save ~2-5k tokens)
3. Summarize git risk as tier-only, no per-file detail (save ~1-3k tokens)
4. Truncate diff to changed hunks only, omit unchanged context lines (save ~20-40%)
5. If still over budget after all truncations: switch to chunked mode (split diff into smaller pieces)

**Prompt assembly data model:** `assemble_explorer_prompt()` returns a `PromptContext` dataclass, not a raw string. This allows `check_token_budget()` to truncate individual sections and re-render:

```python
@dataclass
class PromptContext:
    """All sections of an assembled explorer prompt. Each section is a string.
    render() concatenates them with markdown headers."""
    global_contract: str
    pass_prompt: str
    diff: str
    changed_files: str
    complexity: str
    git_risk: str
    scan_results: str
    callers: str
    language_standards: str
    review_instructions: str
    spec: str

    def render(self) -> str:
        sections = [
            ("## Global Contract", self.global_contract),
            ("## Your Focus", self.pass_prompt),
            ("## Diff to Review", self.diff),
            ("## Context\n### Changed Files", self.changed_files),
            ("### Complexity Hotspots", self.complexity),
            ("### Git Risk Scores", self.git_risk),
            ("### Callers and Callees", self.callers),
            ("### Deterministic Scan Results (already reported — do not restate)", self.scan_results),
            ("### Language Standards", self.language_standards),
            ("### Review Instructions", self.review_instructions),
            ("## Spec/Plan", self.spec),
        ]
        return "\n\n".join(f"{header}\n{content}" for header, content in sections if content)

    def estimate_tokens(self) -> int:
        return len(self.render()) // 4  # ~1 token per 4 chars


PROMPT_BUDGET_TOKENS = 70_000  # configurable via .codereview.yaml

def check_token_budget(ctx: PromptContext, expert_name: str) -> str:
    if ctx.estimate_tokens() <= PROMPT_BUDGET_TOKENS:
        return ctx.render()

    progress(message=f"Explorer {expert_name} prompt exceeds budget "
             f"({ctx.estimate_tokens()}t > {PROMPT_BUDGET_TOKENS}t), truncating")

    # Progressive truncation cascade — each step mutates PromptContext and re-checks
    truncations = [
        ("scan_results", summarize_scans_counts_only),
        ("language_standards", lambda _: ""),
        ("git_risk", summarize_git_risk_tiers_only),
        ("diff", truncate_to_changed_hunks_only),
    ]
    for field_name, truncator in truncations:
        setattr(ctx, field_name, truncator(getattr(ctx, field_name)))
        if ctx.estimate_tokens() <= PROMPT_BUDGET_TOKENS:
            return ctx.render()

    raise PromptBudgetExceeded(ctx.estimate_tokens(), PROMPT_BUDGET_TOKENS)
```

The chunking threshold (currently 80 files / 8000 lines) remains as a fast heuristic. Token budget is the safety net that catches cases where a small number of large files produce huge diffs.

**Configurable:**
```yaml
token_budget:
  explorer_prompt: 70000  # max tokens for assembled explorer prompt
  judge_prompt: 80000     # max tokens for assembled judge prompt
```

### Explorer output: who writes it to disk

Explorer sub-agents return their output as text to the parent agent. **The parent agent is responsible for writing this text to the output_file path.** This is explicit in the thin SKILL.md (see below) and works because:

1. orchestrate.py specifies the output_file path in the launch packet
2. The thin SKILL.md instructs the agent to extract JSON from each explorer's returned text
3. The agent writes the extracted JSON to the designated path
4. Phase 2 reads from these paths

**JSON extraction from LLM output:** Explorer sub-agents may return their JSON wrapped in preamble text, markdown code blocks, or trailing explanation. The thin SKILL.md instructs the agent to extract the JSON. As a safety net, Phase 2's `parse_explorer_output()` also includes a `extract_json_from_text()` utility:

```python
def extract_json_from_text(text: str) -> Any:
    """Extract JSON from LLM output that may include surrounding text.

    Fallback chain:
    1. Clean text (smart quotes, trailing commas)
    2. Try json.loads(text) directly
    3. Extract from ```json ... ``` markdown blocks
    4. Use balanced-bracket extraction to find JSON starting at first [ or {
    """
    # 0. Preprocessing: replace smart quotes, strip trailing commas
    cleaned = text.replace('\u201c', '"').replace('\u201d', '"')
    cleaned = cleaned.replace('\u2018', "'").replace('\u2019', "'")
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)  # trailing commas

    # 1. Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block
    match = re.search(r'```(?:json)?\s*\n(.*?)\n```', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Use balanced-bracket extraction — walks through each [ or { start position,
    #    extracts a balanced candidate string, and attempts json.loads().
    #    Recovers partial JSON (e.g. truncated arrays) by trying each start position.
    starts = [i for i, ch in enumerate(cleaned) if ch in '[{']
    for start in starts:
        candidate = _extract_balanced_json_candidate(cleaned, start)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not extract JSON from text ({len(text)} chars)")
```

**Dedicated tests for `extract_json_from_text()` (add to test-orchestrate.py):**

| Test | Input | Expected |
|------|-------|----------|
| `test_extract_direct` | `[{"a": 1}]` | Parsed array |
| `test_extract_markdown_fenced` | `` ```json\n[{"a": 1}]\n``` `` | Parsed array |
| `test_extract_with_preamble` | `"Here are findings:\n[{"a": 1}]"` | Parsed array |
| `test_extract_smart_quotes` | `[{\u201csummary\u201d: \u201ctest\u201d}]` | Parsed array |
| `test_extract_trailing_comma` | `[{"a": 1},]` | Parsed array |
| `test_extract_braces_in_strings` | `[{"msg": "use {x} here"}]` | Parsed array (balanced-bracket extraction handles this) |
| `test_extract_nested_arrays` | `[{"tests": ["a", "b"]}]` | Parsed array |
| `test_extract_truncated` | `[{"a": 1}, {"b":` | Recovers partial JSON (`{"a": 1}`) via balanced-bracket extraction |
| `test_extract_empty` | `""` | Raises ValueError |
| `test_extract_trailing_text` | `[{"a": 1}]\nI found 1 issue.` | Parsed array |

---

## Phase 2: `--post-explorers`

### Input

```bash
python3 scripts/orchestrate.py post-explorers --session-dir "$SESSION_DIR"
```

**Output:** `<session_dir>/judge-input.json`. No stdout output.

### Processing steps

```python
def post_explorers(launch_packet):
    session = Path(launch_packet["session_dir"])

    # 1. Read and validate all explorer outputs
    all_findings = []
    explorer_status = {}
    spec_requirements = []

    for task in get_all_tasks(launch_packet):  # flattens waves[].tasks[] into single list
        output_path = Path(task["output_file"])
        name = task["name"]

        # Handle explorer failures gracefully
        if not output_path.exists():
            explorer_status[name] = {"status": "missing", "findings": 0}
            progress(message=f"Explorer {name}: output file missing — skipped")
            continue

        try:
            raw = extract_json_from_text(output_path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            explorer_status[name] = {"status": "invalid_json", "findings": 0, "error": str(e)}
            progress(message=f"Explorer {name}: invalid JSON output — skipped")
            continue

        # Normalize output format (handles list, dict with findings/requirements, wrong shape)
        findings, reqs = parse_explorer_output(raw, name)
        if findings is None:
            # Wrong shape: valid JSON but not list or dict (e.g., string, number, null)
            explorer_status[name] = {
                "status": "wrong_shape", "findings": 0,
                "error": f"Expected list or dict, got {type(raw).__name__}: {str(raw)[:200]}",
            }
            progress(message=f"Explorer {name}: wrong output shape ({type(raw).__name__}) — skipped")
            continue
        all_findings.extend(findings)
        spec_requirements.extend(reqs)
        explorer_status[name] = {"status": "ok", "findings": len(findings)}

    raw_count = len(all_findings)
    progress(message=f"Collected {raw_count} findings from "
             f"{sum(1 for s in explorer_status.values() if s['status'] == 'ok')} explorers"
             f" ({sum(1 for s in explorer_status.values() if s['status'] != 'ok')} failed)")

    # 2. Deterministic pre-filter (reduces judge cognitive load)
    #    The judge still does semantic dedup (root cause grouping, cross-explorer
    #    synthesis). This step removes only mechanical duplicates and low-confidence
    #    noise so the judge starts with a cleaner input.
    all_findings = dedup_exact(all_findings)  # see dedup_exact() definition below

    # Confidence pre-filter: use a LOWER floor than the final enrichment floor.
    # The judge's Calibrator may upgrade low-confidence findings based on cross-explorer
    # corroboration. Using the full floor here permanently kills findings the judge
    # might have saved. Using floor - 0.15 gives the judge a recalibration window.
    # enrich-findings.py applies the real floor after the judge.
    config_floor = launch_packet.get("_config", {}).get("confidence_floor", 0.65)
    pre_filter_floor = max(config_floor - 0.15, 0.40)  # never below 0.40
    all_findings = [f for f in all_findings
                    if f.get("confidence", 1.0) >= pre_filter_floor]

    # Findings cap: if still > 50 findings after dedup, warn.
    # The judge can handle ~30 findings well; 30-50 is degraded; 50+ is unreliable.
    JUDGE_FINDINGS_CAP = 50
    if len(all_findings) > JUDGE_FINDINGS_CAP:
        progress(message=f"WARNING: {len(all_findings)} findings exceed judge cap "
                 f"({JUDGE_FINDINGS_CAP}). Pre-filtering by confidence.")
        all_findings.sort(key=lambda f: f.get("confidence", 0), reverse=True)
        all_findings = all_findings[:JUDGE_FINDINGS_CAP]

    progress(message=f"After pre-filter: {len(all_findings)} findings "
             f"(was {raw_count} raw)")

    # 3. Assemble judge prompt
    judge_prompt = assemble_judge_prompt(
        judge_prompt_file=Path(launch_packet["judge"]["prompt_file"]),
        explorer_findings=all_findings,
        spec_requirements=spec_requirements,
        scan_results_file=Path(launch_packet["scan_results_file"]),
        spec_file=launch_packet.get("spec_file"),
        context_summary=launch_packet.get("context_summary", ""),
    )
    judge_prompt_path = session / "judge-prompt.md"
    judge_prompt_path.write_text(judge_prompt)

    return {
        "status": "ready_for_judge",
        "explorer_finding_count": len(all_findings),
        "explorer_status": explorer_status,
        "judge_prompt_file": str(judge_prompt_path),
        "judge_output_file": launch_packet["judge"]["output_file"],
        "judge_model": launch_packet["judge"]["model"],
    }


def parse_explorer_output(raw, explorer_name: str):
    """Normalize explorer output to (findings_list, requirements_list).

    Returns:
        (findings, requirements) on success.
        (None, None) if raw is an unexpected type (string, number, null).

    Handles three formats:
    - Standard: JSON array of findings → (findings, [])
    - Spec verification: {"requirements": [...], "findings": [...]} → (findings, requirements)
    - Wrong shape: anything else → (None, None)
    """
    requirements = []
    if isinstance(raw, list):
        findings = raw
    elif isinstance(raw, dict):
        findings = raw.get("findings", [])
        requirements = raw.get("requirements", [])
    else:
        return None, None  # wrong shape: string, number, null, etc.

    # Tag each finding with its source explorer
    for f in findings:
        f["_explorer"] = explorer_name

    return findings, requirements


def dedup_exact(findings: list) -> list:
    """Remove exact duplicates: same (file, line, pass, severity) tuple.

    When duplicates exist (e.g., two explorers flagged the same file:line with the
    same pass and severity), keep the one with the higher confidence score.
    Two findings at the same location with different pass values (e.g., correctness
    and security) are NOT duplicates — they represent different concerns.
    """
    seen = {}  # key: (file, line, pass, severity) → finding with highest confidence
    for f in findings:
        key = (f.get("file"), f.get("line"), f.get("pass"), f.get("severity"))
        existing = seen.get(key)
        if existing is None or f.get("confidence", 0) > existing.get("confidence", 0):
            seen[key] = f
    return list(seen.values())
```

### Judge assembled prompt template

Like the explorer template, the judge gets a **complete, self-contained prompt** assembled by orchestrate.py. The judge is an agent with tool access (Read/Grep/Glob for the Verifier's existence checks).

```markdown
# Assembled prompt: <session_dir>/judge-prompt.md
# (generated by orchestrate.py --post-explorers)

## Judge Contract
<content of prompts/reviewer-judge.md>

## Explorer Findings
### correctness (5 findings)
<JSON array from correctness explorer>
### security (3 findings)
<JSON array from security explorer>
### shell-script (2 findings)
<JSON array from shell-script explorer>
### test-adequacy (4 findings)
<JSON array from test-adequacy explorer>

Total: 14 findings from 4 explorers.
<explorer_status summary: which explorers ran, which failed>

## Spec Requirements (if spec-verification explorer ran)
<requirements JSON array, or omitted if no spec>

## Deterministic Scan Results
<scan findings JSON from run-scans.sh — needed for Gatekeeper "duplicate of deterministic" check>

## Diff Summary
<changed file list with per-file stats: path | +lines/-lines — NOT the full diff>
<The judge uses Read/Grep tools to examine actual code when needed by the Verifier>

## Spec/Plan (if --spec was provided)
<spec content>

## Context Summary
<one-line summary: "32 files, 7.5k lines, 5 experts, 3 scan findings">
```

**Key design decisions for the judge prompt:**

- **Findings grouped by explorer** with per-explorer headers. This helps the Calibrator (Expert 3) with cross-explorer synthesis — it can see which explorers flagged the same area.
- **Scan results included inline.** The Gatekeeper (Expert 1) needs them for the "duplicate of deterministic" auto-discard rule. These are typically 1-5k tokens.
- **Diff summary, NOT full diff.** The judge doesn't need to re-read the entire diff — it has the file list for "outside diff scope" checks. The Verifier uses Read/Grep tools to check actual code at cited file:line locations.
- **Spec content included** when `--spec` was provided, for the Synthesizer's spec compliance check.
- **Token budget:** Judge prompt is typically 7k (contract) + 10-30k (findings) + 3-5k (scan results) + 1-2k (diff summary) + 0-10k (spec) = 21-54k tokens. The judge then uses tool calls (each 2-5k tokens) for Verifier checks. Configurable via `token_budget.judge_prompt` (default: 80k).

---

## Phase 3: `--finalize`

### Input

```bash
python3 scripts/orchestrate.py finalize --session-dir "$SESSION_DIR"
```

**Output:** `<session_dir>/finalize.json` + `.agents/reviews/` artifacts. No stdout output.

### Processing steps

```python
def finalize(launch_packet, judge_output_path):
    session = Path(launch_packet["session_dir"])
    config = launch_packet["_config"]

    # 1. Run enrich-findings.py (subprocess, capture stdout)
    enriched_json = run_subprocess_json(
        ["python3", str(SCRIPTS_DIR / "enrich-findings.py"),
         "--judge-findings", str(judge_output_path),
         "--scan-findings", launch_packet["scan_results_file"],
         "--confidence-floor", str(config.get("confidence_floor", 0.65))],
    )
    progress(message=f"Enriched: {enriched_json.get('tier_summary', {})}")

    # Write enriched to temp file for lifecycle input
    enriched_path = session / "enriched.json"
    enriched_path.write_text(json.dumps(enriched_json))

    # 2. Write changed files list for lifecycle deferred-scope logic
    changed_files_path = session / "changed-files.txt"
    # (already written in Phase 1, but verify it exists)

    # 3. Run lifecycle.py (subprocess, capture stdout)
    lifecycle_json = run_subprocess_json(
        ["python3", str(SCRIPTS_DIR / "lifecycle.py"),
         "--findings", str(enriched_path),
         "--suppressions", ".codereview-suppressions.json",
         "--changed-files", str(changed_files_path),
         "--scope", launch_packet["scope"],
         "--base-ref", launch_packet["base_ref"],
         "--head-ref", launch_packet.get("head_ref", "")],
    )
    progress(message=f"Lifecycle: {lifecycle_json.get('lifecycle_summary', {})}")

    # 4. Assemble report envelope (conforms to findings-schema.json)
    report = assemble_report_envelope(
        launch_packet=launch_packet,
        enriched=enriched_json,
        lifecycle=lifecycle_json,
        judge_output=extract_json_from_text(Path(judge_output_path).read_text()),
        timing=assemble_timing(launch_packet),
    )

    # 5. Render markdown report (follows references/report-template.md)
    markdown = render_markdown_report(report)

    # 6. Write temp files for validation
    report_json_path = session / "report.json"
    report_json_path.write_text(json.dumps(report, indent=2))
    report_md_path = session / "report.md"
    report_md_path.write_text(markdown)

    # 7. Run validate_output.sh (subprocess)
    validation = run_subprocess(
        ["bash", str(SCRIPTS_DIR / "validate_output.sh"),
         "--findings", str(report_json_path),
         "--report", str(report_md_path)],
    )
    progress(message=f"Validation: {'PASS' if validation.returncode == 0 else 'FAIL'}")

    # 8. Save final artifacts to .agents/reviews/
    date_str = datetime.now().strftime("%Y-%m-%d")
    target = launch_packet["scope"]  # e.g., "branch-main"
    artifact_dir = Path(".agents/reviews")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_artifact = artifact_dir / f"{date_str}-{target}.json"
    md_artifact = artifact_dir / f"{date_str}-{target}.md"
    json_artifact.write_text(json.dumps(report, indent=2))
    md_artifact.write_text(markdown)

    # 9. Do NOT cleanup session directory here — the agent may still need
    # launch.json for PR comments (Step 6) or debugging.
    # Cleanup is agent-initiated: thin SKILL.md calls --cleanup after the
    # review is fully complete.

    return {
        "status": "complete",
        "verdict": report["verdict"],
        "verdict_reason": report["verdict_reason"],
        "tier_summary": report.get("tier_summary", {}),
        "json_artifact": str(json_artifact),
        "markdown_artifact": str(md_artifact),
        "session_dir": str(session),  # agent uses this for cleanup
        "report_preview": markdown[:3000],
    }
```

### Report envelope assembly

`assemble_report_envelope()` produces a JSON object conforming to `findings-schema.json`:

```python
def assemble_report_envelope(launch_packet, enriched, lifecycle, judge_output, timing):
    """Build the final review JSON conforming to findings-schema.json.

    IMPORTANT: The verdict is RE-DERIVED from the final findings, not taken from
    the judge output. The judge computes its verdict before enrichment and lifecycle
    processing. If enrichment drops a high-severity finding (below confidence floor)
    or lifecycle suppresses one, the judge's verdict may be FAIL while the final
    findings have no must_fix items. Re-deriving prevents this inconsistency.

    Strengths, spec_gaps, and spec_requirements come from the judge (they don't
    change during enrichment/lifecycle).
    """
    final_findings = lifecycle.get("findings", [])
    tier_summary = enriched.get("tier_summary", {})

    # Re-derive verdict from final findings (not judge's pre-enrichment verdict)
    verdict, verdict_reason = derive_verdict(final_findings, tier_summary)

    # Lifecycle summary: use explicit zeros when lifecycle.py didn't run
    lifecycle_summary = lifecycle.get("lifecycle_summary",
        {"new": 0, "recurring": 0, "rejected": 0, "deferred": 0, "deferred_resurfaced": 0})

    envelope = {
        "run_id": launch_packet["review_id"],
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "scope": launch_packet["scope"],
        "base_ref": launch_packet["base_ref"],
        "head_ref": launch_packet.get("head_ref", ""),
        "pr_number": launch_packet.get("pr_number"),
        "review_mode": launch_packet["mode"],
        "files_reviewed": launch_packet["changed_files"],
        # Verdict re-derived from final findings
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        # From judge output (these don't change during enrichment/lifecycle)
        "strengths": judge_output.get("strengths", []),
        "spec_gaps": judge_output.get("spec_gaps", []),
        "spec_requirements": judge_output.get("spec_requirements", []),
        # From enrichment + lifecycle
        "findings": final_findings,
        "suppressed_findings": lifecycle.get("suppressed_findings", []),
        "tier_summary": tier_summary,
        "dropped": enriched.get("dropped", {}),
        "lifecycle_summary": lifecycle_summary,
        # From scan orchestration
        "tool_status": launch_packet.get("tool_status", {}),
        # Timing
        "_timing": timing,
        # Chunked mode metadata
        **({"chunk_count": len(launch_packet.get("chunks", [])),
            "chunks": launch_packet.get("chunks", [])}
           if launch_packet["mode"] == "chunked" else {}),
    }
    return envelope


def derive_verdict(findings: list, tier_summary: dict) -> tuple[str, str]:
    """Re-derive verdict deterministically from final findings.

    Same rules as the judge (reviewer-judge.md §4c) but applied mechanically
    to the post-enrichment/lifecycle findings:
    - FAIL: any finding with severity high/critical AND confidence >= 0.80
    - WARN: any finding with severity medium OR action_tier == should_fix
    - PASS: all findings are low/consider, or no findings
    """
    must_fix = tier_summary.get("must_fix", 0)
    should_fix = tier_summary.get("should_fix", 0)

    if must_fix > 0:
        blocking = [f for f in findings if f.get("action_tier") == "must_fix"]
        reason = f"{must_fix} blocking issue(s): " + "; ".join(
            f["summary"][:80] for f in blocking[:3])
        return "FAIL", reason
    if should_fix > 0:
        return "WARN", f"{should_fix} issue(s) to address before merge."
    if findings:
        return "PASS", "Minor suggestions only — no issues blocking merge."
    return "PASS", "No issues found."
```

### Markdown report rendering

`render_markdown_report()` follows the template in `references/report-template.md`. Implementation uses Python string formatting (no Jinja2 or external template engines):

```python
def render_markdown_report(report: dict) -> str:
    """Render the review as markdown following references/report-template.md."""
    sections = []
    sections.append(render_header(report))
    sections.append(render_tool_status(report["tool_status"]))
    sections.append(render_strengths(report["strengths"]))
    # Group findings by action_tier
    must_fix = [f for f in report["findings"] if f.get("action_tier") == "must_fix"]
    should_fix = [f for f in report["findings"] if f.get("action_tier") == "should_fix"]
    consider = [f for f in report["findings"] if f.get("action_tier") == "consider"]
    if must_fix:
        sections.append(render_tier("Must Fix", must_fix))
    if should_fix:
        sections.append(render_tier("Should Fix", should_fix))
    if consider:
        sections.append(render_tier("Consider", consider))
    if report.get("spec_requirements"):
        sections.append(render_spec_verification(report))
    sections.append(render_summary(report))
    return "\n\n".join(sections)
```

---

## Supporting Function Specifications

Functions called in the pseudocode that need explicit specification.

### `load_review_instructions(repo_root)`

Search these paths in order, concatenate all found content:
1. `repo_root / "REVIEW.md"` — extract "Always check" and "Style" sections (see v1.3 F20)
2. `repo_root / ".github/codereview.md"` — include full content
3. `repo_root / ".codereview.md"` — include full content
4. `.codereview.yaml` `custom_instructions` field — include as-is

Return concatenated string. Return empty string if nothing found. No error on missing files.

### `load_language_standards(changed_files)`

Detect languages from file extensions, then search for standards references:
1. `SKILL_ROOT / "references" / "{language}.md"` (co-located with the skill)
2. Search installed skills: glob `~/.claude/skills/standards/references/{language}.md`

Return a dict: `{"python": "<content>", "typescript": "<content>"}`. Return `{}` if standards skill not installed — log: "INFO: standards skill not installed."

### `load_spec(path)`

Read the file at `path`. If the file is larger than 50KB (~12k tokens), truncate to the first 50KB with a note: `"[Spec truncated to 50KB — full spec at {path}]"`. Raise FileNotFoundError if the path doesn't exist (spec is explicitly requested, so it's required).

### `assemble_expert_panel(diff_result, config, spec_content)`

**Core experts** (always included): correctness, security, test-adequacy.

**Activated experts** — grep ADDED lines only (lines starting with `+` in the diff) for activation patterns:

| Expert | Activation regex (on added lines) | Skip condition |
|--------|----------------------------------|----------------|
| shell-script | `\.(sh\|bash\|zsh\|ps1\|bat\|cmd)$` in changed_files, OR `Makefile\|Justfile\|Dockerfile` | — |
| api-contract | `route\|endpoint\|handler\|@app\.\|@api\.\|@router\.\|export (function\|class)\|\.proto\|\.graphql\|openapi\|swagger` | — |
| concurrency | `goroutine\|go func\|threading\|Thread\|async def\|asyncio\|\.lock\(\|[Mm]utex\|chan \|channel\|atomic\|sync\.\|Promise\.all\|Worker\(\|spawn\|tokio\|rayon\|par_iter\|crossbeam\|Semaphore` | — |
| error-handling | `catch\|except\|rescue\|recover\|if err\|Result::Err\|\.catch\(\|on_error` | Skip if diff is test-only, docs-only, or config-only |
| reliability | `open\(\|connect\(\|pool\|timeout\|retry\|cache\|\.close\(\|defer\|context\.WithTimeout\|http\.Get\|fetch\(` | — |
| spec-verification | — | Only if `spec_content` is not None |

**Config override:** If `config["experts"]["force_all"]` is True, activate all experts. If `config.get("passes")` is set, intersect activated experts with the configured passes list. If `config["experts"][name]` is False, disable that expert.

Returns a list of expert dicts with `name`, `prompt_file`, `model`, `core`, `activation_reason`.

### `deep_merge(base, override)`

Merge `override` into `base` recursively. Rules:
- **Dicts** merge recursively (keys in override add to or replace keys in base)
- **Arrays** replace entirely (override array replaces base array, not appended)
- **Null** values in override delete the key from the result
- **Scalars** in override replace the base value

Example: `deep_merge({"a": [1,2], "b": {"x": 1, "y": 2}}, {"a": [3], "b": {"y": 9}})` → `{"a": [3], "b": {"x": 1, "y": 9}}`

### `run_subprocess_text(cmd, ...)`

Same as `run_subprocess_json()` but returns `result.stdout` as a string instead of parsing JSON. Used for git diff output and validate_output.sh. Same timeout, error handling, and encoding behavior.

### Timing across phases

Each phase appends timing data to `<session_dir>/timing.jsonl` (append-only JSONL):
```json
{"phase": "prepare", "start_ms": 1711450000000, "end_ms": 1711450030000, "duration_ms": 30000}
{"phase": "post_explorers", "start_ms": 1711450300000, "end_ms": 1711450305000, "duration_ms": 5000}
```

`assemble_timing()` in `--finalize` reads this file and produces the `_timing` object for the report envelope:
```json
{"total_ms": 480000, "phases": [{"name": "prepare", "duration_ms": 30000}, ...]}
```

Explorer and judge timing is added by the agent (between phases) as separate entries.

---

## Launch Packet Schema

The launch packet is the contract between Phase 1 (producer) and Phases 2-3 + the agent (consumers). All fields are documented with types and required/optional status.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | `"ready" \| "empty" \| "error"` | Yes | Phase 1 result status |
| `message` | string | If status != ready | Human-readable status message |
| `review_id` | string | Yes | Unique ID: `YYYYMMDDTHHMMSSZ-PID` |
| `session_dir` | string (absolute path) | Yes | Temp directory for all session artifacts |
| `mode` | `"standard" \| "chunked"` | Yes | Review mode |
| `scope` | `"branch" \| "range" \| "staged" \| "commit" \| "pr" \| "path"` | Yes | How the diff was determined |
| `base_ref` | string | Yes | Git ref for the base |
| `head_ref` | string | Yes | Git ref for the head |
| `pr_number` | int \| null | Yes | PR number if scope=pr, else null |
| `changed_files` | string[] | Yes | List of changed file paths |
| `file_count` | int | Yes | Length of changed_files |
| `diff_lines` | int | Yes | Total lines in the diff |
| `waves` | Wave[] | Yes | Uniform execution plan (see below). Always present — standard mode has one wave, chunked mode has multiple. |
| `judge` | JudgeConfig | Yes | Judge configuration |
| `scan_results_file` | string (absolute path) | Yes | Path to run-scans.sh output JSON |
| `tool_status` | object | Yes | Tool status from run-scans.sh |
| `spec_file` | string \| null | Yes | Path to spec if --spec provided |
| `context_summary` | string | Yes | Human-readable summary for progress display |
| `_config` | object | Yes | Allowlisted config subset for Phase 2/3. Contains only keys these phases need: `confidence_floor`, `experts`, `token_budget`, `large_diff`, `pushback_level`, `judge_model`, `pass_models`. Full config (which may contain API keys or custom endpoints) is NOT written to disk. |
| `chunks` | Chunk[] \| null | Yes | Null in standard mode; array in chunked mode (metadata only) |
| `post_wave_task` | Task \| null | No | If present, launch after all waves complete (cross-chunk synthesis) |
| `_timing` | object | No | Phase 1 timing data |

**Note:** `experts` is NOT a top-level field. All tasks are inside `waves[].tasks[]`. This eliminates conditional mode logic in the thin SKILL.md — the agent always iterates waves.

**Expert schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Expert identifier (e.g., "correctness", "shell-script") |
| `model` | string | Yes | Model to use (e.g., "sonnet", "opus") |
| `prompt_file` | string | Yes | Source prompt filename (e.g., "reviewer-correctness-pass.md") |
| `assembled_prompt_file` | string (absolute path) | Yes | Path to the fully assembled prompt |
| `output_file` | string (absolute path) | Yes | Path where the explorer should write output |
| `activation_reason` | string | Yes | Why this expert was activated |

**JudgeConfig schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt_file` | string (absolute path) | Yes | Path to judge prompt source |
| `model` | string | Yes | Model for the judge |
| `output_file` | string (absolute path) | Yes | Path for judge output |

**Chunk schema (chunked mode only):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | int | Yes | Chunk number (1-based) |
| `description` | string | Yes | Directory path(s) covered |
| `files` | string[] | Yes | File paths in this chunk |
| `risk_tier` | `"critical" \| "standard" \| "low-risk"` | Yes | Highest risk tier of any file |
| `diff_lines` | int | Yes | Lines changed in this chunk |
| `experts` | Expert[] | Yes | Experts assigned to this chunk |
| `cross_chunk_summary` | string | Yes | Cross-chunk interface description |

**Wave schema (always present — standard mode has one wave):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `wave` | int | Yes | Wave number (1-based) |
| `tasks` | Task[] | Yes | Tasks to launch in this wave (all in parallel) |

**Task schema (inside waves):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Task identifier (e.g., "correctness", "chunk1-security") |
| `model` | string | Yes | Model to use |
| `assembled_prompt_file` | string (absolute path) | Yes | Path to fully assembled prompt |
| `output_file` | string (absolute path) | Yes | Path where output should be written |
| `activation_reason` | string | Yes | Why this task was included |
| `core` | boolean | Yes | If true, retry once on failure; if false, skip on failure |

**Standard mode example:** One wave with all experts.
```json
"waves": [
  {"wave": 1, "tasks": [
    {"name": "correctness", "model": "sonnet", "assembled_prompt_file": "/tmp/.../correctness-prompt.md", "output_file": "/tmp/.../correctness.json", "activation_reason": "core"},
    {"name": "security", "model": "sonnet", "assembled_prompt_file": "/tmp/.../security-prompt.md", "output_file": "/tmp/.../security.json", "activation_reason": "core"},
    {"name": "shell-script", "model": "sonnet", "assembled_prompt_file": "/tmp/.../shell-script-prompt.md", "output_file": "/tmp/.../shell-script.json", "activation_reason": ".sh files in diff"}
  ]}
]
```

**Chunked mode example:** Multiple waves, tasks reference chunks.
```json
"waves": [
  {"wave": 1, "tasks": [
    {"name": "chunk1-correctness", "model": "sonnet", "assembled_prompt_file": "/tmp/.../chunk1-correctness-prompt.md", "output_file": "/tmp/.../chunk1-correctness.json", "activation_reason": "core, chunk 1 (critical)"},
    {"name": "chunk1-security", "model": "sonnet", "assembled_prompt_file": "/tmp/.../chunk1-security-prompt.md", "output_file": "/tmp/.../chunk1-security.json", "activation_reason": "core, chunk 1 (critical)"}
  ]},
  {"wave": 2, "tasks": [
    {"name": "chunk2-correctness", "model": "sonnet", "assembled_prompt_file": "/tmp/.../chunk2-correctness-prompt.md", "output_file": "/tmp/.../chunk2-correctness.json", "activation_reason": "core, chunk 2 (standard)"}
  ]}
],
"post_wave_task": {
  "name": "cross-chunk-synthesis", "model": "sonnet",
  "assembled_prompt_file": "/tmp/.../cross-chunk-prompt.md",
  "output_file": "/tmp/.../cross-chunk.json",
  "activation_reason": "chunked mode cross-chunk analysis"
}
```

The agent iterates `waves[]` identically in both modes — no conditional logic.

---

## Explorer Failure Handling

Explorers are LLM-driven and inherently unreliable. The pipeline must handle these failure modes:

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Explorer crashes / non-zero exit | Sub-agent reports error | Log warning; proceed without that explorer's findings |
| Explorer returns invalid JSON | `json.loads()` fails in Phase 2 | Log warning with parse error; skip explorer |
| Explorer returns empty `[]` | Valid but no findings | Normal — explorer found nothing |
| Explorer times out | Sub-agent timeout (agent-managed) | Log warning; proceed without that explorer |
| Explorer returns wrong format | `parse_explorer_output()` returns empty | Log warning; skip explorer |

**Differentiated retry for core vs extended explorers:**

Core explorers (correctness, security, test-adequacy) are qualitatively different from activated experts (shell-script, concurrency, api-contract). A review without correctness analysis is fundamentally incomplete.

| Explorer type | On failure | Retry? |
|--------------|-----------|--------|
| **Core** (correctness, security, test-adequacy) | Retry once with truncated prompt (drop language standards + scan results to reduce context) | Yes — one retry |
| **Activated** (shell-script, error-handling, etc.) | Skip, log warning | No |

The Task schema includes a `core: true` field so the agent knows which explorers warrant retry. The thin SKILL.md instructs: "If a core explorer fails, retry once. If it fails again, log a prominent warning: 'Correctness analysis unavailable — review may miss functional bugs.'"

**Policy:** The review continues with whatever explorers succeeded. Phase 2 reports `explorer_status` showing which explorers produced results and which failed. The agent relays failed explorer names to the user. A review with 3/5 successful explorers is still valuable.

**Minimum threshold:** If zero core explorers succeed after retries, the agent warns the user: "No core analysis completed. Consider retrying the full review." It does NOT proceed silently.

---

## Script Failure Handling

Each script phase calls sub-scripts that can fail. Unlike explorer failures (LLM-driven, inherently unreliable), script failures are deterministic and have known failure modes:

| Script | Failure mode | Recovery |
|--------|-------------|----------|
| `discover-project.py` | crash / bad JSON / timeout | Proceed with empty project profile; log warning |
| `complexity.sh` | crash / bad JSON / radon hang | Proceed without complexity context; log warning |
| `git-risk.sh` | crash / bad JSON / shallow clone | Proceed without risk context; log warning |
| `run-scans.sh` | exit 1 (jq missing = fatal dep) | **FATAL:** abort `--prepare` with clear error ("jq is required") |
| `run-scans.sh` | bad JSON / timeout | Proceed without scan results (AI-only review); log warning |
| `coverage-collect.py` | crash / bad JSON / timeout | Proceed without coverage data; log warning |
| `enrich-findings.py` | crash / import error | **FATAL:** abort `--finalize` (enrichment is a required step) |
| `lifecycle.py` | crash / validation error | Proceed with unenriched findings; log warning |
| `validate_output.sh` | exit 1 (validation failure) | Save artifacts anyway; mark `validation_status: "fail"` in finalize output |

**Principle:** Only two scripts are fatal: `run-scans.sh` exit 1 (missing jq, a hard dependency) and `enrich-findings.py` crash (tier classification is core pipeline). Everything else degrades gracefully.

---

## Explorer Invocation

The agent has two options. Both work because orchestrate.py has already assembled the complete prompt.

### Option A: Sub-agents (recommended)

```
For each expert in launch_packet["experts"]:
  Launch Agent tool with:
    subagent_type: "general-purpose"
    prompt: <content of assembled_prompt_file>
    model: <expert.model>
    description: "Review explorer: <expert.name>"
    run_in_background: true
```

Launch all experts in a single message for parallel execution. Wait for all to complete.

### Option B: Claude CLI one-off calls (advanced)

```bash
# Verify actual CLI flags before using — flags may differ across versions
claude --print "$(cat /tmp/codereview-XXXXXXXX/explorer-correctness-prompt.md)" \
  --model sonnet \
  > /tmp/codereview-XXXXXXXX/explorer-correctness.json &
```

**Note:** Claude CLI flag syntax varies by version. Consult `claude --help` for the current interface. Option A (sub-agents) is the portable default.

For the judge, always use a sub-agent (not CLI) because the judge needs Read/Grep/Glob tools for adversarial verification.

---

## Progress Streaming

orchestrate.py writes progress to stderr as structured JSONL:

```python
def progress(message: str, step: int = None, total: int = None, detail: str = None):
    """Write structured progress to stderr for the agent to relay."""
    entry = {"ts": time.strftime("%H:%M:%S"), "message": message}
    if step is not None:
        entry["step"] = step
        entry["total"] = total
    if detail:
        entry["detail"] = detail
    print(json.dumps(entry), file=sys.stderr, flush=True)
```

The agent reads stderr output and relays to the user:

```
  [1/8] Diff extracted (32 files, 7,469 lines)
  [2/8] Standard mode
  [3/8] Context gathered (scans: 3 findings)
  [5/8] Expert panel: correctness, security, test-adequacy, shell-script, error-handling
  [6/8] Explorer prompts assembled (5 prompts)
  [8/8] Launch packet ready
  [AI] Launching 5 explorers in parallel...
  [AI] 3/5 complete... waiting for correctness, security
  [AI] All explorers complete (47 raw findings)
  [AI] Post-explorer processing...
  [AI] Launching judge...
  [AI] Judge complete. Finalizing...
  [1/4] Enriched: {must_fix: 2, should_fix: 5, consider: 3}
  [2/4] Lifecycle: {new: 8, recurring: 2}
  [3/4] Validation: PASS
  [4/4] Saved artifacts
```

---

## Chunked Mode

In chunked mode, Phase 1 does additional work. The launch packet structure changes (see Launch Packet Schema — chunks and waves fields).

**Standard mode:** `experts[]` is a flat list at the top level. Agent iterates and launches all.

**Chunked mode:** `experts` at top level is absent. Instead, `chunks[].experts[]` has per-chunk expert lists, and `waves[]` specifies launch order. The agent follows the wave plan:

```
For wave in launch_packet["waves"]:
  For task_id in wave["tasks"]:
    Find the expert object by matching task_id = "chunk{id}-{name}"
    Launch sub-agent with that expert's assembled_prompt_file
  Wait for all tasks in this wave to complete
```

**Cross-chunk synthesis** (if applicable): After all chunk waves complete, the agent launches a cross-chunk synthesis sub-agent using `launch_packet["cross_chunk_synthesis"]`.

### Thin SKILL.md handles both modes

The thin SKILL.md (see below) includes instructions for both standard and chunked mode. The agent checks `launch_packet.mode` and follows the appropriate path.

---

## Thin SKILL.md

```markdown
# Code Review Skill

## Quick Start
(unchanged — same examples as current SKILL.md)

## When to Use / When NOT to Use
(unchanged)

## Execution

### Error handling (applies to all steps)
After each script phase, check the status field in the output JSON.
If "error": report the message to the user and stop.
After each agent step, if the agent fails or returns no output,
report the failure and offer to retry or skip.

### Step 1: Prepare
Create a session directory and run the orchestrator:
` ``bash
SESSION_DIR=$(mktemp -d /tmp/codereview-XXXXXXXX)
python3 scripts/orchestrate.py prepare --session-dir "$SESSION_DIR" [flags from user]
` ``

Read `$SESSION_DIR/launch.json`. If status is "empty": tell user
"No changes found to review" and stop.

### Step 2: Launch Explorers

Read the launch packet. For each wave in `waves[]`:
  Launch ALL tasks in the wave in parallel (single message, multiple
  Agent tool calls). For each task:
    - Read the assembled prompt from `task.assembled_prompt_file`
    - model: from `task.model`
    - description: "Review explorer: <task.name>"
  Wait for all tasks in this wave to complete.

  **Core explorer retry:** If a task with `core: true` fails, retry
  once. If it fails again, warn: "<name> analysis unavailable."

After each explorer completes, immediately:
  Extract the JSON from its response (find the JSON array or object).
  Write it to `task.output_file`.
  (Process one result at a time — do not accumulate all results.)

If `post_wave_task` exists in the launch packet, launch it after all
waves complete.

Relay progress: "[AI] Launching N explorers... [AI] M/N complete..."

### Step 3: Post-Explorer Processing
` ``bash
python3 scripts/orchestrate.py post-explorers --session-dir "$SESSION_DIR"
` ``

Read `$SESSION_DIR/judge-input.json`.

### Step 4: Launch Judge
Launch a single sub-agent:
- prompt: read from `judge_prompt_file` in the judge input
- model: from `judge_model`
- description: "Review judge: synthesize findings"

After the judge completes, extract JSON from its response and
write it to `judge_output_file`.

### Step 5: Finalize
` ``bash
python3 scripts/orchestrate.py finalize --session-dir "$SESSION_DIR"
` ``

Read `$SESSION_DIR/finalize.json`. Present the report to the user:
- Show verdict, tier summary, and report_preview
- Tell user where full artifacts are saved (json_artifact, markdown_artifact)

### Step 6: PR Comments (optional)
If in PR mode and user asks, post findings as inline PR comments using `gh api`.
Always ask before posting.

### Step 7: Cleanup
After the review is fully complete (including any PR comments):
` ``bash
rm -rf "$SESSION_DIR"
` ``

## Suppress a Finding
` ``bash
python3 scripts/lifecycle.py suppress \
  --review <latest review JSON path> \
  --finding-id <id> \
  --status rejected --reason "explanation" \
  --suppressions .codereview-suppressions.json
` ``
(The suppress subcommand goes directly to lifecycle.py, not through orchestrate.py.)

## Configuration
(unchanged — .codereview.yaml reference)

## Prompt Files
(unchanged — table of prompt files)
```

---

## Testing Strategy

orchestrate.py is the backbone of the pipeline — it must be thoroughly tested.

### Mocking approach

No LLM mocking needed. Each phase's tests mock only file I/O:

- **Phase 1 (`--prepare`) unit tests:** Mock `subprocess.run` to return canned stdout for each sub-script. Test that the launch packet has correct structure.
- **Phase 2 (`--post-explorers`) unit tests:** Write fixture JSON files to the session directory as if explorers had written them. Call `--post-explorers`. No subprocess mocking needed.
- **Phase 3 (`--finalize`) unit tests:** Write fixture enriched/lifecycle output. Mock `subprocess.run` for enrich-findings.py and lifecycle.py. Test that artifacts are saved correctly.
- **Integration tests:** Use real git repos (small fixture repos) for Phase 1, fixture files for explorer/judge output.

### Unit tests (in `tests/test-orchestrate.py`)

**Core pipeline:**

| Test | What it verifies |
|------|-----------------|
| `test_extract_diff_branch` | Diff extraction with --base main produces correct changed_files |
| `test_extract_diff_pr` | PR diff extraction via gh cli |
| `test_extract_diff_empty` | Empty diff returns status=empty |
| `test_mode_selection_standard` | Below threshold → standard mode |
| `test_mode_selection_chunked` | Above threshold → chunked mode |
| `test_mode_selection_force_flags` | --force-chunk and --no-chunk override thresholds |
| `test_expert_panel_core_always` | Core experts always present |
| `test_expert_panel_shell_activation` | .sh files activate shell-script expert |
| `test_expert_panel_no_activation` | No .sh files → no shell-script expert |
| `test_expert_panel_force_all` | --force-all-experts activates all |
| `test_prompt_assembly_includes_contract` | Assembled prompt contains global contract |
| `test_prompt_assembly_includes_diff` | Assembled prompt contains the diff |
| `test_prompt_assembly_includes_scan_results` | Assembled prompt contains scan summary |
| `test_config_loading_defaults` | No .codereview.yaml → all defaults |
| `test_config_loading_file` | .codereview.yaml values loaded |
| `test_config_cli_overrides` | CLI flags override yaml values |
| `test_config_allowlist` | Only allowlisted keys appear in launch packet _config |
| `test_path_resolution` | Prompt files found relative to skill root |
| `test_stale_session_cleanup` | --prepare deletes session dirs older than 2 hours |
| `test_global_timeout` | --timeout causes abort after deadline |

**Token budget:**

| Test | What it verifies |
|------|-----------------|
| `test_token_budget_within_limit` | Normal prompt passes budget check |
| `test_token_budget_truncation_cascade` | Over-budget prompt triggers progressive truncation |
| `test_token_budget_exceeded_raises` | Prompt still over budget after all truncations raises PromptBudgetExceeded |
| `test_prompt_context_render` | PromptContext dataclass renders all sections in correct order |

**JSON extraction (critical — most failure-prone component):**

| Test | What it verifies |
|------|-----------------|
| `test_extract_json_direct` | Clean JSON parses directly |
| `test_extract_json_markdown_fenced` | `` ```json [...] ``` `` extracts correctly |
| `test_extract_json_with_preamble` | "Here are findings:\n[...]" extracts the array |
| `test_extract_json_smart_quotes` | Curly quotes replaced before parsing |
| `test_extract_json_trailing_comma` | `[{...},]` parsed after comma removal |
| `test_extract_json_braces_in_strings` | `{"msg": "use {x}"}` parsed correctly via balanced-bracket extraction |
| `test_extract_json_nested_arrays` | `[{"tests": ["a", "b"]}]` parsed correctly |
| `test_extract_json_truncated` | Partial JSON recovers first complete object via balanced-bracket extraction |
| `test_extract_json_empty` | Empty string raises ValueError |
| `test_extract_json_trailing_text` | `[...]\nI found 1 issue.` extracts the array |
| `test_extract_json_string_value` | `"just a string"` raises ValueError (wrong shape for findings) |

**Pre-filter:**

| Test | What it verifies |
|------|-----------------|
| `test_dedup_exact_removes_duplicates` | Same (file, line, pass, severity) → keep higher confidence |
| `test_dedup_exact_keeps_different_pass` | Same file:line but different pass → both kept |
| `test_findings_cap` | > 50 findings capped to 50 by confidence |
| `test_confidence_floor_from_config` | Uses config value, not hardcoded 0.65 |

### Integration tests (in `tests/test-orchestrate-integration.sh`)

| Test | What it verifies |
|------|-----------------|
| Full round-trip | Phase 1 runs against fixture git repo → write fixture explorer outputs → Phase 2 reads Phase 1's actual output → write fixture judge output → Phase 3 reads Phase 2's actual output → validate_output.sh passes |
| Phase 2 explorer failure | Phase 2 with one missing + one invalid explorer output still produces judge input |
| Phase 2 all explorers fail | Phase 2 with zero valid outputs returns error status |
| Launch packet schema | Phase 1 output validates against launch-packet-schema.json |
| Script failure recovery | Phase 1 with complexity.sh returning error still produces launch packet |
| Chunked mode packet | Phase 1 with large fixture diff produces valid chunked launch packet with waves |
| Artifact filenames unique | Two reviews of same scope produce different artifact filenames |

### Fixture data

- `tests/fixtures/orchestrate/small-diff.patch` — 5-file diff for standard mode testing
- `tests/fixtures/orchestrate/large-diff.patch` — 100-file diff for chunked mode testing
- `tests/fixtures/orchestrate/mock-explorer-correctness.json` — Sample correctness findings
- `tests/fixtures/orchestrate/mock-explorer-security.json` — Sample security findings
- `tests/fixtures/orchestrate/mock-explorer-malformed.txt` — Preamble + JSON (tests extract_json)
- `tests/fixtures/orchestrate/mock-judge-output.json` — Sample judge output
- `tests/fixtures/orchestrate/launch-packet-standard.json` — Expected standard mode launch packet
- `tests/fixtures/orchestrate/launch-packet-chunked.json` — Expected chunked mode launch packet
- `tests/fixtures/orchestrate/codereview.yaml` — Sample config for config loading tests

---

## Design Tradeoffs and Implementer Notes

### Why multi-phase, not monolithic (Issue 15)

PR-Agent uses a single monolithic prompt per tool — one LLM call, 30-90 seconds, trading thoroughness for speed. Our multi-phase architecture (5+ explorers + judge + optional verification) is more complex but provides: (1) parallel investigation (5x throughput on the explorer phase), (2) adversarial validation (the judge catches ~30-50% of explorer false positives based on our review data), (3) per-explorer tool access (each explorer can Grep/Read independently — impossible in a monolithic prompt). The tradeoff is wall clock time: 6-13 minutes for a standard review vs 30-90 seconds. Quality is the primary goal; speed is secondary.

### Expected wall clock time (Issue 11)

| Phase | Standard (30 files) | Chunked (100 files) |
|-------|-------------------|-------------------|
| `--prepare` (scripts) | 10-30 seconds | 30-60 seconds |
| Explorer launch + execution | 2-4 minutes | 4-8 minutes (waves) |
| `--post-explorers` | 2-5 seconds | 5-10 seconds |
| Judge execution | 3-8 minutes | 5-10 minutes |
| `--finalize` | 5-15 seconds | 10-20 seconds |
| **Total** | **6-13 minutes** | **10-19 minutes** |

The judge phase is the longest single step. Progress streaming helps — the user sees script phases completing in real time, and explorer completion counts during the parallel phase. The judge phase is a single long silence (mitigated by the judge's tool calls appearing in sub-agent output if the agent framework supports it).

**Future: `--quick` mode.** Add a `--quick` flag that: (1) runs only 2 core explorers (correctness + security), (2) uses a simplified judge (Gatekeeper + Synthesizer only, skip Verifier tool calls), (3) targets 2-3 minute reviews. This is a Phase 2+ item — MVP ships with the full pipeline.

### Chunked mode token redundancy (Issue 7)

With 8 chunks × 5 explorers = 40 assembled prompts, each containing ~13k tokens of shared content (global contract, language standards, review instructions, scan results, spec). That's ~520k tokens of redundant prompt text across all invocations, costing ~$1.56 at Sonnet input pricing.

This is an intentional tradeoff: each sub-agent is stateless and needs the full context. Optimization opportunities for Phase 3:
- Omit language standards for Tier 3 (low-risk) chunks
- Summarize scan results as counts-only for non-security explorers
- Omit spec content for non-spec explorers
- Add a `context_budget` field to the chunk schema so `--prepare` can progressively reduce context for lower-tier chunks

### Expert panel activation heuristics (Issue 8)

The regex-based activation signals (grep diff for `goroutine|async def|Mutex`, etc.) produce false positives (~10-20% estimated — comments, strings, deleted code) and false negatives (concurrency via library abstractions not in the regex list).

This is acceptable: a false-positive activation costs ~$0.05 and 30 seconds (one unnecessary explorer). A false-negative costs finding quality. The `force_all_experts` config is the escape hatch.

**Implementer should:** (1) scope regex to added lines only (lines starting with `+` in the diff), (2) expand the pattern list for each domain (e.g., add `rayon`, `par_iter`, `crossbeam`, `Semaphore`, `CountDownLatch` to concurrency), (3) in Phase 2 when code_intel.py is available, use structural import analysis as the primary signal with regex as fallback.

### Deterministic pre-filter in --post-explorers (Issue 9)

The plan adds a mechanical pre-filter (exact dedup + confidence floor + findings cap) before the judge. This does NOT replace the judge's semantic dedup — the judge still does root-cause grouping and cross-explorer synthesis. The pre-filter removes only mechanical noise so the judge starts with a cleaner, smaller input. Expected reduction: raw 30-50 findings → 15-30 after pre-filter.

### Resumability (Issue 13)

MVP has no checkpoint/resume. A crash in `--finalize` means re-running the full pipeline. This is acceptable for 6-13 minute reviews.

**Phase 2+ item:** Add `--resume <session_dir>` that detects which phases completed (by checking for output files in the session directory) and resumes from the last incomplete phase. The session directory is a natural checkpoint store. Ensure `--finalize` is idempotent (re-running with same inputs produces same output).

---

## Migration Path

### Phase 1: Core orchestrator (MVP)

Build orchestrate.py with `--prepare`, `--post-explorers`, and `--finalize`. Standard mode only.

**Phase 1a — Minimal shippable (1-2 weeks, ~400 lines):**
- Argument parsing: `--base` mode only (branch review — the most common)
- Diff extraction and mode selection (standard only, detect-and-warn for chunked threshold)
- Deterministic scans: delegate to run-scans.sh
- Expert panel assembly: core experts only (correctness, security, test-adequacy) — no activated experts yet
- Prompt assembly from templates (global contract + pass prompt + diff + scan results)
- `--post-explorers`: read explorer outputs, extract_json_from_text, dedup, assemble judge prompt
- `--finalize`: enrichment only (enrich-findings.py) + report rendering + artifact saving
- Session directory management (agent-created via --session-dir)
- Launch packet schema
- Tests for extract_json_from_text, dedup_exact, prompt assembly

**Phase 1b — Full MVP (adds ~400 more lines):**
- All review target modes (PR, range, staged, path)
- Parallel context gathering (complexity, git risk, project discovery, coverage)
- Activated experts (VP F11 — shell-script, error-handling, api-contract, concurrency)
- Token budget with progressive truncation
- `--finalize`: lifecycle.py integration, validation, REVIEW.md loading, domain checklists
- Configuration loading from `.codereview.yaml` (requires PyYAML)
- Progress streaming to stderr
- Global timeout

**Not in Phase 1:** Chunked mode execution, `--enrich-context`, `--triage`, code_intel.py integration, verification pipeline integration, Claude CLI invocation.

**Migration coexistence:** Add `use_orchestrator: true` to `.codereview.yaml`. The current SKILL.md checks this flag — if true, it delegates to orchestrate.py. If false (default), uses the current flow. This lets users opt in during migration.

**Result:** SKILL.md → ~80 lines. Full alternating `SCRIPT → AGENT → SCRIPT → AGENT → SCRIPT` flow. Agent's only job: launch explorers, launch judge, present report.

### Phase 2: Structural context + cross-file planning

Add `--enrich-context` phase for cross-file context (v1.3 F19 planner + VP F6 sufficiency).
Integrate code_intel.py (v1.3 F3-F5) into `--prepare` for structural context.
Integrate diff formatter (v1.3 F6) into prompt assembly.
Integrate prescan (v1.3 F8) into `--prepare`.
Add file-level triage (v1.3 F21) into `--prepare`.
Add documentation context injection (VP F7) into `--prepare`.

**New phases in pipeline:**
```
--prepare → Agent: cross-file planner → --enrich-context → Agent: explorers → --post-explorers → Agent: judge → --finalize
```

**Result:** Richer context in explorer prompts. Cross-file bugs detectable.

### Phase 3: Verification pipeline + chunked mode

Add `--triage` phase for VP F0 Stages 1-2.
Add agent steps for VP F0 Stage 1 (feature extraction) and Stage 3 (verification).
Add VP F4 (multi-model spot-check) as optional agent step.
Add VP F9 (ticket verification) detection in `--prepare`.
Add chunked mode execution (clustering, wave planning, cross-chunk synthesis).

**New phases in pipeline:**
```
--prepare → [planner] → [--enrich-context] → Agent: explorers → --post-explorers → Agent: feature extraction → --triage → Agent: verification → Agent: judge → --finalize
```

**Result:** Full pipeline with verification, chunked mode, ticket compliance.

### Phase 4: Claude CLI integration (optional)

Add `--invoke-method cli` option that generates individual Claude CLI commands instead of a launch packet. Exact CLI flags determined from `claude --help` at implementation time.

---

## Interaction with Other Plans

### Compatible (no conflict, call via subprocess or include in prompts)

| Plan Feature | Interaction | When |
|-------------|-------------|------|
| v1.3 F1 (run-scans.sh) | Already a script — orchestrate.py calls it | Phase 1 MVP |
| v1.3 F2 (enrich-findings.py) | Already a script — orchestrate.py calls it in `--finalize` | Phase 1 MVP |
| v1.3 F3-F5 (code_intel.py) | orchestrate.py calls for structural context in `--prepare` | Phase 2 |
| v1.3 F6 (diff formatter) | orchestrate.py calls `code_intel.py format-diff`, uses formatted diff in assembled prompts | Phase 2 |
| v1.3 F8 (prescan) | orchestrate.py runs prescan.py, includes in explorer context | Phase 2 |
| v1.3 F9 (domain checklists) | orchestrate.py loads checklist files based on diff content, includes in prompts | Phase 1 MVP |
| v1.3 F10 (git risk) | Already a script — orchestrate.py calls it | Phase 1 MVP |
| v1.3 F11-F13 (prompt changes) | Content inside assembled prompts — no pipeline change | Phase 1 MVP |
| v1.3 F15-F18 (prompt changes) | Content inside assembled prompts — no pipeline change | Phase 1 MVP |
| v1.3 F20 (REVIEW.md) | orchestrate.py reads REVIEW.md, includes sections in prompts | Phase 1 MVP |
| v1.3 F22 (path instructions) | orchestrate.py reads config, matches paths, includes in prompts | Phase 1 MVP |
| VP F1 (two-pass judge) | Judge prompt restructure — content change, not pipeline | Phase 1 MVP |
| VP F3 (review summary) | Report template change in `--finalize` | Phase 1 MVP |
| VP F5 (fix validation) | Verifier prompt addition — content change | Phase 3 |
| VP F7 (doc context injection) | New context source in `--prepare` (package detection + optional web fetch) | Phase 2 |
| VP F8 (per-finding scoring) | Judge prompt addition — content change | Phase 2 |
| VP F10 (output repair) | Logic in `--finalize` before validation | Phase 1 MVP |
| VP F11 (adaptive expert panel) | Panel assembly logic IS orchestrate.py's expert selection | Phase 1 MVP |

### Become new phase pairs (LLM steps between script phases)

| Plan Feature | How it fits | When |
|-------------|------------|------|
| v1.3 F19 (cross-file planner) | Agent step between `--prepare` and explorers. orchestrate.py `--enrich-context` executes the planner's grep queries | Phase 2 |
| VP F0 Stage 1 (feature extraction) | Agent step after `--post-explorers`: batch LLM call for boolean features | Phase 3 |
| VP F0 Stage 2 (deterministic triage) | Script phase `--triage`: pure logic on extracted features | Phase 3 |
| VP F0 Stage 3 (verification agent) | Agent step after `--triage`: per-finding deep verification | Phase 3 |
| VP F4 (multi-model spot-check) | Agent step after judge: spot-check high/critical findings with alternate model | Phase 3 |
| VP F6 (context sufficiency) | Agent step within `--enrich-context` loop: evaluates context completeness | Phase 2 |
| VP F9 (ticket verification) | Detection script in `--prepare`; verification in spec explorer prompt | Phase 2 |

### Superseded

| Plan Feature | Why superseded |
|-------------|----------------|
| v1.3 F14 (output file batching) | **Fully superseded.** The orchestrator writes explorer outputs to session dir files by design. The judge receives file paths via the assembled prompt. F14's activation threshold, summary table, and Read-tool instructions are all built into `--post-explorers` and judge prompt assembly. No separate implementation needed. |

### Needs spec update (compatible but references old SKILL.md steps)

| Plan Feature | What to update |
|-------------|----------------|
| v1.3 F7 (interactive setup) | Runs before `--prepare`, not inside it. Agent-driven interactive step. |
| v1.3 F21 (file-level triage) | Triage logic moves into `--prepare` as Python code, not SKILL.md Step 3.6 |

---

## Files to Create

| File | Size estimate | Phase |
|------|--------------|-------|
| `skills/codereview/scripts/orchestrate.py` | ~800-1200 lines | Phase 1 MVP |
| `skills/codereview/launch-packet-schema.json` | ~100 lines | Phase 1 MVP |
| `tests/test-orchestrate.py` | ~400 lines | Phase 1 MVP |
| `tests/test-orchestrate-integration.sh` | ~200 lines | Phase 1 MVP |
| `tests/fixtures/orchestrate/` (directory with fixture files) | ~6 files | Phase 1 MVP |

## Files to Modify

| File | Change | Phase |
|------|--------|-------|
| `skills/codereview/SKILL.md` | Rewrite to thin wrapper (~80 lines) | Phase 1 MVP |
| `skills/codereview/prompts/reviewer-judge.md` | Add note: judge is sole authority for semantic dedup | Phase 1 MVP |
| `skills/codereview/references/design.md` | Add architecture diagram and rationale | Phase 1 MVP |
| `skills/codereview/references/acceptance-criteria.md` | Add scenarios for each phase | Phase 1 MVP |

## Dependencies

- **Required:** Python 3.8+, PyYAML (`pip install pyyaml`), git, jq (for run-scans.sh), bash 3.2+
- PyYAML is required for `.codereview.yaml` parsing. Use `--no-config` to skip config loading (all defaults) if PyYAML is unavailable.
- **Future optional:** tree-sitter + grammars (for code_intel.py in Phase 2)
- **Existing scripts:** run-scans.sh, enrich-findings.py, lifecycle.py, complexity.sh, git-risk.sh, discover-project.py, coverage-collect.py, validate_output.sh
- **Platform:** macOS, Linux. Windows requires WSL, Git Bash, or compatible bash shell. Native Windows is not supported (subprocess calls use `bash` explicitly).

## Effort

| Phase | Scope | Effort | Depends on |
|-------|-------|--------|-----------|
| Phase 1a (minimal) | Core pipeline: --base mode, core experts, scans, enrichment, report | Small-Medium (1-2 weeks) | Existing scripts only |
| Phase 1b (full MVP) | All modes, activated experts, token budget, lifecycle, config, progress | Medium (1-2 weeks) | Phase 1a |
| Phase 2 (context + planning) | --enrich-context, code_intel, prescan, diff formatter, triage | Medium | v1.3 F3-F6, F8, F19 |
| Phase 3 (verification + chunked) | VP F0 phases, chunked mode, ticket verification | Medium | VP F0 design |
| Phase 4 (CLI) | Optional Claude CLI invocation | Small | — |

---

## Appendix: Feature Reference

All Fxx identifiers referenced in this plan, with one-line descriptions and their source documents.

**v1.3 features** (defined in `docs/plan-treesitter.md`):

| ID | Name | One-line description |
|----|------|---------------------|
| F1 | Scan Orchestration | `run-scans.sh` — deterministic tool execution and finding normalization |
| F2 | Finding Enrichment | `enrich-findings.py` — ID generation, confidence gating, tier classification |
| F3 | Code Intelligence Base | `code_intel.py` — tree-sitter structural analysis (functions, imports, exports) |
| F4 | Dependency Graph | `code_intel.py graph` — cross-file symbol relationships and co-change frequency |
| F5 | Semantic Search | `code_intel.py graph --semantic` — vector similarity for related code discovery |
| F6 | Diff Formatter | `code_intel.py format-diff` — transform unified diff into LLM-optimized format |
| F7 | Interactive Setup | `code_intel.py setup` — dependency detection and install prompts |
| F8 | Prescan | `prescan.py` — fast static pattern checks (secrets, swallowed errors, dead code) |
| F9 | Domain Checklists | Static checklist files loaded based on diff content (auth, database, API) |
| F10 | Git History Risk | `git-risk.sh` — per-file churn frequency and bug-related commit counts |
| F11 | Test Pyramid Vocabulary | Structured test level (L0-L5) and bug-finding level (BF1-BF9) classification |
| F12 | Per-File Certification | Explorers must certify clean files with evidence, not just return `[]` |
| F13 | Contract Completeness Gate | Structured completeness assessment for spec verification |
| F14 | Output File Batching | **Superseded by orchestrator** — explorer outputs written to session dir |
| F15 | Pre-Existing Bug Classification | Distinguish introduced bugs from pre-existing bugs made reachable |
| F16 | Provenance-Aware Review Rigor | Adjust investigation depth based on code origin (human, AI, generated) |
| F17 | Phantom Knowledge Self-Check | Anti-hallucination guardrail in global contract |
| F18 | Mental Execution Framing | Correctness explorer simulates execution rather than pattern-matching |
| F19 | Cross-File Context Planner | LLM generates targeted grep patterns to find cross-file dependencies |
| F20 | REVIEW.md | Repo-level review directives (always check, style, skip) |
| F21 | File-Level Triage | Classify files as trivial/complex before launching expensive AI passes |
| F22 | Path-Based Review Instructions | Per-path review focus rules from `.codereview.yaml` |

**Verification Pipeline features** (defined in `docs/plan-verification-pipeline.md`):

| ID | Name | One-line description |
|----|------|---------------------|
| VP F0 | Verification Round | 3-stage: feature extraction → deterministic triage → agent verification |
| VP F1 | Two-Pass Judge | Restructure judge into verify-then-synthesize |
| VP F3 | Review Summary | Condensed copy-pasteable summary block for PR descriptions |
| VP F4 | Multi-Model Spot-Check | Cross-check high/critical findings with alternate model |
| VP F5 | Fix Validation | Verify suggested fixes don't introduce new bugs |
| VP F6 | Context Sufficiency | Evaluate cross-file context completeness, generate additional queries |
| VP F7 | Documentation Context | Inject library/framework documentation into explorer context |
| VP F8 | Per-Finding Scoring | 0-10 numeric quality score per finding |
| VP F9 | Ticket & Task Verification | Auto-detect local planning artifacts, verify implementation completeness |
| VP F10 | Output Repair | JSON repair strategies for malformed model output |
| VP F11 | Adaptive Expert Panel | Change-type-driven expert roster (shell, API, concurrency, etc.) |

---

## Appendix: Known Limitations and Implementation Notes

**Agent context pressure (Issue 22):** With 5+ explorer results flowing through the parent agent, context can reach 50-75k tokens. Mitigation: the thin SKILL.md instructs the agent to process each explorer result immediately upon completion (extract JSON, write to file) rather than accumulating all results before processing. Background agent results should be handled one at a time.

**Cross-chunk synthesis (Issue 19):** Referenced in chunked mode but not yet defined. This is a **Phase 3 deliverable**. For MVP and Phase 2, chunked mode is not implemented. When implemented, the cross-chunk synthesis agent will receive: the changeset manifest, CROSS-CHUNK tagged findings from explorers, and per-chunk finding summaries. Its prompt will be defined as part of Phase 3 work.

**Finding provenance after judge merge (Issue 24):** When the judge's Calibrator merges two findings into one, the merged finding gets a new fingerprint from `lifecycle.py`. This means it appears as "new" in lifecycle tracking even if both parent findings were "recurring." This is a known limitation, mitigated by `lifecycle.py`'s fuzzy matching (`FUZZY_MATCH_THRESHOLD = 0.60`) which can match merged findings against previous reviews. No plan change needed — document for implementer awareness.

**Artifact filename collision (Issue 29):** `--finalize` saves to `.agents/reviews/{date}-{scope}.json`. Two same-day reviews of the same scope overwrite. Implementer should use `{date}-{review_id_short}-{scope}.json` where `review_id_short` is the first 8 chars of review_id.

**Progress streaming during --prepare (Issue 30):** `--prepare` is a single Bash tool call. All progress lines appear simultaneously after it completes (10-30 seconds). This is an inherent limitation — real-time progress requires streaming, which conflicts with the synchronous Bash tool. Acceptable for MVP; the main value of progress is inter-phase updates ("[AI] Launching explorers...") which the agent controls directly.

**JSON extraction uses balanced-bracket extraction (resolved):** The `extract_json_from_text()` function uses balanced-bracket extraction (`_extract_balanced_json_candidate()`) to find complete JSON structures. This correctly handles brackets, quotes, and escape sequences inside JSON strings, and can recover partial objects from truncated arrays.

**Parallel agent limit (Issue 32):** Claude Code supports multiple Agent tool calls in a single message for parallel execution. Document max_parallel as 12 (configurable via `max_parallel_agents` in config). If a wave has more tasks, `plan_waves()` in `--prepare` splits it into sub-waves.

**Model selection guidance (Issue 28):** Defaults: Sonnet for all explorers and judge. Recommendations: Opus for judge (highest cognitive load, 4-phase sequential analysis), Haiku for extended/activated explorers (optional passes where cost matters more than depth). Estimated cost per review: standard (5 Sonnet explorers + 1 Sonnet judge ≈ $0.50-1.50), with Opus judge ($1.00-3.00).

**CLI flag naming (Issue 10):** orchestrate.py uses `--base` (user-facing) while run-scans.sh uses `--base-ref` (internal). orchestrate.py maps `--base` → `--base-ref` when calling run-scans.sh. The user-facing flag is shorter and matches git conventions.

**Memory usage:** Peak ~10MB for chunked mode prompt assembly (40 prompts at 100-300KB each, assembled sequentially and written to disk immediately). Not a concern for modern systems but noted for constrained CI runners.

**Subprocess stderr is captured, not streamed:** All sub-scripts called via `subprocess.run(capture_output=True)`. This means progress from long-running scripts (run-scans.sh can take 60+ seconds) is not visible until the subprocess completes. Acceptable tradeoff: streaming would require Popen + reader threads, adding complexity. The user sees progress between phases, not within them.

**Prompt file validation (Issue 44):** `--prepare` validates all required prompt files exist before doing any work. Missing prompt file → abort immediately with: "Missing prompt file: {path}. The codereview skill may be corrupted — reinstall." Saves the user from a 30-second wait followed by a traceback.

**post_wave_task is null until Phase 3 (Issue 45):** The launch packet schema includes `post_wave_task` for cross-chunk synthesis, but the prompt for this agent is not yet defined. In Phase 1 and Phase 2, this field is always null. The thin SKILL.md says "If post_wave_task exists, launch it" — if null, this is a no-op.

**Report envelope vs findings-schema.json (Issue 46):** After `assemble_report_envelope()`, implementer should validate the envelope against `findings-schema.json` (use `jsonschema` if available, or manual field checks). This catches envelope/schema drift early. When `lifecycle.py` fails and returns `{}`, `lifecycle_summary` is populated with explicit zeros: `{"new": 0, "recurring": 0, "rejected": 0, "deferred": 0, "deferred_resurfaced": 0}`.

**run-scans.sh project profile (Issue 53):** The project profile from `discover-project.py` should be passed to `run-scans.sh` via `--project-profile`. Since both run in parallel, the dependency must be resolved: either run discover-project.py first (fast, 5-10s) and pass its output to run-scans.sh, or accept that Tier 3 project commands are unavailable during parallel execution. Recommendation: run discover-project.py first (it's fast), then start the remaining scripts in parallel including run-scans.sh with the profile.

**report_preview truncation (Issue 54):** Truncate at a section boundary (`\n## ` or `\n### ` before char 3000) rather than at an arbitrary character position, to avoid cutting markdown tables or code blocks mid-line.

**Encoding in subprocess calls (Issue 49):** All `subprocess.run()` calls use `encoding='utf-8', errors='replace'`. If replacement characters appear in the diff (non-UTF8 source files), log a warning: "Non-UTF8 content detected — replacement characters used."

**Phase 1a → 1b compatibility (Issue 51):** The launch packet schema is forward-compatible from Phase 1a: `waves` is always present, `task.core` is always present. Phase 1a produces fewer tasks (core only) but the schema shape is identical. `post-explorers` and `finalize` work with either Phase 1a or 1b launch packets.

**select_mode() in Phase 1 (Issue 69):** Phase 1 does not implement chunked execution. `select_mode()` in Phase 1 returns `"standard"` always, but logs a warning when the diff exceeds chunked thresholds: `"WARNING: Diff exceeds chunked threshold (N files, M lines) but chunked mode is not yet available. Review quality may degrade."` Phase 3 changes this to return `"chunked"` when thresholds are exceeded.

**Chunked-mode functions in Phase 1 (Issue 70):** `build_manifest()`, `cluster_files()`, and `plan_waves()` are not implemented in Phase 1. The `if mode == "chunked"` block in prepare() is dead code in Phase 1. Implementer should stub these with `raise NotImplementedError("Chunked mode available in Phase 3")` to prevent confusion.

**Render and truncation functions (Issues 72, 73):** `render_markdown_report()` calls 7 sub-functions (render_header, render_tool_status, render_strengths, render_tier, render_spec_verification, render_summary). Each produces the corresponding section from `references/report-template.md` — refer to that file for exact format. Truncation functions: `summarize_scans_counts_only(scan_json)` → `"3 findings (2 high, 1 medium)"` from the scan JSON's findings array; `summarize_git_risk_tiers_only(risk_json)` → `"4 high-risk, 8 medium, 20 low"` from the risk JSON's summary object; `truncate_to_changed_hunks_only(diff_text)` → strip context lines from unified diff, keeping only `@@` headers and `+`/`-` lines.

**Coverage and project profile data flow (Issues 76, 77):** Coverage data from `coverage-collect.py` should be included in `PromptContext` as a `coverage` field and rendered as a `### Coverage Data` section in assembled prompts. Project profile from `discover-project.py` should be passed to `run-scans.sh` via `--project-profile` for Tier 3 scans. This creates a dependency (discover must complete before scans start). Resolution: run discover-project.py first (~5-10s), then parallelize the remaining scripts (scans, complexity, git-risk, coverage). Update the parallel execution model accordingly.
