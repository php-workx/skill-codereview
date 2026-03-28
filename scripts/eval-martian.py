#!/usr/bin/env python3
"""
Martian CodeReBench evaluation runner.

Runs code reviews against the Martian CodeReBench offline benchmark
(50 curated PRs from 5 repos, 136 golden comments) and computes
precision/recall/F1 comparable to the public leaderboard.

Requirements:
    claude                   # Claude Code CLI (for review + judge steps)
    gh                       # GitHub CLI (optional, for fetching PR metadata)

Usage:
    python3 scripts/eval-martian.py setup                  # Clone benchmark data
    python3 scripts/eval-martian.py prepare                # Clone repos, fetch PRs
    python3 scripts/eval-martian.py review                 # Run reviews (expensive)
    python3 scripts/eval-martian.py review --resume        # Skip already-reviewed PRs
    python3 scripts/eval-martian.py review --limit 5       # Quick: 5 PRs only
    python3 scripts/eval-martian.py judge                  # Score against golden comments
    python3 scripts/eval-martian.py report                 # Show leaderboard comparison
    python3 scripts/eval-martian.py run                    # Full pipeline
    python3 scripts/eval-martian.py run --limit 5          # Quick end-to-end test

Directories (all under .eval/, gitignored):
    .eval/benchmark/    Cloned Martian benchmark repo
    .eval/repos/        Cached blobless repo clones
    .eval/reviews/      Findings JSON per PR (one file per review)
    .eval/results/      Judge output, metrics, timestamped results

Notes:
    - The review step invokes `claude -p` in each repo directory. This requires
      Claude Code CLI installed and authenticated. Each review takes 2-5 minutes
      and costs ~$0.50-2.00 in API usage.
    - The judge step batches multiple pairs per claude call and runs PRs in parallel.
    - Results are timestamped so you can track improvement across skill iterations.
    - Use --resume to continue an interrupted review run without re-doing completed PRs.
    - Use --workers N to control parallelism (default: 5, one per repo).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

_print_lock = threading.Lock()

# ─── Constants ────────────────────────────────────────────────────────────────

EVAL_DIR = Path(".eval")
BENCHMARK_URL = "https://github.com/withmartian/code-review-benchmark.git"
GOLDEN_SUBDIR = Path("offline") / "golden_comments"
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "codereview"

# Map golden comment filenames → repo config
REPOS = {
    "sentry": {
        "golden_file": "sentry.json",
        "clone_url": "https://github.com/getsentry/sentry.git",
        "default_branch": "master",
        "language": "python",
    },
    "grafana": {
        "golden_file": "grafana.json",
        "clone_url": "https://github.com/grafana/grafana.git",
        "default_branch": "main",
        "language": "go",
    },
    "cal_dot_com": {
        "golden_file": "cal_dot_com.json",
        "clone_url": "https://github.com/calcom/cal.com.git",
        "default_branch": "main",
        "language": "typescript",
    },
    "discourse": {
        "golden_file": "discourse.json",
        "clone_url": "https://github.com/discourse/discourse.git",
        "default_branch": "main",
        "language": "ruby",
    },
    "keycloak": {
        "golden_file": "keycloak.json",
        "clone_url": "https://github.com/keycloak/keycloak.git",
        "default_branch": "main",
        "language": "java",
    },
}

# Review prompt — invokes the actual /codereview skill.
# The skill directory is symlinked into each benchmark repo during prepare.
REVIEW_PROMPT = """\
/codereview --range {base_ref}..{head_ref}{passes_arg}

After the review is complete, extract ALL findings from the review output and save them
as a JSON array to: {findings_path}

Each element must have at minimum: "pass", "summary", "severity", "file", "line", "evidence".
The "pass" field must be one of: correctness, security, reliability, performance, testing, maintainability.
The file must contain ONLY a valid JSON array, no markdown wrapping.
If the review produced no findings, write: []
"""

# Judge prompt — matches Martian's methodology (semantic matching)
JUDGE_PROMPT = """\
You are evaluating an AI code review tool.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches — different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue
- If both describe the same general problem area in the same code, it's a match

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true or false, "confidence": 0.0 to 1.0}}"""

# Public leaderboard (Martian offline benchmark, Claude Opus 4.5 judge)
LEADERBOARD = [
    ("Cubic v2", 56.3, 68.6, 61.8),
    ("Augment", 47.5, 61.3, 53.5),
    ("Qodo Extended Summary", 40.2, 67.2, 50.3),
    ("Qodo v2.2", 44.6, 54.7, 49.2),
    ("Qodo v2", 42.9, 55.5, 48.4),
    ("Qodo Extended", 37.2, 62.8, 46.7),
    ("Macroscope", 48.4, 43.8, 46.0),
    ("Cursor Bugbot", 47.2, 43.8, 45.5),
    ("Propel", 52.5, 38.7, 44.5),
    ("Devin", 54.3, 37.2, 44.2),
    ("Cubic Dev", 30.1, 75.9, 43.2),
    ("Greptile v4", 33.1, 56.9, 41.8),
    ("Sourcery", 33.3, 51.8, 40.6),
    ("Kodus v2", 46.7, 35.8, 40.5),
    ("Greptile", 41.5, 39.4, 40.4),
    ("Claude Code", 34.8, 40.9, 37.6),
    ("Qodo", 31.8, 44.5, 37.1),
    ("GitHub Copilot", 28.3, 53.3, 37.0),
    ("Baz", 48.8, 29.2, 36.5),
    ("Claude", 34.8, 35.8, 35.3),
    ("CodeRabbit", 24.7, 39.4, 30.3),
]


# ─── Data Structures ─────────────────────────────────────────────────────────


@dataclass
class GoldenComment:
    comment: str
    severity: str


@dataclass
class BenchmarkPR:
    pr_id: str
    repo_key: str
    language: str
    pr_title: str
    url: str
    original_url: str
    pr_number: int
    golden_comments: list[GoldenComment]
    commit_sha: str = ""  # non-empty when original_url points to a commit (not a PR)
    fork_repo: str = ""  # e.g. "ai-code-review-evaluation/discourse-graphite"
    fork_pr_number: int = 0  # PR number in the fork repo


@dataclass
class Finding:
    summary: str
    severity: str = ""
    file: str = ""
    line: int = 0
    evidence: str = ""


@dataclass
class JudgeMatch:
    golden_comment: str
    golden_severity: str
    candidate: str
    confidence: float
    reasoning: str


@dataclass
class PRResult:
    pr_id: str
    repo_key: str
    language: str
    findings_count: int = 0
    true_positives: list[JudgeMatch] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[GoldenComment] = field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0


# ─── Helpers ──────────────────────────────────────────────────────────────────


def git(repo_dir: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a git command in the given repo directory."""
    return subprocess.run(
        ["git", "-C", str(repo_dir)] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def extract_pr_number(url: str) -> int:
    """Extract PR number from a GitHub PR URL."""
    m = re.search(r"/pull/(\d+)", url)
    return int(m.group(1)) if m else 0


def extract_original_pr_number(url: str) -> int:
    """Extract PR number from repo name pattern like sentry__sentry__tool__PR92393__date."""
    m = re.search(r"__PR(\d+)__", url)
    return int(m.group(1)) if m else 0


def parse_golden_comments(benchmark_dir: Path) -> list[BenchmarkPR]:
    """Parse golden comment files into structured PR data."""
    golden_dir = benchmark_dir / GOLDEN_SUBDIR
    prs: list[BenchmarkPR] = []

    for repo_key, config in REPOS.items():
        golden_file = golden_dir / config["golden_file"]
        if not golden_file.exists():
            print(f"  Warning: {golden_file.name} not found, skipping {repo_key}")
            continue

        with open(golden_file) as f:
            entries = json.load(f)

        for entry in entries:
            url = entry.get("url", "")
            original_url = entry.get("original_url", "")

            # Determine how to access this PR's diff
            pr_number = 0
            commit_sha = ""
            fork_repo = ""
            fork_pr_number = 0

            # Check if original_url is a commit (not a PR)
            commit_match = re.search(r"/commit/([0-9a-f]{10,})", original_url or "")
            if commit_match:
                commit_sha = commit_match.group(1)

            # Try to get PR number from original URL
            if not commit_sha and original_url:
                pr_number = extract_pr_number(original_url)
            if pr_number == 0 and not commit_sha:
                pr_number = extract_original_pr_number(url)

            # Extract fork repo and fork PR number from the url field
            fork_match = re.match(
                r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url or ""
            )
            if fork_match:
                fork_repo = fork_match.group(1)
                fork_pr_number = int(fork_match.group(2))

            # If we still have no pr_number and no commit, use fork PR number
            if pr_number == 0 and not commit_sha:
                pr_number = fork_pr_number

            # Skip entries flagged as inaccessible
            az = entry.get("az_comment", "")
            if "not in the repo" in az.lower():
                continue

            # Build a unique PR ID
            if commit_sha:
                pr_id = f"{repo_key}-{commit_sha[:8]}"
            elif pr_number:
                pr_id = f"{repo_key}-{pr_number}"
            else:
                pr_id = f"{repo_key}-unknown-{len(prs)}"

            golden = [
                GoldenComment(
                    comment=c["comment"],
                    severity=c.get("severity", "Medium"),
                )
                for c in entry.get("comments", [])
            ]

            prs.append(
                BenchmarkPR(
                    pr_id=pr_id,
                    repo_key=repo_key,
                    language=config["language"],
                    pr_title=entry.get("pr_title", ""),
                    url=url,
                    original_url=original_url,
                    pr_number=pr_number,
                    golden_comments=golden,
                    commit_sha=commit_sha,
                    fork_repo=fork_repo,
                    fork_pr_number=fork_pr_number,
                )
            )

    return prs


def load_prs() -> list[BenchmarkPR]:
    """Load PR metadata from the saved file."""
    metadata_file = EVAL_DIR / "pr-metadata.json"
    if not metadata_file.exists():
        return []

    with open(metadata_file) as f:
        data = json.load(f)

    prs = []
    for d in data:
        golden = [GoldenComment(**g) for g in d.pop("golden_comments")]
        prs.append(BenchmarkPR(**d, golden_comments=golden))
    return prs


# ─── Setup ────────────────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> bool:
    """Clone the Martian benchmark repository and parse golden comments."""
    benchmark_dir = EVAL_DIR / "benchmark"
    golden_dir = benchmark_dir / GOLDEN_SUBDIR

    if golden_dir.exists() and not getattr(args, "force", False):
        print(f"Benchmark data already at {benchmark_dir}")
    else:
        print("Cloning Martian benchmark repo...")
        if benchmark_dir.exists():
            subprocess.run(["rm", "-rf", str(benchmark_dir)])

        benchmark_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", BENCHMARK_URL, str(benchmark_dir)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(f"Error cloning: {r.stderr}")
            return False

    if not golden_dir.exists():
        print(f"Error: golden comments not found at {golden_dir}")
        return False

    prs = parse_golden_comments(benchmark_dir)
    total_golden = sum(len(pr.golden_comments) for pr in prs)
    print(f"Parsed {len(prs)} PRs with {total_golden} golden comments")

    for repo_key in REPOS:
        rp = [p for p in prs if p.repo_key == repo_key]
        gc = sum(len(p.golden_comments) for p in rp)
        lang = REPOS[repo_key]["language"]
        print(
            f"  {repo_key:<14s} {lang:<12s} {len(rp):>2d} PRs, {gc:>3d} golden comments"
        )

    # Save metadata for later steps
    metadata_file = EVAL_DIR / "pr-metadata.json"
    with open(metadata_file, "w") as f:
        json.dump([asdict(pr) for pr in prs], f, indent=2)

    print(f"Metadata saved to {metadata_file}")
    return True


# ─── Prepare ──────────────────────────────────────────────────────────────────


def cmd_prepare(args: argparse.Namespace) -> bool:
    """Clone source repos (blobless) and fetch PR refs."""
    repos_dir = EVAL_DIR / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)

    prs = load_prs()
    if not prs:
        print("No PR metadata found. Run 'setup' first.")
        return False

    # Group PRs by repo
    by_repo: dict[str, list[BenchmarkPR]] = {}
    for pr in prs:
        by_repo.setdefault(pr.repo_key, []).append(pr)

    for repo_key, repo_prs in by_repo.items():
        config = REPOS[repo_key]
        repo_dir = repos_dir / repo_key

        # Clone repo (blobless — downloads tree structure, fetches blobs on demand)
        if not (repo_dir / ".git").exists():
            print(f"\nCloning {repo_key} (blobless, this may take a minute)...")
            r = subprocess.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    config["clone_url"],
                    str(repo_dir),
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if r.returncode != 0:
                print(f"  Error: {r.stderr.strip()}")
                # Try shallow clone as fallback
                print("  Retrying with shallow clone...")
                r = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        config["clone_url"],
                        str(repo_dir),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if r.returncode != 0:
                    print(f"  Failed: {r.stderr.strip()}")
                    continue
        else:
            print(f"\n{repo_key}: already cloned")

        # Symlink our skill into the repo so claude -p can discover it
        skills_target = repo_dir / "skills" / "codereview"
        if not skills_target.exists():
            skills_target.parent.mkdir(parents=True, exist_ok=True)
            skills_target.symlink_to(SKILL_DIR)
            print(f"  Linked skill → {SKILL_DIR}")

        # Fetch PR refs
        fetched = 0
        skipped = 0
        failed = 0
        for pr in repo_prs:
            # Case 1: Commit-based PR (original_url points to a commit)
            if pr.commit_sha:
                r = git(repo_dir, "cat-file", "-t", pr.commit_sha)
                if r.returncode == 0:
                    skipped += 1
                else:
                    # Fetch history deep enough to include this commit
                    r = git(repo_dir, "fetch", "origin", "--depth=500", timeout=120)
                    r2 = git(repo_dir, "cat-file", "-t", pr.commit_sha)
                    if r2.returncode == 0:
                        fetched += 1
                    else:
                        print(f"    {pr.pr_id}: commit {pr.commit_sha[:10]} not found")
                        failed += 1
                continue

            if pr.pr_number == 0:
                failed += 1
                continue

            ref_name = f"pr-{pr.pr_number}"
            r = git(repo_dir, "rev-parse", "--verify", "--quiet", ref_name)
            if r.returncode == 0:
                skipped += 1
                continue

            # Case 2: Normal PR — fetch from original repo
            r = git(
                repo_dir,
                "fetch",
                "origin",
                f"pull/{pr.pr_number}/head:{ref_name}",
                timeout=120,
            )
            if r.returncode == 0:
                fetched += 1
                continue

            # Case 3: Fork-based PR — fetch from fork repo
            if pr.fork_repo and pr.fork_pr_number:
                remote = f"fork-{pr.fork_repo.replace('/', '-')}"
                git(
                    repo_dir,
                    "remote",
                    "add",
                    remote,
                    f"https://github.com/{pr.fork_repo}.git",
                )
                r = git(
                    repo_dir,
                    "fetch",
                    remote,
                    f"pull/{pr.fork_pr_number}/head:{ref_name}",
                    timeout=120,
                )
                if r.returncode == 0:
                    fetched += 1
                    continue

            print(f"    {pr.pr_id}: could not fetch PR ref")
            failed += 1

        print(f"  PRs: {fetched} fetched, {skipped} cached, {failed} failed")

    return True


