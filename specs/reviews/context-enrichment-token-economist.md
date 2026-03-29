# Context Enrichment Plan: Token Budget Impact Analysis

Reviewer perspective: cost and context-window optimization specialist.

Reviewed document: `docs/plan-context-enrichment.md`

---

## 1. Inventory of New Context Sources

The plan adds seven new context sources to the explorer prompt, on top of the existing baseline. Here is each source with its estimated token footprint.

### Current baseline (before this plan)

| Component | Source in orchestrate.py | Typical tokens |
|-----------|-------------------------|----------------|
| Global contract | `reviewer-global-contract.md` (5.7KB) | ~1,500 |
| Pass prompt | e.g., `reviewer-correctness-pass.md` (17KB) | ~4,300 |
| Diff | `diff_result.diff_text` (variable) | 5,000-40,000 |
| Changed files list | newline-delimited paths | 100-500 |
| Complexity JSON | `complexity.sh` output | 200-800 |
| Git risk JSON | `git-risk.sh` output | 200-600 |
| Scan results JSON | `run-scans.sh` output | 300-1,500 |
| Callers stub | Static string "Use Grep/Read..." | ~10 |
| Language standards | Loaded from `references/<lang>.md` | 0-1,200 |
| Review instructions | `REVIEW.md` / `.codereview.yaml` custom_instructions | 0-500 |
| Spec | Loaded from `--spec` flag | 0-12,500 (50KB cap) |

**Baseline total (typical mid-size PR, no spec):** ~15,000-50,000 tokens.

### New sources from this plan

| # | Source | Feature | Estimated tokens (typical) | Estimated tokens (worst case) | Notes |
|---|--------|---------|---------------------------|------------------------------|-------|
| N1 | Prescan signals section | F1 | 300-800 | 2,000 | 15 signals with file/line/description. Large-diff caps to critical/high only. |
| N2 | Domain checklists | F2 | 0-500 per checklist | 1,500 (3 checklists x 500) | Each checklist is ~15 items at ~30-35 tokens each. Plan says "~1.5k tokens" when all three fire. |
| N3 | Cross-file planner results | F12 | 1,000-3,000 | 5,000 | Up to 10 queries x 5 results. Plan caps at 5k tokens. Includes rationale text per result. |
| N4 | REVIEW.md directives | F13 | 0-400 | 900 | 30 items cap per section (Always check + Style). Skip section does not enter prompt. |
| N5 | Path-based instructions | F15 | 0-200 | 600 | 1-3 sentences per matching path pattern x number of matching patterns. |
| N6a | Complexity JSON (upgraded) | F0c | 200-800 | 1,200 | Replaces current complexity.sh output. Richer schema (analyzer field, tool_status). Marginal increase. |
| N6b | Functions JSON | F0c | 300-1,000 | 2,500 | New. Per-function definitions with params, return type, line ranges. 20 functions at ~50 tokens each = 1,000. |
| N6c | Graph JSON | F0c | 500-2,000 | 4,000 | New. Nodes + edges + stats. 30 nodes at ~40 tokens each + 40 edges at ~30 tokens each = 2,400. Semantic edges add ~600 more. |
| N7 | Formatted diff (before/after blocks) | F0c format-diff | +30% to +60% over unified diff | +80% over unified diff | Replaces the diff, not additive. See section 4 for detailed analysis. |

**Key observation:** N6b (functions JSON) and N6c (graph JSON) are not explicitly described as being injected into the explorer prompt context. The plan says the graph is used by the cross-file planner (F12) and that the "context packet" includes a graph summary. But the `PromptContext` dataclass in `orchestrate.py` does not currently have fields for these. The plan needs to clarify whether raw JSON or a summarized text form enters the prompt. My estimates above assume summarized text. If raw JSON is injected, multiply by 1.5-2x due to JSON structural overhead.

---

## 2. Worst-Case Scenario: All Features Fire Simultaneously

**Scenario:** SQL project with concurrency (Go + channels + database), REVIEW.md present with all three sections populated, 4 path instruction rules matching, prescan finds 15 signals across 8 categories, graph has 30 nodes and 40 edges with semantic layer, 3 domain checklists match, cross-file planner returns 10 queries with 5 results each, spec file loaded.

