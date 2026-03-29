# Spec A: Expert Registry & Structured Selection

**Status:** Draft (revised after pre-mortem)
**Author:** Research session 2026-03-28
**Depends on:** orchestrate.py (existing)
**Related:** Spec B (per-expert enrichment, separate doc — depends on this spec + context enrichment plan)
**Supersedes:** Verification Pipeline Feature 11 (Adaptive Expert Panel) — F11's architectural design is subsumed by this spec's registry + selection mechanism. F11's shell-script expert prompt content is preserved as source material (see "Shell-Script Expert: Source Material" section).

## Problem

Our expert roster is small (3 core + 7 extended = 10 total), selection is regex-based against the diff text (matches in comments/strings), and experts have no structured metadata for automated selection. When a PR touches Helm charts, Terraform modules, or database migrations, no relevant expert activates.

Meanwhile, the judge operates with a fixed neutral lens — it doesn't know which domains the review covered or which expert perspectives are missing.

## Goals

1. **Add structured frontmatter** to every expert prompt so the orchestrator can select experts without reading full files
2. **Expand** the expert roster with domain-specific experts that fill real coverage gaps
3. **Improve selection** from regex-on-diff to signal-based matching (file types, imports, AST constructs, project metadata)
4. **Inform the judge** about which expert perspectives were applied and which domains had no coverage

## Non-Goals

- Persona identity framing ("you are a senior K8s engineer") — research shows this hurts coding tasks (Wharton/USC "Playing Pretend", March 2026). Focus on review criteria and checklists instead.
- Multi-model routing (different LLM per expert) — deferred, blocked on runtime constraints
- Changing the explorer output schema
- Replacing the global contract (investigation protocol stays shared)
- Dynamic expert generation via LLM (deferred to Spec B)
- Dynamic enrichment / per-expert checklist injection (deferred to Spec B)
- Per-chunk expert selection in chunked mode (selection is per-review)

---

## Design

### Layer 1: Expert Prompt Frontmatter

Every expert prompt gets YAML frontmatter. The orchestrator parses only the frontmatter during the selection phase.

```yaml
---
name: concurrency
tier: extended                    # core | extended
description: >
  Reviews concurrent and async code for race conditions, deadlocks,
  shared mutable state, and goroutine/thread/task lifecycle issues.
domains:
  - async
  - threading
  - locks
  - channels
  - parallel-processing
languages: [python, go, rust, typescript, java]
activates_on:
  patterns:
    - "async def|asyncio|await "
    - "goroutine|go func|sync\\.Mutex|sync\\.RWMutex"
    - "thread::spawn|tokio::spawn|Arc<Mutex"
    - "Promise\\.all|Worker\\(|SharedArrayBuffer"
    - "synchronized|ExecutorService|CompletableFuture"
  file_types: []                  # e.g., [".tf", ".helm"]
  imports: ["asyncio", "threading", "tokio", "rayon", "crossbeam"]
  project_markers: []             # e.g., presence of go.mod, Cargo.toml
deactivates_on:
  max_files: 1                    # skip if diff has ≤1 file
  file_types_only: [".md", ".txt", ".json"]  # skip if ALL changed files are these types
  ignore_paths: []                # e.g., ["**/test_*", "**/tests/**"] — skip if ALL matches in these paths
  min_signals: 1                  # require N+ signal types to activate (use 2 for noisy experts)
groups: []                        # e.g., ["security"] — enables alias expansion
requires_context: []              # e.g., ["spec_content"] — skip if runtime context is absent
---
```

**Fields removed from original draft (per pre-mortem):**
- `complements` / `supersedes` — no consumer algorithm exists; dedup is the judge's job
- `cost: standard | high` — implies model routing, which is a non-goal; use existing `pass_models` config

**Backward compatibility:** Experts without frontmatter continue to work — `EXTENDED_EXPERT_PATTERNS` and `CORE_EXPERTS` remain as fallback until all experts have frontmatter and regression tests pass. Then the hardcoded dicts are removed.

### Data Types

```python
@dataclass(frozen=True)
class ActivationRule:
    """Conditions that trigger expert selection."""
    patterns: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    project_markers: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class DeactivationRule:
    """Conditions that suppress expert selection even if activation rules match."""
    max_files: int | None = None
    file_types_only: list[str] = field(default_factory=list)
    ignore_paths: list[str] = field(default_factory=list)  # glob patterns — skip if ALL matches are in these paths
    min_signals: int = 1              # require N+ signal sources to activate (default: 1 = any signal)

@dataclass(frozen=True)
class ExpertMeta:
    """Parsed frontmatter from a single expert prompt file."""
    name: str
    tier: str                          # "core" | "extended"
    description: str = ""
    domains: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    activates_on: ActivationRule = field(default_factory=ActivationRule)
    deactivates_on: DeactivationRule = field(default_factory=DeactivationRule)
    groups: list[str] = field(default_factory=list)
    requires_context: list[str] = field(default_factory=list)
    prompt_file: str = ""              # e.g., "reviewer-concurrency-pass.md"
    aliases: list[str] = field(default_factory=list)  # additional names for this expert

@dataclass
class SelectedExpert:
    """An expert that passed selection, with scoring rationale."""
    name: str
    score: float
    reasons: list[str]
    prompt_file: str
    tier: str
    domains: list[str]

@dataclass(frozen=True)
class ReviewSignals:
    """Aggregated signals from the prepare phase, used for expert selection."""
    added_lines: str                   # concatenated added lines from diff
    file_extensions: set[str]          # e.g., {".py", ".tf", ".yaml"}
    changed_files: list[str]           # full relative paths
    imports: set[str] | None           # from code_intel.py; None when unavailable
    project_files: set[str]            # filenames at repo root
    file_count: int
    has_spec: bool                     # whether spec_content is non-empty


class ExpertRegistry:
    """Loads and caches ExpertMeta from prompt files."""

    def __init__(self, prompts_dir: Path) -> None:
        self._dir = prompts_dir
        self._experts: list[ExpertMeta] | None = None

    def load(self) -> list[ExpertMeta]:
        """Parse all reviewer-*-pass.md files. Cache result."""
        if self._experts is not None:
            return self._experts
        self._experts = []
        for path in sorted(self._dir.glob("reviewer-*-pass.md")):
            meta = parse_expert_frontmatter(path.read_text(), path.name)
            if meta is not None:
                self._experts.append(meta)
        return self._experts

    def get(self, name: str) -> ExpertMeta | None:
        return next((e for e in self.load() if e.name == name or name in e.aliases), None)

    @property
    def known_names(self) -> set[str]:
        names = set()
        for e in self.load():
            names.add(e.name)
            names.update(e.aliases)
        return names

    def group_members(self, group: str) -> set[str]:
        """Return all expert names belonging to a group (e.g., 'security')."""
        return {e.name for e in self.load() if group in e.groups}
```

