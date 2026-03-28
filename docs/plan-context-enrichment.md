# Plan: Context Enrichment

Improve review quality by enriching the context available to AI explorers. Pre-compute code intelligence (callers, functions, imports, dependency graphs), add domain-specific checklists, enable cross-file analysis, and support repo-level review directives.

## Status

| Feature | Status | Notes |
|---------|--------|-------|
| F0c: code_intel.py | Not started | Foundation — build first |
| F0b: enrich-findings.py | Not started | Post-explorer finding enrichment |
| F1: Prescan | Not started | Depends on code_intel.py |
| F2: Domain Checklists | Not started | Independent |
| F12: Cross-File Planner | Not started | Enhanced by code_intel.py graph |
| F13: REVIEW.md Directives | Not started | Independent |
| F15: Path-Based Instructions | Not started | Independent |

## Design Principles

**Scripts Over Prompts** — Wherever a step is mechanical (deterministic rules, data transformation, tool invocation, arithmetic), implement it as a script. This eliminates agent divergence and makes the pipeline testable.

**Use scripts for:** Tool detection/invocation, data transformation, rule-based classification, hash computation, file I/O and artifact management.

**Use AI for:** Understanding code semantics, investigating call paths and data flow, assessing severity, cross-cutting synthesis, report narration.

**The boundary is judgment.** If a step requires reading code and reasoning about behavior, it's an AI task. If it's applying a formula or running a tool, it's a script.

**Checklists Over Instructions** — When giving AI explorers domain-specific context, provide concrete checklist items (questions to answer, patterns to look for) rather than open-ended instructions. Checklists constrain investigation scope and produce more consistent findings across runs.

### Script dependencies

**Required:** `bash` 3.2+ (macOS default), `python3` 3.8+ (for `code_intel.py`, `prescan.py`, `enrich-findings.py` — if absent, agent falls back to manual execution for those steps), `jq` 1.6+ (for `run-scans.sh` JSON manipulation — hard dependency for that script).

**Optional (enhanced analysis):** `tree-sitter` + language grammars (`pip install tree-sitter tree-sitter-python tree-sitter-go tree-sitter-typescript tree-sitter-java tree-sitter-rust`) — enables structural code analysis across all languages. Without tree-sitter, `code_intel.py` falls back to `radon`/`gocyclo` for complexity and regex for other subcommands.

**Optional (semantic search):** `sqlite-vec` + `model2vec` (`pip install sqlite-vec model2vec`) — enables semantic similarity search in the `graph --semantic` subcommand. Finds related code by purpose, not just by name. ~50MB total, numpy-only dependency. For higher-quality embeddings: `pip install sqlite-vec onnxruntime` (~150MB, no PyTorch). Without these, `graph` runs in structural-only mode. See Feature 0c `graph` subcommand for details.

**Optional (deterministic tools):** `radon`, `gocyclo`, `shellcheck`, `semgrep`, `trivy`, `osv-scanner` — all degrade gracefully when missing. When semgrep is not installed, `code_intel.py patterns` provides a lightweight fallback for the most common static analysis checks. `ast-grep` (`npm install -g @ast-grep/cli` or `cargo install ast-grep`) enables structural security pattern matching with bundled rules. Language-specific linters (`ruff`, `golangci-lint`, `clippy`, `biome`) are detected and run when present.

## Architecture Context

The current pipeline is driven by `scripts/orchestrate.py` with these phases:

1. **`orchestrate.py prepare`** — Target detection, diff extraction, config loading, triage, pass selection, prompt assembly. Produces `launch.json` with explorer waves.
2. **Explorers** — AI sub-agents launched in parallel waves. Each explorer investigates a specific domain (correctness, security-config, security-dataflow, test-adequacy, etc.) and writes JSON findings.
3. **`orchestrate.py post-explorers`** — Collects explorer outputs, runs deterministic scans, assembles judge input.
4. **Judge** — Single AI sub-agent that adversarially validates all findings. Produces filtered/scored JSON.
5. **`orchestrate.py finalize`** — Enriches findings, renders report, writes artifacts.

Context is assembled in `prepare()` and fed to explorers via assembled prompt files. **This plan adds new context sources to that assembly.**

SKILL.md is a thin wrapper that drives the flow — it delegates deterministic work to `orchestrate.py` rather than containing pipeline logic itself.

**Already built (not in this plan):**
- `scripts/run-scans.sh` (F0a) — Deterministic scan orchestration, already integrated into `post-explorers` phase
- `scripts/git-risk.sh` (F3) — Git history risk scoring, already integrated into `prepare` phase
- File-level triage (F14) — Built into `orchestrate.py prepare`, classifies files as skip/skim/full-review

**Security explorer split:** The original single security explorer has been split into `security-config` (core, always runs) and `security-dataflow` (activated when dataflow patterns detected). This is already implemented in `orchestrate.py`.

## Execution Order

**Wave 1** (parallel, no deps):
- F2 (Domain Checklists) — new markdown files + orchestrate.py integration
- F13 (REVIEW.md Directives) — orchestrate.py reads REVIEW.md
- F15 (Path-Based Instructions) — .codereview.yaml extension

**Wave 2** (after Wave 1 or parallel if no file conflicts):
- F0c (code_intel.py) — the main infrastructure piece
- F0b (enrich-findings.py) — post-explorer enrichment

**Wave 3** (depends on F0c):
- F1 (Prescan) — imports code_intel.py
- F12 (Cross-File Planner) — uses code_intel.py graph

---

## Feature 0c: `scripts/code_intel.py` — Shared Code Intelligence Module

Originally this feature was `complexity.sh` — a bash script wrapping `radon` (Python) and `gocyclo` (Go). This left TypeScript, Java, Rust, and every other language without complexity analysis.

`code_intel.py` replaces `complexity.sh` and becomes the shared code intelligence layer that multiple pipeline steps use. It provides language-agnostic structural analysis via **tree-sitter** (optional, with fallback to `radon`/`gocyclo`/regex when tree-sitter is not installed).

**What it provides:**

| Subcommand | What it extracts | Used by |
|-----------|-----------------|---------|
| `complexity` | Per-function cyclomatic complexity (all languages) | `orchestrate.py prepare` complexity analysis (replaces radon/gocyclo calls) |
| `functions` | Function definitions: name, params, return type, line range, exported/private | `orchestrate.py prepare` context gathering (replaces ad-hoc agent Grep for callers) |
| `imports` | Import/require/use statements: module, names, line | Large-diff Phase A (reliable cross-chunk interface detection) |
| `exports` | Public API surface: exported functions, classes, types | Adaptive pass selection (structural detection) |
| `callers` | Call sites for a given function name, with file + line + context | Context gathering (replaces ad-hoc agent Grep) |
| `patterns` | Lightweight static analysis checks (semgrep fallback) | `run-scans.sh` (when semgrep not installed) |
| `graph` | Unified dependency graph: definitions -> references -> callers -> co-change frequency | Cross-file context planner (F12), context packet assembly |
| `format-diff` | Transform unified diff into LLM-optimized before/after block format | Diff preparation in `orchestrate.py prepare`, before context packet assembly |

### The `graph` subcommand — review-time dependency graph

Inspired by CodeRabbit's CodeGraph, which builds a dependency map that enables finding bugs **outside the diff range** by traversing from changed symbols to their consumers, producers, and implicit dependents.

CodeRabbit maintains a persistent graph (rebuilt per review). We build it at review time from the changed files outward. This is slower for the first review but requires zero infrastructure.

**How it works:**

1. Parse changed files with tree-sitter -> extract all defined symbols (functions, classes, types, constants) and all references (calls, imports, type annotations)
2. For each defined symbol that was modified in the diff, search the repo for external references (files that import or call the changed symbol) — uses Grep, bounded to top 20 results
3. For each importing file found, parse it to extract its own symbols and references — building a 1-hop dependency neighborhood
4. Query git log for co-change frequency: files that frequently change together with the changed files in the last 6 months (reuses git-risk.sh data when available)
5. Output a unified graph with nodes (symbols) and edges (references, calls, imports, co-changes)

**Output:**
```json
{
  "nodes": [
    { "id": "src/auth/login.py::validate_session", "kind": "function", "file": "src/auth/login.py", "line": 42, "modified_in_diff": true },
    { "id": "src/api/views.py::handle_request", "kind": "function", "file": "src/api/views.py", "line": 78, "modified_in_diff": false }
  ],
  "edges": [
    { "from": "src/api/views.py::handle_request", "to": "src/auth/login.py::validate_session", "type": "calls", "line": 82 },
    { "from": "src/api/views.py", "to": "src/auth/login.py", "type": "co_change", "frequency": 8 }
  ],
  "stats": { "nodes": 24, "edges": 31, "files_traversed": 8, "depth": 1 }
}
```

**How the pipeline uses the graph:**

- **Cross-file planner (F12):** Instead of generating search patterns from scratch, the planner receives the pre-built graph. It can immediately see which files depend on changed symbols — no grep needed for direct dependencies. The planner focuses its LLM call on identifying *non-obvious* relationships (symmetric counterparts, configuration dependents) that the structural graph misses.
- **Context packet:** The graph's 1-hop neighborhood (files that call/import changed symbols) is summarized in the context packet. Explorers see "these files depend on your changes" without having to discover this themselves.
- **Large-diff mode Phase A:** The graph provides the cross-chunk interface summary — which chunks have dependencies on each other.
- **Risk tiering:** Files with high co-change frequency with changed files get promoted to higher risk tiers.

**Depth control:** Default depth is 1 (changed files + their direct dependents). `--depth 2` traverses two hops but is significantly slower and produces larger graphs. Depth 1 covers the vast majority of cross-file bugs. Depth 2 is useful for large-diff mode where cross-chunk dependencies need deeper tracing.

**Caching (optional):** The graph can be cached in `.codereview-cache/graph-<repo-hash>.json` (structural) and `.codereview-cache/semantic-<repo-hash>.db` (semantic index) for faster subsequent reviews. When cached, only the delta (new/modified files) needs re-parsing and re-embedding. First review builds from scratch; subsequent reviews update incrementally.