### Token budget breakdown (70k budget)

| Component | Tokens |
|-----------|--------|
| Global contract | 1,500 |
| Pass prompt (correctness, largest) | 4,300 |
| **Formatted diff** (assume 5k unified -> ~8k formatted) | **8,000** |
| Changed files list | 400 |
| Complexity JSON | 800 |
| Git risk JSON | 600 |
| Scan results JSON | 1,200 |
| Language standards (Go + Python) | 1,200 |
| **Prescan signals** | **2,000** |
| **Domain checklists** (SQL + concurrency + LLM) | **1,500** |
| **Cross-file planner results** | **5,000** |
| **REVIEW.md directives** | **900** |
| **Path-based instructions** | **600** |
| **Functions JSON summary** | **2,000** |
| **Graph JSON summary** | **3,500** |
| Spec (50KB cap) | 12,500 |
| Callers (now pre-computed) | 800 |
| Review instructions (custom_instructions) | 300 |
| **Total** | **~47,100** |

This fits within the 70k budget with ~23k tokens of headroom. However, note:

1. **The diff is the variable that breaks this.** If the diff is 25k tokens in unified format (a large but not uncommon PR), the formatted version could be 35k-40k tokens. At that point the total hits ~74,000 and exceeds the budget.

2. **The spec is the second-largest variable.** A full 50KB spec consumes 12,500 tokens. With a large diff AND a spec, we are over budget even without any new features.

3. **Multiply by explorer count.** Each explorer gets its own copy of this context. With 5-7 experts active (core 3 + security-dataflow + concurrency + error-handling + reliability, all triggered by the scenario), that is 5-7 copies of 47k tokens in input. At Sonnet pricing ($3/M input), the input cost alone for explorers is: 7 x 47,000 / 1,000,000 x $3 = **$0.99** for explorers alone, up from a baseline of ~$0.35.

### Revised worst case with large diff

| Component | Tokens |
|-----------|--------|
| Fixed context (contract + pass + files + standards + instructions) | 9,000 |
| Formatted diff (large PR, 2000 lines changed) | 35,000 |
| All new context sources (N1-N6c) | 16,300 |
| Spec | 12,500 |
| Existing context (complexity + risk + scans + callers) | 3,400 |
| **Total** | **~76,200** |

This **exceeds the 70k budget by 6,200 tokens.** The truncation cascade kicks in.

---

## 3. Truncation Strategy Analysis

### Current truncation cascade (`check_token_budget`)

The existing `check_token_budget()` function in `orchestrate.py` (line 721) applies a fixed sequence of truncations:

1. `scan_results` -> summarize to counts only
2. `language_standards` -> drop entirely
3. `git_risk` -> summarize to tier counts only
4. `diff` -> truncate to changed hunks only (60 lines max)

If still over budget after all four truncations, it raises `PromptBudgetExceeded`.

### The problem: no unified budget allocation

The plan adds 5+ new context sources but does not update the truncation cascade. This means:

1. **New sources have no truncation path.** If prescan signals, domain checklists, cross-file context, REVIEW.md directives, path instructions, functions JSON, and graph JSON collectively consume 16k tokens, there is no mechanism to shed them when the budget is tight. They are injected as raw strings into `PromptContext` fields, but those fields are not listed in the truncation cascade.

2. **Truncation order does not reflect value.** The current cascade drops language standards (high value for style) before it touches the diff (highest value). The new sources should be ranked by information value and inserted into the cascade appropriately.

3. **Each feature independently claims tokens.** The prescan caps "critical/high only" in large-diff mode. The cross-file planner caps at "5k tokens." But these caps are independent. There is no shared budget negotiation: if the diff is 40k tokens, the 5k cross-file cap still fires, and combined with prescan (2k) and checklists (1.5k), the auxiliary context alone is 8.5k on top of a 40k diff. The budget must accommodate both.

4. **The formatted diff makes truncation harder.** The current `truncate_to_changed_hunks_only()` function operates on unified diff syntax (looks for `@@`, `+`, `-` prefixes). The formatted diff uses `__new hunk__`, `__old hunk__`, and numbered lines. The truncation function will not work on the new format without being updated.

