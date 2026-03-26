# Plan: Code Review Skill v1.3

Eight features in two groups to improve review quality and reduce agent interpretation variance. Feature 0 is the foundation — it extracts mechanical pipeline steps into scripts. Group A (Features 1-3) enriches explorer context with new data sources. Group B (Features 4-7) improves explorer and judge behavior through prompt and architecture changes. All features are independent of each other but Feature 0 should be done first to establish the scripting pattern and minimize merge conflicts in SKILL.md.

### Relationship to v1.2

| v1.2 Feature | Disposition |
|-------------|-------------|
| **0: Script extraction** | Carried forward as v1.3 Feature 0 (identical scope) |
| **1: Git history risk** | Carried forward as v1.3 Feature 3 (unchanged except Tier 1 promotion interaction with large-diff chunking, which now exists on this branch) |
| **2: Test coverage data** | **Dropped.** The `coverage.run_tests: false` path (check existing artifacts) was the default, but in practice coverage artifacts are stale or absent in most repos. The test-adequacy explorer already identifies untested functions by reading test files — measured coverage adds complexity without proportional benefit. If coverage data becomes important later, it can be re-scoped as a v1.4 feature. |
| **3: Finding lifecycle** | **Being built separately** by another team. This plan assumes `scripts/lifecycle.py` will exist and consume the enriched findings JSON from Feature 0b's `enrich-findings.py`. The interface contract: `lifecycle.py` reads the output of `enrich-findings.py` (JSON with `findings` array where each finding has `id`, `source`, `pass`, `severity`, `confidence`, `file`, `line`, `summary`, `action_tier`, plus optional fields). |
| **4: Multi-model council** | **Deferred** to v1.4. The single-model adversarial judge provides good precision. Multi-model adds cost and complexity that isn't justified until the single-model review is battle-tested. |

### Design Principles

Carried forward from v1.2:

**Scripts Over Prompts** — Wherever a step is mechanical (deterministic rules, data transformation, tool invocation, arithmetic), implement it as a script. This eliminates agent divergence and makes the pipeline testable.

**Use scripts for:** Tool detection/invocation, data transformation, rule-based classification, hash computation, file I/O and artifact management.

**Use AI for:** Understanding code semantics, investigating call paths and data flow, assessing severity, cross-cutting synthesis, report narration.

**The boundary is judgment.** If a step requires reading code and reasoning about behavior, it's an AI task. If it's applying a formula or running a tool, it's a script.

New in v1.3:

**Checklists Over Instructions** — When giving AI explorers domain-specific context, provide concrete checklist items (questions to answer, patterns to look for) rather than open-ended instructions. Checklists constrain investigation scope and produce more consistent findings across runs.

### Script dependencies

**Required:** `bash` 3.2+ (macOS default), `python3` 3.8+ (for `code_intel.py`, `prescan.py`, `enrich-findings.py` — if absent, agent falls back to manual execution for those steps), `jq` 1.6+ (for `run-scans.sh` JSON manipulation — hard dependency for that script).

**Optional (enhanced analysis):** `tree-sitter` + language grammars (`pip install tree-sitter tree-sitter-python tree-sitter-go tree-sitter-typescript tree-sitter-java tree-sitter-rust`) — enables structural code analysis across all languages. Without tree-sitter, `code_intel.py` falls back to `radon`/`gocyclo` for complexity and regex for other subcommands.

**Optional (deterministic tools):** `radon`, `gocyclo`, `shellcheck`, `semgrep`, `trivy`, `osv-scanner` — all degrade gracefully when missing. When semgrep is not installed, `code_intel.py patterns` provides a lightweight fallback for the most common static analysis checks.

---

## Feature 0: Extract Existing Pipeline Steps into Scripts

**Goal:** Move mechanical steps that are currently described as agent instructions in SKILL.md into executable scripts. This reduces agent interpretation variance, makes the pipeline testable, and establishes the scripting pattern for Features 1-4.

### Scripts to extract

#### 0a. `scripts/run-scans.sh` — Deterministic scan orchestration (from Step 3)

Currently, `references/deterministic-scans.md` is a reference document that the agent reads and reimplements each time. Different runs may execute tools in different order, handle errors differently, or miss tools entirely.

**Extract into a single script that:**

1. Accepts `CHANGED_FILES` (newline-delimited, via stdin or file arg) and `BASE_REF` (env var or flag)
2. Detects available tools (`semgrep`, `trivy`, `osv-scanner`, `shellcheck`, `pre-commit`, `sonarqube`)
3. Sets up sandbox/cache dirs (TRIVY_CACHE_DIR, SEMGREP_HOME, etc.)
4. Runs available tools in parallel where possible (semgrep + sonarqube)
5. Normalizes each tool's output into the standard finding shape (JSON object — see normalization spec below)
6. Deduplicates on `file:line:summary` key
7. Outputs two things:
   - `stdout`: JSON object with `{ "findings": [...], "tool_status": {...} }`
   - `stderr`: human-readable progress log

**Requires:** `jq` for JSON parsing and construction. If `jq` is not available, the script exits with an error message: "jq is required for run-scans.sh — install via: brew install jq / apt install jq". This is a hard dependency — JSON normalization without jq is unreliable.

**Interface:**
```bash
echo "$CHANGED_FILES" | bash scripts/run-scans.sh --base-ref "$BASE_REF" > /tmp/codereview-scans.json
```

**Normalization spec — mapping tool outputs to standard finding shape:**

Each tool finding is normalized into:
```json
{
  "source": "deterministic",
  "pass": "<mapped category>",
  "severity": "<mapped severity>",
  "confidence": 1.0,
  "file": "<path>",
  "line": 0,
  "summary": "<description>",
  "evidence": "<tool-specific detail>",
  "sources": ["<tool_name>"]
}
```

**Severity mapping (tool-native → standard):**

| Tool | Tool Severity | Standard Severity |
|------|--------------|-------------------|
| semgrep | ERROR | high |
| semgrep | WARNING | medium |
| semgrep | INFO | low |
| trivy | CRITICAL | critical |
| trivy | HIGH | high |
| trivy | MEDIUM | medium |
| trivy | LOW | low |
| osv-scanner | (by CVSS ≥9) | critical |
| osv-scanner | (by CVSS 7-8.9) | high |
| osv-scanner | (by CVSS 4-6.9) | medium |
| osv-scanner | (by CVSS <4) | low |
| shellcheck | error | high |
| shellcheck | warning | medium |
| shellcheck | info | low |
| sonarqube | BLOCKER, CRITICAL | critical |
| sonarqube | MAJOR | high |
| sonarqube | MINOR | medium |
| sonarqube | INFO | low |

**Pass mapping:** All deterministic findings use `pass: "security"` for vulnerability scanners (trivy, osv-scanner, semgrep security rules), `pass: "reliability"` for shellcheck and pre-commit, `pass: "correctness"` for semgrep code-pattern rules and sonarqube bugs, `pass: "maintainability"` for sonarqube code smells. When the tool provides a category/rule ID, use the most specific mapping available; default to `"security"` for vulnerability-class tools and `"correctness"` for everything else.

**What stays in the agent's hands:** The agent still calls the script from Step 3 and reads the output. The agent does NOT re-interpret `deterministic-scans.md` — it runs the script and consumes the JSON.

**`references/deterministic-scans.md` becomes:** Reference documentation only (explaining what each tool does, when to install them, etc.). No longer the source of executable logic.

#### 0b. `scripts/enrich-findings.py` — Finding enrichment and classification (from Step 5)

Currently, the agent performs Step 5 by reading SKILL.md rules and applying them. This is the most divergence-prone step — agents make different tier assignment choices, skip deduplication steps, or miscalculate severity weights.

**Extract the mechanical parts into a Python script that:**

1. Accepts the judge's output JSON (via `--judge-findings`) — expects the full judge output object with a `findings` key (JSON array). The script extracts `.findings` from the object. Also accepts deterministic findings JSON (via `--scan-findings`) — expects the output of `run-scans.sh` (object with `findings` key).
2. Combines both findings arrays into one list
3. Assigns `source` field: `"ai"` for judge findings (unless already set), `"deterministic"` for scan findings (already set by `run-scans.sh`)
4. Generates stable `id` for each finding: `<pass>-<file-hash>-<line>` where `<file-hash>` is first 4 chars of SHA-256 of the file path
5. Applies confidence floor (drops AI findings below threshold, default 0.65)
6. Applies evidence check (high/critical without `failure_mode` → downgrade to medium)
7. Assigns `action_tier` mechanically: Must Fix / Should Fix / Consider per the rules table
8. Ranks within each tier by `severity_weight * confidence`
9. Computes `tier_summary` counts
10. Outputs enriched findings JSON to stdout

**Output format** (consumed by `lifecycle.py` from v1.2 Feature 3):
```json
{
  "findings": [
    {
      "id": "security-a3f1-42",
      "source": "ai",
      "pass": "security",
      "severity": "high",
      "confidence": 0.88,
      "file": "src/auth/login.py",
      "line": 42,
      "summary": "...",
      "evidence": "...",
      "failure_mode": "...",
      "fix": "...",
      "tests_to_add": [],
      "test_category_needed": [],
      "action_tier": "must_fix"
    }
  ],
  "tier_summary": { "must_fix": 1, "should_fix": 3, "consider": 2 },
  "dropped": { "below_confidence_floor": 2, "downgraded_to_medium": 1 }
}
```

The `dropped` object provides transparency about what was filtered and why, so downstream consumers (and humans debugging the pipeline) can understand the enrichment decisions.