**Relationship to Feature 12 (cross-file planner):** The structural graph says "file B calls function X from file A." The semantic layer says "function `check_auth_token` is similar in purpose to `validate_session`." The planner (Feature 12) says "file A changed the hash algorithm — search for the corresponding verify function." Together, structural + semantic + planner cover three layers of cross-file relationships: explicit dependencies, implicit similarity, and domain-specific patterns.

### Semantic layer (`--semantic` flag)

The structural graph finds cross-file relationships by following explicit references (imports, calls, type annotations). But it misses code related by **purpose** — a function named `check_auth_token` in one module and `validate_session` in another may serve the same role without any explicit dependency between them.

The `--semantic` flag adds vector-based similarity search to the graph, enabling "search by purpose, not just keywords." This is our lightweight equivalent of CodeRabbit's LanceDB semantic index, inspired by MoFlo's local WASM-based embedding approach but implemented in Python with lighter, faster libraries.

**Technical stack (two tiers, graceful degradation):**

| Tier | Embedding Library | Vector Storage | Install Size | Embedding Speed | When to use |
|------|------------------|---------------|-------------|----------------|-------------|
| **Lightest** | `model2vec` | `sqlite-vec` | ~50MB | 500x faster than MiniLM (static lookup) | Default when `--semantic` is used. numpy-only dependency. Slightly lower recall, massively faster. |
| **Best quality** | `onnxruntime` (MiniLM-L6-v2 ONNX) | `sqlite-vec` | ~150MB | ~2,200 embeddings/sec on CPU | When `onnxruntime` is installed. Identical embeddings to MoFlo/CodeRabbit's MiniLM. No PyTorch needed. |

Both tiers use `sqlite-vec` for storage and search — a pure-C SQLite extension with zero dependencies that runs everywhere SQLite runs. At code review scale (<50K vectors per repo), brute-force cosine similarity in sqlite-vec is fast enough (sub-second for 384-dimensional vectors). No HNSW index needed at this scale.

**Why NOT the heavier options:**

| Library | Why skip |
|---------|---------|
| `sentence-transformers` | Pulls in PyTorch (~500MB+). Massive for an optional feature. |
| `ChromaDB` | Heavy dependency chain, known installation issues on macOS/Python 3.12+. |
| `LanceDB` | pyarrow dependency is large. Great at billion-vector scale, overkill here. |
| `faiss-cpu` | Designed for 100M+ vectors. Unnecessarily complex for <50K. |

**How it works:**

1. After building the structural graph (steps 1-5 above), extract the **text representation** of each node: function name + parameter names + return type + docstring (if present) + first 3 lines of body. This produces a ~50-200 token text per symbol.

2. Generate a 384-dimensional embedding vector for each text:
   - If `model2vec` is installed: use a distilled MiniLM model (~8MB on disk). First run downloads/distills automatically, cached in `.codereview-cache/models/`. Produces embeddings in microseconds per symbol.
   - Elif `onnxruntime` is installed: use a pre-exported MiniLM-L6-v2 ONNX model (~80MB, downloaded once, cached). Produces embeddings in ~0.5ms per symbol.
   - Else: skip semantic layer, warn: "Neither model2vec nor onnxruntime installed — semantic search disabled."

3. Store embeddings in a sqlite-vec database (`.codereview-cache/semantic-<repo-hash>.db`). Each row: `(symbol_id TEXT, file TEXT, kind TEXT, embedding FLOAT[384])`.

4. For each changed symbol in the diff, query the semantic index for the top 5 most similar symbols in the repo (excluding the symbol itself). These are **semantically related** code that may need attention.

5. Add semantic edges to the graph output:
   ```json
   { "from": "src/auth/login.py::validate_session", "to": "src/middleware/jwt.py::check_auth_token", "type": "semantic_similarity", "score": 0.87 }
   ```

6. The cross-file planner (Feature 12) receives these semantic edges and can immediately see "these functions serve a similar purpose — if one changed, check the other."

**Output with `--semantic`:**

The graph JSON gains two additions:
```json
{
  "nodes": [ ... ],
  "edges": [
    // structural edges (calls, imports, co_change) — same as without --semantic
    { "from": "...", "to": "...", "type": "calls", "line": 82 },
    // NEW: semantic similarity edges
    { "from": "src/auth/login.py::validate_session", "to": "src/middleware/jwt.py::check_auth_token", "type": "semantic_similarity", "score": 0.87 },
    { "from": "src/auth/login.py::validate_session", "to": "src/tests/auth_test.py::test_session_validation", "type": "semantic_similarity", "score": 0.79 }
  ],
  "stats": {
    "nodes": 24, "edges": 38, "files_traversed": 8, "depth": 1,
    "semantic": { "enabled": true, "model": "model2vec|onnx-minilm", "symbols_indexed": 142, "index_time_ms": 340 }
  }
}
```

**Incremental indexing (when cache is used):**

The semantic index tracks file modification times. On subsequent reviews:
- Unchanged files: skip embedding, reuse cached vectors
- Modified files: re-parse, re-embed only changed/new symbols, update sqlite-vec rows
- Deleted files: remove from index

First review of a medium repo (~500 files, ~2000 functions): ~5-15 seconds for indexing (model2vec tier) or ~30-60 seconds (onnxruntime tier). Subsequent reviews with cache: <1 second delta update.

**How the pipeline uses semantic edges:**

- **Cross-file planner (F12):** The planner sees both structural edges ("B calls A") and semantic edges ("C is similar to A"). For semantic edges with score > 0.8, the planner treats them as high-priority: "these functions may be symmetric counterparts — investigate whether the change to A requires a matching change to C."
- **Context packet:** Semantic neighbors of changed functions are included as "Related by Purpose" in the context packet, alongside "Related by Dependency" from structural edges.
- **Explorers:** See both types of relationships. The correctness explorer can investigate semantic neighbors for consistency violations. The security explorer can check whether a security fix to one function was also applied to a semantically similar function.

**Graceful degradation chain:**

```
sqlite-vec + model2vec installed     -> full semantic search (fastest)
sqlite-vec + onnxruntime installed   -> full semantic search (best quality)
sqlite-vec only (no embedding lib)   -> no semantic search, structural graph only
nothing extra installed              -> no semantic search, structural graph only
```

The graph subcommand always works — `--semantic` is additive. Without the optional dependencies, it produces the structural graph exactly as described above.

**Install:**

```bash
# Lightest (recommended for most users)
pip install sqlite-vec model2vec

# Best embedding quality
pip install sqlite-vec onnxruntime

# Both (model2vec used by default, onnxruntime available via --embedding-model onnx)
pip install sqlite-vec model2vec onnxruntime
```

### Interface (subcommand-based CLI)

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

# Dependency graph — structural only
echo "$CHANGED_FILES" | python3 scripts/code_intel.py graph [--depth 1] [--cache .codereview-cache/] > /tmp/codereview-graph.json

# Dependency graph — structural + semantic similarity
echo "$CHANGED_FILES" | python3 scripts/code_intel.py graph --semantic [--depth 1] [--cache .codereview-cache/] > /tmp/codereview-graph.json

# Dependency graph — semantic with specific embedding model
echo "$CHANGED_FILES" | python3 scripts/code_intel.py graph --semantic --embedding-model onnx [--cache .codereview-cache/] > /tmp/codereview-graph.json

# Transform unified diff into LLM-optimized format
git diff $BASE_REF | python3 scripts/code_intel.py format-diff > /tmp/codereview-formatted.diff

# With dynamic context expansion to function boundaries (requires tree-sitter)
git diff $BASE_REF | python3 scripts/code_intel.py format-diff --expand-context > /tmp/codereview-formatted.diff
```

All subcommands read `CHANGED_FILES` from stdin (newline-delimited) and output JSON to stdout. Exception: `format-diff` reads a unified diff from stdin and outputs the formatted diff to stdout (not JSON).

### Output formats

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

// patterns (semgrep fallback)
{
  "analyzer": "tree-sitter|regex-only",
  "findings": [
    { "pattern": "sql-injection", "severity": "high", "file": "src/api/orders.py", "line": 34,
      "summary": "String concatenation in execute() call", "evidence": "cursor.execute(\"SELECT * FROM orders WHERE id=\" + order_id)" }
  ],
  "tool_status": { "tree_sitter": "ran|not_installed" }
}
```

### `patterns` subcommand — lightweight semgrep fallback

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

### Architecture

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

### Tree-sitter language support

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

### How other pipeline steps use code_intel.py

| Pipeline step | Current approach | With code_intel.py |
|--------------|-----------------|-------------------|
| **Context gathering** (callers/callees) | Agent uses ad-hoc Grep — matches in comments, strings, variable names | Orchestrator runs `code_intel.py functions` + `code_intel.py callers --target X` — structural extraction, agent consumes JSON |
| **Complexity analysis** | Inline bash calling radon/gocyclo — Python and Go only | Orchestrator runs `code_intel.py complexity` — all languages |
| **Large-diff Phase A** (import graph) | Agent uses Grep to find imports — fragile | Orchestrator runs `code_intel.py imports` — reliable cross-chunk interface detection |
| **Deterministic scans** | semgrep or nothing for code patterns | `run-scans.sh` calls `code_intel.py patterns` when semgrep is not installed |
| **Adaptive pass selection** | Agent greps diff for `goroutine\|async def\|Mutex` — matches in comments | Orchestrator runs `code_intel.py exports` — structural detection of public API, concurrency constructs |

All `code_intel.py` integrations are **optional** — the pipeline works without `code_intel.py` by falling back to the current behavior (agent Grep, radon/gocyclo, no semgrep fallback). Each integration should be presented as:

```
If python3 and scripts/code_intel.py are available:
  <run code_intel.py subcommand, consume JSON>
Otherwise:
  <existing approach (agent Grep, radon/gocyclo, etc.)>
```

This avoids making the Python dependency mandatory while giving a significantly better experience when it's present.

