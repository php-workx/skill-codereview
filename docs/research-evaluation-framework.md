# Research: Evaluation Framework for Code Review Quality

How do we measure whether our code review skill is actually good? Currently we have acceptance criteria (docs/plan-treesitter.md references) and manual testing, but no automated quality measurement. This is a blind spot — we can't objectively compare prompt changes, model upgrades, or architectural modifications.

This document collects approaches for automated review quality measurement: public benchmarks we can run against, internal evaluation frameworks we could build, and the scoring methodologies behind each.

Last updated: 2026-03-26

---

## Why We Need This

Without automated evaluation:
- Prompt changes are validated by "it looks right" — subjective and inconsistent
- We can't measure false positive rates across versions
- We can't compare model performance (sonnet vs opus vs haiku for explorers)
- We can't regression-test after architectural changes (e.g., adding the verification pipeline)
- We can't measure the impact of new features (does Feature 12 cross-file planner actually find more bugs?)
- We can't position ourselves against competitors on neutral ground

With automated evaluation:
- Every prompt change gets a coverage/validity score before merging
- Model upgrades are measured, not guessed
- Architectural changes have before/after metrics
- New features prove their value with data
- We know where we stand against tools like CodeRabbit, Qodo, Cursor Bugbot

---

## Public Benchmarks Landscape

### Tier 1: Code Review-Specific Benchmarks

These directly evaluate code review tools and are the most relevant for us.

#### Martian CodeReBench

- **URL:** https://codereview.withmartian.com/ | https://github.com/withmartian/code-review-benchmark
- **What:** The primary industry benchmark for comparing AI code review tools. Open-source, reproducible.
- **How:** Two components:
  - **Offline benchmark**: 50 curated PRs from 5 repos (Sentry/Python, Grafana/Go, Cal.com/TypeScript, Discourse/Ruby, Keycloak/Java) with human-verified "golden comments." LLM judge (Opus 4.5, Sonnet 4.5, and GPT-5.2 independently) matches tool output against golden comments.
  - **Online benchmark**: Continuously samples fresh real-world PRs from GitHub. Measures which bot comments developers actually act on (fix code after receiving the comment). Avoids data leakage.
- **Metrics:** Precision, Recall, F1 (primary)
- **Languages:** Python, Go, TypeScript, Ruby, Java
- **Last updated:** March 2026 (continuously updated online)
- **Tools tested:** 38 tools (see leaderboard below)

**Offline Leaderboard (top 20, Claude Opus 4.5 judge):**

| Rank | Tool | Precision | Recall | F1 |
|------|------|-----------|--------|-----|
| 1 | Cubic v2 | 56.3% | 68.6% | 61.8% |
| 2 | Augment | 47.5% | 61.3% | 53.5% |
| 3 | Qodo Extended Summary | 40.2% | 67.2% | 50.3% |
| 4 | Qodo v2.2 | 44.6% | 54.7% | 49.2% |
| 5 | Qodo v2 | 42.9% | 55.5% | 48.4% |
| 6 | Qodo Extended | 37.2% | 62.8% | 46.7% |
| 7 | Macroscope | 48.4% | 43.8% | 46.0% |
| 8 | Cursor Bugbot | 47.2% | 43.8% | 45.5% |
| 9 | Propel | 52.5% | 38.7% | 44.5% |
| 10 | Devin | 54.3% | 37.2% | 44.2% |
| 11 | Cubic Dev | 30.1% | 75.9% | 43.2% |
| 12 | Greptile v4 | 33.1% | 56.9% | 41.8% |
| 13 | Sourcery | 33.3% | 51.8% | 40.6% |
| 14 | Kodus v2 | 46.7% | 35.8% | 40.5% |
| 15 | Greptile | 41.5% | 39.4% | 40.4% |
| 16 | Claude Code | 34.8% | 40.9% | 37.6% |
| 17 | Qodo | 31.8% | 44.5% | 37.1% |
| 18 | GitHub Copilot | 28.3% | 53.3% | 37.0% |
| 19 | Baz | 48.8% | 29.2% | 36.5% |
| 20 | Claude | 34.8% | 35.8% | 35.3% |
| ... | | | | |
| 25 | CodeRabbit | 24.7% | 39.4% | 30.3% |
| 28 | Graphite | 100.0% | 8.8% | 16.1% |

