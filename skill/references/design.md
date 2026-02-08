# Architecture & Design Rationale

This document contains background context for the codereview skill. It is not needed at runtime — the executing agent should follow SKILL.md directly.

---

## Architecture: Explorer-Judge Pattern

```
┌──────────────────────────────────────────────────────────────┐
│  Step 2: Context Gathering                                    │
│  - Diff analysis, callers/callees, dead code check            │
│  - Complexity analysis (radon/gocyclo)                        │
│  - Spec/plan loading                                          │
│  → Produces context packet                                    │
└──────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────┐
│  Step 3: Deterministic Scans                                  │
│  semgrep, trivy, osv-scanner, shellcheck, pre-commit,         │
│  sonarqube (via skill-sonarqube, if installed)                 │
│  → Produces deterministic findings                            │
└──────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│ Explorer:     │ │ Explorer:     │ │ Explorer:     │  ... (4 total)
│ Correctness   │ │ Security      │ │ Reliability   │
│               │ │               │ │               │
│ Uses Grep,    │ │ Uses Grep,    │ │ Uses Grep,    │
│ Read, Glob    │ │ Read, Glob    │ │ Read, Glob    │
│ to investigate│ │ to investigate│ │ to investigate│
│               │ │               │ │               │
│ Returns JSON  │ │ Returns JSON  │ │ Returns JSON  │
│ findings      │ │ findings      │ │ findings      │
└───────┬───────┘ └───────┬───────┘ └───────┬───────┘
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────────┐
│  Review Judge                                                 │
│  - Deduplicates explorer findings                             │
│  - Validates claims against codebase (Grep/Read)              │
│  - Assesses strengths                                         │
│  - Checks spec completeness                                   │
│  - Produces verdict: PASS / WARN / FAIL                       │
│  → Returns validated findings + verdict + strengths            │
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│  Step 5: Classify into tiers                                  │
│  Step 6: Format report with Next Steps                        │
│  Step 7: Save artifacts (.md + .json)                         │
└──────────────────────────────────────────────────────────────┘
```

**Why explorer-judge instead of independent passes:**
- Explorers use `sonnet` (fast, good at search) — cheaper and faster for investigation
- The judge uses the default model (thorough) — better for synthesis and verdict
- Explorers can use tools (Grep/Read/Glob) to deeply investigate their area
- The judge sees all findings together, resolving conflicts and removing duplicates
- Total context pressure is lower: each explorer only handles one specialty

---

## Design Rationale

| Decision | Why | Source |
|----------|-----|--------|
| Explorer-judge architecture | Specialized sub-agents investigate deeply, judge synthesizes and validates | Vibe/council explorer pattern, context window management |
| Complexity analysis in context | Feed radon/gocyclo scores to AI so it flags high-complexity functions | Vibe skill complexity step |
| Language standards (optional) | Give explorers concrete language-specific rules; graceful degradation if not installed | Vibe/standards skill two-tier system |
| Dead code / YAGNI check | Avoid reviewing and fixing unused code — waste of agent time | receiving-code-review YAGNI pattern |
| Spec/plan comparison | Check implementation completeness against requirements | requesting-code-review plan comparison |
| Merge verdict (PASS/WARN/FAIL) | Clear ship/no-ship signal for humans and downstream agents | Vibe/council verdict pattern, requesting-code-review assessment |
| Strengths section | Acknowledge good patterns — review isn't just finding faults | requesting-code-review strengths output |
| Configurable pushback level | fix-all for agent workflows, cautious for human review | User feedback: agents should fix most issues, but not rabbit-hole |
| Configurable review cadence | pre-commit for quality-critical, wave-end for throughput | User request: some projects need every-commit review |
| Next Steps with fix ordering | Guide downstream agents on what to fix and in what order | receiving-code-review implementation ordering |
| Deterministic scans before AI | Run semgrep/trivy/osv-scanner/sonarqube first so AI skips restating their findings | Previous plan, playbook 4-stage pipeline |
| Comprehensive findings (no hard cap) | Code agents fix fast — surface everything actionable, let tiers prioritize | Agent-assisted workflow reality |
| Action tiers (Must/Should/Consider) | Structured prioritization without losing lower-severity findings | Replaces rigid comment budget |
| Confidence floor (0.65) | Dramatically reduces false positives | Playbook signal controls |
| Structured JSON + Markdown output | JSON for machine consumption and validation; Markdown for humans | Global contract schema |
| Envelope metadata in artifacts | run_id/timestamp/scope/tool_status/verdict make reviews traceable | Previous plan |
| Best-effort degradation | Skip unavailable tools with explicit status rather than failing | Previous plan |
| Repo-level config file | Teams customize passes, cadence, pushback, paths, thresholds | CodeRabbit `.coderabbit.yaml`, Gemini `config.yaml` |

---

## Future: Multi-Model Consensus (v2)

Running the same review across multiple models (e.g., Claude + Codex) and comparing their findings could significantly improve review quality. When two models independently flag the same issue, confidence is very high. When they disagree, the disagreement itself is a signal worth human attention.

Adversarial debate (where models review each other's findings and must steel-man opposing views before revising) is another promising direction — the council/vibe skills demonstrate this pattern works well.

These are areas to explore once the core single-model review is battle-tested.
