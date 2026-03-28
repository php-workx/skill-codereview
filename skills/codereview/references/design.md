# Architecture & Design Rationale

This document contains background context for the codereview skill. It is not needed at runtime — the executing agent should follow SKILL.md directly.

## Orchestrator Architecture

The Python orchestrator is the producer for the review pipeline. It writes a launch packet to `session_dir/launch.json`, then the later phases consume that file plus `judge-input.json` and `judge.json` as their shared state. The key constraint is that prompt assembly and artifact paths live in the packet, not in ad hoc agent memory.

The launch packet should capture the review scope, diff metadata, wave plan, judge configuration, scan outputs, and timing/config context in a machine-readable form. That keeps the judge and finalize phases deterministic and lets the pipeline evolve without changing the agent-facing contract.

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
│  Review Judge — Named Expert Panel (reviewer-judge-*.md)       │
│  Expert 1: Gatekeeper — pre-filter triage (auto-discard       │
│     phantom knowledge, speculative, framework-guaranteed,     │
│     outside scope, style-only, duplicate of deterministic)    │
│  Expert 2: Verifier — existence + evidence check with         │
│     Read/Grep (verified / unverified / disproven)             │
│  Expert 3: Calibrator — severity calibration, root cause      │
│     grouping, cross-explorer synthesis, contradiction resolve │
│  Expert 4: Synthesizer — strengths, spec compliance, verdict  │
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
| Historical risk scoring | Git log churn + bug frequency provides a per-file risk signal. Files with recent bug history get extra explorer attention. | Complements path-based risk heuristics with data-driven signal |
| Test coverage data integration | Collect measured line/function coverage from language-native tools (go cover, coverage.py, tarpaulin, c8/nyc) and feed it as context to explorers. Replaces inference-based coverage guessing with data. Best-effort: skips gracefully when tools are absent. | Measured coverage is strictly more accurate than AI inference for determining which code is exercised by tests |
| Finding fingerprinting (SHA-256, 12 hex chars) | Stable cross-review identity using `file_path + pass + severity + normalized(summary)`. Line numbers excluded because they shift; exact summary excluded because AI wording varies. 12 hex chars (48 bits) gives negligible collision probability for review-scale datasets (< 1000 findings). | Enables lifecycle tracking (new vs recurring) and suppression matching without requiring deterministic AI output |
| Suffix stemming in fingerprint normalization | Simple regex stripping of `-ing`, `-ed`, `-tion`, `-ment`, `-ness`, `-ly`, `-ble`, `-er`, `-est` bumps exact-match rate from ~70-80% to ~85-90%. Example: "missing" and "miss" converge, "validation" and "valida" converge. | No NLP library dependency; good enough for morphological variant convergence. Remaining ~10-15% mismatch caught by fuzzy match fallback |
| Fuzzy match fallback (60% key term overlap) | Secondary matching when exact fingerprint differs: same `file + pass + severity` and >= 60% stemmed word set overlap. Catches AI summary rewording that escapes stemming (e.g., "missing" vs "lacks"). | Tradeoff: 60% threshold is data-driven via test fixtures. Too low → false matches between genuinely different findings. Too high → misses legitimate rewording |
| Deferred scope (file/pass/exact) | Controls when deferred findings resurface. `file` (default): any change to the file. `pass`: change to file AND same pass fires. `exact`: only exact fingerprint match. | Granular control prevents noise — e.g., a deferred security finding shouldn't resurface when only correctness pass reviews a typo fix. `exact` is the strictest: effectively permanent deferral unless the exact same finding reappears |
| Named expert panel | Restructures judge as sequential expert roles (Gatekeeper→Verifier→Calibrator→Synthesizer). Forces sequential reasoning, makes analysis auditable, prevents step skipping. Zero cost — prompt reorganization only. | Kodus-AI panel-of-experts pattern |
| `code_intel.py` shared module (F0c) | Tree-sitter is optional with regex fallback so the skill works on any machine without pip installs. A single Python module replaces `complexity.sh` (bash) because Python handles multi-language AST parsing, regex patterns, and JSON output more reliably than bash string manipulation — and shares language config across subcommands (complexity, functions, imports, patterns, graph). | Context enrichment: code intelligence foundation |
| `enrich-findings.py` extraction (F0b) | Mechanical enrichment (ID assignment, tier classification, confidence floor filtering, evidence downgrade) is extracted from the agent into a deterministic script so results are reproducible across runs and independently testable. Judgment-dependent work (deduplication, root-cause grouping, severity calibration) stays in the agent because it requires reading code and reasoning about behavior. | Context enrichment: scripts-over-prompts principle |
| Prescan as context not findings (F1) | Prescan signals (secrets, swallowed errors, long functions, stubs, dead code candidates) are fed to explorers as investigation hints, not as findings — because regex patterns have high false-positive rates and lack the semantic understanding to confirm issues. Python over bash because multi-language function extraction and AST-aware checks require regex engines and data structures beyond bash's capabilities. Tree-sitter optional with the same regex fallback as `code_intel.py`. | Context enrichment: prescan signals |
| Static markdown checklists (F2) | Domain checklists are plain markdown files (one per domain: SQL, concurrency, LLM, etc.) because they are human-readable, version-controllable, and require no runtime infrastructure. Domain detection uses inline grep in `orchestrate.py` rather than a separate script because the detection logic is a few regex checks — not complex enough to justify a standalone file. | Context enrichment: domain checklists |
| LLM-driven cross-file planner (F12) | Pure graph analysis misses non-obvious relationships (a config change that affects a distant handler, a type alias used across modules). An LLM planner using Haiku tier catches these semantic connections at low cost. Results are mechanically enforced — the orchestrator injects planner output into explorer prompts rather than relying on explorers to discover cross-file relationships independently. (deterministic fallback — LLM planner integration planned) | Context enrichment: cross-file analysis |
| REVIEW.md alongside `.codereview.yaml` (F13) | Config YAML controls pipeline behavior (passes, thresholds, model routing); REVIEW.md provides human-authored review directives in prose. Keeping them separate preserves each file's single responsibility. Markdown format maximizes discoverability — teams already write CONTRIBUTING.md and CODEOWNERS, so REVIEW.md fits naturally. | Context enrichment: repo-level directives |
| fnmatch path instructions (F15) | Uses `fnmatch` glob patterns (e.g., `src/api/**`) consistent with the existing `ignore_paths` config, avoiding a second pattern syntax. Per-path granularity (not per-directory) allows instructions to target specific file patterns like `*_test.go` or `migrations/*.sql` without requiring directory-level grouping. | Context enrichment: path-based instructions |
| Phantom knowledge self-check (F10) | Absolutist framing ("DO NOT make claims") risks suppressing legitimate findings about code behind opaque abstractions (DI containers, macro-heavy crates, dynamic dispatch). The softened approach — mark as assumed, lower confidence, let the judge decide — preserves findings for adversarial review while flagging their evidentiary basis. Four self-check questions are positioned after Phase 4 (Confidence Calibration) so they're fresh when the explorer starts output generation. | Wharton/USC persona research, Huang et al. 2023 self-correction limitations, pre-mortem adversarial QA review |
| Test pyramid vocabulary (L0-L5, BF1-BF8) | "Add a test" is not actionable — developers need to know *what kind* of test to write. L-levels map to the standard test pyramid; BF-levels identify specific bug-finding techniques (property, snapshot, chaos, regression, backward-compat). Combined, they turn test-gap findings from vague suggestions into specific testing recipes. | Test-adequacy explorer gap analysis, research on structured test vocabulary |
| Per-file certification (F5) | Explorers returning bare `[]` provide no signal — can't distinguish "checked thoroughly, clean" from "skimmed and missed issues". Certification forces explicit accounting. Phase 1 validates file coverage only; tool call validation deferred until agent-to-orchestrator tool call passing is designed. | AgentOps deep audit protocol, pre-mortem pm-001 |
| Contract completeness gate (F6) | Per-requirement tracing catches what code missed implementing. The completeness gate catches what the spec forgot to specify — missing states, unhandled errors, contradictions, untestable requirements. Opt-in for formal specs only; severity low/consider so it never blocks merge. | AgentOps council 4-item contract gate |
| Always file-batch explorer output (F7) | Removes dual-mode discontinuity (inline vs file). Summary table gives the judge a triage overview; inline JSON preserved as reference. Foundation for future optimization where judge reads files on-demand instead of receiving all findings in prompt. | AgentOps council output pattern, context window management |
| Mental execution integrated into phases (F11) | A standalone preamble suffers from positional amnesia — by token 40k+ the instructions are forgotten. Integrating into Phases 3, 4, and 6 where the analysis naturally belongs keeps the instructions adjacent to the investigation context. Also avoids contradicting the confidence calibration table's 0.70-0.84 tier with absolutist "trace EXACT path or don't report" framing. | Pre-mortem systems architecture review, positional amnesia research |
| Pre-existing bug classification (F8) | Reviewers don't want to fix old bugs in a feature PR — but a dormant bug that the PR activates is critical context. The `pre_existing` + `pre_existing_newly_reachable` flags let explorers classify and the enrichment pipeline filter: non-reachable pre-existing bugs are dropped, reachable ones are retained with tier downgrade for medium/low severity. | Claude Octopus pre_existing_newly_reachable field |
| Provenance-aware review rigor (F9) | AI-generated code has distinct failure modes (over-abstraction, placeholder logic, unwired helpers) that human code rarely exhibits. Rather than always checking for these patterns (which would increase false positives on human code), the `--provenance` flag enables targeted investigation. The enrichment pipeline boosts AI-codegen risk findings from `consider` to `should_fix` when provenance indicates AI generation. | Claude Octopus provenance tracking, Codex autonomous agent output patterns |