### Frontmatter Parsing

```python
import re

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

def parse_expert_frontmatter(prompt_text: str, filename: str) -> ExpertMeta | None:
    """Extract YAML frontmatter from an expert prompt file.

    Returns None (not raises) when:
    - No `---` delimiters found
    - yaml module not available
    - YAML parse error (malformed frontmatter)
    - Required field `name` missing

    Logs a warning on parse failure.
    """
    if yaml is None:
        return None  # graceful fallback; caller uses EXTENDED_EXPERT_PATTERNS

    match = _FRONTMATTER_RE.match(prompt_text)
    if not match:
        return None

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        progress("expert_frontmatter_warning", file=filename, error=str(exc))
        return None

    if not isinstance(data, dict) or "name" not in data:
        progress("expert_frontmatter_warning", file=filename, error="missing 'name' field")
        return None

    act = data.get("activates_on", {})
    deact = data.get("deactivates_on", {})

    return ExpertMeta(
        name=data["name"],
        tier=data.get("tier", "extended"),
        description=data.get("description", ""),
        domains=data.get("domains", []),
        languages=data.get("languages", []),
        activates_on=ActivationRule(
            patterns=act.get("patterns", []),
            file_types=act.get("file_types", []),
            imports=act.get("imports", []),
            project_markers=act.get("project_markers", []),
        ),
        deactivates_on=DeactivationRule(
            max_files=deact.get("max_files"),
            file_types_only=deact.get("file_types_only", []),
        ),
        groups=data.get("groups", []),
        requires_context=data.get("requires_context", []),
        prompt_file=filename,
        aliases=data.get("aliases", []),
    )
```

**YAML dependency:** `yaml` remains optional. When unavailable, `parse_expert_frontmatter()` returns `None` for every file, and the system falls back entirely to `EXTENDED_EXPERT_PATTERNS` / `CORE_EXPERTS`. This is the same graceful degradation as the existing `.codereview.yaml` handling. No dual-maintenance: once all experts have frontmatter and PyYAML is available, the hardcoded dicts are dead code — but they remain as a safety net.

### Layer 2: Expert Roster Expansion

New experts follow OCR's focused template — 40-60 lines, concrete checklists, standardized structure.

**Existing (keep, add frontmatter):**

| Expert | Tier | Notes |
|--------|------|-------|
| correctness | core | |
| security-config | core | |
| test-adequacy | core | |
| security-dataflow | extended | groups: [security] |
| concurrency | extended | |
| error-handling | extended | **Noisy activation** — set `min_signals: 2` and `ignore_paths: ["**/test_*", "**/*_test.*", "**/tests/**"]` to prevent firing on 70%+ of diffs |
| reliability | extended | **Noisy activation** — set `min_signals: 2` and `ignore_paths: ["**/test_*", "**/*_test.*", "**/tests/**"]`. Split from shared file (see below) |
| shell-script | extended | Split from shared file (see below) |
| api-contract | extended | |
| spec-verification | extended | requires_context: [spec_content] |

**Pre-requisite split:** `reviewer-reliability-performance-pass.md` is currently shared by `shell-script` and `reliability` (many-to-one mapping in `EXPERT_PROMPT_FILES`). Split into `reviewer-shell-script-pass.md` and `reviewer-reliability-pass.md` before adding frontmatter, so each file has exactly one `name:`.

**New experts to add:**

The roster below is the full candidate list. Each needs to be evaluated for:
- **Differentiation:** Does this expert catch issues that existing experts miss?
- **Activation frequency:** Will this expert activate often enough to justify maintenance?
- **Overlap:** Does this overlap substantially with an existing expert + enrichment?

| # | Expert | Activates on | Differentiation from existing | Overlap risk |
|---|--------|-------------|-------------------------------|-------------|
| 1 | database | SQL queries, ORM calls, migrations, schema changes | Correctness doesn't know migration safety patterns, N+1 detection, index analysis | Low — distinct domain |
| 2 | infrastructure | Dockerfile, K8s manifests, Terraform, Helm, CI configs | No existing expert reviews IaC or container configs | Low — distinct domain |
| 3 | performance | Hot loops, N+1 queries, memory allocation, caching, BigO | Reliability covers timeouts/pools but not algorithmic perf | Medium — overlaps reliability on caching |
| 4 | authorization | RBAC, ABAC, permission checks, policy engines, middleware auth | Security-config covers secrets/headers but not authz logic | Medium — overlaps security-config on auth |
| 5 | frontend | React/Vue/Svelte components, CSS, DOM, bundle config, hydration | No existing expert reviews component patterns or rendering | Low — distinct domain |
| 6 | accessibility | ARIA, alt text, form labels, focus management, a11y test libs | No existing expert reviews a11y | Low — distinct domain |
| 7 | ai-integration | LLM API calls, prompt construction, embeddings, model config | No existing expert reviews AI/LLM patterns | Low — distinct domain |
| 8 | data-pipeline | ETL, streaming, Kafka, data validation, schema evolution | Correctness reviews logic but not pipeline-specific patterns | Medium — overlaps correctness on data transforms |
| 9 | observability | Logging, metrics, tracing, OpenTelemetry, alert configs | Reliability covers some monitoring; this is focused on instrumentation correctness | Medium — overlaps reliability |
| 10 | dependency-management | Package manifest, lockfile, version bumps, CVE exposure | Security-config covers secrets; this covers supply chain | Medium — overlaps security-config on CVEs |
| 11 | mobile | React Native, Flutter, iOS/Android platform code | Frontend covers web; this covers native-specific patterns | Low if project uses mobile |
| 12 | migration-safety | DB migrations, backward compat, zero-downtime deploy | Database covers queries; this covers migration orchestration | Medium — overlaps database |
| 13 | state-management | Redux, Zustand, global state, session handling, cache invalidation | Frontend covers component patterns; this covers state bugs | High — likely better as frontend enrichment |
| 14 | configuration | Env vars, feature flags, config files, secrets references | Security-config already covers config review | High — redundant with security-config + enrichment |
| 15 | documentation | README, docstring, API doc, changelog changes | Findings are almost always low-severity style preferences | High — noise risk per pre-mortem |

**Prompt structure for new experts:**