**Interface:**
```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/codereview-judge.json \
  --scan-findings /tmp/codereview-scans.json \
  --confidence-floor 0.65 \
  > /tmp/codereview-enriched.json
```

**What stays in the agent's hands:**
- Step 5a item 5: Deduplication by "same root cause" — this requires judgment about whether two findings describe the same underlying issue with different wording. The agent does this BEFORE passing findings to the script.
- Step 5a item 6: "No linter restatement" — detecting that a finding restates what a linter already catches requires understanding the finding's content. The agent does this BEFORE passing findings to the script.

The agent runs dedup and linter-restatement removal first (using AI judgment), then pipes the clean list to `enrich-findings.py` for mechanical enrichment.

#### 0c. `scripts/code_intel.py` — Shared code intelligence module (replaces `complexity.sh`)

Originally this feature was `complexity.sh` — a bash script wrapping `radon` (Python) and `gocyclo` (Go). This left TypeScript, Java, Rust, and every other language without complexity analysis.

`code_intel.py` replaces `complexity.sh` and becomes the shared code intelligence layer that multiple pipeline steps use. It provides language-agnostic structural analysis via **tree-sitter** (optional, with fallback to `radon`/`gocyclo`/regex when tree-sitter is not installed).

**What it provides:**

| Subcommand | What it extracts | Used by |
|-----------|-----------------|---------|
| `complexity` | Per-function cyclomatic complexity (all languages) | Step 2d (replaces radon/gocyclo calls) |
| `functions` | Function definitions: name, params, return type, line range, exported/private | Step 2a-2b (replaces ad-hoc agent Grep for callers) |
| `imports` | Import/require/use statements: module, names, line | Step 2-L Phase A (reliable cross-chunk interface detection) |
| `exports` | Public API surface: exported functions, classes, types | Step 3.5 (structural adaptive pass selection) |
| `callers` | Call sites for a given function name, with file + line + context | Step 2b (replaces ad-hoc agent Grep) |
| `patterns` | Lightweight static analysis checks (semgrep fallback) | Step 3 (when semgrep not installed) |

**Interface (subcommand-based CLI):**

```bash
# Cyclomatic complexity (replaces complexity.sh)
echo "$CHANGED_FILES" | python3 scripts/code_intel.py complexity > /tmp/codereview-complexity.json

# Function definitions for context gathering
echo "$CHANGED_FILES" | python3 scripts/code_intel.py functions > /tmp/codereview-functions.json

# Import graph for cross-chunk interface detection
echo "$CHANGED_FILES" | python3 scripts/code_intel.py imports > /tmp/codereview-imports.json

# Public API surface for adaptive pass selection
echo "$CHANGED_FILES" | python3 scripts/code_intel.py exports > /tmp/codereview-exports.json

# Callers of a specific function (replaces agent Grep)
echo "$CHANGED_FILES" | python3 scripts/code_intel.py callers --target "login" > /tmp/codereview-callers.json

# Lightweight static analysis (semgrep fallback)
echo "$CHANGED_FILES" | python3 scripts/code_intel.py patterns > /tmp/codereview-patterns.json
```

All subcommands read `CHANGED_FILES` from stdin (newline-delimited) and output JSON to stdout.

**Output formats:**

```json
// complexity
{
  "analyzer": "tree-sitter|radon|gocyclo|mixed",
  "hotspots": [
    { "file": "src/auth/login.py", "function": "validate_session", "score": 15, "rating": "C", "line": 42 }
  ],
  "tool_status": { "tree_sitter": "ran|not_installed", "radon": "ran|not_installed", "gocyclo": "ran|not_installed" }
}

// functions
{
  "functions": [
    { "file": "src/auth/login.py", "name": "validate_session", "params": ["request", "token"], "returns": "Session|None",
      "line_start": 42, "line_end": 87, "exported": true, "language": "python" }
  ]
}

// imports
{
  "imports": [
    { "file": "src/auth/login.py", "module": "src.models.user", "names": ["User", "UserRole"], "line": 3 }
  ]
}

// exports
{
  "exports": [
    { "file": "src/auth/login.py", "name": "validate_session", "kind": "function", "line": 42 },
    { "file": "src/auth/login.py", "name": "Session", "kind": "class", "line": 10 }
  ]
}

// callers
{
  "target": "login",
  "call_sites": [
    { "file": "src/api/views.py", "caller": "handle_request", "line": 78, "context": "result = login(request.username, request.password)" }
  ]
}

// patterns (semgrep fallback — see below)
{
  "analyzer": "tree-sitter|regex-only",
  "findings": [
    { "pattern": "sql-injection", "severity": "high", "file": "src/api/orders.py", "line": 34,
      "summary": "String concatenation in execute() call", "evidence": "cursor.execute(\"SELECT * FROM orders WHERE id=\" + order_id)" }
  ],
  "tool_status": { "tree_sitter": "ran|not_installed" }
}
```

**`patterns` subcommand — lightweight semgrep fallback:**

When semgrep is not installed, `code_intel.py patterns` provides 6 high-value checks that cover the most common semgrep findings:

| Pattern | What tree-sitter catches | Regex fallback |
|---------|------------------------|----------------|
| `sql-injection` | String concatenation/f-string inside `execute()`/`query()`/`raw()` call arguments | `execute\(.*[+f]` |
| `command-injection` | String concatenation inside `exec()`/`system()`/`subprocess.run()`/`os.popen()` call arguments | `(exec\|system\|popen\|subprocess)\(.*[+f]` |
| `unused-import` | Import nodes with no reference to imported name in file body | N/A (too noisy without AST) |
| `unreachable-code` | Statements after `return`/`throw`/`raise` in same block (excluding comments/docstrings) | N/A (too noisy without AST) |
| `resource-leak` | `open()`/`connect()`/`acquire()` without matching `close()`/`release()` in same function scope | N/A (requires scope analysis) |
| `empty-error-handler` | Error-handling nodes (try/catch/except) with empty or pass-only bodies | `except.*:\s*pass`, `catch\s*\([^)]*\)\s*\{\s*\}` |

The `patterns` subcommand output uses the same finding shape as `run-scans.sh` (with `source: "deterministic"`, `confidence: 1.0`). This means `run-scans.sh` can call `code_intel.py patterns` as part of its scan bundle when semgrep is not installed, and the findings merge seamlessly.

**Architecture:**

```python
# scripts/code_intel.py — simplified structure

class CodeIntel:
    """Shared code intelligence layer."""

    def __init__(self):
        self._parsers = {}  # lang -> Parser (lazy-loaded)
        self._has_treesitter = self._try_import_treesitter()

    def parse(self, file_path: str, content: str) -> ParsedFile:
        """Parse a file. Returns tree-sitter tree if available, else raw content."""
        lang = self._detect_language(file_path)
        if self._has_treesitter and lang in self._grammars:
            tree = self._get_parser(lang).parse(content.encode())
            return ParsedFile(path=file_path, lang=lang, tree=tree, content=content)
        return ParsedFile(path=file_path, lang=lang, tree=None, content=content)

    def get_functions(self, parsed: ParsedFile) -> list[FunctionInfo]:
        if parsed.tree:
            return self._query_functions_treesitter(parsed)
        return self._extract_functions_regex(parsed)

    def get_imports(self, parsed: ParsedFile) -> list[ImportInfo]: ...
    def get_exports(self, parsed: ParsedFile) -> list[ExportInfo]: ...
    def get_call_sites(self, parsed: ParsedFile, target: str) -> list[CallSite]: ...

    def cyclomatic_complexity(self, parsed: ParsedFile) -> list[CCResult]:
        """Count branching nodes per function. Tree-sitter or radon/gocyclo fallback."""
        if parsed.tree:
            return self._cc_treesitter(parsed)
        return self._cc_external_tool(parsed)  # radon, gocyclo

    def find_patterns(self, parsed: ParsedFile) -> list[PatternMatch]:
        """Lightweight static analysis. Tree-sitter queries or regex fallback."""
        if parsed.tree:
            return self._patterns_treesitter(parsed)
        return self._patterns_regex(parsed)

class LanguageConfig:
    """Tree-sitter grammar + language-specific query definitions."""
    # Per-language: function node types, import node types, branching node types,
    # export conventions (Python: no underscore prefix; Go: capitalized; TS: export keyword)
    LANGUAGES = {
        "python":     { "grammar": "tree-sitter-python",     "fn": "function_definition", ... },
        "go":         { "grammar": "tree-sitter-go",         "fn": "function_declaration", ... },
        "typescript": { "grammar": "tree-sitter-typescript", "fn": "function_declaration", ... },
        "java":       { "grammar": "tree-sitter-java",       "fn": "method_declaration",   ... },
        "rust":       { "grammar": "tree-sitter-rust",       "fn": "function_item",        ... },
    }

def main():
    subcommand = sys.argv[1]  # complexity|functions|imports|exports|callers|patterns
    intel = CodeIntel()
    files = sys.stdin.read().strip().split('\n')
    # dispatch to subcommand handler...
    print(json.dumps(result))
```

**Tree-sitter language support:**

| Language | Detection | Grammar package | Fallback |
|----------|-----------|----------------|----------|
| Python | `.py` | `tree-sitter-python` | `radon` for CC, regex for patterns |
| Go | `.go` | `tree-sitter-go` | `gocyclo` for CC, regex for patterns |
| TypeScript/JavaScript | `.ts`, `.tsx`, `.js`, `.jsx` | `tree-sitter-typescript`, `tree-sitter-javascript` | Regex only |
| Java | `.java` | `tree-sitter-java` | Regex only |
| Rust | `.rs` | `tree-sitter-rust` | Regex only |
| Shell | `.sh` | N/A (regex only) | `shellcheck` handles structural checks |

