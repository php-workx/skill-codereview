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

## Git History Risk Scoring

| Scenario | Expected Behavior |
|----------|-------------------|
| Git history available | `scripts/git-risk.sh` produces per-file risk JSON with churn, bug_commits, last_bug, and risk tier for each changed file |
| Shallow clone (< 50 commits) | Output includes `"shallow_clone": true` and a warning about potentially incomplete scores |
| All files low-risk | Output includes all files with `risk: "low"`, summary shows `high: 0, medium: 0`. Context packet notes: "All changed files have low historical risk" |
| File with high historical risk | File with BUG_COMMITS >= 3 or (BUG_COMMITS >= 2 and CHURN >= 10) classified as `risk: "high"` |
| Tier 2 file promoted to Tier 1 | File classified as Tier 2 (standard) by path heuristics but with `risk: "high"` from git history is promoted to Tier 1 (critical) in large-diff mode |
| Script not available | Git history risk scoring skipped, explorers still run without historical risk context |
| Script fails (non-zero exit) | Agent logs stderr, skips git history risk, continues with remaining context gathering |
| New files (no git history) | New files have churn=0, bug_commits=0, risk="low" — correct default for files with no history |
| `--months` flag | Custom lookback period used; `lookback_months` in output reflects the specified value |

## Test Coverage Data Integration

| Scenario | Expected Behavior |
|----------|-------------------|
| Go files in diff, `cover.out` exists | `scripts/coverage-collect.py` parses Go coverage, outputs per-file line coverage and uncovered functions for changed files |
| Python files in diff, `.coverage` exists | Script parses Python coverage database (via `coverage json` export), outputs per-file data for changed files |
| Rust files in diff, `tarpaulin-report.json` exists | Script parses Tarpaulin JSON report, outputs per-file data for changed files |
| TypeScript files in diff, `coverage/` directory exists | Script parses Istanbul/c8/nyc JSON coverage, outputs per-file data for changed files |
| No coverage tool installed | `tool_status` entry has `status: "not_installed"`, script does not fail |
| No existing coverage data, `run_tests: false` (default) | `tool_status` entry has `status: "skipped"` with note about setting `run_tests: true` |
| Stale coverage artifact (older than recent commits) | Warning included: "Coverage data may be stale (predates recent changes)" |
| `run_tests: true`, tests fail | Partial coverage reported if available, `tool_status: "partial"` with note about test failures |
| `run_tests: true`, test suite times out | Partial coverage reported, note about timeout |
| Multi-language repo (e.g., Go + Python) | Each language processed independently, separate `tool_status` entries (`coverage_go`, `coverage_python`) |
| Empty CHANGED_FILES | Valid JSON with empty `languages_detected`, `coverage_data`, `tool_status` |
| Only test files in CHANGED_FILES | Test files excluded from coverage output, `coverage_data` is empty |
| Script not available | Coverage collection skipped, explorers still work without measured coverage data |
| Script fails (non-zero exit) | Agent logs stderr, skips coverage data, continues with remaining context gathering |

## Finding Lifecycle & Fingerprinting

