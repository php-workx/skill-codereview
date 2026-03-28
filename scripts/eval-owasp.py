#!/usr/bin/env python3
"""
OWASP Benchmark evaluation runner.

Tests our security detection against the OWASP Benchmark — the industry standard
for comparing SAST tools. Each test case is a standalone file with a known
vulnerability (or intentionally safe code that tests false positive resistance).

Scoring uses the Youden Index (TPR - FPR) per CWE category, same as every
published SAST tool scorecard. Scores range from -1 (perfectly wrong) to +1 (perfect).

Requirements:
    claude          # Claude Code CLI (for AI security review)
    semgrep         # (optional) for deterministic baseline

Usage:
    python3 scripts/eval-owasp.py setup                          # Clone benchmark repos
    python3 scripts/eval-owasp.py scan [--lang python|java]      # Semgrep baseline (fast)
    python3 scripts/eval-owasp.py review [--lang python|java]    # AI security review
    python3 scripts/eval-owasp.py score                          # Compute Youden Index
    python3 scripts/eval-owasp.py report                         # Scorecard vs known tools
    python3 scripts/eval-owasp.py run [--lang python]            # Full pipeline

Modes:
    scan    — deterministic tools only (semgrep). Fast, free, comparable to SAST tools.
    review  — AI security explorer. Expensive but catches what semgrep misses.
    score   — scores whichever results exist (scan, review, or both merged).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_print_lock = threading.Lock()

# ─── Constants ────────────────────────────────────────────────────────────────

EVAL_DIR = Path(".eval")
OWASP_DIR = EVAL_DIR / "owasp"

REPOS = {
    "python": {
        "url": "https://github.com/OWASP-Benchmark/BenchmarkPython.git",
        "expected_csv": "expectedresults-0.1.csv",
        "testcode_dir": "testcode",
        "benchmark_id": "owasp-python",
        "version": "0.1",
    },
    "java": {
        "url": "https://github.com/OWASP-Benchmark/BenchmarkJava.git",
        "expected_csv": "expectedresults-1.2.csv",
        "testcode_dir": "src/main/java/org/owasp/benchmark/testcode",
        "benchmark_id": "owasp-java",
        "version": "1.2",
    },
}

# Known tool scores for comparison (Youden Index, from published OWASP scorecards)
# Known tool scores for comparison.
# Java: from published OWASP scorecards (BenchmarkJava v1.2).
# Python: self-measured from SARIF results shipped in BenchmarkPython repo.
# No official Python scorecards exist yet (v0.1 preliminary).
KNOWN_SCORES = {
    "java": [
        ("Commercial Average", 0.27),
        ("Semgrep OSS", 0.18),
        ("SpotBugs", 0.14),
        ("SonarQube", 0.13),
        ("FindBugs", 0.10),
    ],
    "python": [
        ("Bandit (self-measured)", -0.027),
        ("Bearer (self-measured)", +0.002),
    ],
}

# CWE aliases: semgrep may report a sibling CWE that OWASP counts as the same vuln
CWE_ALIASES = {
    327: 328,  # Broken Crypto → Weak Hash (OWASP uses 328 for hash category)
    95: 94,  # Eval Injection → Code Injection
    564: 89,  # Hibernate Injection → SQL Injection
}

# Semgrep rulesets for maximum OWASP coverage
SEMGREP_CONFIGS = ["p/security-audit", "p/python", "p/owasp-top-ten"]


def build_ai_review_prompt(cwe_list: str, files_text: str, lang: str) -> str:
    """Build a language-aware OWASP review prompt."""
    lang = (lang or "python").lower()
    if lang == "java":
        sources = (
            "HttpServletRequest.getParameter(), getHeader(), getCookies(), "
            "request bodies, servlet parameters, file reads, database reads, "
            "System.getenv(), System.getProperty()."
        )
        sinks = (
            "SQL: Statement.executeQuery()/executeUpdate() with string concatenation; "
            "Command: Runtime.exec(), ProcessBuilder with untrusted args; "
            "XPath: XPathExpression with concatenated input; "
            "LDAP: DirContext.search() with string-built filters; "
            "Template/XSS: response.getWriter().write(), JSP/Thymeleaf output without escaping; "
            "File: File/FileInputStream/Paths.get() with user-controlled paths; "
            'Crypto: MessageDigest.getInstance("MD5"/"SHA-1"), weak javax.crypto usage; '
            "Deserialization: ObjectInputStream.readObject()."
        )
        sanitization = (
            "PreparedStatement with bind variables IS sanitization for SQL. "
            "StringEscapeUtils / framework escaping can sanitize HTML sinks. "
            "Variable reassignment or bean copying is NOT sanitization."
        )
        examples = (
            "- Statement + string concat = VULNERABLE (CWE-89)\n"
            "- PreparedStatement with ? parameters = SECURE\n"
            "- Runtime.exec(userInput) = VULNERABLE (CWE-78)\n"
            "- MessageDigest MD5/SHA-1 for passwords/tokens = VULNERABLE (CWE-328)\n"
            "- SecureRandom = SECURE, java.util.Random = WEAK (CWE-330)"
        )
    else:
        sources = (
            "function parameters, configparser reads, os.environ/os.getenv, sys.argv, "
            "database reads, file reads, request parameters, cookie values, HTTP headers."
        )
        sinks = (
            "SQL: cursor.execute(), connection.execute() with string formatting; "
            "Command: os.system(), subprocess with shell=True or string args, os.popen(); "
            "XPath: xpath() / find() with string concatenation; "
            "LDAP: search_s() / search() with string-formatted filters; "
            "Template: render_template_string(), Markup(), innerHTML; "
            "File: open() with dynamic path, os.path.join with unsanitized input; "
            "Eval: eval(), exec() with dynamic input; "
            "Deserialization: pickle.loads(), yaml.load() without SafeLoader."
        )
        sanitization = (
            "Indirection (configparser round-trips, dict storage, variable reassignment) "
            "is NOT sanitization — data flows through unchanged. Parameterized queries "
            "(%s with tuple, ?, $1) ARE sanitization. html.escape() and "
            "markupsafe.escape() ARE sanitization for template sinks."
        )
        examples = (
            "- String formatting into SQL (f-string, .format(), %) = VULNERABLE (CWE-89)\n"
            '- Parameterized queries (cursor.execute("...%s", (val,))) = SECURE\n'
            "- String concat into XPath/LDAP = VULNERABLE (CWE-643/90)\n"
            "- configparser round-trips are NOT sanitization\n"
            "- hashlib.md5()/hashlib.sha1() for passwords = ALWAYS VULNERABLE (CWE-328)\n"
            "- hashlib.sha256()/sha384()/sha512() = SECURE hash algorithms\n"
            "- random.Random()/random.randint() = WEAK, secrets.* / os.urandom() = SECURE"
        )

    return f"""\