```markdown
---
{frontmatter as defined above}
---

Review this diff for {domain} issues.

You are the {name} explorer. Your focus: {1-sentence summary}.

---

## Investigation Phases

Follow the chain-of-thought protocol from the global contract. Apply these pass-specific steps:

### Phase 1 — {Domain-specific inventory}
{concrete instructions with tool usage — Grep/Read/Glob}

### Phase 2 — {Domain-specific analysis}
{concrete checks against known patterns}

### Phase 3 — {Domain-specific integration check}
{how this code interacts with the rest of the system}

## What You Look For

{Concrete checklist of 10-15 items, organized by sub-domain}

## Calibration Examples

### IS a finding
{1-2 examples with file:line, evidence, failure_mode}

### IS NOT a finding
{1-2 examples showing what this expert should NOT report}
```

### Shell-Script Expert: Full Prompt Content

The shell-script expert is the most detailed of the new experts because shell review requires domain-specific investigation phases that no other expert covers. Motivated by the CodeRabbit gap analysis: findings #1, #5, #6, #14, #15, #24, #26, #27, #29 were all missed because our fixed panel had no expert calibrated for shell/script semantics.

**The Wave 1 implementation must write this content to `reviewer-shell-script-pass.md`.**

#### Frontmatter

```yaml
---
name: shell-script
tier: extended
description: >
  Reviews shell scripts for correctness, portability, error handling,
  and security patterns specific to bash/sh/POSIX scripts.
domains:
  - shell
  - bash
  - scripting
  - ci-cd
languages: [bash, sh, zsh]
activates_on:
  file_types: [".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"]
  patterns:
    - "^\\+\\#\\!/.+\\b(?:bash|sh)\\b"
  project_markers: ["Makefile", "Justfile", "Dockerfile"]
deactivates_on:
  max_files: 0
groups: []
requires_context: []
aliases: []
---
```

#### Investigation Phases

```markdown
Review this diff for shell script issues.

You are the shell-script explorer. Your focus: bash/shell correctness, portability,
error handling, and security patterns specific to shell scripts.

---

## Investigation Phases

Follow the chain-of-thought protocol from the global contract. Apply these pass-specific steps:

### Phase 1 — set -e Interaction Analysis
For each script using `set -e`, `set -euo pipefail`, or `errexit`:
1. **Identify all command substitutions** (`$(...)`, backticks). Under `set -e`,
   a failed command inside `$()` on an assignment line does NOT trigger exit in
   some shells (bash 4.4+), but DOES in assignment-then-use patterns and in
   subshells. Trace each command substitution to determine if failure is caught.
2. **Identify all conditional patterns** (`if cmd; then`, `cmd || handler`,
   `cmd && next`). Under `set -e`, these are exempt — failures inside conditions
   don't trigger exit. But `set -e` DOES trigger on the NEXT uncaught failure.
3. **Check jq/awk/sed pipelines** — under `set -eo pipefail`, a failed jq in a
   pipeline (`cat file | jq . | grep key`) causes the entire pipeline to fail.
   Verify the script handles this (e.g., `|| true`, `|| echo default`).
4. **Trace early-abort paths** — if a command fails and `set -e` triggers, what
   state is left behind? Temp files not cleaned up? Locks not released? Partial
   output written?

### Phase 2 — POSIX Portability
For scripts that claim POSIX or Bash 3 compatibility:
1. **Grep for non-POSIX constructs:**
   - `\s`, `\d`, `\w` in `grep -E` (use `[[:space:]]`, `[[:digit:]]`, `[[:alnum:]_]`)
   - `[[ ]]` double brackets (use `[ ]` for POSIX)
   - `local -r`, `local -a` (not in POSIX `local`)
   - `mapfile`/`readarray` (bash 4+ only)
   - `${var,,}` / `${var^^}` case modification (bash 4+ only)
   - `&>` redirect (use `>file 2>&1`)
   - `<(process substitution)` (not POSIX)
2. **Check shebang** — `#!/usr/bin/env bash` vs `#!/bin/sh`. If shebang says sh,
   all bash-isms are bugs.
3. **Check tool-specific portability:**
   - BSD `sed` vs GNU `sed` (`-i` flag behavior differs)
   - BSD `grep` vs GNU `grep` (ERE `\s` not supported in BSD)
   - `date` flags differ between BSD (macOS) and GNU
   - `mktemp` template syntax differs slightly

### Phase 3 — Error Swallowing Analysis
For each `|| true`, `2>/dev/null`, `|| :`, or `|| echo` pattern:
1. **Classify intent:** Is this swallowing expected errors (cleanup, optional
   features) or hiding real failures?
   - **Intentional:** `rm -f "$tmpfile" 2>/dev/null || true` — file may not exist
   - **Harmful:** `chmod +x scripts/*.sh 2>/dev/null || true` — hides permission errors
   - **Harmful:** `jq '.key' "$file" 2>/dev/null || true` — hides malformed JSON
2. **Check scope:** Does `|| true` apply to just one command or to a pipeline?
   `cmd1 | cmd2 || true` catches cmd2 failure but not cmd1 (with pipefail).
3. **Check downstream assumptions:** If the command fails silently, does subsequent
   code assume it succeeded? E.g., `mkdir -p "$dir" 2>/dev/null || true` followed
   by `echo "data" > "$dir/file"` — the write fails if mkdir failed.

### Phase 4 — Dependency Validation
For each external tool the script uses extensively:
1. Check if the script validates the tool's availability upfront (before doing
   work that depends on it).
2. If not, trace what happens when the tool is missing — does it fail fast with
   a clear error, or silently produce wrong/empty output across many invocations?
3. Common pattern: script requires `jq` for 30+ operations but never checks
   `command -v jq`. Missing jq causes 30 silent failures instead of one clear error.

### Phase 5 — String Interpolation Safety
For bash scripts that construct structured data (JSON, YAML, XML, SQL):
1. **Identify construction patterns:**
   - Heredoc with variable substitution: `cat <<EOF ... ${var} ... EOF`
   - echo/printf with variables: `echo "{\"key\": \"$value\"}"`
   - String concatenation: `json="$json,\"$key\":\"$val\""`
2. **Check for injection:** Can the variable contain characters that break the
   format? Quotes in JSON values, newlines in YAML, semicolons in SQL.
3. **Check for safe alternatives:** Does the script have access to `jq -n --arg`
   (for JSON), `python3 -c` (for any format), or format-specific escaping?
4. Report the injection vector AND the safe alternative.

### Phase 6 — Redirect and Pipe Semantics
1. **stdin override:** When a command receives input from both a pipe AND a
   redirect (`echo data | cmd < file`), the redirect wins. The piped data is
   lost. This is a common test bug — piping test data into a function that
   also has a `< source_file` redirect.
2. **stdout/stderr capture:** `result=$(cmd 2>&1)` captures both. `result=$(cmd)`
   captures only stdout. If `cmd` writes errors to stderr and the script checks
   `$result`, stderr content is lost.
3. **Subshell variable isolation:** `echo data | while read line; do VAR=$line; done`
   — the `while` runs in a subshell (due to pipe). `$VAR` is NOT set after the
   loop. Use `while read line; do ...; done < <(echo data)` or a temp file.
