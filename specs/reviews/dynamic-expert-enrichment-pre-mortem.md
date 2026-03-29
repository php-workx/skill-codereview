# Pre-Mortem: Spec B — Dynamic Expert Enrichment & Generation

**Date:** 2026-03-28
**Spec:** `specs/dynamic-expert-enrichment.md`
**Scope mode:** HOLD SCOPE
**Verdict:** WARN — Well-structured spec with strong research backing, but two architectural issues and several implementation gaps need resolution.

## Council Panel

| Judge | Perspective | Findings | Key Issue |
|-------|-------------|----------|-----------|
| Missing Requirements | Edge cases, integration gaps | 15 | SKILL.md generation flow undefined; post_explorers doesn't discover generated expert |
| Feasibility | Code compat, architecture | 9 | `_prompt_path_for_expert()` crashes for generated experts; SKILL.md too fragile for 10-step orchestration |
| Spec Completeness | Types, acceptance criteria | 8 | `ChecklistMeta` undefined; pseudocode contradicts degradation matrix for None frontmatter |
| Scope & Architecture | Right-sizing, complexity | 6 | Per-expert enrichment is ~600 tokens savings for significant complexity; dynamic generation may be premature |

---

## Critical Findings

### F1. SKILL.md cannot reliably orchestrate a 10-step generation flow

**Consensus: 3/4 judges (Missing Reqs, Feasibility, Completeness).** The spec says generation happens in SKILL.md but never specifies the actual SKILL.md instructions. SKILL.md is a markdown prompt to an LLM, not executable code. The generation flow requires: reading coverage_gaps, reading a template, filling placeholders, spawning a sub-agent, validating output (4 checks), handling failures, writing to session dir, constructing a task entry, assembling the context packet, and appending to the wave. That's 10 steps of complex logic in natural-language instructions to an AI.

**Fix:** Move generation into a new CLI subcommand: `orchestrate.py generate-expert --session-dir ... --domain ...`. This script reads launch.json, fills the template, calls the LLM (via subprocess or API), validates output, assembles the full context packet using real `assemble_explorer_prompt()` machinery, writes the prompt file, and updates launch.json. SKILL.md just calls one bash command if `coverage_gaps.generation_eligible` is true. This preserves the "deterministic scripts handle assembly" principle.

### F2. `_prompt_path_for_expert()` crashes for generated experts

**Consensus: 2/4 judges (Missing Reqs, Feasibility).** `assemble_explorer_prompt()` calls `_prompt_path_for_expert(expert_name)` which looks up `EXPERT_PROMPT_FILES[name]`. A generated expert named "helm-charts" is not in this dict and will raise `ValueError`. If SKILL.md bypasses `assemble_explorer_prompt()`, the generated expert misses all standard context (diff, complexity, git risk, scan results).

**Fix:** If generation moves to `orchestrate.py generate-expert` (per F1), this is solved — the subcommand accepts a `--prompt-file` path override and uses the standard assembly pipeline. If generation stays in SKILL.md, add an `assemble_explorer_prompt()` parameter `pass_prompt_text: str | None = None` that bypasses the file lookup.

### F3. `post_explorers()` doesn't discover dynamically-added explorer output

**Consensus: 2/4 judges (Missing Reqs, Feasibility implicitly).** If a generated expert writes output to `{session_dir}/explorer-{domain}.json`, `post_explorers()` won't find it because it reads the task list from the original `launch.json`. The generated expert's findings are silently lost.

**Fix:** Either (a) the `generate-expert` subcommand updates `launch.json` with the new task entry before `post-explorers` runs, or (b) `post_explorers()` discovers explorer outputs by globbing `session_dir/explorer-*.json`, or (c) add a `--extra-explorer` flag to `post-explorers`.

### F4. `match_enrichments()` pseudocode contradicts the degradation matrix

**Consensus: 2/4 judges (Missing Reqs, Completeness).** Existing checklists (`checklist-sql-safety.md`, `checklist-concurrency.md`, `checklist-llm-trust.md`) have no frontmatter. The pseudocode guards on `if checklist_meta and ...` — when `checklist_meta` is `None`, the checklist is silently skipped. But the degradation matrix says "no frontmatter → inject globally." The pseudocode must explicitly handle `None` as global injection.

**Fix:** Add explicit `None` handling: `if checklist_meta is None: enrichments.append(content); continue`.

---

## High Findings

### F5. Don't change `assemble_explorer_prompt()` signature

**Consensus: 3/4 judges (Feasibility, Completeness, Scope).** Changing `domain_checklists: str` to `enrichments: list[str]` breaks 2 call sites, 10+ tests, the `PromptContext` dataclass, the `render()` method, and the truncation cascade. The in-flight context enrichment work modifies the same file.

**Fix:** Keep `domain_checklists: str`. Have `match_enrichments()` return `list[str]`, join them into a single string before passing. The enrichment routing happens upstream in the `prepare()` loop, not in the prompt assembly signature. This makes the change invisible to downstream code.

### F6. `ChecklistMeta` dataclass undefined

**Consensus: 2/4 judges (Missing Reqs, Completeness).** `parse_checklist_frontmatter()` is referenced but never defined. No return type, no error handling contract.

**Fix:** Define `ChecklistMeta(name, relevant_domains, relevant_experts)` dataclass and full `parse_checklist_frontmatter()` function with the same pattern as Spec A's expert parser.