### The `format-diff` subcommand — LLM-optimized diff transformation

**No leading AI code review tool feeds raw unified diffs to LLMs.** Both CodeRabbit and PR-Agent/Qodo independently arrived at the same approach: split diffs into "new hunk / old hunk" before/after blocks with line numbers on the new code only. Academic research (Diff-XYZ benchmark, ContextCRBench) confirms that diff format significantly impacts LLM comprehension.

The `format-diff` subcommand transforms a standard unified diff (`git diff` output) into a format optimized for LLM code review. This is a deterministic transformation — no LLM call, no optional dependencies, pure text processing.

**Why this matters:**

Unified diffs interleave old and new code with `+`/`-` prefixes. The LLM must mentally reconstruct what changed:

```diff
@@ -42,7 +42,9 @@ def validate_session(request, token):
-    session = cache.get(token)
-    if session:
-        return session
+    session = cache.get(token)
+    if session and not session.expired:
+        session.refresh()
+        return session
     return None
```

The before/after format makes the change explicit:

```
## File: src/auth/session.py

@@ def validate_session (line 42)
__new hunk__
42  session = cache.get(token)
43 +if session and not session.expired:
44 +    session.refresh()
45 +    return session
46  return None
__old hunk__
 session = cache.get(token)
-if session:
-    return session
 return None
```

Key properties of the output format:
- **Separated old/new into distinct labeled blocks** — the LLM processes each independently, no interleaving
- **Line numbers on new code only** — explorers need line numbers for findings, but old code is reference-only
- **Function/class name in hunk header** — `@@ def validate_session (line 42)` rather than cryptic `@@ -42,7 +42,9 @@`
- **`+`/`-` markers preserved within each block** — explicit change markers help comprehension (confirmed by research: Diff-XYZ benchmark found familiar markers outperform verbose alternatives)
- **Per-file sections with file path header** — clear file boundaries

**Dynamic context expansion (`--expand-context`):**

When tree-sitter is available, `--expand-context` extends each hunk's context to the enclosing function or class boundary instead of the default 3-line window. This means the LLM sees the complete function being modified, not a random slice.

Inspired by PR-Agent's dynamic context system, which uses asymmetric expansion:
- Before: expand up to enclosing function/class signature (max 8 lines)
- After: expand down to end of enclosing block (max 3 lines past the hunk)

Without tree-sitter, `--expand-context` falls back to heuristic boundary detection (scan upward for `def`/`func`/`function`/`fn`/`class` keywords).

**Implementation:**

```python
def format_diff(unified_diff: str, expand_context: bool = False) -> str:
    """Transform unified diff into LLM-optimized before/after block format."""
    files = parse_unified_diff(unified_diff)  # Split into per-file chunks

    output = []
    for file_diff in files:
        output.append(f"## File: {file_diff.path}")

        for hunk in file_diff.hunks:
            # Determine enclosing function/class name for the hunk header
            func_name = find_enclosing_function(file_diff.path, hunk.new_start)
            header = f"@@ {func_name} (line {hunk.new_start})" if func_name else f"@@ line {hunk.new_start}"

            if expand_context and has_treesitter():
                hunk = expand_to_function_boundary(hunk, file_diff.path)

            output.append(header)

            # New hunk: lines with line numbers, + markers on added lines
            output.append("__new hunk__")
            for line in hunk.new_lines:
                prefix = "+" if line.added else " "
                output.append(f"{line.number:>4} {prefix}{line.text}")

            # Old hunk: lines without line numbers, - markers on removed lines
            # Only include if there are removed lines (skip for pure additions)
            if hunk.has_removals:
                output.append("__old hunk__")
                for line in hunk.old_lines:
                    prefix = "-" if line.removed else " "
                    output.append(f" {prefix}{line.text}")

            output.append("")  # blank line between hunks

    return "\n".join(output)
```

**`orchestrate.py` integration:**

In the prepare phase (before context packet assembly), after loading the diff:

```
If python3 and scripts/code_intel.py are available:
  FORMATTED_DIFF=$(git diff $BASE_REF | python3 scripts/code_intel.py format-diff --expand-context)
Otherwise:
  FORMATTED_DIFF=$(git diff $BASE_REF)
```

The formatted diff replaces the raw diff everywhere it's used:
- **Context packet:** Explorers receive the formatted diff instead of raw unified diff
- **Explorer launch:** Each explorer's diff input is the formatted version
- **Judge:** Judge sees the formatted diff when verifying findings

The raw diff is still available for deterministic tools (run-scans.sh, prescan.py) that expect standard unified diff format.

**No optional dependencies.** This subcommand works with just Python 3 — no tree-sitter needed for the basic transformation. Tree-sitter only enhances it via `--expand-context` (function boundary detection). Without tree-sitter, hunk headers show line numbers only (no function names), and `--expand-context` uses keyword-based heuristics.

**Evidence this works:**
- CodeRabbit and PR-Agent both use this format in production (independently developed, convergent design)
- Diff-XYZ benchmark: search/replace (separated blocks) scored 0.96 EM vs 0.90 for unified diff on Apply tasks with large models
- ContextCRBench: understanding developer intent (which the formatted hunk headers convey) boosted F1 by 72-80%
- Aider: unified diffs with explicit markers reduced GPT-4 "laziness" by 3x vs blocks without markers — our format preserves +/- markers within each block

### The `setup` subcommand — interactive dependency installation

When `/codereview` runs for the first time (or when invoked explicitly), the agent runs `code_intel.py setup --check` to detect what's installed. If recommended dependencies are missing, the agent asks the user whether to install them before proceeding with the review.

**The flow (driven by SKILL.md instructions, not a standalone script):**

```
Step 0 (first review only): Dependency Setup

1. Run: python3 scripts/code_intel.py setup --check --json
2. Read the JSON output
3. If all recommended deps are present -> skip to Step 1 (review proceeds)
4. If deps are missing:
   a. Show the user what's installed vs missing (human-readable summary)
   b. Ask: "Install full recommended set? This gives you semantic search,
           AST security rules, and language-specific linters — significantly
           better reviews. (~250MB, one-time install)

           -> yes (recommended) / skip"
   c. If "yes": run code_intel.py setup --install --tier full
   d. If "skip": continue with what's available
5. After install (or skip), write marker: .codereview-cache/setup-complete
6. On subsequent reviews, check for marker -> skip Step 0 entirely
```

The agent drives this interaction naturally — it asks the question, reads the response, runs the install. No special interactive script needed.

**`setup --check` output (JSON mode for agent consumption):**

```json
{
  "python_env": {
    "type": "venv",
    "path": "/Users/dev/project/.venv",
    "pip_target": "--prefix /Users/dev/project/.venv"
  },
  "installers": {
    "pip": { "available": true, "version": "24.0" },
    "npm": { "available": true, "version": "10.5.0" },
    "go": { "available": true, "version": "1.22.1" },
    "cargo": { "available": false },
    "rustup": { "available": false }
  },
  "dependencies": {
    "tree_sitter":       { "installed": true,  "version": "0.22.0", "tier": "minimal", "installer": "pip" },
    "tree_sitter_python":{ "installed": true,  "version": "0.21.0", "tier": "minimal", "installer": "pip" },
    "tree_sitter_go":    { "installed": false, "tier": "minimal", "installer": "pip", "install_cmd": "pip install tree-sitter-go" },
    "tree_sitter_rust":  { "installed": false, "tier": "minimal", "installer": "pip", "install_cmd": "pip install tree-sitter-rust" },
    "sqlite_vec":        { "installed": false, "tier": "full", "installer": "pip", "install_cmd": "pip install sqlite-vec" },
    "model2vec":         { "installed": false, "tier": "full", "installer": "pip", "install_cmd": "pip install model2vec" },
    "onnxruntime":       { "installed": false, "tier": "full", "installer": "pip", "install_cmd": "pip install onnxruntime" },
    "ruff":              { "installed": false, "tier": "full", "installer": "pip", "install_cmd": "pip install ruff" },
    "ast_grep":          { "installed": false, "tier": "full", "installer": "npm", "install_cmd": "npm install -g @ast-grep/cli" },
    "biome":             { "installed": false, "tier": "full", "installer": "npm", "install_cmd": "npm install -g @biomejs/biome" },
    "golangci_lint":     { "installed": false, "tier": "full", "installer": "go",  "install_cmd": "go install github.com/golangci-lint/golangci-lint/cmd/golangci-lint@latest" },
    "gocyclo":           { "installed": false, "tier": "full", "installer": "go",  "install_cmd": "go install github.com/fzipp/gocyclo/cmd/gocyclo@latest" },
    "clippy":            { "installed": false, "tier": "full", "installer": "rustup", "install_cmd": "rustup component add clippy" },
    "semgrep":           { "installed": true,  "version": "1.67.0", "tier": "external", "installer": "pip" },
    "trivy":             { "installed": true,  "version": "0.51.1", "tier": "external", "installer": "brew/apt" },
    "shellcheck":        { "installed": true,  "version": "0.9.0", "tier": "external", "installer": "brew/apt" },
    "radon":             { "installed": true,  "version": "6.0.1", "tier": "external", "installer": "pip" }
  },
  "summary": {
    "installed": 8,
    "total": 17,
    "missing_by_tier": { "minimal": 2, "full": 8 },
    "missing_by_installer": { "pip": 5, "npm": 2, "go": 2, "rustup": 1 }
  }
}
```

**`setup --check` output (human-readable mode, no `--json`):**

