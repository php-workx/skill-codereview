# Plan: Code Review Skill v1.3

Fourteen features in four groups to improve review quality and reduce agent interpretation variance. Feature 0 is the foundation — it extracts mechanical pipeline steps into scripts. Group A (Features 1-3, 12, 13) enriches explorer context with new data sources. Group B (Features 4-7, 10, 11) improves explorer and judge behavior through prompt and architecture changes. Group C (Features 8-9) adds provenance awareness and pre-existing bug classification. Features 8-9 and 13 inspired by analysis of the Claude Octopus multi-AI review architecture; Features 10-12 from Kodus-AI analysis. All features are independent of each other but Feature 0 should be done first to establish the scripting pattern and minimize merge conflicts in SKILL.md.

### Relationship to v1.2

| v1.2 Feature | Disposition |
|-------------|-------------|
| **0: Script extraction** | Carried forward as v1.3 Feature 0 (identical scope) |
| **1: Git history risk** | Carried forward as v1.3 Feature 3 (unchanged except Tier 1 promotion interaction with large-diff chunking, which now exists on this branch) |
| **2: Test coverage data** | **Dropped.** The `coverage.run_tests: false` path (check existing artifacts) was the default, but in practice coverage artifacts are stale or absent in most repos. The test-adequacy explorer already identifies untested functions by reading test files — measured coverage adds complexity without proportional benefit. If coverage data becomes important later, it can be re-scoped as a future feature. |
| **3: Finding lifecycle** | **Being built separately** by another team. This plan assumes `scripts/lifecycle.py` will exist and consume the enriched findings JSON from Feature 0b's `enrich-findings.py`. The interface contract: `lifecycle.py` reads the output of `enrich-findings.py` (JSON with `findings` array where each finding has `id`, `source`, `pass`, `severity`, `confidence`, `file`, `line`, `summary`, `action_tier`, plus optional fields). |
| **4: Multi-model council** | **Deferred** to Verification Pipeline phase (see `plan-verification-pipeline.md`). The single-model adversarial judge provides good precision. Multi-model adds cost and complexity that isn't justified until the single-model review is battle-tested. |

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

**Optional (semantic search):** `sqlite-vec` + `model2vec` (`pip install sqlite-vec model2vec`) — enables semantic similarity search in the `graph --semantic` subcommand. Finds related code by purpose, not just by name. ~50MB total, numpy-only dependency. For higher-quality embeddings: `pip install sqlite-vec onnxruntime` (~150MB, no PyTorch). Without these, `graph` runs in structural-only mode. See Feature 0c `graph` subcommand for details.

**Optional (deterministic tools):** `radon`, `gocyclo`, `shellcheck`, `semgrep`, `trivy`, `osv-scanner` — all degrade gracefully when missing. When semgrep is not installed, `code_intel.py patterns` provides a lightweight fallback for the most common static analysis checks. `ast-grep` (`npm install -g @ast-grep/cli` or `cargo install ast-grep`) enables structural security pattern matching with bundled rules. Language-specific linters (`ruff`, `golangci-lint`, `clippy`, `biome`) are detected and run when present.

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

**ast-grep integration (when installed):**

When `ast-grep` (aka `sg`) is detected, `run-scans.sh` runs it with a bundled security rule set. ast-grep performs structural AST-level pattern matching — it catches issues that regex misses (e.g., a hardcoded secret inside a function argument vs in a comment). Near-zero false positive rate because matches are structural, not textual.

Inspired by CodeRabbit's `ast-grep-essentials` repository (150+ rules across 13 languages, almost entirely security-focused). We bundle a curated subset of high-value rules:

| Rule Category | What it catches | Languages |
|--------------|----------------|-----------|
| Hardcoded secrets | DB connection strings, API keys, JWT signing secrets with literal values | Python, Go, TS, Java, Rust |
| Weak cryptography | MD5, SHA1 for security purposes, DES/3DES/RC4, ECB mode, small RSA keys | Python, Go, TS, Java, Rust |
| JWT vulnerabilities | No-verify decode, none algorithm, hardcoded signing secrets | Python, TS, Java |
| Injection patterns | Command injection via string concat in exec/system/subprocess calls | Python, Go, TS, Java |
| Insecure config | Debug mode enabled, SSL verify disabled, bind 0.0.0.0, HTTP-only cookies disabled | Python, Go, TS, Java |

Rules are shipped as YAML files in `skills/codereview/rules/ast-grep/` and passed to ast-grep via `sg scan --rule <dir>`. This avoids requiring users to install `ast-grep-essentials` separately.

**Install ast-grep:** `npm install -g @ast-grep/cli` or `cargo install ast-grep`

**Severity mapping:** ast-grep rules include severity (hint/warning). Map: `warning` → `high`, `hint` → `medium`.

**What stays in the agent's hands:** The agent still calls the script from Step 3 and reads the output. The agent does NOT re-interpret `deterministic-scans.md` — it runs the script and consumes the JSON.

**Language-specific linters (when installed):**

Beyond the core tools (semgrep, trivy, shellcheck), `run-scans.sh` detects and runs language-specific linters that catch real issues deterministically. These reduce LLM workload — issues caught by linters don't need AI explorers to find them.

Inspired by CodeRabbit's 40+ linter integration. We prioritize the fastest and most valuable linters per language:

| Language | Linter | What it catches | Install |
|----------|--------|----------------|---------|
| Python | Ruff | Style, imports, type issues, security (replaces Pylint+Flake8, 10-100x faster) | `pip install ruff` |
| Go | golangci-lint | 100+ linters including vet, staticcheck, errcheck, gosec | `go install github.com/golangci-lint/golangci-lint/cmd/golangci-lint@latest` |
| Rust | Clippy | Correctness, performance, style, complexity | Bundled with rustup |
| TypeScript/JS | Biome | Style, correctness, imports (replaces ESLint, much faster) | `npm install -g @biomejs/biome` |

All are optional — missing linters are skipped with a note in `tool_status`. The script runs them on changed files only (not the entire repo). Output is normalized into the standard finding shape.

**CVE/advisory live lookup (optional):** When `WebSearch` or `WebFetch` tools are available and the diff modifies dependency files (`requirements.txt`, `go.mod`, `package.json`, `Cargo.toml`, `pom.xml`, `Gemfile`), `run-scans.sh` can optionally extract added/changed dependency versions and output a `cve_check_needed` list in the tool status. The agent then uses WebSearch to check for recent CVEs not yet in the trivy/osv-scanner databases. This catches zero-day or very recent advisories. Inspired by Claude Octopus's dedicated CVE reviewer role (Perplexity → Gemini fallback for live vulnerability lookup).

The script does NOT perform the web search — it identifies which dependencies changed and flags them. The agent decides whether to search. This keeps the script deterministic.

```json
{
  "findings": [...],
  "tool_status": {...},
  "cve_check_needed": [
    { "ecosystem": "npm", "package": "jsonwebtoken", "version": "9.0.0", "file": "package.json", "line": 15 },
    { "ecosystem": "pip", "package": "cryptography", "version": "41.0.0", "file": "requirements.txt", "line": 8 }
  ]
}
```

