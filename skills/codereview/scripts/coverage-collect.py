#!/usr/bin/env python3
"""coverage-collect.py — Collect test coverage data for changed files.

Detects languages from file extensions, checks for existing coverage artifacts,
optionally runs tests, parses coverage output, and filters to changed files.

Input:  CHANGED_FILES on stdin (newline-delimited)
Output: JSON coverage data to stdout

Python 3 stdlib only (json, subprocess, os, sys, argparse, time, pathlib).

Usage:
    echo "$CHANGED_FILES" | python3 scripts/coverage-collect.py \
        [--run-tests] [--timeout 300] > /tmp/codereview-coverage.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extension → language mapping
EXTENSION_LANGUAGE = {
    ".go": "go",
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".js": "typescript",
    ".tsx": "typescript",
    ".jsx": "typescript",
    ".rb": "ruby",
    ".rake": "ruby",
    ".java": "java",
    ".kt": "java",
    ".scala": "java",
}

# Test file patterns to exclude from coverage output
TEST_PATTERNS = [
    re.compile(r"test_[^/]*\.py$"),
    re.compile(r"[^/]*_test\.py$"),
    re.compile(r"[^/]*_test\.go$"),
    re.compile(r"[^/]*\.test\.(ts|js|tsx|jsx)$"),
    re.compile(r"[^/]*\.spec\.(ts|js|tsx|jsx)$"),
    re.compile(r"__tests__/"),
    re.compile(r"tests?/test_"),
    re.compile(r"tests?/.*_test\."),
    re.compile(r"[^/]*_spec\.rb$"),
    re.compile(r"spec/"),
    re.compile(r"[^/]*Test\.java$"),
    re.compile(r"[^/]*Tests\.java$"),
    re.compile(r"src/test/"),
]

# Coverage artifact paths to check, per language (checked in order)
COVERAGE_ARTIFACTS = {
    "go": ["cover.out", "coverage.out"],
    "python": [".coverage", "coverage.json", "cover.json", "htmlcov/"],
    "rust": ["tarpaulin-report.json", "lcov.info", "lcov.json"],
    "typescript": ["coverage/", ".nyc_output/", "coverage-final.json"],
    "ruby": ["coverage/.resultset.json", "coverage/.last_run.json"],
    "java": ["target/site/jacoco/jacoco.xml", "build/reports/jacoco/test/jacocoTestReport.xml"],
}

# Tool detection commands and versions, per language
TOOL_DETECTION = {
    "go": [
        {"name": "go tool cover", "check": ["go", "tool", "cover", "-h"], "tool_key": "coverage_go"},
    ],
    "python": [
        {"name": "coverage", "check": ["coverage", "--version"], "tool_key": "coverage_python"},
        {"name": "pytest-cov", "check": ["pytest", "--co", "-q", "--cov", "--help"], "tool_key": "coverage_python"},
    ],
    "rust": [
        {"name": "cargo-tarpaulin", "check": ["cargo", "tarpaulin", "--version"], "tool_key": "coverage_rust"},
        {"name": "cargo-llvm-cov", "check": ["cargo", "llvm-cov", "--version"], "tool_key": "coverage_rust"},
    ],
    "typescript": [
        {"name": "c8", "check": ["npx", "c8", "--version"], "tool_key": "coverage_typescript"},
        {"name": "nyc", "check": ["npx", "nyc", "--version"], "tool_key": "coverage_typescript"},
        {"name": "jest", "check": ["npx", "jest", "--version"], "tool_key": "coverage_typescript"},
    ],
    "ruby": [
        {"name": "simplecov", "check": ["ruby", "-e", "require 'simplecov'"], "tool_key": "coverage_ruby"},
    ],
    "java": [
        {"name": "jacoco-maven", "check": ["mvn", "--version"], "tool_key": "coverage_java"},
        {"name": "jacoco-gradle", "check": ["gradle", "--version"], "tool_key": "coverage_java"},
    ],
}

# Test generation commands, per language+tool.
# {COVER_DIR} is replaced at runtime with a per-invocation temp directory.
TEST_COMMANDS = {
    "go": {
        "go tool cover": ["go", "test", "-coverprofile={COVER_DIR}/cover.out", "./..."],
    },
    "python": {
        "coverage": ["coverage", "run", "-m", "pytest"],
        "pytest-cov": ["pytest", "--cov", "--cov-report=json:{COVER_DIR}/cover.json"],
    },
    "rust": {
        "cargo-tarpaulin": ["cargo", "tarpaulin", "--out", "json", "--output-dir", "{COVER_DIR}/"],
        "cargo-llvm-cov": ["cargo", "llvm-cov", "--json", "--output-path", "{COVER_DIR}/lcov.json"],
    },
    "typescript": {
        "c8": ["npx", "c8", "--reporter=json", "--reports-dir={COVER_DIR}/", "npm", "test"],
        "nyc": ["npx", "nyc", "--reporter=json", "--report-dir={COVER_DIR}/", "npm", "test"],
        "jest": ["npx", "jest", "--coverage", "--coverageDirectory={COVER_DIR}/"],
    },
    "ruby": {
        "simplecov": ["bundle", "exec", "rspec"],
    },
    "java": {
        "jacoco-maven": ["mvn", "test"],
        "jacoco-gradle": ["gradle", "test", "jacocoTestReport"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_int(value, default=0):
    """Convert value to int, returning default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def is_test_file(filepath):
    """Check if a filepath matches test file patterns."""
    for pattern in TEST_PATTERNS:
        if pattern.search(filepath):
            return True
    return False