| Scenario | Expected Behavior |
|----------|-------------------|
| First review (no previous artifact) | All findings tagged as `new`. No suppressions file → no suppressions. Lifecycle features are invisible until the second review. |
| Recurring detection | Findings matching a previous review (by exact fingerprint or fuzzy match) are tagged as `recurring`. `lifecycle_summary.recurring` reflects the count. |
| Rejected suppression | Finding matching a `rejected` suppression is moved to `suppressed_findings[]` with `lifecycle_status: "rejected"`. It does not appear in `findings[]` or the report. |
| Deferred suppression (`deferred_scope: "file"`, default) | Deferred finding is suppressed unless the file is in CHANGED_FILES, in which case it resurfaces as a normal finding. |
| Deferred suppression (`deferred_scope: "pass"`) | Deferred finding resurfaces only if the file is in CHANGED_FILES AND the current finding's `pass` matches the suppression's `pass`. |
| Deferred suppression (`deferred_scope: "exact"`) | Deferred finding resurfaces only on exact fingerprint match. Fuzzy-matched findings stay suppressed. |
| Expired suppression | Suppression with `expires_at` in the past is ignored. Finding resurfaces as `new` or `recurring` depending on whether it appeared in the previous review. |
| Malformed suppressions file | Warn on stderr, skip all suppressions. Review continues normally — fail-open behavior. |
| `--raw` mode | Accepts raw judge output without enrichment fields (`action_tier`, `source`, `id`). Fingerprinting and lifecycle tagging still work. |
| Fingerprint stability | Fingerprints are 12 hex chars (SHA-256 truncated). Same finding across runs produces the same fingerprint if file, pass, severity, and normalized summary match. |
| Suppressed findings in JSON envelope | `suppressed_findings[]` array present in output with same shape as `findings[]` but `lifecycle_status` is `rejected` or `deferred`. |
| Lifecycle summary | `lifecycle_summary` object with `new`, `recurring`, `rejected`, `deferred`, `deferred_resurfaced` counts. All counts are non-negative integers. |

## Named Expert Panel (Judge)

| Scenario | Expected Behavior |
|----------|-------------------|
| Gatekeeper discards phantom knowledge finding | Finding references a function that does not exist in the codebase. Gatekeeper sets `gatekeeper_action: "discard"` with reason "References non-existent code." Finding is excluded from Verifier input. |
| Gatekeeper discards speculative concern | Finding says "might cause issues" with no concrete failure mode. Gatekeeper sets `gatekeeper_action: "discard"` with reason "Speculative — no concrete failure mode." Finding is excluded from Verifier input. |
| Verifier downgrades unverified finding | Verifier cannot conclusively confirm a finding with Read/Grep but evidence is plausible. Finding marked `verification: "unverified"`, confidence reduced by 0.15. If confidence drops below 0.65, Calibrator removes it. |
| Verifier drops disproven finding | Verifier finds a valid defense (e.g., null guard exists upstream) that contradicts the finding. Finding marked `verification: "disproven"` and dropped before Calibrator phase. |
| Calibrator merges root cause group | Three explorers report symptoms of the same underlying issue in the same function. Calibrator merges into one finding with the highest severity, combined evidence, and the most specific fix. |

## Pipeline Scripts

| Scenario | Expected Behavior |
|----------|-------------------|
| `scripts/git-risk.sh` available | Step 2i runs the script, includes per-file risk scores in context packet |
| `scripts/git-risk.sh` not available | Git history risk scoring skipped. Explorers still work without it. |
| `scripts/run-scans.sh` available | Step 3 runs the script, consumes JSON output. Agent does not re-interpret `deterministic-scans.md` |
| `scripts/run-scans.sh` not available | Agent falls back to manual tool execution per `references/deterministic-scans.md` |
| `scripts/enrich-findings.py` available | Step 5 runs the script for mechanical enrichment (ID, tier, confidence floor) |
| `scripts/enrich-findings.py` not available (python3 missing) | Agent performs Step 5 enrichment manually. Warning logged. |
| `scripts/complexity.sh` available | Step 2d runs the script, includes hotspots in context packet |
| `scripts/complexity.sh` not available | Agent runs radon/gocyclo manually or skips complexity analysis |
| `scripts/discover-project.py` available | Step 2a-1 discovers project tooling, agent interprets build files |
| `scripts/discover-project.py` not available | Project discovery skipped, `run-scans.sh` uses Tier 1 + Tier 2 only (no Tier 3 project commands) |
| `scripts/coverage-collect.py` available | Step 2j runs the script, includes per-file coverage data in context packet |
| `scripts/coverage-collect.py` not available | Coverage collection skipped. Explorers still work without measured coverage data. |
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