**Generated code exclusion:** `run-scans.sh` should skip generated code files before running tools. Beyond the basic globs in `ignore_paths`, add language-framework-aware patterns (inspired by PR-Agent's `generated_code_ignore.toml`):

| Framework | Patterns |
|-----------|----------|
| Protobuf | `*.pb.go`, `*.pb.cc`, `*_pb2.py`, `*.pb.swift`, `*.pb.rb`, `*.pb.h` |
| OpenAPI | `__generated__/**`, `openapi_client/**`, `openapi_server/**` |
| GraphQL | `*.graphql.ts`, `*.generated.ts` |
| gRPC | `*_grpc.py`, `*Grpc.java`, `*Grpc.cs`, `*_grpc.ts` |
| Go generate | `*_gen.go`, `*generated.go` |
| Dart | `*.g.dart`, `*.freezed.dart` |

These are applied alongside `.codereview.yaml` `ignore_paths`. The script accepts an optional `--ignore-generated` flag (default: on) to disable if the user wants to review generated code.

**Deletion-only hunk filtering:** When constructing the diff for explorers, `run-scans.sh` and the diff presentation in Step 1 should strip hunks that contain only deleted lines (no additions or context changes). Deleted code rarely contains new bugs — the interesting review targets are additions and modifications. This saves tokens and reduces noise. Inspired by PR-Agent's `handle_patch_deletions()` / `omit_deletion_hunks()` functions. Deleted files are still listed by name (for scope awareness) but their full diff is omitted.

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
8. Generates `llm_prompt` field for each finding (see below)
9. Ranks within each tier by `severity_weight * confidence`
10. Computes `tier_summary` counts
11. Outputs enriched findings JSON to stdout

**`llm_prompt` field generation (step 8):**

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
| `graph` | Unified dependency graph: definitions → references → callers → co-change frequency | Step 2m (cross-file context planner), Step 2h (context packet) |
| `format-diff` | Transform unified diff into LLM-optimized before/after block format | Step 2 (diff preparation, before context packet assembly) |

#### The `graph` subcommand — review-time dependency graph

Inspired by CodeRabbit's CodeGraph, which builds a dependency map that enables finding bugs **outside the diff range** by traversing from changed symbols to their consumers, producers, and implicit dependents.

CodeRabbit maintains a persistent graph (rebuilt per review). We build it at review time from the changed files outward. This is slower for the first review but requires zero infrastructure.

**How it works:**

1. Parse changed files with tree-sitter → extract all defined symbols (functions, classes, types, constants) and all references (calls, imports, type annotations)
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

- **Step 2m (cross-file planner):** Instead of generating search patterns from scratch, the planner receives the pre-built graph. It can immediately see which files depend on changed symbols — no grep needed for direct dependencies. The planner focuses its LLM call on identifying *non-obvious* relationships (symmetric counterparts, configuration dependents) that the structural graph misses.
- **Step 2h (context packet):** The graph's 1-hop neighborhood (files that call/import changed symbols) is summarized in the context packet. Explorers see "these files depend on your changes" without having to discover this themselves.
- **Step 2-L Phase A (large-diff mode):** The graph provides the cross-chunk interface summary — which chunks have dependencies on each other.
- **Risk tiering (Step 1.5):** Files with high co-change frequency with changed files get promoted to higher risk tiers.

**Depth control:** Default depth is 1 (changed files + their direct dependents). `--depth 2` traverses two hops but is significantly slower and produces larger graphs. Depth 1 covers the vast majority of cross-file bugs. Depth 2 is useful for large-diff mode where cross-chunk dependencies need deeper tracing.

**Caching (optional):** The graph can be cached in `.codereview-cache/graph-<repo-hash>.json` (structural) and `.codereview-cache/semantic-<repo-hash>.db` (semantic index) for faster subsequent reviews. When cached, only the delta (new/modified files) needs re-parsing and re-embedding. First review builds from scratch; subsequent reviews update incrementally.

**Relationship to Feature 12 (cross-file planner):** The structural graph says "file B calls function X from file A." The semantic layer says "function `check_auth_token` is similar in purpose to `validate_session`." The planner (Feature 12) says "file A changed the hash algorithm — search for the corresponding verify function." Together, structural + semantic + planner cover three layers of cross-file relationships: explicit dependencies, implicit similarity, and domain-specific patterns.

#### Semantic layer (`--semantic` flag)

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

- **Step 2m (cross-file planner):** The planner sees both structural edges ("B calls A") and semantic edges ("C is similar to A"). For semantic edges with score > 0.8, the planner treats them as high-priority: "these functions may be symmetric counterparts — investigate whether the change to A requires a matching change to C."
- **Step 2h (context packet):** Semantic neighbors of changed functions are included as "Related by Purpose" in the context packet, alongside "Related by Dependency" from structural edges.
- **Explorers:** See both types of relationships. The correctness explorer can investigate semantic neighbors for consistency violations. The security explorer can check whether a security fix to one function was also applied to a semantically similar function.

**Graceful degradation chain:**

```
sqlite-vec + model2vec installed     → full semantic search (fastest)
sqlite-vec + onnxruntime installed   → full semantic search (best quality)
sqlite-vec only (no embedding lib)   → no semantic search, structural graph only
nothing extra installed              → no semantic search, structural graph only
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

#### The `format-diff` subcommand — LLM-optimized diff transformation

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

**SKILL.md integration:**

In Step 2 (before context packet assembly), after loading the diff:

```
If python3 and scripts/code_intel.py are available:
  FORMATTED_DIFF=$(git diff $BASE_REF | python3 scripts/code_intel.py format-diff --expand-context)
Otherwise:
  FORMATTED_DIFF=$(git diff $BASE_REF)
```

The formatted diff replaces the raw diff everywhere it's used:
- **Step 2h (context packet):** Explorers receive the formatted diff instead of raw unified diff
- **Step 4a (explorer launch):** Each explorer's diff input is the formatted version
- **Step 4b (judge):** Judge sees the formatted diff when verifying findings

The raw diff is still available for deterministic tools (run-scans.sh, prescan.py) that expect standard unified diff format.

**No optional dependencies.** This subcommand works with just Python 3 — no tree-sitter needed for the basic transformation. Tree-sitter only enhances it via `--expand-context` (function boundary detection). Without tree-sitter, hunk headers show line numbers only (no function names), and `--expand-context` uses keyword-based heuristics.

**Evidence this works:**
- CodeRabbit and PR-Agent both use this format in production (independently developed, convergent design)
- Diff-XYZ benchmark: search/replace (separated blocks) scored 0.96 EM vs 0.90 for unified diff on Apply tasks with large models
- ContextCRBench: understanding developer intent (which the formatted hunk headers convey) boosted F1 by 72-80%
- Aider: unified diffs with explicit markers reduced GPT-4 "laziness" by 3x vs blocks without markers — our format preserves +/- markers within each block

#### The `setup` subcommand — interactive dependency installation

When `/codereview` runs for the first time (or when invoked explicitly), the agent runs `code_intel.py setup --check` to detect what's installed. If recommended dependencies are missing, the agent asks the user whether to install them before proceeding with the review.

**The flow (driven by SKILL.md instructions, not a standalone script):**

```
Step 0 (first review only): Dependency Setup

1. Run: python3 scripts/code_intel.py setup --check --json
2. Read the JSON output
3. If all recommended deps are present → skip to Step 1 (review proceeds)
4. If deps are missing:
   a. Show the user what's installed vs missing (human-readable summary)
   b. Ask: "Install full recommended set? This gives you semantic search,
           AST security rules, and language-specific linters — significantly
           better reviews. (~250MB, one-time install)

           → yes (recommended) / skip"
   c. If "yes": run code_intel.py setup --install --tier full
   d. If "skip": continue with what's available
5. After install (or skip), write marker: .codereview-cache/setup-complete
6. On subsequent reviews, check for marker → skip Step 0 entirely
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
  ✓ python3 3.11.4 (venv: /Users/dev/project/.venv)
  ✓ jq 1.7.1
  ✓ bash 5.2.26

STRUCTURAL ANALYSIS (tree-sitter)
  ✓ tree-sitter 0.22.0
  ✓ tree-sitter-python
  ✗ tree-sitter-go          pip install tree-sitter-go
  ✓ tree-sitter-typescript
  ✓ tree-sitter-java
  ✗ tree-sitter-rust         pip install tree-sitter-rust

SEMANTIC SEARCH (graph --semantic)
  ✗ sqlite-vec               pip install sqlite-vec
  ✗ model2vec                pip install model2vec
  ✗ onnxruntime              pip install onnxruntime

LINTERS & STATIC ANALYSIS
  ✓ semgrep 1.67.0
  ✓ trivy 0.51.1
  ✓ shellcheck 0.9.0
  ✗ ast-grep                 npm install -g @ast-grep/cli
  ✗ ruff                     pip install ruff
  ✓ golangci-lint 1.57.2
  ✗ biome                    npm install -g @biomejs/biome

COMPLEXITY
  ✓ radon 6.0.1
  ✗ gocyclo                  go install github.com/fzipp/gocyclo/cmd/gocyclo@latest

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

    print(f"✓ {len(newly_installed)} dependencies installed successfully")
    if still_missing:
        print(f"✗ {len(still_missing)} failed — see errors above")
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

**Edge cases:**

- **No pip available**: Should not happen (we require python3), but if it does, warn and skip pip installs.
- **npm not available**: Skip ast-grep and biome. Note: "ast-grep and biome require npm — install Node.js to enable AST security rules and JS/TS linting."
- **go not available**: Skip golangci-lint and gocyclo. Note: "golangci-lint requires Go — install Go to enable Go linting."
- **pip install fails (permission denied on system Python)**: Retry with `--user` flag. If that fails too, suggest: "Consider creating a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`"
- **npm install -g fails (permission denied)**: Suggest: "Try `npm install -g --prefix ~/.local @ast-grep/cli`" or "Use npx instead (slower but no install needed)."
- **Partial install success**: Report what succeeded and what failed. Don't block the review — proceed with what's available.
- **Offline environment**: pip/npm/go install will fail. User should pre-install dependencies or use `--skip-setup` flag.
- **CI environment**: Setup should be done in CI setup step, not during review. The marker file (`.codereview-cache/setup-complete`) can be pre-created to skip the interactive prompt. Or: `code_intel.py setup --install --tier full --non-interactive` (no prompt, just install).

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

- `skills/codereview/SKILL.md` — Update Steps 2a-2b (optional code_intel integration), 2d (code_intel.py complexity), 3 (run-scans.sh, code_intel.py patterns fallback), 3.5 (code_intel.py exports for adaptive pass selection), 5 (enrich-findings.py). Update Step 2-L Phase A (code_intel.py imports for cross-chunk interfaces). Update Step 1 (target resolution) to add deletion-only hunk filtering and generated code exclusion to diff preparation. Keep logic descriptions as documentation, clearly marked "implemented by script."
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

**Pattern checks (6 categories):**

| ID | Pattern | Severity | Tree-sitter detection (structural) | Regex fallback (all languages) |
|----|---------|----------|-------------------------------------|-------------------------------|
| P-SEC | Hardcoded secrets | critical | Same as regex (secrets are string literals, not structural) | `(password\|secret\|api_key\|token)\s*=\s*['"][^'"]+['"]` in non-test files |
| P-ERR | Swallowed errors | high | Query: error-handling nodes with empty/pass-only bodies. Covers `except: pass` (Python), `if err != nil { }` (Go), empty `catch {}` (TS/Java/Rust), `_ = err` (Go) | `except.*:\s*pass`, `catch\s*\([^)]*\)\s*\{\s*\}`, `_ = err` |
| P-LEN | Long functions | medium | Query: function/method definition nodes, compute `end_line - start_line`. Works for all languages with function nodes. | Count lines between `def`/`func`/`function`/`fn` and closing brace/dedent (heuristic, less accurate) |
| P-TODO | TODO/FIXME markers | low | Same as regex (comments are leaves, regex is sufficient) | `TODO\|FIXME\|XXX\|HACK` |
| P-COMMENT | Commented code | low | Query: comment nodes containing language keywords (`def`, `func`, `function`, `class`, `if`, `for`, `return`) | `^\s*[#//]\s*(def \|func \|function \|class \|if \|for \|return )` |
| P-DEAD | Dead code candidates | medium | Query: function definition names → scan for call-site references in same file. Functions defined but never called locally. Excludes entry points, test functions, exported/public functions. | Name-based grep: extract `def foo`/`func Foo`/`function foo` definitions, grep for `foo(` calls in same file. Higher false-positive rate. |
| P-STUB | Stub/placeholder logic | high | Query: function bodies containing only `pass`/`return None`/`return nil`/`return null`/`return 0`/`return ""`/`throw "not implemented"`, or bodies with <5 non-comment lines and a TODO marker. Also flags `PLACEHOLDER` or `FIXME: implement` markers. | `def.*:\s*pass$`, `return nil$`, `return null$`, `NotImplementedError`, `todo!()`, `unimplemented!()` |
| P-UNWIRED | Unwired components | medium | Query: function/class definitions in changed files → scan import graph for any file that imports them. Definitions with zero importers outside their own file are "unwired." Excludes: test files, entry points, exported API surface. Uses `code_intel.py imports` and `code_intel.py functions` data. | N/A (requires import graph — tree-sitter only, skipped in regex mode) |

**Implementation completeness levels:**

In addition to individual pattern checks, the prescan assesses each changed file's implementation completeness using a 4-level model inspired by Claude Octopus's implementation verification:

| Level | Name | Criteria | Signal |
|-------|------|----------|--------|
| L1 | Exists | File/function is defined | Lowest — code is present but may be placeholder |
| L2 | Substantive | Function bodies have >5 non-comment lines, no stub markers, non-trivial logic | Code looks real, not placeholder |
| L3 | Wired | Function is imported or called from at least one other file (uses `code_intel.py imports`) | Code is connected to the application |
| L4 | Functional | L3 + no P-STUB findings + no P-UNWIRED findings | Code is complete and integrated |

The assessment uses data already gathered by the pattern checks and `code_intel.py`. Files at L1 or L2 in non-test code are strong signals for explorers — especially when provenance is `ai-assisted` or `autonomous` (Feature 9).

**Note:** Implementation completeness is a prescan signal, not a finding. It's included in the context packet (Step 2h) as a summary. Explorers may investigate L1/L2 files more carefully, but the prescan does not produce findings from the completeness assessment.

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

**File filtering:** Exclude `__pycache__`, `.venv`, `node_modules`, `.git`, `test_fixtures`, `*_test.*` (for P-SEC only), generated code (`*.pb.go`, `*.generated.*`, `*.g.dart`). Uses the same generated code exclusion patterns as `run-scans.sh` (Feature 0a) — protobuf, openapi, graphql, grpc, go-generate, dart codegen. Imported from a shared list to avoid pattern drift between scripts.

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

### Investigation scope discipline

Add to the global contract alongside the certification requirement:

```markdown
## Investigation Scope

Your investigation MUST stay within the scope of the diff and its direct dependencies.

- **In scope:** Changed files, callers of changed functions, callees of changed functions,
  types/interfaces used by changed code, test files for changed code.
- **Out of scope:** Code that is unrelated to the diff, even if it has bugs.

If you discover a bug in unrelated code while tracing a call path, do NOT report it
as a standalone finding. If the bug is relevant because the diff makes it reachable,
report it with `pre_existing: true` (Feature 8). If it's unrelated, ignore it.

This prevents investigation drift — especially in large codebases where every file
has something that could be improved. Your job is to review THIS diff, not audit
the entire repository.
```

Inspired by Claude Octopus's "auto-freeze" pattern, which locks investigation scope to the affected module during debugging. We don't mechanically freeze scope (explorers need Read/Grep across the codebase to trace callers), but we make the discipline explicit in the contract.

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

## Feature 8: Pre-Existing Bug Classification

**Goal:** Distinguish between bugs introduced by the current diff and pre-existing bugs that become newly reachable through the diff's code changes. This reduces noise (reviewers don't want to fix old bugs in a feature PR) while still surfacing important issues (a dormant bug that the PR activates is critical context).

