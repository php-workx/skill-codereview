---
name: Benchmark Learnings
description: Hard-won lessons from running OWASP and Martian benchmarks — sample bias, parsing failures, infrastructure bugs, and how to interpret results correctly
type: reference
---

## Running OWASP Benchmark

**Setup:** `eval-owasp.py setup --lang python` (1,230 tests) or `--lang java` (2,740 tests). Java is the established benchmark with published vendor scorecards.

**Critical: --limit causes sample bias.** The first N test cases are NOT representative. CWE categories cluster by test ID range. `--limit 50` gives 26 weakrand + 11 xpathi + 10 pathtraver but ZERO sqli, ldapi, trustbound, xss, cmdi. Always run the full set for definitive results, or use `--limit` only for smoke tests knowing the numbers are meaningless.

**Batch size and timeout matter.** The taint protocol prompt is longer and produces longer responses. Batch size 10 + timeout 120s caused silent timeouts → "0 results" batches → missing data. Fixed to batch 5 + timeout 300s. Watch for "0 results" in output — that means parse failure or timeout, not "no vulnerabilities."

**Three vulnerability models, not one.** Applying taint analysis to everything causes false negatives on crypto (CWE-327/328). MD5 for password storage is vulnerable regardless of whether user input reaches it. The eval prompt must use: taint analysis for injection, algorithm analysis for crypto/hash, flag analysis for cookies.

**AI overrides semgrep in merge.** The scoring merge was union (either flags → flagged). This means semgrep FPs can never be corrected. Changed to: for files the AI reviewed, trust AI verdict; for files AI didn't review, keep semgrep. This matters for cmdi (100% FPR from semgrep) and securecookie (100% FPR).

**100% recall on reviewed files ≠ 100% overall recall.** The AI had perfect recall on every file it saw, but the overall score showed 10-20% recall because most files weren't reviewed. Always check both "recall on reviewed" and "overall recall" to distinguish prompt quality from coverage.

## Running Martian Benchmark

**Setup:** `eval-martian.py setup && eval-martian.py prepare` clones 5 repos (sentry, grafana, cal.com, discourse, keycloak) and fetches PR refs.

**Default passes: correctness,reliability.** The golden set is correctness bugs. Running all experts (security, testing, maintainability) generates valid findings that don't match the golden set, artificially depressing precision from ~40% to ~10%. Set in eval-martian.py.

**findings_path must be relative.** Claude sometimes creates nested directories when given absolute paths (e.g., `.eval/repos/sentry/.eval/repos/sentry/.eval-findings-X.json`). Fixed by passing just the filename. Always check for doubled paths when debugging 0-finding PRs.

**Pass tagging is essential.** The REVIEW_PROMPT must ask for the "pass" field (correctness, security, testing, etc.). Without it, 65% of findings are untagged and can't be filtered or analyzed by category. Added to the prompt as a required field.

**2-turn vs 20-turn pattern.** The same model non-deterministically either invokes /codereview atomically (1-2 outer turns, 300-600s, often loses findings) or steps through the pipeline (15-30 turns, 60-120s, reliable). Sonnet orchestrator is more consistent than haiku. The 2-turn reviews have 80-90% non-API time (local scripts running inside one big turn).

**prompt-test command for rapid iteration.** `eval-martian.py prompt-test` runs one-shot `claude -p --max-turns 1 --tools ""` against golden bugs. Tests prompt quality in isolation. Much faster ($0.05/PR vs $0.30/PR) and showed the correctness prompt alone gets F1 38.7% vs full pipeline 15.1% — proving the pipeline adds noise, not the prompt.

## Interpreting Results

**Youden Index (OWASP):** TPR - FPR per CWE category, averaged. Range -1 to +1. Our baseline: +0.073 (semgrep only), improved to +0.133 (semgrep + AI on 150 files). Projected +0.50+ on full run based on 100% recall on reviewed files.

**F1 Score (Martian):** Harmonic mean of precision and recall. Our best: F1 16.0% (full pipeline), F1 38.7% (one-shot correctness prompt). Top of leaderboard: Cubic v2 at F1 61.8%.

**Precision is our main problem on Martian, not recall.** The pipeline generates 10.5 findings per PR vs 2.7 golden bugs. 77% of findings are non-correctness (maintainability 43%, reliability 17%, testing 11%, security 4%). These are real issues but don't match the golden set. Filtering to correctness-only would ~4x precision.

**Recall bottleneck is infrastructure, not AI quality.** 40% of missed golden bugs (50 of 125) are in PRs with 0 findings — reviews that failed (path bug, timeout, 2-turn pattern), not reviews where the AI missed bugs. Fixing infrastructure bugs has more impact than improving prompts.

**The eval measures the PROMPT, not the pipeline.** Both OWASP and Martian eval scripts use `claude -p` which runs the prompt in a Claude session. For OWASP, it's one-shot (no tools). For Martian, it's the full skill with tools. But the eval doesn't measure deterministic scan quality, context assembly quality, or judge quality independently. Each component needs its own measurement.

## Known Benchmark Limitations

- **OWASP Python is v0.1 (preliminary)** — no published vendor scorecards yet. Java v1.2 has published scores.
- **Martian golden set is narrow** — 2.7 bugs per PR average. Many valid findings count as FP.
- **Discourse PRs are commit-based** — 10 PRs map to commit SHAs, not PR numbers. pr_id format is `discourse-{sha[:8]}`.
- **sentry-5 is fork-only** — no diff available without cloning the fork repo.
- **Classify command not yet run** — would tell us how many "FPs" are actually valid findings the golden set missed. This is the key to understanding our real precision.