**Tree-sitter is optional.** If `tree-sitter` is not installed (`import tree_sitter` fails), the module falls back to:
- `radon` for Python complexity, `gocyclo` for Go complexity, nothing for other languages
- Regex for function/import extraction (less accurate but functional)
- Regex for pattern detection (only sql-injection and command-injection; structural checks like unused-import and unreachable-code require AST and are skipped)

**Install tree-sitter:** `pip install tree-sitter tree-sitter-python tree-sitter-go tree-sitter-typescript tree-sitter-java tree-sitter-rust`

**How other pipeline steps use code_intel.py:**

| Pipeline step | Current approach | With code_intel.py |
|--------------|-----------------|-------------------|
| **Step 2a-2b** (callers/callees) | Agent uses ad-hoc Grep — matches in comments, strings, variable names | Orchestrator runs `code_intel.py functions` + `code_intel.py callers --target X` — structural extraction, agent consumes JSON |
| **Step 2d** (complexity) | Inline bash calling radon/gocyclo — Python and Go only | Orchestrator runs `code_intel.py complexity` — all languages |
| **Step 2-L Phase A** (import graph) | Agent uses Grep to find imports — fragile | Orchestrator runs `code_intel.py imports` — reliable cross-chunk interface detection |
| **Step 3** (deterministic scans) | semgrep or nothing for code patterns | `run-scans.sh` calls `code_intel.py patterns` when semgrep is not installed |
| **Step 3.5** (adaptive pass selection) | Agent greps diff for `goroutine\|async def\|Mutex` — matches in comments | Orchestrator runs `code_intel.py exports` — structural detection of public API, concurrency constructs |

These SKILL.md integration changes are described in the "Interaction with existing pipeline" sections of Features 0 and 1, and are detailed under "SKILL.md changes for code_intel.py" below.

### Edge cases

- **Scripts not executable**: Invoke via `bash scripts/...` / `python3 scripts/...` explicitly. Use the explicit interpreter approach for portability.
- **Python not available**: `enrich-findings.py` and `code_intel.py` require Python 3. If `python3` is not available, the agent falls back to performing Steps 2d and 5 manually (as it does today). Log a warning: "python3 not found — falling back to agent-based enrichment/analysis."
- **Script fails**: If a script exits non-zero, the agent logs the stderr output and falls back to manual execution for that step. Scripts never block the review — they degrade gracefully.
- **Script output is invalid JSON**: The agent validates script output with `jq . < output.json` before consuming. If invalid, fall back to manual execution.
- **tree-sitter installed but grammar missing for a language**: That language falls back to regex/external-tool for all subcommands. Other languages with grammars still use tree-sitter. Log to stderr.
- **File too large (>10,000 lines)**: Skip tree-sitter parsing (memory/time risk). Fall back to regex/external-tool for that file. Log warning.

### Interaction with existing pipeline

- **SKILL.md Steps 2d, 3, 5**: Rewritten to invoke scripts. Step 2d invokes `code_intel.py complexity`. Step 3 invokes `run-scans.sh` (which may internally call `code_intel.py patterns`). Step 5 invokes `enrich-findings.py`.
- **SKILL.md Steps 2a-2b** (optional improvement): When `code_intel.py` is available, the orchestrator runs `functions` and `callers` subcommands for deterministic context extraction. When not available, the agent does ad-hoc Grep as before. This is an improvement, not a requirement — the agent's investigation still works without it.
- **SKILL.md Step 2-L Phase A** (optional improvement): When `code_intel.py` is available, the orchestrator runs `imports` for the cross-chunk interface summary. Replaces agent's ad-hoc Grep for imports.
- **SKILL.md Step 3.5** (optional improvement): When `code_intel.py` is available, the orchestrator runs `exports` to detect public API surface structurally. Falls back to diff-grep otherwise.
- **`references/deterministic-scans.md`**: Retains all documentation but executable snippets move to `run-scans.sh`. Add section documenting `code_intel.py patterns` as semgrep fallback.
- **`validate_output.sh`**: Unchanged.

### SKILL.md changes for code_intel.py

The `code_intel.py` integration touches multiple SKILL.md steps. Each integration is **optional** — the pipeline works without `code_intel.py` by falling back to the current behavior (agent Grep, radon/gocyclo, no semgrep fallback). SKILL.md should present each as:

```
If python3 and scripts/code_intel.py are available:
  <run code_intel.py subcommand, consume JSON>
Otherwise:
  <existing approach (agent Grep, radon/gocyclo, etc.)>
```

This avoids making the Python dependency mandatory while giving a significantly better experience when it's present.

### Testing

```bash
# Test complexity (replaces complexity.sh test)
echo "src/auth/login.py" | python3 scripts/code_intel.py complexity | jq '.hotspots | length'

# Test function extraction
echo "src/auth/login.py" | python3 scripts/code_intel.py functions | jq '.functions[].name'

# Test import graph
echo "src/auth/login.py" | python3 scripts/code_intel.py imports | jq '.imports[].module'

# Test callers
echo "src/auth/login.py\nsrc/api/views.py" | python3 scripts/code_intel.py callers --target "validate_session" | jq '.call_sites | length'

# Test semgrep fallback patterns
echo "src/api/orders.py" | python3 scripts/code_intel.py patterns | jq '.findings[] | select(.pattern == "sql-injection")'

# Test enrich-findings.py
python3 scripts/enrich-findings.py \
  --judge-findings tests/fixtures/judge-output.json \
  --scan-findings tests/fixtures/scan-output.json \
  | jq '.findings | length'

# Test run-scans.sh
echo "src/auth/login.py" | bash scripts/run-scans.sh --base-ref HEAD~1 | jq .
```

### Files to create

- `skills/codereview/scripts/run-scans.sh` — Deterministic scan orchestration
- `skills/codereview/scripts/enrich-findings.py` — Finding enrichment and classification
- `skills/codereview/scripts/code_intel.py` — Shared code intelligence module (replaces `complexity.sh`)
- `tests/fixtures/judge-output.json` — Sample judge output for testing enrich-findings.py (minimum: 5 findings across 3 passes, including one high-severity without failure_mode to test downgrade, one below confidence floor to test filtering)
- `tests/fixtures/scan-output.json` — Sample run-scans.sh output for testing enrich-findings.py (minimum: 3 deterministic findings from 2 different tools, including one that would collide with a judge finding to test dedup-by-agent scenario)
- `tests/fixtures/code_intel/` — Multi-language fixture files for testing each subcommand (Python, Go, TypeScript at minimum; one file per language with known functions, imports, complexity hotspots, and pattern violations)

### Files to modify

- `skills/codereview/SKILL.md` — Update Steps 2a-2b (optional code_intel integration), 2d (code_intel.py complexity), 3 (run-scans.sh, code_intel.py patterns fallback), 3.5 (code_intel.py exports for adaptive pass selection), 5 (enrich-findings.py). Update Step 2-L Phase A (code_intel.py imports for cross-chunk interfaces). Keep logic descriptions as documentation, clearly marked "implemented by script."
- `skills/codereview/references/deterministic-scans.md` — Mark as reference-only, add pointer to `scripts/run-scans.sh`, document `code_intel.py patterns` as semgrep fallback
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: tree-sitter available vs not, radon/gocyclo fallback, semgrep fallback patterns, python3 missing, jq missing, script failure fallback, script invalid output, per-language code_intel output

### Effort: Medium-Large (largest feature in the plan — but it provides infrastructure used by Features 1, 2, and the pipeline itself)

---

## Feature 1: Prescan

**Goal:** Run fast static checks as part of context gathering. Catches obvious issues (hardcoded secrets, dead code, swallowed errors, long functions) in seconds, providing deterministic signals that guide explorers toward high-risk areas. Works across all languages the skill reviews, not just Python.

Inspired by the AgentOps vibe `prescan.sh` pattern, but implemented as Python for multi-language AST support.

### Where it fits

New sub-step **2k** in Step 2 (Gather Context), after complexity analysis (2d) and before the context packet assembly (2h — which remains the final assembly step despite now being alphabetically earlier than 2i/2j/2k; see "Step numbering" note below).

**Step numbering note:** The current SKILL.md has sub-steps 2a through 2h, where 2h is the final assembly step. This plan adds 2i (Feature 2 — domain checklists), 2j (Feature 3 — git risk), and 2k (this feature — prescan). All three are data-gathering steps that contribute to the context packet assembled in 2h. The alphabetical ordering is unfortunate (2i/2j/2k come "after" 2h) but renumbering 2h would break all existing references. Instead, SKILL.md should include a note: "Step 2h is the assembly step — execute it last, after all 2a–2k data gathering is complete."

### Why Python, not bash

The earlier version of this feature was `prescan.sh` — a bash script with Python one-liners embedded via `python3 -c '...'` for AST checks. That design has three problems:

1. **Language bias.** Python AST (`ast` module) only parses Python. Go got awk heuristics, everything else got regex-only. But the checks themselves (swallowed errors, long functions, dead code) apply to every language.
2. **Fragility.** Multi-line Python stuffed into single-quoted bash strings breaks on quoting edge cases and is untestable in isolation.
3. **Extensibility.** Adding a new language means writing a new set of bash/awk heuristics. A Python script with a language abstraction adds a class.