Inspired by Claude Octopus's `pre_existing_newly_reachable` finding field, which tracks bugs that existed before the PR but become reachable via new code paths.

### Where it fits

Schema change (`findings-schema.json`), enrichment script change (`enrich-findings.py` from Feature 0b), explorer prompt change (global contract + calibration examples). No pipeline changes, no new scripts.

### Schema additions

Add two optional fields to the finding schema:

```json
{
  "pre_existing": false,
  "pre_existing_newly_reachable": false
}
```

| Field | Type | When set |
|-------|------|----------|
| `pre_existing` | boolean | `true` when the bug exists in code that was NOT changed in this diff — the explorer traced a call path from changed code into unchanged buggy code |
| `pre_existing_newly_reachable` | boolean | `true` when the bug existed before but the diff creates a new code path that reaches it (e.g., a new caller of a function with an existing nil-map bug) |

Both default to `false` (omitted = bug introduced by the diff, which is the common case).

### Explorer prompt changes

Add to `prompts/reviewer-global-contract.md` in the Output Schema section:

```markdown
## Pre-Existing vs Introduced Bugs

When investigating a potential issue, determine whether the bug is:

1. **Introduced by this diff** (default) — the diff creates the bug. `pre_existing` is false or omitted.
2. **Pre-existing but relevant** — the bug is in unchanged code, but the diff makes it more likely to trigger (new caller, new code path, changed preconditions). Set `pre_existing: true` and `pre_existing_newly_reachable: true`.
3. **Pre-existing and unrelated** — the bug is in unchanged code and no new code path reaches it. **Do not report.** This is noise.

The key question: *Does this diff change the likelihood of this bug being triggered?* If yes, report it with the pre-existing flags. If no, suppress it.
```

