# Research: Adaptive & Dynamic Expert Selection in AI Code Review

**Date:** 2026-03-28
**Feeds into:** `specs/adaptive-expert-selection.md` (Spec A), `specs/dynamic-expert-enrichment.md` (Spec B)

## Research Question

Do any AI code review tools use adaptive expert selection (selecting from a pool based on the diff) or dynamic expert generation (synthesizing new reviewer personas at runtime)? What does the research say about persona effectiveness?

## Motivation

Our codereview skill has 10 experts (3 core, 7 extended) selected by regex pattern matching against the diff text. This has three problems:
1. Regex matches in comments and strings (false activation)
2. When a PR touches Helm charts, Terraform, or CUDA, no expert activates (coverage gaps)
3. The expert set is fixed — no way to adapt to domains we didn't anticipate

We investigated whether other tools solve these problems, and what research says about the effectiveness of dynamic expert systems.

---

## Landscape Analysis

### Tier 1: True Dynamic Expert Generation

**AgentVerse** (ICLR 2024, arXiv:2308.10848)
- A recruiter LLM dynamically generates expert descriptions from scratch based on the task goal
- Four-stage loop: Expert Recruitment → Collaborative Decision-Making → Action Execution → Evaluation
- Group composition is dynamically adjusted based on feedback from evaluation
- Research framework, not a production code review tool
- No ablation study isolating dynamic vs static recruitment

**Solo Performance Prompting / SPP** (NAACL 2024, arXiv:2307.05300)
- A single LLM identifies what expert personas are needed, generates them, then role-plays multi-turn self-collaboration
- Published finding: LLM-generated personas outperform pre-specified fixed personas
- Only works with GPT-4-class models — not effective in GPT-3.5 or Llama2-13b
- Not applied specifically to code review

**Open Code Review / OCR** (github.com/spencermarx/open-code-review)
- 29 personas: 14 specialist + 5 holistic + 10 famous-engineer (Kent Beck, Sandi Metz, etc.)
- Ephemeral reviewers can be created inline with `--reviewer "Focus on error handling in the auth flow"`
- Prompt structure: Focus Areas → Review Approach → What You Look For → Output Style → Agency Reminder
- Metadata in separate `reviewers-meta.json` with `tier`, `focus_areas`, `is_default`
- Ephemeral creation is user-initiated, not automated from diff analysis
- **Best prompt structure for our use case** — focused 40-60 line review checklists, not kitchen-sink capability lists

**CAMEL Framework** (arXiv:2303.17760)
- Role-playing with inception prompting — LLM generates role descriptions given task context
- Foundational multi-agent framework; general-purpose, no code review benchmarks

### Tier 2: Dynamic Selection from Static Pool

**Qodo 2.0 / PR-Agent** (qodo.ai)
- 15+ specialized review agents
- Orchestrator analyzes PR characteristics, activates relevant agents — "a documentation update doesn't need deep security analysis; a payment flow change demands it"
- Judge agent evaluates findings across agents, resolves conflicts, removes duplicates
- Self-reflection scoring: code suggestions scored 0-10 with calibrated bands (8-10 critical, 3-7 minor, 0 wrong)
- Tiered model routing: `model_weak` (gpt-4o), `model` (gpt-5.4), `model_reasoning` (o4-mini), `fallback_models`
- Selection logic not publicly documented

**Anthropic Claude Code Review** (claude.com/blog/code-review)
- Scales agent count with PR complexity — "Large or complex changes get more agents and a deeper read; trivial ones get a lightweight pass"
- Specialized probes for data-handling errors, API misuse, cross-file consistency
- Runs on nearly every internal Anthropic PR; 84% of large PRs get findings, 7.5 avg issues
- $15-25/review; architecture details not public

**Ellipsis AI** (ellipsis.dev)
- "Hundreds of agents with thousands of prompts"
- Hierarchical: top-level agents decompose review task, delegate to specialized lower-level agents
- Multi-step RAG + language server proxies for IDE-like context (go-to-definition, find-references)
- Multistage filtering pipeline for false positives
- Largest static pool found; not open source

**DyLAN** (arXiv:2310.02170, COLM 2024)
- Dynamic LLM-Agent Network with "Agent Importance Score" for inference-time agent selection
- Two-stage: Team Optimization (select agents) → Task Solving (dynamic architecture + early stopping)
- 13% improvement on MATH and HumanEval
- Research framework, not code-review-specific

**MasRouter** (ACL 2025, aclanthology.org/2025.acl-long.757/)
- Cascaded controller network handles collaboration mode, role allocation, and LLM routing
- 1.8-8.2% improvement over SOTA on MBPP; 52% overhead reduction on HumanEval
- Requires training the router; not applied to code review