A pure Python implementation uses **tree-sitter** (optional) for structural checks across languages, with regex fallback when tree-sitter is not installed. This gives us the same quality of analysis for Go, TypeScript, Java, and Rust that we previously only had for Python.

### Implementation: `scripts/prescan.py`

**Interface:**
```bash
echo "$CHANGED_FILES" | python3 scripts/prescan.py > /tmp/codereview-prescan.json
```

Reads `CHANGED_FILES` on stdin (newline-delimited), same as the other scripts. Target resolution is already done in Step 1 — the prescan does NOT re-resolve targets.

**Output:** JSON to stdout:
```json
{
  "file_count": 12,
  "analyzer": "tree-sitter|regex-only",
  "languages_detected": ["python", "go", "typescript"],
  "patterns": {
    "hardcoded_secrets": { "count": 1, "severity": "critical", "findings": [...] },
    "swallowed_errors": { "count": 2, "severity": "high", "findings": [...] },
    "long_functions":   { "count": 3, "severity": "medium", "findings": [...] },
    "todo_markers":     { "count": 5, "severity": "low", "findings": [...] },
    "commented_code":   { "count": 2, "severity": "low", "findings": [...] },
    "dead_code":        { "count": 1, "severity": "medium", "findings": [...] }
  },
  "summary": { "critical": 1, "high": 2, "medium": 4, "low": 7 }
}
```

The `analyzer` field tells the consumer whether tree-sitter was used or whether the script fell back to regex-only. This matters because tree-sitter checks are structurally aware (fewer false positives) while regex checks are pattern-based (more false positives, but still useful as signals).

Each finding in the arrays has: `file`, `line`, `pattern_id`, `description`, `evidence`.

**Pattern checks (6 categories):**

| ID | Pattern | Severity | Tree-sitter detection (structural) | Regex fallback (all languages) |
|----|---------|----------|-------------------------------------|-------------------------------|
| P-SEC | Hardcoded secrets | critical | Same as regex (secrets are string literals, not structural) | `(password\|secret\|api_key\|token)\s*=\s*['"][^'"]+['"]` in non-test files |
| P-ERR | Swallowed errors | high | Query: error-handling nodes with empty/pass-only bodies. Covers `except: pass` (Python), `if err != nil { }` (Go), empty `catch {}` (TS/Java/Rust), `_ = err` (Go) | `except.*:\s*pass`, `catch\s*\([^)]*\)\s*\{\s*\}`, `_ = err` |
| P-LEN | Long functions | medium | Query: function/method definition nodes, compute `end_line - start_line`. Works for all languages with function nodes. | Count lines between `def`/`func`/`function`/`fn` and closing brace/dedent (heuristic, less accurate) |
| P-TODO | TODO/FIXME markers | low | Same as regex (comments are leaves, regex is sufficient) | `TODO\|FIXME\|XXX\|HACK` |
| P-COMMENT | Commented code | low | Query: comment nodes containing language keywords (`def`, `func`, `function`, `class`, `if`, `for`, `return`) | `^\s*[#//]\s*(def \|func \|function \|class \|if \|for \|return )` |
| P-DEAD | Dead code candidates | medium | Query: function definition names → scan for call-site references in same file. Functions defined but never called locally. Excludes entry points, test functions, exported/public functions. | Name-based grep: extract `def foo`/`func Foo`/`function foo` definitions, grep for `foo(` calls in same file. Higher false-positive rate. |

**No P-CC (complexity) check.** Complexity analysis is already handled by `scripts/code_intel.py complexity` (Feature 0c). The prescan does NOT duplicate that work. Since `prescan.py` imports `code_intel.py`, it can access the parsed tree — but it does not re-run complexity analysis. If the orchestrator needs prescan + complexity, it runs both scripts and merges the outputs in the context packet.

**Tree-sitter language support:**

| Language | Detection | Grammar package |
|----------|-----------|----------------|
| Python | `.py` | `tree-sitter-python` |
| Go | `.go` | `tree-sitter-go` |
| TypeScript/JavaScript | `.ts`, `.tsx`, `.js`, `.jsx` | `tree-sitter-typescript`, `tree-sitter-javascript` |
| Java | `.java` | `tree-sitter-java` |
| Rust | `.rs` | `tree-sitter-rust` |
| Shell | `.sh` | Regex only (shellcheck handles structural checks) |

**Tree-sitter is optional.** If `tree-sitter` is not installed (`import tree_sitter` fails), the script falls back to regex-only mode for all checks. Regex mode still provides value — just with higher false-positive rates for P-ERR, P-LEN, and P-DEAD. The `analyzer` field in the output tells the consumer which mode was used.

**Install tree-sitter:** `pip install tree-sitter tree-sitter-python tree-sitter-go tree-sitter-typescript tree-sitter-java tree-sitter-rust`

**File filtering:** Exclude `__pycache__`, `.venv`, `node_modules`, `.git`, `test_fixtures`, `*_test.*` (for P-SEC only), generated code (`*.pb.go`, `*.generated.*`, `*.g.dart`).

### Script architecture

`prescan.py` imports `code_intel.py` (Feature 0c) rather than building its own parser. This avoids duplicate tree-sitter initialization and ensures consistent language detection across the pipeline.

```python
# scripts/prescan.py — simplified structure

from code_intel import CodeIntel, ParsedFile

class PatternChecker:
    """Base class for pattern checks. Subclasses implement check()."""
    def check(self, parsed: ParsedFile) -> list[Finding]: ...

class SecretChecker(PatternChecker): ...      # P-SEC: regex-only, all languages
class SwallowedErrorChecker(PatternChecker):  # P-ERR: uses code_intel.find_patterns() or regex
    def check(self, parsed: ParsedFile) -> list[Finding]:
        if parsed.tree:
            return self._check_structural(parsed)
        return self._check_regex(parsed)
class LongFunctionChecker(PatternChecker): ...  # P-LEN: uses code_intel.get_functions() for line spans
class TodoChecker(PatternChecker): ...          # P-TODO: regex-only
class CommentedCodeChecker(PatternChecker): ... # P-COMMENT: regex-only
class DeadCodeChecker(PatternChecker): ...      # P-DEAD: uses code_intel.get_functions() + reference scan

def main():
    intel = CodeIntel()
    files = sys.stdin.read().strip().split('\n')
    checkers = [SecretChecker(), SwallowedErrorChecker(), LongFunctionChecker(),
                TodoChecker(), CommentedCodeChecker(), DeadCodeChecker()]
    results = {}
    for file_path in filter_files(files):
        content = read_file(file_path)
        parsed = intel.parse(file_path, content)  # shared parser from code_intel
        for checker in checkers:
            results.setdefault(checker.pattern_id, []).extend(
                checker.check(parsed)
            )
    print(json.dumps(format_output(results, analyzer=intel.analyzer_name)))
```

This structure means:
- **No duplicate parsing** — `prescan.py` and `code_intel.py` share the same tree-sitter initialization and `ParsedFile` objects
- **Adding a pattern check** = add a `PatternChecker` subclass
- **Adding a language** = add a grammar to `code_intel.py`'s `LanguageConfig` (prescan gets it for free)
- **Testable** — each checker can be tested independently with fixture `ParsedFile` objects

### Prescan is context, not findings

Prescan output is injected into the explorer context packet (Step 2h) as a "Prescan Signals" section. It is NOT added to the deterministic findings list (Step 3) and NOT passed to `enrich-findings.py` (Step 5).

**Why:** Prescan checks are fast heuristics with known false positives (e.g., P-SEC flags `test_password = "hunter2"` in test setup; P-DEAD flags functions called via dynamic dispatch). They serve as attention signals for explorers, not final findings. Explorers investigate the flagged areas with tools (Grep/Read/Glob) and produce proper findings with evidence and confidence scores.

**Dedup with explorer output:** There is no mechanical dedup between prescan signals and explorer findings. The judge already deduplicates explorer findings in Step 4b. If an explorer produces a finding about the same issue a prescan flagged, the explorer's finding is the authoritative one (with evidence, confidence, and failure_mode). The prescan signal is consumed as context and not carried forward.

### Interaction with existing pipeline

- **Step 2k (new)**: Run `scripts/prescan.py`, read JSON output
- **Step 2h (context packet assembly)**: Include prescan summary as a "Prescan Signals" section. Critical/high findings get explicit callouts so explorers prioritize investigation of those areas:
  ```
  ## Prescan Signals (fast static checks, tree-sitter mode)
  CRITICAL: 1 potential hardcoded secret detected — investigate with full context
  - src/auth/config.py:17 — P-SEC: possible hardcoded credential

  HIGH: 2 swallowed errors detected
  - src/api/orders.py:45 — P-ERR: except: pass (error swallowed)
  - src/utils/retry.go:23 — P-ERR: empty error handler (if err != nil {})

  6 additional signals (medium/low) omitted for brevity.
  ```
- **Step 2-L Phase A (large-diff mode)**: Include prescan in global context. Cap to critical/high signals only to save tokens.
- **Step 3 (deterministic scans)**: Unchanged. Prescan and deterministic scans are separate tracks.

### Testing

Each checker can be tested independently with fixture files:

```bash
# Test with a fixture file containing known patterns
echo "tests/fixtures/prescan/swallowed_errors.py" | python3 scripts/prescan.py | jq '.patterns.swallowed_errors.count'
# Expected: 3

echo "tests/fixtures/prescan/swallowed_errors.go" | python3 scripts/prescan.py | jq '.patterns.swallowed_errors.count'
# Expected: 2

echo "tests/fixtures/prescan/clean_file.py" | python3 scripts/prescan.py | jq '.summary'
# Expected: all zeros

# Test regex fallback (without tree-sitter)
PRESCAN_NO_TREESITTER=1 echo "tests/fixtures/prescan/swallowed_errors.py" | python3 scripts/prescan.py | jq '.analyzer'
# Expected: "regex-only"
```

