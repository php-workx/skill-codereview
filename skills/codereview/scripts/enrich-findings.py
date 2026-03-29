#!/usr/bin/env python3
"""Finding enrichment and classification — deterministic pipeline.

Combines judge (AI) and scan (deterministic) findings into a single enriched
list with stable IDs, confidence gating, evidence checks, action-tier
classification, LLM prompt generation, and intra-tier ranking.

Usage:
    python3 scripts/enrich-findings.py \
        --judge-findings /tmp/codereview-judge.json \
        --scan-findings /tmp/codereview-scans.json \
        --confidence-floor 0.65 \
        [--code-intel-output graph.json] \
        [--no-llm-prompts] \
        > /tmp/codereview-enriched.json

Input format (both files):
    { "findings": [ { "pass": "...", "file": "...", "line": N, ... }, ... ] }
    Either file may be omitted if there are no findings of that type.

Output (stdout):
    {
      "findings": [ ... enriched findings sorted by tier then rank ... ],
      "tier_summary": { "must_fix": N, "should_fix": N, "consider": N },
      "dropped": { "below_confidence_floor": N, "downgraded_to_medium": N }
    }
"""

import argparse
import hashlib
import json
import re
import sys


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

SEVERITY_ORDER = ["low", "medium", "high", "critical"]

TIER_ORDER = {
    "must_fix": 0,
    "should_fix": 1,
    "consider": 2,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_id(finding: dict) -> str:
    """Generate a stable, collision-resistant finding ID.

    Shape: <pass>-<first 4 hex of sha256(file)>-<line>
    """
    pass_name = finding.get("pass", "unknown")
    file_path = finding.get("file", "")
    line = finding.get("line", 0)
    file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:4]
    return f"{pass_name}-{file_hash}-{line}"


def load_findings(path: str) -> list:
    """Load findings from a JSON file.  Returns [] on missing/empty file."""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not load {path}: {exc}", file=sys.stderr)
        return []

    if isinstance(data, dict):
        return data.get("findings", [])
    if isinstance(data, list):
        return data
    return []


def validate_finding(finding: dict, index: int) -> bool:
    """Check that a finding has required fields.  Warn and return False if not."""
    if not finding.get("file"):
        print(
            f"WARNING: finding #{index} missing 'file' field, skipping",
            file=sys.stderr,
        )
        return False
    if finding.get("line") is None:
        print(
            f"WARNING: finding #{index} missing 'line' field, skipping"
            f" (file={finding.get('file', '?')})",
            file=sys.stderr,
        )
        return False
    return True


_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def apply_minimum_severity(findings: list, minimum: str) -> tuple[list, int]:
    """Drop findings below the minimum severity level.

    Returns (kept_findings, dropped_count).
    """
    min_rank = _SEVERITY_RANK.get(minimum, 1)
    if min_rank <= 1:
        return findings, 0
    kept = []
    dropped = 0
    for f in findings:
        sev = f.get("severity", "low").lower()
        if _SEVERITY_RANK.get(sev, 1) >= min_rank:
            kept.append(f)
        else:
            dropped += 1
    return kept, dropped


def apply_confidence_floor(findings: list, floor: float) -> tuple[list, int]:
    """Drop AI findings whose confidence is below *floor*.

    Returns (kept_findings, dropped_count).
    """
    kept = []
    dropped = 0
    for f in findings:
        if f.get("source") == "ai":
            confidence = f.get("confidence")
            if confidence is None or confidence < floor:
                dropped += 1
                continue
        kept.append(f)
    return kept, dropped


def apply_evidence_check(findings: list) -> tuple[list, int]:
    """Downgrade AI findings at high/critical without failure_mode to medium.

    Deterministic findings are exempt — scan tools handle their own evidence.
    Returns (modified_findings, downgrade_count).
    """
    downgraded = 0
    for f in findings:
        if f.get("source") == "deterministic":
            continue
        severity = f.get("severity", "").lower()
        if severity in ("high", "critical") and not f.get("failure_mode"):
            f["severity"] = "medium"
            downgraded += 1
    return findings, downgraded


def assign_action_tier(finding: dict) -> str:
    """Assign action_tier mechanically.

    Rules (first match wins):
    1. must_fix:   critical, OR high + failure_mode
    2. should_fix: high, OR medium + failure_mode
    3. consider:   everything else
    """
    severity = finding.get("severity", "low").lower()
    has_failure_mode = bool(finding.get("failure_mode"))

    # Rule 1 — Must Fix
    if severity == "critical":
        return "must_fix"
    if severity == "high" and has_failure_mode:
        return "must_fix"

    # Rule 2 — Should Fix
    if severity == "high":
        return "should_fix"
    if severity == "medium" and has_failure_mode:
        return "should_fix"

    # Rule 3 — Consider (everything else)
    return "consider"