Add calibration example to `prompts/reviewer-correctness-pass.md`:

```json
{
  "pass": "correctness",
  "severity": "high",
  "confidence": 0.85,
  "pre_existing": true,
  "pre_existing_newly_reachable": true,
  "file": "src/utils/cache.py",
  "line": 78,
  "summary": "Existing race condition in cache invalidation now reachable from new batch endpoint",
  "evidence": "cache.py:78 has unsynchronized read-modify-write on the cache dict (pre-existing, unchanged in this diff). The new batch_process() endpoint at api/batch.py:34 (added in this diff) calls invalidate_cache() from multiple goroutines. Before this diff, invalidate_cache() was only called from the single-threaded CLI path."
}
```

### Enrichment script changes (Feature 0b)

`enrich-findings.py` applies these rules to pre-existing findings:

1. Pre-existing findings that are NOT newly reachable → drop (should not have been reported, but safety net)
2. Pre-existing + newly reachable + severity high/critical → keep tier as-is (the activation is important)
3. Pre-existing + newly reachable + severity medium/low → downgrade action_tier by one level (e.g., should_fix → consider)

### Interaction with existing pipeline

- **Explorer prompts**: Global contract gets classification guidance. Correctness pass gets one calibration example. Other passes may encounter pre-existing bugs less frequently — no changes needed.
- **Judge (Step 4b)**: No explicit change. The judge already validates findings — it will naturally assess whether pre-existing claims are accurate by checking git blame / diff boundaries.
- **Report**: Pre-existing findings are marked with a `(pre-existing)` label in the report. Grouped separately within their tier to reduce noise.
- **Schema**: Two optional boolean fields added. Backward compatible — omission means `false`.

### Edge cases

- **Explorer unsure if pre-existing**: If the explorer can't determine whether code was changed in the diff, it omits the flag (defaults to introduced). Better to over-report than miss an activation.
- **Pre-existing bug fixed by the diff**: Not a finding. The explorer should not report bugs that the diff fixes.
- **Entire file is new**: No pre-existing bugs possible — the explorer can skip the classification.

### Files to modify

- `skills/codereview/findings-schema.json` — Add `pre_existing` and `pre_existing_newly_reachable` optional boolean fields
- `skills/codereview/prompts/reviewer-global-contract.md` — Add pre-existing vs introduced classification guidance
- `skills/codereview/prompts/reviewer-correctness-pass.md` — Add calibration example
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Feature 9: Provenance-Aware Review Rigor

**Goal:** Adjust review rigor based on how the code was produced. AI-generated code has distinct failure modes (over-abstraction, placeholder logic, unused helpers, mock data in production paths) that human-authored code rarely exhibits. Reviewers who know the provenance can look for the right class of problems.

Inspired by Claude Octopus's provenance-aware review, which elevates scrutiny for AI-assisted and autonomous code with specific risk pattern checklists.

### Where it fits

New CLI flag (`--provenance`), SKILL.md arg parsing, global contract addition, enrichment script change (Feature 0b). No new scripts, no pipeline changes.

### The `--provenance` flag

```bash
/codereview --provenance ai-assisted --base main
/codereview --provenance autonomous --base main
```

| Value | Meaning | Review adjustment |
|-------|---------|-------------------|
| `human` | Written by a person | Standard review (default) |
| `ai-assisted` | Human-directed, AI-generated code (Copilot, Claude suggestions) | Elevated: check for over-abstraction, weak tests, unnecessary flexibility |
| `autonomous` | Fully autonomous agent output (Codex tasks, crank runs, factory mode) | Highest: verify wiring, check for placeholder logic, validate operational safety |
| `unknown` | Provenance not specified | Standard review, no assumptions |

Default when `--provenance` is not provided: `unknown` (equivalent to `human` in practice — no elevated checks).

### SKILL.md changes

Add to Step 1 (argument parsing):

```
**If `--provenance <value>` provided:** Store the provenance value for inclusion in the context packet.
Valid values: human, ai-assisted, autonomous, unknown. Default: unknown.
```