Unit tests should cover:
- Each checker × each supported language (with tree-sitter)
- Each checker × regex fallback mode
- File filtering (test files excluded from P-SEC, generated code excluded from all)
- Empty file list → empty output
- File with no issues → all zeros
- Binary/unreadable files → skipped gracefully

### Edge cases

- **No Python 3**: The entire prescan is skipped. The orchestrator falls back to no prescan context (all other pipeline steps still work). Log: "python3 not found — prescan skipped."
- **tree-sitter not installed**: Falls back to regex-only mode. All 6 checks still run with regex patterns. `analyzer` field reports `"regex-only"`. Log to stderr: "tree-sitter not installed — using regex-only mode (higher false positive rate). Install: pip install tree-sitter tree-sitter-python tree-sitter-go ..."
- **tree-sitter installed but grammar missing for a language**: That language falls back to regex for structural checks. Other languages with grammars still use tree-sitter. Log: "tree-sitter-java not installed — Java files will use regex-only checks."
- **No shellcheck**: Skip shell-specific P-ERR. Log warning.
- **Empty file list**: Output `{ "file_count": 0, "analyzer": "...", "languages_detected": [], "patterns": {}, "summary": {} }`.
- **Large file lists (>200 files)**: Cap at 200 files (sorted by risk tier if available from Step 1.5). Log "Prescan capped at 200 files" in stderr.
- **File too large (>10,000 lines)**: Skip tree-sitter parsing (memory/time risk). Fall back to regex for that file. Log warning.
- **Binary file or encoding error**: Skip the file. Log to stderr.

### Files to create

- `skills/codereview/scripts/prescan.py` — Multi-language static pattern prescan
- `tests/fixtures/prescan/` — Fixture files for each language × each pattern (see Testing section)

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2k, include prescan output in context packet assembly (Step 2h), add step numbering note
- `skills/codereview/references/acceptance-criteria.md` — Add prescan scenarios: tree-sitter mode, regex fallback, per-language checks, no python3, empty files
- `skills/codereview/references/design.md` — Add rationale entry (why Python not bash, why tree-sitter optional, why prescan is context not findings)

### Effort: Medium

---

## Feature 2: Domain-Specific Checklists

**Goal:** Auto-detect code patterns in the diff (SQL/ORM, LLM/AI, concurrency) and inject targeted checklist items into the explorer context. This gives explorers concrete things to look for in specialized domains without changing the explorer prompts themselves.

Inspired by the AgentOps vibe domain checklist pattern (Step 2.3), but implemented as static reference files loaded by the orchestrator.

### Where it fits

New sub-step **2i** in Step 2 (Gather Context), after complexity analysis (2d) and before context packet assembly (2h). See Feature 1 for the step numbering note — 2i is a data-gathering step executed before 2h despite alphabetical ordering. Also applies to Step 2-L Phase A (large-diff mode) as a lightweight global context component.

### Detection logic

The **orchestrator agent** greps the diff content for trigger patterns. If any pattern matches, the agent reads the corresponding checklist file and includes it in the context packet. This is a simple grep-then-read — no script needed, since the detection is 3 grep calls and the agent already has the diff in context.

**Detection patterns (ERE syntax for `grep -E`):**

| Trigger Pattern (`grep -E` on DIFF) | Checklist File | What It Covers |
|--------------------------------------|----------------|----------------|
| `SELECT\|INSERT\|UPDATE\|DELETE\|JOIN\|SQLAlchemy\|sqlalchemy\|GORM\|gorm\|Prisma\|prisma\|Knex\|knex\|sequelize\|ActiveRecord\|active_record\|\.query\(\|\.execute\(\|\.raw\(` | `references/checklist-sql-safety.md` | SQL injection, parameterized queries, ORM misuse, N+1 patterns, transaction safety, migration risks |
| `anthropic\|openai\|google\.generativeai\|cohere\|replicate\|langchain\|llm\|LLM\|ChatModel\|chat_model\|completion\|embedding` | `references/checklist-llm-trust.md` | Prompt injection, output sanitization, token limits, PII in prompts, model response validation, cost controls |
| `goroutine\|go func\|threading\|Thread\|async def\|asyncio\|\.lock\(\|Mutex\|RwLock\|chan \|channel\|atomic\|sync\.\|Promise\.all\|Worker\(\|spawn\|tokio\|Arc<` | `references/checklist-concurrency.md` | Race conditions, deadlocks, goroutine leaks, shared state, lock ordering, channel misuse, async pitfalls |

**Implementation in SKILL.md** (not a script — inline agent instructions):
```bash
# Step 2i: Domain checklist detection
CHECKLISTS=""
if echo "$DIFF" | grep -qE 'SELECT|INSERT|UPDATE|DELETE|JOIN|sqlalchemy|GORM|Prisma|Knex|sequelize|ActiveRecord|\.query\(|\.execute\(|\.raw\('; then
  CHECKLISTS="$CHECKLISTS sql-safety"
fi
if echo "$DIFF" | grep -qE 'anthropic|openai|google\.generativeai|cohere|replicate|langchain|llm|LLM|ChatModel|completion|embedding'; then
  CHECKLISTS="$CHECKLISTS llm-trust"
fi
if echo "$DIFF" | grep -qE 'goroutine|go func|threading|Thread|async def|asyncio|\.lock\(|Mutex|RwLock|chan |atomic|sync\.|Promise\.all|Worker\(|spawn|tokio|Arc<'; then
  CHECKLISTS="$CHECKLISTS concurrency"
fi
# For each matched checklist, Read references/checklist-<name>.md
```

**Why not a script?** The detection is 3 grep calls with static patterns. A script would add indirection without benefit. The checklists themselves are static markdown files. If the number of checklists grows beyond ~6, extract detection into a script.

**Detection is deliberately over-inclusive.** Better to load a checklist that turns out irrelevant (explorers will simply find nothing to flag) than to miss a checklist that would have caught an issue.

### Checklist file format

Each checklist is a markdown file with a flat list of questions. Questions are phrased so an explorer can answer yes/no with evidence:

```markdown
# SQL Safety Checklist

Check each item. If the answer is "yes" for any, report a finding with evidence.

- [ ] Does any SQL query use string concatenation or f-strings instead of parameterized queries?
- [ ] Does any ORM query use `.raw()` or `.execute()` with user-controlled input?
- [ ] Is there a query inside a loop that could be an N+1 pattern?
- [ ] Are database transactions missing where multiple related writes occur?
- [ ] Does a migration drop or rename a column without a backfill strategy?
- [ ] Is there a `.first()` or `LIMIT 1` without an `ORDER BY`?
- [ ] Does any query SELECT * where only specific columns are needed?
- [ ] Are database credentials hardcoded or passed via query string?
```

### How checklists reach explorers

The orchestrator includes loaded checklists in the context packet (Step 2h) as an additional section:

```
## Domain-Specific Checklists (auto-detected)

The following checklists were loaded because domain-specific patterns were detected in the diff.
Check each item during your investigation. If an item applies and a violation is found, report
it as a finding with the checklist question as context.

### SQL Safety (triggered by: SQL queries detected in diff)
<contents of checklist-sql-safety.md>

### Concurrency (triggered by: goroutine/mutex patterns detected in diff)
<contents of checklist-concurrency.md>
```

No changes to explorer prompt files. Checklists are injected as context, not as prompt modifications.

### Edge cases

- **Multiple checklists match**: Include all matching checklists. If all three match on a single diff, that's ~1.5k tokens — acceptable.
- **No checklists match**: Skip the section entirely. No "Domain-Specific Checklists" section in the context packet.
- **Large-diff mode**: Run detection once globally (Phase A), include matched checklists in `GLOBAL_CONTEXT`. Each chunk explorer receives the same checklists — they're domain-level, not file-level.
- **False trigger**: A test file imports `sqlalchemy` for test fixtures. The checklist loads, explorers investigate, find nothing, report nothing. No harm — just a few wasted tokens.

### Files to create

- `skills/codereview/references/checklist-sql-safety.md` — SQL/ORM safety checklist (~15 items)
- `skills/codereview/references/checklist-llm-trust.md` — LLM trust boundary checklist (~12 items)
- `skills/codereview/references/checklist-concurrency.md` — Concurrency safety checklist (~15 items)

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2i: domain checklist detection and loading. Add to Step 2h context packet and Step 2-L Phase A.
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: SQL detected, LLM detected, concurrency detected, multiple match, none match

### Effort: Small

---

## Feature 3: Git History Risk Scoring

**Goal:** Give explorers a per-file risk signal based on historical bug frequency and churn, so they pay more attention to code that has been problematic before.

Carried forward from v1.2 Feature 1 — unchanged except for the addition of Tier 1 promotion (which interacts with the large-diff chunking from the current branch).

### Where it fits

New sub-step **2j** in Step 2 (Gather Context), after domain checklists (2i). Contributes data to the context packet assembled in 2h (see Feature 1 for the step numbering note). Also integrates into Step 2-L Phase A (large-diff mode) as a lightweight global context component.

### Implementation: `scripts/git-risk.sh`

**Interface:**
```bash
echo "$CHANGED_FILES" | bash scripts/git-risk.sh [--months 6] > /tmp/codereview-git-risk.json
```