```

#### Calibration Examples

**True Positive — set -e Interaction (High Confidence):**
```json
{
  "pass": "reliability",
  "severity": "high",
  "confidence": 0.90,
  "file": "scripts/validate_output.sh",
  "line": 113,
  "summary": "set -e aborts script when jq fails on malformed .findings, preventing error summary",
  "evidence": "Line 113-120: type check detects non-array .findings and increments ERRORS. But line 125: BAD_FINDING_COUNT=$(jq '[.findings[] | ...]' ...) — jq .findings[] fails with exit 5 when findings is not an array. With set -euo pipefail (line 35), this aborts the script before reaching the RESULT: FAIL summary at line 413.",
  "failure_mode": "On malformed input, the validation script crashes instead of reporting a clean FAIL.",
  "fix": "After type check fails, set FINDING_COUNT=0 and skip per-finding validation."
}
```

**True Positive — JSON Injection via Bash Interpolation (Medium Confidence):**
```json
{
  "pass": "security",
  "severity": "medium",
  "confidence": 0.80,
  "file": "scripts/timing.sh",
  "line": 55,
  "summary": "Variable $name interpolated directly into JSON string without escaping",
  "evidence": "Line 55: echo \"{\\\"type\\\":\\\"start\\\",\\\"name\\\":\\\"$name\\\"}\" >> \"$TIMING_FILE\". The $name comes from first positional arg. A step name with double-quote produces malformed JSONL. Downstream jq parse fails, summary falls back to zeros.",
  "failure_mode": "Timing data silently lost when step name contains JSON-special characters.",
  "fix": "Use jq: jq -n --arg name \"$name\" --argjson ts \"$ts\" '{type:\"start\",name:$name,ts:$ts}'"
}
```

**False Positive — Do NOT Report:**
Scenario: `rm -f "$tmpfile" 2>/dev/null || true` in cleanup block. The `rm -f` handles missing files, `|| true` handles permission errors. Script doesn't depend on cleanup succeeding. Best-effort cleanup is intentional and harmless.

#### False Positive Suppression Rules

Do NOT report:
1. **`|| true` on cleanup/teardown** (temp files, locks, cache dirs) where failure is harmless
2. **Non-POSIX constructs** in scripts with `#!/usr/bin/env bash` shebang that don't claim POSIX
3. **Missing tool check** for tools used only once with proper error handling on that invocation
4. **`2>/dev/null`** on optional/informational commands (version checks, feature detection)
5. **Heredoc string interpolation** when variable is guaranteed safe (integer from `wc -l`, filename from controlled list)

#### Pass Value Assignment

Shell correctness findings use `pass: "reliability"`. Injection findings use `pass: "security"`. Logic bugs use `pass: "correctness"`. The judge deduplicates across pass values by file+line.

### Expert Interaction Model

When multiple experts activate on the same diff, their scopes overlap. The judge handles dedup, but experts should be aware of their boundaries:

| Expert pair | Division of responsibility |
|------------|---------------------------|
| shell-script + error-handling | Shell expert: `set -e`, `\|\| true`, dependency checks (shell-specific). Error-handling expert: try/catch, error returns, retry patterns (application-level). No overlap in practice — different language families. |
| shell-script + security | Shell expert: string interpolation safety (Phase 5). Security expert: trust boundaries, command injection, data flow. Judge deduplicates if both flag same line. |
| shell-script + correctness | Shell expert may find logic bugs (wrong grep flags, incorrect conditionals). Reports with `pass: "correctness"`. Judge deduplicates with correctness explorer. |
| database + security-dataflow | Database expert: N+1, migration safety, index analysis. Security-dataflow: SQL injection, data exposure. Overlap on SQL queries — judge deduplicates. |
| frontend + accessibility | Frontend: component lifecycle, rendering, bundle. Accessibility: ARIA, focus, screen reader. Minimal overlap — complementary perspectives. |

**Cross-pass finding categorization:** An expert may emit findings with a `pass` value different from its own name when the finding fits a different concern category. The shell-script expert emits `pass: "reliability"` for shell correctness issues and `pass: "security"` for injection findings. The judge handles dedup semantically (Gatekeeper → Verifier → Calibrator), not mechanically by file+line.

**Finding provenance (`source_expert` field):** Every finding carries a `source_expert` field set by the orchestrator when collecting explorer output. This field is preserved through the entire pipeline (verification, judge, enrichment, final report). It enables:
- Per-expert quality metrics over time (which experts produce findings that survive the judge?)
- Debugging false positives (which expert generated this?)
- A/B testing expert prompt changes

Schema addition to `findings-schema.json`:
```json
{
  "source_expert": "shell-script"  // set by orchestrator, preserved by judge
}
```

The judge prompt includes a note: "Preserve the `source_expert` field on all findings — do not modify or remove it."

### Layer 3: Selection Mechanism

Replace the current inline `EXTENDED_EXPERT_PATTERNS` loop in `assemble_expert_panel()` (orchestrate.py:904) with a structured selection pipeline.

#### 3a. Signal Collection

```python
def collect_review_signals(
    diff_result: DiffResult,
    config: dict[str, Any],
    spec_content: str | None,
) -> ReviewSignals:
    """Gather all signals needed for expert selection.

    Called once during prepare(), before expert selection.
    """
    added = _added_lines(diff_result.diff_text)
    extensions = {Path(f).suffix for f in diff_result.changed_files if Path(f).suffix}

    # Project markers: check for key files at repo root
    project_files: set[str] = set()
    for marker in ("go.mod", "Cargo.toml", "package.json", "pyproject.toml",
                    "Dockerfile", "docker-compose.yml", "Makefile", "Justfile",
                    "terraform.tf", "helmfile.yaml", ".eslintrc.json"):
        if (Path.cwd() / marker).exists():
            project_files.add(marker)

    # Imports from code_intel (None when unavailable)
    imports: set[str] | None = None
    # TODO: integrate with code_intel.py imports when available

    return ReviewSignals(
        added_lines=f"{chr(10).join(diff_result.changed_files)}\n{added}",
        file_extensions=extensions,
        changed_files=diff_result.changed_files,
        imports=imports,
        project_files=project_files,
        file_count=diff_result.file_count,
        has_spec=bool(spec_content),
    )
```

#### 3b. Expert Matching

The selection uses binary activation (any signal fires = candidate) plus priority sort (more signals = higher rank), not weighted scoring. This matches the current regex system's behavior but with more signal sources. Weighted scoring is deferred until eval data shows binary activation selects wrong experts.

