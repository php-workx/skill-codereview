# Pre-Mortem: Adaptive & Dynamic Expert Selection

**Date:** 2026-03-28
**Spec:** `specs/adaptive-expert-selection.md`
**Scope mode:** HOLD SCOPE
**Verdict:** WARN — Strong architectural vision, but significant gaps block implementation

## Council Panel

| Judge | Perspective | Key Finding |
|-------|-------------|-------------|
| Missing Requirements | Integration gaps, edge cases | 20 findings; existing code touchpoints not traced through |
| Feasibility | Technical constraints, code compat | 10 findings; function names wrong, YAML dependency issue, LLM-in-prepare breaks architecture |
| Spec Completeness | Data types, acceptance criteria | 15 findings; core dataclasses undefined, no degradation matrix |
| Scope & Architecture | Right-sizing, complexity | 9 findings; split into two specs, 25 experts speculative, dynamic generation premature |

---

## Critical Findings (must fix before implementation)

### F1. Split into two specs

**Consensus: 4/4 judges.** The spec bundles a solid deterministic improvement (frontmatter + expanded roster + signal-based selection) with speculative features (dynamic enrichment + LLM generation + judge awareness) that depend on unbuilt context enrichment features (F0c, F1).

**Fix:** Split into:
- **Spec A: Expert Registry & Structured Selection** (Layers 1-3) — Fully deterministic, deliverable now
- **Spec B: Dynamic Enrichment & Generation** (Layers 4-6) — Depends on code_intel.py, prescan.py; ships after Spec A proves out

### F2. Stale function names throughout

**Consensus: 2/4 judges.** The spec repeatedly references `_build_explorer_panel()` which does not exist. The actual function is `assemble_expert_panel()` at orchestrate.py:904 with signature `(diff_result, config, spec_content)`. The spec's pseudocode `select_experts(signals, registry, config)` is not a drop-in replacement — every caller and the downstream data flow (`_build_expert()`, `EXPERT_PROMPT_FILES`, `expert_to_task()`) needs updating.

**Fix:** Trace through the actual call chain: `prepare()` -> `assemble_expert_panel()` -> `_build_expert()` -> `EXPERT_PROMPT_FILES[name]`. Map each spec function to its real integration point.

### F3. Core data types undefined

**Consensus: 3/4 judges.** `ReviewSignals`, `ExpertMeta`, `SelectedExpert`, `ExpertRegistry` are used in pseudocode but never defined with fields and types. An implementer cannot write the dataclass.

**Fix:** Add full `@dataclass` definitions with field names, types, and defaults. Judge 3 provided complete suggested definitions.

### F4. `detected_domains` has no producer

**Consensus: 3/4 judges.** The gap detection system (`detect_coverage_gaps()`) reads `signals.detected_domains`, but nothing populates this field. There is no mapping from file extensions or imports to semantic domain names like "async", "helm", "terraform". This mapping is the actual hard part of selection and the spec hand-waves it.

**Fix:** Define the domain taxonomy as a concrete `DOMAIN_SIGNALS` mapping or, better, derive it from the expert frontmatter — a domain is "detected" if any expert that covers it has at least one activation signal triggered, regardless of total score. Add the `detect_domains()` function to the spec.

### F5. `supersedes` / `complements` — dead fields with no consumer

**Consensus: 4/4 judges.** Defined in frontmatter, referenced in prose, but `select_experts()` never reads them. No algorithm specified. Creates maintenance burden (every new expert author must populate them) with zero current value.

**Fix:** Remove from Wave 0-3. Add to frontmatter only when the consumption algorithm is designed and implemented. Document as "planned future field" in Open Questions.

### F6. YAML is optional but frontmatter parsing requires it

**Consensus: 3/4 judges.** `yaml` is imported conditionally. If frontmatter patterns replace `EXTENDED_EXPERT_PATTERNS`, maintaining both copies creates silent behavior divergence depending on whether PyYAML is installed.

