# Spec B: Per-Expert Enrichment

**Status:** Draft (revised after pre-mortem — dynamic generation deferred)
**Author:** Research session 2026-03-28
**Depends on:** Spec A Wave 0 (ExpertRegistry, SelectedExpert), Spec A Wave 1 (language + domain checklists)
**Related:** `specs/adaptive-expert-selection.md` (Spec A), `docs/plan-context-enrichment.md`

## Why This Is a Separate Spec

The Spec A pre-mortem (4-judge council, 33 findings) identified a clean architectural seam: Spec A is fully deterministic (frontmatter parsing, signal-based selection, coverage gap detection). This spec adds per-expert routing of enrichment content. They were split because Spec A's value (expanded roster, better selection) stands on its own.

## What's Deferred: Dynamic Expert Generation

The Spec B pre-mortem (4-judge council, 23 findings) recommended deferring dynamic expert generation until Spec A's `detect_coverage_gaps()` provides real frequency data on how often coverage gaps occur after the expanded roster ships. The coverage map already tells the judge about gaps — that may be sufficient.

**Decision:** Dynamic generation (the original Feature 2 and Feature 3 of this spec) is deferred to a future addendum. The full design, research background, template, LLM call details, caching strategy, judge skepticism rules, and SKILL.md integration flow are preserved in Appendix A of this document for when generation is needed. The pre-mortem recommended moving generation into an `orchestrate.py generate-expert` subcommand rather than SKILL.md; that recommendation is noted in the appendix.

**Trigger for revisiting:** If >10% of reviews after Spec A ships show meaningful coverage gaps where a generated expert would have caught issues that static experts missed, build generation.

## Problem

Even with Spec A's expanded roster (15 experts, 5 new domain specialists), all experts get the same global context regardless of what specific technologies the diff uses. The `database` expert reviewing a PostgreSQL migration gets the same checklists as the `concurrency` expert. Language-specific footguns (Go goroutine leaks, Python mutable default args) exist as checklists but have no mechanism to reach the right experts.

## Goals

1. **Per-expert enrichment** — Route language checklists, domain checklists, and (when available) prescan signals to the experts that need them, instead of injecting everything globally

## Non-Goals

- Dynamic expert generation via LLM (deferred — see above)
- Judge skepticism rules for generated experts (deferred with generation)
- Replacing Spec A's selection mechanism (enrichment builds on it)
- Changing the `assemble_explorer_prompt()` function signature
- Generating persona identities (Wharton "Playing Pretend" study — personas hurt coding tasks)

---

## Background: Research That Informed This Design

### Baz Awesome Reviewers as Enrichment Source

4,468 code review rules mined from real GitHub PR discussions (github.com/baz-scm/awesome-reviewers). Each rule has YAML frontmatter (`title`, `description`, `label`, `language`, `repository`, `comments_count`, `repository_stars`) and companion JSON with full PR discussion provenance.

We curated language checklists from this dataset in Spec A Wave 1: Go (25 items from 30 rules across 16 repos), Python (24 items from 30 rules across 15 repos). TypeScript, Rust, Java planned.

**Selection approach:** Build-time curation (label filter + manual review) produces 15-25 item checklists per language/domain. Inject the full curated checklist at runtime (~300-500 tokens each). The LLM naturally focuses on relevant items. Keyword indexing is the upgrade path if checklists grow beyond ~30 items per file.

### The Persona Paradox

The Wharton/USC "Playing Pretend" study (arXiv:2512.05858, March 2026) found generic expert personas hurt coding task performance by 32%. The PRISM follow-up showed dynamic routing between persona and base model is the fix. Our enrichment approach aligns: inject specific review criteria and checklists, not persona identity.

---

## Design

### Data Types