```python
def select_experts(
    signals: ReviewSignals,
    registry: ExpertRegistry,
    config: dict[str, Any],
) -> list[SelectedExpert]:
    """Select experts based on collected signals.

    Deterministic and testable — no LLM calls.
    """
    force_all, expert_flags = _expert_panel_config(config)
    disabled = {name for name, enabled in expert_flags.items() if enabled is False}

    # Expand disabled set to include experts whose aliases match a disabled name.
    # This handles config drift when experts are split: if a user has
    # `experts: {security-config: false}` and we add `authorization` with
    # `aliases: [security-config]`, disabling security-config also disables authorization.
    for expert in registry.load():
        if any(alias in disabled for alias in expert.aliases):
            disabled.add(expert.name)

    allowed_passes: set[str] | None = set(config.get("passes", [])) or None

    # Expand group aliases in allowed_passes (e.g., "security" -> individual experts)
    if allowed_passes is not None:
        expanded: set[str] = set()
        for p in allowed_passes:
            members = registry.group_members(p)
            if members:
                expanded.update(members)
            else:
                expanded.add(p)
        allowed_passes = expanded

    selected: list[SelectedExpert] = []

    for expert in registry.load():
        if expert.name in disabled:
            continue

        # requires_context check (e.g., spec-verification needs spec_content)
        if "spec_content" in expert.requires_context and not signals.has_spec:
            continue

        reasons: list[str] = []

        # Core experts always selected
        if expert.tier == "core":
            reasons.append("core expert")
        elif force_all:
            reasons.append("force_all")
        else:
            # Deactivation check
            if _should_deactivate(expert, signals):
                continue

            # Pattern matching
            for pattern in expert.activates_on.patterns:
                if re.search(pattern, signals.added_lines,
                             flags=re.IGNORECASE | re.MULTILINE):
                    reasons.append(f"pattern: {pattern[:50]}")
                    break

            # File type matching
            if expert.activates_on.file_types:
                overlap = set(expert.activates_on.file_types) & signals.file_extensions
                if overlap:
                    reasons.append(f"file_type: {overlap}")

            # Import matching (when code_intel available)
            if signals.imports and expert.activates_on.imports:
                overlap = set(expert.activates_on.imports) & signals.imports
                if overlap:
                    reasons.append(f"import: {overlap}")

            # Project marker matching
            for marker in expert.activates_on.project_markers:
                if marker in signals.project_files:
                    reasons.append(f"marker: {marker}")
                    break

            # Must have enough activation reasons (min_signals, default 1)
            if len(reasons) < expert.deactivates_on.min_signals:
                continue

        selected.append(SelectedExpert(
            name=expert.name,
            score=len(reasons),  # simple: more signals = higher rank
            reasons=reasons,
            prompt_file=expert.prompt_file,
            tier=expert.tier,
            domains=expert.domains,
        ))

    # Apply allowed_passes filter
    if allowed_passes is not None:
        known = registry.known_names
        invalid = sorted(allowed_passes - known)
        if invalid:
            raise ValueError(f"Unknown pass names: {', '.join(invalid)}")
        selected = [e for e in selected if e.name in allowed_passes]
        if not selected:
            raise ValueError("No review passes remain after applying configuration.")
        return selected  # no cap when user explicitly selects passes

    # Sort by signal count (descending), cap at max_experts
    selected.sort(key=lambda e: (-1 if e.tier == "core" else 0, -e.score))
    max_experts = config.get("max_experts", 6)
    return selected[:max_experts]


def _should_deactivate(expert: ExpertMeta, signals: ReviewSignals) -> bool:
    """Check if an expert should be suppressed despite activation signals."""
    rules = expert.deactivates_on

    # max_files: skip if diff touches <= max_files files
    if rules.max_files is not None and signals.file_count <= rules.max_files:
        return True

    # file_types_only: skip if ALL changed files match these extensions
    if rules.file_types_only:
        only_exts = set(rules.file_types_only)
        if signals.file_extensions and signals.file_extensions.issubset(only_exts):
            return True

    # ignore_paths: skip if ALL changed files match ignore patterns
    # e.g., ["**/test_*", "**/*_test.*", "**/tests/**"] suppresses activation
    # when the only matches are in test files
    if rules.ignore_paths:
        from fnmatch import fnmatch
        all_ignored = all(
            any(fnmatch(f, pat) for pat in rules.ignore_paths)
            for f in signals.changed_files
        )
        if all_ignored:
            return True

    return False
```

#### 3c. Coverage Gap Detection

```python
def detect_coverage_gaps(
    selected: list[SelectedExpert],
    registry: ExpertRegistry,
    signals: ReviewSignals,
) -> list[str]:
    """Identify domains present in the diff that no selected expert covers.

    A domain is "detected" if ANY expert in the registry has at least one
    activation signal triggered for it, regardless of whether that expert
    was selected (it may have been suppressed by deactivation rules, disabled
    by config, or cut by max_experts cap).
    """
    # Detect all domains present in the diff
    detected_domains: set[str] = set()
    for expert in registry.load():
        if _any_activation_signal(expert, signals):
            detected_domains.update(expert.domains)

    # Domains covered by selected experts
    covered_domains: set[str] = set()
    for expert in selected:
        covered_domains.update(expert.domains)

    uncovered = detected_domains - covered_domains
    return sorted(uncovered)


def _any_activation_signal(expert: ExpertMeta, signals: ReviewSignals) -> bool:
    """Check if any single activation signal fires for this expert."""
    for pattern in expert.activates_on.patterns:
        if re.search(pattern, signals.added_lines, flags=re.IGNORECASE | re.MULTILINE):
            return True
    if expert.activates_on.file_types:
        if set(expert.activates_on.file_types) & signals.file_extensions:
            return True
    if signals.imports and expert.activates_on.imports:
        if set(expert.activates_on.imports) & signals.imports:
            return True
    for marker in expert.activates_on.project_markers:
        if marker in signals.project_files:
            return True
    return False
```

### Layer 4: Judge Coverage Awareness

The orchestrator injects an expert coverage summary into the judge prompt context. This is informational markdown — not machine-parsed.

```python
def render_coverage_map(
    selected: list[SelectedExpert],
    uncovered_domains: list[str],
) -> str:
    """Render expert coverage map for the judge prompt.

    Token budget: ~200-400 tokens. Injected into judge context (80k budget).
    """
    lines = ["## Expert Coverage Map", "",
             "| Expert | Tier | Domains | Activation Signals |",
             "|--------|------|---------|-------------------|"]
    for e in selected:
        domains_str = ", ".join(e.domains[:5]) or "general"
        reasons_str = "; ".join(e.reasons[:3])
        lines.append(f"| {e.name} | {e.tier} | {domains_str} | {reasons_str} |")

    lines.append("")
    if uncovered_domains:
        lines.append("### Uncovered Domains")
        for d in uncovered_domains:
            lines.append(f"- **{d}**: detected in diff but no specialist expert ran")
        lines.append("")
        lines.append("Apply extra scrutiny to findings in uncovered domains.")
    else:
        lines.append("### Uncovered Domains")
        lines.append("None — all detected domains have expert coverage.")

    return "\n".join(lines)
```