# ─── Review ───────────────────────────────────────────────────────────────────


def get_merge_base(repo_dir: Path, default_branch: str, head_ref: str) -> Optional[str]:
    """Find the merge base between default branch and PR head."""
    # First ensure we have the default branch
    git(repo_dir, "fetch", "origin", default_branch, timeout=120)

    r = git(repo_dir, "merge-base", f"origin/{default_branch}", head_ref)
    if r.returncode == 0:
        return r.stdout.strip()[:12]
    return None


def run_single_review(
    pr: BenchmarkPR,
    repo_dir: Path,
    reviews_dir: Path,
    model: str,
    passes: list[str] | None = None,
) -> bool:
    """Run a code review on a single PR via claude -p."""
    passes_arg = f" --passes {','.join(passes)}" if passes else ""
    default_branch = REPOS[pr.repo_key]["default_branch"]

    # Determine base_ref and head_ref based on PR type
    if pr.commit_sha:
        # Commit-based: diff is commit~1..commit
        head_ref = pr.commit_sha
        base_ref = f"{pr.commit_sha}~1"
    else:
        head_ref = f"pr-{pr.pr_number}"
        base_ref = get_merge_base(repo_dir, default_branch, head_ref)
        if not base_ref:
            with _print_lock:
                print(f"    [{pr.pr_id}] Could not find merge base")
            return False

    # Verify the range has actual changes
    r = git(repo_dir, "diff", "--stat", f"{base_ref}..{head_ref}")
    if not r.stdout.strip():
        with _print_lock:
            print(f"    [{pr.pr_id}] No diff between {base_ref}..{head_ref}")
        return False

    # PR-specific temp file — use RELATIVE path so Claude writes it in repo cwd
    findings_filename = f".eval-findings-{pr.pr_id}.json"
    findings_path = repo_dir / findings_filename
    if findings_path.exists():
        findings_path.unlink()
    # Also clean up any doubled-path files from previous runs
    doubled = repo_dir / str(findings_path)
    if doubled.exists():
        doubled.unlink()

    prompt = REVIEW_PROMPT.format(
        base_ref=base_ref,
        head_ref=head_ref,
        findings_path=findings_filename,  # relative, not absolute
        passes_arg=passes_arg,
    )

    with _print_lock:
        print(
            f"    [{pr.pr_id}] Reviewing {base_ref[:8]}..{head_ref} ({pr.language})..."
        )
    start = time.time()

    try:
        r = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--max-turns",
                "100",
                "--dangerously-skip-permissions",
            ],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min — full skill with explorers needs time
        )
    except FileNotFoundError:
        print("    Error: 'claude' CLI not found. Install Claude Code first.")
        return False
    except subprocess.TimeoutExpired:
        print("    Timeout (1800s)")
        return False

    elapsed = time.time() - start

    if r.returncode != 0:
        findings_path.unlink(missing_ok=True)
        error_file = reviews_dir / f"{pr.pr_id}.raw.json"
        with open(error_file, "w") as f:
            json.dump(
                {
                    "elapsed_s": elapsed,
                    "returncode": r.returncode,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                },
                f,
                indent=2,
            )
        with _print_lock:
            detail = (r.stderr or r.stdout or "claude review failed").strip()
            print(f"    [{pr.pr_id}] Claude review failed: {detail[:240]}")
        return False

    # Parse claude JSON envelope for metadata (cost, tokens, turns)
    claude_meta: dict = {}
    claude_result_text = ""
    if r.returncode == 0 and r.stdout.strip():
        try:
            envelope = json.loads(r.stdout)
            claude_result_text = envelope.get("result", "")
            claude_meta = {
                k: envelope.get(k)
                for k in (
                    "cost_usd",
                    "duration_ms",
                    "duration_api_ms",
                    "num_turns",
                    "total_cost_usd",
                    "session_id",
                )
                if envelope.get(k) is not None
            }
        except json.JSONDecodeError:
            claude_result_text = r.stdout

    # Collect findings — prefer the file we asked Claude to write
    findings: list[dict] = []
    if findings_path.exists():
        try:
            with open(findings_path) as f:
                raw = json.load(f)
            if isinstance(raw, list):
                findings = raw
        except (json.JSONDecodeError, TypeError):
            pass
        findings_path.unlink(missing_ok=True)

    # Fallback: extract JSON array from Claude's text output
    if not findings and claude_result_text:
        findings = _extract_json_array(claude_result_text)

    # Retry detection: 1-2 turn reviews with 0 findings are likely stuck reviews
    # where haiku invoked /codereview atomically and the skill failed silently.
    num_turns = claude_meta.get("num_turns", 0)
    if not findings and num_turns <= 2 and elapsed > 30:
        with _print_lock:
            print(
                f"    [{pr.pr_id}] 0 findings in {num_turns} turns ({elapsed:.0f}s) — retrying..."
            )
        # Also check doubled path
        for candidate in repo_dir.rglob(f".eval-findings-{pr.pr_id}.json"):
            try:
                recovered = json.load(open(candidate))
                if isinstance(recovered, list) and recovered:
                    findings = recovered
                    with _print_lock:
                        print(
                            f"    [{pr.pr_id}] Recovered {len(findings)} findings from {candidate}"
                        )
                    break
            except (json.JSONDecodeError, TypeError):
                pass

    # Save findings + metadata together
    output_file = reviews_dir / f"{pr.pr_id}.json"
    with open(output_file, "w") as f:
        json.dump(findings, f, indent=2)

    # Save raw claude output envelope for debugging/timing analysis
    raw_file = reviews_dir / f"{pr.pr_id}.raw.json"
    with open(raw_file, "w") as f:
        json.dump(
            {
                "claude_meta": claude_meta,
                "elapsed_s": elapsed,
                "findings_count": len(findings),
                "result_text_length": len(claude_result_text),
            },
            f,
            indent=2,
        )

    # Print timing summary
    meta_parts = [f"{len(findings)} findings", f"{elapsed:.0f}s wall"]
    if claude_meta.get("num_turns"):
        meta_parts.append(f"{claude_meta['num_turns']} turns")
    if claude_meta.get("cost_usd"):
        meta_parts.append(f"${claude_meta['cost_usd']:.2f}")
    with _print_lock:
        print(f"    [{pr.pr_id}] {', '.join(meta_parts)} → {output_file.name}")
    return True