**This is the single most important issue in the plan from a token economics perspective.** Without a unified budget allocator, the system will hit `PromptBudgetExceeded` on large PRs with multiple features enabled, and the failure mode is an exception rather than graceful degradation.

---

## 4. Formatted Diff Impact

### Size comparison: unified vs before/after block format

The plan's `format-diff` transforms unified diffs into separated before/after blocks. Let me quantify the inflation.

**Structural overhead per hunk:**
- New: `## File: <path>` header (one per file, ~10 tokens)
- New: `@@ def function_name (line N)` (one per hunk, ~8 tokens vs ~5 for `@@ -N,M +N,M @@`)
- New: `__new hunk__` label (3 tokens per hunk)
- New: `__old hunk__` label (3 tokens per hunk, when removals exist)
- New: Line numbers on every new-hunk line (~2 tokens per line)

**Content duplication:** Context lines (unchanged lines around the change) appear in BOTH the new hunk and old hunk blocks. In unified diff, they appear once.

**Quantitative estimate for a typical hunk:**

A unified diff hunk with 3 lines context, 2 removals, 3 additions (11 lines total):
- Unified: 11 lines + 1 header = ~12 lines, ~60 tokens
- Formatted: new hunk (3 context + 3 added = 6 lines with numbers) + old hunk (3 context + 2 removed = 5 lines) + 3 labels = ~14 lines + overhead, ~85 tokens

**Inflation factor: ~1.4x for a typical hunk.**

For a pure addition (no removals), the old hunk is omitted, so inflation is only ~1.15x (just line numbers and labels added).

For a refactor with many removals and additions, context lines are duplicated in both blocks, driving inflation to ~1.6x.

With `--expand-context` (expanding to function boundaries), context lines increase significantly. A 5-line change in a 40-line function would include ~35 context lines duplicated in both blocks. This pushes inflation to **1.8-2.0x**.

**Estimate for a 5,000-token unified diff:**

| Scenario | Formatted tokens | Inflation |
|----------|-----------------|-----------|
| Mostly additions (new code) | ~5,750 | 1.15x |
| Typical mixed changes | ~7,000 | 1.40x |
| Heavy refactoring (many edits) | ~8,000 | 1.60x |
| With `--expand-context` | ~9,000-10,000 | 1.80-2.00x |

**Recommendation:** The diff is the largest single context component. A 1.4-1.8x inflation on the largest component has outsized impact on total budget. The plan should either (a) make `--expand-context` opt-in and off by default, or (b) budget the formatted diff with a hard token cap that triggers truncation before other context sources are shed.

---

## 5. Cost Per Review

### Baseline (from eval baseline measurements)

- ~$5/PR, 47 turns, all Sonnet, 28 minutes
- After optimization: 4.2x faster

### New cost components

| Cost driver | Mechanism | Estimated additional cost | Per-review or per-explorer |
|-------------|-----------|--------------------------|---------------------------|
| Cross-file planner LLM call (F12) | New LLM call with diff summary input | $0.05-0.15 | Per-review (once) |
| Larger explorer prompts | More input tokens per explorer | +20-40% input tokens | Per-explorer |
| More explorers activated | Domain checklists may trigger extended experts | 0-2 additional experts | Per-review |
| format-diff processing | Python script, no LLM | Negligible ($0) | Per-review |
| prescan.py processing | Python script, no LLM | Negligible ($0) | Per-review |
| code_intel.py processing | Python script, no LLM | Negligible ($0) | Per-review |
| Semantic indexing (graph --semantic) | Local embedding, no LLM | Negligible ($0) | Per-review |

### Estimated cost impact

**Scenario A: Small PR (3 files, 200 lines), 3 core experts only.**
- Baseline input per explorer: ~15k tokens. New: ~20k tokens (+33%).
- No cross-file planner (too small to justify).
- No domain checklists fire.
- Cost increase: ~$0.05/PR (+1%). Negligible.

**Scenario B: Medium PR (10 files, 800 lines), 5 experts.**
- Baseline input per explorer: ~30k tokens. New: ~40k tokens (+33%).
- Cross-file planner fires: +$0.10.
- 1 domain checklist fires.
- Cost increase: ~$0.50/PR (+10%). Acceptable.

