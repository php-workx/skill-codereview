# Plan: Code Review Skill v1.3

Sixteen features in four groups to improve review quality and reduce agent interpretation variance. The foundation (Feature 0) extracts mechanical pipeline steps into scripts. Group A enriches explorer context. Group B improves explorer and judge behavior. Group C adds provenance awareness. Features 8-9, 13 inspired by Claude Octopus analysis; Features 10-12 from Kodus-AI analysis; Features 14-15 from CodeRabbit gap analysis.

**Execution is split into two parallel plans:**
- **[Plan: Context Enrichment](plan-context-enrichment.md)** — code_intel.py, enrich-findings.py, prescan, checklists, cross-file planner, REVIEW.md, path instructions (F0b, F0c, F1, F2, F12, F13, F15)
- **[Plan: Explorer & Judge Behavior](plan-explorer-judge-behavior.md)** — phantom knowledge, mental execution, test pyramid, certification, contract gate, batching, provenance (F4-F11)

Archive of the full original spec: `plan-treesitter-v1.md`

---

## Feature Status

| # | Feature | Status | Plan | Notes |
|---|---------|--------|------|-------|
| **F0a** | run-scans.sh | **Done** | — | Built and improved: merged tiers, added ast-grep + actionlint, removed osv-scanner + sonarqube |
| **F0b** | enrich-findings.py | Not started | Context Enrichment | Post-explorer finding enrichment |
| **F0c** | code_intel.py | Not started | Context Enrichment | Foundation — callers, functions, imports, graph |
| **F1** | Prescan | Not started | Context Enrichment | Depends on code_intel.py |
| **F2** | Domain Checklists | Not started | Context Enrichment | Independent |
| **F3** | Git History Risk | **Done** | — | git-risk.sh built and integrated |
| **F4** | Test Pyramid Vocabulary | Not started | Explorer/Judge | Prompt + schema |
| **F5** | Per-File Certification | Not started | Explorer/Judge | Judge + global contract |
| **F6** | Contract Completeness Gate | Not started | Explorer/Judge | Judge + spec-verification |
| **F7** | Output File Batching | Not started | Explorer/Judge | Judge + global contract + orchestrate.py |
| **F8** | Pre-Existing Bug Classification | Not started | Explorer/Judge | Depends on enrich-findings.py (F0b) |
| **F9** | Provenance-Aware Rigor | Not started | Explorer/Judge | Depends on enrich-findings.py (F0b) |
| **F10** | Phantom Knowledge Self-Check | Not started | Explorer/Judge | Quick win — prompt only |
| **F11** | Mental Execution Framing | Not started | Explorer/Judge | Quick win — prompt only |
| **F12** | Cross-File Context Planner | Not started | Context Enrichment | Enhanced by code_intel.py graph |
| **F13** | REVIEW.md Directives | Not started | Context Enrichment | Independent |
| **F14** | File-Level Triage | **Done** | — | Built, enabled by default, triage_files() in orchestrate.py |
| **F15** | Path-Based Review Instructions | Not started | Context Enrichment | Independent |

## Completed Since Original Plan

These items were built outside the original plan scope but fulfill v1.3 goals:

| Item | What was built | Date |
|------|---------------|------|
| **Python orchestrator** | `scripts/orchestrate.py` — deterministic pipeline replacing agent-interpreted SKILL.md. Handles diff extraction, expert panel assembly, prompt rendering, token budgeting, launch packets. | 2026-03-27 |
| **Security explorer split** | Two focused passes: `security-dataflow` (taint analysis, IRIS-inspired) and `security-config` (pattern recognition, CWE-specific tables). Backward compat for `passes: [security]` and `pass_models`. | 2026-03-27 |
| **ast-grep-essentials** | 184 deterministic security rules integrated into run-scans.sh. Lazy-clones from GitHub. | 2026-03-27 |
| **File-level triage** | `triage_files()` in orchestrate.py. Enabled by default. Classifies files as complex/trivial based on extension and line count. | 2026-03-27 |
| **suggest_missing_tests flag** | Config flag (default: off) suppresses "add test for X" suggestions while keeping stale/broken test detection. CLI: `--suggest-missing-tests`. | 2026-03-28 |
| **--passes CLI flag** | Run subset of experts: `--passes correctness,reliability`. Used by Martian benchmark (default: correctness,reliability). | 2026-03-28 |
| **Scan consolidation** | Merged Tier 1 + Tier 2 into single parallel wave. Removed osv-scanner (redundant with trivy), removed sonarqube (required external server). Added actionlint for GitHub Actions. | 2026-03-28 |
| **Eval infrastructure** | OWASP + Martian benchmarks with prompt-test command, three vulnerability models, pass tagging, findings_path fix, batch/timeout tuning. See `docs/benchmark-guide.md`. | 2026-03-27/28 |