---

## Large Changeset Support (Chunked Review Mode)

For diffs exceeding 80 files or 8000 lines, the standard pipeline hits context window limits — each explorer receives the entire diff (~40-80k tokens) plus context, leaving insufficient room for investigation. The chunked review mode transparently scales the review by splitting files into groups and using a single final judge.

### Architecture

```
Step 1:    Determine review target (unchanged)
                │
Step 1.5:  Mode selection + Diff triage + File clustering
           ├─ Count files/lines → decide standard vs chunked mode
           ├─ Build changeset manifest (risk tiers per file)
           ├─ Cluster files into chunks (8-15 files, max 2000 diff lines)
           └─ Write full diff to temp file (protect orchestrator context)
                │
Step 2-L:  Tiered context gathering
           ├─ Phase A: lightweight global context (~5k tokens)
           │   ├─ Import graph, dead code (new functions only)
           │   └─ Complexity hotspots (C+ only), standards, spec
           └─ Phase B: chunk-scoped deep context (per chunk, ~10-15k)
               ├─ Callers/callees (top 5/3 per function)
               └─ Types, test files
                │
Step 3:    Deterministic scans (unchanged, output scoped per chunk)
                │
Step 3.5:  Adaptive pass selection (per chunk, not global)
                │
Step 4-L:  Chunked AI review
           │
           ├─ 4-L.2: Chunked explorers (waves of 8-12 parallel Tasks)
           │   ├─ Wave 1: All passes × Tier 1 (critical) chunks
           │   ├─ Wave 2: Core passes × Tier 2 (standard) chunks
           │   └─ Wave 3: Extended passes × Tier 2 + Tier 3 chunks
           │   (Spec verification runs as single global pass with full diff)
           │
           ├─ 4-L.3: Cross-chunk synthesizer (single agent)
           │   └─ Interface mismatches, data flow, consistency, shared resources
           │   └─ Receives actual diff content at chunk boundaries
           │
           └─ 4-L.4: Final judge (full adversarial validation)
               └─ Same rigor as standard mode, cross-chunk root cause grouping, verdict
                │
Steps 5-7: Merge, format (+ chunk summary table), save
```