Results consistent across all 3 judge models with same top-5 ordering.

**Online leaderboard diverges significantly.** CodeRabbit claims #1 online with F1 51.2% across ~300K real PRs (recall 53.5%, precision 49.2%), despite ranking #25 offline. This divergence likely reflects that the offline benchmark tests catching specific curated bugs in 50 PRs, while the online benchmark tests whether developers act on comments at scale — different dimensions of quality.

**Notable observations:**
- Graphite: 100% precision but 8.8% recall — rarely comments, but always right
- Cubic Dev: 75.9% recall — highest of any tool — but 30.1% precision (lots of noise)
- No tool exceeds ~76% recall on the offline benchmark — there's a ceiling
- 3 tools scored 0% (Bito, Sentry, Vercel) — didn't produce meaningful review comments

**Caveat:** Offline gold dataset was initially built using datasets from two tools (Augment and Greptile), potentially creating category bias.

#### c-CRAB (Code Review Agent Benchmark)

- **Paper:** https://arxiv.org/html/2603.23448v1 | **Code:** https://github.com/c-CRAB-Benchmark
- **What:** The most rigorous benchmark — uses executable tests as the oracle. Instead of asking "does this look like a good review?", it asks "can a coding agent successfully implement the suggestion and pass tests?"
- **How:** 184 PR instances across 67 repos, 234 validated review comments converted to executable tests (62 behavioral, 172 structural). Review tool analyzes PR → coding agent (Claude Code) applies suggestions → tests verify correctness.
- **Metrics:** Test pass rate per instance and aggregate
- **Languages:** Python (from SWE-CARE dataset)
- **Last updated:** March 2026

**Results:**

| Tool | Pass Rate |
|------|-----------|
| Human baseline | 100% |
| Claude Code | 32.1% |
| Devin | 24.8% |
| PR-Agent | 23.1% |
| Codex | 20.1% |

Shows substantial room for improvement across the industry. The executable test oracle avoids subjectivity of LLM-as-judge.

#### CR-Bench

- **Paper:** https://arxiv.org/abs/2603.11078
- **What:** Defect detection benchmark derived from SWE-bench, with a novel Signal-to-Noise Ratio (SNR) metric that directly measures developer trust.
- **How:** 584 instances (174 manually validated in CR-Bench-Verified) from real GitHub issues. CR-Evaluator classifies each review into Bug Hit, Valid Suggestion, or Noise.
- **Metrics:** Precision, Recall, F1, plus Usefulness Rate and SNR
- **Languages:** Python
- **Last updated:** March 2026

SNR is particularly relevant for us — our explorer-judge architecture explicitly optimizes for this (explorers maximize recall, judge maximizes precision = high SNR).

#### SWR-Bench

- **Paper:** https://arxiv.org/abs/2509.01494
- **What:** Full PR-level code review benchmark. 1,000 manually verified PRs from 12 Python projects (500 with issues, 500 clean).
- **Metrics:** Precision, Recall, F1 based on objective ground-truth matching (~90% agreement with human evaluators)
- **Languages:** Python
- **Key finding:** Best tool achieved only F1 of 19.38%. Multi-review aggregation improved F1 by 43.67% — suggesting that running multiple review passes and merging (our explorer pattern) is the right architectural direction.

#### CodeReviewQA