```
Code Review Skill — Dependency Status

PYTHON ENVIRONMENT
  OK python3 3.11.4 (venv: /Users/dev/project/.venv)
  OK jq 1.7.1
  OK bash 5.2.26

STRUCTURAL ANALYSIS (tree-sitter)
  OK tree-sitter 0.22.0
  OK tree-sitter-python
  MISSING tree-sitter-go          pip install tree-sitter-go
  OK tree-sitter-typescript
  OK tree-sitter-java
  MISSING tree-sitter-rust         pip install tree-sitter-rust

SEMANTIC SEARCH (graph --semantic)
  MISSING sqlite-vec               pip install sqlite-vec
  MISSING model2vec                pip install model2vec
  MISSING onnxruntime              pip install onnxruntime

LINTERS & STATIC ANALYSIS
  OK semgrep 1.67.0
  OK trivy 0.51.1
  OK shellcheck 0.9.0
  MISSING ast-grep                 npm install -g @ast-grep/cli
  MISSING ruff                     pip install ruff
  OK golangci-lint 1.57.2
  MISSING biome                    npm install -g @biomejs/biome

COMPLEXITY
  OK radon 6.0.1
  MISSING gocyclo                  go install github.com/fzipp/gocyclo/cmd/gocyclo@latest

Installed: 11/20   Missing: 9

Recommended: run 'code_intel.py setup --install --tier full' to install all missing tools
```

**`setup --install` implementation:**

```python
def install(tier: str = "full"):
    env = detect_python_env()
    installers = detect_installers()  # pip, npm, go, cargo, rustup
    deps = get_missing_dependencies(tier)

    # Group by installer
    pip_deps = [d for d in deps if d.installer == "pip"]
    npm_deps = [d for d in deps if d.installer == "npm"]
    go_deps  = [d for d in deps if d.installer == "go"]
    rustup_deps = [d for d in deps if d.installer == "rustup"]

    # Show what will happen
    print("Will install:")
    if pip_deps:
        pip_flag = "--user" if env.type == "system" else ""
        target = f"into {env.path}" if env.type == "venv" else "with --user flag"
        print(f"  pip ({target}):")
        for d in pip_deps:
            print(f"    {d.name}")
    if npm_deps and installers.npm:
        print(f"  npm (global):")
        for d in npm_deps:
            print(f"    {d.name}")
    if go_deps and installers.go:
        print(f"  go (into {go_bin_path()}):")
        for d in go_deps:
            print(f"    {d.name}")
    if rustup_deps and installers.rustup:
        print(f"  rustup:")
        for d in rustup_deps:
            print(f"    {d.name}")

    # Report what CAN'T be installed
    skipped = [d for d in deps if not installers.get(d.installer)]
    if skipped:
        print(f"\n  Cannot install ({', '.join(set(d.installer for d in skipped))} not found):")
        for d in skipped:
            print(f"    {d.name} — needs {d.installer}")

    # Execute installs (grouped by installer for efficiency)
    if pip_deps:
        names = " ".join(d.pip_name for d in pip_deps)
        pip_cmd = f"pip install {pip_flag} {names}".strip()
        run(pip_cmd)

    if npm_deps and installers.npm:
        for d in npm_deps:
            run(d.install_cmd)

    if go_deps and installers.go:
        for d in go_deps:
            run(d.install_cmd)

    if rustup_deps and installers.rustup:
        for d in rustup_deps:
            run(d.install_cmd)

    # Verify
    print("\nVerifying installation...")
    results = check_all()
    newly_installed = [d for d in results if d.just_installed]
    still_missing = [d for d in results if not d.installed and d.tier in (tier, "minimal")]

    print(f"OK {len(newly_installed)} dependencies installed successfully")
    if still_missing:
        print(f"FAIL {len(still_missing)} failed — see errors above")
```

**Python environment detection logic:**

```python
def detect_python_env():
    """Detect where pip install should target."""
    import sys, os

    # Check for virtual environment (venv or virtualenv)
    if sys.prefix != sys.base_prefix:
        return PythonEnv(type="venv", path=sys.prefix, pip_flag="")

    # Check for conda
    if os.environ.get("CONDA_DEFAULT_ENV"):
        conda_prefix = os.environ.get("CONDA_PREFIX", sys.prefix)
        return PythonEnv(type="conda", path=conda_prefix, pip_flag="")

    # Check for pipx (if code_intel.py itself was installed via pipx)
    if "pipx" in sys.prefix:
        return PythonEnv(type="pipx", path=sys.prefix, pip_flag="")

    # System Python — use --user to avoid permission issues
    user_site = site.getusersitepackages()
    return PythonEnv(type="system", path=user_site, pip_flag="--user")
```

**Installer detection logic:**

```python
def detect_installers():
    """Detect which package managers are available."""
    return {
        "pip":    which("pip3") or which("pip"),           # always present (we require python3)
        "npm":    which("npm"),                             # for ast-grep, biome
        "go":     which("go"),                              # for golangci-lint, gocyclo
        "cargo":  which("cargo"),                           # for ast-grep (alternative)
        "rustup": which("rustup"),                          # for clippy
    }
```

**SKILL.md integration (Step 0):**

The agent checks for `.codereview-cache/setup-complete` before every review. If absent:

```
Step 0: Dependency Setup (first review only)

If .codereview-cache/setup-complete does NOT exist:

1. Run: python3 scripts/code_intel.py setup --check --json
2. Parse the JSON output.
3. If summary.missing_by_tier.full > 0:
   Show the user the human-readable check output.
   Ask: "I recommend installing the full dependency set for the best review quality.
         This includes semantic code search, AST security rules, and language-specific
         linters. One-time install, ~250MB.

         Install? (yes / skip)"

   If yes: Run python3 scripts/code_intel.py setup --install --tier full
           Show install results.
   If skip: Note in report footer: "Some optional tools are missing.
            Run code_intel.py setup --check for details."

4. Write .codereview-cache/setup-complete with timestamp.
5. Proceed to Step 1.

If .codereview-cache/setup-complete EXISTS:
  Skip to Step 1.

To re-run setup: /codereview --setup (deletes marker and re-runs Step 0)
```

**Tiers:**

| Tier | What it installs | Use case |
|------|-----------------|----------|
| `minimal` | tree-sitter + all language grammars | Structural analysis only. For users who want minimal footprint. |
| `full` | minimal + sqlite-vec + model2vec + onnxruntime + ruff + ast-grep + biome + golangci-lint + gocyclo + clippy | Full review quality. Recommended for all users. Installs only what's possible (skips tools whose installer is missing). |

Two tiers, not three. "Minimal" covers structural analysis (the foundation everything else builds on). "Full" covers everything we can install. The agent recommends full.

### Edge cases

- **Scripts not executable**: Invoke via `bash scripts/...` / `python3 scripts/...` explicitly. Use the explicit interpreter approach for portability.
- **Python not available**: `code_intel.py` requires Python 3. If `python3` is not available, the agent falls back to performing context gathering manually (as it does today). Log a warning: "python3 not found — falling back to agent-based analysis."
- **Script fails**: If a script exits non-zero, the agent logs the stderr output and falls back to manual execution for that step. Scripts never block the review — they degrade gracefully.
- **Script output is invalid JSON**: The agent validates script output with `jq . < output.json` before consuming. If invalid, fall back to manual execution.
- **tree-sitter installed but grammar missing for a language**: That language falls back to regex/external-tool for all subcommands. Other languages with grammars still use tree-sitter. Log to stderr.
- **File too large (>10,000 lines)**: Skip tree-sitter parsing (memory/time risk). Fall back to regex/external-tool for that file. Log warning.
- **No pip available**: Should not happen (we require python3), but if it does, warn and skip pip installs.
- **npm not available**: Skip ast-grep and biome. Note: "ast-grep and biome require npm — install Node.js to enable AST security rules and JS/TS linting."
- **go not available**: Skip golangci-lint and gocyclo. Note: "golangci-lint requires Go — install Go to enable Go linting."
- **pip install fails (permission denied on system Python)**: Retry with `--user` flag. If that fails too, suggest: "Consider creating a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`"
- **npm install -g fails (permission denied)**: Suggest: "Try `npm install -g --prefix ~/.local @ast-grep/cli`" or "Use npx instead (slower but no install needed)."
- **Partial install success**: Report what succeeded and what failed. Don't block the review — proceed with what's available.
- **Offline environment**: pip/npm/go install will fail. User should pre-install dependencies or use `--skip-setup` flag.
- **CI environment**: Setup should be done in CI setup step, not during review. The marker file (`.codereview-cache/setup-complete`) can be pre-created to skip the interactive prompt. Or: `code_intel.py setup --install --tier full --non-interactive` (no prompt, just install).

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
```

### Files to create

- `scripts/code_intel.py` — Shared code intelligence module (replaces `complexity.sh`)
- `tests/fixtures/code_intel/` — Multi-language fixture files for testing each subcommand (Python, Go, TypeScript at minimum; one file per language with known functions, imports, complexity hotspots, and pattern violations)

### Files to modify

- `scripts/orchestrate.py` — Integrate `code_intel.py` calls into `prepare()` for complexity, functions, callers, imports, exports, format-diff. Add `--setup` flag support. Each integration is optional with graceful fallback.
- `skills/codereview/SKILL.md` — Add Step 0 (dependency setup). Update references to complexity analysis, context gathering, and diff formatting to note code_intel.py integration.
- `skills/codereview/references/deterministic-scans.md` — Add section documenting `code_intel.py patterns` as semgrep fallback
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: tree-sitter available vs not, radon/gocyclo fallback, semgrep fallback patterns, python3 missing, jq missing, script failure fallback, script invalid output, per-language code_intel output

### Acceptance criteria

- `code_intel.py complexity` produces correct output for Python, Go, TypeScript, Java, and Rust files (with tree-sitter) and Python/Go (with radon/gocyclo fallback)
- `code_intel.py functions` extracts function names, params, return types, and line ranges for all supported languages
- `code_intel.py imports` extracts import statements for all supported languages
- `code_intel.py exports` identifies public API surface per language conventions
- `code_intel.py callers` finds call sites for a given target function
- `code_intel.py patterns` detects sql-injection, command-injection, and empty-error-handler patterns
- `code_intel.py graph` produces a valid graph JSON with nodes and edges
- `code_intel.py graph --semantic` adds semantic similarity edges when sqlite-vec + model2vec/onnxruntime are installed
- `code_intel.py format-diff` transforms unified diff into before/after block format
- `code_intel.py format-diff --expand-context` expands context to function boundaries (with tree-sitter)
- `code_intel.py setup --check` detects installed/missing dependencies
- `code_intel.py setup --install` installs missing dependencies grouped by installer
- All subcommands degrade gracefully when tree-sitter is not installed
- All subcommands handle empty input, binary files, and encoding errors without crashing

### Effort: Medium-Large (largest feature in the plan — provides infrastructure used by F1, F12, and the pipeline itself)

---

## Feature 0b: `scripts/enrich-findings.py` — Finding Enrichment and Classification

Currently, the agent performs finding enrichment by reading rules and applying them. This is the most divergence-prone step — agents make different tier assignment choices, skip deduplication steps, or miscalculate severity weights.

**Extract the mechanical parts into a Python script that:**

1. Accepts the judge's output JSON (via `--judge-findings`) — expects the full judge output object with a `findings` key (JSON array). The script extracts `.findings` from the object. Also accepts deterministic findings JSON (via `--scan-findings`) — expects the output of `run-scans.sh` (object with `findings` key).
2. Combines both findings arrays into one list
3. Assigns `source` field: `"ai"` for judge findings (unless already set), `"deterministic"` for scan findings (already set by `run-scans.sh`)
4. Generates stable `id` for each finding: `<pass>-<file-hash>-<line>` where `<file-hash>` is first 4 chars of SHA-256 of the file path
5. Applies confidence floor (drops AI findings below threshold, default 0.65)
6. Applies evidence check (high/critical without `failure_mode` -> downgrade to medium)
7. Assigns `action_tier` mechanically: Must Fix / Should Fix / Consider per the rules table
8. Generates `llm_prompt` field for each finding (see below)
9. Ranks within each tier by `severity_weight * confidence`
10. Computes `tier_summary` counts
11. Outputs enriched findings JSON to stdout

### `llm_prompt` field generation (step 8)

Each finding gets an `llm_prompt` field — a self-contained prompt that another LLM can use to fix the issue. Inspired by Kodus-AI's `llmPrompt` field and CodeRabbit's "Prompt for AI Agents" section.

This is NOT an LLM call — it's a deterministic template filled from the finding's existing fields:

```python
def generate_llm_prompt(finding: dict) -> str:
    """Generate a copy-pasteable prompt for another LLM to fix this issue."""
    parts = [
        f"In {finding['file']} at line {finding['line']}, there is a {finding['severity']} {finding['pass']} issue.",
        finding['summary'],
    ]
    if finding.get('evidence'):
        parts.append(f"Evidence: {finding['evidence']}")
    if finding.get('failure_mode'):
        parts.append(f"This causes: {finding['failure_mode']}")
    if finding.get('fix'):
        parts.append(f"Suggested approach: {finding['fix']}")
    parts.append("Also check for similar patterns in the same file and related files.")
    return " ".join(parts)