## Architecture (Current State)

The pipeline is now driven by `scripts/orchestrate.py`, not SKILL.md:

```
orchestrate.py prepare
    │ diff extraction, config loading, project discovery
    │ parallel: complexity.sh, git-risk.sh, run-scans.sh, coverage-collect.py
    │ triage_files() → classify changed files
    │ assemble_expert_panel() → select active experts
    │ assemble_explorer_prompt() → render prompts with context
    │ build_launch_packet() → JSON packet for the skill
    ▼
SKILL.md (thin, ~50 lines)
    │ reads launch.json, launches explorer agents
    ▼
Explorer agents (parallel)
    │ correctness, security-dataflow, security-config, test-adequacy
    │ + conditional: shell-script, api-contract, concurrency, etc.
    ▼
orchestrate.py post-explorers
    │ normalize, deduplicate, build judge input
    ▼
Judge agent
    │ 4-expert panel: Gatekeeper → Verifier → Calibrator → Synthesizer
    ▼
orchestrate.py finalize
    │ render report, write artifacts
```

Features in the two plans add to this pipeline:
- **Context Enrichment** adds new data sources in `prepare` (code_intel, prescan, checklists, cross-file planner, REVIEW.md, path instructions)
- **Explorer/Judge Behavior** modifies explorer prompts (phantom knowledge, mental execution) and judge behavior (certification, batching, completeness gate)

## Design Principles

**Scripts Over Prompts** — Wherever a step is mechanical, implement it as a script.

**Checklists Over Instructions** — Concrete checklist items rather than open-ended instructions.

**The boundary is judgment.** If it requires reading code and reasoning about behavior → AI. If it's applying a formula or running a tool → script.

## Dependency Graph

```
                        ┌── Plan: Context Enrichment ──────────────────────┐
                        │                                                   │
                        │  Wave 1: F2, F13, F15 (independent, parallel)     │
                        │  Wave 2: F0c (code_intel.py), F0b (enrich)        │
                        │  Wave 3: F1 (prescan), F12 (cross-file planner)   │
                        │                                                   │
 Already done:          └───────────────────────────────────────────────────┘
 F0a, F3, F14,                     │
 security split,                   │ F0b feeds into F8, F9
 triage, --passes                  ▼
                        ┌── Plan: Explorer & Judge Behavior ───────────────┐
                        │                                                   │
                        │  Wave 1: F10, F11 (quick wins, parallel)          │
                        │  Wave 2: F4, F5, F6, F7 (serialize F5/F6/F7)     │
                        │  Wave 3: F8, F9 (after enrich-findings.py)        │
                        │                                                   │
                        └───────────────────────────────────────────────────┘
```

Plans 2 and 3 can execute in parallel. The only cross-plan dependency is F0b (enrich-findings.py) which F8 and F9 consume.

## Relationship to Other Plans

| Plan | Relationship |
|------|-------------|
| [Verification Pipeline](plan-verification-pipeline.md) | Adds 3-stage safeguard after explorers. Independent of v1.3 features. Can be built in parallel. |
| [Python Orchestrator](plan-orchestrator.md) | **Done.** orchestrate.py is the foundation these features build on. |
| [Security Explorer](handoff-security-explorer-results.md) | **Done.** Security-dataflow + security-config split, ast-grep, triage, eval improvements. |
