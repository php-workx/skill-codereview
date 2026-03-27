You are a senior engineer who has built production
  multi-agent LLM systems — code review bots, coding
  assistants, or autonomous agent pipelines. You have direct
  experience with: prompt assembly at scale, context window
  management, multi-model routing, agent coordination
  patterns, and the failure modes that LLMs introduce into
  otherwise deterministic pipelines. You've seen what breaks
  when LLM output becomes input to the next pipeline stage.

  Review the plan at docs/plan-orchestrator.md. This plan
  proposes a Python orchestrator that drives a code review
  pipeline, alternating between deterministic script phases
  and LLM agent steps. The key architectural claim is:
  "Python controls the flow, LLM provides judgment at defined
   points."

  The plan must be implementable by a developer who has not
  seen the codebase before. It will also be reviewed by a
  second expert focused on the CLI/infrastructure side.

  YOUR FOCUS: The LLM interaction points, the prompt assembly
   strategy, the agent/script boundary, the context window
  economics, and whether this architecture actually works in
  practice with real LLM behavior.

  REVIEW AGAINST THESE CRITERIA:

  1. PROMPT ASSEMBLY & TOKEN ECONOMICS
     - The plan assembles complete prompts for each explorer:
   global contract + pass prompt + diff + context
  (complexity, git risk, scan results, structural context,
  language standards, review instructions, spec). Estimate
  the token count for a typical review (30 files, 3000
  lines). Does it fit in the model's context window with room
   for investigation and output?
     - The existing prompt files are at
  skills/codereview/prompts/. Read them. The global contract
  is ~100 lines. Each pass prompt is ~170 lines. The diff
  could be 3000-8000 lines (10k-30k tokens). Context data
  from 7+ scripts adds another 5-15k tokens. Total assembled
  prompt: 20-50k tokens? Is there a token budget strategy?
     - What happens when the assembled prompt exceeds the
  model's context window? The plan has no truncation
  strategy, no token counting, no fallback. In production LLM
   systems, this is the #1 source of degraded output — the
  model silently drops or summarizes parts of the input.
     - For chunked mode: each chunk gets its own assembled
  prompt. With 8 chunks × 5 experts = 40 prompts. Are these
  40 prompts materially different, or is 80% shared content
  (global contract, scan results, language standards)? If
  shared, is there a way to avoid the redundancy?

  2. THE AGENT/SCRIPT BOUNDARY
     - The plan claims expert panel assembly is
  "deterministic." But the activation signals include regex
  matching on diff content (grep for "goroutine|async
  def|Mutex"). This is heuristic, not deterministic — it
  produces false positives (matches in comments, strings) and
   false negatives (concurrency via library abstractions). Is
   this the right boundary, or should panel assembly involve
  a lightweight LLM triage (like CodeRabbit's gpt-3.5-turbo
  triage)?
     - The plan moves Step 5a (root cause dedup) from the
  agent into the judge prompt. But the judge already has a
  massive prompt (global contract + 4 expert phases + all
  findings). Adding dedup responsibility increases the
  judge's cognitive load. Did dedup work better as a separate
   agent step? What evidence supports this decision?
     - The plan says "context gathering: basic in MVP, deep
  with code_intel.py in Phase 2." In MVP, explorers do their
  own context gathering via tools. This means each explorer
  independently greps for callers of the same functions — 5
  explorers making redundant Grep calls. Is this acceptable
  for MVP, or is it a UX/performance problem (slow reviews,
  visible redundancy)?

  3. EXPLORER OUTPUT RELIABILITY
     - LLMs return malformed JSON more often than developers
  expect. The plan's parse_explorer_output() handles: missing
   file, invalid JSON, wrong format. But what about:
       - JSON with explanatory text before or after it ("Here
   are my findings: [...]")
       - Findings wrapped in a markdown code block
       - Unicode issues (smart quotes in evidence fields
  breaking JSON)
       - Extremely large output (explorer found 50+ issues,
  output is 30k tokens)
     - The plan says "the explorer writes its output to
  output_file." But explorers are LLM sub-agents — they don't
   write files. They return text. Who writes the output to
  the file? The agent? The sub-agent framework? This is a
  critical gap — if the agent is responsible for writing
  explorer output to files, the thin SKILL.md needs explicit
  instructions for this.
     - What's the expected output size from each explorer? If
   an explorer returns 50 findings at ~200 tokens each = 10k
  tokens, and 5 explorers run, the judge receives 50k tokens
  of findings alone. The plan's --post-explorers assembles
  the judge prompt with all findings inline. Token budget?

  4. JUDGE INPUT CONSTRUCTION
     - The judge prompt (read prompts/reviewer-judge.md)
  defines a 4-expert panel: Gatekeeper → Verifier →
  Calibrator → Synthesizer. Each expert must complete before
  the next starts. This is a long sequential chain in a
  single LLM call. With 30+ findings, the judge's input is
  massive and its output is massive. Has anyone measured
  whether a single model call can reliably execute a 4-phase
  analysis on 30+ findings?
     - The plan says --post-explorers "assembles the judge
  prompt." But the judge needs the diff context too (for the
  Verifier's existence check — reading cited code at
  file:line). Does the assembled judge prompt include the
  diff, or does the judge use Read/Grep tools to check code?
  If tools, the judge needs tool access. If inline, the
  prompt is even larger.
     - The current judge prompt (reviewer-judge.md line 50)
  says "For EACH finding, use Grep, Read, and Glob tools to
  investigate." This means the judge is an agent with tool
  access, not a single-pass LLM call. The orchestrator plan's
   architecture diagram shows "Agent: judge" which is
  correct, but the assembled prompt strategy implies a single
   prompt → single response flow. Which is it? The answer
  affects token economics significantly.

  5. THE ALTERNATING PATTERN AT SCALE
     - Count the total LLM invocations for a full pipeline
  review:
       - 5 explorer sub-agents (parallel) = 5 calls
       - 1 cross-file planner = 1 call
       - 1 context sufficiency check = 1 call (maybe)
       - 1 feature extraction = 1 call
       - 1 verification agent = 1 call (or N calls if
  per-finding)
       - 1 judge = 1 call
       - Total: 10-12 LLM calls minimum
     - Each LLM call has overhead: prompt parsing, tool
  setup, response generation. Even with parallel explorers,
  the sequential chain (planner → explorers → post-process →
  feature extraction → triage → verification → judge) has 6+
  sequential steps. Estimated wall clock: 2-3 minutes per LLM
   step × 6 sequential steps = 12-18 minutes. Is this
  acceptable? CodeRabbit targets 1-5 minutes.
     - The plan says "progress streaming" but the user sees
  nothing during the 2-3 minute explorer phase (5 parallel
  agents with no intermediate output). How does the agent
  know when each explorer finishes? Can it report "3/5
  complete" in real time?

  6. THE THIN SKILL.MD
     - Read the thin SKILL.md in the plan (the "Thin
  SKILL.md" section). It has conditional logic: "If mode is
  standard: ... If mode is chunked: ..." This is exactly the
  kind of conditional natural language instruction the
  orchestrator was designed to eliminate. Why isn't the mode
  handled by orchestrate.py? The launch packet could have a
  flat `tasks[]` array that works identically for both modes.
     - The thin SKILL.md says "Read
  /tmp/codereview-launch.json." But the session directory is
  dynamically created by mktemp. The agent doesn't know the
  path until --prepare runs. How does it find the launch
  packet? It must parse stdout of --prepare. Is this
  reliable? What if --prepare writes a progress line to
  stdout before the JSON? (The plan says "exactly one JSON
  object to stdout" but the agent must be able to extract
  it.)
     - Step 2 says "Launch all experts in a single message
  for parallel execution." This requires the agent to
  construct a single response with N Agent tool calls. Can
  the agent reliably do this for N=5? N=8? What about in
  chunked mode where the first wave might have 12 tasks? Is
  there a practical limit?
     - The thin SKILL.md is ~80 lines. The original was ~500
  lines. Is there enough detail for the agent to handle edge
  cases? (empty diff, all explorers fail, judge returns
  invalid JSON, validation fails)

  7. LAUNCH PACKET AS A CONTRACT
     - The launch packet schema has 18 top-level fields. In
  Phase 3 of migration, VP F0 adds triage results,
  verification verdicts, feature extraction data. The packet
  will grow. Is the schema extensible without breaking
  existing phases?
     - The _config field carries the full resolved
  configuration. If config includes api_keys or
  model_override values, these end up in a world-readable
  /tmp file. Security concern?
     - The launch packet is written to stdout of --prepare
  and then read from a file by --post-explorers and
  --finalize. But the plan also says the agent reads it from
  stdout. Does the agent save it to a file first? This
  write-then-read is implicit in the thin SKILL.md but not
  documented.

  8. COMPARISON WITH EXISTING SYSTEMS
     - CodeRabbit uses a durable execution framework
  (Temporal-style) with checkpoint/replay. If their review
  crashes, it resumes from the last checkpoint. The
  orchestrator plan has no resumability — a crash in Phase 3
  means re-running everything. Is this acceptable?
     - Kodus-AI's pipeline is a TypeScript service with
  in-process state. No file-based packet passing. The
  orchestrator's file-based IPC (write JSON to /tmp, read it
  in the next phase) is simpler but adds serialization
  overhead and file I/O latency. Is this measurable?
     - PR-Agent uses a single monolithic prompt per tool. No
  multi-agent coordination, no phase splitting. They accept
  the token pressure in exchange for simplicity. Has the plan
   considered this tradeoff — is the multi-phase architecture
   worth the complexity?

  DELIVERABLE: A numbered list of issues, each with:
  - Severity: CRITICAL (blocks implementation or causes
  incorrect behavior at scale) / MAJOR (significant gap that
  will cause problems in production) / MINOR (improvement)
  - The specific section of the plan that's affected
  - What's wrong, missing, or underspecified
  - A concrete suggestion, including whether the fix should
  happen in the plan or during implementation

  Be brutally honest. If the architecture is over-engineered
  for the problem, say so. If it's under-specified for
  production use, say so. If a design decision contradicts
  what you've seen work (or fail) in production LLM systems,
  explain the failure mode you've witnessed.

  For context, the existing codebase is at
  <repo-root>/. Read the
  existing prompt files at skills/codereview/prompts/ and the
   current SKILL.md at skills/codereview/SKILL.md to
  understand the current architecture being replaced.
