# Spec: First-Class Chunked Review

**Status:** Draft
**Author:** Research session 2026-03-29
**Depends on:** orchestrate.py (existing chunked infrastructure), code_intel.py, cross_file_planner.py, prescan.py
**Context:** The current chunked mode is disabled in SKILL.md. The orchestrator can build chunks but uses naive directory-based clustering. This spec upgrades chunked review to first-class status with semantic clustering, hierarchical judgment, and full integration with the explorer-judge pipeline.

## Problem

Large diffs (1000+ lines, 30+ files) are common in vibe engineering, autonomous agent output, and feature branches. The current review pipeline hits context window limits — each explorer receives the full diff (~40-80k tokens) plus context, leaving insufficient room for investigation. Users don't commit AI code before reviewing it, so git history isn't always available for scoping.

The existing chunked mode (80 files / 8000 lines threshold) uses directory-based clustering, runs all experts on all chunks, and funnels everything through a single judge with no cross-chunk awareness. It's disabled in the skill layer because the quality is insufficient.

### Evidence: Context Enrichment PR Review (2026-03-29)

A real review of 15 changed files (1.3M token diff) against a 70k token budget demonstrated the problem concretely. Three real bugs were missed:

1. **Off-by-one in caller range check** (code_intel.py:724) — boundary condition where `line_start <= line <= line_end` should be end-exclusive. The correctness explorer never saw this section because the diff was truncated.
2. **After-context expansion skips first post-hunk line** (code_intel.py:1777) — `hunk_end = new_start + sum(...)` off by one. Same truncation issue — the format-diff implementation was stripped from the explorer's view.
3. **`llm_used` hardcoded False** (cross_file_planner.py:282) — trivial but valid. The file was new and likely truncated entirely.

**Root cause:** The truncation cascade preserves context (git risk, scan results, standards) and drops diff content. But the diff IS the review — explorers reviewing a skeleton can't find precision bugs. CodeRabbit, which sees the full diff with no budget constraint, caught all three.

**Key design constraint for this spec:** Chunked mode must ensure every explorer sees the FULL diff for its chunk, within budget. This is the single biggest lever for review quality on large diffs.

## Goals

1. **Semantic chunking** — cluster files by call graph connectivity, test-source pairing, and import relationships, not just directory proximity
2. **Token-budget-aware chunk sizing** — balance by estimated token count, not file count
3. **Risk-tiered wave ordering** — critical chunks reviewed first with all experts; low-risk chunks get core experts only
4. **Hierarchical judgment** — per-chunk judges for focused validation + lightweight final judge for dedup and cross-chunk synthesis
5. **Cross-chunk analysis** — detect interface mismatches, data contract breaks, and type changes across chunk boundaries
6. **Full pipeline integration** — certification, completeness gate, provenance, pre-existing classification all work in chunked mode
7. **SKILL.md integration** — remove the "not yet available" gate, make chunked mode transparent to users

## Non-Goals

- Changing the activation threshold (80 files / 8000 lines is fine; tuning is a separate concern)
- Per-file review (chunks are the unit, not individual files)
- Streaming/incremental review (chunks are built upfront, not discovered during review)
- Replacing standard mode for small diffs

## Cost & Latency Analysis

### Cost Model

| Mode | LLM Calls | Estimated Cost (relative) |
|------|-----------|--------------------------|
| **Standard** | N explorers + 1 judge | 1x (baseline) |
| **Chunked (C chunks)** | (N explorers × C chunks) + C per-chunk judges + 1 synthesizer + 1 final judge | ~1.5-2.5x for 3-5 chunks |

The per-chunk judges are cheaper than the standard judge because they review fewer findings (5-15 vs 50-100) in less context. The final judge is lightweight (synthesis, no investigation). The synthesizer is a single focused agent.

**Cost reduction from risk-tiered dispatch:** Low-risk chunks get only core experts (4 instead of 8-10), saving ~40-60% of explorer calls on those chunks. A typical 5-chunk split with 1 critical + 3 standard + 1 low-risk:

```
Standard mode:  10 explorers × 1 = 10 explorer calls + 1 judge = 11 total
Chunked mode:   10×1 (critical) + 4×3 (standard core) + 4×1 (low-risk core)
                + 3×3 (standard extended, where activated)
                = 10 + 12 + 4 + 9 = 35 explorer calls
                + 5 chunk judges + 1 synthesizer + 1 final judge = 42 total
```

Total LLM calls: ~4x more, but each call is cheaper (smaller context). Net cost: ~2-2.5x standard mode. This is acceptable because standard mode on the same diff would either truncate heavily (missing bugs) or fail entirely.

### Latency Model

```
Standard:  prepare (5s) → explorers parallel (60-90s) → post (2s) → judge (60-90s) → finalize (2s)
           Total: ~2-3 minutes

Chunked:   prepare+chunk (8s) → explorers wave 1 (60-90s) → wave 2 (60-90s) → wave 3 (30s)
           → per-chunk post (2s each, parallel) → per-chunk judges (60-90s, parallel with stagger)
           → synthesizer (30-45s) → final judge (15-30s) → finalize (2s)
           Total: ~4-6 minutes
```

**Stagger strategy for per-chunk judges:** Launch with 2-second delay between each to avoid API rate limit spikes. For 5 chunks: chunk 1 at t=0, chunk 2 at t=2s, chunk 3 at t=4s, etc. All finish within the same ~90s window since judge execution time >> stagger delay. Same stagger applies to explorer waves when multiple chunks dispatch in the same wave.

Latency is ~2x standard mode. Acceptable because the alternative is a failed or truncated review.

---

## Design

### Phase 1: Semantic Chunking Pipeline

Replace `build_chunks()` (currently directory-based, max 15 files) with a multi-signal clustering algorithm.

#### Step 1: Build File Relationship Graph

Inputs (already available from `prepare()` context gathering):
- `code_intel.py graph` output → import/export/caller edges
- `code_intel.py functions` output → function definitions per file
- `prescan.py` output → risk signals per file
- Changed files list from diff extraction

Build an adjacency graph where files are nodes and edges are weighted by:

| Edge Type | Weight | Source |
|-----------|--------|--------|
| Direct import (A imports B) | 3 | code_intel imports |
| Caller-callee (function in A calls function in B) | 4 | code_intel graph |
| Test-source pair (test_foo.py ↔ foo.py) | 5 (always same chunk) | Naming convention + import analysis |
| Same directory | 1 | Path analysis |
| Shared external dependency | 1 | code_intel imports (both import same module) |

#### Step 2: Cluster Files

Use the relationship graph to cluster files into chunks via weighted graph partitioning:

**Algorithm: Anchor-Expand-Balance**

```python
def build_semantic_chunks(files, graph, file_tokens, max_chunk_tokens):
    # 1. Compute node weight = sum of edge weights for each file
    node_weights = {f: sum(graph.edge_weight(f, g) for g in graph.neighbors(f)) for f in files}

    # 2. Sort by node weight descending — highest-connected files are anchors
    sorted_files = sorted(files, key=lambda f: node_weights[f], reverse=True)

    # 3. Force test-source pairs (weight=5 edges are hard constraints)
    forced_pairs = find_test_source_pairs(files, graph)  # {test_file: source_file}

    # 4. Greedy expansion from anchors
    chunks = []
    assigned = set()
    for anchor in sorted_files:
        if anchor in assigned:
            continue
        chunk = [anchor]
        chunk_tokens = file_tokens.get(anchor, 0)
        assigned.add(anchor)

        # Add forced pair partner
        if anchor in forced_pairs:
            partner = forced_pairs[anchor]
            chunk.append(partner)
            chunk_tokens += file_tokens.get(partner, 0)
            assigned.add(partner)

        # Expand to neighbors by edge weight (highest first)
        neighbors = sorted(
            [(n, graph.edge_weight(anchor, n)) for n in graph.neighbors(anchor) if n not in assigned],
            key=lambda x: x[1], reverse=True
        )
        for neighbor, weight in neighbors:
            neighbor_tokens = file_tokens.get(neighbor, 0)
            if chunk_tokens + neighbor_tokens <= max_chunk_tokens:
                chunk.append(neighbor)
                chunk_tokens += neighbor_tokens
                assigned.add(neighbor)
                # Also add neighbor's forced pair
                if neighbor in forced_pairs and forced_pairs[neighbor] not in assigned:
                    pair = forced_pairs[neighbor]
                    chunk.append(pair)
                    chunk_tokens += file_tokens.get(pair, 0)
                    assigned.add(pair)

        chunks.append(Chunk(files=chunk, estimated_tokens=chunk_tokens))

    # 5. Orphan collection — unassigned files grouped by directory
    orphans = [f for f in files if f not in assigned]
    if orphans:
        chunks.extend(group_by_directory(orphans, file_tokens, max_chunk_tokens))

    return chunks
```

**Edge weight rationale:**

| Edge Type | Weight | Why this value |
|-----------|--------|----------------|
| Test-source pair | 5 (hard constraint) | Separating test from source makes both reviews worse. Test needs source context to verify; source needs test to check coverage. |
| Caller-callee | 4 | Direct call path — a change in the callee can break the caller. Must review together to catch contract violations. |
| Direct import | 3 | Dependency relationship — importer may use types/constants from importee. Slightly weaker than caller because import doesn't imply active call path in the diff. |
| Same directory | 1 | Weak proxy for relatedness. Files in the same package often share conventions but may be unrelated. Tiebreaker only. |
| Shared external dep | 1 | Both import the same library — weak signal that they're in the same domain. Tiebreaker only. |

**Test-source pairing conventions:**

| Language | Test file pattern | Example |
|----------|------------------|---------|
| Python | `test_{name}.py`, `{name}_test.py`, `tests/test_{name}.py` | `test_auth.py` ↔ `auth.py` |
| Go | `{name}_test.go` (same dir) | `auth_test.go` ↔ `auth.go` |
| TypeScript/JS | `{name}.test.ts`, `{name}.spec.ts`, `__tests__/{name}.ts` | `auth.test.ts` ↔ `auth.ts` |
| Rust | `#[cfg(test)]` in same file, `tests/{name}.rs` | inline or `tests/auth.rs` ↔ `src/auth.rs` |
| Java | `Test{Name}.java`, `{Name}Test.java` | `AuthTest.java` ↔ `Auth.java` |

Matching is case-insensitive, strips common prefixes/suffixes, and uses import analysis as a fallback when naming conventions don't match.

#### Step 3: Token Budget Balancing

Each chunk has a token budget (not a file count limit):

```python
MAX_CHUNK_TOKENS = 15_000  # ~60k chars of diff content
# Derived from: explorer prompt budget (70k) minus context overhead (~55k)
```

Estimate per-file tokens from diff line count (roughly 4 chars/token, ~80 chars/line → ~20 tokens/line).

If a single file exceeds `MAX_CHUNK_TOKENS`, it gets its own chunk. The explorer will use progressive truncation internally.

If a cluster exceeds the budget, split at the weakest edge (lowest weight connection between sub-clusters).

#### Step 4: Risk Tier Assignment

Assign each chunk a risk tier based on the highest-risk file it contains:

| Tier | Criteria | Source |
|------|----------|--------|
| **Critical** | Contains files with prescan P-SEC signals, OR auth/payment/security path patterns, OR complexity rating D or worse | prescan.py + code_intel complexity + path heuristics |
| **Standard** | Contains code files with no critical signals | Default for .py/.go/.ts/.rs files |
| **Low-risk** | Contains only config, docs, tests-only, generated files | Extension-based triage |

#### Step 5: Generate Cross-Chunk Interface Summary

For each chunk, compute its "API surface" — the functions, types, and data structures that cross chunk boundaries:

```python
@dataclass
class CrossChunkInterface:
    chunk_id: int
    exports_to_other_chunks: list[dict]   # [{name, file, line, consumed_by_chunks: [int]}]
    imports_from_other_chunks: list[dict]  # [{name, file, line, provided_by_chunk: int}]
    shared_state: list[dict]              # [{name, file, type: "db_table|cache_key|config|global_var"}]
```

