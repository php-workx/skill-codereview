# Research: CodeRabbit Architecture — What Makes Reviews Thorough

CodeRabbit's reviews are the most detailed and contextually rich in the industry — not because they catch the most curated bugs (they rank #25 on Martian's offline benchmark), but because they produce reviews that developers actually act on (#1 on Martian's online benchmark across ~300K real PRs). This distinction matters: thoroughness is measured by developer behavior, not toy benchmarks.

This document dissects their architecture to understand how they achieve this quality, maps each technique to our skill's architecture, and identifies what we should adopt vs skip. Speed analysis is threaded throughout — CodeRabbit's primary downside is latency (1-5 minutes typical, up to 20-30 minutes worst case), and our goal is to achieve comparable quality faster.

Last updated: 2026-03-26

Sources: CodeRabbit engineering blog (15 posts), documentation (docs.coderabbit.ai), OpenAI partnership post, Google Cloud case study, LanceDB case study, Endor Labs security analysis, FluxNinja Aperture integration post, independent benchmark analyses (Martian, Greptile, AIMultiple), community experience reports.

---

## Benchmark Performance

| Benchmark | Rank | Score | What it measures |
|-----------|------|-------|-----------------|
| Martian Offline (50 PRs, golden bugs) | **#25 of 38** | F1 30.3% (P 24.7%, R 39.4%) | Catching specific curated bugs |
| Martian Online (~300K real PRs) | **Claims #1** | F1 51.2% (P 49.2%, R 53.5%) | Developer behavior — do devs act on comments? |
| Greptile (50 bug-fix PRs) | **4th of 5** | 44% catch rate | Bug catch rate on reintroduced bugs |
| Greptile false positives | **Best** | 2 FP (vs competitors' 11) | Precision / signal-to-noise |

The offline/online divergence reveals a fundamental truth about code review quality: **catching curated golden bugs is a different skill than producing reviews that developers act on.** The offline benchmark favors tools that generate many targeted comments about specific known issues. The online benchmark favors tools whose comments are actionable, contextualized, and trustworthy enough that developers change code in response.

CodeRabbit excels at the second because of context engineering (reviews reference specific callers, dependencies, and issue tracker context), low false positive rate (verification agent filters noise), and actionable output (one-click committable fixes).

---

## Architecture Overview

CodeRabbit is a hybrid pipeline + agentic system running on Google Cloud Run.

**Infrastructure:**
- Google Cloud Run second-generation (microVM with kernel-level isolation + Linux cgroups)
- 8 vCPUs, 32 GiB RAM per instance, concurrency of 8 requests per instance
- ~200+ instances during peak hours, ~10 requests/second peak
- 3600-second timeout per review
- Jailkit inside containers for additional process confinement (belt-and-suspenders after a security incident where unsandboxed RuboCop configs allowed arbitrary code execution)
- Short-lived repository-specific tokens only — no persistent credentials in sandbox

```
Webhook (GitHub/GitLab/Azure/Bitbucket)
    │
    ▼
Billing/Auth Check (lightweight Cloud Run)
    │
    ▼
Task Queue (Google Cloud Tasks)
    │
    ▼
Execution Service (Cloud Run instance)
    │
    ├── 1. Pre-processing & Context Assembly
    │     ├── PR metadata, diff calculation, path filtering
    │     ├── Code graph traversal (CodeGraph — dynamic dependency graph)
    │     ├── Semantic index search (LanceDB — vector embeddings)
    │     ├── Issue tracker data (Jira, Linear, GitHub Issues)
    │     ├── Organization learnings database (scoped: auto/local/global)
    │     ├── Custom review instructions (path-based from .coderabbit.yaml)
    │     ├── IDE guideline files (Cursor/Copilot/Cline/Windsurf rules)
    │     └── Web queries for security updates/documentation
    │
    ├── 2. Triage (cheap model)
    │     ├── gpt-3.5-turbo classifies diffs as trivial or complex
    │     ├── Trivial changes (doc updates, variable renames) skip deep review (~50% cost savings)
    │     └── Semantic caching: compare new diffs against previous review results (~20% cost savings)
    │
    ├── 3. Static Analysis (40+ tools in sandbox)
    │     ├── Code quality: ESLint, Biome, oxlint, Ruff, Pylint, golangci-lint, Clippy, RuboCop, ...
    │     ├── Security: Semgrep, Checkov, Brakeman, Gitleaks, Betterleaks
    │     ├── Infra: Actionlint, Yamllint, ShellCheck, CircleCI validation
    │     ├── AST: ast-grep (24 languages, 150+ security rules via ast-grep-essentials)
    │     ├── Grammar: LanguageTool (30+ natural languages)
    │     └── AI-enhanced dedup: filters noise before LLM sees results
    │
    ├── 4. LLM Review (multi-model, multi-stage)
    │     ├── Parallel swarm: fan-out per file, bounded concurrency, rate-limit-aware
    │     ├── Context: 1:1 code-to-context ratio via custom diff format + assembled context
    │     ├── Frontier models (GPT-5, Claude Opus/Sonnet, O3/O4-mini) for deep review
    │     ├── Efficient models (Nemotron-3-Nano-30B, GPT-4.1) for summarization
    │     └── Model-specific prompt layers (Claude: imperative; OpenAI: structured)
    │
    ├── 5. Verification Agent (post-generation QA)
    │     ├── Reasoning model (O1/O3) articulates thinking chain
    │     ├── Generates shell/Python verification scripts (grep, ast-grep, cat)
    │     ├── Executes scripts in sandbox to extract proof from codebase
    │     ├── Self-healing: 3 attempts (implement → review → fix → re-review)
    │     └── "Comments come with receipts" — evidence attached to every finding
    │
    └── 6. Output Formatting
          ├── PR summary (optionally in PR description or walkthrough comment)
          ├── Sequence diagrams (Mermaid, for API/event/async flow changes)
          ├── Changed files summary table
          ├── Detailed per-file walkthrough
          ├── Line-level comments with severity, category, and committable fix suggestions
          └── Grouped: Potential Issue / Refactor Suggestion / Nitpick (profile-dependent)
```

**Orchestration patterns:**
- **Parallel Swarm**: Fan-out/fan-in for concurrent file reviews. Bounded concurrency ("max 10 concurrent reviews, queues the rest"). Originally hit 429 rate limits on 50+ file PRs — solved by distributing load across 4 OpenAI accounts + FluxNinja Aperture (token bucket algorithm, weighted fair queuing on separate GKE cluster).
- **Self-Healing Loop**: Validation gates check format, schema, relevance; failed validations trigger regeneration with error context (3 attempts max, then surface to human).
- **Saga Pattern**: Each forward action registers a compensating rollback; prevents partial-failure broken states.
- **Durable State Store**: Temporal-style durable execution — state checkpointed after each activity. Server crash → replay from last checkpoint. Zero-compute hibernation when waiting for human input.

---

## Deep Dive: What Makes Reviews Thorough

### 1. Context Engineering (The Moat)

CodeRabbit maintains a **1:1 ratio of code-to-context** in LLM prompts. For every line of code reviewed, they feed equivalent contextual information. This is their primary differentiator — the models are commodity; the context is not.

#### Context Source: CodeGraph

A dynamic dependency graph regenerated per review:
- Tracks **definitions, references, call chains, and co-change frequency** (files that frequently change together in commit history are treated as implicitly coupled)
- When a PR modifies file A, CodeGraph traverses to find files B, C, D that depend on A's changed symbols — enabling bug detection **outside the diff range**
- **Not purely static** — updated per review event, not just at index time

What remains undisclosed: data structure (adjacency list? property graph?), whether tree-sitter is used for symbol extraction, storage format.

#### Context Source: Semantic Index (LanceDB)

Vector embeddings enabling "search by purpose, not just keywords":
- **What is indexed**: Code structure (syntactic + semantic), issue tracker tickets, historical PRs, custom instructions, chat-based learnings — five distinct data categories
- **Scale**: "Tens of thousands of tables" — suggesting per-PR/entity tables, not monolithic
- **Update velocity**: "High velocity upserts, no downtime, no re-indexing" — near-real-time
- **Auto-updating vectors**: Developer chats and PR outcomes are re-embedded automatically ("pattern drift detection")
- **Performance**: P99 latency under 1 second across 50K+ daily PRs

What remains undisclosed: embedding model, vector dimensionality, chunk granularity (function vs file level), whether they use hybrid search (vector + full-text).

#### Context Source: Issue Tracker

Jira, Linear, GitHub Issues integration:
- Auto-links PRs to issues via description parsing and commit messages
- `assess_linked_issues` config option evaluates whether the PR actually addresses linked issues
- `related_issues` and `related_prs` surface relevant historical context
- Requirements and acceptance criteria from tickets are injected into review context

#### Context Source: Organization Learnings

Team-specific preferences accumulated from PR chat interactions:
- Scoped: **Auto** (inferred from interactions), **Local** (file/directory), **Global** (repo-wide)
- When developers correct CodeRabbit, it stores the correction to avoid repeat mistakes
- Learnings are retrieved and injected into context on future reviews
- Configurable via `knowledge_base.learnings` in `.coderabbit.yaml`

#### Context Source: Custom Instructions

Path-based review rules via `.coderabbit.yaml`:
```yaml
reviews:
  path_instructions:
    - path: "src/app/api/**/*.ts"
      instructions: "Focus on error handling and authentication"
    - path: "tests/**"
      instructions: "Check for proper assertions and coverage"
```

Also supports IDE guideline files (Cursor rules, Copilot instructions, Cline/Windsurf guidelines) — auto-detected and integrated.

#### Context Source: Web Queries

Real-time web search during review for latest security advisories and library documentation. Part of the `knowledge_base.web_search` config.

#### Custom Diff Format

They do NOT feed raw unified diffs to LLMs. They designed "new input formats, closer to how humans understand changes" with:
- Few-shot examples per model
- Duplicate identification and collapse in diffs
- Hybrid formatting: raw diffs for high-priority files, summaries for less-critical context
- Early approach (from open-source `ai-pr-reviewer`): gpt-3.5-turbo compressed large diffs into concise summaries for gpt-4 — two-stage diff compression

What remains undisclosed: the actual format specification, no examples published.

#### Gap Analysis: Context Sources

| CodeRabbit Context | Our Equivalent | Gap | Feasibility |
|-------------------|---------------|-----|-------------|
| CodeGraph (persistent dependency graph) | Callers/callees via ad-hoc Grep (per-review) + planned code_intel.py | **Major** — they traverse a graph; we search from scratch | code_intel.py functions/imports/callers could approximate this at review time |
| Semantic Index (LanceDB vectors) | None | **Major** — we can't "search by purpose" | Would require persistent infrastructure; skip for CLI tool |
| Issue Tracker | `--spec` flag (manual) | **Medium** — they auto-link; we require explicit spec | Could parse PR description for issue links automatically |
| Learnings Database | None | **Major** — no inter-review learning | CLI-feasible: `.codereview-learnings.json` accumulated from user feedback |
| Custom Instructions | `.codereview.yaml` + REVIEW.md (v1.3 Feature 13) | **Small** — similar capability | Path-based instructions could be added to .codereview.yaml |
| IDE Guidelines | None | **Small** — niche feature | Could auto-detect .cursorrules, .github/copilot-instructions.md |
| Web Queries | None (planned: Verification Pipeline Feature 7) | **Medium** | Requires internet access; opt-in |
| Custom Diff Format | Raw unified diff | **Unknown impact** | Low-cost experiment — transform diffs before feeding to explorers |

### 2. Triage Layer (Cost + Speed Optimization)

Before any expensive LLM review, a cheap model classifies diffs:

- **gpt-3.5-turbo** classifies each file's changes as "trivial or complex"
- Trivial changes (doc updates, variable renames, formatting) skip deep review entirely — **saves ~50% of costs**
- **Semantic caching**: gpt-3.5-turbo compares new diffs against previous review results to prevent regenerating identical comments on incremental commits — **saves ~20% of costs**

#### Relevance to our skill

We don't have an explicit triage step. Our adaptive pass selection (Step 3.5) skips irrelevant extended passes, but we don't skip files within a pass. Adding a file-level triage ("is this file worth deep review?") before explorer launch could significantly reduce latency on PRs with many trivial changes.

**Speed implication:** This is CodeRabbit's single most impactful speed optimization. A cheap, fast triage pass that eliminates 50% of files from deep review cuts total LLM time roughly in half.

### 3. Static Analysis Layer (40+ Tools)

**Code quality:**
- ESLint, Biome, oxlint (JS/TS)
- Ruff, Pylint, Flake8 (Python)
- golangci-lint (Go)
- Clippy (Rust)
- RuboCop (Ruby)
- PHPStan, PHPMD, PHPCS (PHP)
- SwiftLint (Swift)
- LanguageTool (grammar/spelling for 30+ natural languages)

**Security/SAST:**
- Semgrep
- Checkov (infrastructure-as-code)
- Brakeman (Ruby security)
- Gitleaks, Betterleaks (secret detection)

**Infrastructure:**
- Actionlint (GitHub Actions)
- Yamllint
- ShellCheck
- CircleCI validation, Hadolint (Dockerfiles)

**AST analysis (ast-grep-essentials):**
150+ rules across 13 languages, almost entirely security-focused:
- Hardcoded secrets/passwords for 15+ database and API clients
- JWT vulnerabilities (no-verify, none algorithm, hardcoded signing secrets)
- Weak cryptography (MD5, SHA1, RC2/4, DES, 3DES, Blowfish, ECB mode, small RSA keys)
- Injection risks (command injection, XXE, format strings)
- Insecure configurations (debug mode, SSL verify disabled, bind 0.0.0.0)
- Memory safety for C/C++ (sizeof(this), null calls, string_view temporaries, vector invalidation)
- Framework-specific (Angular SCE, Express session secrets, Flask/Django secrets, Rails force_ssl)

**AI-enhanced deduplication:** After running all tools, an AI layer filters noise and deduplicates before the LLM reviewer sees results.

#### Relevance to our skill

We run semgrep, trivy, osv-scanner, shellcheck, and pre-commit. v1.3 adds prescan (Feature 1) and code_intel.py patterns (Feature 0c) as semgrep fallback.

**The gap is breadth and ast-grep.** CodeRabbit's ast-grep rules are almost entirely security checks — hardcoded secrets, weak crypto, injection patterns. These are structural (AST-level), not regex, so they have near-zero false positives. Our tree-sitter integration (v1.3 Feature 0c) provides the AST infrastructure; we could build similar rules.

**Recommendation:** Priority additions to `run-scans.sh`:
1. **ast-grep with security rules** — highest value/effort ratio. Structural pattern matching for secrets, weak crypto, injection.
2. **Ruff** (Python) — fast, replaces Pylint+Flake8, catches real issues
3. **golangci-lint** (Go) — comprehensive Go linter suite
4. **Clippy** (Rust) — standard Rust linter

### 4. Multi-Model Strategy

**Model tiers:**

| Tier | Models | Used for |
|------|--------|----------|
| Frontier reasoning | GPT-5/5.2-Codex, Claude Opus 4/Sonnet 4.5, O3, O4-mini | Deep review, bug detection, agentic verification |
| Efficient | Nemotron-3-Nano-30B, GPT-4.1 | Context summarization, docstring generation, routine QA |
| Triage | gpt-3.5-turbo | Diff classification (trivial vs complex), semantic caching |

**Model-specific prompt layers:**
- Core prompt is model-agnostic
- Model-specific "prompt subunits" adjust styling:
  - **Claude**: Strong imperative language ("DO," "DO NOT"). Latest system prompt is attended to most carefully.
  - **OpenAI**: General aligned instructions. Attention decreases top-to-bottom in system prompts.

**Token management:**
- Estimation formula: `estimated_tokens = character_count / 4 + max_tokens`
- Token budget per request: 40,000 burst capacity, 40,000 tokens/minute refill (matching OpenAI rate limits)
- Rate limiting via FluxNinja Aperture: weighted fair queuing with priority (paid > free, chat > review)
- Distributed across 4 OpenAI accounts to avoid per-account rate limits

#### Relevance to our skill

Our `pass_models` config supports per-pass model overrides. We don't have:
- Model-specific prompt formatting (our prompts assume Claude)
- A triage model that pre-classifies files before deep review
- Token budget management across a review session

### 5. Verification Agent

The verification agent is a post-generation quality gate using reasoning models (O1/O3):

1. **Every comment** generated by the LLM goes through verification (not just high-severity)
2. The reasoning model articulates its thinking chain (visible as "monologue" traces in PR comments)
3. The agent generates **shell and Python verification scripts** using grep, cat, ast-grep
4. Scripts are executed in the sandbox to extract proof from the codebase
5. Only findings with evidence survive — "comments come with receipts"

**Self-healing loop:**
- Cycle: Implement → Review → Fix Critical/Warning → Re-review
- 3-attempt maximum (initial + one correction + final attempt)
- After 3: surfaces the issue to human
- Checks per cycle: (a) were issues fixed? (b) did new issues appear? (c) do the same issues remain?

**Keep/discard decision:** Context engineering prepares "the list of most important issues suggested by all the tools in an instructive manner to the reasoning model." The reasoning model decides what to surface based on evidence from verification scripts.

#### Relevance to our skill

Our planned Verification Pipeline (Feature 0) covers the same ground with a more structured approach:
- Stage 1 (feature extraction) + Stage 2 (deterministic triage) have no CodeRabbit equivalent — they filter before the expensive verification agent
- Stage 3 (agent verification) is similar, but CodeRabbit's version generates executable scripts while ours uses tool calls (Read/Grep/Glob)

**Key insight to adopt:** The "generate a verification script" pattern. Instead of our verifier reading code and reasoning about it, it could generate a concrete grep/read command that proves or disproves the finding. This produces verifiable evidence, not just LLM reasoning.

### 6. Incremental Reviews

- Default: re-review after each push, focusing only on new commits since last review
- `auto_pause_after_reviewed_commits: 5` — auto-pauses after N reviewed commits to avoid noise on active PRs
- `@coderabbitai full review` forces a complete re-review from scratch
- State stored within the PR itself (not on CodeRabbit servers) for privacy
- Semantic caching prevents regenerating identical comments when code hasn't materially changed

#### Relevance to our skill

We don't have incremental review — each invocation reviews the full diff. For pre-commit/pre-push cadence, incremental review would be valuable: "what changed since last review?" This is a future consideration, not a current priority.

### 7. Configuration System

`.coderabbit.yaml` at repo root with extensive options:

**Review profiles:** Two modes:
- **Chill** (default): Nitpicks hidden. Only potential issues and refactor suggestions.
- **Assertive**: Nitpicks included alongside issues and refactors.

**Key configuration sections:**
- `reviews.path_instructions` — per-path review guidance (glob patterns)
- `reviews.path_filters` — include/exclude files
- `reviews.auto_review` — drafts, base branches, labels that trigger/skip review
- `reviews.finishing_touches` — docstrings, unit tests, simplify, custom recipes
- `reviews.pre_merge_checks` — built-in and custom validation rules
- `reviews.tools` — 60+ linters individually configurable
- `knowledge_base` — learnings scope, issue tracker integration, web search, MCP
- `language` — 70+ locale codes for review output language
- `tone_instructions` — free-text tone customization (max 250 chars, any persona)

**Chat commands** (`@coderabbitai` in PR comments):
- `review` / `full review` — trigger incremental or full review
- `pause` / `resume` / `ignore` — control review lifecycle
- `summary` — generate/update PR summary
- `generate docstrings` / `generate unit tests` / `generate sequence diagram`
- `autofix` — auto-apply ALL unresolved findings as commits
- `autofix stacked pr` — create separate fix PR
- `resolve` — mark all review comments as resolved
- `configuration` / `generate configuration` / `help`
- Free-form follow-up questions in any comment thread

#### Relevance to our skill

Our `.codereview.yaml` is simpler but covers the core needs (cadence, confidence floor, pass models, ignore/focus paths, custom instructions). Key gaps:
- **Path-based instructions** — per-path review guidance is highly useful and easy to add
- **Review profiles** (chill/assertive) — we could add a `detail_level: standard | thorough` config
- **Tone customization** — irrelevant for CLI output
- **Chat interaction** — not applicable to CLI (but follow-up review of specific findings could be a future feature)

### 8. Output Structure

**PR-level outputs:**
- High-level summary (optionally in PR description)
- Sequence diagrams (Mermaid, for API/event/async flow changes)
- Changed files summary table
- Per-file detailed walkthrough
- Code review effort estimate
- Related issues and PRs
- Suggested labels and reviewers

**Comment-level outputs:**
- Line-level comments with severity (Critical/Major/Minor/Trivial/Info)
- Category (Potential Issue / Refactor Suggestion / Nitpick)
- **Committable suggestions** — GitHub-native suggestion format with "Commit suggestion" button
- "Prompt for AI Agents" section — copy-pasteable prompt for other LLMs to implement the fix
- `@coderabbitai autofix` applies all unresolved findings as commits

**Severity levels (5):**

| Level | Color | What it means |
|-------|-------|---------------|
| Critical | Red | System failures, security breaches, data loss |
| Major | Orange | Significant functionality/performance impact |
| Minor | Yellow | Important but non-critical |
| Trivial | Blue | Low-impact quality improvements |
| Info | White | Contextual comments, no action required |

#### Relevance to our skill

Our output is structured for tooling (findings JSON) and human reading (markdown report). We don't have:
- Sequence diagram generation (additive feature, doesn't affect finding quality)
- Committable suggestions (our `fix` field contains the fix but isn't formatted for GitHub's suggestion UI)
- "Prompt for AI Agents" field (Kodus has `llmPrompt` field — same concept; we could add this)

---

## Speed Analysis: Why CodeRabbit Is Slow

**Reported latency:**
- Typical: 1-5 minutes
- CodeRabbit's own statement: "can take up to five minutes before you see the first comment"
- Worst case: 20-30 minutes (large repos, many files)
- Data point: 23 comments on 140-line PR in 3 minutes
- Competitor comparison: GitHub Copilot responds in <30 seconds (3-10x faster)
- CodeRabbit has embraced this as "Slow AI" — positioning quality over speed

**Latency breakdown by stage:**

| Stage | Time | Parallelizable? | Serial dependency |
|-------|------|-----------------|-------------------|
| Context assembly (CodeGraph, LanceDB, issue tracker, learnings) | 5-15s | Partially | Must complete before LLM review |
| Triage (cheap model classifies files) | 2-5s | Yes | Must complete before deep review |
| Static analysis (40+ tools) | 10-30s | Yes (parallel) | Can overlap with context assembly |
| LLM deep review (per file, frontier model) | 10-30s per file | Yes (fan-out, rate-limited) | Must wait for context + triage |
| Verification agent (per comment) | 5-15s per comment | Yes (parallel) | Must wait for LLM review |
| Output formatting + diagram generation | 5-10s | No | Must wait for verification |
| **Total for 20-file PR** | **~2-8 minutes** | | |

**The critical path:** Context assembly → Triage → LLM review → Verification → Output. Each stage blocks the next. Within stages, parallelism helps but is rate-limited by LLM API quotas.

### Speed Strategies for Our Skill

| Strategy | What it does | Impact | Status |
|----------|-------------|--------|--------|
| **Parallel explorer passes** | 8 explorers run simultaneously, not file-by-file | **Major** — our architecture is inherently faster than CodeRabbit's per-file sequential approach | Already in architecture |
| **File-level triage** | Cheap model classifies files before deep review; skip trivial files | **Major** — CodeRabbit saves ~50% costs this way | Not planned — should add |
| **Deterministic triage before verification** | Feature extraction + deterministic rules filter obvious FPs for free | **Medium** — reduces verification workload 30-50% | Planned (Verification Pipeline Stage 1-2) |
| **Threshold-based verification** | Only verify when >5 findings | **Medium** — skips entire verification for small reviews | Planned (Verification Pipeline Feature 0) |
| **Adaptive pass selection** | Skip irrelevant passes (concurrency on CSS) | **Small-Medium** | Already in architecture |
| **Skip persistent infrastructure** | Per-review search instead of CodeGraph/LanceDB | **Eliminates setup latency** | Current design — tradeoff: less context, but faster |
| **Incremental linting** | Only lint changed files | **Small** | Planned in run-scans.sh |

---

## Gap Analysis: Path to CodeRabbit-Quality Reviews

### Priority 1 — Must close (highest quality impact):

**1a. File-level triage before deep review**
CodeRabbit's ~50% cost savings from triage is also a ~50% speed savings. Add a triage step (cheap model or heuristic) that classifies files as trivial (skip) or complex (review). This is not in any current plan and should be added.
- Heuristic approach (no LLM): files with <5 changed lines AND no function signature changes → skip deep review, only run linters
- LLM approach: haiku classifies each file in batch
- **Proposed location:** New Step 3.6 in SKILL.md, between adaptive pass selection (3.5) and explorer launch (4a)

**1b. Richer cross-file context via code_intel.py dependency graph**
Our planned Feature 12 (cross-file planner) + Feature 0c (code_intel.py) can approximate CodeRabbit's CodeGraph at review time. Specifically: `code_intel.py imports` + `code_intel.py callers` + `code_intel.py functions` together build a per-review dependency graph. The cross-file planner then uses this graph to generate targeted search queries.
- **Enhancement to Feature 0c:** Add a `graph` subcommand that combines imports + functions + callers into a single dependency output. This replaces ad-hoc Grep with structural analysis.

**1c. ast-grep security rules**
CodeRabbit's 150+ ast-grep rules are almost entirely security-focused and have near-zero false positives. We should integrate ast-grep with a curated rule set from `ast-grep-essentials`.
- **Proposed location:** Addition to `run-scans.sh` (v1.3 Feature 0a) — detect ast-grep, run with rules from a bundled ruleset
- Priority rules: hardcoded secrets, weak crypto, JWT vulnerabilities, injection patterns

### Priority 2 — Should close (meaningful improvement):

**2a. Verification with executable evidence**
CodeRabbit's verifier generates shell/Python scripts that prove findings. Our verifier (Verification Pipeline Feature 0, Stage 3) uses tool calls. Consider: the verifier should output a `verification_command` field alongside the verdict — a concrete grep/read command that anyone can run to confirm the finding.

**2b. Path-based review instructions**
Add to `.codereview.yaml`:
```yaml
path_instructions:
  - path: "src/auth/**"
    instructions: "Focus on authentication bypass and session management"
  - path: "migrations/**"
    instructions: "Check for data loss and backward compatibility"
```
Low effort, high value. Can be implemented alongside REVIEW.md (v1.3 Feature 13).

**2c. Custom diff format experiment**
CodeRabbit says their custom diff format made a significant difference. We should experiment with transforming unified diffs before feeding to explorers. Possible formats:
- Side-by-side with annotations
- Compressed summary for low-priority files, full diff for high-priority
- Few-shot examples showing "here's a diff, here's what changed semantically"
Low-cost experiment with measurable impact via our planned evaluation framework.

**2d. More deterministic tools**
Add Ruff (Python), golangci-lint (Go), Clippy (Rust) to `run-scans.sh`. Each catches real issues deterministically, reducing LLM workload.

### Priority 3 — Future opportunity:

**3a. Learnings system**
CLI-compatible approach: `.codereview-learnings.json` accumulates corrections across reviews. User flags false positives via `--mark-fp <finding-id>`. Script accumulates these. On next review, learnings injected as context.

**3b. Semantic caching for incremental review**
When re-reviewing a PR after a push, diff the new changes against previously reviewed changes. Skip re-reviewing unchanged code. Requires state from previous review runs.

**3c. "Prompt for AI Agents" field**
Add an `llm_prompt` field to findings (Kodus already has this as `llmPrompt`). A copy-pasteable prompt that another LLM can use to fix the issue. Low effort, meaningful UX improvement.

**3d. Sequence diagrams**
Auto-generate Mermaid diagrams for API/event flow changes. High developer value, additive feature.

**3e. Committable suggestions format**
Format the `fix` field in findings as GitHub-compatible suggestion blocks so users can paste them into PR comments as committable suggestions.

---

## What NOT to Adopt

| CodeRabbit Feature | Why skip |
|-------------------|----------|
| Persistent CodeGraph + LanceDB | Requires infrastructure we don't have as a CLI tool. Per-review analysis via code_intel.py is the right tradeoff. |
| 70+ language locales | Irrelevant for CLI output; model handles translation implicitly |
| PR description generation | We produce reports, not PR comments |
| Chat interaction model | Not applicable to CLI |
| Review profiles (Chill/Assertive) | Our action tiers (must_fix/should_fix/consider) serve the same purpose |
| Saga pattern / durable state | Over-engineered for CLI; our reviews are single-shot |
| 4 OpenAI accounts for rate limiting | We use Claude Code's built-in model access |
| Tone customization | Fun but irrelevant for developer tooling |
| Auto-assign reviewers / suggested labels | PR management features, not review quality |

---

## References

### CodeRabbit Engineering Blog
- Deep Dive — https://www.coderabbit.ai/blog/coderabbit-deep-dive
- Pipeline AI vs Agentic AI — https://www.coderabbit.ai/blog/pipeline-ai-vs-agentic-ai-for-code-reviews-let-the-model-reason-within-reason
- Accurate Reviews on Massive Codebases — https://www.coderabbit.ai/blog/how-coderabbit-delivers-accurate-ai-code-reviews-on-massive-codebases
- Art and Science of Context Engineering — https://www.coderabbit.ai/blog/the-art-and-science-of-context-engineering
- Context Engineering for AI Code Reviews — https://www.coderabbit.ai/blog/context-engineering-ai-code-reviews
- Agentic Code Validation — https://www.coderabbit.ai/blog/how-coderabbits-agentic-code-validation-helps-with-code-reviews
- AI Native Universal Linter (ast-grep) — https://www.coderabbit.ai/blog/ai-native-universal-linter-ast-grep-llm
- Models Are No Longer Interchangeable — https://www.coderabbit.ai/blog/the-end-of-one-sized-fits-all-prompts-why-llm-models-are-no-longer-interchangeable
- Benchmarking GPT-5 — https://www.coderabbit.ai/blog/benchmarking-gpt-5-why-its-a-generational-leap-in-reasoning
- Squeezing Water from Stone (Rate Limiting) — https://www.coderabbit.ai/blog/squeezing-water-from-stone
- Ballooning Context in the MCP Era — https://www.coderabbit.ai/blog/handling-ballooning-context-in-the-mcp-era-context-engineering-on-steroids
- Behind the Curtain: Bringing a New Model Online — https://www.coderabbit.ai/blog/behind-the-curtain-what-it-really-takes-to-bring-a-new-model-online-at-coderabbit
- Cost-Effective Generative AI Application — https://www.coderabbit.ai/blog/how-we-built-cost-effective-generative-ai-application
- The Rise of Slow AI — https://www.coderabbit.ai/blog/the-rise-of-slow-ai-why-devs-should-stop-speedrunning-stupid
- Tops Martian Benchmark — https://www.coderabbit.ai/blog/coderabbit-tops-martian-code-review-benchmark
- Boosting Static Analysis Accuracy with AI — https://www.coderabbit.ai/blog/boosting-static-analysis-accuracy-with-ai
- Pre-Merge Checks — https://www.coderabbit.ai/blog/pre-merge-checks-built-in-and-custom-pr-enforced

### Partner Case Studies
- Google Cloud: How CodeRabbit Built Its Agent — https://cloud.google.com/blog/products/ai-machine-learning/how-coderabbit-built-its-ai-code-review-agent-with-google-cloud-run
- LanceDB Case Study — https://lancedb.com/blog/case-study-coderabbit/
- OpenAI: Shipping Code Faster — https://openai.com/index/coderabbit/

### Documentation
- Review Overview — https://docs.coderabbit.ai/guides/code-review-overview
- Configuration Reference — https://docs.coderabbit.ai/reference/configuration
- Review Commands — https://docs.coderabbit.ai/reference/review-commands
- Learnings — https://docs.coderabbit.ai/guides/learnings
- Supported Tools — https://docs.coderabbit.ai/tools
- CLI Skills — https://docs.coderabbit.ai/cli/skills

### External Analysis
- Endor Labs: PwnedRabbit Security Analysis — https://www.endorlabs.com/learn/when-coderabbit-became-pwnedrabbit-a-cautionary-tale-for-every-github-app-vendor-and-their-customers
- Architecting CodeRabbit-like Agent (Orchestration) — https://learnwithparam.com/blog/architecting-coderabbit-ai-agent-orchestration-brain
- Bits & Brains: Inside CodeRabbit's Architecture — https://www.linkedin.com/pulse/bits-brains-inside-coderabbits-architecture-rajat-jain-flvkf
- State of AI Code Review Tools 2025 — https://www.devtoolsacademy.com/blog/state-of-ai-code-review-tools-2025/
- ast-grep-essentials Repository — https://github.com/coderabbitai/ast-grep-essentials

### Benchmarks
- Martian Code Review Bench — https://codereview.withmartian.com/
- Greptile AI Code Review Benchmarks — https://www.greptile.com/benchmarks
