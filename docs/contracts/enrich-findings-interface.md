# Interface Contract: enrich-findings.py

**Status:** Draft
**Script:** `skills/codereview/scripts/enrich-findings.py`
**Purpose:** Formal contract between the enrichment script and its consumers (report renderer, downstream agents, and feature branches F8 and F9) so that each party can code against a stable, documented interface.

---

## 1. Input Schema (CLI Flags)

| Flag | Type | Default | Description |
|---|---|---|---|
| `--judge-findings` | `str` (file path) | `""` | Path to JSON file containing AI judge findings. Empty string means no AI findings. |
| `--scan-findings` | `str` (file path) | `""` | Path to JSON file containing deterministic scan findings. Empty string means no scan findings. |
| `--confidence-floor` | `float` | `0.65` | AI findings with `confidence` strictly below this value are dropped. Deterministic findings are never dropped by this filter. |
| `--code-intel-output` | `str` (file path) | `""` | Path to code-intel call-graph JSON. When provided, findings are enriched with caller counts and severity may be boosted for highly-called files. |
| `--no-llm-prompts` | `bool` (flag) | `False` | When present, omits the `llm_prompt` field from all output findings. |

Both file-path flags accept an empty string to indicate "no file"; the script treats missing or unreadable files as an empty finding set and emits a warning to stderr.

---

## 2. Input File Formats

### 2.1 Judge Findings (`--judge-findings`)

Expected JSON structure:

```json
{
  "findings": [
    {
      "pass": "correctness",
      "file": "src/auth/login.py",
      "line": 42,
      "severity": "high",
      "confidence": 0.87,
      "summary": "...",
      "evidence": "...",
      "failure_mode": "...",
      "fix": "..."
    }
  ]
}
```

The script also accepts a bare JSON array (i.e., the file contains `[...]` directly rather than `{"findings": [...]}`).

Required fields per finding: `file` (non-empty string), `line` (integer). Findings missing either field are skipped with a warning to stderr.

The `source` field is unconditionally overwritten to `"ai"` regardless of what the file contains.

### 2.2 Scan Findings (`--scan-findings`)

Same structure as judge findings. Differences in how the script treats them:

- `source` defaults to `"deterministic"` if absent (not overwritten if already present).
- `confidence` defaults to `1.0` if absent.
- Deterministic findings are never filtered by `--confidence-floor`.
- Deterministic findings are never downgraded by the evidence check.

### 2.3 Code-Intel Graph (`--code-intel-output`)

```json
{
  "nodes": [
    { "file": "src/utils.py", "callers": ["src/app.py", "src/worker.py"] }
  ],
  "edges": [
    { "from": "src/app.py", "to": "src/utils.py" }
  ]
}
```

Both `nodes` and `edges` are optional. The script counts callers per file from both sources and takes the maximum. Files with more than 3 callers have their severity boosted by one level.

---

## 3. Output Schema

The script writes a single JSON object to **stdout**. It always exits 0. Any warnings are written to **stderr** and do not affect the output structure.

### 3.1 Top-Level Object

```json
{
  "findings": [...],
  "tier_summary": { "must_fix": 0, "should_fix": 0, "consider": 0 },
  "dropped": { "below_confidence_floor": 0, "downgraded_to_medium": 0 }
}
```

| Field | Type | Description |
|---|---|---|
| `findings` | `array` | Enriched finding objects, sorted by tier then descending severity-weight × confidence. |
| `tier_summary` | `object` | Count of findings per action tier. Always contains exactly the three keys listed. |
| `dropped.below_confidence_floor` | `int` | Number of AI findings removed because confidence was below `--confidence-floor`. |
| `dropped.downgraded_to_medium` | `int` | Number of AI high/critical findings downgraded to medium because `failure_mode` was absent. Note: these findings are still present in the output; the counter reflects severity changes, not removals. |

### 3.2 Finding Object

| Field | Type | Nullable | Description |
|---|---|---|---|
| `id` | `string` | No | Stable ID: `<pass>-<4-hex-sha256(file)>-<line>`. Collision-resistant within a single pass. |
| `source` | `"ai" \| "deterministic"` | No | Origin of the finding. Set unconditionally by the script (see §2). |
| `pass` | `string` | No | Review pass name. Valid values: `correctness`, `security`, `reliability`, `performance`, `testing`, `maintainability`, `spec_verification`. Unknown values are preserved as-is. |
| `severity` | `"low" \| "medium" \| "high" \| "critical"` | No | Effective severity after evidence check and code-intel boost. May differ from the input value. |
| `confidence` | `float` (0.0–1.0) | No | Confidence score. Deterministic findings default to `1.0`. |
| `file` | `string` | No | Relative file path. Preserved from input. |
| `line` | `int` | No | Line number. Preserved from input. |
| `summary` | `string` | No | Human-readable description of the issue. |
| `evidence` | `string \| null` | Yes | Supporting evidence from the diff or code. |
| `failure_mode` | `string \| null` | Yes | Concrete failure scenario. Drives tier promotion and evidence-check gating. |
| `fix` | `string \| null` | Yes | Suggested remediation. |
| `llm_prompt` | `string \| null` | Yes | Deterministic template prompt for downstream LLM fix generation. `null` when `--no-llm-prompts` is passed. |
| `action_tier` | `"must_fix" \| "should_fix" \| "consider"` | No | Mechanical classification. See tier rules in §5. |
| `affected_callers` | `int` | No (when code-intel is provided); absent otherwise | Number of callers of the finding's file per the code-intel graph. Present only when `--code-intel-output` is non-empty and the graph loads successfully. |

