# Handoff: CodeRabbit Gap Analysis — What We Missed and Why

**Date:** 2026-03-26
**Branch:** `feat/deterministic-pipeline` (PR #2)
**Commit:** `f120a3a08d16005d58988a3b2a91ecab2c29b2b9`
**PR URL:** https://github.com/php-workx/skill-codereview/pull/2
**Source:** CodeRabbit automated review on PR #2
**Purpose:** Detailed analysis of 30 findings CodeRabbit caught that our own code review skill did not. Use this to identify skill improvements, additional checks, and process changes.

To restore this exact state: `git checkout f120a3a08d16005d58988a3b2a91ecab2c29b2b9`

---

## Executive Summary

CodeRabbit reviewed our PR #2 (18 commits, 33 files, 7,911 lines) and found 30 issues: 1 critical, 24 major, 4 minor, 1 nitpick. Our own review pipeline (vibe check + manual bug hunt) caught 4 bugs before the PR — CodeRabbit found 30 more.

The findings cluster into 7 categories. The most significant gap is **cross-file contract verification** — cases where one script produces output in a shape that another script doesn't expect. Our explorers review files individually and miss these interface mismatches. The second biggest gap is **portability and platform-specific bugs** that require knowledge our AI passes don't have.

### Findings by category

| Category | Count | Findings | Our skill's current capability |
|----------|-------|----------|-------------------------------|
| Logic bugs in scripts | 8 | #8, #9, #13, #16, #17, #19, #20, #22 | Partial — correctness pass could catch some, but requires deep script semantic understanding |
| Schema/contract mismatches | 4 | #11, #12, #13, #21 | Gap — no cross-file contract verification |
| Error handling / resilience | 5 | #1, #5, #15, #23, #27 | Partial — reliability pass exists but doesn't focus on script-specific failure modes |
| Test quality | 5 | #4, #18, #24, #28, #29 | Gap — test fixture correctness is out of scope |
| Portability/compatibility | 3 | #6, #7, #24 | Gap — no platform-specific lint |
| Security | 2 | #14, #26 | Exists — security pass should catch path traversal and injection |
| Prompt/design logic | 2 | #2, #3 | Gap — prompt instruction quality is meta-level |
| Documentation | 1 | #25 | Not in scope — markdownlint handles this |

### Actionable recommendations (prioritized)

| Priority | Improvement | Catches | Effort | Type |
|----------|------------|---------|--------|------|
| 1 | **Cross-file contract checker** — verify producer/consumer JSON shapes match across scripts | #13, #19, #21 | Medium | New script or AI pass |
| 2 | **Schema self-test** — validate script outputs against findings-schema.json with fixtures | #11, #12 | Small | Test addition |
| 3 | **shellcheck with --shell=sh** — catch non-POSIX constructs in scripts claiming Bash 3 compat | #6 | Small | Config change |
| 4 | **jq dependency check** — upfront validation in run-scans.sh | #15 | Tiny | Script fix |
| 5 | **Test isolation** — use `mktemp` instead of hardcoded `/tmp` paths | #18 | Small | Test fix |
| 6 | **Append-only data structure audit** — flag data structures that only append without dedup | #17 | Medium | Correctness pass addition |
| 7 | **Step ordering validation** — verify SKILL.md step dependencies are satisfiable | #21, #23 | Medium | New check |

---

## Detailed Gap Analysis

### Gap 1: Cross-File Contract Verification

**Findings:** #13, #19, #21

**The problem:** Our explorer passes review each file independently. When `run-scans.sh` produces JSON with field name `lint_commands` but `discover-project.py`'s documented output uses `commands`, no single-file review catches this. Similarly, `lifecycle.py` stores `summary_snippet` in suppressions but the fuzzy match function looks for `summary` — the mismatch is invisible when reviewing either function alone.

**Why our skill missed it:** Our architecture has each explorer reviewing the diff file-by-file. The cross-chunk synthesizer (large-diff mode) is designed for this but isn't implemented yet for standard mode. Even if it were, cross-file contract verification requires tracing data flow: "this function produces JSON with field X → that function reads field Y → X ≠ Y → mismatch."

**What to do:**
- Option A: Add a **contract consistency script** (`scripts/check-contracts.py`) that extracts JSON shapes from each script's output (by parsing the code or running with fixtures) and verifies consumers expect the same shape.
- Option B: Add **interface consistency** as an explicit check in the correctness explorer prompt — "when a file reads JSON produced by another script, verify the field names match."
- Option C: Add cross-file data flow tracing to the **test suite** — integration tests that pipe one script's actual output into the next script and verify it works.

**Recommendation:** Option C is the most reliable (deterministic, not AI judgment). We already have a pipeline chain test (section 15 in test-scripts.sh) but it only tests enrich → lifecycle → validate. Extend it to cover discover-project → run-scans and lifecycle suppress → lifecycle match.

### Gap 2: Schema Self-Consistency

**Findings:** #11, #12

**The problem:** Our `findings-schema.json` says `tool_status.status` is an enum of `["ran", "skipped", "failed", "not_installed", "sandbox_blocked"]`, but `run-scans.sh` also produces `"timeout"` (exit code 124). The schema rejects valid review output. Similarly, `suppressed_findings` items lack the `required` constraints that regular findings have, so malformed suppression records pass validation.

**Why our skill missed it:** The AI reviewers don't mechanically cross-reference the JSON schema against the code that produces the JSON. This is a deterministic check — compare the set of values a script can produce against the set of values the schema allows.

**What to do:**
- Add `"timeout"` and `"partial"` to the `tool_status.status` enum in the schema.
- Make `suppressed_findings` items reuse the regular finding schema (with `lifecycle_status` narrowed to `rejected|deferred`).
- Add a **schema conformance test** to the test suite: run each script with known input and validate the output against `findings-schema.json` using a JSON Schema validator.

### Gap 3: Platform Portability

**Findings:** #6, #7

**The problem:** `complexity.sh` uses `grep -E '\s'` which is not POSIX-compatible (macOS BSD grep doesn't support `\s` in ERE). `git-risk.sh` uses commit count < 50 as a shallow-clone proxy instead of `git rev-parse --is-shallow-repository`.

**Why our skill missed it:** Our AI reviewers don't have deep knowledge of BSD vs GNU grep differences or git plumbing commands. These are the kind of platform-specific gotchas that specialized linters (shellcheck with `--shell=sh`) catch better than general-purpose AI.

**What to do:**
- Run `shellcheck --shell=sh` on all bash scripts (in addition to `bash -n` syntax check). shellcheck catches `\s` usage and other non-POSIX constructs.
- Replace `git rev-list --count` shallow detection with `git rev-parse --is-shallow-repository`.
- Add a portability note to the scripts-over-prompts design principle: "All bash scripts must be POSIX-compatible for portability. Use `shellcheck --shell=sh` to verify."

### Gap 4: Test Fixture Correctness

**Findings:** #4, #18, #28, #29

**The problem:** The fuzzy-match fixture pair at lines 23-29 marks "Missing null check in parse function" vs "Missing null check in render function" as `expected_fuzzy_match: true` — but these are genuinely different findings that the deduper should NOT merge. Hard-coded `/tmp` paths make tests stateful. The Semgrep classifier test doesn't actually exercise the normalizer (stdin redirection overrides JSON pipe). The validator-check loop accepts fixtures that trip multiple unrelated FAILs.

**Why our skill missed it:** Test quality is meta — reviewing whether a test tests what it claims requires understanding the test's *intent*, not just its code. This is a hard problem for any reviewer. However, finding #4 (wrong expected_fuzzy_match) is a correctness bug that a careful reviewer should have caught.

**What to do:**
- Fix the fuzzy-match pair (#4): change `expected_fuzzy_match` to `false` for parse/render.
- Replace hardcoded `/tmp` paths with `mktemp -d` (#18).
- Fix the Semgrep classifier test (#29): pipe actual semgrep-format JSON into `normalize_semgrep`.
- Tighten the validator loop (#28): check that each fixture triggers exactly one FAIL category, not just "at least one FAIL."
- Consider adding a **test review pass** to the code review skill — an explorer that specifically checks test assertions match their descriptions.

### Gap 5: Logic Bugs Requiring Deep Understanding

**Findings:** #9, #17, #20, #22

**The problem:**
- **#9:** `critical` findings with confidence 0.65-0.79 fall to "consider" tier (they should be "should_fix"). The tier assignment rules have a gap: the first rule checks `(critical OR high) AND confidence >= 0.80`, and `critical` with 0.75 falls through to the catch-all "consider."
- **#17:** Suppressions are append-only. If you suppress a finding, then suppress it again with different status/reason, the first (stale) entry wins because matching stops at first hit.
- **#20:** Auto-discovery of previous review matches on `scope` and `base_ref` only. Every branch review against `main` lands in the same bucket — a review from branch A can be selected as "previous" for branch B.
- **#22:** `deferred_scope="exact"` doesn't check if the file is in `changed_files` — it resurfaces on any run where the fingerprint matches, even if nothing changed.

**Why our skill missed it:** These require reasoning about algorithm behavior across multiple code paths. The correctness explorer checks for bugs in individual functions but doesn't simulate the algorithm end-to-end. Finding #9 specifically is a logic error in a rules table that looks correct at first glance — you need to trace through all severity/confidence combinations to find the gap.

**What to do:**
- Fix each bug directly (these are all real).
- For prevention: add **truth table coverage** to the correctness pass calibration — when reviewing rule-based classification code, enumerate all input combinations and verify each maps to the expected output.
- Add **append-only data structure** as a pattern the correctness pass watches for — flag structures that only append without checking for duplicates or overrides.

### Gap 6: Error Handling in Scripts

**Findings:** #1, #5, #15, #23, #27

**The problem:**
- **#1:** `validate_output.sh` detects non-array `.findings` but subsequent jq queries still try to iterate `.findings[]`, crashing under `set -euo pipefail`.
- **#5:** Install script's `chmod` errors are swallowed by `2>/dev/null || true`.
- **#15:** `run-scans.sh` requires `jq` but never checks if it's installed. Missing `jq` silently fails across 30+ invocations.
- **#23:** SKILL.md tells the agent to run `timing.sh` but also says "timing must never block the review." Without an existence check, missing timing.sh aborts the review.
- **#27:** `validate_output.sh` checks if `suppressed_findings` is an array but doesn't protect against scalar entries within the array.

**Why our skill missed it:** Our reliability/error-handling explorer pass exists but its calibration examples focus on application-level error handling (try/catch, error returns), not script-level resilience (set -e interactions, jq failure modes, silent error swallowing). The pass needs calibration examples specific to bash scripts and Python CLI scripts.

**What to do:**
- Fix each issue directly.
- Add **bash resilience patterns** to the error-handling explorer calibration: `set -e` interaction with command substitution, `jq` failure modes, silent error swallowing via `2>/dev/null || true`.
- Add a **dependency check pattern** to the correctness pass: when a script uses a tool extensively, check that it validates the tool's availability upfront.

### Gap 7: Security

**Findings:** #14, #26

**The problem:**
- **#14:** `discover-project.py` resolves stdin paths and walks up the directory tree to find project roots. Absolute or `../` paths can escape the repository and classify external directories as project contexts.
- **#26:** `timing.sh` writes `$name` and `$value` directly into JSON strings. Quotes, backslashes, or newlines in either field corrupt the JSONL log.

**Why our skill missed it:** Finding #14 is a textbook path traversal — our security pass should have caught this. The fact that it didn't suggests the security pass isn't looking at CLI scripts' stdin handling as an attack surface. Finding #26 is JSON injection via string interpolation — again, something the security pass should catch but may not recognize in bash context.

**What to do:**
- Fix both issues.
- Add **CLI input validation** as a calibration example in the security pass: "When a script reads paths from stdin, check that paths are validated to stay within the expected directory."
- Add **bash string interpolation into JSON** as a calibration example: "When bash builds JSON via string interpolation, check that values are escaped/sanitized."

---

## Implementation Roadmap

### Phase 1: Fix the actual bugs (this sprint)

All 30 findings should be triaged and fixed. Group by file:

| File | Findings | Type |
|------|----------|------|
| `scripts/run-scans.sh` | #13, #15, #16 | Logic + missing dependency check |
| `scripts/lifecycle.py` | #17, #19, #20, #22 | Logic bugs in matching/discovery |
| `scripts/enrich-findings.py` | #9, #10 | Tier assignment gap + ID collisions |
| `scripts/coverage-collect.py` | #8 + inline critical | Test command config + artifact lookup |
| `scripts/discover-project.py` | #14 | Path traversal |
| `scripts/complexity.sh` | #6 | grep portability |
| `scripts/git-risk.sh` | #7 | Shallow clone detection |
| `scripts/timing.sh` | #26 | JSON injection |
| `scripts/validate_output.sh` | #1, #27 | Error handling under set -e |
| `scripts/install-codereview-skill.sh` | #5 | chmod error swallowing |
| `findings-schema.json` | #11, #12 | Missing enum values + weak suppression schema |
| `prompts/reviewer-judge.md` | #2, #3 | Partial downgrade scope + Calibrator minting |
| `SKILL.md` | #21, #23 | Step ordering + timing fallback |
| `tests/test-scripts.sh` | #18, #24, #28, #29 | Test isolation + assertion quality |
| `tests/fixtures/fuzzy-match-pairs.json` | #4 | Wrong expected match |
| `references/report-template.md` | #30 | Lifecycle label format |
| `docs/research-multi-model-council.md` | #25 | Markdown fence languages |

### Phase 2: Improve the codereview skill (next sprint)

| Improvement | Prevents | Files to modify |
|-------------|----------|----------------|
| Add cross-file contract tests to test suite | #13, #19, #21 | `tests/test-scripts.sh` |
| Schema conformance test (script output vs findings-schema.json) | #11, #12 | `tests/test-scripts.sh` |
| `shellcheck --shell=sh` in validate script and CI | #6 | `package.json`, `scripts/run-scans.sh` |
| Correctness pass: truth table coverage calibration example | #9 | `prompts/reviewer-correctness-pass.md` |
| Correctness pass: append-only data structure audit pattern | #17 | `prompts/reviewer-correctness-pass.md` |
| Security pass: CLI stdin path validation calibration example | #14 | `prompts/reviewer-security-pass.md` |
| Security pass: bash JSON interpolation calibration example | #26 | `prompts/reviewer-security-pass.md` |
| Error handling pass: bash resilience patterns (set -e, jq) | #1, #15, #23, #27 | `prompts/reviewer-error-handling-pass.md` |
| Test quality: add test review guidance to test-adequacy pass | #4, #28, #29 | `prompts/reviewer-test-adequacy-pass.md` |

### Phase 3: Structural improvements (future)

| Improvement | Prevents | Scope |
|-------------|----------|-------|
| Cross-file interface consistency pass (new explorer) | #13, #19 | New prompt file |
| Step dependency graph validator for SKILL.md | #21, #23 | New script |
| Test isolation framework (mktemp-based) | #18 | Test infrastructure |

---

## Appendix A: All 30 Findings — Full Details

### Finding 1 (Major) — validate_output.sh:113-127

**Summary:** Normalize `findings` to an empty array after the type check fails.

**Detail:** The type guard at lines 113-120 detects when `findings` is not an array and increments `ERRORS`, but lines 125+ contain multiple jq operations that iterate over `.findings[]`. With `set -euo pipefail`, malformed input causes the first such jq command to fail with exit code 5, aborting the script before it reaches the consolidated `RESULT: FAIL` message. Either normalize `findings` to `[]` when the type check fails, or conditionally skip all per-finding validation blocks.

**Affected operations:** Line 125 `BAD_FINDING_COUNT`, Line 131 `MISSING_DETAIL`, Line 138 `BAD_SOURCES`, Lines 143/148/153/158/163 (confidence, evidence, severity, source, pass filters).

**Category:** Error handling — `set -e` interaction with jq failure modes.

**Why we missed it:** Our error-handling pass doesn't have calibration for bash `set -e` interaction with subsequent commands that depend on a failed precondition.

---

### Finding 2 (Major) — reviewer-judge.md:184-189

**Summary:** Scope `partial` requirement downgrades to bugs that actually affect that requirement.

**Detail:** Line 187 currently treats any bug in the same file as evidence that the requirement is only partially implemented. In multi-requirement files, this incorrectly downgrades unrelated requirements. Should only mark `partial` when the bug overlaps the requirement's `impl_evidence` or demonstrates behavior that violates that specific requirement.

**Category:** Prompt logic — overbroad downgrade rule.

**Why we missed it:** This is a prompt quality issue — our AI reviewers don't review other AI prompts for logical flaws.

---

### Finding 3 (Major) — reviewer-judge.md:135-149

**Summary:** Don't let the Calibrator mint new findings after verification.

**Detail:** Lines 139 and 147 tell the Calibrator to create new test-gap/contradiction findings, but Line 282 says only findings that survived all expert phases may be emitted. Anything introduced by the Calibrator skips Gatekeeper and Verifier, so the final report can contain issues that were never adversarially checked.

**Category:** Prompt logic — inconsistent rules between expert phases.

**Why we missed it:** Same as #2 — prompt self-consistency review is not in our skill's scope.

---

### Finding 4 (Major) — tests/fixtures/fuzzy-match-pairs.json:23-29

**Summary:** Don't teach the deduper that different symbols are duplicates.

**Detail:** `parse function` and `render function` are separate findings. Marking this pair as `expected_fuzzy_match: true` will hide one of them whenever the summary template is similar. The 80% word overlap (4/5 words match) is coincidental — the differing word (`parse` vs `render`) is the semantically important one.

**Category:** Test fixture correctness — wrong expected value.

**Why we missed it:** Test review is not in our skill's scope. A "test adequacy" pass that also reviews test assertion correctness could catch this.

---

### Finding 5 (Major) — scripts/install-codereview-skill.sh:50-53

**Summary:** Don't swallow `chmod` failures during install.

**Detail:** `2>/dev/null || true` hides real permission errors as well as empty globs. The installer can report success while copied scripts remain non-executable.

**Category:** Error handling — silent error swallowing.

**Why we missed it:** Our error-handling pass should flag `|| true` patterns, but may not prioritize install scripts.

---

### Finding 6 (Major) — skills/codereview/scripts/complexity.sh:93

**Summary:** The radon matcher regex isn't portable to macOS grep.

**Detail:** `grep -E` with `\s` is not supported in BSD/macOS grep. POSIX ERE does not define `\s`. Use portable POSIX character class `[[:space:]]` instead.

**Category:** Portability — BSD vs GNU grep.

**Why we missed it:** No platform portability knowledge in our AI passes. shellcheck with `--shell=sh` would catch this.

---

### Finding 7 (Major) — skills/codereview/scripts/git-risk.sh:51-55

**Summary:** Use Git's real shallow-repository detection command.

**Detail:** Commit count < 50 is not a proxy for shallow clones. `git rev-parse --is-shallow-repository` (Git 2.15+) returns `true`/`false` directly, avoiding false positives (small repos) and false negatives (deep shallow clones).

**Category:** Portability — using the wrong git command for the task.

**Why we missed it:** Requires knowing that `git rev-parse --is-shallow-repository` exists. AI reviewers' knowledge of git plumbing is uneven.

---

### Finding 8 (Major) — skills/codereview/scripts/coverage-collect.py:75-79

**Summary:** Fix TypeScript coverage commands to actually execute tests, not just report.

**Detail:** `c8 report` and `nyc report` only render reports from pre-existing coverage data; they don't run tests. On a clean checkout without existing coverage data, these will fail. Only `jest --coverage` actually executes tests. The test commands for `c8` and `nyc` need to be `c8 run <test-command>` and `nyc <test-command>`.

**Category:** Logic bug — commands don't do what the comments say.

**Why we missed it:** Requires knowing the specific behavior of `c8 report` vs `c8 run`. Domain-specific tool knowledge that AI reviewers don't reliably have.

---

### Finding 9 (Major) — skills/codereview/scripts/enrich-findings.py:98-119

**Summary:** Don't demote mid-confidence `critical` findings to `consider`.

**Detail:** Tier assignment rules: Must Fix = `(critical OR high) AND confidence >= 0.80`, Should Fix = `medium severity, OR high with confidence 0.65-0.79`. A `critical` finding with confidence 0.75 doesn't match either rule and falls to `consider`. This is worse than a `medium` finding with the same confidence (which gets `should_fix`).

**Category:** Logic bug — gap in rules table.

**Why we missed it:** Requires enumerating all severity × confidence combinations and verifying each maps correctly. Our correctness pass doesn't do truth table analysis on rule-based classification code.

---

### Finding 10 (Major) — skills/codereview/scripts/enrich-findings.py:55-65

**Summary:** Make the generated finding IDs collision-resistant.

**Detail:** Current ID shape `<pass>-<4 hex file hash>-<line>` has collisions: two different findings on same file/line/pass share an ID. Different files can collide on 16-bit hash (4 hex = 16 bits = collision at ~256 files). Should use more bits and include more fields.

**Category:** Logic bug — insufficient entropy in identifier.

**Why we missed it:** Requires reasoning about hash collision probability. Our correctness pass doesn't flag short hash identifiers as collision risks.

---

### Finding 11 (Major) — skills/codereview/findings-schema.json:302-327

**Summary:** `suppressed_findings` no longer enforces the finding contract.

**Detail:** Schema drops core `required` set and most field definitions for suppressed findings, allowing malformed suppression records to validate. Should reuse regular finding schema and narrow only `lifecycle_status` to `rejected|deferred`.

**Category:** Schema/contract mismatch — weak validation.

**Why we missed it:** No deterministic check that compares schema constraints between `findings` and `suppressed_findings`.

---

### Finding 12 (Major) — skills/codereview/findings-schema.json:193-200

**Summary:** Add `timeout` to the `tool_status.status` enum.

**Detail:** `run-scans.sh` records `"timeout"` when a tool exits with code 124, but the schema enum only allows `["ran", "skipped", "failed", "not_installed", "sandbox_blocked"]`. Valid reviews fail schema validation.

**Category:** Schema/contract mismatch — missing enum value.

**Why we missed it:** No deterministic check that the schema enum covers all values the scripts can produce.

---

### Finding 13 (Major) — skills/codereview/scripts/run-scans.sh:986-998

**Summary:** `--project-profile` is being parsed in a shape the documented interpreter never emits.

**Detail:** jq looks for `.contexts[].lint_commands[]` but the documented format (in plan and SKILL.md) uses a `commands` object with `cmd` fields. Tier 3 project-configured commands are silently disabled because the jq path doesn't match the actual JSON shape.

**Category:** Cross-file contract mismatch — producer and consumer disagree on field names.

**Why we missed it:** Single-file review can't catch this — requires comparing the JSON shape in `discover-project.py` output against the jq query in `run-scans.sh`.

---

### Finding 14 (Major) — skills/codereview/scripts/discover-project.py:110-139

**Summary:** Reject paths that resolve outside the checkout before walking upward.

**Detail:** The script resolves stdin paths and walks up directories to find project roots. Absolute paths or `../` can escape the repository root and classify external directories as project contexts, potentially leaking information about the host filesystem structure.

**Category:** Security — path traversal via stdin input.

**Why we missed it:** Our security pass should catch path traversal but may not be calibrated for CLI scripts that read paths from stdin (vs web request paths).

---

### Finding 15 (Major) — skills/codereview/scripts/run-scans.sh:16-18

**Summary:** Abort before doing work when `jq` is unavailable.

**Detail:** Script documents `jq` as a hard dependency but contains no upfront validation. Missing `jq` silently fails across 30+ invocations while appearing successful, producing no useful output.

**Category:** Error handling — missing dependency check.

**Why we missed it:** Our error-handling pass doesn't have a "check dependencies upfront" pattern. It focuses on error handling within functions, not script-level prerequisites.

---

### Finding 16 (Major) — skills/codereview/scripts/run-scans.sh:768-810

**Summary:** Repo-wide linters leak stale findings from untouched files into reviews.

**Detail:** `cargo clippy` and `golangci-lint run ./...` scan the entire workspace, unlike `ruff` which uses `"${FILES[@]}"`. Stale warnings from untouched files appear as findings in the review, creating noise.

**Category:** Logic bug — inconsistent scoping across Tier 2 tools.

**Why we missed it:** Requires comparing how each tool is scoped. `ruff` uses `${FILES[@]}` but `clippy` and `golangci-lint` use `./...`. The inconsistency is only visible by reading all tool invocations side by side.

---

### Finding 17 (Major) — skills/codereview/scripts/lifecycle.py:356-361

**Summary:** Later suppression edits are shadowed by the oldest matching entry.

**Detail:** Matching stops at the first fingerprint hit, but new suppressions are always appended. If the same finding is suppressed again with new status/reason/expiry, the stale entry continues to win forever.

**Category:** Logic bug — append-only data structure without override semantics.

**Why we missed it:** Requires reasoning about the lifecycle of an append-only data structure across multiple operations. Our correctness pass doesn't flag append-only patterns.

---

### Finding 18 (Major) — tests/test-scripts.sh:510-512

**Summary:** Hard-coded `/tmp` paths make the suite stateful across runs.

**Detail:** Tests assume paths like `/tmp/nonexistent-suppressions.json` are absent. A leftover file from a previous run changes test behavior. This pattern appears throughout the test harness, undermining the deterministic pipeline goal.

**Category:** Test quality — stateful tests.

**Why we missed it:** Test isolation is not in our review scope. Should be caught by a test quality pass.

---

### Finding 19 (Major) — skills/codereview/scripts/lifecycle.py:365-366

**Summary:** Generated suppressions can never hit the fuzzy-match fallback.

**Detail:** `apply_suppressions()` passes raw suppression records into `fuzzy_match()`, but `run_suppress()` persists `summary_snippet` (first ~80 chars), not `summary`. The fuzzy matcher normalizes the `summary` field which doesn't exist on suppression records, so fuzzy matching always returns `False`.

**Category:** Cross-file contract mismatch — field name discrepancy between writer and reader.

**Why we missed it:** Same as #13 — requires tracing data flow across functions to see that the field name changes between persist and match.

---

### Finding 20 (Major) — skills/codereview/scripts/lifecycle.py:223-247

**Summary:** Auto-discovery can select the wrong prior review.

**Detail:** Candidates are grouped only by `scope` and `base_ref`. Every branch review against `main` lands in the same bucket. A recent artifact from another branch can be selected as "previous" for the current branch, falsely marking findings as `recurring`.

**Category:** Logic bug — insufficient scoping of prior review selection.

**Why we missed it:** Requires understanding the lifecycle across multiple review runs and branches. Single-run review doesn't catch multi-run interaction bugs.

---

### Finding 21 (Major) — skills/codereview/SKILL.md:243

**Summary:** `git-risk` and coverage are referenced before the workflow generates them.

**Detail:** Step 1.5c uses `scripts/git-risk.sh` output for Tier 1 file classification, and Step 2h says the context packet includes git-risk/coverage data, but these artifacts are created in Steps 2i/2j. The step ordering in SKILL.md is not satisfiable as written.

**Category:** Documentation logic — step dependency violation.

**Why we missed it:** Requires reading the entire SKILL.md workflow as a dependency graph and verifying each step's inputs are available. Our review doesn't do workflow-level dependency analysis.

---

### Finding 22 (Major) — skills/codereview/scripts/lifecycle.py:412-415

**Summary:** `deferred_scope="exact"` resurfaces immediately on an unchanged file.

**Detail:** The `exact` scope branch ignores `changed_files` — any exact fingerprint match makes `should_resurface = True` on the next run. This makes exact deferral useless unless the finding's summary changes. Should still require the file to be in `changed_files`.

**Category:** Logic bug — missing guard condition in deferred scope.

**Why we missed it:** Requires tracing through the three `deferred_scope` branches and verifying each behaves as documented. The `exact` branch is the edge case.

---

### Finding 23 (Major) — skills/codereview/SKILL.md:70-109

**Summary:** The timing workflow still fails hard when `timing.sh` is unavailable.

**Detail:** SKILL.md tells the agent to run `bash scripts/timing.sh ...` unconditionally, but also says "timing must never block the review." Without an existence check or no-op wrapper, following the instructions literally aborts the review when `timing.sh` is missing.

**Category:** Documentation logic — contradictory instructions.

**Why we missed it:** Prompt self-consistency checking is not in our review scope.

---

### Finding 24 (Major) — tests/test-scripts.sh:9

**Summary:** `set -e` exits the harness before capturing command substitution failures.

**Detail:** Patterns like `ENRICH_TRUNC=$(python3 ...)` followed by `ENRICH_TRUNC_RC=$?` cannot work with `set -euo pipefail`. When python3 fails, `set -e` terminates the test script at the command substitution line, never reaching `$?`. Need `|| true` or `|| ENRICH_TRUNC_RC=$?` on the same line.

**Category:** Portability — bash `set -e` semantics in command substitution.

**Why we missed it:** We actually encountered and fixed this exact pattern earlier (sections 6b and 7 integration test), but the test hardening agent introduced new instances of the same anti-pattern.

---

### Finding 25 (Minor) — docs/research-multi-model-council.md:126-132

**Summary:** Add language identifiers to the new fenced blocks.

**Detail:** markdownlint MD040 triggered on 3 fenced code blocks without language identifiers. Adding `text` keeps the document lint-clean.

**Category:** Documentation — linter compliance.

**Why we missed it:** Not in our review scope. markdownlint handles this.

---

### Finding 26 (Minor) — skills/codereview/scripts/timing.sh:49-79

**Summary:** Serialize timing events instead of interpolating raw strings.

**Detail:** Lines 55, 65, 77 write `$name` and `$value` straight into JSON strings via bash interpolation. A quote, backslash, or newline in either field corrupts the JSONL log, causing `summary` to silently fall back to zeros.

**Category:** Security — JSON injection via string interpolation.

**Why we missed it:** Our security pass should catch injection via string interpolation, but its calibration examples focus on web contexts (SQL, shell commands), not JSON construction in bash.

---

### Finding 27 (Minor) — skills/codereview/scripts/validate_output.sh:313-321

**Summary:** Guard `suppressed_findings` elements before reading `.lifecycle_status`.

**Detail:** The array-type check (lines 313-318) does not protect line 321 from scalar entries. A malformed item like `"bad-entry"` (string instead of object) makes jq abort instead of being counted as invalid.

**Category:** Error handling — incomplete input validation.

**Why we missed it:** Requires understanding jq's behavior on type mismatches within arrays.

---

### Finding 28 (Minor) — tests/test-scripts.sh:1216-1224

**Summary:** This loop doesn't enforce the specificity it describes.

**Detail:** The comment says each fixture should hit exactly one error category, but the test uses `-ge 1` which accepts fixtures that trip multiple unrelated `FAIL`s. This weakens the validator-fixture matrix as a regression check.

**Category:** Test quality — assertion doesn't match stated intent.

**Why we missed it:** Test assertion quality is not in our review scope.

---

### Finding 29 (Minor) — tests/test-scripts.sh:848-853

**Summary:** The Semgrep classifier is not actually exercised here.

**Detail:** The stdin redirection `< "$SCRIPTS/run-scans.sh"` overrides the JSON pipe input, so `normalize_semgrep` receives the bash script text (not JSON) and always falls back to `[]`. A broken classifier would pass this test.

**Category:** Test quality — test doesn't test what it claims.

**Why we missed it:** Requires understanding bash redirection precedence and recognizing that `<` overrides piped input.

---

### Finding 30 (Nitpick) — skills/codereview/references/report-template.md:55-56

**Summary:** Make lifecycle labeling explicit in the detailed finding block.

**Detail:** Use a labeled `Lifecycle` field instead of a standalone `**[NEW]**` token so it mirrors the table format and is easier to parse programmatically.

**Category:** Documentation — format consistency.

**Why we missed it:** Style nitpick, not a bug.

---

### Inline Critical Finding — skills/codereview/scripts/coverage-collect.py:60

**Summary:** `--run-tests` won't find several artifacts it just asked the tools to produce.

**Detail:** `find_existing_artifact()` only probes repo-root paths, but the test commands write into `cover_dir`. Some filenames also don't match the lookup table (`cover.json` vs `coverage.json`, `lcov.json` vs `lcov.info`). A successful test run can therefore fall through to "no coverage artifact was generated."

**Category:** Logic bug — artifact lookup doesn't match artifact production paths.

**Why we missed it:** Requires tracing the file paths through `run_tests()` (which writes to `cover_dir`) and `find_existing_artifact()` (which checks repo root), noticing they look in different places.

---

## Appendix B: CodeRabbit Detection Methods

Based on the findings, CodeRabbit appears to use these detection methods that our skill currently lacks:

| Method | Findings caught | Our equivalent |
|--------|----------------|----------------|
| **Cross-file data flow tracing** | #13, #19, inline critical | None — our explorers review files independently |
| **Enum/value completeness checking** | #12 | None — no deterministic schema-vs-code check |
| **POSIX portability analysis** | #6 | Partial — we run shellcheck but not with `--shell=sh` |
| **Rule truth table analysis** | #9 | None — correctness pass doesn't enumerate input combinations |
| **Git command knowledge** | #7 | Weak — AI has uneven git plumbing knowledge |
| **Tool-specific behavior knowledge** | #8 | Weak — AI doesn't know `c8 report` vs `c8 run` |
| **set -e interaction analysis** | #1, #24 | None — error handling pass not calibrated for bash specifics |
| **Append-only structure analysis** | #17 | None — correctness pass doesn't flag append-only patterns |
| **Multi-run state analysis** | #20, #22 | None — review is single-run, doesn't simulate cross-run behavior |
| **Prompt self-consistency** | #2, #3, #21, #23 | None — prompt quality is meta-level |