- **Dataset:** https://huggingface.co/datasets/Tomo-Melb/CodeReviewQA
- **What:** Review comprehension benchmark (not generation). 900 examples across 9 languages, formulated as multiple-choice QA.
- **Probes:** Change Type Recognition, Change Localisation, Solution Identification
- **Languages:** C, C++, C#, Go, Java, JavaScript, PHP, Python, Ruby
- **Key finding:** Top model (Llama-3.1-70B) achieved only 50.3% exact match. Measures whether models *understand* reviews, not whether they can *produce* them.

### Tier 2: Security-Focused Benchmarks

Relevant for evaluating our security explorer specifically.

#### OWASP Benchmark

- **URL:** https://owasp.org/www-project-benchmark/ | https://github.com/OWASP-Benchmark/BenchmarkJava
- **What:** Industry standard for SAST/DAST tool comparison. Java v1.2: 2,740 test cases covering 11 CWEs. Python v0.1: 1,230 test cases covering 14 CWEs.
- **Scoring:** Youden Index (TPR - FPR), normalized 0-100. Automated scorecard generation.
- **Languages:** Java, Python
- **Every major SAST tool has been scored** — CodeQL, Semgrep, Snyk, SonarQube, SpotBugs, Checkmarx, Fortify, Veracode, etc.

#### NIST Juliet / SAMATE SARD

- **URL:** https://samate.nist.gov/SARD/test-suites
- **What:** 81,000+ synthetic C/C++ and Java programs covering 181 CWEs. SARD overall: 450,000+ test cases.
- **Suitability:** Enormous scale, synthetic rather than real-world. Good for systematic CWE coverage evaluation.

#### Other security benchmarks

- **CASTLE** (2025): 250 hand-crafted C programs, 25 CWEs. Compared 13 SAST tools + 10 LLMs.
- **SecVulEval** (2025): 25,440 C/C++ function samples, 5,867 CVEs. Best LLM (Claude 3.7 Sonnet) achieved only 23.83% F1.
- **CWE-Bench-Java**: 120 CVEs spanning 4 CWEs. Manually vetted with buggy/fixed versions.
- **PrimeVul**: ~7k vulnerable + ~229k benign C/C++ functions, 140+ CWEs. Best label quality of available datasets.

### Tier 3: Bug/Defect Datasets (Source Material)

Not benchmarks per se, but useful for constructing our own evaluation:

- **Defects4J**: 854 real Java bugs across 16 projects. Gold standard. Each bug has buggy/fixed versions + triggering tests.
- **BugsInPy**: 493 real Python bugs from 17 projects. Python equivalent of Defects4J.
- **SWE-bench**: 2,294 real GitHub issues (Python). Designed for patch generation, but CR-Bench derived code review tasks from it.
- **CodeXGLUE**: 14 datasets across 10 code tasks including defect detection (Devign dataset) and code refinement (Bugs2Fix).
- **Microsoft CodeReviewer data**: https://zenodo.org/record/6900648 — Large-scale code change + review data from 9 languages.

### Tier 4: Evaluation Frameworks

Tools for building evaluation pipelines:

- **PromptFoo** (https://github.com/promptfoo/promptfoo): Open-source prompt/agent testing. CLI + CI/CD. Used by Kodus-AI. Most mature option.
- **DeepEval** (https://github.com/confident-ai/deepeval): Python-native LLM eval on pytest. 60+ metrics.
- **Braintrust** (https://www.braintrust.dev): Evals at all stages, logging, monitoring.

### Key Academic Survey

"A Survey of Code Review Benchmarks and Evaluation Practices in Pre-LLM and LLM Era" (https://arxiv.org/abs/2602.13377) — Analyzes 99 papers (58 pre-LLM, 41 LLM era). Key gaps identified: limited language coverage (mostly Java/Python), inadequate metrics (BLEU), lack of runtime evaluation, missing macro-level tasks. The field is moving from textual similarity → LLM-as-judge → executable test oracles → developer behavior signals.

---

## Kodus-AI's Internal Evaluation (Reference Implementation)

Source: local path `~/workspaces/kodus-ai`, specifically the `evals/` directory. Uses PromptFoo.

### Dataset Structure

Reference datasets contain known bugs planted in realistic code diffs:
```json
{
  "inputs": {
    "filePath": "src/path/file.ts",
    "fileContent": "full source code",
    "patchWithLinesStr": "diff with line numbers",
    "pullRequest": { "body": "PR description" }
  },
  "outputs": {
    "reference_outputs": {
      "codeSuggestions": [
        {
          "label": "bug",
          "relevantFile": "src/path/file.ts",
          "relevantLinesStart": 10,
          "relevantLinesEnd": 10,
          "existingCode": "buggy code snippet",
          "improvedCode": "fixed version",
          "suggestionContent": "detailed explanation",
          "oneSentenceSummary": "one-liner"
        }
      ]
    }
  }
}
```

Coverage: 5 languages (TypeScript, React, Python, Java, Ruby) x 2 types (single-file + cross-file) = ~130 test cases.

### Scoring Methodology

**Three-part scoring:**

1. **Parse Assertion (deterministic):** Valid JSON with correct schema. Pass/fail.

2. **Judge Assertion (LLM-based, dual judge):**
   - Both Sonnet AND GPT evaluate independently (prevents single-model bias)
   - `coverage_score = found_bugs / total_reference_bugs` (recall)
   - `validity_score = valid_suggestions / total_suggestions` (precision)
   - `judge_score = (coverage * 0.5) + (validity * 0.5)`
   - `final_score = avg(sonnet_score, gpt_score)`
   - Pass threshold: `final_score >= 0.7`

3. **Line Accuracy Assertion (deterministic):**
   - IoU (Intersection over Union) of predicted vs reference line ranges
   - Merges overlapping adjacent reference bugs (gap <= 1 line)
   - One suggestion can match multiple reference bugs
   - Metrics: `avg_iou`, `exact_match_%`, `within_3_lines_%`

**Validity criteria (aggressively strict):**
A suggestion is valid ONLY if a concrete scenario proves the issue:
- Bug: specific input -> wrong output
- Performance: realistic workload with measurable degradation
- Security: realistic attack vector with concrete consequence

**Invalid (rejected):** Cannot demonstrate concrete scenario; style/naming; "best practices" without negative consequence; vague language; defensive programming without proving reachability; duplicate suggestions.

### Specialized Evaluations

- **Safeguard evaluation:** Tests verification pipeline decisions. `action_accuracy` (60%) + `reason_quality` (40%). Threshold >= 0.7.
- **Cross-file A/B testing:** Control (no context) vs treatment (with cross-file snippets). Measures delta in coverage/validity.
- **Planner evaluation:** `symbol_coverage` (30%) + `upstream_coverage` (25%) + `fp_rate` (25%) + `category_coverage` (20%).

---

## Design Space for Our Skill

### Option A: Run against public benchmarks

**Martian CodeReBench** is the most direct path to neutral comparison:
1. We'd need an adapter that converts our findings JSON to the format their judge expects
2. Our skill reviews staged diffs, not PRs — we'd need to simulate PR context from their dataset
3. The offline benchmark is 50 PRs across 5 repos — feasible to run manually
4. Would give us an F1 score directly comparable to 38 other tools

**c-CRAB** gives the most rigorous signal:
1. Python only — limits coverage
2. Requires a coding agent to implement our suggestions — additional infrastructure
3. But the executable test oracle eliminates subjectivity entirely

**Recommended first step:** Run against Martian CodeReBench offline. It's open-source, multi-language, and has the most tools already scored.

### Option B: Build internal evaluation

Adapt Kodus-AI's PromptFoo approach for our architecture:

| Kodus Metric | Our Equivalent | Notes |
|-------------|---------------|-------|
| `coverage_score` | Recall: findings matching reference bugs / total reference bugs | Same concept, different output format |
| `validity_score` | Precision: valid findings / total findings | Need to classify findings against reference |
| `line_accuracy` | IoU of `file:line` in findings vs reference | Our findings have `file` and `line` fields |
| `parse_assertion` | Schema validation via `validate_output.sh` | Already exists |
| `dual_judge` | Two models score independently | Prevents anchoring bias |
| `safeguard_eval` | Verification pipeline accuracy | Test triage decisions against known outcomes |

**Minimum viable evaluation:**
1. **10 test cases** — 2 per language (Python, Go, TypeScript, Java, Rust), each with 2-5 planted bugs
2. **Single judge** — One model scores coverage and validity
3. **Schema check** — Validate output against findings-schema.json
4. **Runner script** — `scripts/eval.sh` runs `/codereview` on each test case and scores

### Option C: Both (recommended)

Run against Martian for external positioning. Build internal evals for development iteration. The internal evals can be faster (fewer test cases) and test specific components (individual explorers, verification pipeline, judge accuracy).

### Evaluation of specific components

| Component | How to evaluate | Priority |
|-----------|----------------|----------|
| **Individual explorers** | Give each explorer the same diff + context, score findings independently | High — identifies which passes contribute most |
| **Judge** | Give judge known-good and known-bad findings, measure filter accuracy | High — validates precision optimization |
| **Verification pipeline** | Give triage known true/false positives, measure decision accuracy | Medium — validates Feature 0 of Verification Pipeline |
| **Cross-file planner** | Planner evaluation from Kodus (symbol coverage, upstream coverage) | Medium — validates v1.3 Feature 12 |
| **End-to-end** | Full skill run on reference PRs, score final output | Required — the headline metric |

### Open questions

1. **How to create reference datasets?** Options: plant bugs manually (expensive but controlled), use known CVEs, extract from real bug-fix PRs (Defects4J/BugsInPy), or synthesize from SWE-bench like CR-Bench did.
2. **How to handle non-determinism?** Run multiple times and average? Report variance? Use temperature 0 for reproducibility?
3. **What pass threshold?** Kodus uses 0.7. Martian uses F1. c-CRAB uses test pass rate. We should track all three.
4. **Offline vs online divergence:** CodeRabbit ranks #25 offline but claims #1 online. This suggests the two dimensions (curated bug detection vs developer impact) measure different things. Which matters more for us?
5. **Speed as a metric?** CodeRabbit is known for slow reviews. Latency should be tracked alongside quality — p50, p95, and comparison to competitor speeds.

---

## References

- Martian CodeReBench — https://codereview.withmartian.com/ | https://github.com/withmartian/code-review-benchmark
- c-CRAB — https://arxiv.org/html/2603.23448v1 | https://github.com/c-CRAB-Benchmark
- CR-Bench — https://arxiv.org/abs/2603.11078
- SWR-Bench — https://arxiv.org/abs/2509.01494
- CodeReviewQA — https://huggingface.co/datasets/Tomo-Melb/CodeReviewQA | https://arxiv.org/abs/2503.16167
- OWASP Benchmark — https://owasp.org/www-project-benchmark/ | https://github.com/OWASP-Benchmark/BenchmarkJava
- NIST Juliet / SAMATE SARD — https://samate.nist.gov/SARD/test-suites
- Survey of Code Review Benchmarks — https://arxiv.org/abs/2602.13377
- Kodus-AI evals — local path `~/workspaces/kodus-ai/evals/`
- CodeRabbit benchmark blog — https://www.coderabbit.ai/blog/coderabbit-tops-martian-code-review-benchmark
- CodeAnt AI benchmark analysis — https://www.codeant.ai/blogs/ai-code-review-benchmark-results-from-200-000-real-pull-requests
- Greptile benchmarks — https://www.greptile.com/benchmarks
- Defects4J — https://github.com/rjust/defects4j
- BugsInPy — https://github.com/soarsmu/BugsInPy
- SWE-bench — https://github.com/SWE-bench/SWE-bench
- PromptFoo — https://github.com/promptfoo/promptfoo