### Tier 3: Static Pre-Defined Expert Panels

**Diffray** (diffray.ai)
- 11 static agents: Security, Performance, Bug Hunter, Quality Guardian, Architecture Advisor, Consistency Checker, Documentation Reviewer, Test Analyst, General Reviewer, SEO Agent, Refactoring Advisor
- Agents self-filter by language/file-type relevance
- Claims 87% fewer false positives and 3x more bugs vs single-agent
- No dynamic generation or user customization

**Agent Council** (github.com/andrewvaughan/agent-council)
- 13 personas across 6 councils (Product, Feature, Architecture, Review, Deployment, GTM)
- Voting: Approve/Concern/Block; complexity tiers (Standard/Advanced) scale depth
- Full SDLC coverage, not just code review

**AI Council** (ai-council.tech)
- 6 agents with vote (APPROVE/REVISE/REJECT), confidence scores, structured debates
- Judge synthesizes votes; low confidence triggers abstention
- CI mode with exit codes; MCP integration

**Calimero AI Code Reviewer** (github.com/calimero-network/ai-code-reviewer)
- 2-5+ configurable agents, multi-model (Claude + GPT-4 simultaneously)
- Consensus-based scoring weights findings by inter-agent agreement

**HubSpot Sidekick** (InfoQ report, March 2026)
- 2-agent: primary reviewer + judge that filters feedback
- Multi-model fallback via Aviator framework (Anthropic, OpenAI, Google)
- 90% faster feedback, 80% engineer approval

### Tier 4: Prompt Libraries

**Baz Awesome Reviewers** (github.com/baz-scm/awesome-reviewers)
- 4,468 review prompts/rules across 15+ languages
- Mined from real code review discussions in 1,000+ open source repos
- YAML frontmatter: `title`, `description`, `label`, `language`, `repository`, `comments_count`, `repository_stars`
- Companion JSON with full PR discussion provenance
- Top labels: Configurations (637), Code Style (398), Security (344), API (324), Error Handling (247)
- Top languages: TypeScript (673), Python (501), Go (303), Rust (273), Java (135), Ruby (66)
- **Not personas** — individual micro-rules with bad/good code examples
- We curated language checklists from this dataset: Go (25 items), Python (24 items)

---

## Existing Systems We Analyzed In-Depth

### Claude Octopus (~/workspaces/claude-octopus)

32 personas in `agents/personas/`. Not a large expert roster for code review — it's a general-purpose agent system. The "parallel specialist fleet" for code review uses only 3 models (Codex for logic, Gemini for security, Claude for architecture). The value is cross-provider diversity, not large persona pools.

**Persona format:** Rich YAML frontmatter (`name`, `description`, `model`, `memory`, `tools`, `when_to_use`, `avoid_if`, `examples`, `hooks`). Very broad prompts — 100-180 lines covering entire domains. The `code-reviewer` persona alone covers security, performance, architecture, testing, 8 language-specific sections. Kitchen-sink approach.

**Key insight:** `when_to_use` and `avoid_if` fields are valuable for routing. But the broad prompts are the wrong model for code review — focused checklists beat capability lists.

### Kodus-AI (previously analyzed)

Panel of named experts (Edward/Alice/Bob/Charles/Diana) in a **single prompt** — not separate agents. Edward (gatekeeper) applies 6 auto-discard rules; Diana (referee) constructs final output. This is prompt engineering for structured reasoning, not multi-agent.

**What we adopted:** The judge's 4-stage sequential expert panel (Gatekeeper → Verifier → Calibrator → Synthesizer) was adapted from this pattern.

**What we didn't adopt:** Using the panel pattern for the review itself. Our explorers are separate parallel agents with independent tool access.

### CodeRabbit (previously analyzed)

Multi-model routing across 3 tiers: triage (gpt-3.5-turbo), efficient (Nemotron-3-Nano-30B, GPT-4.1), frontier (GPT-5, Claude Opus, O3). Model-specific prompt layers (Claude: imperative; OpenAI: structured).

**What we didn't adopt:** Multi-model routing — blocked by our runtime constraint (all sub-agents are Claude).

---

## Critical Research on Persona Effectiveness

### "Playing Pretend" — Expert Personas Hurt Coding Tasks

**Source:** Wharton GAIL / USC, March 2026 (arXiv:2512.05858)

- Coding scores dropped by 0.65 points on a 10-point scale with expert personas
- On MT-Bench coding with Mistral-7B, expert prompting dropped from 9.00 to 6.10 (32% reduction)
- Overall accuracy: 68.0% with expert personas vs 71.6% baseline
- Personas help ONLY on alignment-dependent tasks (writing, role-playing, safety)
- For knowledge-retrieval tasks (code analysis, math, factual questions), personas divert attention from factual recall

