# Infrastructure & Developer Experience Review: Context Enrichment Plan

**Reviewer perspective:** Senior infrastructure / developer experience engineer.
**Document reviewed:** `docs/plan-context-enrichment.md` (1785 lines, 7 features).
**Date:** 2026-03-28

---

## 1. `code_intel.py` (F0c) -- Dependency Sprawl and Installation Surface Area

**Verdict: RISKY**

The two-tier model (minimal/full) looks clean on paper but masks the real problem: the "full" tier spans four package managers (pip, npm, go, rustup) with no transactional install guarantee. A user who runs `setup --install --tier full` and hits a npm permission error for ast-grep will end up in an intermediate state where some tools installed and others did not. The plan handles this ("partial install success: proceed with what's available"), but the *user's mental model* is binary -- they said "yes" to the prompt, so they expect everything to work. The next review will silently skip ast-grep pattern matching, and the user won't know they're getting degraded results unless they re-run `setup --check`.

The `--non-interactive` flag for CI is mentioned exactly once, in the edge cases section, as an afterthought. CI is the primary execution environment for code review at scale. The entire setup flow is designed around an interactive agent-driven prompt ("ask the user... if yes... if skip"), which will silently do nothing in headless contexts. The marker file `.agents/codereview/setup-complete` workaround requires users to understand and pre-create it in their CI pipeline -- that's a documentation tax, not graceful degradation.

The fallback chain when python3 is absent ("agent falls back to manual execution") is optimistic. The agent performing "manual" context gathering means issuing ad-hoc Grep calls without structural awareness -- which is exactly the current behavior the plan is trying to replace. That fallback is tolerable for the prescan and complexity analysis, but the `format-diff` subcommand has no agent-side equivalent. If python3 is missing, the entire diff formatting improvement disappears silently. The plan should state explicitly which capabilities are lost when python3 is unavailable, not wave it away with "falls back to manual execution."

---

## 2. `format-diff` Subcommand -- Is This the Right Layer?

**Verdict: RETHINK**

`format-diff` is pure text processing: parse unified diff, rearrange into before/after blocks, emit text. It has zero optional dependencies. It does not use tree-sitter, sqlite-vec, model2vec, or any language grammar for its core operation. The `--expand-context` flag optionally uses tree-sitter, but the base transformation is deterministic string manipulation.

Placing it inside `code_intel.py` means every consumer of diff formatting must depend on the entire code intelligence module -- its import chain, its lazy tree-sitter initialization, its language detection logic. This coupling is unnecessary. If `code_intel.py` fails to import (corrupted venv, missing Python dependency), diff formatting goes down with it, even though it could run standalone.

`format-diff` should be either a standalone script (`scripts/format_diff.py`) or, since it's pure text processing, a bash/awk implementation that has zero Python dependency. The plan's own principle is "if a step requires reading code and reasoning about behavior, it's an AI task; if it's applying a formula or running a tool, it's a script." Diff reformatting is applying a formula. Coupling it to the heaviest script in the pipeline violates that principle.

---

## 3. Prescan (F1) -- Runtime Cost

**Verdict: RISKY**

The plan says "fast static checks" but provides no latency budget. Let me estimate. For a 50-file diff with tree-sitter available:

- Parse 50 files with tree-sitter: ~50ms total (tree-sitter is fast, ~1ms/file for typical source)
- 8 checkers x 50 files = 400 check invocations. Most are tree-sitter queries or regex scans, so ~1-5ms each. Call it ~1-2 seconds total.
- P-DEAD (dead code) scans for references within the same file. P-UNWIRED scans the import graph across the repo. The plan says P-UNWIRED "uses code_intel.py imports and code_intel.py functions data" -- but that data needs to be computed first. If prescan runs after complexity analysis but before context packet assembly, the import graph may not be available yet. If prescan has to build it, that's an additional O(repo-size) scan.

The realistic estimate for 50 files is 2-5 seconds with tree-sitter, which is acceptable. But in regex-only mode (no tree-sitter), P-LEN uses heuristic line counting and P-DEAD uses name-based grep. Grep-based P-DEAD on a large repo could take 10+ seconds for 50 files if each function name triggers a repo-wide search.

The plan caps at 200 files, which is good. But it does not specify a wall-clock timeout. If a single checker hangs (say, regex backtracking on a pathological file), the entire prescan blocks `prepare`. Add a per-file timeout (e.g., 500ms) and a total prescan timeout (e.g., 15 seconds). If the budget is exceeded, emit partial results and move on.

---

## 4. Cross-File Planner (F12) -- LLM Call in the Prepare Phase

**Verdict: RISKY**

Every other step in `prepare` is deterministic: read files, run scripts, parse JSON, assemble context. F12 introduces a non-deterministic LLM call that can timeout, return malformed JSON, hallucinate symbol names, or cost unexpected money. This is a qualitative change to the prepare phase's reliability contract.