```

The `llm_prompt` differs from `summary` in purpose:
- **`summary`** is for the human reading the report — concise, describes the problem
- **`llm_prompt`** is for an LLM that will implement the fix — includes file, line, context, evidence, and an instruction to fix

Generated by default. Disable with `--no-llm-prompts` flag.

### Output format

Consumed by `lifecycle.py` (finding lifecycle, built separately):

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
      "llm_prompt": "In src/auth/login.py at line 42, there is a high security issue. SQL injection via string formatting in user lookup query. Evidence: cursor.execute(f\"SELECT * FROM users WHERE name='{username}'\"). This causes: arbitrary SQL execution with attacker-controlled input. Suggested approach: use parameterized query with %s placeholder. Also check for similar patterns in the same file and related files.",
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

### Interface

```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/codereview-judge.json \
  --scan-findings /tmp/codereview-scans.json \
  --confidence-floor 0.65 \
  > /tmp/codereview-enriched.json
```

### What stays in the agent's hands

- **Deduplication by "same root cause"** — this requires judgment about whether two findings describe the same underlying issue with different wording. The agent does this BEFORE passing findings to the script.
- **"No linter restatement"** — detecting that a finding restates what a linter already catches requires understanding the finding's content. The agent does this BEFORE passing findings to the script.

The agent runs dedup and linter-restatement removal first (using AI judgment), then pipes the clean list to `enrich-findings.py` for mechanical enrichment.

### Edge cases

- **No judge findings**: Script accepts `--judge-findings` pointing to an empty `{"findings": []}` or omitted entirely (uses scan findings only).
- **No scan findings**: Script accepts `--scan-findings` omitted entirely (uses judge findings only).
- **Both empty**: Outputs `{"findings": [], "tier_summary": {}, "dropped": {}}`.
- **Missing required fields**: If a finding is missing `file` or `line`, skip it and log a warning.
- **Invalid JSON input**: Exit with error code and descriptive message.

### Testing

```bash
# Test enrich-findings.py
python3 scripts/enrich-findings.py \
  --judge-findings tests/fixtures/judge-output.json \
  --scan-findings tests/fixtures/scan-output.json \
  | jq '.findings | length'
```

### Files to create

- `scripts/enrich-findings.py` — Finding enrichment and classification
- `tests/fixtures/judge-output.json` — Sample judge output for testing (minimum: 5 findings across 3 passes, including one high-severity without failure_mode to test downgrade, one below confidence floor to test filtering)
- `tests/fixtures/scan-output.json` — Sample run-scans.sh output for testing (minimum: 3 deterministic findings from 2 different tools, including one that would collide with a judge finding to test dedup-by-agent scenario)

### Files to modify

- `scripts/orchestrate.py` — Integrate `enrich-findings.py` into `finalize()` phase. Currently enrichment logic is inline; extract to script call.
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: confidence floor filtering, evidence downgrade, tier assignment, llm_prompt generation, empty inputs

### Acceptance criteria

- Script combines judge + scan findings into a single list
- Stable IDs are generated deterministically (same input = same ID)
- Confidence floor filtering drops low-confidence AI findings
- High/critical findings without `failure_mode` are downgraded to medium
- `action_tier` assignment matches the documented rules table
- `llm_prompt` field is generated for each finding
- `tier_summary` counts are correct
- `dropped` object accurately reports filtering decisions
- Script handles empty, missing, and malformed inputs gracefully

### Effort: Medium

---

## Feature 1: Prescan

**Goal:** Run fast static checks as part of context gathering. Catches obvious issues (hardcoded secrets, dead code, swallowed errors, long functions) in seconds, providing deterministic signals that guide explorers toward high-risk areas. Works across all languages the skill reviews, not just Python.

Inspired by the AgentOps vibe `prescan.sh` pattern, but implemented as Python for multi-language AST support.

### Where it fits

New step in the `orchestrate.py prepare` phase, after complexity analysis and before the context packet assembly.

**Note:** Prescan output is injected into the explorer context packet as a "Prescan Signals" section. It is NOT added to the deterministic findings list and NOT passed to `enrich-findings.py`.

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

Reads `CHANGED_FILES` on stdin (newline-delimited), same as the other scripts. Target resolution is already done in `orchestrate.py prepare` — the prescan does NOT re-resolve targets.

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
    "dead_code":        { "count": 1, "severity": "medium", "findings": [...] },
    "stubs":            { "count": 2, "severity": "high", "findings": [...] },
    "unwired":          { "count": 1, "severity": "medium", "findings": [...] }
  },
  "implementation_completeness": {
    "files_assessed": 12,
    "levels": {
      "L4_functional": ["src/auth/login.py", "src/api/orders.py"],
      "L3_wired": ["src/utils/cache.py"],
      "L2_substantive": ["src/api/batch.py"],
      "L1_exists": ["src/api/webhooks.py"]
    },
    "summary": "10 files at L4 (functional), 1 at L3 (wired but untested), 1 at L2 (substantive but not imported)"
  },
  "summary": { "critical": 1, "high": 4, "medium": 5, "low": 7 }
}
```

The `analyzer` field tells the consumer whether tree-sitter was used or whether the script fell back to regex-only. This matters because tree-sitter checks are structurally aware (fewer false positives) while regex checks are pattern-based (more false positives, but still useful as signals).

Each finding in the arrays has: `file`, `line`, `pattern_id`, `description`, `evidence`.

### Pattern checks (8 categories)

| ID | Pattern | Severity | Tree-sitter detection (structural) | Regex fallback (all languages) |
|----|---------|----------|-------------------------------------|-------------------------------|
| P-SEC | Hardcoded secrets | critical | Same as regex (secrets are string literals, not structural) | `(password\|secret\|api_key\|token)\s*=\s*['"][^'"]+['"]` in non-test files |
| P-ERR | Swallowed errors | high | Query: error-handling nodes with empty/pass-only bodies. Covers `except: pass` (Python), `if err != nil { }` (Go), empty `catch {}` (TS/Java/Rust), `_ = err` (Go) | `except.*:\s*pass`, `catch\s*\([^)]*\)\s*\{\s*\}`, `_ = err` |
| P-LEN | Long functions | medium | Query: function/method definition nodes, compute `end_line - start_line`. Works for all languages with function nodes. | Count lines between `def`/`func`/`function`/`fn` and closing brace/dedent (heuristic, less accurate) |
| P-TODO | TODO/FIXME markers | low | Same as regex (comments are leaves, regex is sufficient) | `TODO\|FIXME\|XXX\|HACK` |
| P-COMMENT | Commented code | low | Query: comment nodes containing language keywords (`def`, `func`, `function`, `class`, `if`, `for`, `return`) | `^\s*[#//]\s*(def \|func \|function \|class \|if \|for \|return )` |
| P-DEAD | Dead code candidates | medium | Query: function definition names -> scan for call-site references in same file. Functions defined but never called locally. Excludes entry points, test functions, exported/public functions. | Name-based grep: extract `def foo`/`func Foo`/`function foo` definitions, grep for `foo(` calls in same file. Higher false-positive rate. |
| P-STUB | Stub/placeholder logic | high | Query: function bodies containing only `pass`/`return None`/`return nil`/`return null`/`return 0`/`return ""`/`throw "not implemented"`, or bodies with <5 non-comment lines and a TODO marker. Also flags `PLACEHOLDER` or `FIXME: implement` markers. | `def.*:\s*pass$`, `return nil$`, `return null$`, `NotImplementedError`, `todo!()`, `unimplemented!()` |
| P-UNWIRED | Unwired components | medium | Query: function/class definitions in changed files -> scan import graph for any file that imports them. Definitions with zero importers outside their own file are "unwired." Excludes: test files, entry points, exported API surface. Uses `code_intel.py imports` and `code_intel.py functions` data. | N/A (requires import graph — tree-sitter only, skipped in regex mode) |