def generate_llm_prompt(finding: dict) -> str:
    """Generate a deterministic LLM prompt for a finding.

    This is NOT an LLM call — it produces a template that downstream LLMs
    can use to understand and fix the issue.
    """
    parts = [
        f"In {finding['file']} at line {finding['line']}, "
        f"there is a {finding['severity']} {finding['pass']} issue.",
        finding["summary"],
    ]
    if finding.get("evidence"):
        parts.append(f"Evidence: {finding['evidence']}")
    if finding.get("failure_mode"):
        parts.append(f"This causes: {finding['failure_mode']}")
    if finding.get("fix"):
        parts.append(f"Suggested approach: {finding['fix']}")
    parts.append("Also check for similar patterns in the same file and related files.")
    return " ".join(parts)


def rank_key(finding: dict):
    """Sort key: tier order first, then descending severity_weight * confidence."""
    tier = TIER_ORDER.get(finding.get("action_tier", "consider"), 2)
    weight = SEVERITY_WEIGHT.get(finding.get("severity", "low"), 1)
    confidence = finding.get("confidence", 0.0)
    # Negate score so higher scores sort first within each tier
    return (tier, -(weight * confidence))


def compute_tier_summary(findings: list) -> dict:
    """Return counts per action tier."""
    summary = {"must_fix": 0, "should_fix": 0, "consider": 0}
    for f in findings:
        tier = f.get("action_tier", "consider")
        if tier in summary:
            summary[tier] += 1
    return summary


def boost_severity(severity: str) -> str:
    """Boost severity to the next level (low->medium->high->critical)."""
    idx = (
        SEVERITY_ORDER.index(severity.lower())
        if severity.lower() in SEVERITY_ORDER
        else 0
    )
    next_idx = min(idx + 1, len(SEVERITY_ORDER) - 1)
    return SEVERITY_ORDER[next_idx]


def downgrade_action_tier(tier: str) -> str:
    """Downgrade action_tier by one level (must_fix→should_fix→consider)."""
    if tier == "must_fix":
        return "should_fix"
    if tier == "should_fix":
        return "consider"
    return "consider"


def apply_pre_existing_rules(findings: list) -> tuple[list, int]:
    """F8: Pre-existing bug classification.

    Rules:
    - pre_existing=True, newly_reachable=False → drop (unrelated to diff)
    - pre_existing=True, newly_reachable=True, severity medium/low → downgrade tier by one level
    - pre_existing and newly_reachable pass through to output unchanged

    Returns (kept_findings, dropped_count).
    """
    kept = []
    dropped = 0
    for f in findings:
        pre_existing = f.get("pre_existing", False)
        newly_reachable = f.get("pre_existing_newly_reachable", False)

        if pre_existing and not newly_reachable:
            # Safety net: drop non-reachable pre-existing findings
            dropped += 1
            continue

        if pre_existing and newly_reachable:
            severity = f.get("severity", "low").lower()
            if severity in ("medium", "low"):
                f["action_tier"] = downgrade_action_tier(
                    f.get("action_tier", "consider")
                )

        kept.append(f)
    return kept, dropped


AI_CODEGEN_PATTERNS = [
    r"\bplaceholder\b",
    r"\bstub\b",
    r"\btodo\b",
    r"\bunwired\b",
    r"\bdead code\b",
    r"\bmock data\b",
    r"\bhardcoded\b",
    r"\blocalhost\b",
    r"\bexample\.com\b",
    r"\bsilent\b",
    r"\bswallow\b",
    r"\bempty catch\b",
    r"\bpass\b",
    r"\bover-abstract\b",
    r"\bunnecessary\b",
    r"\bpremature\b",
    r"\bunused\b",
]
_AI_CODEGEN_RE = re.compile("|".join(AI_CODEGEN_PATTERNS), re.IGNORECASE)


def apply_provenance_boost(findings: list[dict], provenance: str) -> list[dict]:
    """Boost severity of AI-codegen risk findings when provenance indicates AI generation."""
    if provenance not in ("ai-assisted", "autonomous"):
        return findings
    for finding in findings:
        summary_lower = (
            (finding.get("summary") or "") + " " + (finding.get("evidence") or "")
        ).lower()
        if finding.get("action_tier") == "consider" and not finding.get("pre_existing"):
            if _AI_CODEGEN_RE.search(summary_lower):
                finding["action_tier"] = "should_fix"
    return findings


def load_code_intel(path: str) -> dict:
    """Load code-intel graph JSON.  Returns empty dict on failure."""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: Could not load code-intel graph {path}: {exc}", file=sys.stderr
        )
        return {}


