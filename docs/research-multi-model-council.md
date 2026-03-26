# Research: Multi-Model Council for Code Review

Collecting ideas and approaches from other projects on how multiple AI models can collaborate on code review. This is a research document — not an active plan. The goal is to understand the design space before committing to an architecture.

Last updated: 2026-03-26 (added PR-Agent analysis)

---

## Why Multi-Model?

Single-model review has a fundamental limitation: the model's blind spots are systematic. If Claude consistently misses a class of bugs (e.g., Go channel deadlocks, Python GIL-related races), no amount of prompt engineering fixes it — the model's training distribution determines what it sees. A second model with different training data catches different things.

The question isn't whether multi-model helps — it's whether the quality improvement justifies the cost and complexity.

---

## Approaches Observed

### 1. Claude Octopus — Parallel Specialist Fleet

**Architecture:** 3 models run in parallel, each assigned a review specialty:
- Codex (OpenAI): logic and correctness
- Gemini (Google): security and edge cases
- Claude (Anthropic): architecture and synthesis

**Verification:** A dedicated Round 2 verifier (Codex or Claude) reviews each finding and assigns `confirmed / false_positive / needs_debate`.

**Debate:** Round 3 runs adversarial debate on `needs_debate` findings using ACH (Analysis of Competing Hypotheses) falsification — each model tries to disprove the others' conclusions.

**Quality gate:** 75% consensus threshold before findings are accepted.

**Strengths:**
- True diversity of perspective — different training data catches different bugs
- Verification round filters false positives before synthesis
- Adversarial debate resolves contested findings with structured argumentation

**Weaknesses:**
- Requires 3 API keys and 3 subscription costs
- Latency: parallel exploration + sequential verification + debate = slow
- Provider management complexity (fallback routing, auth freshness, lockout)
- Overkill for most reviews — a 5-line bug fix doesn't need 3 models

### 2. Claude Octopus — Blinded Evaluation Mode

**Architecture:** Each model evaluates independently without seeing others' proposals. Prevents anchoring bias.

Each model receives:
- The same code diff
- The same review criteria
- NO access to other models' findings

Each produces independently:
- Top 3 risks
- Overlooked concerns
- Critical assumptions
- Evaluation criteria (rated 1-10)

A synthesizer then merges the independent evaluations.

**Strengths:**
- Eliminates groupthink / anchoring
- Each model brings genuinely independent perspective
- Easy to add/remove models without changing others' behavior

**Weaknesses:**
- Most expensive mode (no shared context, full exploration per model)
- Synthesis step needs to reconcile potentially contradictory findings
- No cross-pollination — models can't build on each other's insights

### 3. Aider — Architect + Editor Pattern

**Architecture:** Two models with distinct roles:
- Architect (strong model, e.g., Opus): reasons about what to change, produces a plan
- Editor (fast model, e.g., Sonnet): implements the plan as actual code edits

**Relevance to code review:**
- Architect model identifies issues and reasons about severity
- Editor model verifies by attempting the fix — if the fix is trivial, the finding is real; if the fix is complex or breaks things, the finding may be wrong

**Strengths:**
- Natural role separation (reasoning vs. execution)
- Cost-efficient: strong model only does the expensive reasoning
- The "attempt the fix" verification is a unique signal

**Weaknesses:**
- Not designed for code review (designed for code editing)
- "Attempt the fix" verification is expensive and potentially dangerous
- Only two models, not a true council

### 4. Constitutional AI / Principle-Based Critique

**Architecture:** Multiple evaluation passes against different constitutions (value systems):
- Security constitution: OWASP, CWE, secure coding principles
- Performance constitution: algorithmic complexity, resource management
- Maintainability constitution: clean code, SOLID, readability

Each pass uses the same model but with a different constitution prompt. The model critiques the code from that constitution's perspective.

**Relevance to code review:**
- Our explorer passes already do this (correctness, security, reliability, etc.)
- The difference: constitutional AI makes the principles *explicit and auditable*
- A constitution can be versioned, reviewed, and updated by the team

**Strengths:**
- No multi-model complexity — single model, multiple perspectives
- Constitutions are auditable and customizable per repo
- Cheaper than multi-model (same model, multiple prompts)

**Weaknesses:**
- Same model blind spots across all constitutions
- Not a true "second opinion" — it's the same model with different instructions

### 5. PR-Agent (Qodo) — Self-Reflection with Dedicated Reasoning Model