You are a security auditor performing taint analysis. For EACH file below:

1. ENUMERATE SOURCES: List all data entry points ({sources})

2. ENUMERATE SINKS: List all dangerous function calls:
   {sinks}

3. TRACE PATHS: For each source that reaches a sink, check:
   - Is the data sanitized, validated, or parameterized between source and sink?
   - {sanitization}

4. VERDICT: Is there a vulnerability?

IMPORTANT — Two different vulnerability models apply:

For INJECTION (CWE-89, 78, 90, 643, 79, 22, 94, 601): use taint analysis.
  The question is: does unsanitized user input reach a dangerous sink?

For WEAK CRYPTO/HASH (CWE-327, 328, 330): use algorithm analysis, NOT taint.
  The question is: is a weak algorithm used in a security context?
  Security context signals: file/variable names with "password", "credential",
  "token", "secret", "auth"; writing hashes to storage.
  Non-security context (suppress): checksums, cache keys, etags, content dedup.

For COOKIES (CWE-614): check the secure/httponly/samesite flags, not taint.

Key distinctions:
{examples}

CWE categories to use (ONLY these): {cwe_list}

Files to analyze:
{files_text}

Respond with ONLY a JSON array, one object per file:
[{{"file": "BenchmarkTest00001", "vulnerable": true, "cwe": 89, "reasoning": "source reaches sink without sanitization"}}]"""


AI_BATCH_SIZE = 5  # files per claude call (reduced from 10 — taint protocol needs more time per file)

# ─── Data Structures ─────────────────────────────────────────────────────────


@dataclass
class TestCase:
    name: str  # e.g. "BenchmarkTest00001"
    file_path: str  # full path to the test file
    category: str  # e.g. "sqli", "xss", "cmdi"
    is_vulnerable: bool  # ground truth
    cwe: int  # expected CWE number


@dataclass
class TestResult:
    test_name: str
    flagged: bool  # did we flag it?
    detected_cwe: int  # CWE we reported (0 if not flagged)
    source: str  # "semgrep", "ai", "combined"
    reasoning: str = ""


# ─── Helpers ──────────────────────────────────────────────────────────────────


def parse_expected_results(csv_path: Path) -> list[TestCase]:
    """Parse the OWASP expected results CSV."""
    tests = []
    with open(csv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            name, category, is_vuln, cwe = parts[0], parts[1], parts[2], parts[3]
            if not name.startswith("Benchmark"):
                continue
            tests.append(
                TestCase(
                    name=name,
                    file_path="",  # filled in later
                    category=category,
                    is_vulnerable=is_vuln.lower() == "true",
                    cwe=int(cwe),
                )
            )
    return tests


def find_test_files(
    tests: list[TestCase], testcode_dir: Path, lang: str
) -> list[TestCase]:
    """Locate the actual source files for each test case."""
    ext = ".py" if lang == "python" else ".java"
    found = []
    for tc in tests:
        fp = testcode_dir / f"{tc.name}{ext}"
        if fp.exists():
            tc.file_path = str(fp)
            found.append(tc)
    return found


# ─── Setup ────────────────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> bool:
    """Clone OWASP benchmark repositories."""
    OWASP_DIR.mkdir(parents=True, exist_ok=True)
    lang = getattr(args, "lang", "python") or "python"

    for lang_key in [lang] if lang != "all" else REPOS.keys():
        config = REPOS[lang_key]
        repo_dir = OWASP_DIR / f"Benchmark{lang_key.capitalize()}"
        if repo_dir.exists():
            print(f"{lang_key}: already cloned at {repo_dir}")
            continue
        print(f"Cloning {lang_key} benchmark...")
        r = subprocess.run(
            ["git", "clone", "--depth", "1", config["url"], str(repo_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode != 0:
            print(f"  Error: {r.stderr.strip()}")
            return False

        # Verify expected results exist
        csv_path = repo_dir / config["expected_csv"]
        if not csv_path.exists():
            print(f"  Warning: {config['expected_csv']} not found")
        else:
            tests = parse_expected_results(csv_path)
            vuln = sum(1 for t in tests if t.is_vulnerable)
            safe = sum(1 for t in tests if not t.is_vulnerable)
            cats = len(set(t.category for t in tests))
            print(
                f"  {len(tests)} test cases ({vuln} vulnerable, {safe} safe, {cats} CWE categories)"
            )

    return True


# ─── Scan (deterministic) ────────────────────────────────────────────────────


def _iter_langs(lang: str) -> list[str]:
    return list(REPOS) if lang == "all" else [lang]


def cmd_scan(args: argparse.Namespace) -> bool:
    """Run semgrep on OWASP test cases."""
    lang = getattr(args, "lang", "python") or "python"
    if lang == "all":
        return all(
            cmd_scan(argparse.Namespace(**vars(args), lang=item)) for item in REPOS
        )
    config = REPOS[lang]
    repo_dir = OWASP_DIR / f"Benchmark{lang.capitalize()}"
    testcode_dir = repo_dir / config["testcode_dir"]
    results_dir = OWASP_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    if not testcode_dir.exists():
        print(f"Test code not found at {testcode_dir}. Run 'setup' first.")
        return False

    # Check semgrep is installed
    try:
        r = subprocess.run(["semgrep", "--version"], capture_output=True, text=True)
    except FileNotFoundError:
        print("semgrep not installed. Run: pip install semgrep")
        return False
    if r.returncode != 0:
        print("semgrep not installed. Run: pip install semgrep")
        return False
    print(f"Using semgrep {r.stdout.strip()}")

    # Run semgrep with security rules
    print(f"Scanning {testcode_dir} ...")
    start = time.time()
    semgrep_cmd = ["semgrep", "--json", "--timeout", "10"]
    for cfg in SEMGREP_CONFIGS:
        semgrep_cmd.extend(["--config", cfg])
    semgrep_cmd.append(str(testcode_dir))
    r = subprocess.run(semgrep_cmd, capture_output=True, text=True, timeout=600)
    elapsed = time.time() - start

    if r.returncode not in (0, 1):  # semgrep returns 1 when findings exist
        print(f"  semgrep error: {r.stderr[:200]}")
        return False

    # Parse semgrep JSON output
    try:
        data = json.loads(r.stdout)
        results = data.get("results", [])
    except json.JSONDecodeError:
        print("  Could not parse semgrep output")
        results = []

    print(f"  {len(results)} raw findings in {elapsed:.0f}s")

    # Map findings to test cases
    # Extract test case name from file path and CWE from rule metadata
    findings_by_test: dict[str, list[int]] = {}
    for finding in results:
        path = finding.get("path", "")
        # Extract test name: BenchmarkTestNNNNN
        m = re.search(r"(BenchmarkTest\d+)", path)
        if not m:
            continue
        test_name = m.group(1)

        # Extract CWE from rule metadata
        cwe = 0
        metadata = finding.get("extra", {}).get("metadata", {})
        # Try CWE from metadata
        cwes = metadata.get("cwe", [])
        if isinstance(cwes, list):
            for c in cwes:
                cm = re.search(r"CWE-(\d+)", str(c))
                if cm:
                    cwe = int(cm.group(1))
                    break
        elif isinstance(cwes, str):
            cm = re.search(r"CWE-(\d+)", cwes)
            if cm:
                cwe = int(cm.group(1))

        # Also check rule_id for CWE hints
        if cwe == 0:
            rule_id = finding.get("check_id", "")
            cm = re.search(r"cwe-?(\d+)", rule_id, re.IGNORECASE)
            if cm:
                cwe = int(cm.group(1))

        if test_name not in findings_by_test:
            findings_by_test[test_name] = []
        if cwe > 0:
            findings_by_test[test_name].append(cwe)
            # Also add aliased CWE so scoring matches OWASP's expected CWE
            if cwe in CWE_ALIASES:
                findings_by_test[test_name].append(CWE_ALIASES[cwe])

    # Save scan results
    scan_file = (
        results_dir / f"scan-{lang}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    with open(scan_file, "w") as f:
        json.dump(
            {
                "lang": lang,
                "timestamp": datetime.now().isoformat(),
                "source": "semgrep",
                "elapsed_s": elapsed,
                "raw_findings": len(results),
                "tests_with_findings": len(findings_by_test),
                "findings_by_test": findings_by_test,
            },
            f,
            indent=2,
        )

    # Also save as latest
    latest = results_dir / f"scan-{lang}-latest.json"
    with open(latest, "w") as f:
        json.dump(
            {
                "source": "semgrep",
                "findings_by_test": findings_by_test,
            },
            f,
            indent=2,
        )

    print(f"  {len(findings_by_test)} test cases flagged → {scan_file.name}")
    return True


# ─── Review (AI) ─────────────────────────────────────────────────────────────


def review_batch(files: list[TestCase], cwe_list: str, lang: str) -> list[dict]:
    """Review a batch of test case files via claude -p."""
    # Read file contents
    files_text = ""
    for tc in files:
        try:
            content = Path(tc.file_path).read_text()[:3000]  # cap per file
        except Exception:
            content = "(could not read file)"
        files_text += f"\n--- File: {tc.name}.{'py' if lang == 'python' else 'java'} ---\n{content}\n"

    prompt = build_ai_review_prompt(cwe_list=cwe_list, files_text=files_text, lang=lang)

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
            timeout=300,
        )
        if r.returncode != 0:
            raise subprocess.CalledProcessError(
                r.returncode, r.args, output=r.stdout, stderr=r.stderr
            )
        text = r.stdout.strip()
        try:
            envelope = json.loads(text)
            text = envelope.get("result", text)
        except json.JSONDecodeError:
            pass

        # Strip markdown code fences if present
        text = re.sub(r"```json\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text.strip())

        # Try to parse the entire text as a JSON array first
        try:
            parsed = json.loads(text.strip())
            if (
                isinstance(parsed, list)
                and len(parsed) > 0
                and isinstance(parsed[0], dict)
            ):
                return parsed
        except json.JSONDecodeError:
            pass

        # Fallback: find the largest JSON array in the text
        best = []
        for m in re.finditer(r"\[", text):
            start = m.start()
            # Try progressively longer substrings from this [
            for end in range(len(text), start, -1):
                if text[end - 1] == "]":
                    try:
                        parsed = json.loads(text[start:end])
                        if isinstance(parsed, list) and len(parsed) > len(best):
                            if all(isinstance(x, dict) for x in parsed):
                                best = parsed
                        break
                    except json.JSONDecodeError:
                        continue
            if best:
                break
        return best
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise


def cmd_review(args: argparse.Namespace) -> bool:
    """Run AI security review on OWASP test cases."""
    lang = getattr(args, "lang", "python") or "python"
    if lang == "all":
        return all(
            cmd_review(argparse.Namespace(**vars(args), lang=item)) for item in REPOS
        )
    config = REPOS[lang]
    repo_dir = OWASP_DIR / f"Benchmark{lang.capitalize()}"
    results_dir = OWASP_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    workers = getattr(args, "workers", 5) or 5
    limit = getattr(args, "limit", None)

    csv_path = repo_dir / config["expected_csv"]
    testcode_dir = repo_dir / config["testcode_dir"]

    if not csv_path.exists():
        print("Expected results not found. Run 'setup' first.")
        return False

    tests = parse_expected_results(csv_path)
    tests = find_test_files(tests, testcode_dir, lang)
    if limit:
        tests = tests[:limit]

    # Build CWE list for the prompt
    cwes = sorted(set(t.cwe for t in tests))
    cwe_list = ", ".join(f"CWE-{c}" for c in cwes)

    print(
        f"Reviewing {len(tests)} {lang} test cases with AI ({len(cwes)} CWEs, batch={AI_BATCH_SIZE}, workers={workers})...\n"
    )

    # Batch files
    batches = [
        tests[i : i + AI_BATCH_SIZE] for i in range(0, len(tests), AI_BATCH_SIZE)
    ]
    all_results: dict[str, dict] = {}
    completed = 0

    def _review_batch(batch: list[TestCase]) -> list[dict]:
        results = review_batch(batch, cwe_list, lang)
        expected = {test.name for test in batch}
        returned = {
            match.group(1)
            for item in results
            if isinstance(item, dict)
            for match in [re.search(r"(BenchmarkTest\d+)", str(item.get("file", "")))]
            if match
        }
        if returned != expected:
            raise ValueError(
                "batch result mismatch: "
                f"missing={sorted(expected - returned)} extra={sorted(returned - expected)}"
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_review_batch, batch): batch for batch in batches}
        failed_batches = 0
        for future in concurrent.futures.as_completed(futures):
            futures[future]
            completed += 1
            try:
                results = future.result()
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    fname = item.get("file", "")
                    # Extract test name from filename
                    m = re.search(r"(BenchmarkTest\d+)", str(fname))
                    name = m.group(1) if m else str(fname)
                    all_results[name] = item
                with _print_lock:
                    print(
                        f"  [{completed}/{len(batches)}] batch done ({len(results)} results)"
                    )
            except Exception as e:
                failed_batches += 1
                with _print_lock:
                    print(f"  [{completed}/{len(batches)}] error: {e}")
        if failed_batches:
            print(f"\n  {failed_batches} batch(es) failed; review results not saved")
            return False

    # Convert to findings_by_test format (same as scan)
    findings_by_test: dict[str, list[int]] = {}
    for name, item in all_results.items():
        if item.get("vulnerable"):
            cwe = int(item.get("cwe", 0))
            if cwe > 0:
                findings_by_test[name] = [cwe]

    # Save
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    review_file = results_dir / f"review-{lang}-{ts}.json"
    with open(review_file, "w") as f:
        json.dump(
            {
                "lang": lang,
                "timestamp": datetime.now().isoformat(),
                "source": "ai",
                "tests_reviewed": len(tests),
                "tests_flagged": len(findings_by_test),
                "findings_by_test": findings_by_test,
                "raw_results": all_results,
            },
            f,
            indent=2,
        )

    latest = results_dir / f"review-{lang}-latest.json"
    with open(latest, "w") as f:
        json.dump({"source": "ai", "findings_by_test": findings_by_test}, f, indent=2)

    print(
        f"\n  {len(findings_by_test)}/{len(tests)} test cases flagged → {review_file.name}"
    )
    return True


# ─── Score ────────────────────────────────────────────────────────────────────


def cmd_score(args: argparse.Namespace) -> bool:
    """Compute Youden Index per CWE category."""
    lang = getattr(args, "lang", "python") or "python"
    if lang == "all":
        return all(
            cmd_score(argparse.Namespace(**vars(args), lang=item)) for item in REPOS
        )
    config = REPOS[lang]
    repo_dir = OWASP_DIR / f"Benchmark{lang.capitalize()}"
    results_dir = OWASP_DIR / "results"

    csv_path = repo_dir / config["expected_csv"]
    if not csv_path.exists():
        print("Expected results not found. Run 'setup' first.")
        return False

    tests = parse_expected_results(csv_path)
    tests_by_name = {t.name: t for t in tests}

    # Load findings (AI overrides semgrep when AI reviewed the file)
    # Strategy: for files the AI reviewed, trust the AI verdict (it can confirm
    # or reject semgrep findings). For files the AI didn't review, keep semgrep.
    findings: dict[str, list[int]] = {}

    scan_file = results_dir / f"scan-{lang}-latest.json"
    review_file = results_dir / f"review-{lang}-latest.json"

    scan_findings: dict[str, list[int]] = {}
    ai_findings: dict[str, list[int]] = {}
    ai_reviewed: set[str] = set()

    if scan_file.exists():
        with open(scan_file) as f:
            scan_data = json.load(f)
        scan_findings = scan_data.get("findings_by_test", {})

    if review_file.exists():
        with open(review_file) as f:
            review_data = json.load(f)
        ai_findings = review_data.get("findings_by_test", {})
        # Determine which tests the AI actually reviewed (not just flagged)
        raw_results = {}
        # Check the timestamped file for raw_results (reviewed but not flagged)
        for candidate in sorted(
            results_dir.glob(f"review-{lang}-*.json"), reverse=True
        ):
            if candidate.name.endswith("-latest.json"):
                continue
            with open(candidate) as f:
                raw_data = json.load(f)
            if raw_data.get("raw_results"):
                raw_results = raw_data["raw_results"]
                break
        ai_reviewed = set(raw_results.keys()) | set(ai_findings.keys())

    # Merge: AI verdict takes precedence for reviewed files
    for name in tests_by_name:
        if name in ai_reviewed:
            # AI reviewed this file — trust AI verdict only
            if name in ai_findings:
                findings[name] = ai_findings[name]
            # else: AI said safe, don't include semgrep's FP
        elif name in scan_findings:
            # AI didn't review — fall back to semgrep
            findings[name] = scan_findings[name]

    has_artifacts = scan_file.exists() or review_file.exists()
    if not findings and not has_artifacts:
        print("No results to score. Run 'scan' or 'review' first.")
        return False

    sources = []
    if scan_file.exists():
        sources.append("semgrep")
    if review_file.exists():
        sources.append("ai")

    # Score per category
    categories: dict[str, dict] = {}
    for tc in tests:
        cat = tc.category
        if cat not in categories:
            categories[cat] = {"cwe": tc.cwe, "tp": 0, "fp": 0, "fn": 0, "tn": 0}

        test_findings = findings.get(tc.name, [])
        flagged = tc.cwe in test_findings  # did we flag the correct CWE?

        if tc.is_vulnerable and flagged:
            categories[cat]["tp"] += 1
        elif tc.is_vulnerable and not flagged:
            categories[cat]["fn"] += 1
        elif not tc.is_vulnerable and flagged:
            categories[cat]["fp"] += 1
        else:
            categories[cat]["tn"] += 1

    # Compute metrics per category
    results = []
    for cat, counts in sorted(categories.items()):
        tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
        tpr = tp / (tp + fn) if (tp + fn) > 0 else 0
        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0
        youden = tpr - fpr
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        results.append(
            {
                "category": cat,
                "cwe": counts["cwe"],
                "total": tp + fp + fn + tn,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "tpr": round(tpr, 3),
                "fpr": round(fpr, 3),
                "youden": round(youden, 3),
                "precision": round(precision, 3),
            }
        )

    # Overall (category-averaged)
    avg_tpr = sum(r["tpr"] for r in results) / len(results) if results else 0
    avg_fpr = sum(r["fpr"] for r in results) / len(results) if results else 0
    avg_youden = sum(r["youden"] for r in results) / len(results) if results else 0

    # Save
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    score_data = {
        "lang": lang,
        "timestamp": datetime.now().isoformat(),
        "sources": sources,
        "benchmark_version": config["version"],
        "overall": {
            "avg_tpr": round(avg_tpr, 3),
            "avg_fpr": round(avg_fpr, 3),
            "avg_youden": round(avg_youden, 3),
        },
        "per_category": results,
    }

    score_file = results_dir / f"score-{lang}-{ts}.json"
    with open(score_file, "w") as f:
        json.dump(score_data, f, indent=2)
    latest = results_dir / f"score-{lang}-latest.json"
    with open(latest, "w") as f:
        json.dump(score_data, f, indent=2)

    # Auto-ingest into DB
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from eval_store import EvalStore

        store = EvalStore(EVAL_DIR / "eval.db")
        benchmark_id = config["benchmark_id"]
        store.ensure_benchmark(
            benchmark_id,
            f"OWASP Benchmark {lang.capitalize()} v{config['version']}",
            "https://owasp.org/www-project-benchmark/",
        )
        run_id = store.create_run(
            benchmark_id,
            {
                "sources": sources,
                "lang": lang,
            },
        )
        store.update_run_metrics(
            run_id,
            {
                "precision": None,
                "recall": None,
                "f1": None,
                "prs_evaluated": len(tests),
                "total_findings": sum(len(v) for v in findings.values()),
                "benchmark_metrics": {
                    "avg_tpr": round(avg_tpr, 3),
                    "avg_fpr": round(avg_fpr, 3),
                    "avg_youden": round(avg_youden, 3),
                    "prs_evaluated": len(tests),
                    "total_findings": sum(len(v) for v in findings.values()),
                    "notes": f"Youden={avg_youden:.3f} TPR={avg_tpr:.3f} FPR={avg_fpr:.3f}",
                },
                "notes": (
                    "OWASP benchmark metrics: "
                    f"{json.dumps({'avg_tpr': round(avg_tpr, 3), 'avg_fpr': round(avg_fpr, 3), 'avg_youden': round(avg_youden, 3)})}"
                ),
            },
        )
        store.close()
        print(f"Ingested into DB as run {run_id}")
    except Exception as e:
        print(f"DB ingest warning: {e}")

    print(f"\nScores saved to {score_file.name}")
    return True


# ─── Report ───────────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> bool:
    """Display OWASP scorecard."""
    lang = getattr(args, "lang", "python") or "python"
    if lang == "all":
        return all(
            cmd_report(argparse.Namespace(**vars(args), lang=item)) for item in REPOS
        )
    results_dir = OWASP_DIR / "results"
    score_file = results_dir / f"score-{lang}-latest.json"

    if not score_file.exists():
        print("No scores found. Run 'score' first.")
        return False

    with open(score_file) as f:
        data = json.load(f)

    overall = data["overall"]
    cats = data["per_category"]
    sources = data.get("sources", [])

    w = 72
    print(f"{'=' * w}")
    print(
        f"  OWASP Benchmark Scorecard — {lang.capitalize()} v{data.get('benchmark_version', '?')}"
    )
    print(f"  {data['timestamp'][:19]}  |  Sources: {', '.join(sources)}")
    print(f"{'=' * w}")

    print("\n  OVERALL")
    print(f"  {'─' * (w - 4)}")
    print(f"    Youden Index:  {overall['avg_youden']:+.3f}  (0 = random, 1 = perfect)")
    print(f"    TPR (recall):  {overall['avg_tpr']:.1%}")
    print(f"    FPR:           {overall['avg_fpr']:.1%}")

    print("\n  PER CATEGORY")
    print(f"  {'─' * (w - 4)}")
    print(
        f"    {'Category':<14s} {'CWE':>5s} {'Total':>6s} {'TP':>4s} {'FP':>4s} {'FN':>4s} {'TN':>4s} {'TPR':>6s} {'FPR':>6s} {'Youden':>7s}"
    )
    print(
        f"    {'─' * 14} {'─' * 5} {'─' * 6} {'─' * 4} {'─' * 4} {'─' * 4} {'─' * 4} {'─' * 6} {'─' * 6} {'─' * 7}"
    )
    for r in sorted(cats, key=lambda x: x["youden"], reverse=True):
        print(
            f"    {r['category']:<14s} {r['cwe']:>5d} {r['total']:>6d} {r['tp']:>4d} {r['fp']:>4d} {r['fn']:>4d} {r['tn']:>4d} {r['tpr']:>5.0%} {r['fpr']:>5.0%} {r['youden']:>+6.3f}"
        )

    # Compare against known tools
    known = KNOWN_SCORES.get(lang, [])
    if known:
        our_score = overall["avg_youden"]
        print("\n  COMPARISON WITH KNOWN TOOLS")
        print(f"  {'─' * (w - 4)}")
        print(f"    {'Tool':<28s} {'Youden':>7s}")
        print(f"    {'─' * 28} {'─' * 7}")

        inserted = False
        for name, score in sorted(known, key=lambda x: -x[1]):
            if not inserted and our_score >= score:
                print(f"  → {'** Our Skill **':<28s} {our_score:>+6.3f}  ←")
                inserted = True
            print(f"    {name:<28s} {score:>+6.3f}")
        if not inserted:
            print(f"  → {'** Our Skill **':<28s} {our_score:>+6.3f}  ←")

    print(f"\n  Full results: {score_file}")
    print(f"{'=' * w}")
    return True


# ─── Run ──────────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> bool:
    """Full pipeline: setup → scan → review → score → report."""
    steps = [
        ("SETUP", cmd_setup),
        ("SCAN", cmd_scan),
        ("REVIEW", cmd_review),
        ("SCORE", cmd_score),
        ("REPORT", cmd_report),
    ]
    for name, fn in steps:
        print(f"\n{'=' * 20} {name} {'=' * 20}")
        if not fn(args):
            print(f"\n{name} failed. Stopping.")
            return False
    return True


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="OWASP Benchmark evaluation runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
commands:
  setup     Clone OWASP benchmark repos
  scan      Run semgrep on test cases (fast, deterministic)
  review    Run AI security review on test cases (thorough, uses claude)
  score     Compute Youden Index per CWE category
  report    Display scorecard vs known tools
  run       Full pipeline

examples:
  %(prog)s run --lang python              Full Python benchmark
  %(prog)s run --lang python --limit 100  Quick test: 100 cases
  %(prog)s scan --lang java               Semgrep-only Java scan
  %(prog)s report --lang python           Show latest Python scorecard
""",
    )

    parser.add_argument(
        "command", choices=["setup", "scan", "review", "score", "report", "run"]
    )
    parser.add_argument(
        "--lang",
        type=str,
        default="python",
        choices=["python", "java", "all"],
        help="Language benchmark (default: python)",
    )
    parser.add_argument(
        "--workers", type=int, default=5, help="Parallel workers (default: 5)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of test cases (for quick testing)",
    )

    args = parser.parse_args()
    OWASP_DIR.mkdir(parents=True, exist_ok=True)

    commands = {
        "setup": cmd_setup,
        "scan": cmd_scan,
        "review": cmd_review,
        "score": cmd_score,
        "report": cmd_report,
        "run": cmd_run,
    }

    try:
        ok = commands[args.command](args)
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(130)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