Add to Step 2h (context packet assembly):

```
## Code Provenance: <value>

<provenance-specific instructions from global contract>
```

### Global contract additions

Add a new section to `prompts/reviewer-global-contract.md`:

```markdown
## Provenance-Aware Investigation

If the context packet includes a Code Provenance section, adjust your investigation:

### AI-Assisted Code (elevated rigor)

In addition to your normal focus area checks, look for these AI-codegen risk patterns:

- **Over-abstraction**: Interfaces, factories, or generic wrappers around single implementations. Ask: is there a second implementation? If not, the abstraction is premature.
- **Option-heavy APIs**: Functions with many optional parameters or configuration objects that no caller uses. Check actual call sites.
- **Weak tests**: Tests that assert the code runs without error but don't verify behavior. Tests that mirror implementation rather than testing outcomes.
- **Unnecessary flexibility**: Feature flags, plugin systems, or extension points with no concrete second use case.

### Autonomous Code (highest rigor)

All AI-assisted checks, plus:

- **Placeholder logic**: TODO-driven control flow, stub implementations that return hardcoded values, functions that log but don't act.
- **Unwired components**: Classes/functions defined but never imported or called from the main code path. Check the import graph.
- **Mock/test data in production paths**: Hardcoded test values, example.com URLs, "test" credentials outside of test files.
- **Silent failure handling**: Broad catch/except blocks that swallow errors, missing error propagation, functions that return default values on error without logging.
- **Missing rollback**: Database migrations without down migrations, state changes without recovery paths.
- **Speculative abstractions**: Code that solves problems the spec doesn't mention. Check against spec (if provided) or infer from call sites.

### Human-Authored / Unknown

Standard review. No additional risk patterns — your normal focus area investigation is sufficient.
```

### Enrichment script changes (Feature 0b)

`enrich-findings.py` accepts an optional `--provenance` flag:

```bash
python3 scripts/enrich-findings.py \
  --judge-findings /tmp/codereview-judge.json \
  --scan-findings /tmp/codereview-scans.json \
  --provenance autonomous \
  > /tmp/codereview-enriched.json
```

When provenance is `ai-assisted` or `autonomous`:
- Findings matching AI-codegen risk patterns (placeholder logic, unwired components, mock data) get a severity boost of one level (medium → high) if they would otherwise be classified as `consider` tier
- The `provenance` value is included in the enriched output envelope for downstream consumers

### Schema additions

Add one optional envelope field:

```json
{
  "provenance": "human|ai-assisted|autonomous|unknown"
}
```

### Interaction with existing pipeline

- **SKILL.md Step 1**: Parse `--provenance`, store value
- **SKILL.md Step 2h**: Include provenance in context packet
- **Explorer prompts**: Global contract provides risk patterns. Individual explorer prompts do NOT change — the risk patterns are generic across all focus areas.
- **Judge**: No explicit change. The judge sees provenance in the context and may reference it in the verdict reason.
- **Report**: Add provenance to the report header: `**Provenance:** AI-assisted`
- **Validation script**: Add `provenance` to the optional envelope fields check (valid values: human, ai-assisted, autonomous, unknown).

### Edge cases

- **`--provenance` without value**: Error: "Missing value for --provenance. Valid values: human, ai-assisted, autonomous, unknown"
- **Mixed provenance in a single PR**: Not supported in v1.3. The flag applies globally to the entire review. A future version could support per-file provenance via annotations.
- **Large-diff chunked mode**: Provenance applies to all chunks equally — it's a global setting.

### Files to modify

- `skills/codereview/SKILL.md` — Add `--provenance` to Step 1 arg parsing, add to Step 2h context packet
- `skills/codereview/prompts/reviewer-global-contract.md` — Add provenance-aware investigation section
- `skills/codereview/findings-schema.json` — Add optional `provenance` envelope field, add `pre_existing` and `pre_existing_newly_reachable` to finding fields (if not already added by Feature 8)
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: provenance flag values, AI-codegen risk pattern detection

### Effort: Small-Medium

---

## Feature 10: Phantom Knowledge Self-Check

**Goal:** Add an explicit self-check framework to the global contract that forces explorers to verify they aren't hallucinating about code they cannot see. This is the #1 source of false positives in LLM-based code review — the model claims how unseen code behaves and builds findings on that phantom knowledge.

Inspired by analysis of the Kodus-AI code review platform, which embeds "Phantom Knowledge Detection" as a core guardrail throughout their review prompts. Their safeguard pipeline identifies `targets_unchanged_code` and `requires_assumed_input` as the most common false positive triggers.

### Where it fits

Addition to `prompts/reviewer-global-contract.md` — a new section after the existing Calibration section. No pipeline changes, no scripts, no schema changes.

### Prompt additions

Add to `reviewer-global-contract.md`:

```markdown
## Self-Check: Phantom Knowledge Detection

Before finalizing ANY finding, perform this self-check. Phantom knowledge — making
claims about code you cannot see — is the #1 source of false positives.

**The Rule:** If your finding depends on how code you CANNOT see behaves, STOP.
You are hallucinating.

**Self-check questions (ask all 4 before every finding):**

1. **Am I claiming how unseen code behaves?**
   "The auth system hashes the full key" — can you see the auth system?
   "These are executed as separate calls" — can you see the caller?
   "The default limit is 100" — can you see the config?
   If you cannot point to a specific visible line → DO NOT make the claim.

2. **Am I assuming what an imported function returns or accepts?**
   If code imports `validate_token()` from another file and you cannot see that file,
   you CANNOT claim it "returns None on invalid tokens" or "expects a string argument."
   Only analyze what you can see being used in the visible code.

3. **Am I assuming database schema, API contracts, or external system behavior?**
   "The database column is NOT NULL" — can you see the migration?
   "The API returns a 401 on invalid tokens" — can you see the API code?
   If not, these are assumptions, not findings.

4. **Am I building a finding on an assumption from questions 1-3?**
   A chain of reasoning that starts with an assumption produces a speculative finding,
   no matter how rigorous the rest of the chain is.

**Common traps (red flags in your own output):**
- "The implementation does Y" — verify Y is visible
- "The caller expects..." — verify you traced the caller
- "The system will..." — verify you can see the system
- "This is inconsistent with how X works" — verify you can see X

**Exception:** Code gathered during your investigation (via Read/Grep/Glob)
IS visible evidence. Cross-file context provided in the context packet IS visible.
The self-check only applies to claims about code you never read.
```

### Why this is high-impact

Kodus-AI's production data shows that phantom knowledge findings account for the largest share of false positives. Their "Edward" gatekeeper persona exists primarily to catch this pattern. By embedding the self-check directly into our explorer contract, we catch these at the source instead of filtering them later.

This pairs well with Feature 5 (per-file certification) — certification forces explorers to document what they checked, and the phantom knowledge self-check forces them to verify their claims before reporting.

### Files to modify

- `skills/codereview/prompts/reviewer-global-contract.md` — Add Phantom Knowledge Self-Check section
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Tiny (one prompt section, ~30 lines)

---

## Feature 11: Mental Execution Framing for Correctness Explorer

**Goal:** Reframe the correctness explorer's investigation from pattern-matching to mental code execution. Instead of looking for code that *matches known bug patterns*, the explorer *mentally simulates execution* through changed code paths and reports where execution definitively breaks.