```python
@dataclass(frozen=True)
class ChecklistMeta:
    """Parsed frontmatter from a domain/language checklist file."""
    name: str = ""
    relevant_domains: list[str] = field(default_factory=list)
    relevant_experts: list[str] = field(default_factory=list)
    # Note: relevant_experts contains static expert names only.
    # Generated experts (if ever built) match via relevant_domains only,
    # since their names are dynamic.


class ChecklistRegistry:
    """Loads and caches ChecklistMeta from checklist files.

    Follows the same pattern as Spec A's ExpertRegistry:
    directory + glob + frontmatter parse + cache.
    """

    def __init__(self, references_dir: Path) -> None:
        self._dir = references_dir
        self._checklists: list[tuple[Path, ChecklistMeta | None]] | None = None

    def load(self) -> list[tuple[Path, ChecklistMeta | None]]:
        """Parse all checklist-*.md files. Cache result.

        Returns list of (path, meta) tuples. meta is None when
        frontmatter is absent or unparseable (backward-compatible:
        these checklists are injected globally).
        """
        if self._checklists is not None:
            return self._checklists
        self._checklists = []
        for path in sorted(self._dir.glob("checklist-*.md")):
            content = path.read_text()
            meta = parse_checklist_frontmatter(content)
            self._checklists.append((path, meta))
        return self._checklists
```

### Checklist Frontmatter

Domain checklists get lightweight YAML frontmatter for matching:

```yaml
---
name: sql-safety
relevant_domains: [sql, orm, migrations, database]
relevant_experts: [database, security-dataflow, correctness]
---
```

- `relevant_domains` — matched against `expert.domains` from Spec A's ExpertMeta
- `relevant_experts` — explicit override: always inject into these experts regardless of domain match
- Both empty or no frontmatter → checklist is injected into all experts (backward-compatible)
- Unknown names in `relevant_experts` are silently ignored (not errors)

```python
def parse_checklist_frontmatter(text: str) -> ChecklistMeta | None:
    """Extract YAML frontmatter from a checklist markdown file.

    Returns None (not raises) when:
    - No `---` delimiters found (existing checklists have no frontmatter)
    - yaml module not available
    - YAML parse error
    - Parsed data is not a dict

    Reuses _FRONTMATTER_RE from Spec A's parse_expert_frontmatter().
    """
    if yaml is None:
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return ChecklistMeta(
        name=data.get("name", ""),
        relevant_domains=data.get("relevant_domains", []),
        relevant_experts=data.get("relevant_experts", []),
    )
```

### Enrichment Matching