### User-Facing Expert Panel in Report

The coverage map above is for the judge (internal). The user also needs visibility into which experts reviewed their code. Add an **Expert Panel** section to the final report (rendered by `finalize()`):

```markdown
## Expert Panel

This review used 5 experts selected based on your code changes:

| Expert | Why it ran |
|--------|-----------|
| correctness | Core expert (always runs) |
| security-config | Core expert (always runs) |
| test-adequacy | Core expert (always runs) |
| concurrency | Detected: `async def`, `threading.Lock` in 3 files |
| database | Detected: SQL queries, `ALTER TABLE` in migrations/0042.py |

No coverage gaps detected.
```

This is rendered by a new `render_report_expert_panel()` function in `orchestrate.py finalize()`, using the same `SelectedExpert` data. It replaces the current opaque "X explorers ran" log line with user-visible accountability. Users who see "the database expert caught 2 migration safety issues" trust the tool more and understand the value of the expanded roster.

**Report template change:** Add after the summary block (Feature 3 in the quality/compliance spec) and before the detailed findings section.

### Wave Assignment

All selected experts run in a **single parallel wave**. The `max_experts` cap (default 6) bounds parallelism. Multi-wave expert scheduling (e.g., run security first, then domain experts) is not implemented — the current architecture has explorers that are independent; they don't see each other's findings. The judge is the synthesis point.

This matches the current behavior: `waves = [{"wave": 1, "tasks": wave_tasks}]` at orchestrate.py:2267.

### Interaction with Context Enrichment Plan

The context enrichment plan (`docs/plan-context-enrichment.md`) introduces `reviewer-context-planner.md` — an LLM-based planner that generates cross-file context queries. This runs **before** expert selection as part of the context gathering phase (Step 2 in the pipeline).

The context planner is NOT an expert — it does not produce findings. It produces context (cross-file query results) that gets injected into the context packet for all experts. Expert selection is independent of the context planner:

- Context planner runs → produces cross-file context
- Expert selection runs → produces expert panel
- Both outputs feed into context packet assembly
- No dependency between them; they can run in parallel

If `code_intel.py imports` becomes available (context enrichment Feature F0c), expert selection gains import-based matching via `ReviewSignals.imports`. This is the only interaction point — and it degrades gracefully when unavailable.

### Compatibility with Verification Pipeline

The verification architecture spec (`specs/verification-architecture.md`) introduces a 3-stage verification round (F0), two-pass judge (F1), and fix validation (F5). Expert selection is fully independent of these features:

- **F0 (verification round):** Runs on findings from whatever experts were selected. No interaction with selection.
- **F1 (two-pass judge):** Judge structure is independent of which experts ran.
- **F5 (fix validation):** Validates fixes from all experts, regardless of source.

Expert selection produces the expert panel; the verification pipeline processes whatever findings that panel generates. The two systems never need to know about each other.

### SKILL.md Changes

The current Step 3.5 (Adaptive Pass Selection) in SKILL.md is replaced with the orchestrate.py-driven selection. The SKILL.md text should read:

```
Step 3.5: Expert Panel Assembly

Expert selection is handled by orchestrate.py prepare() via the ExpertRegistry.
The launch.json output includes the selected expert panel with activation reasons.

The selected experts are listed in launch.json under waves[].tasks[]. Each task
includes: name, prompt_file, model, activation_reason, core (boolean).

If force_all_passes: true in config, all registered experts are activated.

Log output: "Expert panel: correctness, security-config, test-adequacy, concurrency,
shell-script (5 experts)"
```

No manual expert assembly logic remains in SKILL.md — it reads the panel from launch.json.

### Integration with Existing Code

The new selection system integrates at these specific points in `orchestrate.py`:

| Current code | Location | Change |
|-------------|----------|--------|
| `EXPERT_PROMPT_FILES` dict | Line 83 | Replaced by `ExpertRegistry.get(name).prompt_file` |
| `EXTENDED_EXPERT_PATTERNS` dict | Line 95 | Patterns move into frontmatter `activates_on.patterns`; dict kept as fallback |
| `CORE_EXPERTS` list | Line 82 | Core status moves into frontmatter `tier: core`; list kept as fallback |
| `_build_expert()` | Line 887 | Accepts `ExpertMeta` or `SelectedExpert` instead of looking up `EXPERT_PROMPT_FILES[name]` |
| `assemble_expert_panel()` | Line 904 | Delegates to `select_experts()` when registry has entries; falls back to current logic otherwise |
| `_expert_panel_config()` | Line 869 | Unchanged — `select_experts()` calls it for `force_all` and disabled experts |
| `_prompt_path_for_expert()` | Line 953 | Uses registry instead of `EXPERT_PROMPT_FILES` dict |
| `validate_prompt_files()` | Line 1573 | Validates against registry instead of hardcoded dict |
| `assemble_expert_panel()` `security` alias | Line 934 | Replaced by `groups: [security]` frontmatter + `registry.group_members()` |
| `spec-verification` conditional | Line 925 | Replaced by `requires_context: [spec_content]` frontmatter |
| `CONFIG_ALLOWLIST` | Line 108 | Add `"max_experts"` |
| Coverage map | N/A (new) | `render_coverage_map()` output injected into judge prompt in `post_explorers()`. ~200-400 tokens, within judge's 80k budget. Not in the P0-P9 explorer cascade — this is judge-only context, appended after explorer findings. |

### Return value compatibility

`select_experts()` returns `list[SelectedExpert]`. The existing pipeline expects `list[dict[str, Any]]` with keys `name`, `prompt_file`, `model`, `core`, `activation_reason`. A bridge function converts:

```python
def selected_expert_to_panel_entry(
    expert: SelectedExpert, config: dict[str, Any]
) -> dict[str, Any]:
    """Convert SelectedExpert to the dict format expected by build_launch_packet()."""
    return {
        "name": expert.name,
        "prompt_file": expert.prompt_file,
        "model": config.get("pass_models", {}).get(expert.name)
        or (
            config.get("pass_models", {}).get("security", "sonnet")
            if expert.name.startswith("security-")
            else "sonnet"
        ),
        "core": expert.tier == "core",
        "activation_reason": "; ".join(expert.reasons),
    }
```

---

## Degradation Matrix