Inspired by analysis of the Kodus-AI code review platform, whose v2 system prompt frames the reviewer as a "Bug-Hunter" performing mental simulation through multiple execution contexts. Their approach produces more concrete findings with traceable execution paths because it forces the LLM to reason about actual runtime behavior rather than surface patterns.

### Where it fits

Enhancement to `prompts/reviewer-correctness-pass.md` — adds a mental execution protocol to the existing investigation phases. No pipeline changes, no scripts, no schema changes.

### Prompt additions

Add to `reviewer-correctness-pass.md` as a preamble to Phase 1:

```markdown
## Mental Execution Protocol

For each changed function, do not pattern-match — mentally execute the code.
Trace variable values, follow control flow, and identify where execution breaks.

### Execution Contexts

Simulate the changed code in these contexts (check all that apply):

1. **Repeated invocations** — Does state accumulate incorrectly across calls?
   Check mutable default arguments, module-level caches, class attributes
   that persist between method calls.

2. **Concurrent execution** — What breaks when two threads/goroutines/requests
   hit this code simultaneously? Check shared mutable state, read-modify-write
   sequences without locks.

3. **Delayed execution** — For callbacks, closures, deferred functions: what
   variable values exist when the code ACTUALLY runs vs when it was scheduled?
   Check loop variable capture, closure over mutable references.

4. **Failure mid-operation** — If this function fails halfway through, what
   state is left behind? Check partial writes, uncommitted transactions,
   resources acquired but not released on error paths.

5. **Cardinality analysis** — Are N operations performed when M unique operations
   would suffice (M << N)? Check loops that do redundant work, repeated
   allocations, duplicate network calls.

### What to report

Only report issues where you can trace the EXACT execution path:
- Specific input values that trigger the issue
- Step-by-step execution showing the failure
- The specific line where behavior is wrong
- The concrete incorrect result

Do NOT report: "this could potentially fail if..." — either trace the failure
or don't report it.
```

### Calibration example addition

Add one calibration example to `reviewer-correctness-pass.md` that demonstrates mental execution:

```json
// TRUE POSITIVE (mental execution traced the failure)
{
  "pass": "correctness",
  "severity": "high",
  "confidence": 0.90,
  "file": "src/cache.py",
  "line": 34,
  "summary": "Mutable default argument accumulates state across calls",
  "evidence": "Mental execution: def process(items, seen={}): ... First call: seen={}, works correctly. Second call: seen still contains entries from first call (mutable default persists). Third call: seen grows further. After N calls, seen contains all items ever processed, causing memory growth and incorrect deduplication.",
  "failure_mode": "Memory leak + incorrect behavior: items processed in earlier calls are treated as 'already seen' in later calls"
}
```

### Files to modify

- `skills/codereview/prompts/reviewer-correctness-pass.md` — Add mental execution protocol preamble, add calibration example
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Tiny (one prompt section + one calibration example)

---

## Feature 12: Cross-File Context Planner

**Goal:** Add an LLM-driven context planning step that analyzes the diff and generates targeted search patterns to find cross-file dependencies the explorers should see. Currently, Step 2 gathers callers/callees via ad-hoc agent Grep — this feature adds structured, diff-aware search planning that catches a broader class of cross-file relationships.

Inspired by analysis of the Kodus-AI code review platform, whose `codeReviewCrossFileContextPlanner` uses an LLM to generate up to 10 targeted ripgrep patterns from the diff, categorized by relationship type. Their "Symmetric/Counterpart Operations" category is particularly valuable — it catches bugs where one side of a paired operation changes but the other doesn't (e.g., changing a hash algorithm on the write side but not the read/verify side).

### Where it fits

New sub-step **2m** in Step 2 (Gather Context), after git risk (2j) and before context packet assembly (2h). See Feature 1 for the step numbering note. Uses `code_intel.py functions` (Feature 0c) for structural context when available.

### Implementation

**Not a script — an LLM planning step.** The orchestrator sends the diff summary to a lightweight LLM call that returns structured search queries. The orchestrator then executes those queries via Grep and includes the results in the context packet.

**Step 2m in SKILL.md:**

```
Step 2m: Cross-File Context Planning

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
| 1 | **Symmetric/Counterpart Operations** | Paired operations where one side changed | Hash algorithm changed in `create_token()` → search for `verify_token()`, `validate_token()` |
| 2 | **Consumers & Callers** | Code that depends on changed signatures/contracts | Function `get_user()` now returns `Optional[User]` → search for callers that don't handle `None` |
| 3 | **Test ↔ Implementation** | Test/impl out of sync | Implementation changed → search for test files; test changed → search for implementation |
| 4 | **Configuration & Limits** | Code that depends on changed config/constants | `MAX_RETRIES` changed from 3 to 10 → search for timeout calculations that depend on it |
| 5 | **Upstream Dependencies** | Local imports whose API may constrain the change | Changed function imports `validate()` from utils → search for `validate()` implementation to understand contract |

**Priority order:** Symmetric counterparts (most critical — these are the bugs that file-level analysis systematically misses) > consumers/callers > upstream dependencies > test ↔ implementation > configuration.

### Planner prompt

New file: `prompts/reviewer-context-planner.md`

```markdown
You are a cross-file context planner. Given a diff summary, generate up to 10
ripgrep search patterns that will find code OUTSIDE the diff that could be
affected by or relevant to the changes.

## Search Categories (in priority order)

### 1. Symmetric/Counterpart Operations (HIGHEST PRIORITY)
When the diff changes one side of a paired operation, search for the other side:
- Create → Validate (hash, token, key generation → verification)
- Encode → Decode (serialize → deserialize, marshal → unmarshal)
- Write → Read (database writes → reads, cache sets → gets)
- Producer → Consumer (event emitters → handlers, queue push → pop)
- Format → Parse (toString → fromString, stringify → parse)
- Map key addition → Map key lookup (adding a key → code that reads keys)

### 2. Consumers & Callers
When the diff changes a function signature, return type, or error behavior:
- Search for all call sites of the changed function
- Focus on callers that may be broken by the change

### 3. Test ↔ Implementation
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

- **Step 2a-2b** (callers/callees): Still runs. The context planner supplements — it finds relationships that callers/callees analysis misses (symmetric operations, config dependents).
- **Step 2h** (context packet): Add "Cross-File Context" section with planner results. Each result includes the rationale so explorers understand why the code is relevant.
- **Step 2-L Phase A** (large-diff mode): Run planner once globally on the full diff summary. Include results in global context (~3-5k tokens).
- **Step 4a** (explorers): No prompt changes needed — the cross-file context is injected via the context packet.

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

### Files to create

- `skills/codereview/prompts/reviewer-context-planner.md` — Cross-file context planner prompt

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 2m, include results in context packet (Step 2h) and Step 2-L Phase A
- `skills/codereview/references/design.md` — Add rationale entry
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: symmetric operation detected, no cross-file relationships, large-diff mode, planner returns 0 results

### Future extension: Context Sufficiency Feedback Loop

This feature is designed as a single-pass planner. Verification Pipeline Feature 6 extends it with a **sufficiency feedback loop**: after collecting context, evaluate whether it's sufficient and generate additional queries if gaps remain. See `docs/plan-verification-pipeline.md` Feature 6.

