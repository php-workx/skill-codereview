# Handoff: Security Explorer Improvement

## Context

We built a full evaluation infrastructure (Martian CodeReBench + OWASP Benchmark) and have baseline measurements. The OWASP results reveal a clear gap: **6 CWE categories have zero coverage from deterministic tools** because they require data-flow reasoning that only paid tools (Semgrep Pro) or AI can do. This is the highest-leverage improvement area for the skill.

## The Gap (from OWASP Python benchmark)

Our semgrep baseline: **Youden +0.073** (beats Bandit and Bearer, but well below Semgrep Pro ~+0.25).

| CWE | Category | Semgrep | AI (50-file test) | Why tools miss it |
|-----|----------|---------|-------------------|-------------------|
| 89 | SQL Injection | 0 TP | untested | Input flows through configparser/dict indirection before reaching cursor.execute() |
| 90 | LDAP Injection | 0 TP | untested | Similar indirection patterns |
| 330 | Weak Randomness | 0 TP | 7 TP, 0 FP | Must distinguish random.Random() (weak) from random.SystemRandom() (secure) |
| 501 | Trust Boundary | 0 TP | untested | Session attribute manipulation — pattern-matchers don't model trust boundaries |
| 643 | XPath Injection | 0 TP | 2 TP, 2 FP | String concat in XPath queries with indirect input |
| 79 | XSS | 0 TP | untested | Template injection through indirect data flow |

**AI already proven**: On the SQL injection test, Claude correctly identified "the configparser round-trip is a red herring" and traced user input through to cursor.execute(). No OSS tool catches this. On weak randomness, AI got 7 TP with 0 FP — perfect precision where Bandit had 62 FP.

## What Needs to Happen

### 1. Research security approaches from other projects

Gather all security-related prompts, personas, and CWE checklists from:

| Project | Where to look | What to extract |
|---------|--------------|-----------------|
| **Kodus-AI** | `~/workspaces/kodus-ai` — look for security-specific prompts in the evals and code review pipeline | CWE detection patterns, taint flow instructions, false positive avoidance |
| **PR-Agent (Qodo)** | `~/workspaces/pr-agent` — the `/review` and `/improve` commands have security-specific logic | Vulnerability categories, severity mapping, how they classify security vs non-security |
| **Claude Octopus** | Research needed — this had multiple specialized reviewers including a dedicated security persona | How many security personas? What CWEs does each cover? How do they avoid overlap? |
| **AgentOps** | Check if there are security-specific patterns in the vibe/security skills | Security scanning approach, scanner integration |
| **CodeRabbit** | Already researched (see `docs/research-coderabbit-architecture.md`) | ast-grep security rules, verification agent approach |

### 2. Consider splitting the security explorer

Current: one `reviewer-security-pass.md` covers everything.

Proposed split based on OWASP findings:

**Security Explorer A: "Data Flow" (taint analysis)**
- SQL/Command/LDAP/XPath injection (CWE-89, 78, 90, 643)
- XSS through template injection (CWE-79)
- Path traversal (CWE-22)
- Open redirect (CWE-601)
- Key skill: trace user input → dangerous sink, checking for sanitization/validation at each step
- Prompt should include explicit taint-tracking protocol: "identify sources (request params, cookies, env vars), identify sinks (execute, open, render), trace the path between them"

**Security Explorer B: "Configuration & Crypto" (pattern recognition)**
- Weak hashing/crypto (CWE-327, 328)
- Weak randomness (CWE-330)
- Insecure cookies (CWE-614)
- Trust boundary violations (CWE-501)
- Hardcoded secrets, insecure defaults
- Key skill: recognize secure vs insecure API choices (SecureRandom vs Random, parameterized queries vs string concat)

### 3. Measurement loop

Everything is in place to measure improvement:

```bash
# After changing security explorer prompts:
python3 scripts/eval-owasp.py review --lang python    # ~15 min, 1230 test cases
python3 scripts/eval-owasp.py score --lang python
python3 scripts/eval-owasp.py report --lang python    # Youden per CWE category

# For full code review quality:
python3 scripts/eval-martian.py review --resume        # re-run changed PRs
python3 scripts/eval-martian.py judge
python3 scripts/eval-martian.py report
```

Target: Youden > +0.20 on OWASP Python (competitive with Semgrep Pro).

## Files to Read

- `skills/codereview/prompts/reviewer-security-pass.md` — current security explorer prompt
- `skills/codereview/prompts/reviewer-global-contract.md` — shared investigation protocol
- `docs/research-coderabbit-architecture.md` — CodeRabbit's security approach
- `docs/research-evaluation-framework.md` — benchmark landscape and scoring
- `scripts/eval-owasp.py` — OWASP benchmark runner
- `scripts/eval-martian.py` — Martian benchmark runner
- `scripts/eval_store.py` — shared SQLite analytics

## Files to Modify

- `skills/codereview/prompts/reviewer-security-pass.md` — improve or split
- `skills/codereview/SKILL.md` — add second security explorer if splitting
- `skills/codereview/rules/ast-grep/` — potential for new AST-level security rules

## Open Items from This Session

1. **Martian judge still running old sequential version** — the new batched+parallel judge is ready but the full 34-PR results were scored by the old one. Re-run `judge` with the new code for faster scoring.
2. **12 failed PRs** — Discourse (10 commit-based, now fixed in code) + sentry-5 (fork-only) + 1 timeout. The commit-based fix is in the code but those PRs haven't been reviewed yet. Run `review --resume` to pick them up.
3. **OWASP AI review parsing** — 4/5 batches parse correctly after the markdown fence fix, 1/5 still fails. The JSON extraction could be further improved.
4. **Classify command** — built but not yet run on the full Martian results. This would tell us real precision (how many "false positives" are actually valid findings the golden set missed).
5. **Full OWASP AI review** — only ran on 50 test cases. The full 1,230 would give definitive AI vs semgrep comparison.
