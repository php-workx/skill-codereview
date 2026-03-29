#!/usr/bin/env python3
"""lifecycle.py -- Finding fingerprinting, lifecycle tagging, and suppression management.

Computes stable fingerprints for each finding, tags findings as new/recurring
by comparing against a previous review, and applies suppressions from a
committed suppressions file.

Usage:
    # Main lifecycle operation
    python3 scripts/lifecycle.py \
        --findings /tmp/codereview-enriched.json \
        --previous-review .agents/reviews/previous.json \
        --suppressions .codereview-suppressions.json \
        --changed-files /tmp/changed-files.txt \
        --scope branch --base-ref main

    # Raw mode (no enrich-findings.py dependency)
    python3 scripts/lifecycle.py --findings /tmp/judge-raw.json --raw

    # Suppress a finding
    python3 scripts/lifecycle.py suppress \
        --review .agents/reviews/latest.json \
        --finding-id "security-a3f1-42" \
        --status rejected --reason "Intentional" \
        --suppressions .codereview-suppressions.json

    # Test fixture runner
    python3 scripts/lifecycle.py --test-fixtures tests/fixtures/fuzzy-match-pairs.json

Output (stdout):
    {
      "findings": [ ... new/recurring findings with fingerprint and lifecycle_status ... ],
      "suppressed_findings": [ ... rejected/deferred findings ... ],
      "lifecycle_summary": { "new": N, "recurring": N, "rejected": N, "deferred": N, "deferred_resurfaced": N }
    }
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STOP_WORDS = frozenset(
    [
        "a",
        "the",
        "is",
        "in",
        "of",
        "to",
        "for",
        "and",
        "or",
        "but",
        "not",
        "with",
        "this",
        "that",
    ]
)

SUFFIX_PATTERN = re.compile(r"(ing|ed|tion|ment|ness|ly|ble|er|est)$")

FUZZY_MATCH_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Normalization & Fingerprinting
# ---------------------------------------------------------------------------


def normalize_summary(summary: str) -> str:
    """Normalize a finding summary for fingerprinting.

    Steps (in order):
    1. Lowercase
    2. Strip punctuation
    3. Strip stop words
    4. Stem common suffixes (-ing, -ed, -tion, -ment, -ness, -ly, -ble, -er, -est)
    5. Collapse whitespace
    6. Sort tokens alphabetically
    7. Join with spaces
    """
    # 1. Lowercase
    text = summary.lower()
    # 2. Strip punctuation
    text = re.sub(r"[^\w\s]", "", text)
    # 3-4. Tokenize, strip stop words, stem suffixes
    tokens = text.split()
    stemmed = []
    for word in tokens:
        if word in STOP_WORDS:
            continue
        word = SUFFIX_PATTERN.sub("", word)
        if word:  # avoid empty strings from short words
            stemmed.append(word)
    # 5. Collapse whitespace (already handled by split/join)
    # 6. Sort tokens alphabetically
    stemmed.sort()
    # 7. Join with spaces
    return " ".join(stemmed)


def compute_fingerprint(
    file_path: str, pass_name: str, severity: str, summary: str
) -> str:
    """Compute a 12-hex-char fingerprint for a finding.

    fingerprint = sha256(file + ":" + pass + ":" + severity + ":" + normalize(summary))[:12]
    """
    normalized = normalize_summary(summary)
    raw = f"{file_path}:{pass_name}:{severity}:{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def stemmed_tokens(summary: str) -> set:
    """Return the set of stemmed tokens for fuzzy matching."""
    return set(normalize_summary(summary).split())


# ---------------------------------------------------------------------------
# Fuzzy Matching
# ---------------------------------------------------------------------------


def fuzzy_match(finding_a: dict, finding_b: dict) -> bool:
    """Check if two findings are a fuzzy match.

    Criteria: same file + pass + severity + >= 60% stemmed word overlap.
    Overlap is computed as |intersection| / |smaller set| to handle subset cases.
    """
    if finding_a.get("file") != finding_b.get("file"):
        return False
    if finding_a.get("pass") != finding_b.get("pass"):
        return False
    if finding_a.get("severity") != finding_b.get("severity"):
        return False

    # Use summary_snippet as fallback (suppression records store truncated summary)
    summary_a = finding_a.get("summary") or finding_a.get("summary_snippet", "")
    summary_b = finding_b.get("summary") or finding_b.get("summary_snippet", "")
    tokens_a = stemmed_tokens(summary_a)
    tokens_b = stemmed_tokens(summary_b)

    if not tokens_a or not tokens_b:
        return False

    intersection = tokens_a & tokens_b
    # Use smaller set as denominator so subset relationships count as matches
    smaller = min(len(tokens_a), len(tokens_b))
    overlap = len(intersection) / smaller
    return overlap >= FUZZY_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Loading Helpers
# ---------------------------------------------------------------------------


def load_json_file(path: str, label: str = "file") -> dict | list | None:
    """Load a JSON file, returning None on missing/malformed files."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        print(f"WARNING: {label} not found: {path}", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"WARNING: {label} is malformed JSON: {path}: {exc}", file=sys.stderr)
        return None