```python
# Language checklist file extension mapping
LANG_CHECKLIST_MAP: dict[str, str] = {
    ".go": "checklist-go.md",
    ".py": "checklist-python.md",
    ".ts": "checklist-typescript.md",
    ".tsx": "checklist-typescript.md",
    ".rs": "checklist-rust.md",
    ".java": "checklist-java.md",
    ".rb": "checklist-ruby.md",
}

def match_enrichments(
    expert: dict[str, Any],       # current expert dict format (not SelectedExpert)
    file_extensions: set[str],    # from ReviewSignals or DiffResult
    checklist_registry: ChecklistRegistry,
) -> str:
    """Select enrichment content relevant to this specific expert.

    Returns a single joined string (compatible with existing
    domain_checklists: str parameter on assemble_explorer_prompt).

    Uses expert.get("domains", []) for domain matching — works with
    both the current dict format and future SelectedExpert objects.
    """
    sections: list[str] = []
    expert_name = expert.get("name", "")
    expert_domains = set(expert.get("domains", []))
    total_tokens = 0

    # 1. Language checklists — by file extension
    # Inject ALL matching languages (Go + Python for a mixed diff).
    # Language footguns are concern-agnostic — relevant to all experts.
    seen_lang: set[str] = set()
    for ext in file_extensions:
        filename = LANG_CHECKLIST_MAP.get(ext)
        if filename and filename not in seen_lang:
            for path, meta in checklist_registry.load():
                if path.name == filename:
                    content = path.read_text()
                    tokens = len(content) // 4
                    if total_tokens + tokens <= 1500:  # P5 budget
                        sections.append(content)
                        total_tokens += tokens
                        seen_lang.add(filename)
                    break

    # 2. Domain checklists — by frontmatter matching
    for path, meta in checklist_registry.load():
        if path.name in LANG_CHECKLIST_MAP.values():
            continue  # already handled above
        if path.name in seen_lang:
            continue

        content = path.read_text()

        if meta is None:
            # No parseable frontmatter — inject globally (backward-compatible)
            tokens = len(content) // 4
            if total_tokens + tokens <= 1500:
                sections.append(content)
                total_tokens += tokens
            continue

        if not meta.relevant_domains and not meta.relevant_experts:
            # Frontmatter exists but both fields empty — inject globally
            tokens = len(content) // 4
            if total_tokens + tokens <= 1500:
                sections.append(content)
                total_tokens += tokens
            continue

        # Explicit expert override: relevant_experts always wins
        if expert_name in meta.relevant_experts:
            tokens = len(content) // 4
            if total_tokens + tokens <= 1500:
                sections.append(content)
                total_tokens += tokens
            continue

        # Domain overlap matching
        if expert_domains and set(meta.relevant_domains) & expert_domains:
            tokens = len(content) // 4
            if total_tokens + tokens <= 1500:
                sections.append(content)
                total_tokens += tokens

    # 3. Prescan signals — routed by pattern category
    # When prescan.py is available, route signals to experts by category:
    #   P-SEC → experts with "security" in domains
    #   P-ERR → experts with "error-handling" in domains
    #   P-PERF → experts with "performance" or "reliability" in domains
    #   P-* (all others) → all experts
    # Implementation deferred until prescan.py ships. The routing taxonomy
    # is defined here so prescan output format can be designed to support it.

    return "\n\n".join(sections)
```

**Key design decisions:**

