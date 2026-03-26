# Detection Root Cause Analysis: CodeRabbit Gap

For each of CodeRabbit's findings, this document identifies **why our skill failed to detect it** (the detection root cause), then maps that root cause to planned features in `plan-treesitter.md` (v1.3) and `plan-verification-pipeline.md`.

**Two rounds:** Round 1 (30 findings on v1.2 implementation), Round 2 (11 findings after fix sprint + Ruby/Java feature).

---

## Detection Root Causes (clustered)

### RC1: No cross-file data flow tracing
**Findings:** #13, #19, inline critical

Our explorers review each file's diff independently. When `run_suppress()` writes `summary_snippet` but `fuzzy_match()` reads `summary`, no explorer traces the data flow across these two functions to notice the field name mismatch. Same for `run-scans.sh` reading `lint_commands` that `discover-project.py` never emits, and `find_existing_artifact()` looking in repo root while `run_tests()` writes to `cover_dir`.

**Plan coverage:**
- **v1.3 F19 (Cross-File Context Planner)** — PARTIAL. F19 adds LLM-driven search planning to find cross-file dependencies. Its "Symmetric/Counterpart Operations" category is designed for exactly this: detecting paired write/read operations where the shape changes. However, F19 enriches explorer *context* — it gives explorers more visibility into related code. It doesn't mechanically verify that JSON field names match between producer and consumer. The explorer still needs to notice the mismatch.
- **v1.3 F4 (Dependency Graph)** — PARTIAL. Provides structural cross-file relationships (imports, calls). Helps explorers see connections but doesn't verify data shape consistency.
- **Verification Pipeline F0 (Verification Round)** — NO. Verifies individual findings, doesn't detect cross-file contract issues.

**Gap:** Neither plan adds a **mechanical cross-file contract check** — a script or deterministic pass that extracts JSON field names from producers (jq queries, json.dump calls, dict construction) and consumers (json.load, jq reads, dict access) and verifies they match. F19's context enrichment helps the AI notice mismatches, but the detection is still AI-dependent and not guaranteed.

---

### RC2: No exhaustive rule enumeration (truth tables)
**Findings:** #9, #22

Finding #9: the tier assignment rules have a gap at `critical + confidence 0.65-0.79`. Finding #22: the `deferred_scope="exact"` branch is missing a `changed_files` guard that the other branches have. Both require enumerating all input combinations to a branching/rules structure and verifying each maps to the expected outcome.

**Plan coverage:**
- **v1.3 F18 (Mental Execution Framing)** — PARTIAL. F18 instructs the correctness explorer to "mentally execute" code with specific inputs. Its "Execution Contexts" include "repeated invocations" and "failure mid-operation." However, F18 doesn't explicitly instruct: "for classification/routing code with N×M input combinations, enumerate all combinations and verify each." The mental execution protocol traces individual paths, not exhaustive truth tables.
- **Verification Pipeline F0 Stage 1 (Feature Extraction)** — NO. Extracts boolean features per finding, doesn't analyze the code under review for rule completeness.

**Gap:** Neither plan adds explicit **truth table enumeration** as a correctness pass technique. F18 is the closest — it could be extended with a calibration example showing how to enumerate all severity × confidence combinations for classification code. Specifically: "When reviewing rule-based classification (if/elif chains, match/case, lookup tables), enumerate ALL valid input combinations and verify each maps to a correct output. Pay special attention to values that fall between rule boundaries."

---

### RC3: No multi-run state reasoning
**Findings:** #17, #20

Finding #17: suppressions are append-only — the first matching entry wins, so re-suppressing with a new reason/status has no effect. Finding #20: auto-discovery matches on `scope` + `base_ref` only, so any branch review against `main` can select another branch's review as "previous." Both require reasoning about how the system behaves across multiple invocations over time.

**Plan coverage:**
- **v1.3 F18 (Mental Execution)** — PARTIAL. The "Repeated invocations" execution context is relevant to #17 (what happens when you call suppress twice?). But F18 is scoped to the correctness explorer reviewing diff code, not reasoning about the operational lifecycle of the entire system.
- No other planned feature addresses this.