def apply_code_intel(findings: list, graph: dict) -> list:
    """Enrich findings with code-intel data.

    For each finding, count callers from the graph that reference the file.
    If callers > 3, boost severity to the next level.
    Adds 'affected_callers' field.
    """
    if not graph:
        return findings

    # Build a map: file -> caller count from graph nodes/edges
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    # Count how many callers reference each file (target)
    file_caller_count: dict[str, int] = {}
    for edge in edges:
        target = edge.get("to", "")
        if target:
            file_caller_count[target] = file_caller_count.get(target, 0) + 1

    # Also check nodes directly if they carry caller info
    for node in nodes:
        node_file = node.get("file", "")
        callers = node.get("callers", [])
        if node_file and callers:
            file_caller_count[node_file] = max(
                file_caller_count.get(node_file, 0), len(callers)
            )

    for f in findings:
        file_path = f.get("file", "")
        caller_count = file_caller_count.get(file_path, 0)
        f["affected_callers"] = caller_count
        if caller_count > 3:
            f["severity"] = boost_severity(f.get("severity", "low"))

    return findings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Enrich and classify code-review findings."
    )
    parser.add_argument(
        "--judge-findings",
        default="",
        help="Path to JSON file with AI judge findings.",
    )
    parser.add_argument(
        "--scan-findings",
        default="",
        help="Path to JSON file with deterministic scan findings.",
    )
    parser.add_argument(
        "--confidence-floor",
        type=float,
        default=0.65,
        help="Drop AI findings below this confidence (default: 0.65).",
    )
    parser.add_argument(
        "--code-intel-output",
        default="",
        help="Path to code-intel graph JSON for caller-based enrichment.",
    )
    parser.add_argument(
        "--no-llm-prompts",
        action="store_true",
        default=False,
        help="Skip generating llm_prompt fields.",
    )
    parser.add_argument(
        "--provenance",
        choices=["human", "ai-assisted", "autonomous", "unknown"],
        default="unknown",
        help="Code provenance: human, ai-assisted, autonomous, unknown (default: unknown).",
    )
    parser.add_argument(
        "--minimum-severity",
        default="low",
        choices=["low", "medium", "high", "critical"],
        help="Drop findings below this severity (default: low = keep all).",
    )
    args = parser.parse_args()

    # 1. Load both finding sets
    judge_findings = load_findings(args.judge_findings)
    scan_findings = load_findings(args.scan_findings)

    # 2-3. Assign source and combine
    for f in judge_findings:
        f["source"] = "ai"
    for f in scan_findings:
        f.setdefault("source", "deterministic")
        # Deterministic findings always have confidence 1.0
        if "confidence" not in f:
            f["confidence"] = 1.0

    combined = scan_findings + judge_findings

    # 4. Validate required fields — skip findings missing file or line
    validated = []
    for i, f in enumerate(combined):
        if validate_finding(f, i):
            validated.append(f)
    combined = validated

    # 5. Generate stable IDs
    for f in combined:
        f["id"] = generate_id(f)

    # 6. Confidence floor — drop AI findings below threshold
    combined, below_confidence_floor = apply_confidence_floor(
        combined, args.confidence_floor
    )

    # 7. Code-intel integration — boost severity for high-caller findings (before evidence gate)
    graph = load_code_intel(args.code_intel_output)
    combined = apply_code_intel(combined, graph)

    # 8. Evidence check — downgrade AI high/critical without failure_mode (after boost)
    combined, downgraded_to_medium = apply_evidence_check(combined)

    # 9. Assign action_tier
    for f in combined:
        f["action_tier"] = assign_action_tier(f)

    # 9b. F8: Pre-existing bug classification — drop non-reachable, downgrade reachable medium/low
    combined, dropped_pre_existing = apply_pre_existing_rules(combined)

    # 9c. F9: Provenance boost — elevate AI-codegen risk findings from consider→should_fix
    combined = apply_provenance_boost(combined, args.provenance)

    # 9d. Minimum severity filter — drop findings below configured threshold
    combined, below_minimum_severity = apply_minimum_severity(
        combined, args.minimum_severity
    )

    # 10. Generate llm_prompt (deterministic template, not an LLM call)
    if not args.no_llm_prompts:
        for f in combined:
            f["llm_prompt"] = generate_llm_prompt(f)

    # 11. Rank within each tier by severity_weight * confidence
    combined.sort(key=rank_key)

    # Compute tier_summary
    tier_summary = compute_tier_summary(combined)

    # Output enriched JSON
    output = {
        "findings": combined,
        "tier_summary": tier_summary,
        "provenance": args.provenance,
        "dropped": {
            "below_confidence_floor": below_confidence_floor,
            "below_minimum_severity": below_minimum_severity,
            "downgraded_to_medium": downgraded_to_medium,
            "pre_existing_not_reachable": dropped_pre_existing,
        },
    }
    json.dump(output, sys.stdout, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