def load_findings(path: str, raw: bool = False) -> list:
    """Load findings from a JSON file. Returns [] on missing/empty/malformed."""
    data = load_json_file(path, "findings file")
    if data is None:
        return []

    findings = []
    if isinstance(data, dict):
        findings = data.get("findings", [])
    elif isinstance(data, list):
        findings = data
    else:
        return []

    if not raw:
        # Validate enrichment fields are present (warn but don't fail)
        for f in findings:
            for field in ("action_tier", "source", "id"):
                if field not in f:
                    print(
                        f"WARNING: Finding missing enrichment field '{field}' "
                        f"(use --raw to skip this check): {f.get('summary', '?')[:60]}",
                        file=sys.stderr,
                    )
                    break

    return findings


def load_previous_review(path: str) -> list:
    """Load findings from a previous review artifact."""
    data = load_json_file(path, "previous review")
    if data is None:
        return []
    if isinstance(data, dict):
        return data.get("findings", [])
    return []


def load_suppressions(path: str) -> list:
    """Load suppressions file. Fail-open: malformed JSON -> warn and return []."""
    data = load_json_file(path, "suppressions file")
    if data is None:
        return []
    if isinstance(data, dict):
        return data.get("suppressions", [])
    return []


def load_changed_files(path: str) -> set:
    """Load changed files list (newline-delimited). Returns empty set if missing."""
    if not path:
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except FileNotFoundError:
        print(f"WARNING: Changed files not found: {path}", file=sys.stderr)
        return set()