| Failure | Capabilities Lost | Fallback Behavior | Log Message |
|---------|-------------------|-------------------|-------------|
| **PyYAML not installed** | Frontmatter parsing, ExpertRegistry | Use hardcoded `CORE_EXPERTS` + `EXTENDED_EXPERT_PATTERNS` (current behavior) | `"yaml not available — using hardcoded expert definitions"` |
| **Single expert frontmatter malformed** | Structured selection for that expert | Expert available via `EXTENDED_EXPERT_PATTERNS` if it has an entry; otherwise skipped | `"Failed to parse frontmatter for {file} — falling back to pattern match"` |
| **All frontmatter parsing fails** | Structured selection for all experts | Full fallback to hardcoded dicts | `"All frontmatter parsing failed — using hardcoded expert definitions"` |
| **code_intel.py imports unavailable** | Import-based matching | `signals.imports` is `None`; import block skipped, pattern/file-type matching still works | `"code_intel imports unavailable — expert selection uses pattern matching only"` |
| **No extended experts match** | Extended expert coverage | Only core experts run; gap detection reports uncovered domains | Normal behavior — no special log |
| **All experts disabled by config** | Entire review | `ValueError` raised (existing behavior) | `"No review passes remain after applying configuration."` |

**Key principle:** No failure in the expert selection system causes the review to fail. Each capability degrades to its predecessor behavior.

---

## Logging / Observability

Expert selection emits structured progress events via the existing `progress()` function to stderr.

| Event | When | Payload |
|-------|------|---------|
| `expert_registry_loaded` | After registry.load() | `{total, from_frontmatter, from_fallback}` |
| `expert_frontmatter_warning` | Parse failure | `{file, error}` |
| `expert_selected` | Expert passes selection | `{name, tier, score, reasons}` |
| `expert_deactivated` | Suppressed by rule | `{name, rule}` |
| `expert_skipped` | Below threshold / disabled | `{name, reason}` |
| `expert_selection_complete` | After select_experts() | `{selected: [names], count, max, uncovered_domains}` |

**Debugging "why didn't X activate?":** Check stderr JSON. `expert_skipped` shows why. `expert_deactivated` shows which rule triggered.

---

## Implementation Plan

### Wave 0: Frontmatter + Registry (no behavior change)

1. Split `reviewer-reliability-performance-pass.md` into `reviewer-shell-script-pass.md` and `reviewer-reliability-pass.md`
2. Add YAML frontmatter to all 10 existing expert prompts (patterns copied from `EXTENDED_EXPERT_PATTERNS`)
3. Implement `parse_expert_frontmatter()` and `ExpertRegistry` in orchestrate.py
4. Implement `collect_review_signals()` and `select_experts()` in orchestrate.py
5. Wire `assemble_expert_panel()` to delegate to `select_experts()` when registry has entries
6. Update `_prompt_path_for_expert()` and `validate_prompt_files()` to use registry
7. Replace `security` alias expansion with `groups: [security]` mechanism
8. Replace `spec-verification` conditional with `requires_context: [spec_content]`
9. Add `"max_experts"` to `CONFIG_ALLOWLIST`
10. Replace SKILL.md Step 3.5 text with new "Expert Panel Assembly" block (see SKILL.md Changes section)
11. Tests: golden-file regression (see acceptance criteria)

### Wave 1: Expanded Roster + Language Checklists

**New experts (5):**
1. Write `reviewer-database-pass.md` — migrations, N+1, index analysis, schema evolution
2. Write `reviewer-infrastructure-pass.md` — Dockerfiles, K8s manifests, Terraform, Helm, CI configs
3. Write `reviewer-frontend-pass.md` — component lifecycle, hydration, rendering, bundle config
4. Write `reviewer-accessibility-pass.md` — ARIA, focus management, screen reader compat
5. Write `reviewer-ai-integration-pass.md` — LLM trust boundaries, prompt injection, token budget, model config
6. Each prompt includes frontmatter with activation signals
7. Registry auto-discovers new prompts (no code change needed)

**Language checklists (curated from Baz Awesome Reviewers):**
8. `references/checklist-go.md` — 25 items, 7 categories (done)
9. `references/checklist-python.md` — 24 items, 6 categories (done)
10. Curate `references/checklist-typescript.md` — from 673 Baz TS rules
11. Curate `references/checklist-rust.md` — from 273 Baz Rust rules
12. Curate `references/checklist-java.md` — from 135 Baz Java rules
13. Language checklists are injected into the context packet based on file extensions in the diff (reuses existing `select_checklists()` pattern with a language-extension mapping)

**Domain checklists for Tier 2 folding:**
14. `references/checklist-performance.md` — N+1, BigO, allocation hotspots (folds performance expert into reliability/correctness)
15. `references/checklist-observability.md` — structured logging, trace propagation, metric cardinality (folds into reliability)
16. `references/checklist-dependencies.md` — major version bumps, lockfile, license (folds into security-config)

**Tests:**
17. Each new expert: integration test with a fixture diff that activates it
18. Each new expert: test that single-line typo diff does NOT activate it

### Wave 2: Judge Coverage Awareness + Report + Documentation

1. Implement `detect_coverage_gaps()` and `render_coverage_map()`
2. Inject coverage map into judge prompt in `post_explorers()`
3. Add `### 4e. Coverage Completeness` section to judge prompt
4. Implement `render_report_expert_panel()` — user-facing expert panel section in the final report
5. Add `source_expert` field to finding schema; set in `post_explorers()` when collecting explorer output; instruct judge to preserve it
6. Update `references/report-template.md` — add Expert Panel section after summary, before findings
7. Update `references/findings-schema.json` — add `source_expert: str` field
8. Update `references/design.md` — add rationale entries for: adaptive panel architecture, shell expert provenance (CodeRabbit gap analysis), language checklists approach, Tier 2 folding decisions
9. Update `references/acceptance-criteria.md` — add scenarios: shell files activate shell expert, no shell files skip it, force_all overrides, shell expert finds set-e interaction bug, shell expert finds JSON injection, infrastructure expert activates on Dockerfile
10. Update `docs/CONFIGURATION.md` — document `max_experts`, `experts: {name: false}`, `expert_registry: false` rollback flag
11. Tests: coverage map rendering, gap detection, report expert panel, `source_expert` preservation through judge

### Post-Wave 2: Remove Legacy Fallback

Once all experts have frontmatter and regression tests pass for 2+ weeks:
1. Remove `EXTENDED_EXPERT_PATTERNS` dict
2. Remove `CORE_EXPERTS` list
3. Remove `EXPERT_PROMPT_FILES` dict
4. Remove fallback paths in `assemble_expert_panel()`

---

## Acceptance Criteria