def _extract_json_array(text: str) -> list[dict]:
    """Try to find and parse a JSON array from unstructured text."""
    # Look for JSON arrays (greedy, try largest matches first)
    for m in re.finditer(r"\[[\s\S]*?\]", text):
        try:
            arr = json.loads(m.group())
            if isinstance(arr, list) and all(isinstance(x, dict) for x in arr):
                return arr
        except json.JSONDecodeError:
            continue
    return []


def cmd_review(args: argparse.Namespace) -> bool:
    """Run code reviews on benchmark PRs."""
    reviews_dir = EVAL_DIR / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    repos_dir = EVAL_DIR / "repos"

    prs = load_prs()
    if not prs:
        print("No PR metadata. Run 'setup' first.")
        return False

    # Filters
    if getattr(args, "repo", None):
        prs = [p for p in prs if p.repo_key in args.repo.split(",")]
    if getattr(args, "pr", None):
        prs = [p for p in prs if p.pr_id in args.pr.split(",")]
    if getattr(args, "limit", None):
        prs = prs[: args.limit]

    # Resume: skip completed reviews
    if getattr(args, "resume", False):
        remaining = []
        for pr in prs:
            if (reviews_dir / f"{pr.pr_id}.json").exists():
                pass  # already done
            else:
                remaining.append(pr)
        skipped = len(prs) - len(remaining)
        if skipped:
            print(f"Resuming: skipping {skipped} already-reviewed PRs")
        prs = remaining

    if not prs:
        print("Nothing to review (all done or no matching PRs).")
        return True

    model = getattr(args, "model", "haiku") or "haiku"
    workers = getattr(args, "workers", 5) or 5
    print(f"Reviewing {len(prs)} PRs with model={model}, workers={workers}...\n")

    # Pre-validate PRs and build work items
    work: list[tuple[BenchmarkPR, Path]] = []
    skipped = 0
    for pr in prs:
        repo_dir = repos_dir / pr.repo_key
        if not (repo_dir / ".git").exists():
            print(f"  {pr.pr_id}: repo not cloned, skipping")
            skipped += 1
            continue
        if pr.commit_sha:
            # Commit-based: verify the commit exists
            r = git(repo_dir, "cat-file", "-t", pr.commit_sha)
            if r.returncode != 0:
                print(f"  {pr.pr_id}: commit {pr.commit_sha[:10]} not found, skipping")
                skipped += 1
                continue
        elif pr.pr_number == 0:
            print(f"  {pr.pr_id}: no PR number or commit, skipping")
            skipped += 1
            continue
        else:
            r = git(repo_dir, "rev-parse", "--verify", "--quiet", f"pr-{pr.pr_number}")
            if r.returncode != 0:
                print(f"  {pr.pr_id}: PR ref not found, skipping")
                skipped += 1
                continue
        work.append((pr, repo_dir))

    if skipped:
        print(f"  ({skipped} PRs skipped due to missing data)\n")

    success = 0
    failed = 0
    completed = 0

    passes_str = getattr(args, "passes", None)
    if passes_str is None:
        # Default for Martian benchmark: correctness + reliability
        # Golden set is correctness bugs; reliability catches error handling issues.
        # Other passes (security, testing, maintainability) produce valid findings
        # that don't match the golden set and artificially depress precision.
        passes_str = "correctness,reliability"
    passes = (
        [p.strip() for p in passes_str.split(",") if p.strip()] if passes_str else None
    )
    if passes_str == "all":
        passes = None  # --passes all overrides the default
    if passes:
        print(f"  Expert filter: {', '.join(passes)}\n")

    def _review_worker(item: tuple[BenchmarkPR, Path]) -> bool:
        pr, repo_dir = item
        return run_single_review(pr, repo_dir, reviews_dir, model, passes=passes)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_review_worker, item): item[0] for item in work}
        for future in concurrent.futures.as_completed(futures):
            pr = futures[future]
            completed += 1
            try:
                if future.result():
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                with _print_lock:
                    print(f"    [{pr.pr_id}] Error: {e}")
                failed += 1
            with _print_lock:
                print(f"  Progress: {completed}/{len(work)} done")

    print(f"\nDone: {success} reviewed, {failed} failed, {skipped} skipped")
    return True


# ─── Judge ────────────────────────────────────────────────────────────────────

JUDGE_BATCH_PROMPT = """\
You are evaluating an AI code review tool. For each pair below, determine if the \
candidate issue matches the golden (expected) comment.

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches — different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue
- If both describe the same general problem area in the same code, it's a match

Pairs to evaluate:
{pairs_text}

Respond with ONLY a JSON array (one object per pair):
[{{"pair": 1, "match": true, "confidence": 0.95, "reasoning": "brief explanation"}}, ...]"""

JUDGE_BATCH_SIZE = 15  # pairs per claude call


def judge_batch(pairs: list[tuple[int, int, str, str]], model: str) -> list[dict]:
    """Judge multiple (golden, candidate) pairs in a single claude -p call."""
    pairs_text = "\n".join(
        f'[{i + 1}] Golden: "{g[:300]}" | Candidate: "{c[:300]}"'
        for i, (_, _, g, c) in enumerate(pairs)
    )
    prompt = JUDGE_BATCH_PROMPT.format(pairs_text=pairs_text)

    try:
        r = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--max-turns",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "judge batch failed").strip()
            raise RuntimeError(f"claude judge failed: {detail[:240]}")
        text = r.stdout.strip()
        try:
            envelope = json.loads(text)
            text = envelope.get("result", text)
        except json.JSONDecodeError:
            pass

        # Find JSON array in response
        arr = _extract_json_array(text)
        if not arr or len(arr) != len(pairs):
            # Try individual JSON objects as fallback
            results = []
            for m in re.finditer(r"\{[^{}]*\}", text):
                try:
                    results.append(json.loads(m.group()))
                except json.JSONDecodeError:
                    continue
            if len(results) == len(pairs):
                arr = results
            else:
                raise RuntimeError(
                    f"judge batch returned {len(results) if results else 0} results for {len(pairs)} pairs"
                )

        return [
            {
                "match": bool(item.get("match", False)),
                "confidence": float(item.get("confidence", 0.0)),
                "reasoning": str(item.get("reasoning", "")),
            }
            for item in arr
        ]
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("claude judge timed out") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("claude CLI not found") from exc