### Key Design Decisions

| Decision | Why |
|----------|-----|
| High activation thresholds (80 files / 8000 lines) | Modern models handle large contexts well. Only chunk when truly necessary to avoid information loss from context fragmentation. Raised from 30/3000 after testing showed chunking hurt quality on medium-sized diffs. |
| Directory-based clustering | Deterministic, no AI required, strong proxy for file relatedness. |
| Three-tier risk tiering (Critical / Standard / Low-risk) | Auth/payment files need deeper review than config files. Prioritizes explorer attention where risk is highest. |
| Single final judge (no chunk judges) | Chunk judges created multi-level filtering that removed real findings. A single final judge with full adversarial validation preserves recall while maintaining precision. Explorers optimize for recall, judge optimizes for precision — one gate, not three. |
| Cross-chunk synthesizer with actual diff content | Cross-file pattern detection needs its own context budget and tool access. Including actual diff at chunk boundaries (not just summaries) enables detection of type mismatches, interface changes, and data flow breaks. |
| Spec verification runs globally with full diff | Requirements span the entire changeset — fragmenting per chunk loses traceability. Full diff available via temp file enables behavioral verification, not just existence checks. |
| Wave batching (8-12 per wave) | Prevents spawning too many simultaneous sub-agents while maintaining parallelism. |
| Diff offloading to temp files | Prevents the orchestrator's own context from being consumed by a 40-80k token diff. |

### Tradeoffs

| Risk | Mitigation | Residual |
|------|-----------|----------|
| Cross-chunk bugs missed | Cross-chunk synthesizer with actual diff at boundaries + full adversarial validation in final judge | Medium: subtle interactions through unchanged code may be missed |
| Increased latency (~2-3x) | Highly parallel waves; serial overhead is only synthesizer + final judge | Acceptable: standard mode would fail on the same diff |
| Increased token cost (~2-4x) | Each explorer handles less work; without chunking, review is truncated/superficial | Acceptable tradeoff for quality |
| Final judge context pressure | All raw findings from all chunks sent to final judge | Mitigated by high activation thresholds (80/8000) limiting chunk count; judge can use tools to investigate |

---

## Future: Multi-Model Consensus (v2)

Running the same review across multiple models (e.g., Claude + Codex) and comparing their findings could significantly improve review quality. When two models independently flag the same issue, confidence is very high. When they disagree, the disagreement itself is a signal worth human attention.

Adversarial debate (where models review each other's findings and must steel-man opposing views before revising) is another promising direction — the council/vibe skills demonstrate this pattern works well.

These are areas to explore once the enriched single-model review is battle-tested.