### F7. Per-expert enrichment may be premature optimization

**Consensus: 2/4 judges (Scope, implicitly Feasibility).** The filtering saves ~600 tokens per expert (3 domain checklists × 200 tokens vs 1 relevant checklist). The total prompt budget is 70k. The spec itself notes "the LLM naturally focuses on relevant items." The machinery (new parser, new matching function, new frontmatter on all checklists, fallback chain) is significant.

**Fix:** Ship global injection as default. Add `relevant_domains` frontmatter now (cheap). Implement per-expert filtering behind `enrichment_mode: "per-expert" | "global"` flag, and only activate it after eval data shows irrelevant checklists cause false positives.

### F8. Dynamic generation may be premature given expanded roster

**Consensus: 2/4 judges (Scope, implicitly Feasibility).** After Spec A ships 15 experts + language checklists + domain checklists, how often will coverage gaps actually occur? The spec's example domains (Helm, Terraform) are covered by the new `infrastructure` expert. Generation is a significant amount of machinery for a case the activation threshold makes rare.

**Fix:** Defer Wave 2 until Spec A's `detect_coverage_gaps()` provides real frequency data. If >10% of reviews hit a meaningful gap, build generation. The coverage map already tells the judge about gaps — that may be sufficient.

### F9. Multiple uncovered domains + max 1 generated expert: no priority rule

**Consensus: 2/4 judges (Missing Reqs, Completeness).** When 2+ domains are uncovered but only 1 generated expert is allowed, there's no algorithm for choosing which domain gets the expert.

**Fix:** Pick the domain with the most changed files. Ties broken alphabetically. Add `priority_domain` to the `coverage_gaps` schema, computed by `prepare()`.

### F10. `relevant_experts` matching missing from pseudocode

**Consensus: 2/4 judges (Missing Reqs, Completeness).** Section 1c describes `relevant_experts` as "always inject regardless of domain match" but the `match_enrichments()` code never checks it.

**Fix:** Add `if expert.name in checklist_meta.relevant_experts: enrichments.append(content); continue` before the domain overlap check.

### F11. Render order contradiction

**Consensus: 1/4 judges (Missing Reqs) but architecturally important.** The spec says enrichments inject "between pass prompt and diff context." But current `PromptContext.render()` places diff BEFORE domain checklists. The spec requires a render reorder.

**Fix:** Match the current render order (enrichments after diff, within the Context section). Don't reorder.

---

## Medium Findings

| # | Finding | Source | Resolution |
|---|---------|--------|------------|
| F12 | Synthesizer coverage check (4e) blocked on Wave 2 but only needs Spec A's coverage map | Scope | Move coverage completeness to Spec A Wave 2; keep generated-expert skepticism in Spec B |
| F13 | Chunked mode interaction absent | Missing Reqs | Add section: per-expert enrichment operates per-chunk file set; generation is per-review |
| F14 | `max_experts` interaction with generated expert | Missing Reqs | State: generated experts don't count against `max_experts` cap |
| F15 | Cache description is redundant (mktemp creates fresh dirs) | Missing Reqs | Simplify: "cache lives in session dir and dies with it" |
| F16 | `relevant_experts` creates maintenance burden for static expert names | Missing Reqs | Add note: generated experts match only via `relevant_domains`, not `relevant_experts` |
| F17 | Generated checklist validation doesn't check domain relevance | Missing Reqs | Add heuristic: ≥1 checklist item must contain domain keyword |
| F18 | No integration test strategy for SKILL.md generation flow | Missing Reqs | Add fixture-based integration test requirement |
| F19 | Prescan routing (Open Q #4) shapes the prescan output schema | Scope | Resolve now: P-SEC→security, P-ERR→error-handling, P-*→all |
| F20 | Checklist registry should be formalized alongside ExpertRegistry | Scope | Build `ChecklistRegistry` with same pattern |
| F21 | Spec A types don't exist yet — code against current dict format | Feasibility | Use `expert.get("domains", [])` until Spec A lands |
| F22 | `load_domain_checklists()` migration path undefined | Missing Reqs | Keep as fallback; `match_enrichments()` is a new function |
| F23 | P5 cap not enforced by truncation cascade | Feasibility | Enforce in `match_enrichments()` pre-truncation |

---

## Recommendation

**Verdict: WARN** — Address before implementing.

**Structural changes needed:**

1. **Move generation to `orchestrate.py generate-expert` subcommand** (F1, F2, F3) — This is the single highest-leverage fix. It resolves the SKILL.md fragility, the prompt path crash, and the post_explorers discovery problem in one architectural move. SKILL.md calls one bash command. The subcommand uses the real assembly pipeline.

2. **Don't change `assemble_explorer_prompt()` signature** (F5) — Keep `domain_checklists: str`, join enrichments upstream. Zero test breakage.

3. **Fix the pseudocode to match the degradation matrix** (F4, F10) — Two concrete code fixes: handle `None` frontmatter as global injection, add `relevant_experts` matching.

4. **Define `ChecklistMeta` dataclass** (F6) — Small addition, blocks implementation without it.

5. **Consider deferring Wave 2** (F8) until gap frequency data from Spec A is available. The coverage map already tells the judge about gaps.

## Decision Gate

- [ ] PROCEED — Council passed, ready to implement
- [x] ADDRESS — Fix the 5 structural changes above before implementing
- [ ] RETHINK — Fundamental issues, needs redesign