def judge_pr(
    pr: BenchmarkPR,
    candidates: list[str],
    model: str,
) -> PRResult:
    """Score all candidates for a PR against its golden comments."""
    if not candidates:
        return PRResult(
            pr_id=pr.pr_id,
            repo_key=pr.repo_key,
            language=pr.language,
            findings_count=0,
            false_negatives=list(pr.golden_comments),
        )

    # Build all (golden_idx, cand_idx, golden_text, cand_text) pairs
    all_pairs: list[tuple[int, int, str, str]] = []
    for gi, golden in enumerate(pr.golden_comments):
        for ci, cand in enumerate(candidates):
            all_pairs.append((gi, ci, golden.comment, cand))

    # Judge in batches
    all_matches: list[tuple[int, int, dict]] = []
    for batch_start in range(0, len(all_pairs), JUDGE_BATCH_SIZE):
        batch = all_pairs[batch_start : batch_start + JUDGE_BATCH_SIZE]
        results = judge_batch(batch, model)
        for (gi, ci, _, _), result in zip(batch, results):
            if result["match"]:
                all_matches.append((gi, ci, result))

    # Greedy assignment: highest confidence first, each golden/candidate used once
    all_matches.sort(key=lambda x: x[2]["confidence"], reverse=True)
    used_golden: set[int] = set()
    used_cand: set[int] = set()
    true_positives: list[JudgeMatch] = []

    for gi, ci, result in all_matches:
        if gi in used_golden or ci in used_cand:
            continue
        used_golden.add(gi)
        used_cand.add(ci)
        true_positives.append(
            JudgeMatch(
                golden_comment=pr.golden_comments[gi].comment,
                golden_severity=pr.golden_comments[gi].severity,
                candidate=candidates[ci],
                confidence=result["confidence"],
                reasoning=result["reasoning"],
            )
        )

    fp = [candidates[i] for i in range(len(candidates)) if i not in used_cand]
    fn = [
        pr.golden_comments[i]
        for i in range(len(pr.golden_comments))
        if i not in used_golden
    ]

    tp = len(true_positives)
    n_cand = len(candidates)
    n_gold = len(pr.golden_comments)
    precision = tp / n_cand if n_cand > 0 else 0.0
    recall = tp / n_gold if n_gold > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return PRResult(
        pr_id=pr.pr_id,
        repo_key=pr.repo_key,
        language=pr.language,
        findings_count=n_cand,
        true_positives=true_positives,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def _load_findings_for_pr(reviews_dir: Path, pr_id: str) -> list[dict]:
    """Load the full findings JSON for a PR (for post-hoc analysis)."""
    f = reviews_dir / f"{pr_id}.json"
    if f.exists():
        try:
            with open(f) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def cmd_judge(args: argparse.Namespace) -> bool:
    """Score review findings against golden comments using claude -p as judge."""
    reviews_dir = EVAL_DIR / "reviews"
    results_dir = EVAL_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    prs = load_prs()
    if not prs:
        print("No PR metadata. Run 'setup' first.")
        return False

    if getattr(args, "repo", None):
        prs = [p for p in prs if p.repo_key in args.repo.split(",")]

    judge_model = getattr(args, "judge_model", None) or "sonnet"
    workers = getattr(args, "workers", 5) or 5
    print(f"Judging with model={judge_model}, workers={workers}...\n")

    # Build work items: (pr, candidates) pairs
    work: list[tuple[BenchmarkPR, list[str]]] = []
    total_pairs = 0
    for pr in prs:
        findings_file = reviews_dir / f"{pr.pr_id}.json"
        if not findings_file.exists():
            continue

        with open(findings_file) as f:
            findings_data = json.load(f)

        candidates: list[str] = []
        for fd in findings_data:
            summary = fd.get("summary", "").strip()
            evidence = fd.get("evidence", "").strip()
            if summary:
                text = summary
                if evidence:
                    text += f" Evidence: {evidence}"
                candidates.append(text)

        n_pairs = len(candidates) * len(pr.golden_comments)
        n_batches = (
            (n_pairs + JUDGE_BATCH_SIZE - 1) // JUDGE_BATCH_SIZE if n_pairs else 0
        )
        total_pairs += n_pairs
        print(
            f"  {pr.pr_id}: {len(candidates)} findings × {len(pr.golden_comments)} golden = {n_pairs} pairs ({n_batches} batches)"
        )
        work.append((pr, candidates))

    print(f"\nTotal: {total_pairs} pairs across {len(work)} PRs. Judging...\n")

    all_results: list[PRResult] = []
    completed = 0

    def _judge_worker(item: tuple[BenchmarkPR, list[str]]) -> PRResult:
        pr, cands = item
        return judge_pr(pr, cands, judge_model)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_judge_worker, item): item[0] for item in work}
        for future in concurrent.futures.as_completed(futures):
            pr = futures[future]
            completed += 1
            try:
                result = future.result()
                all_results.append(result)
                tp = len(result.true_positives)
                fp = len(result.false_positives)
                fn = len(result.false_negatives)
                with _print_lock:
                    print(
                        f"  [{completed}/{len(work)}] {pr.pr_id}: TP={tp} FP={fp} FN={fn}  P={result.precision:.0%} R={result.recall:.0%} F1={result.f1:.0%}"
                    )
            except Exception as e:
                with _print_lock:
                    print(f"  [{completed}/{len(work)}] {pr.pr_id}: Error: {e}")
                all_results.append(
                    PRResult(
                        pr_id=pr.pr_id,
                        repo_key=pr.repo_key,
                        language=pr.language,
                        false_negatives=list(pr.golden_comments),
                    )
                )

    if not all_results:
        print("No review findings to judge. Run 'review' first.")
        return False

    # Aggregate metrics
    sum_tp = sum(len(r.true_positives) for r in all_results)
    sum_cand = sum(r.findings_count for r in all_results)
    sum_gold = sum(len(r.true_positives) + len(r.false_negatives) for r in all_results)

    agg_p = sum_tp / sum_cand if sum_cand > 0 else 0.0
    agg_r = sum_tp / sum_gold if sum_gold > 0 else 0.0
    agg_f1 = 2 * agg_p * agg_r / (agg_p + agg_r) if (agg_p + agg_r) > 0 else 0.0

    # Per-language
    by_lang: dict[str, dict] = {}
    for r in all_results:
        d = by_lang.setdefault(r.language, {"tp": 0, "cand": 0, "gold": 0})
        d["tp"] += len(r.true_positives)
        d["cand"] += r.findings_count
        d["gold"] += len(r.true_positives) + len(r.false_negatives)

    lang_metrics: dict[str, dict] = {}
    for lang, d in by_lang.items():
        p = d["tp"] / d["cand"] if d["cand"] > 0 else 0.0
        rc = d["tp"] / d["gold"] if d["gold"] > 0 else 0.0
        f = 2 * p * rc / (p + rc) if (p + rc) > 0 else 0.0
        lang_metrics[lang] = {"precision": p, "recall": rc, "f1": f, **d}

    # Save results
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = {
        "timestamp": datetime.now().isoformat(),
        "judge_model": judge_model,
        "review_model": "haiku (orchestrator) + sonnet (explorers) + opus (judge)",
        "prs_evaluated": len(all_results),
        "total_judge_pairs": total_pairs,
        "aggregate": {
            "precision": agg_p,
            "recall": agg_r,
            "f1": agg_f1,
            "true_positives": sum_tp,
            "total_candidates": sum_cand,
            "total_golden": sum_gold,
        },
        "by_language": lang_metrics,
        "per_pr": [
            {
                "pr_id": r.pr_id,
                "repo_key": r.repo_key,
                "language": r.language,
                "findings_count": r.findings_count,
                "precision": r.precision,
                "recall": r.recall,
                "f1": r.f1,
                "tp": len(r.true_positives),
                "fp": len(r.false_positives),
                "fn": len(r.false_negatives),
                "true_positives": [asdict(m) for m in r.true_positives],
                "false_positives": r.false_positives,
                "false_negatives": [asdict(g) for g in r.false_negatives],
                "all_findings": _load_findings_for_pr(reviews_dir, r.pr_id),
            }
            for r in all_results
        ],
    }

    results_file = results_dir / f"eval-{ts}.json"
    with open(results_file, "w") as f:
        json.dump(output, f, indent=2)

    latest_file = results_dir / "latest.json"
    with open(latest_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to {results_file}")

    # Auto-ingest into SQLite
    try:
        store = _get_store()
        store.ensure_benchmark(
            "martian-offline",
            "Martian CodeReBench Offline",
            "https://codereview.withmartian.com/",
        )
        prs = load_prs()
        for pr_data in prs:
            bp_id = store.ensure_benchmark_pr(
                "martian-offline",
                {
                    "pr_id": pr_data.pr_id,
                    "repo_key": pr_data.repo_key,
                    "language": pr_data.language,
                    "pr_title": pr_data.pr_title,
                    "pr_number": pr_data.pr_number,
                    "commit_sha": pr_data.commit_sha,
                },
            )
            store.ensure_golden_comments(
                bp_id,
                [
                    {"comment": g.comment, "severity": g.severity}
                    for g in pr_data.golden_comments
                ],
            )
        run_id = store.import_from_json("martian-offline", output, reviews_dir)
        store.close()
        print(f"Ingested into DB as run {run_id}")
    except Exception as e:
        print(f"DB ingest warning: {e}")

    return True


# ─── Report ───────────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> bool:
    """Display a comparison report against the public leaderboard."""
    latest = EVAL_DIR / "results" / "latest.json"
    if not latest.exists():
        print("No results found. Run 'judge' first.")
        return False

    with open(latest) as f:
        data = json.load(f)

    agg = data["aggregate"]
    our_p = agg["precision"] * 100
    our_r = agg["recall"] * 100
    our_f1 = agg["f1"] * 100

    # Determine our rank
    rank = 1
    for _, _, _, f1 in LEADERBOARD:
        if f1 > our_f1:
            rank += 1

    w = 72
    print("=" * w)
    print("  Martian CodeReBench — Evaluation Report")
    print(f"  {data['timestamp'][:19]}  |  Judge: {data['judge_model']}")
    print(
        f"  PRs evaluated: {data['prs_evaluated']}  |  Judge pairs: {data.get('total_judge_pairs', '?')}"
    )
    print("=" * w)

    print(f"\n  {'OUR RESULTS':^{w - 4}}")
    print(f"  {'─' * (w - 4)}")
    print(
        f"    Precision:  {our_p:5.1f}%   ({agg['true_positives']} correct / {agg['total_candidates']} findings)"
    )
    print(
        f"    Recall:     {our_r:5.1f}%   ({agg['true_positives']} caught / {agg['total_golden']} golden)"
    )
    print(f"    F1 Score:   {our_f1:5.1f}%   (rank #{rank} of {len(LEADERBOARD) + 1})")

    print("\n  BY LANGUAGE")
    print(f"  {'─' * (w - 4)}")
    for lang, m in sorted(data.get("by_language", {}).items()):
        p = m["precision"] * 100
        r = m["recall"] * 100
        f = m["f1"] * 100
        print(
            f"    {lang:<14s}  P {p:5.1f}%   R {r:5.1f}%   F1 {f:5.1f}%   (TP {m['tp']}/{m['gold']})"
        )

    print("\n  LEADERBOARD COMPARISON")
    print(f"  {'─' * (w - 4)}")
    print(f"    {'#':<5s} {'Tool':<28s} {'Prec':>6s} {'Rec':>6s} {'F1':>6s}")
    print(f"    {'─' * 5} {'─' * 28} {'─' * 6} {'─' * 6} {'─' * 6}")

    inserted = False
    for i, (name, p, r, f1) in enumerate(LEADERBOARD, 1):
        if not inserted and our_f1 >= f1:
            print(
                f"  → #{rank:<4d} {'** Our Skill **':<28s} {our_p:5.1f}% {our_r:5.1f}% {our_f1:5.1f}%  ←"
            )
            inserted = True
        print(f"    #{i:<4d} {name:<28s} {p:5.1f}% {r:5.1f}% {f1:5.1f}%")

    if not inserted:
        print(
            f"  → #{rank:<4d} {'** Our Skill **':<28s} {our_p:5.1f}% {our_r:5.1f}% {our_f1:5.1f}%  ←"
        )

    # Best/worst PRs
    per_pr = data.get("per_pr", [])
    if per_pr:
        by_f1 = sorted(per_pr, key=lambda x: x["f1"])
        print("\n  WORST PRs")
        print(f"  {'─' * (w - 4)}")
        for p in by_f1[:3]:
            print(
                f"    {p['pr_id']:<22s} F1 {p['f1'] * 100:5.1f}%  TP {p['tp']}  FP {p['fp']}  FN {p['fn']}"
            )

        print("\n  BEST PRs")
        print(f"  {'─' * (w - 4)}")
        for p in by_f1[-3:]:
            print(
                f"    {p['pr_id']:<22s} F1 {p['f1'] * 100:5.1f}%  TP {p['tp']}  FP {p['fp']}  FN {p['fn']}"
            )

    # Compare with previous run (if exists)
    results_dir = EVAL_DIR / "results"
    all_results = sorted(results_dir.glob("eval-*.json"))
    if len(all_results) >= 2:
        prev_file = all_results[-2]
        with open(prev_file) as f:
            prev = json.load(f)
        prev_f1 = prev["aggregate"]["f1"] * 100
        delta = our_f1 - prev_f1
        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"
        print("\n  TREND")
        print(f"  {'─' * (w - 4)}")
        print(f"    Previous: F1 {prev_f1:5.1f}%  ({prev['timestamp'][:10]})")
        print(f"    Current:  F1 {our_f1:5.1f}%  {arrow} {abs(delta):+.1f}pp")

    print(f"\n  Full results: {latest}")
    print("=" * w)
    return True


# ─── Classify ─────────────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are an independent code review quality auditor. For each finding below, determine \
whether it describes a real issue in the code.

You will receive the git diff and a list of findings. For each finding, read the cited \
file and line, verify the claim against the actual code, and classify it.

## Git Diff
```
{diff}
```

## Findings to Classify
{findings_text}

## Instructions

For each finding, assign:
- **category**: one of: confirmed_bug, confirmed_vuln, valid_concern, nitpick, speculative, wrong
  - confirmed_bug: code will produce wrong behavior under realistic conditions
  - confirmed_vuln: exploitable security issue with concrete attack path
  - valid_concern: real issue but lower impact (missing test, poor error handling, race potential)
  - nitpick: technically correct but not worth fixing (naming, minor style, defensive improvement)
  - speculative: plausible but no concrete failure scenario, or requires unlikely conditions
  - wrong: finding misunderstands the code, or a defense exists that makes it moot
- **relevance**: 1-10 (how important is this if the category is correct?)
- **confidence**: 0.0-1.0 (how sure are you about the classification?)
- **reasoning**: one sentence explaining your classification

Respond with ONLY a JSON array, one object per finding:
[{{"finding_index": 0, "category": "confirmed_bug", "relevance": 8, "confidence": 0.9, "reasoning": "..."}}]"""

CATEGORIES_REAL = {"confirmed_bug", "confirmed_vuln", "valid_concern"}
CATEGORIES_CORRECT = {"confirmed_bug", "confirmed_vuln", "valid_concern", "nitpick"}
CATEGORIES_ALL = {
    "confirmed_bug",
    "confirmed_vuln",
    "valid_concern",
    "nitpick",
    "speculative",
    "wrong",
}


def _run_classifier(
    prompt: str, engine: str, cwd: str, output_file: Path
) -> list[dict]:
    """Run a classification prompt via claude or codex CLI. Returns parsed JSON array."""
    if output_file.exists():
        output_file.unlink()

    if engine == "claude":
        try:
            r = subprocess.run(
                [
                    "claude",
                    "-p",
                    prompt,
                    "--output-format",
                    "json",
                    "--model",
                    "sonnet",
                    "--max-turns",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=cwd,
            )
            text = r.stdout.strip()
            try:
                text = json.loads(text).get("result", text)
            except json.JSONDecodeError:
                pass
            return _extract_json_array(text)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    elif engine == "codex":
        try:
            r = subprocess.run(
                [
                    "codex",
                    "exec",
                    "-o",
                    str(output_file),
                    "--dangerously-bypass-approvals-and-sandbox",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=cwd,
            )
            if output_file.exists():
                text = output_file.read_text()
                output_file.unlink(missing_ok=True)
            else:
                text = r.stdout.strip()
            return _extract_json_array(text)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    return []


def _merge_classifications(
    claude_results: list[dict],
    codex_results: list[dict],
    n_findings: int,
) -> list[dict]:
    """Merge two classifier outputs into a council verdict per finding."""

    # Severity ordering for "adjacent" detection
    severity_rank = {
        "confirmed_bug": 5,
        "confirmed_vuln": 5,
        "valid_concern": 4,
        "nitpick": 3,
        "speculative": 2,
        "wrong": 1,
    }

    merged = []
    for i in range(n_findings):
        c = next((r for r in claude_results if r.get("finding_index") == i), None)
        x = next((r for r in codex_results if r.get("finding_index") == i), None)

        if not c and not x:
            merged.append(
                {
                    "finding_index": i,
                    "category": "speculative",
                    "relevance": 0,
                    "confidence": 0,
                    "agreement": "no_data",
                    "claude": None,
                    "codex": None,
                }
            )
            continue

        if not c or not x:
            # Single model available — use it directly
            sole = c or x
            source = "claude" if c else "codex"
            merged.append(
                {
                    "finding_index": i,
                    "category": sole.get("category", "speculative"),
                    "relevance": sole.get("relevance", 5),
                    "confidence": sole.get("confidence", 0.5),
                    "agreement": f"single_{source}",
                    "claude": c,
                    "codex": x,
                }
            )
            continue

        c_cat = c.get("category", "speculative")
        x_cat = x.get("category", "speculative")
        c_rel = c.get("relevance", 5)
        x_rel = x.get("relevance", 5)
        c_conf = c.get("confidence", 0.5)
        x_conf = x.get("confidence", 0.5)

        if c_cat == x_cat:
            agreement = "agree"
            category = c_cat
        elif abs(severity_rank.get(c_cat, 0) - severity_rank.get(x_cat, 0)) <= 1:
            agreement = "soft_disagree"
            # Use the more severe category
            category = (
                c_cat
                if severity_rank.get(c_cat, 0) >= severity_rank.get(x_cat, 0)
                else x_cat
            )
        else:
            agreement = "disputed"
            # Use higher-confidence model's category
            category = c_cat if c_conf >= x_conf else x_cat

        merged.append(
            {
                "finding_index": i,
                "category": category,
                "relevance": round((c_rel + x_rel) / 2, 1),
                "confidence": round((c_conf + x_conf) / 2, 2),
                "agreement": agreement,
                "claude": c,
                "codex": x,
            }
        )

    return merged


def classify_pr(
    pr: BenchmarkPR,
    findings: list[dict],
    repo_dir: Path,
    workers: int,
) -> dict:
    """Classify all findings for a PR using a two-model council."""
    # Get the diff for context
    if pr.commit_sha:
        r = git(repo_dir, "diff", f"{pr.commit_sha}~1..{pr.commit_sha}")
    else:
        default_branch = REPOS[pr.repo_key]["default_branch"]
        base = get_merge_base(repo_dir, default_branch, f"pr-{pr.pr_number}")
        r = git(repo_dir, "diff", f"{base}..pr-{pr.pr_number}")
    diff_text = r.stdout[:15000]  # Cap diff to avoid token overload

    # Build findings text
    findings_text = "\n".join(
        f"[{i}] severity={f.get('severity', '?')} file={f.get('file', '?')}:{f.get('line', '?')}\n"
        f"    summary: {f.get('summary', '?')}\n"
        f"    evidence: {f.get('evidence', '?')[:200]}"
        for i, f in enumerate(findings)
    )

    prompt = CLASSIFY_PROMPT.format(diff=diff_text, findings_text=findings_text)
    tmp = EVAL_DIR / "classify-tmp"
    tmp.mkdir(exist_ok=True)

    # Run both classifiers in parallel
    claude_results: list[dict] = []
    codex_results: list[dict] = []

    def run_claude():
        nonlocal claude_results
        claude_results = _run_classifier(
            prompt, "claude", str(repo_dir), tmp / f"{pr.pr_id}-claude.txt"
        )

    def run_codex():
        nonlocal codex_results
        codex_results = _run_classifier(
            prompt, "codex", str(repo_dir), tmp / f"{pr.pr_id}-codex.txt"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(run_claude)
        pool.submit(run_codex)
        pool.shutdown(wait=True)

    # Merge
    merged = _merge_classifications(claude_results, codex_results, len(findings))

    # Compute category counts
    cats = {}
    for m in merged:
        cat = m["category"]
        cats[cat] = cats.get(cat, 0) + 1

    n = len(findings)
    real = sum(1 for m in merged if m["category"] in CATEGORIES_REAL)
    correct = sum(1 for m in merged if m["category"] in CATEGORIES_CORRECT)
    disputed = sum(1 for m in merged if m["agreement"] == "disputed")

    return {
        "pr_id": pr.pr_id,
        "repo_key": pr.repo_key,
        "language": pr.language,
        "findings_count": n,
        "classifications": merged,
        "category_counts": cats,
        "adjusted_precision": real / n if n else 0,
        "inclusive_precision": correct / n if n else 0,
        "disputed_count": disputed,
    }


def cmd_classify(args: argparse.Namespace) -> bool:
    """Classify findings quality using a two-model council (Claude + Codex)."""
    reviews_dir = EVAL_DIR / "reviews"
    results_dir = EVAL_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    repos_dir = EVAL_DIR / "repos"

    prs = load_prs()
    if not prs:
        print("No PR metadata. Run 'setup' first.")
        return False

    if getattr(args, "repo", None):
        prs = [p for p in prs if p.repo_key in args.repo.split(",")]
    if getattr(args, "pr", None):
        prs = [p for p in prs if p.pr_id in args.pr.split(",")]

    # Check codex availability
    has_codex = subprocess.run(["which", "codex"], capture_output=True).returncode == 0
    if not has_codex:
        print(
            "Warning: codex CLI not found. Running Claude-only classification (no council).\n"
        )

    workers = getattr(args, "workers", 5) or 5

    # Build work items
    work: list[tuple[BenchmarkPR, list[dict], Path]] = []
    for pr in prs:
        f = reviews_dir / f"{pr.pr_id}.json"
        if not f.exists():
            continue
        with open(f) as fh:
            findings = json.load(fh)
        if not findings:
            continue
        repo_dir = repos_dir / pr.repo_key
        if not (repo_dir / ".git").exists():
            continue
        work.append((pr, findings, repo_dir))

    if not work:
        print("No findings to classify. Run 'review' first.")
        return False

    total_findings = sum(len(f) for _, f, _ in work)
    print(
        f"Classifying {total_findings} findings across {len(work)} PRs"
        f" (council: claude{' + codex' if has_codex else ' only'})...\n"
    )

    all_results: list[dict] = []
    completed = 0

    def _classify_worker(item: tuple[BenchmarkPR, list[dict], Path]) -> dict:
        pr, findings, repo_dir = item
        return classify_pr(pr, findings, repo_dir, workers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_classify_worker, item): item[0] for item in work}
        for future in concurrent.futures.as_completed(futures):
            pr = futures[future]
            completed += 1
            try:
                result = future.result()
                all_results.append(result)
                cats = result["category_counts"]
                adj_p = result["adjusted_precision"]
                inc_p = result["inclusive_precision"]
                disp = result["disputed_count"]
                with _print_lock:
                    cat_str = " ".join(f"{k}={v}" for k, v in sorted(cats.items()))
                    print(
                        f"  [{completed}/{len(work)}] {pr.pr_id}: adj_P={adj_p:.0%} inc_P={inc_p:.0%} disputed={disp}  {cat_str}"
                    )
            except Exception as e:
                with _print_lock:
                    print(f"  [{completed}/{len(work)}] {pr.pr_id}: Error: {e}")

    if not all_results:
        print("No classifications produced.")
        return False

    # Aggregate
    total = sum(r["findings_count"] for r in all_results)
    total_by_cat: dict[str, int] = {}
    for r in all_results:
        for cat, count in r["category_counts"].items():
            total_by_cat[cat] = total_by_cat.get(cat, 0) + count

    total_real = sum(total_by_cat.get(c, 0) for c in CATEGORIES_REAL)
    total_correct = sum(total_by_cat.get(c, 0) for c in CATEGORIES_CORRECT)
    total_disputed = sum(r["disputed_count"] for r in all_results)

    agg_adj_p = total_real / total if total else 0
    agg_inc_p = total_correct / total if total else 0

    # Save
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = {
        "timestamp": datetime.now().isoformat(),
        "council": f"claude{' + codex' if has_codex else ' only'}",
        "prs_classified": len(all_results),
        "total_findings": total,
        "aggregate": {
            "adjusted_precision": agg_adj_p,
            "inclusive_precision": agg_inc_p,
            "disputed": total_disputed,
            "category_counts": total_by_cat,
        },
        "per_pr": all_results,
    }

    classify_file = results_dir / f"classify-{ts}.json"
    with open(classify_file, "w") as f:
        json.dump(output, f, indent=2)
    latest = results_dir / "classify-latest.json"
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)

    # Print summary
    w = 72
    print(f"\n{'=' * w}")
    print("  Finding Classification Report")
    print(f"  {output['timestamp'][:19]}  |  Council: {output['council']}")
    print(f"  {len(all_results)} PRs, {total} findings classified")
    print(f"{'=' * w}")

    print("\n  CATEGORY DISTRIBUTION")
    print(f"  {'─' * (w - 4)}")
    for cat in [
        "confirmed_bug",
        "confirmed_vuln",
        "valid_concern",
        "nitpick",
        "speculative",
        "wrong",
    ]:
        n = total_by_cat.get(cat, 0)
        pct = n / total * 100 if total else 0
        bar = "#" * int(pct / 2)
        real_marker = (
            " *" if cat in CATEGORIES_REAL else "  " if cat == "nitpick" else ""
        )
        print(f"    {cat:<18s} {n:>4d} ({pct:4.1f}%) {bar}{real_marker}")

    print("\n  PRECISION METRICS")
    print(f"  {'─' * (w - 4)}")
    print(f"    Adjusted P:   {agg_adj_p:5.1%}  (bug + vuln + concern = real issues)")
    print(f"    Inclusive P:   {agg_inc_p:5.1%}  (+ nitpick = technically correct)")
    print(
        f"    Disputed:      {total_disputed:>4d}  (models disagreed, needs human review)"
    )

    print(f"\n  Full results: {classify_file}")
    print(f"{'=' * w}")

    # Auto-ingest classifications into DB (attach to latest run)
    try:
        store = _get_store()
        latest_run = store._latest_run_id("martian-offline")
        if latest_run:
            for pr_cls in all_results:
                bp_id = f"martian-offline:{pr_cls['pr_id']}"
                findings = store.conn.execute(
                    "SELECT id, finding_index FROM findings WHERE run_id=? AND benchmark_pr_id=? ORDER BY finding_index",
                    (latest_run, bp_id),
                ).fetchall()
                fid_map = {row["finding_index"]: row["id"] for row in findings}
                for cls in pr_cls.get("classifications", []):
                    fid = fid_map.get(cls.get("finding_index"))
                    if fid:
                        store.save_classification(fid, latest_run, cls)
            store.update_run_metrics(
                latest_run,
                {
                    "adjusted_precision": agg_adj_p,
                    "inclusive_precision": agg_inc_p,
                },
            )
            store.close()
            print(f"Ingested classifications into DB (run {latest_run})")
        else:
            store.close()
    except Exception as e:
        print(f"DB ingest warning: {e}")

    return True


# ─── Analytics ────────────────────────────────────────────────────────────────


def _get_store():
    from eval_store import EvalStore

    return EvalStore(EVAL_DIR / "eval.db")


def cmd_analytics(args: argparse.Namespace) -> bool:
    """Query the evaluation database for insights, scoped to a benchmark."""
    store = _get_store()
    query_name = getattr(args, "analytics_query", "progress") or "progress"
    benchmark_id = getattr(args, "benchmark", "martian-offline") or "martian-offline"

    # Queries that take benchmark_id
    bench_queries = {
        "progress": ("F1/Precision/Recall over recent runs", store.query_progress),
        "speed": ("Review speed trend over time", store.query_speed_trend),
    }
    # Queries that take run_id (latest within benchmark)
    run_queries = {
        "language": ("Findings breakdown by language", store.query_by_language),
        "category": ("Classification category distribution", store.query_by_category),
        "severity": ("Findings by severity x classification", store.query_by_severity),
        "pass": ("Which explorer passes find real bugs?", store.query_by_pass),
        "missed": (
            "Golden comments we miss (false negatives)",
            store.query_missed_golden,
        ),
        "disputed": (
            "Findings where Claude and Codex disagreed",
            store.query_disputed_findings,
        ),
        "wrong": (
            "Findings classified as wrong (FP patterns)",
            store.query_wrong_findings,
        ),
        "timing": (
            "Per-PR timing breakdown (slowest first)",
            store.query_timing_detail,
        ),
        "timing-lang": ("Average timing by language", store.query_timing_by_language),
        "cost": (
            "Cost per real finding by language",
            store.query_cost_per_real_finding,
        ),
        "density": ("Finding density vs diff size", store.query_finding_density),
        "calibration": (
            "Severity calibration (severity x true category)",
            store.query_severity_calibration,
        ),
        "golden-type": (
            "Golden comment catch rate by severity and language",
            store.query_golden_by_type,
        ),
        "turns": (
            "Per-PR turn stats (tokens, thinking, models)",
            store.query_turn_summary,
        ),
        "models": ("Model usage breakdown across turns", store.query_model_usage),
        "tools": ("Tool call frequency across turns", store.query_tool_frequency),
    }
    # Queries that take benchmark_id and compare across runs
    cross_run_queries = {
        "stability": (
            "Cross-run finding overlap (same PR, different runs)",
            store.query_stability,
        ),
    }

    all_queries = {**bench_queries, **run_queries, **cross_run_queries}

    # List available benchmarks if requested
    if query_name == "benchmarks":
        print("\n  Registered benchmarks\n  " + "─" * 68)
        _print_query_results(
            store.query("SELECT id, name, url FROM benchmarks ORDER BY id")
        )
        store.close()
        return True

    if query_name == "all":
        latest_run = store._latest_run_id(benchmark_id)
        print(
            f"\n  Benchmark: {benchmark_id}"
            + (f"  |  Latest run: {latest_run}" if latest_run else "")
        )
        for name, (desc, fn) in bench_queries.items():
            print(f"\n{'=' * 72}\n  {desc}\n{'=' * 72}")
            _print_query_results(fn(benchmark_id))
        for name, (desc, fn) in run_queries.items():
            print(f"\n{'=' * 72}\n  {desc}\n{'=' * 72}")
            _print_query_results(fn(latest_run) if latest_run else [])
        for name, (desc, fn) in cross_run_queries.items():
            print(f"\n{'=' * 72}\n  {desc}\n{'=' * 72}")
            _print_query_results(fn(benchmark_id))
        store.close()
        return True

    if query_name not in all_queries:
        names = ", ".join(all_queries.keys())
        print(f"Available queries: {names}, all, benchmarks")
        print("  Example: analytics progress --benchmark martian-offline")
        store.close()
        return False

    if query_name in bench_queries:
        desc, fn = bench_queries[query_name]
        print(f"\n  {desc}  [{benchmark_id}]\n  {'─' * 68}")
        _print_query_results(fn(benchmark_id))
    elif query_name in cross_run_queries:
        desc, fn = cross_run_queries[query_name]
        print(f"\n  {desc}  [{benchmark_id}]\n  {'─' * 68}")
        _print_query_results(fn(benchmark_id))
    else:
        latest_run = store._latest_run_id(benchmark_id)
        if not latest_run:
            print(f"No runs found for benchmark '{benchmark_id}'.")
            store.close()
            return False
        desc, fn = run_queries[query_name]
        print(f"\n  {desc}  [{benchmark_id} / {latest_run}]\n  {'─' * 68}")
        _print_query_results(fn(latest_run))

    store.close()
    return True


def _parse_session_turns(session_path: Path) -> list[dict]:
    """Parse a Claude session JSONL file into per-turn data."""
    turns = []
    if not session_path.exists():
        return turns
    with open(session_path) as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "assistant":
                continue
            inner = msg.get("message", {})
            usage = inner.get("usage", {})
            tools = []
            has_thinking = False
            thinking_chars = 0
            for block in inner.get("content", []):
                if isinstance(block, dict):
                    if block.get("type") == "thinking":
                        has_thinking = True
                        thinking_chars += len(block.get("thinking", ""))
                    elif block.get("type") == "tool_use":
                        tools.append(block.get("name", "?"))
            turns.append(
                {
                    "turn": len(turns) + 1,
                    "model": inner.get("model", ""),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_write": usage.get("cache_creation_input_tokens", 0),
                    "has_thinking": has_thinking,
                    "thinking_chars": thinking_chars,
                    "tools_used": ",".join(tools),
                    "is_subagent": msg.get("isSidechain", False),
                }
            )
    return turns


def _get_diff_stats(repo_dir: Path, base_ref: str, head_ref: str) -> dict:
    """Get diff stats (files, additions, deletions) for a range."""
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--numstat", f"{base_ref}..{head_ref}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    files = 0
    additions = 0
    deletions = 0
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            try:
                additions += int(parts[0])
            except ValueError:
                pass
            try:
                deletions += int(parts[1])
            except ValueError:
                pass
    return {"files": files, "additions": additions, "deletions": deletions}


def _find_session_file(session_id: str) -> Optional[Path]:
    """Find the session JSONL file for a given session ID."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.is_dir():
        return None
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        session_file = project_dir / f"{session_id}.jsonl"
        if session_file.exists():
            return session_file
    return None


def _aggregate_prompt_test_verdicts(
    prs_by_id: dict[str, BenchmarkPR],
    all_findings: dict[str, list[dict]],
    all_verdicts: list[tuple[str, int, int, bool, float]],
) -> list[dict]:
    """Aggregate prompt-test verdicts using one-to-one greedy matching per PR."""
    matches_by_pr: dict[str, list[tuple[int, int, float]]] = {}
    for pr_id, golden_idx, candidate_idx, is_match, confidence in all_verdicts:
        if is_match and confidence >= 0.5:
            matches_by_pr.setdefault(pr_id, []).append(
                (golden_idx, candidate_idx, confidence)
            )

    pr_results: list[dict] = []
    for pr_id, findings in all_findings.items():
        pr = prs_by_id.get(pr_id)
        if not pr:
            continue
        matches = sorted(
            matches_by_pr.get(pr_id, []), key=lambda item: item[2], reverse=True
        )
        used_golden: set[int] = set()
        used_candidates: set[int] = set()
        tp = 0
        for golden_idx, candidate_idx, _confidence in matches:
            if golden_idx in used_golden or candidate_idx in used_candidates:
                continue
            used_golden.add(golden_idx)
            used_candidates.add(candidate_idx)
            tp += 1
        fn = len(pr.golden_comments) - tp
        fp = len(findings) - len(used_candidates)
        pr_p = tp / (tp + fp) if (tp + fp) > 0 else 0
        pr_r = tp / (tp + fn) if (tp + fn) > 0 else 0
        pr_f1 = 2 * pr_p * pr_r / (pr_p + pr_r) if (pr_p + pr_r) > 0 else 0
        pr_results.append(
            {
                "pr_id": pr_id,
                "language": pr.language,
                "findings": len(findings),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": pr_p,
                "recall": pr_r,
                "f1": pr_f1,
            }
        )
    return pr_results


def cmd_ingest(args: argparse.Namespace) -> bool:
    """Import existing JSON results, session data, and diff stats into the SQLite database."""
    store = _get_store()
    results_dir = EVAL_DIR / "results"
    reviews_dir = EVAL_DIR / "reviews"
    repos_dir = EVAL_DIR / "repos"
    benchmark_id = "martian-offline"

    store.ensure_benchmark(
        benchmark_id,
        "Martian CodeReBench Offline",
        "https://codereview.withmartian.com/",
    )

    # Import golden comments and collect diff stats
    prs = load_prs()
    print("Importing PR metadata and diff stats...")
    for pr_data in prs:
        bp_id = store.ensure_benchmark_pr(
            benchmark_id,
            {
                "pr_id": pr_data.pr_id,
                "repo_key": pr_data.repo_key,
                "language": pr_data.language,
                "pr_title": pr_data.pr_title,
                "pr_number": pr_data.pr_number,
                "commit_sha": pr_data.commit_sha,
            },
        )
        store.ensure_golden_comments(
            bp_id,
            [
                {"comment": g.comment, "severity": g.severity}
                for g in pr_data.golden_comments
            ],
        )

        # Collect diff stats from git
        repo_dir = repos_dir / pr_data.repo_key
        if repo_dir.exists():
            if pr_data.commit_sha:
                stats = _get_diff_stats(
                    repo_dir, f"{pr_data.commit_sha}~1", pr_data.commit_sha
                )
            elif pr_data.pr_number:
                default_branch = REPOS[pr_data.repo_key]["default_branch"]
                base = get_merge_base(
                    repo_dir, default_branch, f"pr-{pr_data.pr_number}"
                )
                if base:
                    stats = _get_diff_stats(repo_dir, base, f"pr-{pr_data.pr_number}")
                else:
                    stats = {}
            else:
                stats = {}
            if stats.get("files"):
                store.update_pr_diff_stats(
                    bp_id, stats["files"], stats["additions"], stats["deletions"]
                )

    # Import each eval results file
    imported = 0
    for results_file in sorted(results_dir.glob("eval-*.json")):
        if "latest" in results_file.name:
            continue
        with open(results_file) as f:
            data = json.load(f)

        classify_ts = results_file.stem.replace("eval-", "")
        classify_data = None
        for cf in results_dir.glob(f"classify-{classify_ts}*.json"):
            if "latest" not in cf.name:
                with open(cf) as fh:
                    classify_data = json.load(fh)
                break

        run_id = store.import_from_json(benchmark_id, data, reviews_dir, classify_data)
        imported += 1
        print(f"  Imported {results_file.name} -> run {run_id}")

        # Import session turn data for each PR in this run
        sessions_imported = 0
        for pr_data in data.get("per_pr", []):
            bp_id = f"{benchmark_id}:{pr_data['pr_id']}"
            raw_file = reviews_dir / f"{pr_data['pr_id']}.raw.json"
            if not raw_file.exists():
                continue
            with open(raw_file) as fh:
                raw = json.load(fh)
            session_id = raw.get("claude_meta", {}).get("session_id")
            if not session_id:
                continue
            session_file = _find_session_file(session_id)
            if not session_file:
                continue
            turns = _parse_session_turns(session_file)
            if turns:
                store.save_session_turns(run_id, bp_id, session_id, turns)
                sessions_imported += 1
        if sessions_imported:
            print(f"    + {sessions_imported} session turn logs")

    print(f"\nImported {imported} runs into {store.db_path}")
    store.close()
    return True


def _print_query_results(rows: list[dict]):
    """Pretty-print query results as a table."""
    if not rows:
        print("    (no data)")
        return
    keys = list(rows[0].keys())
    widths = {
        k: min(60, max(len(str(k)), max(len(str(r.get(k, "") or "")) for r in rows)))
        for k in keys
    }
    header = "  ".join(f"{k:<{widths[k]}}" for k in keys)
    print(f"    {header}")
    print(f"    {'  '.join('─' * widths[k] for k in keys)}")
    for r in rows:
        vals = []
        for k in keys:
            v = r.get(k, "")
            s = str(v) if v is not None else ""
            if len(s) > widths[k]:
                s = s[: widths[k] - 2] + ".."
            vals.append(f"{s:<{widths[k]}}")
        print(f"    {'  '.join(vals)}")


PROMPT_TEST_TEMPLATE = """\
You are an expert code reviewer. Analyze this diff for bugs, logic errors, \
regressions, and correctness issues.

{prompt_content}

Diff to review:
{diff}

For each bug or issue found, report it. Focus on:
- Logic errors, wrong conditions, off-by-one, null/undefined access
- Missing error handling that will crash in production
- Backward-incompatible changes (callers not updated)
- Race conditions, state corruption, data loss
- Security issues (injection, auth bypass, secrets exposure)

Return ONLY a JSON array of findings:
[{{"summary": "one-line description", "severity": "critical|high|medium|low", \
"file": "path/to/file", "line": 0, "evidence": "brief explanation"}}]

If no real issues, return: []
Do NOT report style issues, naming, or minor improvements."""


def prompt_test_single(
    pr: "BenchmarkPR",
    repo_dir: Path,
    prompt_content: str,
    model: str,
    diff_cap: int = 50000,
) -> list[dict]:
    """One-shot prompt test: feed diff + prompt to claude -p --max-turns 1."""
    default_branch = REPOS[pr.repo_key]["default_branch"]

    if pr.commit_sha:
        head_ref = pr.commit_sha
        base_ref = f"{pr.commit_sha}~1"
    else:
        head_ref = f"pr-{pr.pr_number}"
        r = git(repo_dir, "merge-base", f"origin/{default_branch}", head_ref)
        if r.returncode != 0:
            return []
        base_ref = r.stdout.strip()[:12]

    r = git(repo_dir, "diff", f"{base_ref}..{head_ref}")
    diff_text = r.stdout
    if not diff_text.strip():
        return []

    # Cap diff to avoid prompt overflow
    if len(diff_text) > diff_cap:
        diff_text = (
            diff_text[:diff_cap] + f"\n... [truncated, {len(r.stdout)} chars total]"
        )

    prompt = PROMPT_TEST_TEMPLATE.format(
        prompt_content=prompt_content,
        diff=diff_text,
    )

    try:
        r = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--max-turns",
                "1",
                "--tools",
                "",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        text = r.stdout.strip()
        try:
            envelope = json.loads(text)
            text = envelope.get("result", text)
        except json.JSONDecodeError:
            pass

        text = re.sub(r"```json\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text.strip())

        # Try direct parse first (most reliable)
        try:
            parsed = json.loads(text.strip())
            if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
                return parsed
        except json.JSONDecodeError:
            pass

        # Fallback: find outermost [ ... ] by bracket balancing
        start = text.find("[")
        if start >= 0:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start : i + 1])
                            if isinstance(parsed, list):
                                return [x for x in parsed if isinstance(x, dict)]
                        except json.JSONDecodeError:
                            pass
                        break

        return _extract_json_array(text)  # final fallback
    except subprocess.TimeoutExpired:
        with _print_lock:
            print(f"      TIMEOUT ({pr.pr_id})")
        return []
    except FileNotFoundError:
        return []
    except Exception as exc:
        with _print_lock:
            print(f"      ERROR ({pr.pr_id}): {exc}")
        return []