**Gap:** Neither plan addresses **multi-run behavioral reasoning** as a review technique. This is fundamentally hard for a diff reviewer — the diff shows code, not runtime behavior across deployments. Options:
1. Add "stateful system" as an execution context in F18's mental execution protocol: "If the code manages persistent state (files, databases, caches), simulate what happens after N operations. Does state accumulate correctly? Does matching/lookup work with accumulated state?"
2. Add targeted test expectations in the test-adequacy pass: "For code that manages persistent state, check for multi-operation tests (create + update + delete, not just create)."

---

### RC4: Bash-specific semantic knowledge
**Findings:** #1, #6, #7, #24, #29

Five distinct bash/shell knowledge gaps:
- #1: `set -e` causes script abort when jq fails on malformed input, before reaching the error summary
- #6: `grep -E '\s'` is not POSIX (BSD grep doesn't support `\s` in ERE)
- #7: commit count < 50 is a bad proxy for shallow clone detection; `git rev-parse --is-shallow-repository` exists
- #24: `set -e` exits on failed command substitution before `$?` can be captured
- #29: `< file` redirect overrides piped stdin, so the test doesn't exercise what it claims

**Plan coverage:**
- **v1.3 F1 (Scan Orchestration)** — PARTIAL for #6 only. F1 mentions shellcheck integration, which would catch non-POSIX `\s`. But F1 doesn't mention `--shell=sh` specifically, and shellcheck doesn't catch `set -e` interaction bugs (#1, #24) or stdin redirect precedence (#29).
- **v1.3 F8 (Prescan)** — NO. Prescan catches swallowed errors (P-ERR) via AST pattern matching, but `set -e` interaction is a control flow issue, not a pattern.
- **Verification Pipeline** — NO. No bash-specific verification.

**Gap:** The skill has no mechanism for **bash semantic analysis** beyond shellcheck. The specific gaps are:
1. `set -e` interaction analysis — how does `set -e` propagate through command substitutions, if-statements, and pipeline commands? This requires bash-specific knowledge that neither AI explorers nor deterministic tools currently cover.
2. Bash redirect precedence — `<` overriding pipe is a bash semantics question, not a pattern.
3. Git plumbing knowledge — knowing `git rev-parse --is-shallow-repository` exists is domain knowledge.