### Implementation completeness levels

In addition to individual pattern checks, the prescan assesses each changed file's implementation completeness using a 4-level model inspired by Claude Octopus's implementation verification:

| Level | Name | Criteria | Signal |
|-------|------|----------|--------|
| L1 | Exists | File/function is defined | Lowest — code is present but may be placeholder |
| L2 | Substantive | Function bodies have >5 non-comment lines, no stub markers, non-trivial logic | Code looks real, not placeholder |
| L3 | Wired | Function is imported or called from at least one other file (uses `code_intel.py imports`) | Code is connected to the application |
| L4 | Functional | L3 + no P-STUB findings + no P-UNWIRED findings | Code is complete and integrated |

The assessment uses data already gathered by the pattern checks and `code_intel.py`. Files at L1 or L2 in non-test code are strong signals for explorers — especially when provenance is `ai-assisted` or `autonomous` (Feature 9, in Plan: Explorer/Judge Behavior).

**Note:** Implementation completeness is a prescan signal, not a finding. It's included in the context packet as a summary. Explorers may investigate L1/L2 files more carefully, but the prescan does not produce findings from the completeness assessment.

**No P-CC (complexity) check.** Complexity analysis is already handled by `scripts/code_intel.py complexity` (Feature 0c). The prescan does NOT duplicate that work. Since `prescan.py` imports `code_intel.py`, it can access the parsed tree — but it does not re-run complexity analysis. If the orchestrator needs prescan + complexity, it runs both scripts and merges the outputs in the context packet.

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
class StubChecker(PatternChecker): ...          # P-STUB: uses code_intel.get_functions() for body analysis
class UnwiredChecker(PatternChecker): ...       # P-UNWIRED: uses code_intel.get_imports() + get_functions()

def main():
    intel = CodeIntel()
    files = sys.stdin.read().strip().split('\n')
    checkers = [SecretChecker(), SwallowedErrorChecker(), LongFunctionChecker(),
                TodoChecker(), CommentedCodeChecker(), DeadCodeChecker(),
                StubChecker(), UnwiredChecker()]
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

### Tree-sitter language support

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

**File filtering:** Exclude `__pycache__`, `.venv`, `node_modules`, `.git`, `test_fixtures`, `*_test.*` (for P-SEC only), generated code (`*.pb.go`, `*.generated.*`, `*.g.dart`). Uses the same generated code exclusion patterns as `run-scans.sh` (already built) — protobuf, openapi, graphql, grpc, go-generate, dart codegen. Imported from a shared list to avoid pattern drift between scripts.

### Prescan is context, not findings

Prescan output is injected into the explorer context packet as a "Prescan Signals" section. It is NOT added to the deterministic findings list and NOT passed to `enrich-findings.py`.

**Why:** Prescan checks are fast heuristics with known false positives (e.g., P-SEC flags `test_password = "hunter2"` in test setup; P-DEAD flags functions called via dynamic dispatch). They serve as attention signals for explorers, not final findings. Explorers investigate the flagged areas with tools (Grep/Read/Glob) and produce proper findings with evidence and confidence scores.

**Dedup with explorer output:** There is no mechanical dedup between prescan signals and explorer findings. The judge already deduplicates explorer findings. If an explorer produces a finding about the same issue a prescan flagged, the explorer's finding is the authoritative one (with evidence, confidence, and failure_mode). The prescan signal is consumed as context and not carried forward.

### Interaction with existing pipeline

- **`orchestrate.py prepare`**: Run `scripts/prescan.py`, read JSON output, include in context packet.
- **Context packet assembly**: Include prescan summary as a "Prescan Signals" section. Critical/high findings get explicit callouts so explorers prioritize investigation of those areas:
  ```
  ## Prescan Signals (fast static checks, tree-sitter mode)
  CRITICAL: 1 potential hardcoded secret detected — investigate with full context
  - src/auth/config.py:17 — P-SEC: possible hardcoded credential

  HIGH: 2 swallowed errors detected
  - src/api/orders.py:45 — P-ERR: except: pass (error swallowed)
  - src/utils/retry.go:23 — P-ERR: empty error handler (if err != nil {})

  6 additional signals (medium/low) omitted for brevity.
  ```
- **Large-diff mode Phase A**: Include prescan in global context. Cap to critical/high signals only to save tokens.
- **Deterministic scans**: Unchanged. Prescan and deterministic scans are separate tracks.

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
- Each checker x each supported language (with tree-sitter)
- Each checker x regex fallback mode
- File filtering (test files excluded from P-SEC, generated code excluded from all)
- Empty file list -> empty output
- File with no issues -> all zeros
- Binary/unreadable files -> skipped gracefully

### Edge cases

- **No Python 3**: The entire prescan is skipped. The orchestrator falls back to no prescan context (all other pipeline steps still work). Log: "python3 not found — prescan skipped."
- **tree-sitter not installed**: Falls back to regex-only mode. All checks still run with regex patterns (except P-UNWIRED which is skipped). `analyzer` field reports `"regex-only"`. Log to stderr: "tree-sitter not installed — using regex-only mode (higher false positive rate). Install: pip install tree-sitter tree-sitter-python tree-sitter-go ..."
- **tree-sitter installed but grammar missing for a language**: That language falls back to regex for structural checks. Other languages with grammars still use tree-sitter. Log: "tree-sitter-java not installed — Java files will use regex-only checks."
- **No shellcheck**: Skip shell-specific P-ERR. Log warning.
- **Empty file list**: Output `{ "file_count": 0, "analyzer": "...", "languages_detected": [], "patterns": {}, "summary": {} }`.
- **Large file lists (>200 files)**: Cap at 200 files (sorted by risk tier if available from triage). Log "Prescan capped at 200 files" in stderr.
- **File too large (>10,000 lines)**: Skip tree-sitter parsing (memory/time risk). Fall back to regex for that file. Log warning.
- **Binary file or encoding error**: Skip the file. Log to stderr.

### Files to create

- `scripts/prescan.py` — Multi-language static pattern prescan
- `tests/fixtures/prescan/` — Fixture files for each language x each pattern (see Testing section)

### Files to modify

- `scripts/orchestrate.py` — Integrate prescan into `prepare()` phase, include output in context packet assembly
- `skills/codereview/SKILL.md` — Add mention of prescan signals in context packet description
- `skills/codereview/references/acceptance-criteria.md` — Add prescan scenarios: tree-sitter mode, regex fallback, per-language checks, no python3, empty files
- `skills/codereview/references/design.md` — Add rationale entry (why Python not bash, why tree-sitter optional, why prescan is context not findings)

### Acceptance criteria

- Prescan detects all 8 pattern categories (P-SEC, P-ERR, P-LEN, P-TODO, P-COMMENT, P-DEAD, P-STUB, P-UNWIRED) in tree-sitter mode
- Prescan detects P-SEC, P-ERR, P-LEN, P-TODO, P-COMMENT, P-DEAD, P-STUB in regex-only mode (P-UNWIRED requires tree-sitter)
- Implementation completeness levels are correctly assigned (L1-L4)
- Prescan output is included in context packet as signals, NOT as findings
- Each pattern checker works for Python, Go, TypeScript, Java, and Rust (where applicable)
- File filtering correctly excludes generated code and test files (for P-SEC)
- Graceful degradation: missing tree-sitter, missing grammars, large files, binary files

### Effort: Medium

---

## Feature 2: Domain-Specific Checklists

**Goal:** Auto-detect code patterns in the diff (SQL/ORM, LLM/AI, concurrency) and inject targeted checklist items into the explorer context. This gives explorers concrete things to look for in specialized domains without changing the explorer prompts themselves.

Inspired by the AgentOps vibe domain checklist pattern, but implemented as static reference files loaded by the orchestrator.

### Where it fits

Integrated into the `orchestrate.py prepare` phase, after context gathering and before context packet assembly. Also applies to large-diff mode as a lightweight global context component.

### Detection logic

The **orchestrator** greps the diff content for trigger patterns. If any pattern matches, the orchestrator reads the corresponding checklist file and includes it in the context packet. This is a simple grep-then-read — no script needed, since the detection is 3 grep calls and the orchestrator already has the diff in context.

**Detection patterns (ERE syntax for `grep -E`):**

| Trigger Pattern (`grep -E` on DIFF) | Checklist File | What It Covers |
|--------------------------------------|----------------|----------------|
| `SELECT\|INSERT\|UPDATE\|DELETE\|JOIN\|SQLAlchemy\|sqlalchemy\|GORM\|gorm\|Prisma\|prisma\|Knex\|knex\|sequelize\|ActiveRecord\|active_record\|\.query\(\|\.execute\(\|\.raw\(` | `references/checklist-sql-safety.md` | SQL injection, parameterized queries, ORM misuse, N+1 patterns, transaction safety, migration risks |
| `anthropic\|openai\|google\.generativeai\|cohere\|replicate\|langchain\|llm\|LLM\|ChatModel\|chat_model\|completion\|embedding` | `references/checklist-llm-trust.md` | Prompt injection, output sanitization, token limits, PII in prompts, model response validation, cost controls |
| `goroutine\|go func\|threading\|Thread\|async def\|asyncio\|\.lock\(\|Mutex\|RwLock\|chan \|channel\|atomic\|sync\.\|Promise\.all\|Worker\(\|spawn\|tokio\|Arc<` | `references/checklist-concurrency.md` | Race conditions, deadlocks, goroutine leaks, shared state, lock ordering, channel misuse, async pitfalls |