**Scenario C: Large PR with all features (30 files, 3000 lines), 7 experts.**
- Baseline input per explorer: ~50k tokens. New: ~65k tokens (+30%).
- Cross-file planner fires: +$0.15.
- 2-3 domain checklists fire.
- 2 additional experts triggered by domain checklists: +$1.00-1.50 per extra expert.
- Cost increase: ~$2.00-3.00/PR (+40-60%).

**Scenario D: Worst case (all features, large diff, spec loaded).**
- Budget exceeded, truncation fires, some context is lost.
- 7 experts x 70k input tokens = 490k input tokens = ~$1.47 input cost alone.
- Plus output tokens, judge, cross-file planner.
- Estimated total: ~$8-10/PR (+60-100% over baseline).

### Sensitivity: the cross-file planner is cheap

The F12 cross-file planner is a single lightweight LLM call. Its input is a diff summary (not the full diff), maybe 2-5k tokens. Its output is 10 queries in JSON, maybe 1k tokens. At Sonnet pricing, this is $0.01-0.02 per call. The cost impact of F12 is almost entirely from its **results being injected into every explorer prompt** (5k tokens x 7 explorers = 35k additional input tokens = $0.10), not from the planner call itself.

---

## 6. Proposed Token Budget Allocation

Given a 70,000 token budget per explorer, here is a priority-ordered allocation:

| Priority | Component | Budget (tokens) | % of 70k | Truncation strategy when over budget |
|----------|-----------|-----------------|----------|--------------------------------------|
| P0 (fixed) | Global contract + pass prompt | 6,000 | 8.6% | Never truncated. These are the identity of the explorer. |
| P1 (core) | Formatted diff | 35,000 | 50.0% | Truncate to changed hunks only (update truncator for new format). Hard floor: 5,000 tokens. |
| P2 (core) | Changed files + complexity + git risk | 2,500 | 3.6% | Summarize to counts/tiers. |
| P3 (high value) | Cross-file planner results | 5,000 | 7.1% | Drop low-risk queries first. Floor: top 3 high-risk results. |
| P4 (high value) | Prescan signals | 1,500 | 2.1% | Critical/high only. Floor: top 5 signals. |
| P5 (high value) | Domain checklists | 1,500 | 2.1% | Drop least-relevant checklist first (by trigger pattern match count). |
| P6 (medium value) | REVIEW.md directives | 800 | 1.1% | Truncate to "Always check" only, drop "Style." |
| P7 (medium value) | Path instructions | 500 | 0.7% | Drop instructions for low-risk files first. |
| P8 (medium value) | Functions/graph summaries | 3,000 | 4.3% | Summarize to top-10 nodes by relevance. Drop graph entirely if needed. |
| P9 (medium value) | Scan results | 1,500 | 2.1% | Summarize to counts. |
| P10 (variable) | Language standards | 1,200 | 1.7% | Drop entirely (already in truncation cascade). |
| P11 (variable) | Callers | 800 | 1.1% | Summarize to top 5. |
| P12 (variable) | Spec | 10,000 | 14.3% | Truncate to scoped sections. Drop if budget is critically tight. |
| **Reserve** | Headroom for model overhead | ~700 | 1.0% | -- |

**Total allocated: 70,000 tokens.**

### Truncation cascade (updated)

When the assembled prompt exceeds 70k tokens, shed in this order (lowest value first):

1. `language_standards` -> drop entirely (saves ~1,200)
2. `scan_results` -> summarize to counts (saves ~1,000)
3. `git_risk` -> summarize to tiers (saves ~400)
4. `graph_summary` -> drop (saves ~2,000-3,000)
5. `functions_summary` -> drop (saves ~1,000)
6. `path_instructions` -> drop (saves ~500)
7. `review_md_directives` -> "Always check" only (saves ~400)
8. `domain_checklists` -> drop least-relevant (saves ~500 per checklist)
9. `cross_file_context` -> top 3 high-risk only (saves ~2,000-3,000)
10. `prescan_signals` -> critical only (saves ~1,000)
11. `spec` -> truncate to 5k tokens (saves ~5,000-7,000)
12. `diff` -> truncate to changed hunks (saves variable, often 10,000+)