def detect_languages(changed_files):
    """Detect unique languages from file extensions in changed files."""
    languages = set()
    for f in changed_files:
        ext = Path(f).suffix.lower()
        lang = EXTENSION_LANGUAGE.get(ext)
        if lang:
            languages.add(lang)
    return sorted(languages)


def filter_changed_files(changed_files, language):
    """Filter changed files to those matching the given language, excluding test files."""
    lang_extensions = [ext for ext, lang in EXTENSION_LANGUAGE.items() if lang == language]
    result = []
    for f in changed_files:
        ext = Path(f).suffix.lower()
        if ext in lang_extensions and not is_test_file(f):
            result.append(f)
    return result


def find_existing_artifact(language, cover_dir=None):
    """Check for existing coverage artifacts for a language. Returns path or None.

    Searches both the repo root and cover_dir (if provided) since test commands
    write artifacts to cover_dir but users may also have pre-existing artifacts.
    """
    artifacts = COVERAGE_ARTIFACTS.get(language, [])
    search_dirs = [Path(".")]
    if cover_dir:
        search_dirs.insert(0, Path(cover_dir))
    for search_dir in search_dirs:
        for artifact in artifacts:
            path = search_dir / artifact
            if path.exists():
                return str(path)
    return None


def check_tool_available(tool_info):
    """Check if a coverage tool is available. Returns (available, version_string)."""
    try:
        result = subprocess.run(
            tool_info["check"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # For some tools, --help returns non-zero but still means installed
        # We check stderr too since some tools print version there
        output = result.stdout.strip() or result.stderr.strip()
        # Try to extract version number
        version = None
        version_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
        if version_match:
            version = version_match.group(1)
        # go tool cover -h returns non-zero but tool is available
        if tool_info["name"] == "go tool cover":
            return True, version
        return result.returncode == 0, version
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, None


def detect_tool(language):
    """Detect the first available coverage tool for a language.
    Returns (tool_name, version, tool_key) or (None, None, tool_key).
    """
    tools = TOOL_DETECTION.get(language, [])
    tool_key = f"coverage_{language}"
    for tool_info in tools:
        available, version = check_tool_available(tool_info)
        if available:
            return tool_info["name"], version, tool_info["tool_key"]
    return None, None, tool_key


def check_staleness(artifact_path, changed_files):
    """Check if coverage artifact is stale compared to recent commits touching changed files.
    Returns (is_stale, message) tuple.
    """
    try:
        artifact_mtime = os.path.getmtime(artifact_path)
    except OSError:
        return False, None

    # Get the most recent commit time for any of the changed files
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--"] + changed_files,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            commit_time = int(result.stdout.strip())
            if artifact_mtime < commit_time:
                days_stale = (commit_time - artifact_mtime) / 86400
                if days_stale >= 1:
                    return True, f"Coverage data may be stale (predates recent changes by {int(days_stale)} days)"
                else:
                    return True, "Coverage data may be stale (predates recent changes)"
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass

    return False, None


def run_tests(language, tool_name, timeout, cover_dir="/tmp"):
    """Run tests with coverage for a language. Returns (success, partial, note)."""
    commands = TEST_COMMANDS.get(language, {})
    cmd_template = commands.get(tool_name)
    if not cmd_template:
        return False, False, f"No test command configured for {tool_name}"

    # Substitute {COVER_DIR} with the per-invocation temp directory
    cmd = [arg.replace("{COVER_DIR}", cover_dir) for arg in cmd_template]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, False, None
        else:
            # Tests failed but may have generated partial coverage
            return False, True, f"Tests exited with code {result.returncode}; partial coverage may be available"
    except subprocess.TimeoutExpired:
        return False, True, f"Test suite timed out after {timeout}s; partial coverage may be available"
    except (FileNotFoundError, OSError) as e:
        return False, False, f"Failed to run tests: {e}"


# ---------------------------------------------------------------------------
# Coverage parsers
# ---------------------------------------------------------------------------

def parse_go_coverage(artifact_path, changed_files):
    """Parse Go cover.out format. Returns list of coverage entries."""
    entries = []
    file_stats = {}  # file -> {total_stmts, covered_stmts}

    try:
        with open(artifact_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("mode:") or not line:
                    continue
                # Format: <file>:<startline>.<startcol>,<endline>.<endcol> <num_stmts> <count>
                parts = line.split()
                if len(parts) < 3:
                    continue
                file_part = parts[0].split(":")[0]
                num_stmts = int(parts[1])
                count = int(parts[2])

                if file_part not in file_stats:
                    file_stats[file_part] = {"total": 0, "covered": 0}
                file_stats[file_part]["total"] += num_stmts
                if count > 0:
                    file_stats[file_part]["covered"] += num_stmts
    except (OSError, ValueError):
        return entries

    # Filter to changed files and compute percentages
    changed_set = set(changed_files)
    for filepath, stats in file_stats.items():
        # Match against changed files (may need to strip module prefix)
        matched_file = None
        for cf in changed_set:
            if filepath.endswith(cf) or cf.endswith(filepath) or filepath == cf:
                matched_file = cf
                break
        if matched_file and not is_test_file(matched_file):
            coverage_pct = int((stats["covered"] / stats["total"] * 100)) if stats["total"] > 0 else 0
            entries.append({
                "file": matched_file,
                "line_coverage": coverage_pct,
                "uncovered_functions": [],
                "tool": "go tool cover",
            })

    return entries


def parse_go_func_output(func_output, changed_files):
    """Parse 'go tool cover -func' output for uncovered functions."""
    uncovered = {}  # file -> [func_names]
    changed_set = set(changed_files)

    for line in func_output.strip().split("\n"):
        line = line.strip()
        if not line or "total:" in line:
            continue
        # Format: <file>:<line>: <func_name> <coverage%>
        parts = line.split()
        if len(parts) < 3:
            continue
        file_part = parts[0].rstrip(":")
        file_path = file_part.rsplit(":", 1)[0] if ":" in file_part else file_part
        func_name = parts[1] if len(parts) >= 3 else ""
        coverage_str = parts[-1]

        if coverage_str == "0.0%":
            for cf in changed_set:
                if file_path.endswith(cf) or cf.endswith(file_path) or file_path == cf:
                    if cf not in uncovered:
                        uncovered[cf] = []
                    if func_name:
                        uncovered[cf].append(func_name)
                    break

    return uncovered


def parse_python_coverage_json(artifact_path, changed_files):
    """Parse Python coverage.json format. Returns list of coverage entries."""
    entries = []
    try:
        with open(artifact_path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries

    files_data = data.get("files", {})
    changed_set = set(changed_files)

    for filepath, file_info in files_data.items():
        matched_file = None
        for cf in changed_set:
            if filepath.endswith(cf) or cf.endswith(filepath) or filepath == cf:
                matched_file = cf
                break
        if matched_file and not is_test_file(matched_file):
            summary = file_info.get("summary", {})
            coverage_pct = int(summary.get("percent_covered", 0))
            # Identify uncovered functions from missing lines
            missing_lines = set(file_info.get("missing_lines", []))
            uncovered_funcs = _extract_uncovered_functions_python(matched_file, missing_lines)
            entries.append({
                "file": matched_file,
                "line_coverage": coverage_pct,
                "uncovered_functions": uncovered_funcs,
                "tool": "coverage.py",
            })

    return entries


def parse_python_coverage_db(changed_files):
    """Parse Python .coverage database using coverage json export. Returns list of coverage entries."""
    import tempfile as _tempfile
    export_fd, export_path = _tempfile.mkstemp(suffix=".json", prefix="codereview-cov-")
    os.close(export_fd)
    try:
        result = subprocess.run(
            ["coverage", "json", "-o", export_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return parse_python_coverage_json(export_path, changed_files)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    finally:
        try:
            os.unlink(export_path)
        except OSError:
            pass
    return []


def _extract_uncovered_functions_python(filepath, missing_lines):
    """Try to identify function names at uncovered lines in a Python file."""
    uncovered = []
    try:
        with open(filepath, "r") as f:
            for lineno, line in enumerate(f, 1):
                if lineno in missing_lines:
                    # Check if this line is a function definition
                    match = re.match(r"\s*def\s+(\w+)\s*\(", line)
                    if match:
                        uncovered.append(match.group(1))
    except OSError:
        pass
    return uncovered


def parse_rust_tarpaulin(artifact_path, changed_files):
    """Parse cargo-tarpaulin JSON report. Returns list of coverage entries."""
    entries = []
    try:
        with open(artifact_path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries

    changed_set = set(changed_files)
    file_stats = {}  # file -> {total, covered, uncovered_funcs}

    # Tarpaulin JSON has a list of coverage entries
    for entry in data if isinstance(data, list) else data.get("files", []):
        filepath = entry.get("path", "")
        matched_file = None
        for cf in changed_set:
            if filepath.endswith(cf) or cf.endswith(filepath) or filepath == cf:
                matched_file = cf
                break
        if not matched_file or is_test_file(matched_file):
            continue

        if matched_file not in file_stats:
            file_stats[matched_file] = {"total": 0, "covered": 0, "uncovered_funcs": set()}

        traces = entry.get("traces", [])
        for trace in traces:
            file_stats[matched_file]["total"] += 1
            if trace.get("stats", {}).get("Line", 0) > 0 or trace.get("hits", 0) > 0:
                file_stats[matched_file]["covered"] += 1
            else:
                fn = trace.get("fn_name")
                if fn:
                    file_stats[matched_file]["uncovered_funcs"].add(fn)

    for filepath, stats in file_stats.items():
        coverage_pct = int((stats["covered"] / stats["total"] * 100)) if stats["total"] > 0 else 0
        entries.append({
            "file": filepath,
            "line_coverage": coverage_pct,
            "uncovered_functions": sorted(stats["uncovered_funcs"]),
            "tool": "cargo-tarpaulin",
        })

    return entries


def parse_lcov(artifact_path, changed_files):
    """Parse lcov.info format (used by cargo-llvm-cov and others). Returns list of coverage entries."""
    entries = []
    changed_set = set(changed_files)
    current_file = None
    total_lines = 0
    covered_lines = 0
    uncovered_funcs = []
    current_matched = None

    try:
        with open(artifact_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SF:"):
                    # New source file
                    current_file = line[3:]
                    current_matched = None
                    for cf in changed_set:
                        if current_file.endswith(cf) or cf.endswith(current_file) or current_file == cf:
                            current_matched = cf
                            break
                    total_lines = 0
                    covered_lines = 0
                    uncovered_funcs = []
                elif line.startswith("DA:"):
                    parts = line[3:].split(",")
                    if len(parts) >= 2:
                        try:
                            total_lines += 1
                            if int(parts[1]) > 0:
                                covered_lines += 1
                        except (ValueError, IndexError):
                            pass  # skip malformed DA line
                elif line.startswith("FNDA:"):
                    parts = line[5:].split(",")
                    if len(parts) >= 2 and parts[0] == "0":
                        uncovered_funcs.append(parts[1])
                elif line == "end_of_record":
                    if current_matched and not is_test_file(current_matched):
                        coverage_pct = int((covered_lines / total_lines * 100)) if total_lines > 0 else 0
                        entries.append({
                            "file": current_matched,
                            "line_coverage": coverage_pct,
                            "uncovered_functions": uncovered_funcs,
                            "tool": "lcov",
                        })
    except (OSError, ValueError):
        pass

    return entries


def parse_istanbul_json(artifact_path, changed_files):
    """Parse Istanbul/NYC/c8 JSON coverage format. Returns list of coverage entries."""
    entries = []
    try:
        with open(artifact_path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries

    changed_set = set(changed_files)

    for filepath, file_info in data.items():
        matched_file = None
        for cf in changed_set:
            if filepath.endswith(cf) or cf.endswith(filepath) or filepath == cf:
                matched_file = cf
                break
        if not matched_file or is_test_file(matched_file):
            continue

        # Compute line coverage from statement map
        stmt_map = file_info.get("statementMap", {})
        stmt_hits = file_info.get("s", {})
        if not isinstance(stmt_hits, dict):
            stmt_hits = {}
        total = len(stmt_map)
        covered = sum(1 for k in stmt_hits if _safe_int(stmt_hits[k]) > 0)
        coverage_pct = int((covered / total * 100)) if total > 0 else 0

        # Identify uncovered functions
        fn_map = file_info.get("fnMap", {})
        fn_hits = file_info.get("f", {})
        uncovered_funcs = []
        for key, fn_info in fn_map.items():
            if int(fn_hits.get(key, 0)) == 0:
                name = fn_info.get("name", "")
                if name and name != "(anonymous)":
                    uncovered_funcs.append(name)

        entries.append({
            "file": matched_file,
            "line_coverage": coverage_pct,
            "uncovered_functions": uncovered_funcs,
            "tool": "istanbul",
        })

    return entries


def parse_typescript_coverage(artifact_path, changed_files):
    """Parse TypeScript/JS coverage from coverage directory or file. Returns list of coverage entries."""
    path = Path(artifact_path)
    if path.is_dir():
        # Look for coverage-final.json or coverage-summary.json inside the dir
        for candidate in ["coverage-final.json", "coverage/coverage-final.json"]:
            candidate_path = path / candidate
            if candidate_path.exists():
                return parse_istanbul_json(str(candidate_path), changed_files)
        # Also check lcov.info
        lcov_path = path / "lcov.info"
        if lcov_path.exists():
            return parse_lcov(str(lcov_path), changed_files)
    elif path.is_file():
        if path.suffix == ".json":
            return parse_istanbul_json(str(path), changed_files)
        elif path.name == "lcov.info":
            return parse_lcov(str(path), changed_files)
    return []


def parse_simplecov_json(artifact_path, changed_files):
    """Parse SimpleCov .resultset.json coverage format (Ruby).

    Format: {"RSpec": {"coverage": {"/abs/path/file.rb": {"lines": [null, 1, 0, ...]}}}}
    null = not executable, 0 = not covered, >0 = covered.
    Note: SimpleCov uses absolute paths — match by suffix against relative changed_files.
    """
    entries = []
    try:
        with open(artifact_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries

    changed_set = set(changed_files)

    # Iterate over test suites (usually "RSpec" or "MiniTest")
    for suite_name, suite_data in data.items():
        if not isinstance(suite_data, dict):
            continue
        coverage_data = suite_data.get("coverage", {})
        if not isinstance(coverage_data, dict):
            continue

        for abs_filepath, file_info in coverage_data.items():
            # Match absolute path against relative changed files by suffix
            matched_file = None
            for cf in changed_set:
                if abs_filepath.endswith("/" + cf) or abs_filepath == cf:
                    matched_file = cf
                    break
            if matched_file is None or is_test_file(matched_file):
                continue

            # file_info can be {"lines": [...]} or just [...]
            lines = file_info.get("lines", file_info) if isinstance(file_info, dict) else file_info
            if not isinstance(lines, list):
                continue

            executable = [l for l in lines if l is not None]
            covered = [l for l in executable if l > 0]
            total = len(executable)
            coverage_pct = int((len(covered) / total * 100)) if total > 0 else 0

            entries.append({
                "file": matched_file,
                "line_coverage": coverage_pct,
                "uncovered_functions": [],
                "tool": "simplecov",
            })

    return entries


def parse_jacoco_xml(artifact_path, changed_files):
    """Parse JaCoCo XML coverage format (Java).

    Format: <report><package><sourcefile name="Foo.java">
              <counter type="LINE" missed="5" covered="20"/>
            </sourcefile></package></report>
    """
    import xml.etree.ElementTree as ET
    entries = []
    try:
        tree = ET.parse(artifact_path)
        root = tree.getroot()
    except (OSError, ET.ParseError):
        return entries

    changed_set = set(changed_files)

    for package in root.findall(".//package"):
        pkg_name = package.get("name", "").replace("/", os.sep)
        for sourcefile in package.findall("sourcefile"):
            sf_name = sourcefile.get("name", "")
            # Reconstruct relative path: package/name → src/main/java/package/name
            # Try matching against changed files
            matched_file = None
            for cf in changed_set:
                if cf.endswith(sf_name) or cf.endswith(pkg_name + os.sep + sf_name):
                    matched_file = cf
                    break
            if matched_file is None or is_test_file(matched_file):
                continue

            for counter in sourcefile.findall("counter"):
                if counter.get("type") == "LINE":
                    covered = _safe_int(counter.get("covered", 0))
                    missed = _safe_int(counter.get("missed", 0))
                    total = covered + missed
                    coverage_pct = int((covered / total * 100)) if total > 0 else 0
                    entries.append({
                        "file": matched_file,
                        "line_coverage": coverage_pct,
                        "uncovered_functions": [],
                        "tool": "jacoco",
                    })
                    break

    return entries


# ---------------------------------------------------------------------------
# Per-language coverage collection
# ---------------------------------------------------------------------------

def collect_coverage_for_language(language, changed_files, run_tests_flag, timeout):
    """Collect coverage data for a single language.
    Returns (coverage_entries, tool_status_dict, warnings_list).
    """
    import tempfile as _tempfile
    import shutil as _shutil

    tool_key = f"coverage_{language}"
    lang_files = filter_changed_files(changed_files, language)
    # Per-invocation temp directory for coverage artifacts
    cover_dir = _tempfile.mkdtemp(prefix="codereview-cover-")
    try:
        return _collect_coverage_impl(language, changed_files, run_tests_flag, timeout, lang_files, tool_key, cover_dir)
    finally:
        _shutil.rmtree(cover_dir, ignore_errors=True)


def _collect_coverage_impl(language, changed_files, run_tests_flag, timeout, lang_files, tool_key, cover_dir):
    """Inner implementation for collect_coverage_for_language."""
    warnings = []

    if not lang_files:
        return [], {tool_key: {
            "status": "skipped",
            "version": None,
            "finding_count": 0,
            "note": f"No non-test {language} files in changed files",
        }}, []

    # Step 1: Check for existing coverage artifacts
    artifact_path = find_existing_artifact(language)
    artifact_from_tests = False

    # Step 2: If no existing artifact and --run-tests, detect tool and run tests
    if artifact_path is None and run_tests_flag:
        tool_name, tool_version, tool_key = detect_tool(language)
        if tool_name is None:
            return [], {tool_key: {
                "status": "not_installed",
                "version": None,
                "finding_count": 0,
                "note": f"No coverage tool found for {language}",
            }}, []

        success, partial, note = run_tests(language, tool_name, timeout, cover_dir)
        if not success and not partial:
            return [], {tool_key: {
                "status": "failed",
                "version": tool_version,
                "finding_count": 0,
                "note": note,
            }}, []

        # Re-check for artifacts after running tests (check cover_dir first)
        artifact_path = find_existing_artifact(language, cover_dir=cover_dir)
        artifact_from_tests = True

        if artifact_path is None:
            status = "partial" if partial else "failed"
            return [], {tool_key: {
                "status": status,
                "version": tool_version,
                "finding_count": 0,
                "note": note or "Tests ran but no coverage artifact was generated",
            }}, []

        if partial:
            warnings.append(note)

    elif artifact_path is None:
        # No existing data and --run-tests not set
        tool_name, tool_version, tool_key = detect_tool(language)
        return [], {tool_key: {
            "status": "skipped",
            "version": tool_version,
            "finding_count": 0,
            "note": "No existing coverage data found. Set coverage.run_tests: true to generate.",
        }}, []

    # Step 3: Check staleness (skip if we just generated it)
    if not artifact_from_tests:
        is_stale, stale_msg = check_staleness(artifact_path, lang_files)
        if is_stale and stale_msg:
            warnings.append(stale_msg)

    # Step 4: Parse coverage data
    entries = _parse_coverage(language, artifact_path, lang_files)

    # Detect tool info for status
    tool_name, tool_version, tool_key = detect_tool(language)
    status = "ran" if entries else "skipped"
    if artifact_from_tests:
        status = "ran"

    return entries, {tool_key: {
        "status": status,
        "version": tool_version,
        "finding_count": len(entries),
        "note": None,
    }}, warnings


def _parse_coverage(language, artifact_path, changed_files):
    """Route to the correct parser based on language and artifact type."""
    path = Path(artifact_path)

    if language == "go":
        entries = parse_go_coverage(artifact_path, changed_files)
        # Try to get uncovered function names via go tool cover -func
        try:
            result = subprocess.run(
                ["go", "tool", "cover", "-func", artifact_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                uncovered = parse_go_func_output(result.stdout, changed_files)
                for entry in entries:
                    if entry["file"] in uncovered:
                        entry["uncovered_functions"] = uncovered[entry["file"]]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return entries

    elif language == "python":
        if path.name == "coverage.json" or path.suffix == ".json":
            return parse_python_coverage_json(artifact_path, changed_files)
        elif path.name == ".coverage":
            return parse_python_coverage_db(changed_files)
        return []

    elif language == "rust":
        if "tarpaulin" in path.name:
            return parse_rust_tarpaulin(artifact_path, changed_files)
        elif path.name == "lcov.info":
            return parse_lcov(artifact_path, changed_files)
        return []

    elif language == "typescript":
        return parse_typescript_coverage(artifact_path, changed_files)

    elif language == "ruby":
        if path.suffix == ".json":
            return parse_simplecov_json(artifact_path, changed_files)
        return []

    elif language == "java":
        if path.suffix == ".xml":
            return parse_jacoco_xml(artifact_path, changed_files)
        return []

    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collect test coverage data for changed files.",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        default=False,
        help="Run tests to generate coverage if no existing data found (default: off)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for test execution (default: 300)",
    )
    args = parser.parse_args()

    # Read changed files from stdin
    stdin_data = sys.stdin.read().strip()
    if not stdin_data:
        # Empty input — output valid empty result
        result = {
            "languages_detected": [],
            "coverage_data": [],
            "tool_status": {},
            "warnings": [],
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    changed_files = [f for f in stdin_data.split("\n") if f.strip()]

    # Detect languages
    languages = detect_languages(changed_files)

    if not languages:
        result = {
            "languages_detected": [],
            "coverage_data": [],
            "tool_status": {},
            "warnings": [],
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    # Collect coverage for each language independently
    all_coverage = []
    all_tool_status = {}
    all_warnings = []

    for lang in languages:
        entries, status, warnings = collect_coverage_for_language(
            lang, changed_files, args.run_tests, args.timeout,
        )
        all_coverage.extend(entries)
        all_tool_status.update(status)
        all_warnings.extend(warnings)

    result = {
        "languages_detected": languages,
        "coverage_data": all_coverage,
        "tool_status": all_tool_status,
        "warnings": all_warnings,
    }

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