**Implementation in orchestrate.py** (not a separate script — inline detection):
```bash
# Domain checklist detection
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
# For each matched checklist, read references/checklist-<name>.md
```

**Why not a separate script?** The detection is 3 grep calls with static patterns. A script would add indirection without benefit. The checklists themselves are static markdown files. If the number of checklists grows beyond ~6, extract detection into a script.

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

The orchestrator includes loaded checklists in the context packet as an additional section:

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
- **Large-diff mode**: Run detection once globally (Phase A), include matched checklists in global context. Each chunk explorer receives the same checklists — they're domain-level, not file-level.
- **False trigger**: A test file imports `sqlalchemy` for test fixtures. The checklist loads, explorers investigate, find nothing, report nothing. No harm — just a few wasted tokens.

### Files to create

- `skills/codereview/references/checklist-sql-safety.md` — SQL/ORM safety checklist (~15 items)
- `skills/codereview/references/checklist-llm-trust.md` — LLM trust boundary checklist (~12 items)
- `skills/codereview/references/checklist-concurrency.md` — Concurrency safety checklist (~15 items)

### Files to modify

- `scripts/orchestrate.py` — Add domain checklist detection and loading to `prepare()` phase, include in context packet assembly and large-diff global context.
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: SQL detected, LLM detected, concurrency detected, multiple match, none match

### Acceptance criteria

- SQL checklist loads when diff contains SQL/ORM patterns
- LLM checklist loads when diff contains AI/LLM patterns
- Concurrency checklist loads when diff contains concurrency patterns
- Multiple checklists can load simultaneously
- No checklist loads when no patterns match (no empty section in context)
- Checklists are included in large-diff global context
- Each checklist file has 10-15 concrete, yes/no questions

### Effort: Small

---

## Feature 12: Cross-File Context Planner

**Goal:** Add an LLM-driven context planning step that analyzes the diff and generates targeted search patterns to find cross-file dependencies the explorers should see. Currently, context gathering finds callers/callees via ad-hoc agent Grep — this feature adds structured, diff-aware search planning that catches a broader class of cross-file relationships.

Inspired by analysis of the Kodus-AI code review platform, whose `codeReviewCrossFileContextPlanner` uses an LLM to generate up to 10 targeted ripgrep patterns from the diff, categorized by relationship type. Their "Symmetric/Counterpart Operations" category is particularly valuable — it catches bugs where one side of a paired operation changes but the other doesn't (e.g., changing a hash algorithm on the write side but not the read/verify side).

### Where it fits

New step in the `orchestrate.py prepare` phase, after git risk analysis and before context packet assembly. Uses `code_intel.py functions` (Feature 0c) for structural context when available.

### Implementation

**Not a script — an LLM planning step.** The orchestrator sends the diff summary to a lightweight LLM call that returns structured search queries. The orchestrator then executes those queries via Grep and includes the results in the context packet.

**Integration in orchestrate.py:**

```
Cross-File Context Planning:

Send the diff summary (changed functions, changed signatures, changed constants)
to a lightweight LLM call with the cross-file context planner prompt.

The planner returns up to 10 search queries, each with:
- pattern: ripgrep pattern to execute
- rationale: why this search matters
- risk_level: low/medium/high
- category: one of the 5 categories below

Execute the queries via Grep (high-risk first, cap at 10 queries).
For each query, include up to 5 results in the context packet.
Total budget: ~5k tokens of cross-file context.
```

### Search categories

| # | Category | What It Catches | Example |
|---|----------|----------------|---------|
| 1 | **Symmetric/Counterpart Operations** | Paired operations where one side changed | Hash algorithm changed in `create_token()` -> search for `verify_token()`, `validate_token()` |
| 2 | **Consumers & Callers** | Code that depends on changed signatures/contracts | Function `get_user()` now returns `Optional[User]` -> search for callers that don't handle `None` |
| 3 | **Test <-> Implementation** | Test/impl out of sync | Implementation changed -> search for test files; test changed -> search for implementation |
| 4 | **Configuration & Limits** | Code that depends on changed config/constants | `MAX_RETRIES` changed from 3 to 10 -> search for timeout calculations that depend on it |
| 5 | **Upstream Dependencies** | Local imports whose API may constrain the change | Changed function imports `validate()` from utils -> search for `validate()` implementation to understand contract |

**Priority order:** Symmetric counterparts (most critical — these are the bugs that file-level analysis systematically misses) > consumers/callers > upstream dependencies > test <-> implementation > configuration.

### Planner prompt

New file: `prompts/reviewer-context-planner.md`

```markdown
You are a cross-file context planner. Given a diff summary, generate up to 10
ripgrep search patterns that will find code OUTSIDE the diff that could be
affected by or relevant to the changes.

## Search Categories (in priority order)

### 1. Symmetric/Counterpart Operations (HIGHEST PRIORITY)
When the diff changes one side of a paired operation, search for the other side:
- Create -> Validate (hash, token, key generation -> verification)
- Encode -> Decode (serialize -> deserialize, marshal -> unmarshal)
- Write -> Read (database writes -> reads, cache sets -> gets)
- Producer -> Consumer (event emitters -> handlers, queue push -> pop)
- Format -> Parse (toString -> fromString, stringify -> parse)
- Map key addition -> Map key lookup (adding a key -> code that reads keys)

### 2. Consumers & Callers
When the diff changes a function signature, return type, or error behavior:
- Search for all call sites of the changed function
- Focus on callers that may be broken by the change

### 3. Test <-> Implementation
- If the diff changes an implementation file: search for its test file
- If the diff changes a test file: search for the implementation it tests

### 4. Configuration & Limits
When the diff changes constants, defaults, thresholds, or config values:
- Search for code that reads or depends on those values

### 5. Upstream Dependencies
When the diff imports a local module and uses it in a new way:
- Search for the imported function/class implementation

## Rules
- Use EXACT symbol names from the diff (copy-paste, don't invent)
- Skip deleted symbols (- prefix lines)
- Use word-boundary patterns: \bsymbolName\b
- Prefer simple single-line patterns
- Max 10 queries total
- Include fileGlob to narrow search when possible (e.g., "*.py", "!*_test.go")

## Output
```json
{
  "queries": [
    {
      "pattern": "\\bverify_token\\b",
      "rationale": "create_token() changed hash algorithm — verify_token() must match",
      "risk_level": "high",
      "category": "symmetric",
      "symbol_name": "verify_token",
      "file_glob": "*.py"
    }
  ]
}
```
```

### Interaction with existing pipeline

- **Callers/callees context gathering**: Still runs. The context planner supplements — it finds relationships that callers/callees analysis misses (symmetric operations, config dependents).
- **Context packet**: Add "Cross-File Context" section with planner results. Each result includes the rationale so explorers understand why the code is relevant.
- **Large-diff mode Phase A**: Run planner once globally on the full diff summary. Include results in global context (~3-5k tokens).
- **Explorers**: No prompt changes needed — the cross-file context is injected via the context packet.

### When to skip

- **Diff touches only one file**: Still run — symmetric counterparts and consumers may exist in other files.
- **Diff is test-only**: Skip — test changes rarely affect cross-file contracts.
- **Diff is docs/config only**: Skip.
- **code_intel.py available**: Use `functions` subcommand output to provide the planner with structured function signatures instead of raw diff text. This produces better search patterns.

### Edge cases

- **Planner returns >10 queries**: Truncate to 10, prioritize by risk_level.
- **Query returns 0 results**: Normal — not all cross-file relationships exist. Log and move on.
- **Query returns >20 results**: Too broad — take top 5 by file relevance (prefer same directory, then same package).
- **Total context exceeds 5k tokens**: Truncate, keeping highest-risk results.

### Future extension: Context Sufficiency Feedback Loop

This feature is designed as a single-pass planner. Verification Pipeline Feature 6 extends it with a **sufficiency feedback loop**: after collecting context, evaluate whether it's sufficient and generate additional queries if gaps remain. See `docs/plan-verification-pipeline.md` Feature 6.

### Files to create

- `skills/codereview/prompts/reviewer-context-planner.md` — Cross-file context planner prompt

### Files to modify

- `scripts/orchestrate.py` — Add cross-file context planning step to `prepare()`, execute queries, include results in context packet and large-diff global context.
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: symmetric operation detected, no cross-file relationships, large-diff mode, planner returns 0 results

### Acceptance criteria

- Planner generates search queries for all 5 categories when applicable
- Symmetric counterpart detection works (e.g., changed `encode` finds `decode`)
- Queries use exact symbol names from the diff
- Results are truncated to 5k token budget
- Cross-file context section appears in context packet with rationale for each result
- Planner is skipped for test-only and docs-only diffs
- When code_intel.py is available, planner receives structured function signatures
- Large-diff mode runs planner once globally

### Effort: Medium (new prompt + orchestrate.py integration + context packet formatting)

---

## Feature 13: REVIEW.md — Repo-Level Review Directives

**Goal:** Support a `REVIEW.md` file in the repository root that provides repo-specific review instructions in a simple, discoverable format. Any developer can read and edit it — no YAML syntax, no config file knowledge needed.

Inspired by Claude Octopus's `REVIEW.md` parsing, which extracts three sections (Always check, Style, Skip) and injects them into every review.

### Where it fits

New step in the `orchestrate.py prepare` phase — the orchestrator reads `REVIEW.md` before assembling the context packet. Also works alongside `.codereview.yaml` `custom_instructions` (which serves a similar purpose but is less discoverable).

### Format

```markdown
# Code Review Guidelines

## Always check
- New API endpoints have corresponding integration tests
- Database migrations are backward-compatible and have rollback scripts
- Authentication changes are reviewed by the security team lead
- All public functions have error return paths documented

## Style
- Prefer early returns over nested conditionals
- Use structured logging (key=value), not printf-style
- Error messages must include the operation that failed and the input that caused it

## Skip
- Generated files under src/gen/
- Formatting-only changes in lock files
- Vendored dependencies
```