---

## 4. Action Tier Rules

Tier assignment is deterministic (first-match wins):

| Condition | Tier |
|---|---|
| `severity == "critical"` | `must_fix` |
| `severity == "high"` AND `failure_mode` is non-empty | `must_fix` |
| `severity == "high"` | `should_fix` |
| `severity == "medium"` AND `failure_mode` is non-empty | `should_fix` |
| Everything else | `consider` |

---

## 5. Processing Pipeline

The script processes findings in the following order. Steps are numbered to match inline comments in the source.

1. **Load** both finding sets from disk. Missing or unreadable files produce an empty list and a stderr warning; they do not abort the run.
2. **Assign source labels** — all judge findings get `source = "ai"`; scan findings get `source = "deterministic"` (defaulting, not overwriting).
3. **Combine** — scan findings are prepended to judge findings (`scan + judge`).
4. **Validate required fields** — findings missing `file` or `line` are dropped with a warning to stderr.
5. **Generate stable IDs** — `id` is assigned to every surviving finding.
6. **Confidence floor filtering** — AI findings below `--confidence-floor` are removed; the count goes into `dropped.below_confidence_floor`.
7. **Evidence check** — AI findings at `high` or `critical` severity without a `failure_mode` are downgraded to `medium`; the count goes into `dropped.downgraded_to_medium`.
8. **Code-intel enrichment** — if `--code-intel-output` is provided, each finding gains `affected_callers`; files with more than 3 callers have severity boosted one level.
9. **Action tier assignment** — `action_tier` is set on every finding per the rules in §4.
10. **LLM prompt generation** — unless `--no-llm-prompts` is set, each finding gets an `llm_prompt` template string.
11. **Sort** — findings are sorted by `tier_order` ascending, then by `severity_weight * confidence` descending within each tier.
12. **Emit** — the combined object is JSON-serialised to stdout with 2-space indentation followed by a trailing newline.

---

## 6. Extension Points for F8 and F9

Both F8 and F9 make **additive** changes only. They introduce new CLI flags and new output fields. They do NOT rename, remove, or change the semantics of any existing flag or field listed above.

### 6.1 F8 — Pre-Existing Bug Classification

**New flag:**

| Flag | Type | Default | Description |
|---|---|---|---|
| `--baseline` | `str` (file path) | `""` | Path to a baseline findings JSON from a prior run. Used to determine whether each finding was already present before the current diff. |

**New output field per finding:**

| Field | Type | Nullable | Description |
|---|---|---|---|
| `pre_existing` | `bool` | No | `true` if the finding matches a finding in the baseline (i.e., it predates the current change); `false` otherwise. |

All existing fields and their semantics are unchanged. When `--baseline` is omitted, `pre_existing` is absent from findings (or treated as `false` by consumers; the contract owner will confirm at implementation time).

### 6.2 F9 — Provenance-Aware Review Rigor

**New flag:**

| Flag | Type | Default | Description |
|---|---|---|---|
| `--provenance` | `str` (file path) | `""` | Path to provenance metadata JSON (e.g., author heat-map, file ownership, change frequency). Used to apply provenance-aware enrichment rules. |

**New output field per finding:**

| Field | Type | Nullable | Description |
|---|---|---|---|
| `provenance` | `object \| null` | Yes | Provenance context attached to the finding (e.g., `{"author": "...", "change_frequency": N, "owners": [...]}`). `null` when `--provenance` is omitted. |

All existing fields and their semantics are unchanged.

---

## 7. Invariants

The following guarantees hold for every successful invocation:

1. **Output is always valid JSON** written to stdout, regardless of whether either input file is absent, empty, or contains zero findings.

2. **Findings are sorted** by tier order ascending (`must_fix` < `should_fix` < `consider`), then by `severity_weight * confidence` descending within each tier.

3. **Every finding in the output array** has all of the following fields present and non-null: `id`, `source`, `pass`, `severity`, `confidence`, `file`, `line`, `summary`, `action_tier`.

4. **`tier_summary` counts match the findings array** exactly — the sum of `must_fix + should_fix + consider` equals `len(findings)`.

5. **`dropped.below_confidence_floor`** counts only AI findings removed by the confidence filter; it does not count findings removed by field validation.

6. **`dropped.downgraded_to_medium`** counts severity changes, not removals; all downgraded findings are still present in the output array.

7. **The script exits 0** in all cases. Errors (unreadable files, malformed JSON inputs) produce warnings on stderr but do not abort the pipeline or corrupt the output.