Mitigation: Add bash-specific calibration examples to the error-handling explorer (#1, #24) and correctness explorer (#29). For #6, ensure F1 runs `shellcheck --shell=sh`. For #7, this is domain knowledge that's hard to systematize.

---

### RC5: Security pass not calibrated for CLI/script contexts
**Findings:** #14, #26

Finding #14: `discover-project.py` reads paths from stdin and walks up directories — classic path traversal, but via CLI stdin rather than HTTP request parameters. Finding #26: `timing.sh` interpolates `$name` and `$value` directly into JSON strings — JSON injection via bash string interpolation.

**Plan coverage:**
- **v1.3 F8 (Prescan)** — NO. Prescan checks for P-SEC (hardcoded secrets) but not path traversal or injection in script stdin handling.
- **Verification Pipeline F0 (Verification Round)** — PARTIAL. If the security explorer flags the issue, the verifier would confirm it. But the security explorer needs to flag it first — it needs calibration for CLI attack surfaces.

**Gap:** The security pass's calibration examples and investigation phases focus on **web application patterns**: HTTP request parameters → SQL/command sinks, auth middleware, CORS. It doesn't cover:
1. **CLI stdin as attack surface** — paths read from stdin that escape the working directory
2. **Bash string interpolation into structured formats** — building JSON/YAML/XML via heredoc or echo with unescaped variables

Mitigation: Add calibration examples to `reviewer-security-pass.md` for both patterns. These are straightforward additions — the investigation phases (Trust Boundary Mapping, Data Flow Trace) already apply; the explorer just needs to recognize stdin and bash interpolation as trust boundaries.

---

### RC6: No prompt/workflow self-consistency review
**Findings:** #2, #3, #21, #23

Finding #2: judge prompt's partial-downgrade rule is overbroad (downgrades unrelated requirements in same file). Finding #3: Calibrator can mint new findings that skip Gatekeeper/Verifier, contradicting the rule that all findings must survive all phases. Finding #21: SKILL.md references git-risk and coverage data before the steps that generate them. Finding #23: SKILL.md says timing must never block the review but also says to run timing.sh unconditionally.

**Plan coverage:**
- **Verification Pipeline F1 (Two-Pass Judge)** — NO. Restructures the judge into verify-then-synthesize but doesn't add self-consistency checking of the prompt's own rules.
- No other planned feature addresses this.

**Gap:** **Prompt and workflow documents are not reviewed as code.** The skill treats prompts as configuration/instructions, not as logic that can contain contradictions. This is a meta-level gap — the review skill doesn't review itself.

This is genuinely hard to close via the review skill itself (circular dependency). Options:
1. Accept this as out of scope — prompt quality is maintained by human review during skill development.
2. Add a **workflow dependency validator** (script) for SKILL.md that parses step numbers and verifies each step's referenced inputs exist in earlier steps.
3. For prompt logic (#2, #3), add internal consistency as a review criterion when the diff modifies prompt files.

---

### RC7: Test assertion quality not reviewed
**Findings:** #4, #28, #29

Finding #4: fixture marks `parse function` vs `render function` as expected fuzzy match — the differing word is the semantically important one. Finding #28: validator loop uses `-ge 1` instead of `-eq 1`, accepting fixtures that trip multiple unrelated failures. Finding #29: stdin redirect overrides pipe, so the test doesn't exercise the normalizer.

**Plan coverage:**
- **v1.3 F11 (Test Pyramid Vocabulary)** — NO. F11 improves test classification (L0-L7, BF1-BF9) but doesn't review whether existing test assertions are correct.
- **v1.3 F12 (Per-File Certification)** — NO. Certification is about investigation thoroughness, not test quality.

**Gap:** The test-adequacy explorer checks for **missing tests** but not for **wrong tests** — tests whose assertions don't match their intent, tests that always pass due to tautological conditions, or tests that don't exercise what they claim due to redirection/scoping bugs.

Mitigation: Add a "Test Assertion Quality" section to `reviewer-test-adequacy-pass.md` with these patterns:
- Expected values that seem semantically wrong for the test's stated purpose
- Assertions that are tautologies (compare X to X, always-true conditions)
- Tests where the setup doesn't match the assertion (e.g., mocking the wrong thing, redirecting stdin so the function under test gets wrong input)

---

### RC8: Error handling not calibrated for script patterns
**Findings:** #1, #5, #15, #24, #27

Finding #1: `set -e` causes abort before reaching error summary. Finding #5: `chmod 2>/dev/null || true` hides real errors. Finding #15: run-scans.sh requires jq but never checks for it. Finding #24: `set -e` + command substitution. Finding #27: jq aborts on scalar entries in an array meant to contain objects.

Note: #1 and #24 overlap with RC4 (bash semantics). Listed here because the detection would come from the error-handling explorer, which needs bash-specific calibration.

**Plan coverage:**
- **v1.3 F8 (Prescan)** — PARTIAL for #5. Prescan's P-ERR pattern checks for swallowed errors (`except: pass`, empty error handlers). The bash equivalent (`|| true`, `2>/dev/null`) could be added as a prescan check. But prescan wouldn't catch #15 (missing dependency check) or #27 (jq type mismatch).
- No other planned feature addresses this.

**Gap:** The error-handling explorer's calibration examples focus on **application-level patterns**: try/catch, error returns, Result types. It needs calibration for **script-level error handling**:
1. `set -e` interaction with command substitution and pipelines
2. `|| true` and `2>/dev/null` as error swallowing patterns (distinguish intentional from harmful)
3. Missing upfront dependency validation (a script uses tool X extensively but never checks if X is installed)
4. jq type safety (jq iterating `.[]` on a value that might not be an array)

---

### RC9: Domain-specific tool knowledge
**Findings:** #7, #8

Finding #7: should use `git rev-parse --is-shallow-repository` instead of commit count heuristic. Finding #8: `c8 report` and `nyc report` only render reports from existing data — they don't run tests (need `c8 run` and `nyc <test-command>`).

**Plan coverage:** No planned feature addresses tool-specific behavioral knowledge.

**Gap:** These require knowing what specific CLI tools do — that `c8 report` != `c8 run`, or that `git rev-parse --is-shallow-repository` exists. This is **domain knowledge** that's hard to systematize via calibration examples. AI models may or may not know it depending on training data.

Mitigation options:
1. Add tool-specific correctness checks to the test suite (integration tests that actually run the commands and verify they produce expected output).
2. For coverage-collect.py specifically, the inline critical finding about artifact path mismatch would be caught by RC1 (cross-file data flow tracing) if implemented.
3. Accept this as a residual risk — AI knowledge of specific tool behaviors is inherently incomplete.

---

### RC10: No identifier/entropy analysis
**Finding:** #10

ID format `<pass>-<4 hex chars>-<line>` has only 16 bits of file-path entropy. Different files can collide at ~256 files, and two findings on the same file/line/pass always collide.

**Plan coverage:** No planned feature addresses this.

**Gap:** The correctness pass doesn't reason about **hash collision probability** or **identifier uniqueness guarantees**. This is a specific pattern that could be added as a calibration example: "When reviewing identifier generation code, check the entropy/uniqueness of the ID scheme. Flag short hashes (< 8 hex chars) and IDs that don't include enough distinguishing fields."

---

### RC11: No schema self-consistency checking
**Findings:** #11, #12

Finding #11: `suppressed_findings` items don't enforce the same required fields as regular findings. Finding #12: `tool_status.status` enum doesn't include `timeout` which `classify_status()` produces.

**Plan coverage:** No planned feature addresses mechanical schema-vs-code consistency.

**Gap:** This is a **deterministic check**, not an AI detection issue. A script could extract all string values that flow into a schema-constrained field (by tracing the code) and verify they're in the schema enum. Options:
1. Add a **schema conformance test** to the test suite: run each script with known input and validate output against `findings-schema.json` using a JSON Schema validator.
2. Add a schema consistency check to the API/contract explorer's prompt: "When reviewing code that produces JSON conforming to a schema file, verify all produced values are in the schema's enum/allowed sets."

---

### RC12: No cross-tool scoping consistency analysis
**Finding:** #16

`ruff` runs on `"${FILES[@]}"` (changed files only) but `cargo clippy` and `golangci-lint` run on `./...` (entire workspace). Stale findings from untouched files leak into reviews.

**Plan coverage:**
- **v1.3 F1** — PARTIAL. F1's spec says "runs available tools on changed files" but doesn't specify per-tool scoping strategy. The implementation inconsistency is exactly the kind of thing F1 was meant to prevent by centralizing tool execution.

**Gap:** The correctness pass doesn't **compare equivalent operations across a file** for consistency. When multiple tool invocations follow the same pattern (run tool → normalize → record status), the explorer reviews each independently but doesn't compare scoping strategy across them. A calibration example could address this: "When reviewing code that performs the same operation for multiple items (tools, files, users), verify the operation is consistent across all items."

---

## Summary: Coverage Matrix

| Root Cause | Findings | v1.3 Feature | VP Feature | Status |
|-----------|----------|-------------|------------|--------|
| RC1: Cross-file data flow | #13, #19, inline | F4, F19 (partial) | — | **PARTIAL** — context enrichment helps, no mechanical check |
| RC2: Truth table enumeration | #9, #22 | F18 (partial) | — | **PARTIAL** — F18 traces paths but doesn't mandate exhaustive enumeration |
| RC3: Multi-run state | #17, #20 | — | — | **GAP** |
| RC4: Bash semantics | #1, #6, #7, #24, #29 | F1 (partial, #6 only) | — | **MOSTLY GAP** — shellcheck covers #6; rest uncovered |
| RC5: CLI/script security | #14, #26 | — | F0 (partial) | **GAP** — needs security pass calibration |
| RC6: Prompt/workflow consistency | #2, #3, #21, #23 | — | — | **GAP** |
| RC7: Test assertion quality | #4, #28, #29 | — | — | **GAP** |
| RC8: Script error handling | #1, #5, #15, #24, #27 | F8 (partial, #5 only) | — | **MOSTLY GAP** — prescan catches swallowed errors; rest uncovered |
| RC9: Domain tool knowledge | #7, #8 | — | — | **GAP** (hard to close) |
| RC10: Identifier entropy | #10 | — | — | **GAP** (minor) |
| RC11: Schema consistency | #11, #12 | — | — | **GAP** — needs deterministic test |
| RC12: Cross-tool scoping | #16 | F1 (partial) | — | **PARTIAL** |

### Fully covered: 0 root causes
### Partially covered: 4 root causes (RC1, RC2, RC4, RC12)
### Gaps: 8 root causes (RC3, RC5, RC6, RC7, RC8, RC9, RC10, RC11)

---

## Recommendations for Plan Updates

### High-impact additions (close multiple gaps)

1. **Extend F18 (Mental Execution) with truth table enumeration and multi-run state** — Closes RC2 and RC3. Add two sections to the correctness prompt:
   - "For classification/routing code: enumerate all input combinations"
   - "For stateful systems: simulate N operations and check accumulation"

2. **Add script-specific calibration to error-handling and security passes** — Closes RC5 and RC8. Add calibration examples for:
   - `set -e` interaction patterns (error handling pass)
   - `|| true` / `2>/dev/null` as error swallowing (error handling pass)
   - Missing dependency checks (error handling pass)
   - CLI stdin as attack surface (security pass)
   - Bash string interpolation into JSON/structured formats (security pass)

3. **Add test assertion quality to test-adequacy pass** — Closes RC7. New prompt section:
   - Wrong expected values (semantic mismatch with test name)
   - Tautological assertions
   - Setup that doesn't match assertion (redirect/mock scoping bugs)

### Medium-impact additions

4. **Add schema conformance tests to test suite** — Closes RC11. Not an AI improvement — a deterministic test that validates script outputs against `findings-schema.json`.

5. **Ensure F1 runs `shellcheck --shell=sh`** — Strengthens RC4 coverage for #6. Small config change.

6. **Add cross-tool consistency as correctness calibration** — Addresses RC12. "When multiple operations follow the same pattern, verify consistency across all instances."

### Accepted gaps (hard to close via review skill)

7. **RC6 (Prompt self-consistency)** — Meta-level. Review skill can't review its own prompts during a code review. Best handled by human review during skill development, or a dedicated workflow validator script for SKILL.md step dependencies.

8. **RC9 (Domain tool knowledge)** — Inherent limitation of AI. Specific tool behaviors (c8 run vs report, git rev-parse flags) depend on training data coverage. Best addressed by integration tests that exercise actual tool commands.

9. **RC1 (Cross-file data flow)** — F19 partially addresses. Full mechanical coverage would require a dedicated contract-checking script that's out of scope for the current plans but could be added as a future feature.

---

## Round 2: Post-Fix Sprint + Ruby/Java Feature (2026-03-26)

After pushing 13 commits (8 waves of CodeRabbit fixes + 4 waves of Ruby/Java feature), CodeRabbit found 11 new issues: 2 critical, 6 major, 3 minor.

### Round 2 Findings

| # | File | Severity | Summary | Root Cause |
|---|------|----------|---------|------------|
| R2-1 | research-multi-model-council.md:116 | Minor | Local `~/workspaces/` paths in docs | Doc quality |
| R2-2 | complexity.sh:301 | Major | PMD results not filtered to JAVA_FILES | RC5 (fail-open edge case) |
| R2-3 | coverage-collect.py:114 | Major | `cargo llvm-cov --json` emits LLVM JSON, parser expects LCOV | RC1 + RC9 |
| R2-4 | coverage-collect.py:820 | Major | Partial/timeout status collapsed to `ran` | RC1 (intra-file contract) |
| R2-5 | discover-project.py:702 | Minor | Escaped paths still recorded in context despite warning | RC5 (fail-open) |
| R2-6 | lifecycle.py:430 | Major | Deferred suppressions permanent when `--changed-files` omitted | RC5 + RC3 |
| R2-7 | run-scans.sh:123 | Major | `check_normalized()` uses `jq` without `-e` flag | RC4 (bash semantics) |
| R2-8 | run-scans.sh:749 | Major | TRIVY_TARGETS as string breaks filenames with spaces | RC4 (bash array) |
| R2-9 | run-scans.sh:1220 | Critical | Profile commands executed via `bash -c` — command injection | RC5 (CLI security) |
| R2-10 | run-scans.sh:1446 | Major | `_timing` shape doesn't match schema | RC1 + RC11 |
| R2-11 | tests/test-scripts.sh:282 | Minor | `partial` missing from test status enum | RC11 |

### Key Insight: Same 5 Root Causes Across Both Rounds

| Root Cause | Round 1 Count | Round 2 Count | Persistent? |
|-----------|--------------|--------------|-------------|
| RC1: Cross-file contracts | 3 | 3 (R2-3, R2-4, R2-10) | **YES** |
| RC5: CLI security | 2 | 1 (R2-9) | **YES** |
| RC4: Bash semantics | 5 | 2 (R2-7, R2-8) | **YES** |
| RC5/RC3: Fail-open edge cases | 4 | 3 (R2-2, R2-5, R2-6) | **YES** |
| RC9: Tool knowledge | 2 | 1 (R2-3) | **YES** |
| RC11: Schema consistency | 2 | 2 (R2-10, R2-11) | **YES** |

**No new root causes appeared in Round 2.** The gaps are structural, not incidental. Fixing the 5 structural gaps would prevent the majority of findings in both rounds.

### Round 2 Detection Assessment

**Would the v1.3 plans catch these?**

| Finding | v1.3 Coverage | Verdict |
|---------|--------------|---------|
| R2-2 (PMD not filtered) | F18 mental execution (partial) | Would need "apply this pattern consistently" calibration |
| R2-3 (llvm-cov format) | F19 cross-file context (partial) + web search needed | NO — requires tool-specific knowledge |
| R2-4 (partial→ran) | F18 + F19 (partial) | MAYBE — intra-file but 700 lines apart |
| R2-5 (guard without skip) | F18 mental execution (partial) | MAYBE — if "what does the caller do?" is traced |
| R2-6 (empty changed_files) | F18 mental execution (partial) | MAYBE — if "what if this is empty?" is asked |
| R2-7 (jq -e flag) | — | NO — requires jq-specific knowledge |
| R2-8 (array vs string) | shellcheck (partial) | YES if shellcheck runs (SC2086) |
| R2-9 (command injection) | Security pass + calibration | YES if CLI attack surface calibration added |
| R2-10 (timing shape) | Schema conformance test | YES if deterministic schema test added |
| R2-11 (missing enum) | Schema conformance test | YES if test covers all enum values |

### Updated Recommendations (Post-Round 2)

The prioritized improvement list from Round 1 stands, with these refinements:

1. **Cross-file contract checker** — now confirmed as catching 6 findings across 2 rounds (highest ROI)
2. **Default-value analysis** — add to F18: "For every function parameter, ask: what if this is empty/None/missing?" — catches 7 findings across 2 rounds
3. **CLI security calibration** — "data from repo files → shell execution" — catches 3 findings
4. **shellcheck --shell=sh** on all scripts — catches 4 findings
5. **Schema conformance test** — catches 4 findings