### Effort: Medium (new prompt + SKILL.md integration + context packet formatting)

---

## Feature 13: REVIEW.md — Repo-Level Review Directives

**Goal:** Support a `REVIEW.md` file in the repository root that provides repo-specific review instructions in a simple, discoverable format. Any developer can read and edit it — no YAML syntax, no config file knowledge needed.

Inspired by Claude Octopus's `REVIEW.md` parsing, which extracts three sections (Always check, Style, Skip) and injects them into every review.

### Where it fits

New sub-step in Step 2 (Gather Context) — the orchestrator reads `REVIEW.md` before assembling the context packet. Also works alongside `.codereview.yaml` `custom_instructions` (which serves a similar purpose but is less discoverable).

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

The orchestrator (SKILL.md Step 2) checks for `REVIEW.md` at the repo root:

```bash
# Step 2l: Read REVIEW.md (if present)
if [ -f "REVIEW.md" ]; then
  # Extract sections
  REVIEW_ALWAYS_CHECK=$(sed -n '/^## Always check$/,/^## /p' REVIEW.md | sed '1d;$d')
  REVIEW_STYLE=$(sed -n '/^## Style$/,/^## /p' REVIEW.md | sed '1d;$d')
  REVIEW_SKIP=$(sed -n '/^## Skip$/,/^## /p' REVIEW.md | sed '1d;$d')
fi
```

**Context packet inclusion (Step 2h):**

```
## Repo-Level Review Directives (from REVIEW.md)

### Mandatory Checks
<contents of Always check section>

### Style Preferences
<contents of Style section>
```

**Skip patterns** are applied in Step 1 (target resolution) alongside `ignore_paths` from config.

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

- `skills/codereview/SKILL.md` — Add Step 2l (read REVIEW.md), add to Step 2h context packet, add to Step 1 skip patterns
- `skills/codereview/references/design.md` — Add rationale entry (why REVIEW.md alongside .codereview.yaml)
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: REVIEW.md present, absent, partially populated, combined with config

### Effort: Small

---

## Feature 14: File-Level Triage (Skip Trivial Changes)

**Goal:** Before launching expensive AI explorer passes, classify each changed file as "trivial" (skip deep review) or "complex" (review deeply). This is CodeRabbit's single most impactful speed optimization — they save ~50% of LLM costs by routing trivial changes (doc updates, variable renames, formatting) to linters-only.

Inspired by CodeRabbit's triage layer, which uses gpt-3.5-turbo to classify diffs. We implement a cheaper heuristic-first approach with optional LLM escalation.

### Where it fits

New **Step 3.6** in SKILL.md, between adaptive pass selection (3.5) and explorer launch (4a). The triage decision feeds into Step 4a — explorers only receive files classified as "complex."

### Triage logic (heuristic-first, no LLM needed)

```
For each changed file:
  1. If file matches ignore_paths → SKIP (already handled by Step 1)
  2. If file is in focus_paths → COMPLEX (always review deeply)
  3. If diff for this file has ≤ 3 changed lines AND no function signature changes → TRIVIAL
  4. If file is test-only and no source file in diff changed → TRIVIAL
  5. If file extension is .md, .txt, .json, .yaml, .yml, .toml, .lock → TRIVIAL
  6. If diff contains only comment changes, import reordering, or whitespace → TRIVIAL
  7. Otherwise → COMPLEX
```

**How tree-sitter/code_intel.py helps (when available):**
- "No function signature changes" is checked structurally: `code_intel.py functions` on old vs new file, compare signatures
- "Only comment changes" is checked via AST: if all changed nodes are comment nodes → TRIVIAL
- Without tree-sitter: fall back to regex heuristics (less accurate, errs toward COMPLEX)

### What happens to trivial files

Trivial files are NOT skipped entirely — they still get:
- Deterministic scans (run-scans.sh, ast-grep) — these are cheap and catch real issues
- Prescan (prescan.py) — fast pattern checks
- Inclusion in the context packet summary (so explorers know they exist)

They do NOT get:
- AI explorer passes (correctness, security, reliability, etc.)
- Cross-file context planner queries

### Triage output

Added to the context packet:
```
## File Triage (Step 3.6)
14 files changed: 9 complex (deep review), 5 trivial (linters only)

Trivial (skipping AI review):
- README.md — documentation only
- src/utils/constants.py — 2 lines changed, no function signatures
- tests/conftest.py — test fixture, no source changes in diff
- package-lock.json — lock file
- .github/workflows/ci.yml — CI config
```

### Configuration

```yaml
triage:
  enabled: true               # disable to review all files deeply
  trivial_line_threshold: 3   # max changed lines to consider trivial
  always_review_extensions:    # never mark as trivial
    - .py
    - .go
    - .rs
    - .ts
    - .java
```

### Edge cases

- **Triage disabled**: All files classified as COMPLEX (current behavior).
- **All files trivial**: Skip explorer launch entirely. Run linters, produce report with linter findings only. Verdict: PASS (no AI review needed).
- **Large-diff chunked mode**: Triage runs per-file before chunk assignment. Trivial files go to Tier 3 (low-risk) automatically.
- **False trivial**: A 2-line change in a config file that changes a security parameter would be classified as trivial. The linters should catch this (especially ast-grep security rules), but if they don't, it's missed. The `always_review_extensions` config mitigates this for source code files.

### Files to modify