**How it's built:**
1. From the code_intel graph, identify all caller-callee edges that cross chunk boundaries
2. For each cross-boundary edge: record the function name, file, line, and which chunks are on each side
3. From import analysis: record cross-chunk imports (module in chunk A imported by file in chunk B)
4. From prescan: flag shared state (files that read/write the same DB tables, cache keys, or config files)

**Format in the explorer/judge prompt:**

```markdown
## Cross-Chunk Interfaces (this chunk's external dependencies)

### This chunk exports (called by other chunks):
- `parse_explorer_output()` at orchestrate.py:2356 → called from chunk 3 (test files)
- `PromptContext` class at orchestrate.py:164 → used by chunk 2 (context assembly)

### This chunk imports (calls into other chunks):
- `code_intel.py:graph()` from chunk 2 → called at orchestrate.py:2100

### Shared state:
- `session_dir/launch.json` — written by this chunk, read by chunk 3
```

Token budget for the interface summary: ~500-1000 tokens per chunk. Larger interfaces indicate tighter coupling (may want to merge those chunks).

### Phase 2: Explorer Dispatch by Risk Tier

Replace the current "all experts × all chunks" dispatch with tiered waves:

```
Wave 1: ALL experts × Critical chunks
  (Highest-risk code gets full expert panel)

Wave 2: Core experts × Standard chunks
  (Business logic gets correctness, security, reliability, test-adequacy)

Wave 3: Activated experts × Standard chunks
  (Extended passes where relevant: error-handling, api-contract, concurrency)

Wave 4: Core experts × Low-risk chunks
  (Config/docs get minimal review)

Global: Spec verification with full diff (runs once, not per-chunk)
```

**Cost comparison (5-chunk example: 1 critical + 3 standard + 1 low-risk):**

| Strategy | Explorer calls | Calculation |
|----------|---------------|-------------|
| All × All | 50 | 10 experts × 5 chunks |
| Risk-tiered | 35 | (10×1) + (4×3) + (4×1) + (3×3 activated) = 35 |
| **Reduction** | **30%** | 15 fewer calls |

Actual reduction depends on how many extended experts activate per chunk. Range: 25-45%.

#### Per-Chunk Context Assembly

Each chunk explorer receives:

1. **Chunk diff** (existing: `_chunk_diff()` filters to chunk files)
2. **Chunk-scoped callers/callees** (NEW: filter code_intel graph to functions in chunk files + their direct callers from outside the chunk)
3. **Chunk-scoped prescan signals** (NEW: filter prescan output to chunk files only)
4. **Cross-chunk interface summary** (NEW: list of functions/types in this chunk that are called from OTHER chunks, and functions in other chunks that THIS chunk calls)
5. **Changeset manifest** (existing concept, needs implementation: list all chunks with their files and risk tiers)

The cross-chunk interface summary is critical — it tells each explorer what the "API surface" is between this chunk and others. Findings about these interfaces get CROSS-CHUNK tagged automatically.

### Phase 3: Hierarchical Judgment

Replace the current single-judge model with a two-tier hierarchy:

```
┌─────────────────────────────────────────┐
│  Per-Chunk Explorers (parallel per wave) │
│  correctness, security, reliability, ... │
└────────┬──────────┬──────────┬──────────┘
         │          │          │
    ┌────▼────┐┌────▼────┐┌───▼─────┐
    │ Chunk 1 ││ Chunk 2 ││ Chunk 3 │  Per-Chunk Judges
    │  Judge  ││  Judge  ││  Judge  │  (adversarial validation)
    └────┬────┘└────┬────┘└────┬────┘
         │          │          │
    ┌────▼──────────▼──────────▼────┐
    │        Final Judge             │  Cross-Chunk Synthesis
    │  (dedup + interface analysis)  │
    └───────────────────────────────┘
```

#### Per-Chunk Judge

Each chunk gets its own adversarial judge using the same 4-expert panel (Gatekeeper → Verifier → Calibrator → Synthesizer). The key difference: **the per-chunk judge has focused context** — only the chunk's diff, chunk's explorer findings, and the cross-chunk interface summary.

Benefits:
- Each judge reviews 5-15 findings instead of 50-100
- Context window is never stressed
- Adversarial verification (Read/Grep) is focused on chunk files
- Per-chunk verdict captures the quality signal at chunk granularity

Output: Per-chunk judge produces `chunk-{id}-judge.json` with the same structure as a standard review: verdict, strengths, findings, spec_requirements (if global spec pass produced them).

#### Final Judge (Cross-Chunk Synthesis)

The final judge is a **lightweight synthesis agent** — NOT a full adversarial review. It receives:

1. All per-chunk judge outputs (verdicts + surviving findings)
2. The changeset manifest (which files are in which chunks)
3. The cross-chunk interface summary
4. Any CROSS-CHUNK tagged findings from explorers
5. The spec verification output (global, not per-chunk)

Its job:

1. **Deduplicate** — same finding from overlapping file coverage across chunks
2. **Cross-chunk root cause grouping** — findings in different chunks that share a root cause (e.g., type change in chunk 1 causes consumer bug in chunk 3)
3. **Interface analysis** — verify that data contracts between chunks are consistent (producer/consumer field names, types, shapes)
4. **Accept per-chunk verdicts** — the final judge does NOT re-review findings. If a per-chunk judge accepted a finding, the final judge keeps it unless it's a duplicate or subsumed by a cross-chunk root cause.
5. **Synthesize global verdict** — FAIL if any chunk has must_fix findings; WARN if any has should_fix; PASS otherwise.
6. **Merge spec compliance** — combine per-chunk spec_requirements and the global spec verification output.

The final judge is fast (~30 seconds) because it's not doing investigation — it's doing synthesis over pre-validated findings.

### Phase 4: Cross-Chunk Synthesizer (Before Final Judge)

A dedicated agent that runs AFTER all per-chunk judges but BEFORE the final judge. It receives:

1. The cross-chunk interface summary (generated during chunk building)
2. All per-chunk judge outputs
3. Read/Grep/Glob tool access to the full codebase

Its focused investigation areas:

1. **Data contract mismatches** — chunk A's producer writes field `summary_snippet`, chunk B's consumer reads `summary`. These won't appear in per-chunk reviews because each chunk sees only its side.
2. **Type changes** — chunk A changes a return type from `list` to `dict`. Chunk B's code assumes `list`. Per-chunk explorers may flag chunk A's change as valid but miss the downstream break.
3. **State consistency** — chunk A modifies shared state (database schema, cache keys, config format). Chunk B reads the old format.
4. **Import graph breaks** — chunk A renames/moves a function. Chunk B imports the old name. (This is partially caught by deterministic tools but semantic renaming isn't.)

Output: Cross-chunk findings with `cross_chunk: true` flag. Fed to the final judge alongside per-chunk outputs.

### Phase 5: Pipeline Integration

How existing features work in chunked mode:

| Feature | Chunked Behavior | Notes |
|---------|-----------------|-------|
| **F5 Certification** | Per-chunk per-explorer | Explorer certifies clean for its chunk's files only. Certification validation in per-chunk post-explorers checks `files_checked` against `chunk.files` (not global `changed_files`). The chunk's file list is available in the launch packet's `chunks[].files` array. |
| **F6 Completeness Gate** | Global (spec verification runs once) | Gate evaluates full spec, not per-chunk |
| **F7 Summary Table** | Per-chunk + global | Per-chunk judges get chunk summary; final judge gets all-chunk summary |
| **F8 Pre-existing** | Per-chunk | Git blame is per-file, works naturally per chunk |
| **F9 Provenance** | Global (all chunks same provenance) | Injected into all explorer prompts equally |
| **F10 Phantom Knowledge** | Per-chunk | Self-check scoped to chunk files |
| **F11 Mental Execution** | Per-chunk | Execution contexts scoped to chunk functions |
| **Deterministic scans** | Global run, output filtered per chunk | Semgrep/trivy run once on full diff. In per-chunk post-explorers, scan findings are filtered by matching `finding.file` against `chunk.files`. Findings for files not in the chunk are excluded from the chunk's explorer context. |

### Output Schema Changes

The `finalize.json` output includes chunk metadata when `review_mode == "chunked"`:

```json
{
  "review_mode": "chunked",
  "chunk_count": 4,
  "chunks": [
    {
      "id": 1,
      "description": "src/auth — authentication module",
      "files": ["src/auth/login.py", "src/auth/session.py", "tests/test_login.py"],
      "file_count": 3,
      "diff_lines": 450,
      "risk_tier": "critical",
      "verdict": "WARN",
      "finding_count": 3
    }
  ],
  "cross_chunk_findings": 1,
  "verdict": "WARN",
  "findings": [...]
}
```

Cross-chunk findings have an additional field:

```json
{
  "cross_chunk": true,
  "chunks_involved": [1, 3],
  "summary": "Type mismatch: chunk 1 returns dict, chunk 3 iterates as list"
}
```

`findings-schema.json` additions:
- `cross_chunk`: boolean (optional, default false)
- `chunks_involved`: array of integers (optional, only when cross_chunk=true)

These are additive, backward-compatible changes. Standard mode output is unchanged.

### Phase 6: SKILL.md Integration

Remove the "Chunked review mode is not yet available" message. Add Step 4-L:

```
### Step 4-L: Chunked Review (when mode = "chunked")

4-L.1: Read the launch packet's chunks[] array and wave plan.

4-L.2: For each wave, launch all tasks in parallel (same as standard mode
       but tasks are chunk-scoped). Write results to explorer-chunk{id}-{pass}.json.

4-L.3: After all explorer waves complete, run post-explorers per chunk:
       python3 $SKILL_DIR/scripts/orchestrate.py post-explorers --session-dir $SESSION_DIR --chunk {id}

       The `--chunk` flag filters processing to tasks matching the `chunk{id}-`
       name prefix. Produces `chunk-{id}-judge-input.json` containing:
       - Only findings from this chunk's explorers
       - Certification data scoped to this chunk's files
       - Cross-chunk interface summary for this chunk
       - Chunk-scoped deterministic scan results (filtered by file path)

       Per-chunk post-explorers runs in parallel for all chunks.

4-L.4: Launch per-chunk judges in parallel (one per chunk).
       Each judge receives chunk-scoped findings + cross-chunk interface summary.
       Write results to chunk-{id}-judge.json.

4-L.5: Launch cross-chunk synthesizer.
       Receives all chunk judge outputs + interface summary.
       Write results to cross-chunk-synthesis.json.

4-L.6: Launch final judge (lightweight synthesis).
       Receives all chunk verdicts + cross-chunk findings + spec verification.
       Write results to judge.json.

4-L.7: Proceed to Step 5 (finalize) as normal.
```

---

## Quick Win: Truncation Priority Rework (Pre-Chunking)

Even before chunked mode ships, the truncation cascade should be reworked. The current cascade (`check_token_budget`, lines 1224-1259 of orchestrate.py) treats all context as equally expendable and truncates diff content before exhausting lower-value context. But not all context is equal — some is as critical as the diff itself for making correct judgments.

### Context Value Tiers

| Tier | Context | Value for Judgment | Reasoning |
|------|---------|-------------------|-----------|
| **Tier 1: Judgment-critical** | Diff content | Essential | The review target. Without it, no review. |
| | Callers/callees | Essential | Can't judge a function change without knowing who calls it and what it calls. This IS the diff's meaning. |
| | Cross-file planner output | Essential | "This config change affects a handler 3 files away" — without this, explorer misses cross-file impact entirely. |
| | Prescan signals | High | "Swallowed error at line 42" — directs explorer attention to where bugs likely are. Directly influences what the explorer investigates. |
| | Spec content | High (when present) | Can't judge spec compliance without the spec. Zero bytes when no `--spec`. |
| **Tier 2: Helpful but compressible** | Complexity hotspots | Medium | Useful for prioritization but explorer can see complexity by reading the code. Summary (C+ functions only) is sufficient. |
| | Domain checklists | Medium | Helpful reminders but the explorer's own pass-specific prompt covers the important patterns. |
| | Path instructions | Medium | Repo-specific directives — can be important ("never modify this file without migration") but often empty or low-signal. |
| | Scan results | Low | Already reported by deterministic tools. Explorer is told not to restate them. Main value is avoiding duplicates — a count summary achieves this. |
| | Git risk scores | Low | "This file had 5 bugs last quarter" — interesting for prioritization but doesn't help judge THIS change's correctness. Tier labels are sufficient. |
| **Tier 3: Expendable** | Language standards | Low | Generic style rules. Explorer's own pass prompt covers the important ones. |
| | Graph/function summaries | Low | Redundant with callers/callees (which are Tier 1). The summary adds breadth but callers add depth. |
| | Review.md directives | Low | Advisory repo preferences. Losing these degrades polish, not correctness. |

### Revised Truncation Cascade

```
Phase 1 — Drop Tier 3 (expendable, no judgment loss):
  1. Drop language_standards
  2. Drop graph_summary
  3. Drop functions_summary
  4. Truncate review_md directives (keep always-check items only)

Phase 2 — Compress Tier 2 (summaries preserve value):
  5. Summarize scan results (counts per tool, not full findings)
  6. Summarize git risk (tier labels only, drop file-level metrics)
  7. Summarize complexity (C+ rated functions only, drop A/B)
  8. Drop least-relevant domain checklist
  9. Truncate path instructions (keep first 3 rules only)

Phase 3 — Compress Tier 1 supporting context (last resort before diff):
  10. Truncate cross-file context (top 3 high-risk queries only)
  11. Truncate prescan (critical + high severity only)
  12. Truncate spec (to 5k chars)
  13. Summarize callers (top 5 callers per function, drop callees)

Phase 4 — Touch the diff (absolute last resort):
  14. Truncate diff to changed hunks only (drop surrounding context lines)
  15. If STILL over budget → PromptBudgetExceeded (trigger chunked mode)
```

**Key changes from current cascade:**
- Diff is touched only in Phase 4 (currently step 12 of 12, but prescan and cross-file context are dropped before scan results are summarized — wrong priority)
- Callers/callees are Tier 1 (currently dropped at step 4-5, before scan summaries — too early)
- Prescan is Tier 1 (currently dropped at step 10 alongside cross-file context — should survive longer)
- New step 15: if budget is STILL exceeded after all truncation, raise `PromptBudgetExceeded` which should trigger chunked mode as a fallback rather than producing a skeleton review

### Interaction with Chunked Mode

When chunked mode is active, each chunk's explorer gets a budget-sized slice of the diff (Phase 1 of this spec ensures chunks fit). The truncation cascade still applies within each chunk for the supporting context, but the diff should never need truncation because chunk sizing respects the budget.

The truncation cascade becomes the **fallback for edge cases** (single huge file, unexpectedly large context) rather than the primary mechanism for fitting large diffs.

#### Fallback Control Flow

When `PromptBudgetExceeded` is raised during `prepare()`:

```python
def prepare(args):
    # ... normal prepare flow ...
    try:
        check_token_budget(context, budget=config["token_budget"]["explorer_prompt"])
    except PromptBudgetExceeded:
        if args.no_chunk:
            # User explicitly said no chunking — re-raise as fatal
            raise
        if review_mode == "chunked":
            # Already in chunked mode — single file too large, re-raise
            raise
        # Fallback: restart prepare in chunked mode
        progress("budget_exceeded_fallback", message="Diff too large for standard mode, switching to chunked")
        args.force_chunk = True
        return prepare(args)  # recursive call with force_chunk=True
```

This means `prepare()` may be called twice — once in standard mode (fails budget), once in chunked mode (succeeds because chunks are smaller). The recursive call is bounded: chunked mode can't trigger another fallback because the guard checks `review_mode == "chunked"`.

This is a standalone fix (reorder + add PromptBudgetExceeded fallback) that immediately improves standard mode quality on borderline-large diffs. Not gated on chunked mode.

---

## Implementation Plan

### Wave -1: Truncation Priority Fix (standalone, immediate)
- Reorder check_token_budget() steps to preserve diff content longer
- Move diff truncation to absolute last position
- Unit test: verify diff survives when context is large but diff is small

### Wave 0: Smart Chunking (orchestrate.py)
- Replace `build_chunks()` with graph-based clustering
- Add risk tier assignment from prescan + complexity
- Add token budget balancing
- Add cross-chunk interface summary generation
- Unit tests for clustering algorithm

### Wave 1: Per-Chunk Judge Support (orchestrate.py + prompts)
- Add `--chunk` flag to `post-explorers` subcommand
- Create per-chunk judge prompt assembly
- Add chunk-scoped context (callers, prescan, interface summary)
- Per-chunk judge dispatch and output collection

### Wave 2: Cross-Chunk Synthesizer (new prompt + orchestrate.py)
- Create `reviewer-cross-chunk-synthesizer.md` prompt
- Wire into post-chunk-judges phase
- Implement CROSS-CHUNK tag processing

### Wave 3: Final Judge + SKILL.md (prompts + SKILL.md)
- Create lightweight final judge prompt (synthesis, not re-investigation)
- Add Step 4-L to SKILL.md
- Remove "not yet available" gate
- Integration tests

### Wave 4: Risk-Tiered Wave Ordering
- Implement wave generation from risk tiers
- Adaptive pass selection per chunk
- Update wave dispatch logic

---

## Edge Cases

| Case | Handling |
|------|----------|
| Single file exceeds token budget | Gets its own chunk; explorer uses progressive truncation |
| All files in one directory | Graph clustering still works (call graph > directory) |
| No code_intel graph available | Fallback to directory-based clustering (current behavior) |
| Test file with no corresponding source in diff | Orphan collection groups with related tests or by directory |
| Cross-chunk finding duplicates a per-chunk finding | Final judge deduplicates by file+line+pass+summary |
| Per-chunk judge has zero findings | Certification from explorers is preserved; final judge notes it |
| Only 1 chunk created (small chunked review) | Per-chunk judge IS the final judge; skip synthesis step |
| Spec verification crosses chunk boundaries | Runs globally with full diff; findings distributed to relevant chunks by file |
| User runs with --no-chunk on large diff | Standard mode with budget warnings; user accepts risk |
| Binary files in diff (images, compiled assets) | Excluded from chunks. Binary files have no explorable code. Noted in changeset manifest as "binary, excluded." |
| Deleted files (removed, not modified) | Included in chunks for caller-impact analysis. Explorer checks if anything still imports/calls the deleted code. |
| Renamed files (git rename detection) | Treated as modify of the new path. Git's rename detection (`-M`) links old→new. Included in chunk of the new path. |
| Very small chunks (1-2 files) | Merged into the nearest related chunk if combined token count fits budget. If no related chunk, kept as a standalone micro-chunk with reduced judge (inline validation instead of full 4-expert panel). |

## Error Handling

| Failure | Behavior | Rationale |
|---------|----------|-----------|
| Code_intel graph unavailable | Fall back to directory-based clustering | Degraded but functional. Log warning. |
| One per-chunk judge fails | Continue with remaining chunks. Mark failed chunk as "review incomplete" in final output. | Partial review better than no review. |
| Cross-chunk synthesizer fails | Skip cross-chunk analysis. Final judge operates on per-chunk outputs only. | Cross-chunk findings are bonus, not essential. |
| Final judge fails | Return per-chunk results without global synthesis. Each chunk's verdict stands independently. | Per-chunk reviews are self-contained. |
| Chunk building produces 0 chunks | Error: no changed files to review (same as standard mode empty diff). | Should not happen if diff extraction succeeded. |
| Chunk building produces 1 chunk | Skip per-chunk/final judge split. Run single judge (same as standard mode). | No benefit from hierarchical judgment with 1 chunk. |
| Explorer fails for one chunk | Retry once (core), skip (extended). Same as standard mode failure handling. | Consistent with existing retry policy. |
| Stagger-delayed chunk hits rate limit | Exponential backoff on the specific chunk. Other chunks proceed. | Rate limits are per-request, not global. |

## Configuration

New `.codereview.yaml` keys under a `chunked:` section:

```yaml
chunked:
  # Token budget per chunk (default: 15000, ~60k chars of diff)
  max_chunk_tokens: 15000

  # Model for per-chunk judges (default: sonnet)
  chunk_judge_model: sonnet

  # Model for final judge (default: from judge_model config)
  final_judge_model: null  # null = use judge_model

  # Stagger delay between parallel chunk launches (seconds)
  stagger_delay: 2

  # Maximum parallel chunk judges (respects API rate limits)
  max_parallel_judges: 8
```

All keys are optional. Defaults are used when absent. Existing `output_batching` config (if any) is ignored — chunked mode replaces it.

## Observability & Progress

Chunked mode emits progress events so the user (and SKILL.md agent) can track status:

```python
# During prepare
progress("chunked_mode_activated", chunks=5, total_files=35, total_lines=8500)
progress("chunk_built", chunk_id=1, files=8, tokens=12000, risk_tier="critical")

# During explorer dispatch
progress("chunk_wave_started", wave=1, chunks=[1], experts=10)
progress("chunk_explorer_complete", chunk_id=1, expert="correctness", findings=3)

# During judgment
progress("chunk_judge_started", chunk_id=1, model="sonnet")
progress("chunk_judge_complete", chunk_id=1, verdict="WARN", findings=2)
progress("cross_chunk_synthesis_started")
progress("cross_chunk_synthesis_complete", findings=1)
progress("final_judge_started")
progress("final_judge_complete", verdict="WARN")
```

The SKILL.md agent relays these to the user:
```
[AI] Chunked review: 5 chunks (1 critical, 3 standard, 1 low-risk)
[AI] Wave 1: reviewing critical chunk (8 files)...
[AI] Wave 2: reviewing 3 standard chunks (19 files)...
[AI] Per-chunk judges: 3/5 complete...
[AI] Cross-chunk synthesis: 1 interface finding
[AI] Final verdict: WARN (2 should-fix across chunks 1 and 3)
```

## Open Questions

1. **Per-chunk judge model** — Use Sonnet for per-chunk judges. The per-chunk judge has focused context (5-15 findings, one chunk's diff) which doesn't require Opus-level reasoning. The final judge uses the default model (Opus if configured) for cross-chunk synthesis. Configurable via `.codereview.yaml` `chunked.chunk_judge_model` and `chunked.final_judge_model`.

2. **Cross-chunk synthesizer scope** — should it re-read code (expensive but thorough) or only analyze the interface summary + per-chunk findings (fast but may miss hidden interactions)?

3. **Chunk stability** — if the user makes small edits and re-reviews, do chunks stay stable or reshuffle? Stable chunks allow incremental review; reshuffling may be more accurate.

4. **Parallel budget** — Same as existing wave batching: max 8-12 parallel LLM calls per wave. Per-chunk judges launch with 2-second stagger to smooth API rate limit impact. For 5 chunks, all judges start within 8 seconds and finish within the same ~90s window.

5. ~~**Activation threshold tuning**~~ — Resolved: no external users yet, so backward compatibility is not a concern. Threshold tuning (lowering from 80/8000 to improve quality on medium diffs) is a future optimization to explore once chunked mode is validated.

---

## Testing Strategy

Testing chunked review is harder than testing standard mode because the output (review findings) is non-deterministic and the quality bar is "finds the same bugs." We need tests at four layers, and the quality layer must be built into the development workflow so chunked mode doesn't silently regress when we add new features.

### Layer 1: Chunk Algorithm (unit tests, deterministic, fast)

Test the clustering algorithm with synthetic file graphs. No LLM needed.

```python
class TestBuildChunks:
    def test_import_pair_same_chunk(self):
        """Files that import each other land in same chunk."""
        graph = {"imports": {"a.py": ["b"], "b.py": ["a"]}}
        chunks = build_semantic_chunks(files=["a.py", "b.py", "c.py"], graph=graph)
        a_chunk = find_chunk_for("a.py", chunks)
        b_chunk = find_chunk_for("b.py", chunks)
        assert a_chunk == b_chunk

    def test_test_source_pairing(self):
        """test_foo.py always in same chunk as foo.py."""
        chunks = build_semantic_chunks(files=["foo.py", "test_foo.py", "bar.py"])
        assert find_chunk_for("foo.py", chunks) == find_chunk_for("test_foo.py", chunks)

    def test_token_budget_respected(self):
        """No chunk exceeds MAX_CHUNK_TOKENS."""
        files = [f"file_{i}.py" for i in range(50)]
        chunks = build_semantic_chunks(files=files, file_tokens={f: 5000 for f in files})
        for chunk in chunks:
            assert chunk.estimated_tokens <= MAX_CHUNK_TOKENS

    def test_single_huge_file_own_chunk(self):
        """File exceeding budget gets its own chunk."""
        chunks = build_semantic_chunks(
            files=["huge.py", "small.py"],
            file_tokens={"huge.py": 20000, "small.py": 1000}
        )
        huge_chunk = find_chunk_for("huge.py", chunks)
        assert len(huge_chunk.files) == 1

    def test_completeness(self):
        """Every file appears in exactly one chunk."""
        files = ["a.py", "b.py", "c.py", "d.py"]
        chunks = build_semantic_chunks(files=files)
        all_chunk_files = [f for c in chunks for f in c.files]
        assert sorted(all_chunk_files) == sorted(files)

    def test_stability_on_small_change(self):
        """Adding one file doesn't reshuffle all chunks."""
        files_v1 = ["a.py", "b.py", "c.py"]
        files_v2 = ["a.py", "b.py", "c.py", "d.py"]
        chunks_v1 = build_semantic_chunks(files=files_v1)
        chunks_v2 = build_semantic_chunks(files=files_v2)
        # a.py, b.py, c.py should have the same chunk assignments
        for f in files_v1:
            assert find_chunk_for(f, chunks_v1).files_set & find_chunk_for(f, chunks_v2).files_set

    def test_fallback_without_graph(self):
        """When no code_intel graph, falls back to directory clustering."""
        chunks = build_semantic_chunks(files=["src/a.py", "src/b.py", "lib/c.py"], graph=None)
        assert len(chunks) >= 1  # doesn't crash
```

**When to run:** Every PR, in `just test-unit`. Fast (<1s), no external dependencies.

### Layer 2: Truncation Cascade (unit tests, deterministic, fast)

Test that the reworked cascade drops context in the right order.

```python
class TestTruncationCascade:
    def test_tier3_drops_before_tier2(self):
        """Language standards drop before callers are touched."""
        ctx = make_overbudget_context(language_standards="big", callers="big")
        check_token_budget(ctx, budget=50_000)
        assert ctx.language_standards == ""
        assert ctx.callers != ""  # callers survive

    def test_tier1_survives_longest(self):
        """Callers, prescan, and diff survive when tier 2+3 are dropped."""
        ctx = make_overbudget_context(everything="big")
        check_token_budget(ctx, budget=40_000)
        assert ctx.diff != ""
        assert ctx.callers != ""
        assert ctx.prescan_signals != ""

    def test_diff_never_empty_unless_exception(self):
        """Diff is truncated only after ALL other truncation steps."""
        ctx = make_overbudget_context(diff="huge", everything_else="minimal")
        check_token_budget(ctx, budget=30_000)
        assert ctx.diff != ""  # truncated but not empty

    def test_budget_exceeded_raises(self):
        """When nothing fits, raises PromptBudgetExceeded."""
        ctx = make_overbudget_context(diff="impossibly_huge")
        with pytest.raises(PromptBudgetExceeded):
            check_token_budget(ctx, budget=1_000)
```

**When to run:** Every PR, in `just test-unit`.

### Layer 3: Pipeline Integration (integration tests, may use mocked LLM)

Test that the full chunked pipeline produces a valid review artifact — chunks built, explorers dispatched, per-chunk judges invoked, final judge produces verdict.

```python
class TestChunkedPipelineIntegration:
    def test_chunked_mode_produces_valid_finalize_json(self):
        """Full chunked pipeline: prepare → dispatch → post-explorers → finalize."""
        # Use a large fixture diff (30+ files)
        # Mock explorer/judge outputs with realistic fixture data
        # Assert finalize.json has: verdict, findings, chunk_count, chunks[]

    def test_per_chunk_judges_invoked(self):
        """Each chunk gets its own judge output file."""
        # Assert chunk-{id}-judge.json exists for each chunk

    def test_cross_chunk_findings_in_final_output(self):
        """Cross-chunk synthesizer findings appear in final report."""
        # Use fixture with known cross-chunk interface mismatch
        # Assert a cross_chunk finding exists in final output

    def test_standard_and_chunked_produce_same_schema(self):
        """Output schema identical regardless of mode."""
        # Run same diff through standard mode (small) and chunked mode (forced)
        # Assert both finalize.json files validate against same schema
```

**When to run:** `just test-integration`. Slower (~30s), uses fixture data not real LLM calls.

### Layer 4: Review Quality (eval benchmarks, expensive, periodic)

This is the critical layer — does chunked mode actually find bugs? Three approaches:

#### 4a. Known-Bug Evaluation Using Martian Benchmark

The Martian benchmark provides 45 real PRs with diffs and ground-truth bug labels. Several of these PRs are large enough to trigger chunked mode (30+ files). Rather than creating synthetic fixtures from scratch, we select suitable PRs from the existing benchmark:

**Selection criteria:**
- PR has 20+ changed files (triggers chunked mode when threshold is lowered, or approaches standard threshold)
- PR has at least 2 ground-truth bugs in different files (testable cross-chunk detection)
- PR covers multiple directories (exercises semantic clustering vs directory clustering)

**Process:**
1. Filter Martian PRs by selection criteria → expect 5-10 candidates
2. Run each through both standard and chunked mode
3. Score recall against Martian ground truth labels
4. Compare: does chunked mode match or exceed standard mode recall?

This turns fixture creation into a selection task (~1 hour) instead of a construction task (~days).

For cross-chunk specific bugs (field renames, type changes), we still need 2-3 synthetic fixtures since the Martian benchmark doesn't label cross-file interface bugs specifically. These are small targeted fixtures, not full PR diffs.

**When to run:** `just eval-chunked`. Expensive (real LLM calls), run weekly or before releases.

#### 4b. A/B Mode Comparison

For medium-sized diffs (20-40 files, naturally at the chunked threshold), run BOTH standard and chunked mode and compare:

```python
def eval_mode_comparison(diff_fixture):
    standard_findings = run_review(diff_fixture, mode="standard")
    chunked_findings = run_review(diff_fixture, mode="chunked")

    # Chunked should match or exceed standard mode
    standard_bugs = {(f["file"], f["line"], f["pass"]) for f in standard_findings}
    chunked_bugs = {(f["file"], f["line"], f["pass"]) for f in chunked_findings}

    missed = standard_bugs - chunked_bugs  # regression: standard found, chunked missed
    new = chunked_bugs - standard_bugs     # improvement: chunked found, standard missed

    # Chunked should match or exceed standard mode
    # Standard mode also misses bugs on large diffs (proven in 1.3M token review)
    # so "missed by chunked but found by standard" is informational, not a hard failure
    if missed:
        print(f"INFO: Chunked missed {len(missed)} findings standard caught: {missed}")
    if new:
        print(f"IMPROVEMENT: Chunked found {len(new)} findings standard missed: {new}")

    # Hard failure only if chunked mode is significantly worse
    regression_rate = len(missed) / max(len(standard_bugs), 1)
    assert regression_rate < 0.15, f"Chunked mode regression: missed {regression_rate:.0%} of standard findings"
```

This is a regression test: **chunked mode should match or exceed standard mode recall**. On large diffs, standard mode itself misses bugs due to truncation (proven in the 1.3M token review), so the comparison is informational rather than a strict superset check. A hard failure triggers only if chunked mode misses more than 15% of what standard mode found. If chunked mode finds more (because chunks give each explorer full diff visibility instead of truncated), that's the improvement we're building toward.

**When to run:** Before any release that modifies chunking logic. Expensive.

#### 4c. OWASP/Martian Benchmark Extension

Extend the existing OWASP and Martian eval runners to support chunked mode:

```bash
python3 scripts/eval-owasp.py review --mode chunked --lang python
python3 scripts/eval-owasp.py score
# Compare Youden Index: chunked vs standard
```

The OWASP benchmark is per-file (single-file vulnerabilities), so it won't test cross-chunk issues. But it's a strong regression baseline — chunked mode should NOT decrease the Youden Index on security findings.

**When to run:** `just eval-owasp-chunked`. Periodic, alongside standard eval runs.

### Continuous Regression Prevention

The biggest risk is that **future features break chunked mode silently** because developers test in standard mode only. Prevention:

#### Rule 1: Chunked mode integration test in CI

Add a chunked-mode integration test to `just test-integration` that runs on every PR. Uses fixture data (no LLM calls), but validates the full pipeline produces a valid output.

#### Rule 2: Dual-mode acceptance criteria

Every new feature (like F4-F11) must include acceptance criteria for chunked mode. The feature ticket template should have:

```
## Chunked Mode Impact
- [ ] Feature works in per-chunk explorer context
- [ ] Feature works in per-chunk judge context
- [ ] Feature works in final judge synthesis
- [ ] Test fixture covers chunked mode path
```

If a feature doesn't affect chunked mode (e.g., a prompt-only change), it checks "N/A" with a rationale.

#### Rule 3: Known-bug fixture as release gate

Before any release, the known-bug fixture suite (Layer 4a) must produce recall >= the previous release's score. If recall drops, the release is blocked until investigated.

```bash
just eval-chunked
# Output: recall=0.85 (previous: 0.82) → PASS
# Output: recall=0.75 (previous: 0.82) → FAIL: regression detected
```

#### Rule 4: Mode-comparison spot check

Monthly (or before major releases), run the A/B mode comparison (Layer 4b) on 3-5 real-world diffs. This catches subtle quality regressions that fixture-based tests miss because fixtures have limited diversity.

### Testing the Cross-Chunk Synthesizer

The synthesizer is the hardest component to test because its findings depend on relationships BETWEEN chunks. Dedicated fixtures:

```
fixtures/eval-cross-chunk/
  case-001-field-rename/
    # chunk A renames field "summary" to "summary_snippet"
    # chunk B still reads "summary"
    # Expected: cross-chunk finding about field name mismatch

  case-002-type-change/
    # chunk A changes return type from list to dict
    # chunk B iterates with for-loop (assumes list)
    # Expected: cross-chunk finding about type incompatibility

  case-003-config-format/
    # chunk A changes config schema (adds required field)
    # chunk B reads config without the new field
    # Expected: cross-chunk finding about missing field
```

Each fixture is a multi-file diff designed so that **no single chunk contains both sides of the bug**. The cross-chunk synthesizer must discover the mismatch by analyzing the interface between chunks.

---

## Acceptance Criteria

### Functional
1. Chunked mode activates automatically for diffs exceeding threshold
2. Chunks are built using call graph + imports + test pairing, not just directory
3. Each chunk respects token budget (not file count)
4. Critical chunks reviewed with all experts; low-risk chunks get core only
5. Per-chunk judges produce independent verdicts with focused context
6. Cross-chunk synthesizer catches interface mismatches between chunks
7. Final judge deduplicates and produces global verdict
8. All F4-F11 features work correctly in chunked mode
9. SKILL.md Step 4-L guides the agent through chunked dispatch
10. At least 25% reduction in total explorer invocations vs "all × all" (range: 25-45% depending on extended expert activation)

### Quality & Testing
11. Chunk algorithm unit tests cover: import pairing, test pairing, budget compliance, stability, completeness, fallback without graph
12. Truncation cascade unit tests verify tier ordering: Tier 3 drops before Tier 2, Tier 1 survives longest, diff only touched in Phase 4
13. Pipeline integration test in `just test-integration` validates full chunked flow with fixture data
14. Known-bug fixture suite with at least 3 multi-file cases (including 1 cross-chunk bug)
15. A/B mode comparison: chunked mode recall >= standard mode recall on medium-sized diffs
16. OWASP benchmark: chunked mode Youden Index does not decrease vs standard mode
17. Cross-chunk synthesizer fixture suite with at least 3 cases (field rename, type change, config format)

### Regression Prevention
18. Every new feature ticket includes "Chunked Mode Impact" section
19. Known-bug fixture recall is a release gate (must equal or exceed previous release)
20. Chunked pipeline integration test runs on every PR via `just test-integration`
