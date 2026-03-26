#!/usr/bin/env python3
"""enrich-findings.py — Mechanical finding enrichment and classification.

Combines judge (AI) and scan (deterministic) findings into a single enriched
list with stable IDs, confidence gating, evidence checks, action-tier
classification, and intra-tier ranking.

Usage:
    python3 scripts/enrich-findings.py \
        --judge-findings /tmp/codereview-judge.json \
        --scan-findings /tmp/codereview-scans.json \
        --confidence-floor 0.65 \
        > /tmp/codereview-enriched.json

Input format (both files):
    { "findings": [ { "pass": "...", "file": "...", "line": N, ... }, ... ] }
    Either file may be omitted if there are no findings of that type.

Output (stdout):
    {
      "findings": [ ... enriched findings sorted by tier then rank ... ],
      "tier_summary": { "must_fix": N, "should_fix": N, "consider": N }
    }
"""

import argparse
import hashlib
import json
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

TIER_ORDER = {
    "must_fix": 0,
    "should_fix": 1,
    "consider": 2,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_hash_4(filename: str) -> str:
    """Return the first 4 hex chars of the SHA-256 of *filename*."""
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:4]


def generate_id(finding: dict) -> str:
    """Generate a stable finding ID: <pass>-<file_hash_4chars>-<line>."""
    pass_name = finding.get("pass", "unknown")
    file_path = finding.get("file", "")
    line = finding.get("line", 0)
    return f"{pass_name}-{file_hash_4(file_path)}-{line}"


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


def apply_confidence_floor(findings: list, floor: float) -> list:
    """Drop AI findings whose confidence is below *floor*."""
    kept = []
    for f in findings:
        if f.get("source") == "ai":
            confidence = f.get("confidence")
            if confidence is None or confidence < floor:
                continue
        kept.append(f)
    return kept


def apply_evidence_check(findings: list) -> list:
    """Downgrade high/critical findings that lack a failure_mode to medium."""
    for f in findings:
        severity = f.get("severity", "").lower()
        if severity in ("high", "critical"):
            failure_mode = f.get("failure_mode")
            if not failure_mode:  # None, empty string, or missing
                f["severity"] = "medium"
    return findings


def assign_action_tier(finding: dict) -> str:
    """Assign action_tier mechanically.  First matching rule wins.

    1. Must Fix:   (critical OR high) AND confidence >= 0.80
    2. Should Fix: medium severity, OR high with confidence 0.65-0.79
    3. Consider:   everything else above confidence floor
    """
    severity = finding.get("severity", "low").lower()
    confidence = finding.get("confidence", 0.0)

    # Rule 1 — Must Fix
    if severity in ("critical", "high") and confidence >= 0.80:
        return "must_fix"

    # Rule 2 — Should Fix
    if severity == "medium":
        return "should_fix"
    if severity == "high" and 0.65 <= confidence < 0.80:
        return "should_fix"

    # Rule 3 — Consider (everything else above floor — floor already applied)
    return "consider"


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
    args = parser.parse_args()

    # 1. Load both finding sets
    judge_findings = load_findings(args.judge_findings)
    scan_findings = load_findings(args.scan_findings)

    # 2-3. Assign source and combine
    for f in judge_findings:
        f["source"] = "ai"
    for f in scan_findings:
        f["source"] = "deterministic"
        # Deterministic findings always have confidence 1.0
        if "confidence" not in f:
            f["confidence"] = 1.0

    combined = scan_findings + judge_findings

    # 4. Generate stable IDs
    for f in combined:
        f["id"] = generate_id(f)

    # 5. Confidence floor — drop AI findings below threshold
    combined = apply_confidence_floor(combined, args.confidence_floor)

    # 6. Evidence check — downgrade high/critical without failure_mode
    combined = apply_evidence_check(combined)

    # 7. Assign action_tier
    for f in combined:
        f["action_tier"] = assign_action_tier(f)

    # 8. Rank within each tier by severity_weight * confidence
    combined.sort(key=rank_key)

    # 9. Compute tier_summary
    tier_summary = compute_tier_summary(combined)

    # 10. Output enriched JSON
    output = {
        "findings": combined,
        "tier_summary": tier_summary,
    }
    json.dump(output, sys.stdout, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