**Output:**
```json
{
  "shallow_clone": false,
  "lookback_months": 6,
  "files": [
    { "file": "src/auth/session.py", "churn": 14, "bug_commits": 3, "last_bug": "2026-03-13", "risk": "high" },
    { "file": "src/api/orders.py", "churn": 6, "bug_commits": 1, "last_bug": "2026-02-08", "risk": "medium" }
  ],
  "summary": { "high": 1, "medium": 1, "low": 4 }
}
```

**Script logic:** For each file, compute:
- **Churn**: `git log --oneline --follow --since="N months ago" -- "$file" | wc -l`
- **Bug signal**: `git log --oneline --follow --since="N months ago" --grep='fix\|bug\|revert\|hotfix' -i -- "$file" | wc -l`
- **Recency**: date of last bug-related commit

Risk tier assignment:

| Condition | Risk |
|-----------|------|
| BUG_COMMITS >= 3 OR (BUG_COMMITS >= 2 AND CHURN >= 10) | **high** |
| BUG_COMMITS >= 1 OR CHURN >= 8 | **medium** |
| Otherwise | **low** |

### Interaction with existing pipeline

- **Step 1.5c (large-diff mode)**: A file classified as Tier 2 (standard) by path heuristics but with `risk: "high"` from git history should be promoted to Tier 1 (critical). Add to Tier 1 criteria: "Historical risk = high".
- **Step 2h (context packet)**: Add "Historical Risk" section between complexity scores and language standards. Only include medium/high risk files (low-risk files are the default).
- **Step 2-L Phase A**: Include git risk as part of lightweight global context (~1-2k tokens).
- **Explorer prompts**: No changes needed — explorers receive the context packet which will now include historical risk.

### Edge cases

- **Shallow clones**: `git rev-list --count HEAD` < 50 → emit warning, compute with available history.
- **Renamed files**: `git log --follow` tracks renames. Per-file loop already handles this.
- **New files**: 0 churn, 0 bug commits → risk "low". Correct — no historical signal.
- **All files low-risk**: Output "All changed files have low historical risk." in context packet.

### Files to create

- `skills/codereview/scripts/git-risk.sh` — Git history risk scoring

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2j, include in context packet, add Tier 1 promotion criterion
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios

### Effort: Small

---

## Feature 4: Test Pyramid Vocabulary

**Goal:** Update the test-adequacy explorer prompt to use structured test pyramid levels (L0-L7) and bug-finding levels (BF1-BF9) when classifying test gaps. This makes test gap findings more actionable by telling the user exactly what *kind* of test is needed, not just that "a test is missing."

Inspired by the AgentOps standards skill's test pyramid classification, adapted for our explorer output format.

### Where it fits

Prompt modification + minor schema addition. Changes `prompts/reviewer-test-adequacy-pass.md` (vocabulary and calibration examples) and `findings-schema.json` (3 optional fields). No pipeline changes, no new scripts.

### Test pyramid levels (for classifying existing tests)

| Level | Name | What It Catches | Example |
|-------|------|----------------|---------|
| L0 | Contract/Spec | Spec boundary violations | Schema validation, API contract tests |
| L1 | Unit | Logic bugs in isolated functions | `test_calculate_discount()` |
| L2 | Integration | Module interaction bugs | DB + service layer together |
| L3 | Component | Subsystem-level failures | Auth service end-to-end |
| L4 | Smoke | Critical path regressions | Login → dashboard flow |
| L5 | E2E | Full system behavior | Browser test of complete user journey |

### Bug-finding levels (for classifying what's missing)

| Level | Name | What It Finds | When Needed |
|-------|------|--------------|-------------|
| BF1 | Property | Edge cases from randomized inputs | Data transformations, parsers |
| BF2 | Golden/Snapshot | Output drift | Serializers, formatters, template renderers |
| BF4 | Chaos/Negative | Unhandled failures | External API calls, DB operations, file I/O |
| BF6 | Regression | Reintroduced bugs | Any area with a history of fixes |
| BF8 | Backward compat | Breaking changes | Public APIs, serialization formats |

### How the explorer uses these

The test-adequacy explorer currently reports findings like:
```json
{
  "summary": "Missing test for cancel_order function",
  "tests_to_add": ["Test that cancel_order handles already-cancelled orders"],
  "test_category_needed": ["integration"]
}
```

With the pyramid vocabulary, it would report:
```json
{
  "summary": "Missing test for cancel_order function",
  "tests_to_add": ["L2: Integration test that cancel_order rolls back partial DB writes on failure"],
  "test_category_needed": ["integration"],
  "test_level": "L2",
  "bug_finding_level": "BF4",
  "gap_reason": "cancel_order calls payment API and writes to DB — failure between these steps needs chaos/negative testing, currently only has L1 unit test with mocked DB"
}
```

### Prompt changes

Add to `reviewer-test-adequacy-pass.md`:

1. **Classification vocabulary section**: Define L0-L5 and BF1/BF2/BF4/BF6/BF8 with examples
2. **Gap analysis instructions**: For each function without adequate test coverage, determine:
   - What test level exists (if any)
   - What test level is needed (and why)
   - What bug-finding level would catch the specific risk
3. **Calibration examples**: Add 2-3 examples showing how to classify test gaps using the vocabulary

### Schema changes

Add optional fields to the finding schema for test-adequacy findings:

```json
{
  "test_level": "L0|L1|L2|L3|L4|L5",
  "bug_finding_level": "BF1|BF2|BF4|BF6|BF8",
  "gap_reason": "string"
}
```

These are optional — only populated by test-adequacy findings. Other passes don't use them.

### Edge cases

- **No test files in the diff**: The explorer still runs (it greps for test files in the repo, not just the diff). Pyramid classification applies to found tests.
- **Language without conventional test patterns**: Explorer falls back to generic classification. L1-L3 levels apply across languages.
- **Existing tests hard to classify**: Explorer reports `test_level: "L1"` with a note if uncertain. The judge doesn't re-classify — it trusts the explorer's assessment.

### Files to modify

- `skills/codereview/prompts/reviewer-test-adequacy-pass.md` — Add pyramid vocabulary, gap analysis instructions, calibration examples
- `skills/codereview/findings-schema.json` — Add optional `test_level`, `bug_finding_level`, `gap_reason` fields
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 5: Per-File Certification for Explorers

**Goal:** Require explorers to explicitly state what they checked and why they found nothing, instead of returning an empty `[]`. This forces thorough investigation and creates an audit trail that the judge can verify.

Currently, an explorer that returns `[]` provides no signal — the judge can't distinguish "I checked everything thoroughly and it's clean" from "I skimmed the diff and nothing jumped out." The AgentOps deep audit protocol solves this with per-file category certification: each explorer must either report a finding OR explicitly certify its focus area as clean with a reason.

### Where it fits

Modification to `prompts/reviewer-global-contract.md` (the shared rules all explorers follow). No pipeline changes, no new scripts, no schema changes to the findings output.

### Current behavior

The global contract says:
```
Return `[]` if no issues found in your focus area.
```

### New behavior

Replace the empty-return instruction with a certification requirement:

```markdown
## Empty Result Certification

If you find NO issues in your focus area, you MUST NOT return a bare `[]`.
Instead, return a certification object explaining what you checked:

```json
{
  "certification": {
    "status": "clean",
    "files_checked": ["src/auth/login.py", "src/auth/session.py"],
    "checks_performed": [
      "Traced all 3 callers of login() — none assume a return value that changed",
      "Verified session.pop() uses default=None (safe for missing keys)",
      "Checked backward compatibility — function signature unchanged"
    ],
    "tools_used": ["Grep: callers of login()", "Read: src/auth/session.py:40-60"]
  },
  "findings": []
}
```

**Rules for certification:**
1. `files_checked` must list every file in CHANGED_FILES that is relevant to your focus area. If you skipped a file, explain why (e.g., "test file — not relevant to correctness").
2. `checks_performed` must list 3-5 concrete checks you did (not generic statements). Each check should reference a specific function, line, or pattern you investigated.
3. `tools_used` must list the actual Grep/Read/Glob calls you made. If you made zero tool calls and certified clean, that's a red flag — the judge will flag it.
4. If the diff contains no code relevant to your focus area (e.g., concurrency explorer on a CSS-only diff), certify with: `"status": "not_applicable", "reason": "No code in diff is relevant to concurrency analysis"`.
```

### How the judge uses certifications

The judge already validates each explorer's work (Step 1 in `reviewer-judge.md`). Add a new sub-step:

**Step 0.5: Certification Review (before adversarial validation)**

For each explorer that returned `findings: []`:
1. Read the certification. If no certification present (bare `[]`), note: "Explorer <pass> returned empty without certification — investigation depth unknown."
2. Check `tools_used` — if the explorer made zero tool calls, flag in the report: "Explorer <pass> certified clean without investigation. Findings may be missed."
3. Check `files_checked` — if relevant changed files are missing from the list, the explorer may have missed them.
4. Do NOT re-run the explorer's analysis — just assess whether the certification is plausible.

This is a lightweight check (read the certification, sanity-check it). The judge does not re-do the explorer's work.

### Output handling

The certification object is consumed by the judge and NOT included in the final findings output. It's an internal quality signal, not a user-facing artifact. The judge may mention certification gaps in its `verdict_reason` if they affect confidence.

### Interaction with existing pipeline

- **Step 4a (explorer launch)**: No change to how explorers are launched. The certification requirement is in the global contract prompt, not the orchestrator.
- **Step 4b (judge)**: Add Step 0.5 (certification review) before Step 1 (adversarial validation).
- **Step 4-L (chunked mode)**: Same — certification is per-explorer, works identically in chunked mode.
- **Findings schema**: No change — certification is internal to the explorer-judge exchange.

