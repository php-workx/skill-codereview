#!/usr/bin/env python3
"""Pre-scan signal detection -- fast static checks for explorer context.

Reads changed file paths from stdin (newline-delimited), outputs JSON prescan
signals to stdout.  These are *context signals* for explorers, NOT findings --
they are never passed to enrich-findings.py.

Regex-only mode.  P-UNWIRED is skipped (requires import graph).

Usage:  echo "$CHANGED_FILES" | python3 scripts/prescan.py > prescan.json
"""

from __future__ import annotations
import json
import re
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

WALL_CLOCK_LIMIT = 15.0
PER_FILE_LIMIT = 0.5
MAX_FILE_COUNT = 200
MAX_FILE_LINES = 10_000
EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".rs": "rust",
    ".sh": "shell",
    ".bash": "shell",
    ".rb": "ruby",
}
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "vendor", "dist"}
GENERATED_SUFFIXES = (".pb.go", ".generated.ts", ".generated.js", ".g.dart")

_FUNC_START_RE: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^[ \t]*(?:async\s+)?def\s+(?P<name>\w+)\s*\(", re.MULTILINE),
    "go": re.compile(
        r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(?P<name>\w+)\s*\(", re.MULTILINE
    ),
    "typescript": re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*[\(<]",
        re.MULTILINE,
    ),
    "javascript": re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*[\(<]",
        re.MULTILINE,
    ),
    "java": re.compile(
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?[\w<>\[\]?,\s]+\s+(?P<name>\w+)\s*\(",
        re.MULTILINE,
    ),
    "rust": re.compile(
        r"^\s*(?:pub(?:\s*\(crate\))?\s+)?(?:async\s+)?fn\s+(?P<name>\w+)", re.MULTILINE
    ),
}

# --- helpers ----------------------------------------------------------------


def _detect_language(fp: str) -> str | None:
    return EXTENSION_MAP.get(Path(fp).suffix.lower())


def _read_file_safe(fp: str) -> str | None:
    try:
        p = Path(fp)
        if not p.is_file() or p.stat().st_size > 2_000_000:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _is_test_file(fp: str) -> bool:
    parts = Path(fp).parts
    name = Path(fp).name.lower()
    if any(p in ("tests", "test", "__tests__", "spec") for p in parts):
        return True
    return name.startswith("test_") or name.startswith("test.")


def _should_skip(fp: str) -> bool:
    parts = Path(fp).parts
    return any(p in SKIP_DIRS for p in parts) or any(
        fp.endswith(s) for s in GENERATED_SUFFIXES
    )


def _find_func_end_py(lines: list[str], start: int, indent: int) -> int:
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if not s:
            continue
        if len(lines[i]) - len(lines[i].lstrip()) <= indent and not s.startswith("#"):
            return i
    return len(lines)


def _find_func_end_brace(lines: list[str], start: int) -> int:
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return i + 1
    return len(lines)


def _extract_function_ranges(
    content: str, lines: list[str], lang: str
) -> list[tuple[str, int, int]]:
    """Return (name, start_1based, end_1based) per function."""
    func_re = _FUNC_START_RE.get(lang)
    if not func_re:
        return []
    results: list[tuple[str, int, int]] = []
    for m in func_re.finditer(content):
        name = m.group("name")
        start = content[: m.start()].count("\n") + 1
        indent = (
            len(lines[start - 1]) - len(lines[start - 1].lstrip())
            if start <= len(lines)
            else 0
        )
        end = (
            _find_func_end_py(lines, start - 1, indent)
            if lang == "python"
            else _find_func_end_brace(lines, start - 1)
        )
        results.append((name, start, end))
    return results


def _finding(
    file: str, line: int, pid: str, desc: str, evidence: str
) -> dict[str, Any]:
    return {
        "file": file,
        "line": line,
        "pattern_id": pid,
        "description": desc,
        "evidence": evidence,
    }


# --- pattern checkers -------------------------------------------------------


class PatternChecker(ABC):
    pattern_id: str
    severity: str
    pattern_name: str

    @abstractmethod
    def check(
        self, fp: str, content: str, lang: str | None
    ) -> list[dict[str, Any]]: ...


class SecretChecker(PatternChecker):
    pattern_id = "P-SEC"
    severity = "critical"
    pattern_name = "hardcoded_secrets"
    _RE = re.compile(
        r"(?:password|secret|api_key|api_secret|token|auth_token|access_key)\s*=\s*['\"][^'\"]{3,}['\"]",
        re.IGNORECASE,
    )

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        if _is_test_file(fp):
            return []
        lines = content.splitlines()
        findings: list[dict[str, Any]] = []
        for m in self._RE.finditer(content):
            ln = content[: m.start()].count("\n") + 1
            findings.append(
                _finding(
                    fp,
                    ln,
                    self.pattern_id,
                    "Possible hardcoded credential",
                    lines[ln - 1].strip(),
                )
            )
        return findings


class SwallowedErrorChecker(PatternChecker):
    pattern_id = "P-ERR"
    severity = "high"
    pattern_name = "swallowed_errors"
    _PATS = [
        re.compile(r"except[^:]*:.*\n\s+pass\b", re.MULTILINE),
        re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}", re.MULTILINE),
        re.compile(r"_\s*=\s*err\b"),
    ]

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        lines = content.splitlines()
        findings: list[dict[str, Any]] = []
        for pat in self._PATS:
            for m in pat.finditer(content):
                ln = content[: m.start()].count("\n") + 1
                findings.append(
                    _finding(
                        fp,
                        ln,
                        self.pattern_id,
                        "Swallowed error (error ignored without handling)",
                        lines[ln - 1].strip() if ln <= len(lines) else "",
                    )
                )
        return findings


class LongFunctionChecker(PatternChecker):
    pattern_id = "P-LEN"
    severity = "medium"
    pattern_name = "long_functions"
    THRESHOLD = 50

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        if not lang:
            return []
        lines = content.splitlines()
        findings: list[dict[str, Any]] = []
        for name, start, end in _extract_function_ranges(content, lines, lang):
            length = end - start
            if length > self.THRESHOLD:
                findings.append(
                    _finding(
                        fp,
                        start,
                        self.pattern_id,
                        f"Function '{name}' is {length} lines (threshold: {self.THRESHOLD})",
                        f"def {name}(...) at line {start}",
                    )
                )
        return findings


class TodoChecker(PatternChecker):
    pattern_id = "P-TODO"
    severity = "low"
    pattern_name = "todo_fixme"
    _RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for i, line in enumerate(content.splitlines(), 1):
            m = self._RE.search(line)
            if m:
                findings.append(
                    _finding(
                        fp,
                        i,
                        self.pattern_id,
                        f"{m.group(1)} marker found",
                        line.strip(),
                    )
                )
        return findings


class CommentedCodeChecker(PatternChecker):
    pattern_id = "P-COMMENT"
    severity = "low"
    pattern_name = "commented_code"
    _RE = re.compile(
        r"^\s*[#/]{1,2}\s*(def |func |function |class |if |for |return |import |from )",
        re.MULTILINE,
    )

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        lines = content.splitlines()
        findings: list[dict[str, Any]] = []
        for m in self._RE.finditer(content):
            ln = content[: m.start()].count("\n") + 1
            findings.append(
                _finding(
                    fp,
                    ln,
                    self.pattern_id,
                    "Commented-out code detected",
                    lines[ln - 1].strip() if ln <= len(lines) else "",
                )
            )
        return findings


class DeadCodeChecker(PatternChecker):
    pattern_id = "P-DEAD"
    severity = "medium"
    pattern_name = "dead_code_candidates"

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        if not lang:
            return []
        lines = content.splitlines()
        funcs = _extract_function_ranges(content, lines, lang)
        if not funcs:
            return []
        findings: list[dict[str, Any]] = []
        for name, start, _end in funcs:
            call_re = re.compile(r"\b" + re.escape(name) + r"\b")
            if not any(
                call_re.search(lines[i]) for i in range(len(lines)) if i != start - 1
            ):
                findings.append(
                    _finding(
                        fp,
                        start,
                        self.pattern_id,
                        f"Function '{name}' may be dead code (not referenced in this file)",
                        f"def {name}(...) at line {start}",
                    )
                )
        return findings


_BOILERPLATE_RE = re.compile(
    r"^(?:pub(?:\s*\(crate\))?\s+)?(?:async\s+)?(?:def|fn|func|function)\s+"
)


class StubChecker(PatternChecker):
    pattern_id = "P-STUB"
    severity = "high"
    pattern_name = "stub_placeholder"
    _PATS = [
        re.compile(r"^\s*pass\s*$", re.MULTILINE),
        re.compile(r"raise\s+NotImplementedError\b"),
        re.compile(r"\b(?:todo|unimplemented)!\s*\("),
    ]

    @staticmethod
    def _is_boilerplate(line: str) -> bool:
        s = line.strip()
        if not s or s.startswith(("#", "//", "/*", "*")):
            return True
        if s in ("{", "}", "};", ");"):
            return True
        return bool(_BOILERPLATE_RE.match(s))

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        if not lang:
            return []
        lines = content.splitlines()
        funcs = _extract_function_ranges(content, lines, lang)
        if not funcs:
            return []
        findings: list[dict[str, Any]] = []
        for name, start, end in funcs:
            substantive = [
                ln for ln in lines[start - 1 : end] if not self._is_boilerplate(ln)
            ]
            if not substantive:
                continue
            body = "\n".join(substantive)
            for pat in self._PATS:
                if pat.search(body) and len(substantive) <= 2:
                    findings.append(
                        _finding(
                            fp,
                            start,
                            self.pattern_id,
                            f"Function '{name}' is a stub/placeholder",
                            substantive[0].strip(),
                        )
                    )
                    break
        return findings


class UnwiredChecker(PatternChecker):
    """P-UNWIRED requires an import graph -- skipped in regex-only mode."""

    pattern_id = "P-UNWIRED"
    severity = "medium"
    pattern_name = "unwired_components"

    def check(self, fp: str, content: str, lang: str | None) -> list[dict[str, Any]]:
        return []  # requires import graph, not available in regex mode


ALL_CHECKERS: list[PatternChecker] = [
    SecretChecker(),
    SwallowedErrorChecker(),
    LongFunctionChecker(),
    TodoChecker(),
    CommentedCodeChecker(),
    DeadCodeChecker(),
    StubChecker(),
    UnwiredChecker(),
]

# --- completeness -----------------------------------------------------------


def _assess_completeness(
    fp: str,
    content: str,
    lang: str | None,
    all_files: list[str],
    stub_files: set[str],
    file_cache: dict[str, str] | None = None,
) -> str:
    """L1 exists, L2 substantive, L3 wired, L4 functional."""
    if not lang:
        return "L1_exists"
    lines = content.splitlines()
    funcs = _extract_function_ranges(content, lines, lang)
    if not funcs:
        return "L1_exists"
    has_sub = any(
        sum(
            1
            for ln in lines[s:e]
            if ln.strip() and not ln.strip().startswith(("#", "//", '"""', "'''"))
        )
        > 5
        for _, s, e in funcs
    )
    if not has_sub:
        return "L1_exists"
    stem = Path(fp).stem
    cache = file_cache or {}
    wired = any(
        re.search(r"\b" + re.escape(stem) + r"\b", cache[o])
        for o in all_files
        if o != fp and o in cache
    )
    if wired and fp not in stub_files:
        return "L4_functional"
    if wired and fp in stub_files:
        return "L3_wired"
    if has_sub:
        return "L2_substantive"
    return "L1_exists"


# --- main scan --------------------------------------------------------------


def run_prescan(file_paths: list[str]) -> dict[str, Any]:
    """Run all checkers on given files, return structured output."""
    start_time = time.monotonic()
    filtered = [f for f in file_paths if not _should_skip(f)][:MAX_FILE_COUNT]
    empty_levels = {
        "L4_functional": [],
        "L3_wired": [],
        "L2_substantive": [],
        "L1_exists": [],
    }
    if not filtered:
        return {
            "file_count": 0,
            "analyzer": "regex-only",
            "languages_detected": [],
            "patterns": {},
            "implementation_completeness": {
                "files_assessed": 0,
                "levels": dict(empty_levels),
                "summary": "No files to assess",
            },
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        }

    checker_findings: dict[str, list[dict[str, Any]]] = {
        c.pattern_name: [] for c in ALL_CHECKERS
    }
    languages: set[str] = set()
    stub_files: set[str] = set()
    file_cache: dict[str, str] = {}
    timed_out = False

    for fpath in filtered:
        if time.monotonic() - start_time >= WALL_CLOCK_LIMIT:
            timed_out = True
            break
        content = _read_file_safe(fpath)
        if content is None:
            continue
        if len(content.splitlines()) > MAX_FILE_LINES:
            continue
        file_cache[fpath] = content
        lang = _detect_language(fpath)
        if lang:
            languages.add(lang)
        file_start = time.monotonic()
        for checker in ALL_CHECKERS:
            if time.monotonic() - file_start >= PER_FILE_LIMIT:
                break
            findings = checker.check(fpath, content, lang)
            if findings:
                checker_findings[checker.pattern_name].extend(findings)
                if checker.pattern_id == "P-STUB":
                    stub_files.add(fpath)

    patterns: dict[str, dict[str, Any]] = {}
    sev_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for c in ALL_CHECKERS:
        f = checker_findings[c.pattern_name]
        if f or c.pattern_id != "P-UNWIRED":
            patterns[c.pattern_name] = {
                "count": len(f),
                "severity": c.severity,
                "findings": f,
            }
            sev_counts[c.severity] = sev_counts.get(c.severity, 0) + len(f)

    levels = dict(empty_levels)
    assessed = 0
    for fpath in filtered:
        if time.monotonic() - start_time >= WALL_CLOCK_LIMIT:
            timed_out = True
            break
        content = file_cache.get(fpath)
        if content is None:
            continue
        lang = _detect_language(fpath)
        level = _assess_completeness(
            fpath, content, lang, filtered, stub_files, file_cache
        )
        levels[level].append(fpath)
        assessed += 1
    lc = {k: len(v) for k, v in levels.items() if v}
    result: dict[str, Any] = {
        "file_count": len(filtered),
        "analyzer": "regex-only",
        "languages_detected": sorted(languages),
        "patterns": patterns,
        "implementation_completeness": {
            "files_assessed": assessed,
            "levels": levels,
            "summary": ", ".join(f"{n} files at {k}" for k, n in lc.items())
            or "No files assessed",
        },
        "summary": sev_counts,
    }
    if timed_out:
        result["timed_out"] = True
    return result


# --- formatting / truncation ------------------------------------------------


def format_prescan_context(prescan_json: dict[str, Any]) -> str:
    """Format prescan results into a text context packet for explorer prompts."""
    if not prescan_json or prescan_json.get("file_count", 0) == 0:
        return ""
    analyzer = prescan_json.get("analyzer", "regex-only")
    parts = [f"## Prescan Signals (fast static checks, {analyzer} mode)"]
    summary = prescan_json.get("summary", {})
    patterns = prescan_json.get("patterns", {})
    for sev in ("critical", "high", "medium", "low"):
        count = summary.get(sev, 0)
        if not count:
            continue
        parts.append(f"\n{sev.upper()}: {count} signal(s)")
        for pdata in patterns.values():
            if pdata.get("severity") != sev:
                continue
            for f in pdata.get("findings", [])[:5]:
                parts.append(
                    f"- {f['file']}:{f['line']} -- {f['pattern_id']}: {f['description']}"
                )
    comp = prescan_json.get("implementation_completeness", {}).get("summary", "")
    if comp:
        parts.append(f"\nImplementation completeness: {comp}")
    return "\n".join(parts)


def truncate_prescan_critical_only(text: str) -> str:
    """Drop medium/low prescan signals, keep critical and high only."""
    if not text:
        return ""
    kept: list[str] = []
    in_section = False
    for line in text.splitlines():
        upper = line.strip().upper()
        if upper.startswith("CRITICAL:") or upper.startswith("HIGH:"):
            in_section = True
            kept.append(line)
        elif upper.startswith("MEDIUM:") or upper.startswith("LOW:"):
            in_section = False
        elif upper.startswith("## PRESCAN") or upper.startswith(
            "IMPLEMENTATION COMPLETENESS:"
        ):
            kept.append(line)
            in_section = False
        elif in_section and line.strip().startswith("-"):
            kept.append(line)
        elif not line.strip() and kept and kept[-1].strip():
            kept.append(line)
    return "\n".join(kept).rstrip()


# --- CLI --------------------------------------------------------------------


def main() -> None:
    if sys.stdin.isatty():
        print(
            "Usage: echo 'file1.py\\nfile2.go' | python3 scripts/prescan.py",
            file=sys.stderr,
        )
        sys.exit(1)
    raw = sys.stdin.read().strip()
    file_paths = [f.strip() for f in raw.splitlines() if f.strip()] if raw else []
    result = run_prescan(file_paths)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