**Design implication:** Generic "you are an expert" prompts actively harm review quality. Focus on specific instructions, domain context, and review criteria — not persona framing.

### PRISM — Route Between Persona and Base Model

**Source:** arXiv:2603.18507

- Trained binary gate decides per-query whether persona helps
- LoRA adapter activated only when persona improves output
- Preserves MMLU accuracy while improving alignment tasks

**Design implication:** A prescan step could decide whether to apply persona framing vs just specific instructions per review.

### "Rethinking the Value of Multi-Agent Workflow"

**Source:** arXiv:2601.12307, January 2026

- Single agent with multi-turn conversation can match homogeneous multi-agent workflows (same LLM for all agents)
- Tested across 7 benchmarks (coding, math, QA, planning)
- Key caveat: applies to homogeneous setups only — heterogeneous (different LLMs) multi-agent still wins

**Design implication:** Our multi-agent value comes from genuinely different context per agent (each expert has unique investigation phases, checklists, calibration examples), not from the parallelism alone.

### AgentReview — Peer Review Dynamics

**Source:** EMNLP 2024, aclanthology.org/2024.emnlp-main.70/

- Reviewer persona biases cause 37.1% variation in paper decisions
- Three key dimensions: commitment, intention, knowledgeability

---

## Key Design Decisions (from this research)

### 1. No standalone language experts; language checklists instead

The existing concern-oriented experts (correctness, concurrency, error-handling) already do the investigation work. What they lack is awareness of language-specific traps. A 15-20 item checklist appended to the expert prompt fixes this without adding parallel experts.

Checklists curated from Baz Awesome Reviewers: Go (25 items, 30 source rules from 16 repos), Python (24 items, 30 source rules from 15 repos). TypeScript, Rust, Java planned.

### 2. Checklists over identities

Per the Wharton study, "Check these 12 Helm chart failure modes" beats "You are a senior K8s infrastructure engineer." Our expert prompts already follow this pattern — investigation phases and calibration examples, not identity statements.

### 3. Deterministic selection, not LLM-based

OCR and Qodo both use deterministic selection. AgentVerse and SPP use LLM-based recruitment. We chose deterministic (binary activation + priority sort) because: fully testable, no latency, no non-determinism, and matches the existing architecture's "scripts for deterministic work, AI for semantic work" principle.

### 4. 5 new domain experts, not 15

Tier 1 experts (database, infrastructure, frontend, accessibility, ai-integration) fill genuine coverage gaps. Tier 2 candidates overlap with existing experts and are better served by domain checklists injected as enrichment.

### 5. Dynamic generation in SKILL.md, not prepare()

The `prepare()` function is deterministic. The pre-mortem (4-judge council) unanimously flagged putting an LLM call in prepare() as an architectural violation. Generation runs as a SKILL.md sub-agent between prepare and explorer launch.

---

## Projects Referenced

| Project | URL | License | What we drew from |
|---------|-----|---------|-------------------|
| AgentVerse | ar5iv.labs.arxiv.org/html/2308.10848 | Research | Dynamic recruitment concept |
| Solo Performance Prompting | arxiv.org/abs/2307.05300 | Research | LLM-generated > fixed personas |
| Open Code Review | github.com/spencermarx/open-code-review | OSS | Prompt structure, tier model, 29 personas |
| Baz Awesome Reviewers | github.com/baz-scm/awesome-reviewers | OSS | 4,468 rules, language checklist source |
| Claude Octopus | ~/workspaces/claude-octopus | Internal | Frontmatter schema inspiration, 32 personas |
| Qodo 2.0 / PR-Agent | qodo.ai, github.com/qodo-ai/pr-agent | Apache 2.0 | 15+ agent pool, orchestrator selection pattern |
| Ellipsis AI | ellipsis.dev | Closed | "Hundreds of agents" scale reference |
| Diffray | diffray.ai | Closed | 11 agents, file-type self-filtering |
| DyLAN | arxiv.org/abs/2310.02170 | Research | Agent importance scoring |
| MasRouter | aclanthology.org/2025.acl-long.757/ | Research | Cascaded routing controller |
| Wharton "Playing Pretend" | arxiv.org/abs/2512.05858 | Research | Personas hurt coding tasks |
| PRISM | arxiv.org/abs/2603.18507 | Research | Dynamic persona routing |
| Rethinking Multi-Agent | arxiv.org/abs/2601.12307 | Research | Homogeneous multi-agent ≈ single agent |