**Source:** Analysis of the PR-Agent open-source code review tool (`~/workspaces/pr-agent`), the most widely-deployed AI code review agent. Apache 2.0, Python.

**Architecture:** NOT a council — a **two-pass pipeline** where the second pass uses a potentially different model to score and validate the first pass's output. Multi-model is used for *cost optimization and role separation*, not diversity.

**Model Tier System (4 tiers):**
- `model` (primary): gpt-5.4 — used for complex analysis (review, code suggestions)
- `model_weak`: gpt-4o — used for simpler tasks (PR description, changelog, Q&A)
- `model_reasoning`: o4-mini — used specifically for self-reflection on code suggestions
- `fallback_models`: [o4-mini] — retry chain when primary model fails

**Self-Reflection Flow (code suggestions only):**
```
Step 1: Generate suggestions with primary model (e.g., gpt-5.4)
Step 2: Score each suggestion with reasoning model (e.g., o4-mini)
         → Each suggestion gets a 0-10 score + "why" explanation
Step 3: Filter by score threshold (configurable, default 0)
Step 4: High-scoring suggestions get dual-published (table + inline)
```

The reflection prompt has explicit scoring bands:
- **8-10**: Critical bugs, security vulnerabilities
- **3-7**: Minor issues, style, maintainability
- **0**: Wrong suggestions (docstrings, type hints, comments, unused imports)
- **Special caps**: "verify/ensure" suggestions max 7, error handling max 8

**Fallback logic detail:** If the primary model fails and falls back to `fallback_models[0]`, the reflection step also falls back to the same model (avoids using a "stronger" model for reflection than was used for generation — a smart constraint).

**Model Capabilities Matrix:**
PR-Agent maintains explicit capability lists per model category:
- `USER_MESSAGE_ONLY_MODELS` — no system prompt support (deepseek-reasoner, o1)
- `NO_SUPPORT_TEMPERATURE_MODELS` — temperature must be omitted (o3, o4, gpt-5-mini)
- `SUPPORT_REASONING_EFFORT_MODELS` — accept reasoning_effort param (o3/o4)
- `CLAUDE_EXTENDED_THINKING_MODELS` — Claude extended thinking support
- `STREAMING_REQUIRED_MODELS` — must use streaming API (qwq-plus)

This is infrastructure-level multi-model awareness, not a council pattern.

**Response Comparison Prompt (unused but defined):**
PR-Agent defines a `pr_evaluate_prompt_response.toml` that compares two model responses side-by-side and scores them 1-10 on task adherence, diff analysis quality, feedback prioritization, and conciseness. Output: `which_response_was_better: 0|1|2` + per-response scores. **This prompt is defined but NOT actively used in the codebase** — it appears to be infrastructure for future A/B testing.

**Strengths:**
- Cost-efficient: weak model for easy tasks, strong model for hard tasks, reasoning model for validation
- The self-reflection scoring bands are well-calibrated with explicit caps per suggestion type
- Fallback chain provides resilience without requiring multiple API keys
- Model capability awareness prevents sending unsupported parameters

**Weaknesses:**
- Self-reflection is only used for code suggestions, not for review findings
- The review tool (`/review`) uses a single model with no verification step
- No true council or diversity — all models are from the same provider (OpenAI by default)
- The response comparison prompt is defined but unused — suggests they haven't found a compelling use case for it yet