def cmd_prompt_test(args: argparse.Namespace) -> bool:
    """One-shot prompt evaluation: test a single prompt against golden bugs.

    Like 'review' but uses claude -p --max-turns 1 with no tools.
    Tests prompt quality in isolation, not the full pipeline.
    """
    prompt_file = getattr(args, "prompt_file", None)
    if not prompt_file:
        # Default to correctness prompt
        prompt_file = "skills/codereview/prompts/reviewer-correctness-pass.md"
    prompt_path = Path(prompt_file)
    if not prompt_path.exists():
        print(f"Prompt file not found: {prompt_file}")
        return False

    prompt_content_raw = prompt_path.read_text()
    prompt_name = prompt_path.stem.replace("reviewer-", "").replace("-pass", "")

    # For one-shot mode, strip tool-use instructions (Grep/Read/Glob) that don't work
    # with --max-turns 1. Keep the phase descriptions and calibration examples.
    # If prompt is over 3000 chars, condense it.
    if len(prompt_content_raw) > 3000:
        # Extract key sections: keep calibration examples, FP suppression, and phase names
        lines = prompt_content_raw.splitlines()
        condensed = []
        for line in lines:
            # Skip lines telling the AI to use tools
            if any(
                kw in line.lower()
                for kw in ["**grep**", "**read**", "**glob**", "use grep", "use read"]
            ):
                continue
            condensed.append(line)
        prompt_content = "\n".join(condensed)
        # Further cap if still too long
        if len(prompt_content) > 5000:
            prompt_content = (
                prompt_content[:5000] + "\n... [prompt condensed for one-shot mode]"
            )
    else:
        prompt_content = prompt_content_raw
    print(f"Prompt: {prompt_name} ({len(prompt_content)} chars)")

    # For prompt-test, default to sonnet (--model defaults to haiku for the full pipeline orchestrator)
    model = getattr(args, "model", "sonnet")
    if model == "haiku":
        model = "sonnet"  # haiku is too weak for one-shot correctness analysis
    limit = getattr(args, "limit", None)
    workers = getattr(args, "workers", 5) or 5
    resume = getattr(args, "resume", False)
    repo_filter = (
        set(getattr(args, "repo", "").split(","))
        if getattr(args, "repo", None)
        else None
    )

    # Load benchmark PRs
    prs = load_prs()
    if not prs:
        print("No benchmark PRs loaded. Run 'setup' and 'prepare' first.")
        return False

    if repo_filter:
        prs = [p for p in prs if p.repo_key in repo_filter]
    if limit:
        prs = prs[:limit]

    results_dir = EVAL_DIR / "prompt-tests"
    results_dir.mkdir(parents=True, exist_ok=True)

    judge_model = getattr(args, "judge_model", None) or "sonnet"

    print(
        f"Testing {len(prs)} PRs with prompt '{prompt_name}', model={model}, judge={judge_model}, workers={workers}\n"
    )

    # Phase 1: Generate findings
    all_findings: dict[str, list[dict]] = {}
    completed = 0

    def _test_pr(pr: "BenchmarkPR") -> tuple[str, list[dict]]:
        repo_dir = EVAL_DIR / "repos" / pr.repo_key
        findings = prompt_test_single(pr, repo_dir, prompt_content, model)
        return pr.pr_id, findings

    # Check for resumed results
    result_file = results_dir / f"{prompt_name}-findings.json"
    if resume and result_file.exists():
        with open(result_file) as f:
            all_findings = json.load(f)
        print(f"  Resumed {len(all_findings)} PRs from {result_file.name}")
        prs = [p for p in prs if p.pr_id not in all_findings]
        if not prs:
            print("  All PRs already tested. Proceeding to judge.")
        completed = len(all_findings)

    if prs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_test_pr, pr): pr for pr in prs}
            for future in concurrent.futures.as_completed(futures):
                pr = futures[future]
                completed += 1
                try:
                    pr_id, findings = future.result()
                    all_findings[pr_id] = findings
                    with _print_lock:
                        print(
                            f"  [{completed}/{completed + len(prs) - completed}] {pr_id}: {len(findings)} findings"
                        )
                except Exception as e:
                    with _print_lock:
                        print(f"  [{completed}] {pr.pr_id}: ERROR {e}")
                    all_findings[pr.pr_id] = []

        # Save findings
        with open(result_file, "w") as f:
            json.dump(all_findings, f, indent=2)
        print(f"\n  Findings saved to {result_file}")

    # Phase 2: Judge against golden comments
    print(f"\nJudging {len(all_findings)} PRs against golden comments...")
    prs_by_id = {p.pr_id: p for p in load_prs()}

    total_tp = 0
    total_fp = 0
    total_fn = 0
    total_golden = 0
    pr_results: list[dict] = []

    judge_pairs: list[
        tuple[str, int, int, str, str, str]
    ] = []  # (pr_id, golden_idx, candidate_idx, golden, candidate, golden_severity)

    for pr_id, findings in all_findings.items():
        pr = prs_by_id.get(pr_id)
        if not pr:
            continue
        for golden_idx, gc in enumerate(pr.golden_comments):
            total_golden += 1
            for candidate_idx, f in enumerate(findings):
                summary = f.get("summary", "") + " " + f.get("evidence", "")
                judge_pairs.append(
                    (pr_id, golden_idx, candidate_idx, gc.comment, summary, gc.severity)
                )

    if not judge_pairs:
        print("  No pairs to judge.")
        # All findings are FP, all golden are FN
        for pr_id, findings in all_findings.items():
            pr = prs_by_id.get(pr_id)
            if pr:
                total_fn += len(pr.golden_comments)
                total_fp += len(findings)
    else:
        # Batch judge
        batches = [
            judge_pairs[i : i + JUDGE_BATCH_SIZE]
            for i in range(0, len(judge_pairs), JUDGE_BATCH_SIZE)
        ]
        all_verdicts: list[tuple[str, int, int, bool, float]] = []

        def _judge_batch(batch):
            pairs_for_judge = [
                (golden_idx, candidate_idx, golden, candidate)
                for pr_id, golden_idx, candidate_idx, golden, candidate, sev in batch
            ]
            return judge_batch(pairs_for_judge, judge_model)

        batch_num = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_judge_batch, batch): batch for batch in batches}
            for future in concurrent.futures.as_completed(futures):
                batch = futures[future]
                batch_num += 1
                try:
                    results = future.result()
                    for (
                        pr_id,
                        golden_idx,
                        candidate_idx,
                        golden,
                        candidate,
                        sev,
                    ), verdict in zip(batch, results, strict=True):
                        all_verdicts.append(
                            (
                                pr_id,
                                golden_idx,
                                candidate_idx,
                                verdict["match"],
                                verdict["confidence"],
                            )
                        )
                except Exception as e:
                    raise RuntimeError(f"Judge batch failed: {e}") from e

        for result in _aggregate_prompt_test_verdicts(
            prs_by_id, all_findings, all_verdicts
        ):
            total_tp += result["tp"]
            total_fn += result["fn"]
            total_fp += result["fp"]
            pr_results.append(
                {
                    **result,
                    "precision": round(result["precision"], 3),
                    "recall": round(result["recall"], 3),
                    "f1": round(result["f1"], 3),
                }
            )

    # Report
    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    )

    print(f"\n{'=' * 70}")
    print(f"  Prompt Test: {prompt_name}")
    print(f"  Model: {model} | Judge: {judge_model} | PRs: {len(all_findings)}")
    print(f"{'=' * 70}")
    print(f"  Precision: {precision:.1%}  ({total_tp} TP, {total_fp} FP)")
    print(f"  Recall:    {recall:.1%}  ({total_tp} TP, {total_fn} FN)")
    print(f"  F1:        {f1:.1%}")
    print(f"  Golden:    {total_golden}")
    print()

    # Compare with full pipeline and leaderboard
    print(f"  {'Tool':<30s} {'P':>6s} {'R':>6s} {'F1':>6s}")
    print(f"  {'-' * 48}")
    print(
        f"  ** {prompt_name} (one-shot) **{precision * 100:>8.1f}{recall * 100:>6.1f}{f1 * 100:>6.1f}"
    )
    print(f"  Our full pipeline{10.1:>14.1f}{39.1:>6.1f}{16.0:>6.1f}")
    for name, p, r, f in LEADERBOARD[:10]:
        print(f"  {name:<30s}{p:>6.1f}{r:>6.1f}{f:>6.1f}")

    # Per-language breakdown
    if pr_results:
        print("\n  Per-language:")
        from collections import defaultdict

        by_lang = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
        for r in pr_results:
            by_lang[r["language"]]["tp"] += r["tp"]
            by_lang[r["language"]]["fp"] += r["fp"]
            by_lang[r["language"]]["fn"] += r["fn"]
        for lang, counts in sorted(by_lang.items()):
            tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
            p = tp / (tp + fp) if (tp + fp) > 0 else 0
            r = tp / (tp + fn) if (tp + fn) > 0 else 0
            f = 2 * p * r / (p + r) if (p + r) > 0 else 0
            print(
                f"    {lang:<12s} P={p:.0%} R={r:.0%} F1={f:.0%} (TP={tp} FP={fp} FN={fn})"
            )

    # Save full results
    report_file = results_dir / f"{prompt_name}-report.json"
    with open(report_file, "w") as f:
        json.dump(
            {
                "prompt": prompt_name,
                "prompt_file": str(prompt_path),
                "model": model,
                "judge_model": judge_model,
                "timestamp": datetime.now().isoformat(),
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
                "total_tp": total_tp,
                "total_fp": total_fp,
                "total_fn": total_fn,
                "total_golden": total_golden,
                "pr_results": pr_results,
            },
            f,
            indent=2,
        )
    print(f"\n  Full results: {report_file}")

    return True