**Timeout/failure:** The plan does not specify what happens when the planner LLM call times out or returns invalid JSON. The edge cases section says "planner returns >10 queries: truncate" -- but doesn't cover "planner returns garbage" or "planner takes 30 seconds." Given that `prepare` currently runs in ~5-10 seconds for a normal diff, a 15-30 second LLM roundtrip would double or triple the wall clock time. The plan needs an explicit timeout (e.g., 10 seconds) and a fallback (skip cross-file context, proceed with structural graph only).

**Budget enforcement:** "Cap at 10 queries, 5k tokens" is stated as a plan-level constraint, but there is no mechanism described to enforce it. The planner prompt says "Max 10 queries total," but LLM compliance with output constraints is probabilistic, not guaranteed. The orchestrator must enforce the cap mechanically: parse the JSON, take the first 10, and truncate the token budget by counting tokens on the results, not by trusting the LLM to self-limit.

**Cost:** An extra LLM call per review adds up. If using Haiku/Flash-class models, it's ~$0.001-0.005 per call -- negligible. If using Sonnet-class, it's ~$0.01-0.05. The plan doesn't specify which model tier to use. Given the baseline eval showed $5/PR, even $0.01 extra is noise. But the plan should explicitly state this is a Haiku/Flash-tier call and make the model configurable, so cost doesn't silently escalate if someone routes it to a larger model.

---

## 5. Semantic Search (`graph --semantic`) -- Is This Premature?

**Verdict: RETHINK**

The structural graph (F0c without `--semantic`) already provides: direct callers, importers, co-change frequency, and 1-hop dependency neighborhoods. This covers the majority of cross-file bugs: if you change function X, the graph tells you who calls X, who imports X, and what files historically change alongside X.

The semantic layer adds "find functions that are similar in purpose but have no explicit dependency." The canonical example is `check_auth_token` vs. `validate_session` in different modules. This is a real class of bug, but it's also what F12 (the cross-file planner) exists to catch. F12's "Symmetric/Counterpart Operations" category uses an LLM to reason about paired operations -- which is fundamentally a semantic task. The semantic embedding layer is solving the same problem with a different (heavier) mechanism.

The cost is significant: sqlite-vec + model2vec/onnxruntime add ~50-150MB of dependencies, a model download on first run, an embedding pipeline, a cache database, and incremental indexing logic. That's a lot of machinery for a feature whose value proposition overlaps with F12. The plan acknowledges the overlap ("structural + semantic + planner cover three layers") but doesn't justify why three layers are needed instead of two.

My recommendation: ship the structural graph and the cross-file planner first. Measure how often the planner misses semantic relationships that embeddings would catch. If the miss rate is significant, add the semantic layer as a v2 enhancement. Designing the graph output format to accommodate semantic edges now (so the schema doesn't change later) is fine -- but building and maintaining the embedding pipeline now is premature.

---

## 6. Domain Checklists (F2), REVIEW.md (F13), Path Instructions (F15) -- Token Budget

**Verdict: SOUND**

These three features are lightweight text injection. Let me estimate the token footprint:

- **Domain checklists (F2):** Each checklist is ~15 items, ~500 tokens per checklist. All three match: ~1,500 tokens. The plan correctly notes this is "acceptable."
- **REVIEW.md (F13):** Capped at 30 items per section, 3 sections. At ~20 tokens per item, that's ~1,800 tokens maximum. Typical usage will be much less.
- **Path instructions (F15):** "1-3 sentences per path pattern." At 5 patterns matching, ~500 tokens.

Combined worst case: ~3,800 tokens of additional context. Against a 70,000 token prompt budget, this is ~5.4% -- well within budget.

However, the current truncation waterfall in `check_token_budget` (lines 737-748 of `orchestrate.py`) does not include any of these new context sources. If the prompt is already near budget, adding 3,800 tokens of checklists and directives could push it over before the truncation logic gets a chance to act. The truncation order should be updated to include these new sources as early candidates for trimming -- domain checklists and path instructions are supplementary context and should be shed before the diff or git risk data.

---

## Top 3 Recommendations

1. **Extract `format-diff` from `code_intel.py`.** Make it a standalone script or a zero-dependency Python module. It's the single highest-value, lowest-risk feature in the plan (every review benefits, zero optional dependencies, deterministic). Coupling it to the most complex script in the pipeline is an unforced error. Ship it first and independently.

2. **Add hard timeouts and explicit fallback behavior to F1 (prescan) and F12 (cross-file planner).** Prescan should have a 15-second wall-clock cap with partial result emission. The planner LLM call should have a 10-second timeout with "skip cross-file context" as the fallback. Both should log when they degrade so operators can diagnose slowdowns. Without these, `prepare` loses its predictable latency profile.

3. **Defer the semantic embedding layer (`--semantic`).** Design the graph schema to accommodate semantic edges, but don't build the embedding pipeline, model download, sqlite-vec indexing, or incremental cache until you have data showing the structural graph + LLM planner miss a meaningful class of cross-file relationships. The 50-150MB dependency footprint and the operational complexity of model caching are not justified by the speculative benefit over F12's planner.