def auto_discover_previous_review(
    scope: str,
    base_ref: str,
    head_ref: str = "",
    reviews_dir: str = ".agents/reviews",
) -> str | None:
    """Scan .agents/reviews/ for most recent file matching scope, base_ref, and head_ref.

    Including head_ref prevents cross-branch contamination: reviews from branch A
    won't be selected as "previous" for branch B even if both target the same base.
    """
    if not os.path.isdir(reviews_dir):
        return None

    candidates = []
    for fname in os.listdir(reviews_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(reviews_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("scope") != scope or data.get("base_ref") != base_ref:
                continue
            # Match head_ref when provided (prevents cross-branch selection)
            if head_ref and data.get("head_ref", "") != head_ref:
                continue
            mtime = os.path.getmtime(fpath)
            candidates.append((mtime, fpath))
        except (json.JSONDecodeError, OSError):
            continue

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Atomic Write
# ---------------------------------------------------------------------------


def atomic_write_json(path: str, data: dict | list) -> None:
    """Write JSON to a file using temp-file-plus-rename for atomicity."""
    dir_path = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp", prefix=".lifecycle-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.rename(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core Lifecycle Logic
# ---------------------------------------------------------------------------


def add_fingerprints(findings: list) -> list:
    """Add fingerprint field to each finding."""
    for f in findings:
        f["fingerprint"] = compute_fingerprint(
            f.get("file", ""),
            f.get("pass", ""),
            f.get("severity", ""),
            f.get("summary", ""),
        )
    return findings


def tag_lifecycle(findings: list, previous_findings: list) -> list:
    """Tag each finding as 'new' or 'recurring' based on previous review.

    Match order:
    1. Exact fingerprint match -> recurring
    2. Fuzzy match (same file + pass + severity + >= 60% stemmed word overlap) -> recurring
    3. No match -> new
    """
    if not previous_findings:
        for f in findings:
            f["lifecycle_status"] = "new"
        return findings

    # Build fingerprint index of previous findings
    prev_fingerprints = {
        pf.get("fingerprint") for pf in previous_findings if pf.get("fingerprint")
    }

    for f in findings:
        fp = f.get("fingerprint", "")

        # 1. Exact fingerprint match
        if fp and fp in prev_fingerprints:
            f["lifecycle_status"] = "recurring"
            continue

        # 2. Fuzzy match against previous findings
        matched = False
        for pf in previous_findings:
            if fuzzy_match(f, pf):
                f["lifecycle_status"] = "recurring"
                matched = True
                break

        if not matched:
            # 3. No match -> new
            f["lifecycle_status"] = "new"

    return findings


def apply_suppressions(
    findings: list,
    suppressions: list,
    changed_files: set,
) -> tuple[list, list, int]:
    """Apply suppressions to findings, returning (active_findings, suppressed_findings, deferred_resurfaced_count).

    For each finding:
    1. Exact fingerprint match against suppressions -> candidate
    2. Fuzzy match against suppressions -> candidate
    3. Expiry check: if expires_at is in the past, ignore suppression
    4. rejected -> always suppress
    5. deferred -> apply deferred_scope rules
    """
    if not suppressions:
        return findings, [], 0

    now = datetime.now(timezone.utc)

    active = []
    suppressed = []
    deferred_resurfaced_count = 0

    for f in findings:
        fp = f.get("fingerprint", "")
        matched_suppression = None
        match_type = None  # "exact" or "fuzzy"

        # 1. Exact fingerprint match (iterate in reverse so latest entry wins)
        for s in reversed(suppressions):
            if fp and s.get("fingerprint") == fp:
                matched_suppression = s
                match_type = "exact"
                break

        # 2. Fuzzy match (if no exact match, latest entry wins)
        if matched_suppression is None:
            for s in reversed(suppressions):
                if fuzzy_match(f, s):
                    matched_suppression = s
                    match_type = "fuzzy"
                    break

        if matched_suppression is None:
            active.append(f)
            continue

        # 3. Expiry check
        expires_at = matched_suppression.get("expires_at")
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expiry <= now:
                    # Expired suppression -> finding resurfaces
                    active.append(f)
                    continue
            except (ValueError, TypeError):
                # Malformed expires_at -> treat as no expiry
                pass

        status = matched_suppression.get("status", "rejected")

        # 4. rejected -> always suppress
        if status == "rejected":
            f["lifecycle_status"] = "rejected"
            f["suppression_reason"] = matched_suppression.get("reason", "")
            suppressed.append(f)
            continue

        # 5. deferred -> apply deferred_scope rules
        if status == "deferred":
            deferred_scope = matched_suppression.get("deferred_scope", "file")
            file_path = f.get("file", "")

            # If changed_files is empty (--changed-files not provided), resurface
            # all deferred findings to avoid turning temporary deferrals into
            # permanent hides in the fail-open path.
            if not changed_files:
                should_resurface = True
            elif deferred_scope == "file":
                # Resurface if file is in CHANGED_FILES
                should_resurface = file_path in changed_files
            elif deferred_scope == "pass":
                # Resurface only if file in CHANGED_FILES AND same pass
                should_resurface = file_path in changed_files and f.get(
                    "pass"
                ) == matched_suppression.get("pass")
            elif deferred_scope == "exact":
                # Resurface only on exact fingerprint match AND file is in changed files
                should_resurface = match_type == "exact" and file_path in changed_files
            else:
                # Unknown scope -> default to file behavior
                should_resurface = file_path in changed_files

            if should_resurface:
                deferred_resurfaced_count += 1
                # Resurfaced -> stays in active findings with original lifecycle_status
                active.append(f)
            else:
                f["lifecycle_status"] = "deferred"
                f["suppression_reason"] = matched_suppression.get("reason", "")
                suppressed.append(f)
            continue

        # Unknown status -> don't suppress
        active.append(f)

    return active, suppressed, deferred_resurfaced_count


def compute_lifecycle_summary(
    active: list, suppressed: list, deferred_resurfaced: int
) -> dict:
    """Compute lifecycle summary counts."""
    summary = {
        "new": 0,
        "recurring": 0,
        "rejected": 0,
        "deferred": 0,
        "deferred_resurfaced": deferred_resurfaced,
    }
    for f in active:
        status = f.get("lifecycle_status", "new")
        if status in summary:
            summary[status] += 1
    for f in suppressed:
        status = f.get("lifecycle_status", "rejected")
        if status in summary:
            summary[status] += 1
    return summary


def run_lifecycle(args) -> dict:
    """Main lifecycle operation: fingerprint, tag, suppress, output."""
    # 1. Load current findings
    findings = load_findings(args.findings, raw=args.raw)

    # 2. Load previous review (auto-discover if not provided)
    previous_findings = []
    if args.previous_review:
        previous_findings = load_previous_review(args.previous_review)
    elif args.scope and args.base_ref:
        discovered = auto_discover_previous_review(
            args.scope,
            args.base_ref,
            head_ref=args.head_ref,
        )
        if discovered:
            print(f"Auto-discovered previous review: {discovered}", file=sys.stderr)
            previous_findings = load_previous_review(discovered)

    # 3. Load suppressions (fail-open)
    suppressions = []
    if args.suppressions:
        suppressions = load_suppressions(args.suppressions)

    # 4. Load changed files
    changed_files = load_changed_files(args.changed_files)

    # 5. Compute fingerprints
    findings = add_fingerprints(findings)

    # 6. Add fingerprints to previous findings (for matching)
    if previous_findings:
        previous_findings = add_fingerprints(previous_findings)

    # 7. Tag lifecycle (new/recurring)
    findings = tag_lifecycle(findings, previous_findings)

    # 8. Apply suppressions
    deferred_resurfaced = 0
    suppressed = []
    if suppressions:
        result = apply_suppressions(findings, suppressions, changed_files)
        findings, suppressed, deferred_resurfaced = result
    else:
        suppressed = []

    # 9. Compute summary
    lifecycle_summary = compute_lifecycle_summary(
        findings, suppressed, deferred_resurfaced
    )

    return {
        "findings": findings,
        "suppressed_findings": suppressed,
        "lifecycle_summary": lifecycle_summary,
    }


# ---------------------------------------------------------------------------
# Suppress Subcommand
# ---------------------------------------------------------------------------


def run_suppress(args) -> None:
    """Suppress a finding from a review artifact."""
    # Load the review
    review_data = load_json_file(args.review, "review file")
    if review_data is None:
        print("ERROR: Could not load review file", file=sys.stderr)
        sys.exit(1)

    # Find the finding by ID
    review_findings = review_data.get("findings", [])
    target = None
    for f in review_findings:
        if f.get("id") == args.finding_id:
            target = f
            break

    if target is None:
        print(
            f"ERROR: Finding '{args.finding_id}' not found in review", file=sys.stderr
        )
        sys.exit(1)

    # Compute fingerprint
    fingerprint = compute_fingerprint(
        target.get("file", ""),
        target.get("pass", ""),
        target.get("severity", ""),
        target.get("summary", ""),
    )

    # Build suppression entry
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    suppression = {
        "fingerprint": fingerprint,
        "status": args.status,
        "reason": args.reason,
        "created_at": now_iso,
        "file": target.get("file", ""),
        "pass": target.get("pass", ""),
        "severity": target.get("severity", ""),
        "summary_snippet": target.get("summary", "")[:80],
    }

    # Add deferred-specific fields
    if args.status == "deferred":
        if args.defer_scope:
            suppression["deferred_scope"] = args.defer_scope
        if args.defer_days:
            expires = datetime.now(timezone.utc) + timedelta(days=args.defer_days)
            suppression["expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Load existing suppressions file (or create new)
    supp_path = args.suppressions
    existing = load_json_file(supp_path, "suppressions file")
    if existing is None or not isinstance(existing, dict):
        existing = {"version": 1, "suppressions": []}

    if "suppressions" not in existing:
        existing["suppressions"] = []

    existing["suppressions"].append(suppression)

    # Atomic write
    atomic_write_json(supp_path, existing)

    print(f"Suppressed finding '{args.finding_id}' as {args.status}", file=sys.stderr)
    print(f"  Fingerprint: {fingerprint}", file=sys.stderr)
    print(f"  Written to: {supp_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Test Fixtures Runner
# ---------------------------------------------------------------------------


def run_test_fixtures(fixture_path: str) -> None:
    """Run fuzzy matching logic against test fixture pairs and report accuracy."""
    data = load_json_file(fixture_path, "test fixtures file")
    if data is None:
        print("ERROR: Could not load test fixtures", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        print("ERROR: Test fixtures must be a JSON array", file=sys.stderr)
        sys.exit(1)

    total = len(data)
    exact_correct = 0
    fuzzy_correct = 0
    exact_fp = 0  # false positives
    exact_fn = 0  # false negatives
    fuzzy_fp = 0
    fuzzy_fn = 0

    print(f"Running {total} test fixture pairs...\n")

    for i, pair in enumerate(data, 1):
        finding_a = pair["finding_a"]
        finding_b = pair["finding_b"]
        expected_exact = pair.get("expected_exact_match", False)
        expected_fuzzy = pair.get("expected_fuzzy_match", False)
        note = pair.get("note", "")

        # Compute fingerprints
        fp_a = compute_fingerprint(
            finding_a["file"],
            finding_a["pass"],
            finding_a["severity"],
            finding_a["summary"],
        )
        fp_b = compute_fingerprint(
            finding_b["file"],
            finding_b["pass"],
            finding_b["severity"],
            finding_b["summary"],
        )

        actual_exact = fp_a == fp_b
        actual_fuzzy = fuzzy_match(finding_a, finding_b)

        exact_ok = actual_exact == expected_exact
        fuzzy_ok = actual_fuzzy == expected_fuzzy

        if exact_ok:
            exact_correct += 1
        else:
            if actual_exact and not expected_exact:
                exact_fp += 1
            elif not actual_exact and expected_exact:
                exact_fn += 1

        if fuzzy_ok:
            fuzzy_correct += 1
        else:
            if actual_fuzzy and not expected_fuzzy:
                fuzzy_fp += 1
            elif not actual_fuzzy and expected_fuzzy:
                fuzzy_fn += 1

        status = "PASS" if (exact_ok and fuzzy_ok) else "FAIL"
        if status == "FAIL":
            print(f"  {status} #{i}: {note}")
            if not exact_ok:
                print(f"    Exact: expected={expected_exact}, actual={actual_exact}")
                print(f"    FP_A: {fp_a}")
                print(f"    FP_B: {fp_b}")
            if not fuzzy_ok:
                tokens_a = stemmed_tokens(finding_a["summary"])
                tokens_b = stemmed_tokens(finding_b["summary"])
                intersection = tokens_a & tokens_b
                smaller = (
                    min(len(tokens_a), len(tokens_b)) if tokens_a and tokens_b else 1
                )
                overlap = len(intersection) / smaller if smaller > 0 else 0
                print(f"    Fuzzy: expected={expected_fuzzy}, actual={actual_fuzzy}")
                print(f"    Tokens A: {sorted(tokens_a)}")
                print(f"    Tokens B: {sorted(tokens_b)}")
                print(f"    Overlap: {len(intersection)}/{smaller} = {overlap:.2%}")
        else:
            print(f"  {status} #{i}: {note}")

    print(f"\n{'=' * 50}")
    print(f"Results: {total} pairs tested")
    print(
        f"  Exact match:  {exact_correct}/{total} correct "
        f"({exact_fp} false positives, {exact_fn} false negatives)"
    )
    print(
        f"  Fuzzy match:  {fuzzy_correct}/{total} correct "
        f"({fuzzy_fp} false positives, {fuzzy_fn} false negatives)"
    )
    all_correct = exact_correct == total and fuzzy_correct == total
    print(f"  Overall: {'ALL PASS' if all_correct else 'FAILURES DETECTED'}")

    if not all_correct:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Finding lifecycle management: fingerprinting, tagging, and suppression."
    )

    # Top-level flags for main lifecycle operation
    parser.add_argument(
        "--findings",
        default="",
        help="Path to current findings JSON.",
    )
    parser.add_argument(
        "--previous-review",
        default="",
        help="Path to previous review JSON artifact.",
    )
    parser.add_argument(
        "--suppressions",
        default="",
        help="Path to suppressions file (.codereview-suppressions.json).",
    )
    parser.add_argument(
        "--changed-files",
        default="",
        help="Path to newline-delimited changed files list.",
    )
    parser.add_argument(
        "--scope",
        default="",
        help="Review scope (for auto-discovering previous review).",
    )
    parser.add_argument(
        "--base-ref",
        default="",
        help="Base ref (for auto-discovering previous review).",
    )
    parser.add_argument(
        "--head-ref",
        default="",
        help="Head ref / source branch (prevents cross-branch prior review selection).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Skip expecting enrichment fields (action_tier, source, id).",
    )
    parser.add_argument(
        "--test-fixtures",
        default="",
        help="Path to fuzzy match test fixtures JSON. Runs fixture tests and exits.",
    )

    # Suppress subcommand
    subparsers = parser.add_subparsers(dest="subcommand")
    suppress_parser = subparsers.add_parser(
        "suppress",
        help="Suppress a finding from a review.",
    )
    suppress_parser.add_argument(
        "--review",
        required=True,
        help="Path to review artifact containing the finding.",
    )
    suppress_parser.add_argument(
        "--finding-id",
        required=True,
        help="Finding ID to suppress (e.g., 'security-a3f1-42').",
    )
    suppress_parser.add_argument(
        "--status",
        required=True,
        choices=["rejected", "deferred"],
        help="Suppression status.",
    )
    suppress_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for suppression.",
    )
    suppress_parser.add_argument(
        "--suppressions",
        default=".codereview-suppressions.json",
        help="Path to suppressions file (default: .codereview-suppressions.json).",
    )
    suppress_parser.add_argument(
        "--defer-days",
        type=int,
        default=None,
        help="Number of days to defer (auto-computes expires_at). Only with --status deferred.",
    )
    suppress_parser.add_argument(
        "--defer-scope",
        choices=["file", "pass", "exact"],
        default=None,
        help="Deferred scope: file (default), pass, or exact. Only with --status deferred.",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --test-fixtures mode
    if args.test_fixtures:
        run_test_fixtures(args.test_fixtures)
        return

    # suppress subcommand
    if args.subcommand == "suppress":
        run_suppress(args)
        return

    # Main lifecycle operation
    if not args.findings:
        # No findings provided -> output empty structure
        output = {
            "findings": [],
            "suppressed_findings": [],
            "lifecycle_summary": {
                "new": 0,
                "recurring": 0,
                "rejected": 0,
                "deferred": 0,
                "deferred_resurfaced": 0,
            },
        }
        json.dump(output, sys.stdout, indent=2)
        print()
        return

    output = run_lifecycle(args)
    json.dump(output, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