def cmd_run(args: argparse.Namespace) -> bool:
    """Full pipeline: setup -> prepare -> review -> judge -> report."""
    steps = [
        ("SETUP", cmd_setup),
        ("PREPARE", cmd_prepare),
        ("REVIEW", cmd_review),
        ("JUDGE", cmd_judge),
        ("REPORT", cmd_report),
    ]

    args.resume = True  # always resume in full-pipeline mode

    for name, fn in steps:
        print(f"\n{'=' * 20} {name} {'=' * 20}")
        if not fn(args):
            print(f"\n{name} failed. Stopping.")
            return False

    return True


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Martian CodeReBench evaluation runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
commands:
  setup     Clone the Martian benchmark repository
  prepare   Clone source repos (blobless) and fetch all PR refs
  review    Run code review on each PR via claude -p (expensive)
  judge     Score findings against golden comments (batched, parallel)
  classify  Two-model council (Claude + Codex) classifies finding quality
  report    Display comparison report with public leaderboard
  ingest    Import JSON results into SQLite database
  analytics Query the database (progress, language, category, severity, pass, missed, speed, disputed, wrong, all)
  run       Full pipeline (setup → prepare → review → judge → report)

examples:
  %(prog)s run --limit 5           Quick end-to-end test (5 PRs)
  %(prog)s review --resume         Continue interrupted review run
  %(prog)s judge                   Re-score existing findings (cheap)
  %(prog)s report                  Show latest results vs leaderboard
  %(prog)s review --repo sentry    Review only Python/Sentry PRs
