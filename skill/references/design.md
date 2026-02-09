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
┌──────────────────────────────────────────────────────────────┐
│  Step 3.5: Adaptive Pass Selection                            │
│  - Evaluate skip signals for extended passes                  │
│  - Skip concurrency pass if no concurrency primitives         │
│  - Skip api-contract pass if no public API changes            │
│  - Skip error-handling pass if test/docs/config only          │
│  - Skip spec-verification pass if no spec loaded              │
│  - Core passes (correctness, security, reliability, tests)    │
│    are never skipped                                          │
└──────────────────────────────────────────────────────────────┘
                              │
    ┌────────┬────────┬───────┼───────┬────────┬────────┬────────┐
    ▼        ▼        ▼       ▼       ▼        ▼        ▼        ▼
┌────────┐┌────────┐┌────────┐┌────────┐┌────────┐┌────────┐┌────────┐┌────────┐
│Correct-││Secur-  ││Reliab- ││Test    ││Error   ││API/    ││Concur- ││Spec    │
│ness    ││ity     ││ility   ││Adequacy││Handling││Contract││rency   ││Verif.  │
│        ││        ││        ││        ││        ││        ││        ││        │
│ core   ││ core   ││ core   ││ core   ││extended││extended││extended││extended│
│        ││        ││        ││+ test  ││        ││        ││        ││req→impl│
│        ││        ││        ││category││        ││        ││        ││→tests  │
│ Each explorer: chain-of-thought investigation, calibration            │
│ examples, false positive suppression, Grep/Read/Glob to verify        │
└───┬────┘└───┬────┘└───┬────┘└───┬────┘└───┬────┘└───┬────┘└───┬────┘└───┬────┘
    │         │         │         │         │         │         │         │
    └─────────┴─────────┴────┬────┴─────────┴─────────┴─────────┴─────────┘
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  Review Judge (prompts/reviewer-judge.md)                     │
│  1. Adversarial validation (existence, contradiction,         │
│     severity calibration)                                     │
│  2. Root cause grouping (merge related findings)              │
│  3. Cross-explorer synthesis (catch gaps across explorers)    │
│  4. Strengths assessment (specific, not generic)              │
│  5. Spec compliance: merge spec-verification explorer data,    │
│     validate impl/test claims, produce spec_requirements       │
│  6. Verdict: PASS / WARN / FAIL                               │
│  → Returns findings + verdict + strengths + spec_requirements │
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

**Why enriched prompts with calibration examples:**
- Thin prompts produce thin findings — explorers need concrete examples of what "good" looks like
- Calibration examples anchor severity and confidence scores to specific evidence levels
- False positive suppression lists dramatically reduce noise from common non-issues
- Chain-of-thought investigation phases ensure explorers investigate systematically instead of pattern-matching

**Why adversarial validation in the judge:**
- Explorers over-report by design (optimizing for recall)
- The judge optimizes for precision — every finding that survives should be real
- Contradiction checks catch the most common false positives: "missing null check" when a guard exists upstream, "N+1 query" when the ORM eager-loads, etc.
- Root cause grouping prevents the same issue from being reported 3 times by different explorers

**Why adaptive pass selection:**
- Running a concurrency pass on a CSS change wastes time and produces noise
- Running an API/contract pass on internal refactoring wastes time
- Core passes always run because correctness, security, reliability, and test coverage are always relevant
- Extended passes are specialized enough that skip signals can reliably determine relevance

---

## Design Rationale

| Decision | Why | Source |
|----------|-----|--------|
| Explorer-judge architecture | Specialized sub-agents investigate deeply, judge synthesizes and validates | Vibe/council explorer pattern, context window management |
| Enriched prompts with CoT phases | Thin prompts produce thin findings; structured investigation ensures depth | v1 experience: shallow findings with 16-line prompts |
| Calibration examples in each prompt | Anchor severity/confidence to specific evidence levels | v1 experience: inconsistent severity calibration |
| False positive suppression lists | Dramatically reduces noise from common non-issues | v1 experience: frequent false alarms on parameterized SQL, auto-escaped HTML, etc. |
| Adversarial judge validation | Explorers optimize for recall, judge optimizes for precision | Council/debate pattern: disagreement reveals signal |
| Root cause grouping in judge | Prevent duplicate reports when multiple explorers flag symptoms of the same issue | v1 experience: 3 explorers reporting the same null check bug |
| Extended passes (error-handling, api-contract, concurrency) | These are distinct concern areas that deserve specialized investigation | v1 gap: error swallowing buried in reliability pass, API contracts buried in correctness |
| Adaptive pass selection | Avoid running irrelevant passes — saves time and reduces noise | Skip signals based on diff content: no concurrency primitives → skip concurrency pass |
| Configurable model per pass | Security and concurrency benefit from stronger models; test adequacy is fine with faster models | Model cost/quality tradeoff differs by pass complexity |
| Complexity analysis in context | Feed radon/gocyclo scores to AI so it flags high-complexity functions | Vibe skill complexity step |
| Language standards (optional) | Give explorers concrete language-specific rules; graceful degradation if not installed | Vibe/standards skill two-tier system |
| Dead code / YAGNI check | Avoid reviewing and fixing unused code — waste of agent time | receiving-code-review YAGNI pattern |
| Spec/plan comparison | Check implementation completeness against requirements | requesting-code-review plan comparison |
| Dedicated spec-verification pass | Requirement extraction, implementation tracing, and test category classification need deep investigation that doesn't fit in the judge's synthesis role | v1 gap: judge did shallow keyword matching, no test category awareness |
| Test category classification (unit/integration/e2e) | Knowing a test exists is not enough — knowing what *kind* of test it is determines if the right failure modes are caught | User need: DB interaction tested only with mocks misses schema drift |
| Per-requirement traceability (spec_requirements) | Flat spec_gaps list says what's missing but not what's covered or how — structured output enables downstream tooling and tracking | User need: verify spec section-by-section with evidence |
| --spec-scope flag | Large specs cover many features; scoping avoids context pollution and irrelevant "not implemented" noise | User need: verify specific milestone or section against diff |
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

These are areas to explore once the enriched single-model review is battle-tested.