- **Returns `str`, not `list[str]`** — Compatible with existing `domain_checklists: str` parameter on `assemble_explorer_prompt()`. No signature change needed. No test breakage. (Pre-mortem F5)
- **Uses `expert.get("domains", [])` not `expert.domains`** — Works with current dict format before Spec A lands. (Pre-mortem F21)
- **None frontmatter → inject globally** — Matches degradation matrix. Existing checklists (sql-safety, concurrency, llm-trust) have no frontmatter and will continue to be injected into all experts until frontmatter is added. (Pre-mortem F4)
- **P5 budget enforced in `match_enrichments()`** — Uses `len(content) // 4` consistent with `PromptContext.estimate_tokens()`. Cap at 1,500 tokens per expert. Overflow drops later domain checklists (language checklists are added first, so they're never dropped). (Pre-mortem F23)
- **Multi-language diffs** — A diff with `.go` and `.py` files injects both Go and Python checklists into every expert. Language footguns are concern-agnostic.
- **Prescan routing taxonomy defined** — P-SEC→security, P-ERR→error-handling, P-PERF→reliability. Wiring deferred until prescan.py ships, but the schema decision is locked now so prescan output format can be designed to support it. (Pre-mortem F19)

### Injection Point

Enrichments are injected at the current `domain_checklists` position in `PromptContext.render()` — within the Context section, **after** the diff (matching current render order). No reorder needed. (Pre-mortem F11)

```markdown
## Context
{diff content}
{changed files, complexity, git risk}

### Domain-Specific Checklists        ← enrichments go here (existing position)
{per-expert enrichments from match_enrichments()}
```

### Token Budget Integration

Enrichments live at **P5** in the existing priority cascade (same tier and position as `domain_checklists` today):

| Priority | Content | Budget | Truncation strategy |
|----------|---------|--------|-------------------|
| P0 | Global contract + pass prompt | 6,000 | Never truncated |
| P1 | Formatted diff | 35,000 | Truncate to changed hunks only |
| P2 | Changed files + complexity + git risk | 2,500 | Summarize to counts/tiers |
| P3 | Cross-file planner results | 5,000 | Drop low-risk queries first |
| P4 | Prescan signals | 1,500 | Critical/high only |
| **P5** | **Domain + language checklists (enrichments)** | **1,500** | **Enforced in `match_enrichments()` pre-truncation** |
| P6 | REVIEW.md directives | 800 | Truncate to "Always check" only |
| P7 | Path instructions | 500 | Drop instructions for low-risk files |

**Pre-truncation in `match_enrichments()`:** The P5 cap is enforced inside the function, not by the cascade. This means enrichments never exceed 1,500 tokens regardless of total prompt budget. The existing cascade entry for `domain_checklists` remains as a safety net for total budget pressure.

**Worst case:** 2 language checklists (500 tokens each) + 1 domain checklist (500 tokens) = 1,500 tokens.

### Integration with Existing Code

| Current code | Location | Change |
|-------------|----------|--------|
| `load_domain_checklists()` | orchestrate.py, called from `prepare()` | **Kept as-is** — remains the global fallback. Not renamed, not removed. |
| `select_checklists()` | orchestrate.py:440 | **Kept as-is** — the regex-based selection still works for the global path. |
| `assemble_explorer_prompt()` | orchestrate.py:983 | **Signature unchanged** — `domain_checklists: str` parameter stays. Receives output of `match_enrichments()` (a joined string). |
| `PromptContext.domain_checklists` | orchestrate.py:180 | **Type unchanged** — remains `str`. |
| `PromptContext.render()` | orchestrate.py:205 | **Unchanged** — renders `domain_checklists` under `### Domain-Specific Checklists`. |
| `check_token_budget()` cascade | orchestrate.py:1071 | **Unchanged** — `domain_checklists` entry in cascade is safety net. |
| Per-expert loop in `prepare()` | orchestrate.py:2255-2275 | **Changed** — calls `match_enrichments()` per expert instead of passing global `domain_checklists` |

**Migration path:**
```python
# BEFORE (current code):
domain_checklists = load_domain_checklists(diff_result.diff_text)
for expert in experts:
    prompt = assemble_explorer_prompt(..., domain_checklists=domain_checklists)

# AFTER (this spec):
checklist_reg = ChecklistRegistry(SKILL_DIR / "references")
file_exts = {Path(f).suffix for f in diff_result.changed_files if Path(f).suffix}
# Global fallback computed once (used if per-expert matching fails for any expert)
global_checklists = load_domain_checklists(diff_result.diff_text)

for expert in experts:
    try:
        enriched = match_enrichments(expert, file_exts, checklist_reg)
    except Exception:
        progress("enrichment_fallback", reason="match_enrichments raised")
        enriched = global_checklists
    if not enriched:
        enriched = global_checklists
    prompt = assemble_explorer_prompt(..., domain_checklists=enriched)
```

**Key property:** `load_domain_checklists()` and all its tests remain untouched. It becomes the fallback path. `match_enrichments()` is a new function that produces the same type (`str`) for the same parameter.

### Chunked Mode Interaction

In chunked mode, each chunk gets its own wave and expert set. Per-expert enrichment operates per-chunk:

- **Language checklists:** Filtered to the chunk's file extensions (a chunk with only `.go` files gets only `checklist-go.md`, not `checklist-python.md`)
- **Domain checklists:** Matched against the expert's domains (unchanged — domains are per-expert, not per-chunk)
- **File extensions for `match_enrichments()`:** Derived from the chunk's file set, not the full diff

```python
# Chunked mode:
for chunk in chunks:
    chunk_exts = {Path(f).suffix for f in chunk.files if Path(f).suffix}
    for expert in chunk.experts:
        enriched = match_enrichments(expert, chunk_exts, checklist_reg)
        # ...
```

### Frontmatter for Existing Checklists

The 3 existing checklists have no frontmatter today. Wave 1 adds it. Here are the specific values:

| Checklist | `relevant_domains` | `relevant_experts` | Current regex trigger |
|-----------|--------------------|--------------------|----------------------|
| `checklist-sql-safety.md` | [sql, orm, database, migrations] | [database, security-dataflow, correctness] | SQL keywords in diff |
| `checklist-concurrency.md` | [async, threading, locks, channels, parallel-processing] | [concurrency, correctness] | Concurrency primitives in diff |
| `checklist-llm-trust.md` | [llm, ai, prompts, embeddings] | [ai-integration, security-dataflow, correctness] | LLM API calls in diff |

Once frontmatter is added, these checklists are routed per-expert by `match_enrichments()` instead of globally by `select_checklists()`. The existing `_CHECKLIST_PATTERNS` regex triggers in `select_checklists()` (orchestrate.py:441-463) become dead code on the primary path — they only fire in the `enrichment_mode: "global"` fallback. The regexes are NOT removed; they serve as the fallback mechanism.

### Tier 2 Folding

Spec A's Decisions section defines how Tier 2 expert candidates (performance, authorization, observability, etc.) are folded into existing experts via domain checklists. This spec implements that folding:

| Tier 2 candidate | Checklist | `relevant_domains` | `relevant_experts` |
|-------------------|-----------|--------------------|--------------------|
| performance | `checklist-performance.md` | [performance, optimization, caching] | [reliability, correctness] |
| observability | `checklist-observability.md` | [observability, logging, metrics, tracing] | [reliability] |
| dependencies | `checklist-dependencies.md` | [dependencies, supply-chain] | [security-config] |
| authorization | (added to existing security prompts) | N/A | N/A |
| data-pipeline | `checklist-data-pipeline.md` | [pipeline, etl, streaming, kafka] | [correctness] |
| migration-safety | (subsection of database expert) | N/A | N/A |

### Interaction with `drop_least_relevant_checklist`

The existing truncation cascade includes `drop_least_relevant_checklist` (orchestrate.py:417) which splits `domain_checklists` on `###` headings and drops the last section. Under per-expert enrichment, `match_enrichments()` already enforces the 1,500 token P5 cap before returning. So `drop_least_relevant_checklist` would only fire if total prompt pressure exceeds the 70k budget — and it would operate on already-filtered content. This is correct behavior; no changes needed to the cascade function.

---

## Degradation Matrix

| Failure | Capabilities Lost | Fallback Behavior | Log Message |
|---------|-------------------|-------------------|-------------|
| **`yaml` not installed** | Checklist frontmatter parsing | `parse_checklist_frontmatter()` returns `None` for all; checklists injected globally | (implicit — same as "no frontmatter" case) |
| **Single checklist frontmatter malformed** | Domain matching for that checklist | Checklist injected into all experts (global) | `"checklist_frontmatter_warning"` event with `{file, error}` |
| **No frontmatter on checklist** | Per-expert routing for that checklist | Checklist injected into all experts (global, backward-compatible) | (no warning — this is expected during migration) |
| **`match_enrichments()` raises exception** | Per-expert enrichment for that expert | Falls back to `load_domain_checklists()` global string | `"enrichment_fallback"` event with `{reason}` |
| **All enrichment matching fails** | Per-expert enrichment | All checklists injected globally (current behavior) | `"enrichment_fallback"` event |
| **P5 budget exceeded** | Later checklists dropped | Domain checklists dropped in insertion order (language checklists kept) | `"enrichment_overflow"` event |
| **prescan.py unavailable** | Prescan signal enrichment | Only language + domain checklists injected | `"prescan unavailable — enrichments limited to checklists"` |

**Key principle:** No failure in enrichment causes the review to fail. Each capability degrades to global injection (current behavior).

---

## Logging / Observability

| Event | When | Payload |
|-------|------|---------|
| `enrichment_matched` | Checklist matched to expert | `{expert, checklist, match_type: "language"\|"domain"\|"explicit"\|"global"}` |
| `enrichment_skipped` | Checklist didn't match expert | `{expert, checklist, reason}` |
| `enrichment_overflow` | P5 budget exceeded, checklist dropped | `{expert, dropped_checklist, budget_used, budget_limit}` |
| `enrichment_fallback` | Matching failed, using global injection | `{reason}` |
| `checklist_frontmatter_warning` | Checklist frontmatter parse failure | `{file, error}` |
| `checklist_registry_loaded` | After registry.load() | `{total, with_frontmatter, without_frontmatter}` |

**Debugging "why didn't my expert get the Go checklist?":** Check stderr for `enrichment_matched` / `enrichment_skipped` events. The `match_type` field shows the matching path.

---

## Implementation Plan

### Wave 1: Per-Expert Enrichment

**Depends on:** Spec A Wave 0 (ExpertRegistry, SelectedExpert with `domains` field), Spec A Wave 1 (language + domain checklists exist as files)

1. Define `ChecklistMeta` dataclass and `ChecklistRegistry` class in orchestrate.py
2. Implement `parse_checklist_frontmatter()` (reuse `_FRONTMATTER_RE` from Spec A)
3. Add lightweight frontmatter to existing domain checklists (`checklist-sql-safety.md`, `checklist-concurrency.md`, `checklist-llm-trust.md`)
4. Add frontmatter to new Spec A Wave 1 checklists (`checklist-go.md`, `checklist-python.md`, `checklist-performance.md`, `checklist-observability.md`, `checklist-dependencies.md`)
5. Implement `match_enrichments()` in orchestrate.py with P5 budget enforcement
6. Wire `match_enrichments()` into the per-expert loop in `prepare()`, with `load_domain_checklists()` as fallback
7. Add `enrichment_mode: "per-expert" | "global"` to `CONFIG_ALLOWLIST` and `DEFAULT_CONFIG` (default: `"per-expert"`) — allows rollback to global injection
8. Tests (see acceptance criteria)

### Wave 2: Synthesizer Coverage Completeness (moved from Spec A)

**Depends on:** Spec A Wave 2 (coverage gap detection, coverage map in judge context)

1. Add `### 4e. Coverage Completeness` step to the Synthesizer in `reviewer-judge.md`:
   - For each uncovered domain in the coverage map: note in verdict
   - "Note: No {domain} specialist reviewed this change. Consider manual review of {files}."
2. This is the non-generated-expert portion of the original Feature 3. The generated-expert skepticism rules are deferred with generation.

---

## Acceptance Criteria

### Wave 1: Per-Expert Enrichment
- [ ] `ChecklistMeta` dataclass exists with `name`, `relevant_domains`, `relevant_experts` fields
- [ ] `ChecklistRegistry.load()` returns all `checklist-*.md` files with parsed frontmatter
- [ ] `parse_checklist_frontmatter()` returns `None` for files without frontmatter (no crash)
- [ ] `parse_checklist_frontmatter()` returns `None` when `yaml` is not installed
- [ ] Existing checklists (no frontmatter) are injected into all experts (global, backward-compatible)
- [ ] `match_enrichments()` returns language checklist content when diff has matching extension
- [ ] `match_enrichments()` returns `checklist-sql-safety.md` for database expert (via `relevant_domains`) but NOT for concurrency expert
- [ ] `match_enrichments()` returns checklist for expert named in `relevant_experts` regardless of domain
- [ ] Multi-language: diff with `.go` + `.py` files → both Go and Python checklists included
- [ ] `match_enrichments()` returns `str` compatible with existing `domain_checklists` parameter
- [ ] `assemble_explorer_prompt()` signature is **unchanged**
- [ ] `load_domain_checklists()` and all its existing tests pass unchanged
- [ ] Total enrichment tokens per expert ≤ 1,500 (measured by `len(content) // 4`)
- [ ] When `match_enrichments()` raises, fallback to `load_domain_checklists()` + warning logged
- [ ] `enrichment_mode: "global"` in config forces global injection (bypass per-expert matching)
- [ ] Each checklist with frontmatter: test that it routes to expected experts and not to others

### Wave 2: Synthesizer Coverage Completeness
- [ ] Judge prompt contains `### 4e. Coverage Completeness` section
- [ ] Synthesizer notes uncovered domains in verdict when coverage gaps exist
- [ ] Synthesizer does NOT mention coverage gaps when all domains are covered

### Test Contracts

**Token measurement:** `len(text) // 4` (consistent with `PromptContext.estimate_tokens()`).

**Enrichment failure modes to test:**
1. `parse_checklist_frontmatter()` returns `None` for no frontmatter → checklist injected globally
2. `match_enrichments()` raises any exception → fallback to `load_domain_checklists()` + warning
3. Checklist file missing from disk → silently skipped
4. `yaml` not installed → all checklists injected globally (same as current behavior)

---

## Performance Budget

| Operation | Expected | Hard Limit |
|-----------|----------|------------|
| Checklist frontmatter parsing (all checklists) | <10ms | 50ms |
| Enrichment matching (per expert) | <5ms | 20ms |
| ChecklistRegistry.load() (with cache) | <1ms | 5ms |

**Total prepare phase impact:** <50ms across all experts. Negligible.

---

## Config Schema Changes

Add to `DEFAULT_CONFIG`:
```python
"enrichment_mode": "per-expert",    # "per-expert" | "global"
```

Add to `CONFIG_ALLOWLIST`:
```python
"enrichment_mode",
```

Example `.codereview.yaml`:
```yaml
enrichment_mode: global    # force global injection (rollback)
```

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Per-expert enrichment breaks existing behavior | `load_domain_checklists()` untouched as fallback; `enrichment_mode: global` for rollback |
| Checklist frontmatter migration incomplete | Checklists without frontmatter automatically inject globally (backward-compatible) |
| P5 budget too tight for multi-language diffs | 2 language checklists (1000 tokens) + 1 domain (500) = 1500 — fits. If it doesn't, language checklists are trimmed last |
| `match_enrichments()` has bugs | Every call wrapped in try/except with global fallback |

---

## Decisions

### Global injection as default, per-expert as upgrade

The spec ships with `enrichment_mode: "per-expert"` as default. Users can set `enrichment_mode: "global"` to get current behavior. If eval data shows per-expert enrichment doesn't improve review quality, the default can be flipped without code changes.

### No `assemble_explorer_prompt()` signature change

Pre-mortem consensus (3/4 judges): keep `domain_checklists: str`. `match_enrichments()` returns a joined string. Zero downstream changes, zero test breakage, zero merge conflict with in-flight context enrichment work.

### Prescan routing taxonomy locked (wiring deferred)

P-SEC → security experts, P-ERR → error-handling, P-PERF → reliability/correctness, P-* → all experts. The taxonomy is defined so prescan output format can be designed to support it. Actual wiring deferred until prescan.py ships.

---

## Open Questions

1. **Checklist growth management.** As checklists grow to 20-30 files, should `ChecklistRegistry` support priority ordering or tag-based filtering? Or is the P5 budget cap sufficient?

2. **Baz rules as runtime injection.** Could individual Baz micro-rules be injected at runtime via keyword matching against the diff, instead of pre-curated into checklists? Worth prototyping if curated checklists prove too coarse.

3. **Enrichment quality measurement.** How do we measure whether per-expert enrichment improves review quality vs global injection? Compare false positive rates and finding relevance between `enrichment_mode: "per-expert"` and `"global"` on the eval suite.

---

## Appendix A: Dynamic Expert Generation (Deferred)

The following design was developed during this research session and validated by pre-mortem. It is preserved here for when gap frequency data justifies building it.

### Pre-Mortem Recommendation

The Spec B pre-mortem recommended:
- **Move generation to `orchestrate.py generate-expert` subcommand** instead of SKILL.md (resolves SKILL.md fragility, `_prompt_path_for_expert()` crash, and `post_explorers()` discovery issues)
- **Add `--prompt-file` override to `_prompt_path_for_expert()`** so generated expert prompts use the standard assembly pipeline
- **Update `post_explorers()` to discover generated expert output** via globbing or `launch.json` update
- **Max 1 generated expert per review**; when multiple domains uncovered, pick the one with most changed files

### Generation Architecture

```
prepare()  →  launch.json (includes coverage_gaps field)
    ↓
SKILL.md calls: orchestrate.py generate-expert --session-dir ... --domain ...
    ↓
generate-expert subcommand:
  - Reads launch.json for diff context and coverage gap signals
  - Fills the generation template deterministically (domain, languages, file list)
  - Calls LLM (haiku by default, 30s timeout) to fill investigation phases and checklists
  - Validates output (frontmatter, headings, ≥5 checklist items)
  - Assembles full context packet using real assemble_explorer_prompt() machinery
  - Writes prompt file and updates launch.json with new task entry
    ↓
SKILL.md launches all explorers (static + generated) in parallel
```

### Generation Template

Located at `skills/codereview/prompts/reviewer-generated-template.md`. Two types of placeholders:

**Deterministic (filled by the subcommand):**
- `{domain}`, `{detected_languages}`, `{file_list}`

**LLM-generated:**
- Focus areas (3-5), Grep patterns (3-5), best practice checks (3-5), integration checks (2-3), checklist items (10-15)

### LLM Call Details

- **Model:** `pass_models.generator` config, default `haiku`
- **Temperature:** 0.3
- **Max tokens:** 2,000
- **Timeout:** 30 seconds
- **System prompt:** "You are a code review checklist generator..." (focus on failure modes, not generic advice)

### Output Validation

1. Contains `---` frontmatter delimiters
2. Frontmatter parses as valid YAML
3. Contains at least one `## ` heading
4. Contains at least 5 checklist items (lines matching `^- `)
5. At least 1 checklist item contains the domain name or domain keyword (prevents generic output)

### Judge Calibration

When a generated expert participates:
- Coverage map shows it with `tier: generated` (bold)
- Judge prompt gets conditional "Generated Expert Findings" section
- Confidence downgrade by 0.10 for all generated expert findings
- Static experts preferred over generated in disagreements

### Caching

- Session directory scoped (each `/codereview` invocation creates fresh `mktemp` dir)
- Cache key: domain name
- No cross-session persistence
- Promotion to permanent expert: manual copy to `skills/codereview/prompts/reviewer-{domain}-pass.md`

### Interaction with `max_experts`

Generated experts do NOT count against the `max_experts` cap (Spec A). They are separately bounded by "max 1 generated expert per review." A user who sets `max_experts: 3` for cost control gets at most 3 static experts + 1 generated expert = 4 total.

### Config

```yaml
dynamic_experts: false       # opt-out
pass_models:
  generator: haiku           # model for generation
```

### Testing Strategy

When generation is implemented, the following integration tests are needed:
- Fixture `launch.json` with `generation_eligible: true` + mock generated expert output → verify findings appear in final report
- `generate-expert` subcommand: valid template filling → validates output schema
- `generate-expert` subcommand: LLM timeout → graceful failure, gap still in coverage map
- `generate-expert` subcommand: malformed output → validation rejects, no generated expert runs
- SKILL.md smoke test: verify generation step instructions are syntactically present in SKILL.md (grep for expected step heading)
- `post_explorers()` discovers generated expert output file alongside static expert outputs