If still over budget after step 12: raise `PromptBudgetExceeded`.

---

## 7. Summary Table: Features vs. Token Impact

| Feature | Tokens added (typical) | Tokens added (worst case) | New LLM calls | Cost impact (typical PR) | Risk level |
|---------|----------------------|--------------------------|----------------|-------------------------|------------|
| F0c: format-diff | +2,000 (diff inflation) | +20,000 (large diff) | 0 | +5-10% | **HIGH** -- inflates the largest component |
| F0c: complexity/functions/graph | 1,000-3,800 | 7,700 | 0 | +3-5% | MEDIUM -- new components with no truncation path |
| F1: Prescan signals | 300-800 | 2,000 | 0 | +1-2% | LOW -- well-capped in the plan |
| F2: Domain checklists | 0-500 | 1,500 | 0 | +1-2% | LOW -- bounded and rarely all three fire |
| F12: Cross-file planner | 1,000-3,000 | 5,000 | 1 | +5-10% | MEDIUM -- 5k cap is good, but per-explorer multiply |
| F13: REVIEW.md | 0-400 | 900 | 0 | <1% | LOW |
| F15: Path instructions | 0-200 | 600 | 0 | <1% | LOW |
| **All combined** | **4,500-10,700** | **37,700** | **1** | **+15-30%** | -- |

---

## 8. Three Specific Recommendations

### Recommendation 1: Implement a unified budget allocator before shipping new context sources

The plan adds 7 new context sources but does not update `check_token_budget()` or the `PromptContext` dataclass. The current truncation cascade is a hardcoded 4-step sequence that does not know about prescan, checklists, cross-file context, REVIEW.md, path instructions, functions, or graph data.

**Concrete action:** Before any feature lands, extend `PromptContext` with the new fields and implement a priority-ordered truncation cascade as described in section 6. Each new field needs a truncation function (summarize, cap, or drop). Test the cascade with a synthetic worst-case prompt that exceeds 70k tokens.

Additionally, update `truncate_to_changed_hunks_only()` to handle the `format-diff` output format (which uses `__new hunk__`/`__old hunk__` markers instead of `@@`/`+`/`-` prefixes).

### Recommendation 2: Make `--expand-context` opt-in and budget-aware

The `format-diff --expand-context` flag expands hunks to function boundaries, potentially doubling the diff size. The plan positions this as a quality improvement, and it is. But it is also the single largest token cost amplifier in the entire plan.

**Concrete action:** Default to `format-diff` without `--expand-context`. Only enable expansion when the formatted diff (without expansion) fits within 50% of the explorer budget (i.e., under 35k tokens). This gives expansion room to double the diff and still leave budget for other context. If the diff is already large, expansion should be automatically suppressed with a progress log message.

### Recommendation 3: Cap cross-file context injection per-explorer, not per-review

The plan says "Total budget: ~5k tokens of cross-file context." But this 5k is injected into every explorer's prompt. With 7 explorers, that is 35k tokens of input billed across all explorers. Most of this context is irrelevant to most explorers -- the correctness explorer needs symmetric counterpart context, but the test-adequacy explorer does not need the full graph.

**Concrete action:** Route cross-file context by relevance to each explorer's domain. The correctness explorer gets full cross-file context (5k). The security explorers get security-relevant cross-file results only (perhaps 2k -- consumers of auth functions, config dependents). The test-adequacy explorer gets test<->implementation results only (perhaps 1k). This reduces total cross-file input from 35k tokens across 7 explorers to ~15k tokens, saving ~$0.06 per review and keeping each explorer's context focused.

---

## Appendix: Token Estimation Methodology

All token estimates use the `chars / 4` heuristic that `PromptContext.estimate_tokens()` uses in the current codebase. This is a rough approximation. For JSON content, the true ratio is closer to `chars / 3.5` due to structural characters (`{`, `}`, `:`, `"`) each consuming a token. For natural language markdown, `chars / 4` is reasonably accurate. All estimates should be validated with a tiktoken or Claude tokenizer once the actual content is assembled.