### Wave 0: Frontmatter + Registry
- [ ] All 10 existing expert prompts have valid YAML frontmatter
- [ ] `parse_expert_frontmatter()` returns `ExpertMeta` for each prompt
- [ ] `ExpertRegistry.load()` returns 10 entries (after the reliability/shell-script split)
- [ ] Frontmatter-less prompt → parser returns `None` (no crash)
- [ ] Malformed YAML → parser returns `None` + logs warning
- [ ] **Golden-file regression:** for 5 representative test diffs, `select_experts()` produces the same expert name sets as the current `assemble_expert_panel()`. Test written BEFORE any code changes.
- [ ] `--passes security` expands to `{security-dataflow, security-config}` via `groups:`
- [ ] `--passes concurrency` works for frontmatter-discovered expert
- [ ] `experts: {concurrency: false}` in config disables concurrency expert
- [ ] `spec-verification` expert skipped when no spec provided
- [ ] `EXTENDED_EXPERT_PATTERNS` still used as fallback when `yaml` not installed

### Wave 1: Expanded Roster + Language Checklists
- [ ] 5 new `reviewer-*-pass.md` files: database, infrastructure, frontend, accessibility, ai-integration
- [ ] `ExpertRegistry.load()` returns 15 entries
- [ ] Each new expert: integration test with a fixture diff that activates it
- [ ] Each new expert: test that single-line typo diff does NOT activate it
- [ ] No new expert overlaps with an existing expert for >80% of activation patterns
- [ ] Language checklists exist: `checklist-go.md`, `checklist-python.md`, `checklist-typescript.md` (minimum 3)
- [ ] Language checklists injected into context packet when matching file extensions appear in diff
- [ ] Domain checklists exist for Tier 2 folding: `checklist-performance.md`, `checklist-observability.md` (minimum 2)
- [ ] Each checklist has provenance tracing to source Baz rules

### Wave 2: Judge Coverage Awareness + Report
- [ ] `detect_coverage_gaps()` returns `["terraform"]` for a diff with `.tf` files when no infrastructure expert is selected
- [ ] Coverage map renders as valid markdown
- [ ] Coverage map is present in judge prompt context
- [ ] Judge prompt contains "Uncovered Domains" section
- [ ] Final report contains "Expert Panel" section listing which experts ran and why
- [ ] Every finding in judge output has `source_expert` field set
- [ ] `source_expert` field is preserved through judge (not stripped by Gatekeeper/Verifier/Calibrator/Synthesizer)
- [ ] `error-handling` expert does NOT activate on a diff where all `except` matches are in test files (via `ignore_paths`)
- [ ] `error-handling` expert does NOT activate when only 1 pattern matches (via `min_signals: 2`)

---

## Performance Budget

| Operation | Expected | Hard Limit |
|-----------|----------|------------|
| Frontmatter parsing (all experts) | <25ms | 100ms |
| Signal collection | <50ms | 200ms |
| Expert scoring | <5ms | 50ms |
| Gap detection | <1ms | 5ms |
| Coverage map rendering | <1ms | 5ms |

**Total prepare phase impact:** <80ms. Negligible relative to existing prepare (~2-5s).

---

## Config Schema Changes

Add to `DEFAULT_CONFIG`:
```python
"max_experts": 6,      # int: max experts per review (core + extended)
```

Add to `CONFIG_ALLOWLIST`:
```python
"max_experts",
```

**Rollback flag:** If the new selection causes regressions, set `expert_registry: false` in `.codereview.yaml` to force full fallback to hardcoded dicts. Add `"expert_registry"` to `CONFIG_ALLOWLIST`.

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Frontmatter parsing breaks existing prompts | Fallback to hardcoded dicts; golden-file regression test |
| New experts produce noise | Each expert has calibration examples; judge deduplicates |
| Selection produces different experts than before | Golden-file test written BEFORE changes; reviewed explicitly |
| `yaml` not installed → different behavior | Explicit degradation path; logged |
| Expanding roster increases maintenance | Standardized template; activation + non-activation tests per expert |

---

## Decisions

### Language coverage via checklists, not standalone experts

Language-specific footguns (Go goroutine leaks, Python mutable default args, TypeScript `any` leakage) are handled via **language checklists injected into whichever experts activate**, not via standalone per-language experts.

Rationale: The existing concern-oriented experts (correctness, concurrency, error-handling) already do the investigation work — they trace callers, check boundaries, verify error paths. What they lack is a prompt telling them to look for language-specific traps. A 15-20 item checklist (~300-500 tokens) appended to the expert prompt fixes this without adding a parallel expert that duplicates the investigation.

**Checklist files:** `references/checklist-{language}.md` (e.g., `checklist-go.md`, `checklist-python.md`)
**Source:** Curated from Baz Awesome Reviewers (4,468 rules mined from real GitHub PR discussions), filtered for `comments_count >= 4` and labels in bug-producing categories.
**Injection:** By file extension in the diff → inject into all activated experts (Spec B enrichment mechanism). Until Spec B ships, SKILL.md can inject manually in the context packet.

### New expert roster: Tier 1 only

Add 5 new domain experts where no existing expert provides coverage:
- `database` — migrations, N+1, index analysis, schema evolution
- `infrastructure` — Dockerfiles, K8s manifests, Terraform, Helm, CI configs
- `frontend` — component lifecycle, hydration, rendering, bundle config
- `accessibility` — ARIA, focus management, screen reader compat
- `ai-integration` — LLM trust boundaries, prompt injection, token budget, model config

Tier 2 candidates (performance, authorization, data-pipeline, observability, dependency-management, mobile, migration-safety) are deferred — their value is better delivered by enriching existing experts with domain checklists.

### Tier 2 folding into existing experts

Rather than creating standalone experts, fold Tier 2 domain knowledge into existing experts via checklists:

| Tier 2 candidate | Fold into | How |
|-------------------|-----------|-----|
| performance | reliability + correctness | `checklist-performance.md` (N+1, BigO, allocation hotspots) |
| authorization | security-config + security-dataflow | Expand existing security prompts with authz checklist items |
| data-pipeline | correctness | `checklist-data-pipeline.md` (idempotency, backpressure, schema evolution) |
| observability | reliability | `checklist-observability.md` (structured logging, trace propagation, metric cardinality) |
| dependency-management | security-config | `checklist-dependencies.md` (major version bumps, lockfile, license) |
| migration-safety | database | Subsection of the database expert prompt |

---

## Open Questions

1. **Should we adopt OCR persona-tier?** Kent Beck / Sandi Metz provide a different review lens. The Wharton research suggests caution with identity framing, but OCR's versions focus on review criteria, not roleplay.

2. **How many language checklists to ship in Wave 1?** Candidates: Go, Python, TypeScript, Rust, Java, Ruby. Could start with Go + Python + TypeScript (highest usage) and add others based on demand.