### Edge cases

- **Explorer returns bare `[]` (no certification)**: The judge proceeds but notes the gap. This is a degraded experience, not a failure — older prompt versions or third-party explorers may not certify.
- **Explorer certifies clean but the judge finds an issue in the same area**: The judge reports its finding normally. The certification gap is noted in the verdict reason for transparency.
- **Large-diff chunked mode**: Each chunk explorer certifies independently for its chunk. The cross-chunk synthesizer does NOT certify — it either finds cross-chunk issues or returns `[]` (it's investigating interactions, not doing per-file review).

### Files to modify

- `skills/codereview/prompts/reviewer-global-contract.md` — Replace empty-return instruction with certification requirement
- `skills/codereview/prompts/reviewer-judge.md` — Add Step 0.5 (certification review)
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: explorer certifies clean, explorer returns bare [], explorer certifies but judge finds issue

### Effort: Small

---

## Feature 6: Contract Completeness Gate for Spec Verification

**Goal:** Strengthen the spec-verification pass with a structured completeness gate that catches categories of spec gaps that the current free-form requirement tracing misses. When reviewing code against a spec, the explorer should not only trace individual requirements but also verify that the spec's behavioral contracts are mechanically verifiable.

The AgentOps council enforces a 4-item contract completeness gate before allowing a PASS verdict on spec validation. Our spec-verification explorer already does requirement tracing and test category mapping — this feature adds a structured completeness assessment on top.

### Where it fits

Addition to `prompts/reviewer-spec-verification-pass.md` — a new Phase 6 after the existing Phase 5 (Category Adequacy Assessment). Also a small addition to the judge prompt's Step 5 (Spec Compliance Check).

### The completeness gate

After tracing individual requirements (Phases 1-5), the spec-verification explorer performs a structured assessment of the spec's behavioral completeness. This catches a different class of issues than per-requirement tracing — it catches what the spec *forgot to specify*.

**Gate items (4 checks):**

| # | Check | What It Catches | Example |
|---|-------|----------------|---------|
| 1 | **State transitions** | Missing states, undefined transitions, contradictory flows | Spec says "order can be cancelled" but doesn't define what happens to a partially-shipped order |
| 2 | **Error/edge behavior** | Unspecified error responses, boundary conditions, concurrent access | Spec defines happy path for payment but not what happens when payment gateway times out |
| 3 | **Cross-requirement consistency** | Requirements that contradict each other, or leave gaps between them | REQ-003 says "admin can delete users" but REQ-007 says "user data must be retained for 90 days" |
| 4 | **Testability** | Requirements that cannot be mechanically verified | "The system should be fast" — no metric, no threshold, no test can verify this |

### Explorer prompt additions

Add to `reviewer-spec-verification-pass.md` after Phase 5:

```markdown
### Phase 6 — Contract Completeness Assessment

After tracing individual requirements, assess the spec's completeness as a behavioral contract.
This catches what the spec *forgot to specify*, not what the code forgot to implement.

For each gate item below, determine: PASS (adequately specified), GAP (missing or incomplete),
or N/A (not relevant to this spec).

**6a. State Transitions**
If the spec describes entities with lifecycles (orders, users, sessions, workflows, jobs):
1. List all states mentioned in the spec (e.g., pending, active, suspended, closed)
2. List all transitions mentioned (e.g., pending→active on approval)
3. Check for gaps:
   - Are there states with no outgoing transitions (terminal states)? Are they intentional?
   - Are there transitions that could produce contradictions (e.g., simultaneous cancel and complete)?
   - Is the initial state defined?
   - Is error recovery defined (what state does the entity enter on failure)?
If no lifecycle entities exist in the spec, mark N/A.

**6b. Error/Edge Behavior**
For each integration point mentioned in the spec (API calls, database operations, file I/O,
external services):
1. Does the spec define what happens when the operation fails?
2. Does the spec define timeout behavior?
3. Does the spec define retry/backoff strategy, or explicitly state "no retry"?
4. Does the spec define behavior for malformed input?
If the spec has no integration points, mark N/A.

**6c. Cross-Requirement Consistency**
Review all extracted requirements together:
1. Do any two requirements contradict each other?
2. Are there logical gaps between requirements (e.g., requirement A produces output X,
   requirement B consumes input Y, but X ≠ Y)?
3. Do all requirements use consistent terminology (same term for same concept)?

**6d. Testability**
For each requirement classified as `must` or `should`:
1. Can it be tested with a deterministic assertion?
2. If not (e.g., "should be performant"), flag as a testability gap.
3. Suggest a testable reformulation if possible (e.g., "p95 latency < 200ms").

**Output:** Add a `completeness_gate` object to your output alongside `requirements` and `findings`:

```json
{
  "completeness_gate": {
    "state_transitions": {
      "status": "PASS|GAP|N/A",
      "detail": "Found 4 states, 6 transitions, no gaps" | "Missing: error recovery state for failed payments"
    },
    "error_edge_behavior": {
      "status": "PASS|GAP|N/A",
      "detail": "All 3 integration points have error handling specified" | "Payment gateway timeout behavior unspecified"
    },
    "cross_requirement_consistency": {
      "status": "PASS|GAP|N/A",
      "detail": "No contradictions found" | "REQ-003 and REQ-007 contradict on data deletion"
    },
    "testability": {
      "status": "PASS|GAP|N/A",
      "detail": "All must/should requirements are testable" | "REQ-012 ('system should be responsive') has no testable threshold"
    },
    "overall": "PASS|GAP",
    "gap_count": 0
  }
}
```

**Gate verdict rules:**
- All PASS/N/A → `overall: "PASS"`
- Any GAP → `overall: "GAP"`, and report each gap as a finding with `pass: "spec_verification"`,
  `severity: "medium"`, summary: "Spec completeness gap: <description>"
```

### Judge prompt additions

Add to `reviewer-judge.md` Step 5, after 5c:

```markdown
### 5c.5. Evaluate Completeness Gate

If the spec-verification explorer returned a `completeness_gate` object:
1. Include the gate results in the `spec_requirements` output as a summary note.
2. If `overall: "GAP"`:
   - The gate gaps are already in the findings as `spec_verification` findings.
   - Validate them with your normal adversarial checks (existence, contradiction, severity).
   - A spec gap is NOT a code bug — do not conflate the two. Spec gaps are advisory
     findings suggesting the spec needs clarification, not that the code is wrong.
   - Spec gaps alone do NOT cause a FAIL verdict. They contribute to WARN if the gaps
     could lead to implementation ambiguity.
3. If no `completeness_gate` in the explorer output, skip this step.
```

### Interaction with existing pipeline

- **Step 4a (explorer launch)**: No change — the spec-verification explorer already runs when `--spec` is provided.
- **Step 4b (judge Step 5)**: Add 5c.5 for gate evaluation.
- **Step 6 (report)**: Spec verification section already exists. Gate results appear as findings in the "Spec Verification" section of the report. Add a gate summary if the gate was evaluated:
  ```
  ### Spec Contract Completeness
  | Check | Status | Detail |
  |-------|--------|--------|
  | State Transitions | PASS | 4 states, 6 transitions, no gaps |
  | Error/Edge Behavior | GAP | Payment gateway timeout unspecified |
  | Cross-Requirement | PASS | No contradictions |
  | Testability | GAP | REQ-012 has no testable threshold |
  ```
- **Findings schema**: No change — gate gaps are standard findings with `pass: "spec_verification"`.

### Edge cases

- **No spec provided**: Spec-verification explorer doesn't run → no gate → nothing changes.
- **Spec is too vague for gate assessment**: Explorer marks all gate items as N/A with detail explaining why. No gap findings.
- **Spec is a brief acceptance criteria list (not a full spec)**: The gate still runs but most items will be N/A or PASS. Acceptance criteria lists don't typically have state transitions or integration points. This is fine — the gate adds value proportional to spec complexity.
- **Large-diff chunked mode**: Spec verification runs as a global pass (already the case). The gate runs once, globally, not per-chunk.

### Files to modify

- `skills/codereview/prompts/reviewer-spec-verification-pass.md` — Add Phase 6 (contract completeness gate)
- `skills/codereview/prompts/reviewer-judge.md` — Add Step 5c.5 (gate evaluation)
- `skills/codereview/references/report-template.md` — Add gate summary table to spec verification section
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: gate all PASS, gate with gaps, no spec, vague spec

### Effort: Small-Medium

---

## Feature 7: Output File Batching for Large Reviews

**Goal:** When reviews produce large volumes of findings (20+ findings from 8 explorers), prevent the judge's context window from being overwhelmed by writing explorer results to disk instead of passing them inline. This is the same pattern the AgentOps council uses — and it becomes critical as we add more context to explorer prompts (domain checklists, prescan signals, historical risk, test pyramid data).

### The problem

Currently, each explorer's findings are collected by the orchestrator and passed to the judge in a single prompt:
```
## Explorer Findings
<JSON arrays from all explorers>
```

For a typical review with 4 core + 2-3 extended explorers producing 5-15 findings each, this is 35-100 findings × ~200 tokens per finding = 7,000-20,000 tokens of findings JSON. Add the context packet (~10-20k), deterministic scan results (~2-5k), spec (~0-10k), and the judge prompt itself (~3k), and the judge's input can reach 40-60k tokens, leaving limited room for investigation and output.

For large-diff chunked reviews, the problem is worse: all chunk explorers' findings are sent to the final judge — potentially 100+ findings from 20+ explorers.

### Solution

Write explorer findings to temp files. The judge reads them with the Read tool during its analysis, controlling how much context it loads at once.

### Where it fits

Modification to **Step 4a** (explorer result collection) and **Step 4b** (judge prompt construction) in SKILL.md. Also applies to Step 4-L (chunked mode).

### Implementation

**Step 4a change — write explorer results to disk:**

After each explorer completes, the orchestrator writes its findings to a temp file:
```bash
# Explorer results written by orchestrator (not by the explorer itself)
/tmp/codereview-explorer-correctness.json
/tmp/codereview-explorer-security.json
/tmp/codereview-explorer-reliability.json
/tmp/codereview-explorer-test-adequacy.json
/tmp/codereview-explorer-error-handling.json   # if ran
/tmp/codereview-explorer-api-contract.json     # if ran
/tmp/codereview-explorer-concurrency.json      # if ran
/tmp/codereview-explorer-spec-verification.json # if ran
```

Each file contains the explorer's raw JSON array (or certification object if empty).

**Step 4b change — judge receives file paths, not inline findings:**

Replace the inline findings in the judge prompt with file paths and a summary:

```
## Explorer Findings

Explorer results are written to disk. Read each file to review findings.

| Explorer | File | Finding Count | Key Signals |
|----------|------|--------------|-------------|
| Correctness | /tmp/codereview-explorer-correctness.json | 4 | 1 high (nil map), 2 medium, 1 low |
| Security | /tmp/codereview-explorer-security.json | 2 | 1 high (SQL injection), 1 medium |
| Reliability | /tmp/codereview-explorer-reliability.json | 0 | Certified clean (3 files checked) |
| Test Adequacy | /tmp/codereview-explorer-test-adequacy.json | 3 | 2 missing tests, 1 stale test |
| Error Handling | /tmp/codereview-explorer-error-handling.json | 2 | 1 high (swallowed error) |
| Spec Verification | /tmp/codereview-explorer-spec-verification.json | 5 | 2 not_implemented, 1 partial |

Total: 16 findings across 6 explorers.

Read each file with the Read tool before performing adversarial validation.
Start with the highest-severity signals first.
```

The summary table gives the judge a triage map — it can prioritize reading the files with high-severity findings first, and skip reading files with 0 findings (certified clean).

**Activation threshold:**

This is an optimization, not always needed. Apply file batching when:

| Condition | Mode |
|-----------|------|
| Total explorer findings > 20 | File batching |
| Chunked review mode (any) | File batching (always — too many explorers for inline) |
| Total explorer findings ≤ 20 AND standard mode | Inline (current behavior — simpler) |

Configurable via `.codereview.yaml`:
```yaml
output_batching:
  threshold: 20          # findings count to trigger file batching
  always_in_chunked: true # always use file batching in chunked mode
```

### Interaction with existing pipeline

- **Step 4a**: Orchestrator writes explorer JSON to `/tmp/codereview-explorer-<pass>.json` when batching is active. Otherwise, passes inline as today.
- **Step 4b**: Judge prompt includes file path table instead of inline JSON when batching is active.
- **Step 4-L (chunked mode)**: Always use file batching. Write per-chunk explorer results to `/tmp/codereview-chunk-<N>-<pass>.json`. The final judge receives a manifest of all chunk result files.
- **Judge prompt (`reviewer-judge.md`)**: Add instructions at the top: "If explorer findings are provided as file paths (not inline JSON), use the Read tool to load each file before performing adversarial validation."
- **Cleanup**: Temp files are ephemeral — they're in `/tmp/` and cleaned up by the OS. No explicit cleanup needed.

### Edge cases

- **Judge can't read files (permission issue)**: Fall back to inline passing. The orchestrator detects this if the first Read fails and re-sends findings inline.
- **Explorer produces very large output (>50k tokens)**: The file batching handles this naturally — the judge reads the file and processes it, unlike inline where it would consume prompt space.
- **Empty explorer results**: Write the certification object to the file. The judge reads it and processes Step 0.5 (certification review from Feature 5).

### Files to modify

- `skills/codereview/SKILL.md` — Update Steps 4a and 4b with file batching logic and activation threshold. Update Step 4-L to always use file batching.
- `skills/codereview/prompts/reviewer-judge.md` — Add instruction for reading findings from files when paths are provided
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: threshold exceeded, chunked mode, inline mode, Read failure fallback

### Effort: Small

---

## Execution Order

```
Feature 0 (script extraction)           ← do first, establishes scripting pattern
    │
    │  0a: run-scans.sh        (bash, calls code_intel.py patterns as semgrep fallback)
    │  0b: enrich-findings.py  (python, standalone)
    │  0c: code_intel.py       (python, shared infrastructure — used by 0a, 1, and pipeline)
    │
    │   Group A: Context enrichment (touch SKILL.md Step 2)
    ├── Feature 1 (prescan)              ← Python, imports code_intel.py, SKILL.md Step 2k
    ├── Feature 2 (domain checklists)    ← reference files, SKILL.md Step 2i
    ├── Feature 3 (git risk scoring)     ← bash script, SKILL.md Step 2j + Step 1.5
    │
    │   Group B: Prompt/architecture improvements (no SKILL.md Step 2 changes)
    ├── Feature 4 (test pyramid vocab)   ← prompt + schema edit only
    ├── Feature 5 (per-file certification) ← global contract + judge prompt edit
    ├── Feature 6 (spec completeness gate) ← spec-verification + judge prompt edit
    └── Feature 7 (output file batching) ← SKILL.md Steps 4a/4b + judge prompt edit
```

**Feature 0 should be done first** because it creates `code_intel.py` (shared infrastructure used by Feature 1 and the pipeline) and modifies SKILL.md Steps 2d, 3, and 5. Within Feature 0, build `code_intel.py` (0c) before `run-scans.sh` (0a) since 0a calls `code_intel.py patterns` as a semgrep fallback. `enrich-findings.py` (0b) is independent.

**Feature 1 has a real dependency on Feature 0c** — `prescan.py` imports `code_intel.py`. This is the only hard dependency between features.

**Group A (Features 1-3)** all add sub-steps to SKILL.md Step 2. If done in parallel, coordinate to avoid merge conflicts in that section. Recommended: do them sequentially after Feature 0.

**Group B (Features 4-7)** touch different prompt files and different SKILL.md sections than Group A. They can be done in parallel with Group A and with each other, with two exceptions:
- Features 5 and 7 both modify `reviewer-judge.md` — coordinate if done in parallel.
- Features 5 and 7 both modify `reviewer-global-contract.md` (Feature 5 adds certification, Feature 7 adds file-reading instructions) — coordinate if done in parallel.

**v1.2 Feature 3 (Finding Lifecycle)** is being built separately and can land at any point. It consumes the output of Feature 0b's `enrich-findings.py`.

### Total files to create

| File | Feature | Notes |
|------|---------|-------|
| `skills/codereview/scripts/run-scans.sh` | 0a | Requires jq; calls code_intel.py patterns when semgrep missing |
| `skills/codereview/scripts/enrich-findings.py` | 0b | Requires python3 |
| `skills/codereview/scripts/code_intel.py` | 0c | Shared code intelligence; tree-sitter optional |
| `skills/codereview/scripts/prescan.py` | 1 | Imports code_intel.py |
| `tests/fixtures/prescan/*.py,*.go,*.ts` | 1 | Pattern fixtures per language |
| `tests/fixtures/code_intel/` | 0c | Multi-language fixtures for each subcommand |
| `skills/codereview/scripts/git-risk.sh` | 3 | — |
| `skills/codereview/references/checklist-sql-safety.md` | 2 | ~15 checklist items |
| `skills/codereview/references/checklist-llm-trust.md` | 2 | ~12 checklist items |
| `skills/codereview/references/checklist-concurrency.md` | 2 | ~15 checklist items |
| `tests/fixtures/judge-output.json` | 0b | Test fixture |
| `tests/fixtures/scan-output.json` | 0b | Test fixture |

### Total files to modify

| File | Features | Conflict risk |
|------|----------|---------------|
| `skills/codereview/SKILL.md` | 0, 1, 2, 3, 7 | **High** — do Feature 0 first (Steps 2a-2d, 3, 3.5, 5, 2-L all updated for code_intel.py), then Group A sequentially; Feature 7 touches Steps 4a/4b (different section) |
| `skills/codereview/references/deterministic-scans.md` | 0 | Low |
| `skills/codereview/references/design.md` | 0, 1, 2, 3, 4, 5, 6, 7 | Medium — each feature adds one row to the rationale table |
| `skills/codereview/references/acceptance-criteria.md` | 0, 1, 2, 3, 5, 6, 7 | Medium — each feature adds a section |
| `skills/codereview/references/report-template.md` | 6 | Low — adds gate summary table |
| `skills/codereview/prompts/reviewer-global-contract.md` | 5, 7 | Medium — Feature 5 adds certification, Feature 7 adds file-reading note |
| `skills/codereview/prompts/reviewer-judge.md` | 5, 6, 7 | **Medium** — Feature 5 adds Step 0.5, Feature 6 adds Step 5c.5, Feature 7 adds file-reading instructions |
| `skills/codereview/prompts/reviewer-test-adequacy-pass.md` | 4 | Low |
| `skills/codereview/prompts/reviewer-spec-verification-pass.md` | 6 | Low — adds Phase 6 |
| `skills/codereview/findings-schema.json` | 4 | Low — adds 3 optional fields |