- `skills/codereview/SKILL.md` — Add Step 3.6 (file triage), modify Step 4a to receive only complex files
- `skills/codereview/references/design.md` — Add rationale entry (CodeRabbit's ~50% cost savings, heuristic-first approach)
- `skills/codereview/references/acceptance-criteria.md` — Add scenarios: all trivial, all complex, mixed, triage disabled

### Effort: Small-Medium

---

## Feature 15: Path-Based Review Instructions

**Goal:** Allow per-path review instructions in `.codereview.yaml` that inject targeted guidance for specific parts of the codebase. Files matching a path pattern get additional context telling explorers what to focus on.

Inspired by CodeRabbit's `path_instructions` configuration, which is one of their most-used features for customizing review focus.

### Where it fits

Extension to Step 2h (context packet assembly). When assembling the context for a file, check if any `path_instructions` patterns match. If so, inject the instructions.

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

Path instructions are injected into the context packet (Step 2h) as a per-file section:

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

### Files to modify

- `skills/codereview/SKILL.md` — Add path instruction loading to Step 2h
- `docs/CONFIGURATION.md` — Add `path_instructions` to config reference
- `skills/codereview/references/design.md` — Add rationale entry

### Effort: Small

---

## Execution Order

```
Feature 0 (script extraction)           ← do first, establishes scripting pattern
    │
    │  0a: run-scans.sh        (bash, calls code_intel.py patterns + ast-grep as semgrep fallback)
    │  0b: enrich-findings.py  (python, standalone)
    │  0c: code_intel.py       (python, shared infrastructure — used by 0a, 1, 12, 14, and pipeline)
    │       includes `graph` subcommand (review-time dependency graph)
    │
    │   Group A: Context enrichment (touch SKILL.md Step 2)
    ├── Feature 1 (prescan)              ← Python, imports code_intel.py, SKILL.md Step 2k
    ├── Feature 2 (domain checklists)    ← reference files, SKILL.md Step 2i
    ├── Feature 3 (git risk scoring)     ← bash script, SKILL.md Step 2j + Step 1.5
    ├── Feature 12 (cross-file planner)  ← new prompt + SKILL.md Step 2m, uses code_intel.py graph [from Kodus-AI analysis]
    ├── Feature 13 (REVIEW.md support)   ← SKILL.md Step 2l + context packet [from Octopus analysis]
    ├── Feature 15 (path instructions)   ← .codereview.yaml + SKILL.md Step 2h [from CodeRabbit analysis]
    │
    │   Group B: Prompt/architecture improvements
    ├── Feature 4 (test pyramid vocab)   ← prompt + schema edit only
    ├── Feature 5 (per-file certification) ← global contract + judge prompt edit
    ├── Feature 6 (spec completeness gate) ← spec-verification + judge prompt edit
    ├── Feature 7 (output file batching) ← SKILL.md Steps 4a/4b + judge prompt edit
    ├── Feature 10 (phantom knowledge)   ← global contract edit only [from Kodus-AI analysis]
    ├── Feature 11 (mental execution)    ← correctness pass edit only [from Kodus-AI analysis]
    ├── Feature 14 (file-level triage)   ← SKILL.md Step 3.6 + code_intel.py [from CodeRabbit analysis]
    │
    │   Group C: Provenance and classification (schema + prompts + enrich-findings.py)
    ├── Feature 8 (pre-existing bugs)    ← schema + global contract + enrich-findings.py
    └── Feature 9 (provenance-aware)     ← SKILL.md Step 1 + global contract + enrich-findings.py
```

**Feature 0 should be done first** because it creates `code_intel.py` (shared infrastructure used by Features 1, 12, 14 and the pipeline) and modifies SKILL.md Steps 2d, 3, and 5. Within Feature 0, build `code_intel.py` (0c) before `run-scans.sh` (0a) since 0a calls `code_intel.py patterns` and ast-grep as semgrep fallback. `enrich-findings.py` (0b) is independent. The `graph` subcommand is part of Feature 0c but Feature 12 is the primary consumer.

**Feature 1 has a real dependency on Feature 0c** — `prescan.py` imports `code_intel.py`. This is the only hard dependency between features.

**Feature 12 (cross-file planner) is enhanced by Feature 0c's `graph` subcommand.** Without the graph, the planner generates search patterns from the raw diff (still works). With the graph, the planner receives a pre-built dependency neighborhood and focuses its LLM call on non-obvious relationships.

**Feature 14 (file-level triage) benefits from Feature 0c** — tree-sitter-based triage (structural signature comparison, comment-only detection) is more accurate than regex heuristics. Without code_intel.py, triage falls back to line-count heuristics.

**Group A (Features 1-3, 12, 13, 15)** all add sub-steps to SKILL.md Step 2. If done in parallel, coordinate to avoid merge conflicts in that section. Recommended: do them sequentially after Feature 0.

**Features 10-11 are quick wins** — prompt-only changes with no pipeline/schema impact. They can be done at any point, even before Feature 0. Recommended: do them early for immediate false positive reduction while the larger infrastructure features are being built.

**Group B (Features 4-7, 10-11, 14)** touch different prompt files and different SKILL.md sections than Group A. They can be done in parallel with Group A and with each other, with two exceptions:
- Features 5 and 7 both modify `reviewer-judge.md` — coordinate if done in parallel.
- Features 5, 7, 10 all modify `reviewer-global-contract.md` — coordinate if done in parallel. Feature 10 adds a self-check section (independent of Features 5 and 7).

**Group C (Features 8-9)** both modify the global contract, `findings-schema.json`, and `enrich-findings.py` (Feature 0b). They should be done after Feature 0b lands. They are independent of each other and can be done in parallel.

**v1.2 Feature 3 (Finding Lifecycle)** is being built separately and can land at any point. It consumes the output of Feature 0b's `enrich-findings.py`.

### Total files to create

| File | Feature | Notes |
|------|---------|-------|
| `skills/codereview/scripts/run-scans.sh` | 0a | Requires jq; calls code_intel.py patterns + ast-grep when available |
| `skills/codereview/scripts/enrich-findings.py` | 0b | Requires python3 |
| `skills/codereview/scripts/code_intel.py` | 0c | Shared code intelligence; tree-sitter optional; includes `graph`, `graph --semantic`, and `setup` subcommands |
| `skills/codereview/scripts/prescan.py` | 1 | Imports code_intel.py |
| `skills/codereview/rules/ast-grep/` | 0a | Bundled ast-grep security rules (YAML) |
| `tests/fixtures/prescan/*.py,*.go,*.ts` | 1 | Pattern fixtures per language |
| `tests/fixtures/code_intel/` | 0c | Multi-language fixtures for each subcommand including graph |
| `skills/codereview/scripts/git-risk.sh` | 3 | — |
| `skills/codereview/references/checklist-sql-safety.md` | 2 | ~15 checklist items |
| `skills/codereview/references/checklist-llm-trust.md` | 2 | ~12 checklist items |
| `skills/codereview/references/checklist-concurrency.md` | 2 | ~15 checklist items |
| `skills/codereview/prompts/reviewer-context-planner.md` | 12 | Cross-file context planner prompt |
| `tests/fixtures/judge-output.json` | 0b | Test fixture |
| `tests/fixtures/scan-output.json` | 0b | Test fixture |

### Total files to modify

| File | Features | Conflict risk |
|------|----------|---------------|
| `skills/codereview/SKILL.md` | 0, 1, 2, 3, 7, 9, 12, 13, 14, 15 | **High** — Feature 0 adds Step 0 (interactive setup) + Steps 2d, 3, 3.5, 5, 2-L updates; then Group A sequentially; Feature 7 touches Steps 4a/4b; Feature 9 touches Step 1; Feature 12 adds Step 2m; Feature 13 adds Step 2l; Feature 14 adds Step 3.6; Feature 15 extends Step 2h |
| `skills/codereview/references/deterministic-scans.md` | 0 | Low — add ast-grep and language-specific linter documentation |
| `skills/codereview/references/design.md` | 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15 | Medium — each feature adds one row to the rationale table |
| `skills/codereview/references/acceptance-criteria.md` | 0, 1, 2, 3, 5, 6, 7, 9, 12, 13, 14 | Medium — each feature adds a section |
| `skills/codereview/references/report-template.md` | 6 | Low — adds gate summary table |
| `skills/codereview/prompts/reviewer-global-contract.md` | 5, 7, 8, 9, 10 | **Medium** |
| `skills/codereview/prompts/reviewer-judge.md` | 5, 6, 7 | **Medium** |
| `skills/codereview/prompts/reviewer-test-adequacy-pass.md` | 4 | Low |
| `skills/codereview/prompts/reviewer-spec-verification-pass.md` | 6 | Low |
| `skills/codereview/prompts/reviewer-correctness-pass.md` | 8, 11 | Low |
| `skills/codereview/findings-schema.json` | 0b, 4, 8, 9 | Low — Feature 0b adds `llm_prompt` field; Feature 4 adds 3 optional fields; Feature 8 adds 2 boolean fields; Feature 9 adds envelope field |
| `docs/CONFIGURATION.md` | 14, 15 | Low — add triage config and path_instructions |