""",
    )

    parser.add_argument(
        "command",
        choices=[
            "setup",
            "prepare",
            "review",
            "judge",
            "classify",
            "report",
            "ingest",
            "analytics",
            "prompt-test",
            "run",
        ],
    )
    parser.add_argument("--force", action="store_true", help="Force re-clone / re-run")
    parser.add_argument(
        "--repo",
        type=str,
        help="Filter by repo key (comma-separated, e.g. sentry,grafana)",
    )
    parser.add_argument(
        "--pr", type=str, help="Filter by PR ID (comma-separated, e.g. sentry-92393)"
    )
    parser.add_argument("--limit", type=int, help="Max number of PRs to review")
    parser.add_argument(
        "--resume", action="store_true", help="Skip PRs that already have findings"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Parallel workers for review and judge (default: 5)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="sonnet",
        help="Model for orchestration (default: sonnet; explorers use sonnet, judge uses opus)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Model for judge (default: sonnet)",
    )
    parser.add_argument(
        "analytics_query",
        nargs="?",
        default="progress",
        help="Query for analytics: progress, language, category, severity, pass, missed, speed, disputed, wrong, benchmarks, all",
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        default="martian-offline",
        help="Benchmark to query (default: martian-offline)",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help="Prompt file for prompt-test command (default: correctness prompt)",
    )
    parser.add_argument(
        "--passes",
        type=str,
        default=None,
        help="Comma-separated expert passes for review (e.g. 'correctness' or 'correctness,reliability')",
    )

    args = parser.parse_args()
    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    commands = {
        "setup": cmd_setup,
        "prepare": cmd_prepare,
        "review": cmd_review,
        "judge": cmd_judge,
        "classify": cmd_classify,
        "report": cmd_report,
        "ingest": cmd_ingest,
        "analytics": cmd_analytics,
        "prompt-test": cmd_prompt_test,
        "run": cmd_run,
    }

    try:
        ok = commands[args.command](args)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Partial results are saved — use --resume to continue.")
        sys.exit(130)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