### Sections

| Section | How it's used | Interacts with |
|---------|--------------|----------------|
| **Always check** | Injected into the context packet as mandatory checklist items for all explorers. Each item becomes something explorers must investigate and either confirm or flag. | Domain checklists (Feature 2) — `Always check` items are repo-specific, domain checklists are domain-specific. Both appear in the context packet. |
| **Style** | Injected as style preferences for explorers. Findings about style violations use severity `low` and action tier `consider`. | `.codereview.yaml` `custom_instructions` — if both exist, both are included. `REVIEW.md` is more discoverable for the team. |
| **Skip** | Applied as file exclusion patterns before the diff is sent to explorers. Patterns are `grep -v` compatible. | `.codereview.yaml` `ignore_paths` — if both exist, union of both is applied. |

### Implementation

The orchestrator (`orchestrate.py prepare`) checks for `REVIEW.md` at the repo root:

```bash
# Read REVIEW.md (if present)
if [ -f "REVIEW.md" ]; then
  # Extract sections
  REVIEW_ALWAYS_CHECK=$(sed -n '/^## Always check$/,/^## /p' REVIEW.md | sed '1d;$d')
  REVIEW_STYLE=$(sed -n '/^## Style$/,/^## /p' REVIEW.md | sed '1d;$d')
  REVIEW_SKIP=$(sed -n '/^## Skip$/,/^## /p' REVIEW.md | sed '1d;$d')
fi
```

**Context packet inclusion:**

```
## Repo-Level Review Directives (from REVIEW.md)

### Mandatory Checks
<contents of Always check section>

### Style Preferences
<contents of Style section>
```

**Skip patterns** are applied in target resolution alongside `ignore_paths` from config.

### Precedence

| Source | Priority | Notes |
|--------|----------|-------|
| CLI flags (`--ignore-path`) | Highest | Override everything |
| `.codereview.yaml` `ignore_paths` | High | Structured config |
| `REVIEW.md` Skip section | Medium | Human-readable exclusions |
| Built-in defaults | Lowest | `node_modules/`, `.git/` |

For `custom_instructions` / `Always check` / `Style`: all are additive. No precedence needed — they all appear in the context packet.

### Edge cases

- **No `REVIEW.md`**: Skip silently. No error, no warning. Most repos won't have one initially.
- **`REVIEW.md` exists but has no recognized sections**: Ignore. The file might be a general project review document, not review directives.
- **Both `REVIEW.md` and `.codereview.yaml` `custom_instructions`**: Include both. They serve different audiences — `REVIEW.md` is for the team, `custom_instructions` is for the tool.
- **Large `REVIEW.md` (>100 items)**: Cap at 30 items per section. Log warning: "REVIEW.md has >30 items in 'Always check' — truncating. Consider splitting into domain checklists."
- **`REVIEW.md` in subdirectory**: Not supported. Only repo root. Subdirectory-specific rules can go in domain checklists (Feature 2).

### Files to modify

- `scripts/orchestrate.py` — Add REVIEW.md reading to `prepare()` phase, extract sections, add to context packet, add Skip patterns to target resolution.
- `skills/codereview/references/design.md` — Add rationale entry (why REVIEW.md alongside .codereview.yaml)
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: REVIEW.md present, absent, partially populated, combined with config

### Acceptance criteria

- REVIEW.md is read when present at repo root
- "Always check" items appear in context packet as mandatory checks
- "Style" items appear in context packet as style preferences
- "Skip" patterns are applied as file exclusions
- Missing REVIEW.md is silently skipped (no error)
- REVIEW.md with no recognized sections is ignored
- Both REVIEW.md and .codereview.yaml custom_instructions are included when both exist
- Large REVIEW.md (>30 items per section) is truncated with a warning

### Effort: Small

---

## Feature 15: Path-Based Review Instructions

**Goal:** Allow per-path review instructions in `.codereview.yaml` that inject targeted guidance for specific parts of the codebase. Files matching a path pattern get additional context telling explorers what to focus on.

Inspired by CodeRabbit's `path_instructions` configuration, which is one of their most-used features for customizing review focus.

### Where it fits

Extension to context packet assembly in `orchestrate.py prepare`. When assembling the context for a file, check if any `path_instructions` patterns match. If so, inject the instructions.

### Configuration

```yaml
path_instructions:
  - path: "src/auth/**"
    instructions: "Focus on authentication bypass, session management, and token validation. All auth changes must have integration tests."
  - path: "src/api/**/*.ts"
    instructions: "Check for proper error handling, input validation, and rate limiting. API endpoints must validate all path and query parameters."
  - path: "migrations/**"
    instructions: "Verify backward compatibility, rollback safety, and data preservation. Flag any destructive operations (DROP, DELETE, TRUNCATE)."
  - path: "tests/**"
    instructions: "Check assertion quality — tests should verify behavior, not implementation. Flag tests that only assert the code runs without error."
```

### How instructions reach explorers

Path instructions are injected into the context packet as a per-file section:

```
## Path-Specific Instructions (from .codereview.yaml)

For files matching `src/auth/**`:
Focus on authentication bypass, session management, and token validation.
All auth changes must have integration tests.
```

Multiple patterns can match the same file — all matching instructions are included.

### Interaction with existing features

- **REVIEW.md (Feature 13)**: REVIEW.md provides repo-wide directives. Path instructions provide per-path directives. Both are included when both match.
- **Domain checklists (Feature 2)**: Domain checklists are auto-detected from diff content. Path instructions are explicit per-path config. Complementary — a SQL checklist might load automatically while path instructions say "this directory must use parameterized queries."
- **custom_instructions**: Global custom instructions apply everywhere. Path instructions are targeted. Both are included.
- **Large-diff mode**: Path instructions are included in per-chunk context for files matching the pattern.

### Edge cases

- **No `path_instructions` in config**: Skip silently.
- **No files match any pattern**: Skip silently.
- **Multiple patterns match same file**: Include all matching instructions.
- **Glob pattern syntax**: Use `fnmatch`-style globbing (consistent with `ignore_paths`).
- **Very long instructions**: No hard cap, but document recommendation of 1-3 sentences per path pattern.

### Files to modify

- `scripts/orchestrate.py` — Add path instruction matching to `prepare()` phase context packet assembly. Load `path_instructions` from config, match against changed file paths, include in context.
- `docs/CONFIGURATION.md` — Add `path_instructions` to config reference
- `skills/codereview/references/design.md` — Add rationale entry

### Acceptance criteria

- Path instructions are loaded from `.codereview.yaml` `path_instructions` field
- Files matching a path pattern receive the corresponding instructions in their context
- Multiple patterns can match the same file (all instructions included)
- Path instructions work in both standard and large-diff modes
- Missing `path_instructions` config is silently ignored
- Instructions appear in context packet with the matched pattern for transparency

### Effort: Small

---

## File Inventory

### Files to create

| File | Feature | Notes |
|------|---------|-------|
| `scripts/code_intel.py` | F0c | Shared code intelligence; tree-sitter optional; includes `graph`, `graph --semantic`, `format-diff`, and `setup` subcommands |
| `scripts/enrich-findings.py` | F0b | Requires python3 |
| `scripts/prescan.py` | F1 | Imports code_intel.py |
| `skills/codereview/references/checklist-sql-safety.md` | F2 | ~15 checklist items |
| `skills/codereview/references/checklist-llm-trust.md` | F2 | ~12 checklist items |
| `skills/codereview/references/checklist-concurrency.md` | F2 | ~15 checklist items |
| `skills/codereview/prompts/reviewer-context-planner.md` | F12 | Cross-file context planner prompt |
| `tests/fixtures/code_intel/` | F0c | Multi-language fixtures for each subcommand including graph |
| `tests/fixtures/prescan/` | F1 | Pattern fixtures per language (*.py, *.go, *.ts) |
| `tests/fixtures/judge-output.json` | F0b | Test fixture for enrich-findings.py |
| `tests/fixtures/scan-output.json` | F0b | Test fixture for enrich-findings.py |

### Files to modify

| File | Features | Conflict risk |
|------|----------|---------------|
| `scripts/orchestrate.py` | F0c, F0b, F1, F2, F12, F13, F15 | **High** — F0c adds code_intel.py calls to `prepare()`; F0b adds enrich-findings.py call to `finalize()`; F1 adds prescan to `prepare()`; F2 adds checklist detection; F12 adds cross-file planning; F13 adds REVIEW.md reading; F15 adds path instruction matching |
| `skills/codereview/SKILL.md` | F0c, F1 | Low — thin wrapper, most changes are in orchestrate.py |
| `skills/codereview/references/design.md` | F0c, F0b, F1, F2, F12, F13, F15 | Medium — each feature adds one row to the rationale table |
| `skills/codereview/references/acceptance-criteria.md` | F0c, F0b, F1, F2, F12, F13 | Medium — each feature adds a section |
| `skills/codereview/references/deterministic-scans.md` | F0c | Low — add code_intel.py patterns documentation |
| `docs/CONFIGURATION.md` | F15 | Low — add path_instructions to config reference |

## Dependencies on Other Plans

- **F0b (enrich-findings.py)** is consumed by Plan: Explorer/Judge Behavior features F8 (pre-existing bugs) and F9 (provenance-aware review), which add fields to the enrichment pipeline.
- **code_intel.py graph subcommand** (F0c) is used by F12 (Cross-File Context Planner) in this plan — the planner receives the pre-built dependency graph instead of generating search patterns from scratch.
- **prescan.py** (F1) depends on **code_intel.py** (F0c) — it imports `CodeIntel` and `ParsedFile` to avoid duplicate parsing.
- **F12 (Cross-File Planner)** is extended by Verification Pipeline Feature 6 (Context Sufficiency Feedback Loop) in `docs/plan-verification-pipeline.md`.
- **Finding lifecycle** (`scripts/lifecycle.py`, built separately) consumes the output of F0b's `enrich-findings.py`.