**Relevance to our skill:**
- The **tiered model routing** (weak/primary/reasoning) maps directly to our `pass_models` config. PR-Agent validates this is a good pattern in production.
- The **self-reflection scoring bands** (0-10 with explicit caps per issue type) are the most mature scoring calibration we've seen. Worth adopting for our judge's per-finding scores.
- The **response comparison prompt** is a building block we could use for our spot-check feature (Verification Pipeline Feature 4) — send finding + code to an alternate model and ask "Is this valid?"
- The **fallback constraint** (don't use a stronger model for reflection than for generation) is a subtle but important design principle.
- The unused comparison prompt suggests that **A/B testing model outputs** is harder to make useful in practice than it seems in theory.

### 6. Cursor / Windsurf — Background Review Agent

**Architecture:** A background agent continuously reviews code as it's written:
- Runs on every save or at configurable intervals
- Uses a fast model for real-time feedback
- Escalates complex findings to a stronger model

**Relevance to code review:**
- Not a council pattern per se, but interesting for the cadence model
- Our `cadence: pre-commit` config could trigger a fast background review
- A "shadow review" that runs continuously could catch issues earlier

**Strengths:**
- Fastest feedback loop — issues caught while writing
- Low-cost: fast model for routine checks, strong model only for escalation

**Weaknesses:**
- High noise at the "every save" cadence
- Not applicable to our skill's batch review model

---

## Design Space for Our Skill

### Dimensions to decide

| Dimension | Options | Trade-offs |
|-----------|---------|------------|
| **When to use multi-model** | Always / threshold-based / opt-in flag | Cost vs. quality. Threshold-based (only for high-severity low-confidence findings) is the sweet spot. |
| **Which models** | Same-family variants (Sonnet↔Opus) / cross-provider (Claude↔GPT↔Gemini) | Same-family is simpler (no API keys) but less diverse. Cross-provider is more diverse but requires setup. |
| **How models interact** | Parallel (independent) / sequential (verify after) / adversarial (debate) | Parallel is fastest but needs synthesis. Sequential is simplest. Adversarial is most thorough but slowest. |
| **What they review** | Full diff / only contested findings / only high-severity | Full diff is expensive. Targeted review (only contested/high-severity) is cost-effective. |
| **How to synthesize** | One model synthesizes / voting / structured merge | One synthesizer is simplest. Voting needs ≥3 models. Structured merge needs schema alignment. |

### Constraints specific to our skill

1. **No mandatory API keys.** The skill must work with just Claude (included with Claude Code subscription). Multi-model must be opt-in.
2. **No external service dependencies.** The skill runs locally. No calling cloud APIs that require authentication setup (unless the user explicitly opts in).
3. **Cost transparency.** If multi-model costs money beyond the Claude Code subscription, the skill must tell the user before running.
4. **Batch review model.** Our skill reviews a diff in one shot, not continuously. The multi-model pattern must fit a batch workflow.
5. **Pre-PR / during-PR scope.** The skill reviews code and produces findings. It does not post comments, merge PRs, or command CI. The multi-model output is a finding report, not an action.

### Most promising approach for Verification Pipeline

**Spot-check with model variant** (implemented as Verification Pipeline Feature 4):
- After the judge produces findings, high-severity + low-confidence findings get a spot-check from a different Claude model variant (Sonnet explorer → Opus spot-check, or vice versa)
- No external API keys needed
- Cost-controlled (max N spot-checks, threshold-based activation)
- Provides genuine second opinion without full council complexity

### Future directions (v1.5+)

**If spot-check proves valuable:**
- Expand to cross-provider spot-check (Claude → GPT, requires OPENAI_API_KEY)
- Add blinded evaluation mode for security findings specifically
- Consider structured debate for findings where spot-check returns `uncertain`

**If spot-check doesn't prove valuable:**
- The same-family model variants may not provide enough diversity
- Cross-provider council becomes the next step
- Or: invest in better single-model prompts instead (may have more ROI)

### 6. Kodus-AI — Panel of Named Experts (Single-Prompt Role-Play)

**Source:** Analysis of the Kodus-AI code review platform (~/workspaces/kodus-ai), a production AGPLv3 SaaS with multi-provider LLM support.

**Architecture:** NOT multi-model — a single LLM call with a prompt that instructs the model to role-play a panel of named experts who analyze sequentially within one response. Used in two contexts:

**Safeguard Panel (5 experts, single prompt):**
- **Edward** — Special Cases Guardian (pre-analysis gatekeeper with veto power). Applies 6 auto-discard rules before other experts analyze. Catches the top false positive categories: phantom knowledge claims, configuration syntax errors, speculative null checks, database schema assumptions, undefined symbols with custom imports, quality opinions on test code.
- **Alice** — Syntax & Compilation (type safety, compilation, type contract preservation)
- **Bob** — Logic & Functionality (structural defects, logic errors)
- **Charles** — Style & Consistency (naming conventions, language alignment)
- **Diana** — Final Referee (constructs JSON output, applies the "Fundamental Rule": structural defects are kept, speculative concerns are discarded)

**KodyRules Classifier (3 experts, single prompt):**
- **Alice, Bob, Charles** — each independently evaluates PR diffs against custom team rules, then they critique each other's findings before producing a merged result.

**Key insight:** This is NOT multi-model and NOT separate LLM calls. It's a single prompt that uses named expert role-play to structure the model's reasoning. The panel structure forces:
1. **Sequential analysis** — each expert builds on the previous one's output
2. **Role specialization** — each expert focuses on a narrow concern area
3. **Adversarial review** — later experts can override earlier ones (Edward has veto power; Diana is the final referee)
4. **Gatekeeper pattern** — Edward pre-filters before expensive analysis begins

**Strengths:**
- Zero additional cost — same single LLM call, just better-structured reasoning
- The gatekeeper (Edward) is remarkably effective at catching common false positive categories
- Named experts make the analysis auditable — you can trace which "expert" caught or missed an issue
- The panel structure encourages the model to consider multiple perspectives within one response

**Weaknesses:**
- Still a single model — no true diversity of training data or blind spots
- The experts are simulated, not independent — later experts can be biased by earlier ones
- The quality depends entirely on the model following the role-play instructions faithfully
- Not effective for genuinely contested findings where different training data would help

**Relevance to our skill:**
- This is NOT a replacement for multi-model council — it's a complementary technique for structuring single-model reasoning
- The **gatekeeper pattern** (Edward) is valuable for our verification pipeline. Our Feature 0's Stage 2 (deterministic triage) captures the same intent programmatically — filtering obvious false positives before expensive analysis
- The **named expert role-play** could improve our judge prompt. Instead of a flat instruction list, structure the judge's analysis as sequential expert passes (existence checker → contradiction checker → severity calibrator → synthesizer)
- The **panel for custom rules** (Alice/Bob/Charles) is relevant if we ever expand `custom_instructions` to structured rules — a panel structure helps evaluate rule violations more accurately

**Kodus-AI's broader architecture context:**
- Uses Gemini 2.5 Pro as primary model, DeepSeek V3 as fallback (not for council — for redundancy)
- Uses GPT-4O Mini for feature extraction (fast, cheap, focused task)
- Uses Gemini 2.5 Flash for verification agent (cost-efficient for tool-using tasks)
- This is multi-model in the *infrastructure* sense (different models for different pipeline stages) but not a *council* (models don't deliberate or vote)

### Summary: Multi-Model vs Structured Single-Model

The Kodus-AI and PR-Agent analyses surface an important distinction in the design space:

| Approach | Example | What it provides | Cost |
|----------|---------|-----------------|------|
| **Multi-model council** | Octopus (3 models debate) | Genuine diversity — different training data catches different bugs | 3x+ LLM cost |
| **Multi-model pipeline** | Kodus (different models per stage) | Cost optimization — cheap model for extraction, strong model for analysis | 1.5-2x LLM cost |
| **Tiered model routing** | PR-Agent (weak/primary/reasoning) | Role-appropriate model selection — cheap model for easy tasks, reasoning model for validation | 1.2-1.5x LLM cost |
| **Self-reflection scoring** | PR-Agent (generate → score with reasoning model) | Quality-gated output — numeric scores with calibrated bands enable threshold filtering | 1.1-1.3x LLM cost |
| **Structured single-model** | Kodus panel (named experts in one prompt) | Better reasoning structure — zero additional cost | 0 additional cost |
| **Same-family spot-check** | Our Feature 4 (Sonnet→Opus) | Marginal diversity within model family | 1.2-1.5x LLM cost |

These are not mutually exclusive. The highest-value approach for our skill is likely:
1. **Structured single-model** (free) — apply now via judge prompt restructuring
2. **Tiered model routing** (low-moderate cost) — use different model tiers per pipeline stage. PR-Agent validates this pattern in production: weak model for easy tasks, primary for analysis, reasoning model for validation. Maps to our existing `pass_models` config.
3. **Self-reflection scoring** (low cost) — add a scoring pass to the judge or verification stage, inspired by PR-Agent's 0-10 scoring bands with explicit caps per issue type. This is the single most transferable technique from PR-Agent.
4. **Same-family spot-check** (low cost, opt-in) — Feature 4 of Verification Pipeline
5. **Cross-provider council** (high cost, future) — only if empirical data shows same-family diversity is insufficient

---

## v1.2 Decision: Deferred to Research

During v1.2 planning, we designed a full "Multi-Model Council Review" feature (Feature 4) that would run each core explorer pass with two Claude model variants in parallel, then feed all findings to a cross-model judge. The design included:
- `--council` and `--council-model` CLI flags
- 8 core explorer Tasks (2 per pass × 2 models) instead of 4
- Cross-model synthesis in the judge: corroboration (+0.15 confidence boost), single-source (extra scrutiny), contradiction (flag for human review)
- Chunked + council safeguard (cap at 36 Task calls)
- Config: `council.enabled`, `council.model_b`, `council.passes`

**Why deferred:** The full design was for same-family model variants (Sonnet ↔ Opus), not true cross-provider diversity. Since same-family models share training data, the quality improvement is uncertain — we'd be doubling explorer cost for potentially marginal diversity. True multi-model (Claude + GPT + Gemini) requires cross-vendor spawning that the current Task tool doesn't support.

**What we implemented instead (v1.2):** The **named expert panel** pattern from Kodus-AI, applied to our judge prompt. This restructures the judge's analysis as sequential expert passes — providing better reasoning structure at zero additional cost. See "Implemented: Named Expert Panel" below.

**The full Feature 4 design is preserved in `docs/plan-v1.2.md`** for future reference. If empirical data shows same-family diversity adds meaningful value, or if cross-vendor spawning becomes available, the design can be revived.

### Implemented: Named Expert Panel (v1.2)

Adapted from the Kodus-AI panel pattern (Section 6 above). The judge prompt is restructured as a sequence of named expert roles:

| Expert | Role | What they check |
|--------|------|----------------|
| **Gatekeeper** | Pre-filter triage | Eliminates obvious false positives before expensive analysis: phantom knowledge claims, speculative concerns, framework-guaranteed behavior, findings outside diff scope |
| **Verifier** | Existence + evidence check | For each remaining finding: does the code actually exist? Does the evidence reference real lines? Can the finding be reproduced with Read/Grep? |
| **Calibrator** | Severity + confidence | Applies severity calibration rules, contradiction check (does the finding contradict other findings?), confidence adjustment based on evidence strength |
| **Synthesizer** | Cross-explorer merge | Groups root causes, merges duplicates, identifies cross-cutting patterns, produces verdict + strengths + spec_gaps |

This maps directly to what the judge already does (Steps 1-6 in `reviewer-judge.md`), but gives each analysis phase a named identity that:
1. Forces sequential reasoning (each expert builds on the previous)
2. Makes the analysis auditable (which "expert" kept or dropped a finding)
3. Prevents the judge from skipping steps (a named expert can't be silently omitted)

**Why this works:** Kodus-AI demonstrated that named expert role-play within a single prompt significantly improves reasoning quality at zero cost. The key insight is that **the panel structure is a prompt engineering technique, not a multi-model technique** — it makes the single model reason more carefully by forcing it through distinct analytical lenses.

---

## Open Questions

1. **How much diversity do same-family model variants actually provide?** Sonnet and Opus share training data — how often does Opus catch something Sonnet misses, and vice versa? We need empirical data.

2. **What's the false positive rate improvement from multi-model?** If single-model review has 20% false positives, does adding a verifier model reduce that to 10%? 5%? The improvement needs to justify the cost.

3. **Is adversarial debate worth it for code review?** Octopus uses it, but code review findings are often factual (the bug either exists or it doesn't). Debate is more valuable for subjective decisions (architecture, design). Is the overhead justified?

4. **How do we measure "review quality"?** Without ground truth (known bugs planted in diffs), we can't objectively measure whether multi-model catches more bugs. We'd need a benchmark suite.

5. **Can domain-specific constitutions replace multi-model?** Our domain checklists (v1.3 Feature 2) and per-pass calibration examples already provide domain-specific lenses. Adding more constitutions to a single model may be cheaper than adding models.

---

## References

- Claude Octopus (`~/workspaces/claude-octopus`) — Multi-AI orchestration with parallel fleet, verification round, and adversarial debate
- Aider (https://aider.chat) — Architect + Editor dual-model pattern
- Constitutional AI (Anthropic research) — Principle-based critique with explicit value systems
- Analysis of Competing Hypotheses (ACH) — Intelligence analysis methodology used by Octopus for cross-model falsification
- Kodus-AI (`~/workspaces/kodus-ai`) — Production code review platform with panel-of-experts prompts, 3-stage safeguard pipeline, and multi-model pipeline (different models per stage). AGPLv3, NestJS/TypeScript monorepo. Analyzed 2026-03-26.
- PR-Agent / Qodo (`~/workspaces/pr-agent`) — Most widely-deployed AI code review tool. Self-reflection scoring (generate→score with dedicated reasoning model), tiered model routing (weak/primary/reasoning/fallback), response comparison prompt (defined but unused), and ticket compliance checking. Apache 2.0, Python. Analyzed 2026-03-26.
