# Acceptance Criteria

Validation criteria for the codereview skill. Not needed at runtime — use for testing and verification.

---

## Functional

| Scenario | Expected Behavior |
|----------|-------------------|
| No-diff repo | Exits cleanly: "No changes found to review" |
| Branch diff (`--base`) | Computes merge-base, reviews all commits since divergence, produces findings |
| Commit range (`--range`) | Reviews specific commit range, scope=range |
| PR mode | Fetches PR diff via `gh`, includes PR title/body in context |
| Spec provided | Loads spec, runs spec-verification explorer, produces per-requirement traceability with test category coverage, includes spec verification section in report |
| Spec with --spec-scope | Filters requirements to matching section/milestone, verifies only scoped requirements |
| No spec | Skips spec-verification pass, report omits "Spec Verification" section |
| Missing tools | Scans skipped with explicit status, AI passes still run, report notes gaps |
| Shell files in diff | shellcheck runs if installed, scoped to .sh files only |
| sonarqube skill installed | Runs `sonarqube.py scan --list-only`, findings merged as deterministic source |
| sonarqube not installed | Skipped with tool_status note, other scans and AI passes still run |
| radon/gocyclo available | Complexity scores included in context, hotspots noted in tool status |
| Standards skill installed | Language-specific rules loaded and included in explorer context |
| All tools available | Deterministic + AI findings merged, deduplicated, tiered |
| Empty findings | Valid JSON with `"findings": []`, verdict PASS, report with "No issues found" |
| Dead code detected | YAGNI findings flagged in report |
| Cadence: pre-commit | Skill can be invoked before each commit in agent workflow |
| Cadence: wave-end | Skill invoked with `--base` or `--range` after batch of implementation steps |

## Output Validation

| Check | Requirement |
|-------|-------------|
| JSON structure | `findings.json` validates against `findings-schema.json` |
| Envelope fields | `run_id`, `timestamp`, `scope`, `base_ref`, `head_ref`, `verdict`, `verdict_reason`, `strengths`, `files_reviewed`, `tool_status`, `findings`, `tier_summary`, `spec_requirements`, `review_mode` present |
| Finding fields | Every finding has `id`, `source`, `pass`, `severity`, `confidence`, `file`, `line`, `summary` |
| Confidence gating | No AI findings with `confidence < 0.65` in final output |
| Evidence gating | All `high`/`critical` findings have `failure_mode` populated |
| Action tiers | Every finding classified as Must Fix / Should Fix / Consider |
| Verdict | Report contains PASS/WARN/FAIL with reason |
| Strengths | Report contains at least 1 strength (or "No specific strengths noted") |
| Markdown report | Contains verdict, scope, tool status, strengths, tiered findings, next steps, summary |

## Policy

| Rule | Enforcement |
|------|-------------|
| Review-only | No code files modified by the skill |
| No external API calls | Uses only the active CLI model, no separate model runtime |
| Deterministic before AI | Deterministic scans always run first when tools are available |
| Comprehensive output | All findings above confidence floor are reported (no hard cap) |
| Graceful degradation | Missing tools never cause failure — always noted in tool_status |

## Large-Diff Mode (Chunked Review)

| Scenario | Expected Behavior |
|----------|-------------------|
| Diff with 79 files | Standard mode — no chunking, existing flow unchanged |
| Diff with 81 files | Large-diff mode activates, emits status message, creates chunks |
| Diff with 7999 lines (< 80 files) | Standard mode — line threshold not reached |
| Diff with 8001 lines (< 80 files) | Large-diff mode activates — line threshold reached |
| `--no-chunk` with 100 files | Standard mode forced — no chunking despite exceeding threshold |
| `--force-chunk` with 10 files | Chunked mode forced — chunking despite being below threshold |
| File clustering | Related files grouped by directory, tests paired with implementations |
| Chunk size limits | No chunk exceeds `max_chunk_files` (default 15) or `max_chunk_lines` (default 2000) |
| Tier 1 files (auth, payments) | Classified as Critical risk tier, reviewed in Wave 1 |
| Tier 3 files (tests, docs, config) | Classified as Low-risk tier, reviewed in Wave 3 |
| Cross-chunk interface detection | Import graph identifies cross-chunk dependencies, summary provided to each explorer |
| CROSS-CHUNK flag in explorer output | Findings depending on other chunks are tagged, cross-chunk synthesizer investigates |
| Cross-chunk synthesizer | Detects interface mismatches, data flow breaks, consistency violations across chunks; receives actual diff at chunk boundaries |
| Final judge (chunked) | Receives all raw explorer findings + cross-chunk findings, performs full adversarial validation (same rigor as standard mode), produces verdict |
| Spec verification (chunked) | Runs as single global pass, not chunked — receives manifest + full diff via temp file + spec |
| Report chunk summary | Report includes chunk summary table with files, lines, risk, passes, findings per chunk |
| JSON envelope (chunked) | Includes `review_mode: "chunked"`, `chunk_count`, `chunks` array (with `findings` count per chunk, no `raw_findings`/`validated_findings` split) |
| JSON envelope (standard) | Includes `review_mode: "standard"`, no `chunks` array |
| Orchestrator context protection | Full diff written to temp file, chunk diffs extracted fresh from git |
| Config overrides | `large_diff.*` settings from `.codereview.yaml` respected |

## Pipeline Scripts

| Scenario | Expected Behavior |
|----------|-------------------|
| `scripts/run-scans.sh` available | Step 3 runs the script, consumes JSON output. Agent does not re-interpret `deterministic-scans.md` |
| `scripts/run-scans.sh` not available | Agent falls back to manual tool execution per `references/deterministic-scans.md` |
| `scripts/enrich-findings.py` available | Step 5 runs the script for mechanical enrichment (ID, tier, confidence floor) |
| `scripts/enrich-findings.py` not available (python3 missing) | Agent performs Step 5 enrichment manually. Warning logged. |
| `scripts/complexity.sh` available | Step 2d runs the script, includes hotspots in context packet |
| `scripts/complexity.sh` not available | Agent runs radon/gocyclo manually or skips complexity analysis |
| `scripts/discover-project.py` available | Step 2a-1 discovers project tooling, agent interprets build files |
| `scripts/discover-project.py` not available | Project discovery skipped, `run-scans.sh` uses Tier 1 + Tier 2 only (no Tier 3 project commands) |
| Script exits non-zero | Agent logs stderr, falls back to manual execution for that step |
| Script produces invalid JSON | Agent validates with `jq`, falls back to manual execution if invalid |
| `--project-profile` with valid JSON | `run-scans.sh` executes Tier 3 project commands from profile |
| `--project-profile` with malformed JSON | `run-scans.sh` warns on stderr, skips Tier 3, continues with Tier 1 + 2 |
| Monorepo with multiple project contexts | `discover-project.py` groups files by context, agent interprets each context's build files |
| Empty CHANGED_FILES | All scripts produce valid JSON with empty findings/hotspots/contexts |

## Validation Script

```bash
bash scripts/validate_output.sh \
  --findings .agents/reviews/<latest>.json \
  --report .agents/reviews/<latest>.md
```