**Fix:** Either (a) make `yaml` a hard dependency (it's already the only one), or (b) use a simple regex-based frontmatter parser for the subset of YAML needed (~50 lines, no new dependency), or (c) keep frontmatter as the source of truth and generate the Python dicts from frontmatter at release time (build step, not runtime).

### F7. Degradation matrix missing

**Consensus: 2/4 judges.** The context enrichment plan has a detailed 6-row degradation matrix with exact log messages. This spec has none. Implementers won't know what happens when frontmatter parsing fails, no experts match, generation times out, etc.

**Fix:** Add a degradation matrix following the established pattern. Judge 3 provided a complete 7-row table.

---

## High Findings (should fix before implementation)

### F8. Dynamic generation LLM call breaks deterministic `prepare()` phase

**Consensus: 3/4 judges.** The `prepare()` function is purely deterministic — no LLM calls. The project's design principle is "Scripts Over Prompts" for deterministic work. Adding an LLM call to `prepare` breaks this architecture.

**Fix:** Move generation to SKILL.md (the AI wrapper). After `prepare()` emits `launch.json`, if `coverage_gaps` exist, SKILL.md runs a lightweight sub-agent to generate the expert prompt, then appends it to the waves. This is a SKILL.md change, not an orchestrate.py change. (Deferred to Spec B per F1.)

### F9. 25 experts is speculative — ship 4-5, defer the rest

**Consensus: 2/4 judges.** Writing 15 high-quality expert prompts (each 40-60 lines of domain-specific review instructions with calibration examples) is a major authoring effort. The `documentation`, `state-management`, `dependency-management`, and `configuration` experts overlap with `correctness` if enrichment context is good. The real gap (Helm, Terraform, CUDA) requires 4-5 new experts, not 15.

**Fix:** Wave 1 ships with: `database`, `infrastructure`, `performance`, `authorization`, `frontend`. Defer the rest until eval data shows which domains produce false negatives. The frontmatter + registry makes adding experts later trivial.

### F10. Scoring weights are magic numbers without calibration plan

**Consensus: 2/4 judges.** Weights 0.4/0.5/0.3/0.2 are presented as tunable with no calibration methodology. Additive scoring means two weak signals (file-type 0.3 + marker 0.2 = 0.5) outweigh a single strong signal (pattern 0.4). No metric defined for what "correct selection" means.

**Fix:** Start with simpler model: binary activation (any signal triggers candidacy) + priority sort (more signal types = higher rank) + max_experts cap. This matches the current regex system's behavior but with more signal sources. Defer weighted scoring until eval data shows binary activation selects wrong experts.

### F11. Token budget interaction under-specified

**Consensus: 3/4 judges.** More experts x enrichments x coverage map could blow the token budget. The context enrichment plan has P0-P12 priority tiers. The spec doesn't map expert enrichments to this hierarchy. Current `load_domain_checklists()` is global — every explorer gets every matched checklist. Per-expert enrichment (Layer 4) requires per-expert checklist filtering.

**Fix:** Add a token budget section mapping enrichments to priority tiers. Expert enrichments should be P5-level (domain checklists), not P0 (never truncated). Compute worst-case: 6 experts x (60-line prompt + 500 tokens enrichment) = ~9,000 tokens of pass prompts. (Mostly deferred to Spec B per F1.)

### F12. `--passes` CLI and config `experts:` interaction with dynamic registry

**Consensus: 2/4 judges.** `--passes correctness,database` validates against hardcoded `known_experts = set(CORE_EXPERTS) | set(EXTENDED_EXPERT_PATTERNS)`. New experts from frontmatter won't be in this set. Also: `select_experts()` pseudocode has no mechanism for honoring existing `experts: {concurrency: false}` disable config.

**Fix:** Build registry early in `prepare()`, before validation. Replace hardcoded `known_experts` with `registry.known_names()`. Add explicit handling: when `--passes` lists an expert, force-activate it regardless of score threshold. When `experts: {name: false}` disables one, filter it before scoring.

### F13. Acceptance criteria and test strategy are vague

**Consensus: 3/4 judges.** "Verify backward compat — same experts selected for same diffs" is the right instinct but has no concrete test plan. No golden-file fixtures. No assertion strategy.

**Fix:** Add per-wave acceptance criteria with testable assertions. Define the backward-compat test: snapshot current `assemble_expert_panel()` outputs for 5 representative diffs before any code change. After migration, assert new `select_experts()` produces identical expert name sets. Write the snapshot test before touching any selection code.

### F14. `EXPERT_PROMPT_FILES` many-to-one mapping

**Consensus: 1/4 judges, but critical for Wave 0.** `shell-script` and `reliability` both map to `reviewer-reliability-performance-pass.md`. Frontmatter-based registry assumes one file = one expert. The second alias disappears.

**Fix:** Split `reviewer-reliability-performance-pass.md` into two files, or add an `aliases:` field to frontmatter so one file can register under multiple names.

### F15. `security` alias expansion not updated

**Consensus: 1/4 judges.** `passes: [security]` currently expands to `{security-dataflow, security-config}`. After adding `authorization` (split from security), users expect `security` to include it. But the expansion logic isn't updated.

**Fix:** Define a `groups:` frontmatter field (e.g., `groups: [security]`). The orchestrator expands group names to all experts in that group. Add `security` group to `security-dataflow`, `security-config`, and `authorization`.

### F16. `spec-verification` conditional activation has no frontmatter equivalent

**Consensus: 1/4 judges.** Current code skips `spec-verification` when `not spec_content`. No frontmatter field can express "activate only when a runtime context exists."

**Fix:** Add `requires_context:` field to frontmatter (e.g., `requires_context: [spec_content]`). Or document as a known hardcoded exception in `select_experts()`.

---

## Medium Findings (address during implementation)

| # | Finding | Source | Resolution |
|---|---------|--------|------------|
| F17 | `cost: standard\|high` field has no consumer, implies deferred model routing | Judges 2,4 | Remove from frontmatter schema |
| F18 | `documentation` expert is noise | Judge 4 | Remove from roster; handle via correctness enrichment |
| F19 | `CONFIG_ALLOWLIST` not updated for `max_experts`, `dynamic_experts` | Judges 1,3 | Add new keys in the relevant wave |
| F20 | `validate_prompt_files()` breaks with dynamic roster | Judge 1 | Rewrite to use registry instead of hardcoded dict |
| F21 | Per-chunk expert selection not addressed | Judge 1 | Document as non-goal: selection is per-review |
| F22 | Wave assignment for 20+ experts unresolved | Judges 2,4 | Keep single-wave model; document the decision |
| F23 | Expert enable/disable config drift with splits | Judge 2 | Add alias mechanism: `authorization.aliases = ["security-config"]` |
| F24 | Registry loading performance | Judge 1 | State explicitly: lazy load in prepare, <25ms for 25 files |
| F25 | `context-planner.md` interaction not addressed | Judge 1 | Add section on interaction with context enrichment epic |
| F26 | Logging/observability for selection decisions | Judge 3 | Add event table: `expert_selected`, `expert_deactivated`, `expert_below_threshold` |
| F27 | LLM generation details (model, prompt, temp, timeout) | Judge 3 | Specify fully in Spec B |
| F28 | Template variable resolution ambiguous | Judge 3 | Clarify deterministic vs LLM-filled placeholders in Spec B |
| F29 | Coverage map format stability | Judge 3 | Informational markdown for the judge, not machine-parsed |
| F30 | Performance budget for each operation | Judge 3 | Add latency table: parsing <25ms, scoring <5ms, generation 3-8s |
| F31 | Rollback / feature flag per wave | Judge 1 | Add `expert_selection: "legacy"\|"signal"` config flag |
| F32 | `_should_deactivate()` not defined | Judges 1,3 | Add pseudocode with edge case semantics |
| F33 | Enrichment-to-expert matching algorithm missing | Judge 1 | Defer to Spec B; currently all checklists go to all experts |

---

## Recommendation

**Verdict: WARN** — The spec has strong research backing and sound architecture, but is not implementation-ready in its current form.

**Action: ADDRESS before implementing.** Specifically:

1. **Split the spec** (F1) — This is the single highest-leverage change. Spec A (Layers 1-3) is deliverable now. Spec B (Layers 4-6) waits for context enrichment.

2. **Fix the 7 critical findings** (F1-F7) in Spec A — stale function names, data types, domain detection, remove dead fields, YAML strategy, degradation matrix.

3. **Resolve the high findings** (F8-F16) — most are automatically addressed by the split (F8, F11 move to Spec B) or are concrete additions (F10, F12, F13).

4. **Scope the roster** (F9) — 5 new experts in Wave 1, not 15. The registry makes adding more later trivial.

After these are addressed, the spec will be implementation-ready and robust enough for external review.
