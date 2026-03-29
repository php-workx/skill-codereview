#!/usr/bin/env python3
"""Shared code intelligence module -- language-agnostic structural analysis.

Subcommands: complexity, functions, imports, exports, callers, patterns, setup.
All read CHANGED_FILES from stdin (newline-delimited), output JSON to stdout.
Tree-sitter is optional; all subcommands fall back to regex when unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_has_treesitter = False
try:
    import tree_sitter  # type: ignore[import-untyped]  # noqa: F401

    _has_treesitter = True
except ImportError:
    pass

# --- Language configuration ---
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

_FUNC_RE: dict[str, re.Pattern[str]] = {
    "python": re.compile(
        r"^[ \t]*(?:async\s+)?def\s+(?P<name>\w+)\s*\((?P<params>[^)]*)\)"
        r"(?:\s*->\s*(?P<returns>[^:]+))?\s*:",
        re.MULTILINE,
    ),
    "go": re.compile(
        r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(?P<name>\w+)\s*\((?P<params>[^)]*)\)"
        r"(?:\s*(?:\((?P<returns>[^)]+)\)|(?P<ret_single>[\w.*\[\]]+)))?\s*\{",
        re.MULTILINE,
    ),
    "typescript": re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)"
        r"\s*(?:<[^>]+>\s*)?\((?P<params>[^)]*)\)(?:\s*:\s*(?P<returns>[^{]+))?\s*\{",
        re.MULTILINE,
    ),
    "javascript": re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)"
        r"\s*\((?P<params>[^)]*)\)(?:\s*:\s*(?P<returns>[^{]+))?\s*\{",
        re.MULTILINE,
    ),
    "java": re.compile(
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?"
        r"(?P<returns>[\w<>\[\]?,\s]+)\s+(?P<name>\w+)"
        r"\s*\((?P<params>[^)]*)\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
        re.MULTILINE,
    ),
    "rust": re.compile(
        r"^\s*(?:pub(?:\s*\(crate\))?\s+)?(?:async\s+)?fn\s+(?P<name>\w+)"
        r"\s*(?:<[^>]+>\s*)?\((?P<params>[^)]*)\)(?:\s*->\s*(?P<returns>[^{]+))?\s*\{",
        re.MULTILINE,
    ),
}

_IMPORT_RE: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"^\s*import\s+(?P<module>[\w.]+)(?:\s+as\s+\w+)?", re.MULTILINE),
        re.compile(
            r"^\s*from\s+(?P<module>[\w.]+)\s+import\s+(?P<names>[^#\n]+)", re.MULTILINE
        ),
    ],
    "go": [re.compile(r'^\s*"(?P<module>[^"]+)"', re.MULTILINE)],
    "typescript": [
        re.compile(
            r"""^\s*import\s+(?:(?:\{(?P<names>[^}]+)\}|(?P<default>\w+)|\*\s+as\s+(?P<star>\w+))\s+from\s+)?['"](?P<module>[^'"]+)['"]""",
            re.MULTILINE,
        )
    ],
    "javascript": [
        re.compile(
            r"""^\s*(?:import\s+(?:(?:\{(?P<names>[^}]+)\}|(?P<default>\w+)|\*\s+as\s+(?P<star>\w+))\s+from\s+)?['"](?P<module>[^'"]+)['"]|const\s+(?:\{[^}]+\}|\w+)\s*=\s*require\(['"](?P<req_module>[^'"]+)['"]\))""",
            re.MULTILINE,
        )
    ],
    "java": [
        re.compile(r"^\s*import\s+(?:static\s+)?(?P<module>[\w.]+);", re.MULTILINE)
    ],
    "rust": [
        re.compile(
            r"^\s*use\s+(?P<module>[\w:]+)(?:::\{(?P<names>[^}]+)\})?;", re.MULTILINE
        )
    ],
}

_BRANCH_RE: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"\b(if|elif|for|while|and|or|except|case)\b"),
    "go": re.compile(r"\b(if|else\s+if|for|switch|case|select|\|\||&&)\b"),
    "typescript": re.compile(
        r"\b(if|else\s+if|for|while|switch|case|catch|\|\||&&|\?)\b"
    ),
    "javascript": re.compile(
        r"\b(if|else\s+if|for|while|switch|case|catch|\|\||&&|\?)\b"
    ),
    "java": re.compile(r"\b(if|else\s+if|for|while|switch|case|catch|\|\||&&|\?)\b"),
    "rust": re.compile(r"\b(if|else\s+if|for|while|match|=>|\|\||&&)\b"),
}

_EXPORT_RE: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "python": [
        (re.compile(r"^(?:async\s+)?def\s+(?P<name>\w+)", re.MULTILINE), "function"),
        (re.compile(r"^class\s+(?P<name>\w+)", re.MULTILINE), "class"),
        (re.compile(r"^(?P<name>[A-Z_][A-Z0-9_]*)\s*=", re.MULTILINE), "constant"),
    ],
    "go": [
        (
            re.compile(r"^func\s+(?:\([^)]+\)\s+)?(?P<name>[A-Z]\w*)", re.MULTILINE),
            "function",
        ),
        (re.compile(r"^type\s+(?P<name>[A-Z]\w*)", re.MULTILINE), "type"),
        (re.compile(r"^var\s+(?P<name>[A-Z]\w*)", re.MULTILINE), "variable"),
    ],
    "typescript": [
        (
            re.compile(
                r"^export\s+(?:async\s+)?function\s+(?P<name>\w+)", re.MULTILINE
            ),
            "function",
        ),
        (re.compile(r"^export\s+class\s+(?P<name>\w+)", re.MULTILINE), "class"),
        (
            re.compile(r"^export\s+(?:const|let|var)\s+(?P<name>\w+)", re.MULTILINE),
            "variable",
        ),
        (
            re.compile(r"^export\s+(?:type|interface)\s+(?P<name>\w+)", re.MULTILINE),
            "type",
        ),
        (
            re.compile(r"^export\s+default\s+function\s+(?P<name>\w+)", re.MULTILINE),
            "function",
        ),
        (
            re.compile(r"^export\s+default\s+class\s+(?P<name>\w+)", re.MULTILINE),
            "class",
        ),
    ],
    "javascript": [
        (
            re.compile(
                r"^export\s+(?:async\s+)?function\s+(?P<name>\w+)", re.MULTILINE
            ),
            "function",
        ),
        (re.compile(r"^export\s+class\s+(?P<name>\w+)", re.MULTILINE), "class"),
        (
            re.compile(r"^export\s+(?:const|let|var)\s+(?P<name>\w+)", re.MULTILINE),
            "variable",
        ),
        (
            re.compile(r"^export\s+default\s+function\s+(?P<name>\w+)", re.MULTILINE),
            "function",
        ),
    ],
    "rust": [
        (
            re.compile(
                r"^\s*pub(?:\s*\(crate\))?\s+(?:async\s+)?fn\s+(?P<name>\w+)",
                re.MULTILINE,
            ),
            "function",
        ),
        (
            re.compile(
                r"^\s*pub(?:\s*\(crate\))?\s+struct\s+(?P<name>\w+)", re.MULTILINE
            ),
            "struct",
        ),
        (
            re.compile(
                r"^\s*pub(?:\s*\(crate\))?\s+enum\s+(?P<name>\w+)", re.MULTILINE
            ),
            "enum",
        ),
        (
            re.compile(
                r"^\s*pub(?:\s*\(crate\))?\s+trait\s+(?P<name>\w+)", re.MULTILINE
            ),
            "trait",
        ),
    ],
    "java": [
        (
            re.compile(
                r"^\s*public\s+(?:static\s+)?(?:final\s+)?class\s+(?P<name>\w+)",
                re.MULTILINE,
            ),
            "class",
        ),
        (
            re.compile(
                r"^\s*public\s+(?:static\s+)?(?:final\s+)?interface\s+(?P<name>\w+)",
                re.MULTILINE,
            ),
            "interface",
        ),
        (
            re.compile(
                r"^\s*public\s+(?:static\s+)?(?:final\s+)?enum\s+(?P<name>\w+)",
                re.MULTILINE,
            ),
            "enum",
        ),
    ],
}

_PATTERN_DEFS: list[dict[str, Any]] = [
    {
        "name": "sql-injection",
        "severity": "high",
        "treesitter_only": False,
        "regex": re.compile(
            r"""(?:execute|query|raw|cursor\.execute)\s*\([^)]*(?:\+|f['"])""",
            re.IGNORECASE,
        ),
        "summary": "String concatenation/f-string in SQL execution call",
    },
    {
        "name": "command-injection",
        "severity": "high",
        "treesitter_only": False,
        "regex": re.compile(
            r"""(?:exec|system|popen|subprocess\.(?:run|call|Popen)|os\.(?:system|popen))\s*\([^)]*(?:\+|f['"])""",
            re.IGNORECASE,
        ),
        "summary": "String concatenation/f-string in command execution call",
    },
    {
        "name": "empty-error-handler",
        "severity": "medium",
        "treesitter_only": False,
        "regex": re.compile(
            r"(?:except\s+[^:]*:\s*\n\s+pass\b)|(?:catch\s*\([^)]*\)\s*\{\s*\})",
            re.MULTILINE,
        ),
        "summary": "Error handler with empty body (swallows exceptions)",
    },
    {
        "name": "unused-import",
        "severity": "low",
        "treesitter_only": True,
        "regex": None,
        "summary": "Imported symbol not referenced in file",
    },
    {
        "name": "unreachable-code",
        "severity": "low",
        "treesitter_only": True,
        "regex": None,
        "summary": "Code after return/raise/throw statement",
    },
    {
        "name": "resource-leak",
        "severity": "medium",
        "treesitter_only": True,
        "regex": None,
        "summary": "open()/connect() without matching close()",
    },
]


# --- Data classes ---
@dataclass
class FunctionInfo:
    file: str
    name: str
    params: list[str]
    returns: str
    line_start: int
    line_end: int
    exported: bool
    language: str


@dataclass
class ImportInfo:
    file: str
    module: str
    names: list[str]
    line: int


@dataclass
class ExportInfo:
    file: str
    name: str
    kind: str
    line: int


@dataclass
class CallSite:
    file: str
    caller: str
    line: int
    context: str


@dataclass
class CCResult:
    file: str
    function: str
    score: int
    rating: str
    line: int = 0


@dataclass
class PatternMatch:
    pattern: str
    severity: str
    file: str
    line: int
    summary: str
    evidence: str


# --- Helpers ---

# Stable internal API — imported by prescan.py
# Do not rename without updating prescan.py imports


def _detect_language(file_path: str) -> str | None:
    """Map file extension to language name."""
    return EXTENSION_MAP.get(Path(file_path).suffix.lower())


def _read_file_safe(file_path: str) -> str | None:
    """Read file content, returning None on failure."""
    try:
        path = Path(file_path)
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _score_to_rating(score: int) -> str:
    if score <= 5:
        return "A"
    if score <= 10:
        return "B"
    if score <= 20:
        return "C"
    if score <= 30:
        return "D"
    return "F"


def _is_exported(name: str, language: str) -> bool:
    """Determine if a symbol is part of the public API."""
    if language == "python":
        return not name.startswith("_")
    if language == "go":
        return name[0:1].isupper() if name else False
    return True  # TS/JS/Rust/Java handled at call site


def _read_stdin_files() -> list[str]:
    if sys.stdin.isatty():
        return []
    raw = sys.stdin.read().strip()
    return [f for f in raw.splitlines() if f.strip()] if raw else []


def _line_indent(lines: list[str], line_idx: int) -> int:
    if 0 <= line_idx < len(lines):
        line = lines[line_idx]
        return len(line) - len(line.lstrip())
    return 0


def _find_function_end(lines: list[str], start: int, indent: int, language: str) -> int:
    """Estimate end line of a function via indentation or brace counting."""
    if language == "python":
        for i in range(start + 1, len(lines)):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if len(lines[i]) - len(
                lines[i].lstrip()
            ) <= indent and not stripped.startswith("#"):
                return i
        return len(lines)
    depth = 0
    found_open = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return i + 1
    return len(lines)


# --- Subcommand: complexity ---


def _complexity_regex(files: list[str]) -> tuple[list[CCResult], str]:
    results: list[CCResult] = []
    for fpath in files:
        lang = _detect_language(fpath)
        if not lang or lang not in _FUNC_RE or lang not in _BRANCH_RE:
            continue
        content = _read_file_safe(fpath)
        if content is None:
            continue
        lines = content.splitlines()
        for m in _FUNC_RE[lang].finditer(content):
            line_start = content[: m.start()].count("\n") + 1
            indent = _line_indent(lines, line_start - 1)
            line_end = _find_function_end(lines, line_start - 1, indent, lang)
            func_body = "\n".join(lines[line_start - 1 : line_end])
            score = 1 + len(_BRANCH_RE[lang].findall(func_body))
            if score >= 6:
                results.append(
                    CCResult(
                        fpath,
                        m.group("name"),
                        score,
                        _score_to_rating(score),
                        line_start,
                    )
                )
    return results, "regex-only"


def _complexity_external(files: list[str]) -> tuple[list[CCResult], dict[str, str]]:
    results: list[CCResult] = []
    tool_status: dict[str, str] = {}
    py_files = [f for f in files if f.endswith(".py")]
    go_files = [f for f in files if f.endswith(".go")]
    if py_files and shutil.which("radon"):
        try:
            proc = subprocess.run(
                ["radon", "cc", "-s", "-n", "C"] + py_files,
                capture_output=True,
                text=True,
                timeout=30,
            )
            tool_status["radon"] = "ran"
            current_file = ""
            for line in proc.stdout.splitlines():
                if line.strip().endswith(".py"):
                    current_file = line.strip()
                    continue
                m = re.match(
                    r"\s*[FMCG]\s+(\d+):(\d+)\s+(.+)\s+-\s+([A-F])\s+\((\d+)\)", line
                )
                if m:
                    results.append(
                        CCResult(
                            current_file,
                            m.group(3).strip(),
                            int(m.group(5)),
                            m.group(4),
                            int(m.group(1)),
                        )
                    )
        except (subprocess.TimeoutExpired, OSError):
            tool_status["radon"] = "error"
    elif py_files:
        tool_status["radon"] = "not_installed"
    if go_files and shutil.which("gocyclo"):
        try:
            proc = subprocess.run(
                ["gocyclo", "-over", "10"] + go_files,
                capture_output=True,
                text=True,
                timeout=30,
            )
            tool_status["gocyclo"] = "ran"
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    score = int(parts[0])
                    results.append(
                        CCResult(
                            parts[3].split(":")[0],
                            f"{parts[1]}.{parts[2]}",
                            score,
                            _score_to_rating(score),
                        )
                    )
        except (subprocess.TimeoutExpired, OSError, ValueError):
            tool_status["gocyclo"] = "error"
    elif go_files:
        tool_status["gocyclo"] = "not_installed"
    return results, tool_status


def cmd_complexity(files: list[str]) -> dict[str, Any]:
    regex_results, _ = _complexity_regex(files)
    ext_results, tool_status = _complexity_external(files)
    ext_covered = {(r.file, r.function) for r in ext_results}
    merged = list(ext_results) + [
        r for r in regex_results if (r.file, r.function) not in ext_covered
    ]
    analyzer = "regex-only"
    if any(v == "ran" for v in tool_status.values()):
        analyzer = (
            "mixed"
            if regex_results
            else next(k for k, v in tool_status.items() if v == "ran")
        )
    return {
        "analyzer": analyzer,
        "hotspots": [asdict(r) for r in merged],
        "tool_status": {
            "tree_sitter": "installed_unused" if _has_treesitter else "not_installed",
            **tool_status,
        },
    }


# --- Subcommand: functions ---


def _extract_functions(fpath: str, content: str, lang: str) -> list[FunctionInfo]:
    results: list[FunctionInfo] = []
    func_re = _FUNC_RE.get(lang)
    if func_re is None:
        return results
    lines = content.splitlines()
    for m in func_re.finditer(content):
        name = m.group("name")
        raw_params = m.group("params").strip() if m.group("params") else ""
        params = (
            [
                p.strip().split(":")[0].split(" ")[0]
                for p in raw_params.split(",")
                if p.strip()
            ]
            if raw_params
            else []
        )
        returns = ""
        try:
            returns = (m.group("returns") or "").strip()
        except IndexError:
            pass
        if lang == "go" and not returns:
            try:
                returns = (m.group("ret_single") or "").strip()
            except IndexError:
                pass
        line_start = content[: m.start()].count("\n") + 1
        indent = _line_indent(lines, line_start - 1)
        line_end = _find_function_end(lines, line_start - 1, indent, lang)
        exported = _is_exported(name, lang)
        if lang in ("typescript", "javascript"):
            func_line = lines[line_start - 1] if line_start <= len(lines) else ""
            exported = "export" in func_line
        elif lang == "rust":
            func_line = lines[line_start - 1] if line_start <= len(lines) else ""
            exported = func_line.lstrip().startswith("pub")
        elif lang == "java":
            func_line = lines[line_start - 1] if line_start <= len(lines) else ""
            exported = "public" in func_line
        results.append(
            FunctionInfo(
                fpath, name, params, returns, line_start, line_end, exported, lang
            )
        )
    return results


def cmd_functions(files: list[str]) -> dict[str, Any]:
    all_funcs: list[dict[str, Any]] = []
    for fpath in files:
        lang = _detect_language(fpath)
        if not lang:
            continue
        content = _read_file_safe(fpath)
        if content is None:
            continue
        all_funcs.extend(asdict(f) for f in _extract_functions(fpath, content, lang))
    return {"functions": all_funcs}


# --- Subcommand: imports ---


def _extract_imports(fpath: str, content: str, lang: str) -> list[ImportInfo]:
    results: list[ImportInfo] = []
    for pattern in _IMPORT_RE.get(lang, []):
        for m in pattern.finditer(content):
            module = ""
            try:
                module = m.group("module") or ""
            except IndexError:
                pass
            if not module:
                try:
                    module = m.group("req_module") or ""
                except IndexError:
                    pass
            names: list[str] = []
            try:
                raw_names = m.group("names") or ""
                if raw_names:
                    names = [
                        n.strip().split(" as ")[0].strip()
                        for n in raw_names.split(",")
                        if n.strip()
                    ]
            except IndexError:
                pass
            if not module:
                continue
            results.append(
                ImportInfo(fpath, module, names, content[: m.start()].count("\n") + 1)
            )
    return results


# Stable internal API — imported by prescan.py
# Do not rename without updating prescan.py imports
def cmd_imports(files: list[str]) -> dict[str, Any]:
    all_imports: list[dict[str, Any]] = []
    for fpath in files:
        lang = _detect_language(fpath)
        if not lang:
            continue
        content = _read_file_safe(fpath)
        if content is None:
            continue
        all_imports.extend(asdict(i) for i in _extract_imports(fpath, content, lang))
    return {"imports": all_imports}


# --- Subcommand: exports ---


# Stable internal API — imported by prescan.py
# Do not rename without updating prescan.py imports
def _extract_exports(fpath: str, content: str, lang: str) -> list[ExportInfo]:
    results: list[ExportInfo] = []
    for pattern, kind in _EXPORT_RE.get(lang, []):
        for m in pattern.finditer(content):
            name = m.group("name")
            if lang == "python" and name.startswith("_"):
                continue
            results.append(
                ExportInfo(fpath, name, kind, content[: m.start()].count("\n") + 1)
            )
    return results


def cmd_exports(files: list[str]) -> dict[str, Any]:
    all_exports: list[dict[str, Any]] = []
    for fpath in files:
        lang = _detect_language(fpath)
        if not lang:
            continue
        content = _read_file_safe(fpath)
        if content is None:
            continue
        all_exports.extend(asdict(e) for e in _extract_exports(fpath, content, lang))
    return {"exports": all_exports}


# --- Subcommand: callers ---


def cmd_callers(files: list[str], target: str) -> dict[str, Any]:
    call_sites: list[dict[str, Any]] = []
    call_re = re.compile(rf"\b{re.escape(target)}\s*\(")
    for fpath in files:
        content = _read_file_safe(fpath)
        if content is None:
            continue
        lang = _detect_language(fpath)
        lines = content.splitlines()
        func_ranges: list[tuple[str, int, int]] = []
        func_re = _FUNC_RE.get(lang or "") if lang else None
        if func_re:
            for m in func_re.finditer(content):
                ls = content[: m.start()].count("\n") + 1
                func_ranges.append(
                    (
                        m.group("name"),
                        ls,
                        _find_function_end(
                            lines, ls - 1, _line_indent(lines, ls - 1), lang or ""
                        ),
                    )
                )
        for i, line_text in enumerate(lines, 1):
            if not call_re.search(line_text):
                continue
            stripped = line_text.strip()
            if stripped.startswith(
                ("def ", "func ", "function ", "fn ", "async def ", "async function ")
            ):
                continue
            caller = "<module>"
            for fname, ls, le in func_ranges:
                if ls <= i <= le:
                    caller = fname
                    break
            call_sites.append(asdict(CallSite(fpath, caller, i, stripped)))
    return {"target": target, "call_sites": call_sites}


# --- Subcommand: patterns ---


def cmd_patterns(files: list[str]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for fpath in files:
        content = _read_file_safe(fpath)
        if content is None:
            continue
        lines = content.splitlines()
        for pdef in _PATTERN_DEFS:
            if pdef["treesitter_only"]:
                continue
            regex = pdef["regex"]
            if regex is None:
                continue
            for m in regex.finditer(content):
                line_num = content[: m.start()].count("\n") + 1
                findings.append(
                    {
                        **asdict(
                            PatternMatch(
                                pdef["name"],
                                pdef["severity"],
                                fpath,
                                line_num,
                                pdef["summary"],
                                lines[line_num - 1].strip()
                                if line_num <= len(lines)
                                else "",
                            )
                        ),
                        "source": "deterministic",
                        "confidence": 1.0,
                    }
                )
    return {
        "analyzer": "regex-only",  # tree-sitter parsing not yet implemented
        "findings": findings,
        "tool_status": {
            "tree_sitter": "installed_unused" if _has_treesitter else "not_installed"
        },
    }


# --- Subcommand: setup ---

_DEPS: list[dict[str, Any]] = [
    {
        "name": "tree-sitter",
        "installer": "pip",
        "package": "tree-sitter",
        "tier": "minimal",
        "check": lambda: shutil.which("python3") is not None and _has_treesitter,
    },
    {
        "name": "radon",
        "installer": "pip",
        "package": "radon",
        "tier": "full",
        "check": lambda: shutil.which("radon") is not None,
    },
    {
        "name": "gocyclo",
        "installer": "go",
        "package": "github.com/fzipp/gocyclo/cmd/gocyclo@latest",
        "tier": "full",
        "check": lambda: shutil.which("gocyclo") is not None,
    },
    {
        "name": "semgrep",
        "installer": "pip",
        "package": "semgrep",
        "tier": "full",
        "check": lambda: shutil.which("semgrep") is not None,
    },
]


def _detect_python_env() -> str:
    """Detect the current Python environment type."""
    if "pipx" in sys.prefix:
        return "pipx"
    if sys.prefix != sys.base_prefix:
        return "venv"
    if os.environ.get("CONDA_DEFAULT_ENV"):
        return "conda"
    return "system"


def cmd_setup(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "check", False):
        deps_list = [
            {
                "name": d["name"],
                "installed": d["check"](),
                "installer": d["installer"],
                "package": d["package"],
                "tier": d["tier"],
            }
            for d in _DEPS
        ]
        missing_minimal = sum(
            1 for d in deps_list if not d["installed"] and d["tier"] == "minimal"
        )
        missing_full = sum(
            1 for d in deps_list if not d["installed"] and d["tier"] == "full"
        )
        # Group missing deps by installer for actionable output
        missing_by_installer: dict[str, list[str]] = {}
        for d in deps_list:
            if not d["installed"]:
                inst = d["installer"]
                missing_by_installer.setdefault(inst, []).append(d["name"])
        return {
            "dependencies": deps_list,
            "python_env": _detect_python_env(),
            "summary": {
                "installed": sum(1 for d in deps_list if d["installed"]),
                "total": len(deps_list),
                "missing_by_tier": {
                    "minimal": missing_minimal,
                    "full": missing_full + missing_minimal,
                },
                "missing_by_installer": missing_by_installer,
            },
        }
    if getattr(args, "install", False):
        tier = getattr(args, "tier", "minimal")
        non_interactive = getattr(args, "non_interactive", False)
        python_env = _detect_python_env()
        use_user_flag = python_env == "system"

        results: list[dict[str, Any]] = []
        for d in _DEPS:
            if d["check"]() or (tier == "minimal" and d["tier"] != "minimal"):
                continue

            installer = d["installer"]
            package = d["package"]
            name = d["name"]

            # Build command
            if installer == "pip":
                cmd = ["pip", "install"]
                if use_user_flag:
                    cmd.append("--user")
                cmd.append(package)
            elif installer == "npm":
                cmd = ["npm", "install", "-g", package]
            elif installer == "go":
                cmd = ["go", "install", package]
            else:
                results.append(
                    {
                        "name": name,
                        "status": "skipped",
                        "reason": f"unknown installer: {installer}",
                    }
                )
                continue

            # Check if installer is available
            if not shutil.which(cmd[0]):
                results.append(
                    {
                        "name": name,
                        "status": "skipped",
                        "reason": f"{cmd[0]} not available",
                    }
                )
                continue

            # Execute
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    results.append(
                        {
                            "name": name,
                            "status": "installed",
                            "command": " ".join(cmd),
                        }
                    )
                else:
                    results.append(
                        {
                            "name": name,
                            "status": "failed",
                            "command": " ".join(cmd),
                            "error": proc.stderr[:200],
                        }
                    )
            except subprocess.TimeoutExpired:
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "command": " ".join(cmd),
                        "error": "timeout",
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "command": " ".join(cmd),
                        "error": str(e)[:200],
                    }
                )

        # Post-install verification
        verification = cmd_setup(argparse.Namespace(check=True, install=False))

        installed_count = sum(1 for r in results if r["status"] == "installed")
        failed_count = sum(1 for r in results if r["status"] == "failed")
        skipped_count = sum(1 for r in results if r["status"] == "skipped")

        summary: dict[str, Any] = {
            "results": results,
            "tier": tier,
            "python_env": python_env,
            "installed": installed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "verification": verification,
        }

        if non_interactive:
            summary["exit_code"] = 1 if failed_count > 0 else 0

        return summary
    return {"error": "Use --check or --install"}


# --- Functions summary (text table for PromptContext) ---


def format_functions_summary(functions_json: dict[str, Any]) -> str:
    funcs = functions_json.get("functions", [])
    if not funcs:
        return ""
    header = (
        "### Function Definitions (from code_intel)\n"
        "| File | Function | Params | Returns | Lines | Exported |\n"
        "|------|----------|--------|---------|-------|----------|\n"
    )
    rows = []
    for f in funcs[:50]:
        params = ", ".join(f.get("params", []))
        rows.append(
            f"| {f.get('file', '')} | {f.get('name', '')} | {params} "
            f"| {f.get('returns', '') or ''} | {f.get('line_start', '?')}-{f.get('line_end', '?')} "
            f"| {'yes' if f.get('exported') else 'no'} |"
        )
    return header + "\n".join(rows)


# --- Subcommand: graph ---

# Repo-wide reference search constants
COMMON_NAMES = frozenset(
    {
        "get",
        "set",
        "run",
        "main",
        "init",
        "new",
        "test",
        "setup",
        "close",
        "open",
        "start",
        "stop",
        "read",
        "write",
        "update",
        "delete",
        "create",
        "save",
        "load",
        "find",
        "check",
        "is",
        "has",
        "to",
        "from",
    }
)
MAX_RESULTS_PER_SYMBOL = 20
PER_GREP_TIMEOUT = 5
TOTAL_GRAPH_TIMEOUT = 30
MAX_GRAPH_NODES = 200
CO_CHANGE_MIN_FREQUENCY = 3
CO_CHANGE_MAX_EDGES = 50
CO_CHANGE_MAX_COMMITS = 100


def _detect_semantic_backend() -> str | None:
    """Detect available semantic search backend. Returns None if nothing available."""
    try:
        import model2vec  # noqa: F401

        return "model2vec"
    except Exception as exc:
        print(f"warning: semantic backend detection failed: {exc}", file=sys.stderr)
        return None


def _build_semantic_edges(
    nodes: list[dict],
    files: list[str],
    repo_root: str,
    cache_dir: str | None,
) -> tuple[list[dict], dict]:
    """Build semantic similarity edges. Returns (edges, stats)."""
    backend = _detect_semantic_backend()
    if backend is None:
        return [], {"enabled": False, "reason": "dependencies not available"}

    # Extract text representation for each function node
    function_texts: dict[str, str] = {}
    for node in nodes:
        if node["kind"] != "function":
            continue
        fpath = node["file"]
        content = _read_file_safe(fpath)
        if not content:
            continue
        lines = content.splitlines()
        line_start = node.get("line", 0)
        # Get function signature + first 3 lines of body
        func_lines = lines[max(0, line_start - 1) : line_start + 3]
        function_texts[node["id"]] = " ".join(func_lines)

    if len(function_texts) < 2:
        return [], {
            "enabled": True,
            "model": backend,
            "symbols_indexed": 0,
            "reason": "too few functions",
        }

    # Generate embeddings
    start_time = time.monotonic()
    try:
        if backend == "model2vec":
            import model2vec  # type: ignore[import-untyped]

            # TODO: pass cache_dir when model2vec API supports it
            model = model2vec.StaticModel.from_pretrained("minishlab/potion-base-8M")
            texts = list(function_texts.values())
            ids = list(function_texts.keys())
            embeddings = model.encode(texts)
        else:  # onnx-minilm
            return [], {"enabled": False, "reason": "onnx backend not yet implemented"}
    except Exception as exc:
        return [], {"enabled": False, "reason": str(exc)[:100]}

    index_time = int((time.monotonic() - start_time) * 1000)

    # Find top 5 similar for each changed function
    changed_ids = {n["id"] for n in nodes if n.get("modified_in_diff")}
    semantic_edges: list[dict] = []
    sem_start = time.monotonic()

    # Try numpy vectorized path (model2vec returns numpy arrays) (sc-znde)
    _use_numpy = False
    try:
        import numpy as np

        # Verify embeddings is a numpy array (model2vec returns ndarray)
        if hasattr(embeddings, "shape") and hasattr(embeddings, "__matmul__"):
            _use_numpy = True
    except ImportError:
        pass

    if _use_numpy:
        import numpy as np

        emb = np.asarray(embeddings, dtype=np.float64)
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1  # avoid division by zero
        normalized = emb / norms

        for i, src_id in enumerate(ids):
            if time.monotonic() - sem_start > 10:
                break  # time guard: bail if semantic takes > 10s
            if src_id not in changed_ids:
                continue
            # dot product of normalized vectors = cosine similarity
            sims = normalized @ normalized[i]
            sims[i] = -1.0  # exclude self
            # Get top 5 indices
            top_indices = np.argsort(sims)[-5:][::-1]
            for j in top_indices:
                score = float(sims[j])
                if score > 0.5:
                    semantic_edges.append(
                        {
                            "from": src_id,
                            "to": ids[j],
                            "type": "semantic_similarity",
                            "score": round(score, 3),
                        }
                    )
    else:
        # Fallback: pure Python math for non-numpy embeddings
        import math as _math

        def _cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = _math.sqrt(sum(x * x for x in a))
            norm_b = _math.sqrt(sum(x * x for x in b))
            denom = norm_a * norm_b
            return dot / denom if denom > 0 else 0.0

        for i, src_id in enumerate(ids):
            if time.monotonic() - sem_start > 10:
                break  # time guard: bail if semantic takes > 10s
            if src_id not in changed_ids:
                continue
            src_vec = embeddings[i]
            scores: list[tuple[str, float]] = []
            for j, tgt_id in enumerate(ids):
                if i == j:
                    continue
                tgt_vec = embeddings[j]
                sim = _cosine_sim(src_vec, tgt_vec)
                scores.append((tgt_id, sim))

            scores.sort(key=lambda x: x[1], reverse=True)
            for tgt_id, score in scores[:5]:
                if score > 0.5:  # minimum similarity threshold
                    semantic_edges.append(
                        {
                            "from": src_id,
                            "to": tgt_id,
                            "type": "semantic_similarity",
                            "score": round(score, 3),
                        }
                    )

    stats = {
        "enabled": True,
        "model": backend,
        "symbols_indexed": len(function_texts),
        "index_time_ms": index_time,
    }

    return semantic_edges, stats


def _graph_cache_path(cache_dir: str, repo_root: str) -> Path:
    """Return the cache file path for a given repo root."""
    repo_hash = hashlib.sha256(repo_root.encode()).hexdigest()[:16]
    return Path(cache_dir) / f"graph-{repo_hash}.json"


def _load_graph_cache(cache_path: Path) -> dict | None:
    """Load a graph cache file, returning None on miss or corruption."""
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_graph_cache(cache_path: Path, graph: dict, file_mtimes: dict) -> None:
    """Persist graph data and file modification times to the cache."""
    try:
        cache_data = {
            "graph": graph,
            "file_mtimes": file_mtimes,
            "version": 1,
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(cache_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cache_data, f, indent=2)
            os.replace(tmp_path, str(cache_path))
        except BaseException:
            # Clean up temp file on any write/rename failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        print(f"warning: failed to save graph cache: {exc}", file=sys.stderr)


def cmd_graph(
    files: list[str],
    depth: int = 1,
    semantic: bool = False,
    repo_root: str | None = None,
    cache_dir: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Build a dependency graph from changed files.

    Args:
        files: List of changed file paths.
        depth: Traversal depth (reserved for future use).
        semantic: Whether to use semantic search (not yet implemented).
        repo_root: Root directory for repo-wide grep. Defaults to cwd.
        cache_dir: Directory for incremental indexing cache. If None, no caching.
        embedding_model: Embedding model to use for semantic edges (model2vec or onnx).
            Accepted but not yet used — reserved for future model routing.
    """
    # --- Cache: collect current file modification times ---
    file_mtimes: dict[str, float] = {}
    for fpath in files:
        try:
            file_mtimes[fpath] = os.path.getmtime(fpath)
        except OSError:
            file_mtimes[fpath] = 0

    # --- Cache: load existing cache and determine stale files ---
    effective_root = repo_root or str(Path.cwd())
    cache_path: Path | None = None
    cached_data: dict | None = None
    cached_nodes_by_file: dict[str, list[dict[str, Any]]] = {}
    cached_edges_by_file: dict[str, list[dict[str, Any]]] = {}
    stale_files: set[str] = set(files)  # default: re-parse everything

    if cache_dir:
        cache_path = _graph_cache_path(cache_dir, effective_root)
        cached_data = _load_graph_cache(cache_path)
        if cached_data and cached_data.get("version") == 1:
            cached_mtimes = cached_data.get("file_mtimes", {})
            cached_graph = cached_data.get("graph", {})
            # Index cached nodes and edges by file
            for n in cached_graph.get("nodes", []):
                f = n.get("file", "")
                cached_nodes_by_file.setdefault(f, []).append(n)
            for e in cached_graph.get("edges", []):
                f = e.get("from", "")
                cached_edges_by_file.setdefault(f, []).append(e)
            # Only re-parse files whose mtime changed or are new
            stale_files = set()
            for fpath in files:
                if fpath not in cached_mtimes:
                    stale_files.add(fpath)
                elif file_mtimes.get(fpath, 0) != cached_mtimes.get(fpath, -1):
                    stale_files.add(fpath)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for fpath in files:
        if fpath not in stale_files:
            # Reuse cached data for this file
            nodes.extend(cached_nodes_by_file.get(fpath, []))
            edges.extend(cached_edges_by_file.get(fpath, []))
            continue

        lang = _detect_language(fpath)
        if not lang:
            continue
        content = _read_file_safe(fpath)
        if content is None:
            continue
        for f in _extract_functions(fpath, content, lang):
            nodes.append(
                {
                    "id": f"{fpath}::{f.name}",
                    "kind": "function",
                    "file": fpath,
                    "line": f.line_start,
                    "modified_in_diff": True,
                    "hop_distance": 0,
                }
            )
        for imp in _extract_imports(fpath, content, lang):
            edges.append(
                {
                    "from": fpath,
                    "to": imp.module,
                    "type": "imports",
                    "line": imp.line,
                }
            )

    # --- Step 2: Repo-wide reference search for changed symbols ---
    search_root = effective_root
    changed_functions = [n for n in nodes if n["kind"] == "function"]
    changed_files_set = {str(Path(f).resolve()) for f in files}
    graph_start = time.monotonic()
    caller_nodes: list[dict[str, Any]] = []
    caller_edges: list[dict[str, Any]] = []

    # O(1) dedup sets for node membership checks (sc-v1ad)
    seen_ids: set[str] = {n["id"] for n in nodes}
    seen_files: set[str] = {n["file"] for n in nodes}

    for func_node in changed_functions:
        if time.monotonic() - graph_start > TOTAL_GRAPH_TIMEOUT:
            break
        if len(nodes) + len(caller_nodes) >= MAX_GRAPH_NODES:
            break

        func_name = func_node["id"].split("::")[-1]
        func_file = func_node["file"]

        # Skip short or common names to avoid noisy results
        if len(func_name) < 3 or func_name.lower() in COMMON_NAMES:
            continue

        pattern = rf"\b{re.escape(func_name)}\s*\("
        grep_cmd = ["grep", "-rnl", "-E", pattern]
        # Limit search to known source extensions for performance
        for ext in EXTENSION_MAP:
            grep_cmd.append(f"--include=*{ext}")
        grep_cmd.extend(
            [
                "--exclude-dir=.git",
                "--exclude-dir=node_modules",
                "--exclude-dir=.venv",
                "--exclude-dir=__pycache__",
                "--exclude-dir=vendor",
                ".",
            ]
        )
        try:
            result = subprocess.run(
                grep_cmd,
                cwd=search_root,
                capture_output=True,
                text=True,
                timeout=PER_GREP_TIMEOUT,
            )
            # Resolve grep output paths relative to search_root
            raw_matches = [
                line.lstrip("./")
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
            # Normalize to absolute paths for consistent comparison
            func_file_abs = str(Path(func_file).resolve())
            matching_files = []
            for rel_path in raw_matches:
                abs_path = str((Path(search_root) / rel_path).resolve())
                if abs_path != func_file_abs:
                    matching_files.append(abs_path)
            matching_files = matching_files[:MAX_RESULTS_PER_SYMBOL]
        except (subprocess.TimeoutExpired, Exception):
            continue

        for caller_file in matching_files:
            if caller_file in changed_files_set:
                continue  # already in the graph from the first pass

            # Add caller node if not already present (O(1) set lookup)
            node_id = caller_file
            if node_id not in seen_ids and caller_file not in seen_files:
                caller_nodes.append(
                    {
                        "id": node_id,
                        "kind": "file",
                        "file": caller_file,
                        "line": 0,
                        "modified_in_diff": False,
                        "hop_distance": 1,
                    }
                )
                seen_ids.add(node_id)
                seen_files.add(caller_file)

            # Try to find the specific calling function in caller_file
            caller_content = _read_file_safe(caller_file)
            if caller_content:
                caller_lang = _detect_language(caller_file)
                if caller_lang:
                    caller_funcs = _extract_functions(
                        caller_file, caller_content, caller_lang
                    )
                    found_caller = False
                    for cf in caller_funcs:
                        func_body = "\n".join(
                            caller_content.split("\n")[cf.line_start - 1 : cf.line_end]
                        )
                        if re.search(pattern, func_body):
                            fn_node_id = f"{caller_file}::{cf.name}"
                            if fn_node_id not in seen_ids:
                                caller_nodes.append(
                                    {
                                        "id": fn_node_id,
                                        "kind": "function",
                                        "file": caller_file,
                                        "line": cf.line_start,
                                        "modified_in_diff": False,
                                        "hop_distance": 1,
                                    }
                                )
                                seen_ids.add(fn_node_id)
                            caller_edges.append(
                                {
                                    "from": fn_node_id,
                                    "to": func_node["id"],
                                    "type": "calls",
                                    "line": cf.line_start,
                                }
                            )
                            found_caller = True
                            break
                    if not found_caller:
                        # Couldn't identify specific function, add file-level edge
                        caller_edges.append(
                            {
                                "from": caller_file,
                                "to": func_node["id"],
                                "type": "calls",
                                "line": 0,
                            }
                        )
                else:
                    # Unknown language, add file-level edge
                    caller_edges.append(
                        {
                            "from": caller_file,
                            "to": func_node["id"],
                            "type": "calls",
                            "line": 0,
                        }
                    )

    nodes.extend(caller_nodes)
    edges.extend(caller_edges)

    # --- Step 3: Depth-2 traversal (second hop from caller files) ---
    depth2_nodes: list[dict[str, Any]] = []
    depth2_edges: list[dict[str, Any]] = []

    if depth >= 2:
        MAX_DEPTH2_NODES = 500  # higher cap for depth 2
        # Get all depth-1 caller files that have "calls" edges (not imports)
        depth1_callers: set[str] = set()
        for edge in caller_edges:
            if edge["type"] == "calls":
                from_id = edge["from"]
                # Extract the file from the node id
                file_part = from_id.split("::")[0] if "::" in from_id else from_id
                if file_part not in changed_files_set:
                    depth1_callers.add(file_part)

        # O(1) dedup set for depth-2 nodes, hoisted outside inner loop (sc-v1ad)
        depth2_seen_ids: set[str] = {n["id"] for n in nodes}

        for caller_file in depth1_callers:
            if time.monotonic() - graph_start > TOTAL_GRAPH_TIMEOUT:
                break
            if len(nodes) + len(depth2_nodes) >= MAX_DEPTH2_NODES:
                break

            # Read the caller file and extract its functions
            content = _read_file_safe(caller_file)
            if not content:
                continue
            lang = _detect_language(caller_file)
            if not lang:
                continue

            caller_funcs = _extract_functions(caller_file, content, lang)
            for cf in caller_funcs:
                # Search for references to this caller function
                if len(cf.name) < 3 or cf.name.lower() in COMMON_NAMES:
                    continue

                pattern = rf"\b{re.escape(cf.name)}\s*\("
                grep_cmd_d2 = ["grep", "-rnl", "-E", pattern]
                for ext in EXTENSION_MAP:
                    grep_cmd_d2.append(f"--include=*{ext}")
                grep_cmd_d2.extend(
                    [
                        "--exclude-dir=.git",
                        "--exclude-dir=node_modules",
                        "--exclude-dir=.venv",
                        "--exclude-dir=__pycache__",
                        "--exclude-dir=vendor",
                        ".",
                    ]
                )
                try:
                    grep_result = subprocess.run(
                        grep_cmd_d2,
                        capture_output=True,
                        text=True,
                        timeout=PER_GREP_TIMEOUT,
                        cwd=str(effective_root),
                    )
                    raw_matches = [
                        line.lstrip("./")
                        for line in grep_result.stdout.strip().split("\n")
                        if line.strip()
                    ]
                    # Normalize to absolute paths for consistent comparison
                    matching_files = []
                    for rel_path in raw_matches:
                        abs_path = str((Path(effective_root) / rel_path).resolve())
                        if (
                            abs_path != caller_file
                            and abs_path not in changed_files_set
                        ):
                            matching_files.append(abs_path)
                    matching_files = matching_files[:10]  # smaller cap for depth 2
                except (subprocess.TimeoutExpired, Exception):
                    continue

                for abs_mf in matching_files:
                    node_id = abs_mf
                    if node_id not in depth2_seen_ids:
                        depth2_nodes.append(
                            {
                                "id": node_id,
                                "kind": "file",
                                "file": abs_mf,
                                "line": 0,
                                "modified_in_diff": False,
                                "hop_distance": 2,
                            }
                        )
                        depth2_seen_ids.add(node_id)
                    depth2_edges.append(
                        {
                            "from": abs_mf,
                            "to": f"{caller_file}::{cf.name}",
                            "type": "calls",
                            "line": 0,
                        }
                    )

        nodes.extend(depth2_nodes)
        edges.extend(depth2_edges)

        if len(nodes) > MAX_GRAPH_NODES:
            print(
                f"warning: graph has {len(nodes)} nodes (exceeds {MAX_GRAPH_NODES})",
                file=sys.stderr,
            )

    # Step 4: Co-change frequency from git log
    co_change_edges: list[dict[str, Any]] = []

    for fpath in files:
        if time.monotonic() - graph_start > TOTAL_GRAPH_TIMEOUT:
            break
        try:
            # Get commits that touched this file in the last 6 months
            log_result = subprocess.run(
                [
                    "git",
                    "log",
                    "--oneline",
                    "--follow",
                    "--since=6 months ago",
                    "--format=%H",
                    "--",
                    fpath,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(effective_root),
            )
            if log_result.returncode != 0:
                continue

            commits = [
                c.strip() for c in log_result.stdout.strip().split("\n") if c.strip()
            ]
            commits = commits[:CO_CHANGE_MAX_COMMITS]

            # For each commit, find other files changed in the same commit
            co_change_counts: dict[str, int] = {}
            for commit_sha in commits:
                if time.monotonic() - graph_start > TOTAL_GRAPH_TIMEOUT:
                    break
                try:
                    files_result = subprocess.run(
                        [
                            "git",
                            "diff-tree",
                            "--no-commit-id",
                            "--name-only",
                            "-r",
                            commit_sha,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=str(effective_root),
                    )
                    if files_result.returncode != 0:
                        continue
                    co_files = [
                        f.strip()
                        for f in files_result.stdout.strip().split("\n")
                        if f.strip() and f.strip() != fpath
                    ]
                    for cf in co_files:
                        co_change_counts[cf] = co_change_counts.get(cf, 0) + 1
                except (subprocess.TimeoutExpired, Exception):
                    continue

            # Only keep files with frequency >= threshold
            for co_file, freq in co_change_counts.items():
                if (
                    freq >= CO_CHANGE_MIN_FREQUENCY
                    and len(co_change_edges) < CO_CHANGE_MAX_EDGES
                ):
                    co_change_edges.append(
                        {
                            "from": fpath,
                            "to": co_file,
                            "type": "co_change",
                            "frequency": freq,
                        }
                    )
        except (subprocess.TimeoutExpired, Exception):
            continue

    edges.extend(co_change_edges)

    # Step 5: Semantic similarity edges (optional, graceful degradation)
    semantic_stats: dict[str, Any] = {}
    if semantic:
        semantic_edges, semantic_stats = _build_semantic_edges(
            nodes,
            files,
            str(effective_root),
            cache_dir,
        )
        edges.extend(semantic_edges)

    stats: dict[str, Any] = {
        "nodes": len(nodes),
        "edges": len(edges),
        "files_traversed": len(files),
        "depth": depth,
        "external_references": len(caller_nodes),
        "co_change_edges": len(co_change_edges),
    }
    if depth >= 2:
        stats["depth2_nodes"] = len(depth2_nodes)
    if semantic_stats:
        stats["semantic"] = semantic_stats

    result = {
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
    }

    # --- Cache: persist updated graph ---
    if cache_dir and cache_path is not None:
        _save_graph_cache(cache_path, result, file_mtimes)

    return result


def format_graph_summary(graph_json: dict[str, Any]) -> str:
    """Convert graph JSON to text summary for PromptContext."""
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])
    if not nodes and not edges:
        return ""
    parts: list[str] = []
    modified = [n for n in nodes if n.get("modified_in_diff")]
    if modified:
        symbols = ", ".join(
            f"{n['id'].split('::')[-1]} ({n['file']}:{n.get('line', '?')})"
            for n in modified[:20]
        )
        parts.append(f"Changed symbols: {symbols}")
    dep_lines: list[str] = []
    for e in edges[:30]:
        dep_lines.append(f"  {e['from']}:{e.get('line', '?')} — {e['type']} {e['to']}")
    if dep_lines:
        parts.append("Files that depend on changes:\n" + "\n".join(dep_lines))
    return "\n\n".join(parts)


# --- Subcommand: format-diff ---

_FUNCTION_KW_RE = re.compile(
    r"^\s*(?:(?:async\s+)?(?:def|function|func|fn)\s+\w+|"
    r"(?:pub(?:\s*\(crate\))?\s+)?(?:async\s+)?fn\s+\w+|"
    r"class\s+\w+)",
    re.MULTILINE,
)

_EXPAND_MAX_SCAN = 8  # max lines to scan upward for enclosing function
_EXPAND_AFTER_LINES = 3  # max lines of after-context past hunk end


def _find_enclosing_function(lines: list[str], hunk_start_line: int) -> int | None:
    """Find the line number of the enclosing function/class definition.

    Scans upward from hunk_start_line (1-indexed) looking for a function/class
    keyword. Returns the line number (1-indexed) or None if not found within
    max_scan lines.
    """
    start_idx = max(0, hunk_start_line - 1 - _EXPAND_MAX_SCAN)
    end_idx = hunk_start_line - 1  # convert to 0-indexed

    for idx in range(end_idx - 1, start_idx - 1, -1):
        if idx < 0 or idx >= len(lines):
            continue
        if _FUNCTION_KW_RE.match(lines[idx]):
            return idx + 1  # back to 1-indexed
    return None


def cmd_format_diff(diff_text: str, expand_context: bool = False) -> str:
    """Transform a unified diff into LLM-optimized before/after blocks."""
    if not diff_text.strip():
        return ""

    # Split into per-file sections
    file_sections = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
    output_parts: list[str] = []

    for section in file_sections:
        if not section.strip():
            continue
        # Extract filename from the diff header (b/path)
        header_match = re.match(r"a/\S+\s+b/(\S+)", section)
        if not header_match:
            continue
        file_path = header_match.group(1)
        output_parts.append(f"## File: {file_path}")

        # Pre-read source file if expand_context is requested
        source_lines: list[str] | None = None
        if expand_context:
            source_path = Path(file_path)
            # Boundary check: reject paths that escape the repo root
            try:
                resolved = (Path.cwd() / source_path).resolve()
                if not resolved.is_relative_to(Path.cwd().resolve()):
                    source_lines = None  # skip expansion for out-of-repo paths
                elif resolved.exists():
                    try:
                        source_lines = resolved.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                    except (OSError, UnicodeDecodeError):
                        source_lines = None
            except (OSError, ValueError):
                source_lines = None

        # Find all hunks
        hunk_re = re.compile(
            r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*?)$",
            re.MULTILINE,
        )
        hunk_matches = list(hunk_re.finditer(section))
        for i, hm in enumerate(hunk_matches):
            new_start = int(hm.group(2))
            func_ctx = hm.group(3).strip()
            # Determine hunk body (text between this @@ and the next @@ or end)
            body_start = hm.end()
            body_end = (
                hunk_matches[i + 1].start()
                if i + 1 < len(hunk_matches)
                else len(section)
            )
            body = section[body_start:body_end]
            # Remove trailing newline-only lines from body
            body = body.strip("\n")
            if not body:
                continue

            # Build header line
            hunk_header = (
                f"@@ {func_ctx} (line {new_start})"
                if func_ctx
                else f"@@ (line {new_start})"
            )
            output_parts.append(f"\n{hunk_header}")

            lines = body.split("\n")
            new_lines: list[str] = []  # context + additions
            old_lines: list[str] = []  # context + deletions
            line_num = new_start
            for raw_line in lines:
                if raw_line.startswith("+"):
                    new_lines.append(f"{line_num} +{raw_line[1:]}")
                    line_num += 1
                elif raw_line.startswith("-"):
                    old_lines.append(f"-{raw_line[1:]}")
                else:
                    # Context line (starts with space or is empty)
                    content = raw_line[1:] if raw_line.startswith(" ") else raw_line
                    new_lines.append(f"{line_num}  {content}")
                    old_lines.append(f" {content}")
                    line_num += 1

            # Expand context to enclosing function boundary if requested
            if expand_context and source_lines is not None:
                enclosing_line = _find_enclosing_function(source_lines, new_start)
                if enclosing_line is not None and enclosing_line < new_start:
                    # Prepend context from enclosing function to hunk start
                    context_before = source_lines[enclosing_line - 1 : new_start - 1]
                    if context_before:
                        output_parts.append("__context_before__")
                        for ci, cline in enumerate(context_before):
                            output_parts.append(f"{enclosing_line + ci}  {cline}")

                # After-context: expand up to _EXPAND_AFTER_LINES past hunk end
                hunk_end = new_start + sum(1 for ln in lines if not ln.startswith("-"))
                after_start = hunk_end - 1  # convert 1-based line to 0-based index
                after_end = min(after_start + _EXPAND_AFTER_LINES, len(source_lines))
                if after_start < len(source_lines):
                    context_after_lines = source_lines[after_start:after_end]
                    if context_after_lines:
                        output_parts.append("__context_after__")
                        for ci, cline in enumerate(context_after_lines):
                            output_parts.append(f"{after_start + 1 + ci}  {cline}")

            output_parts.append("__new hunk__")
            output_parts.extend(new_lines)
            output_parts.append("__old hunk__")
            output_parts.extend(old_lines)

    return "\n".join(output_parts)


# --- Main dispatch ---


def main() -> None:
    parser = argparse.ArgumentParser(description="Code intelligence module")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("complexity")
    sub.add_parser("functions")
    sub.add_parser("imports")
    sub.add_parser("exports")
    sub.add_parser("patterns")
    cp = sub.add_parser("callers")
    cp.add_argument("--target", required=True)
    gp = sub.add_parser("graph")
    gp.add_argument("--depth", type=int, default=1)
    gp.add_argument("--semantic", action="store_true")
    gp.add_argument("--repo-root", default=None, help="Root dir for repo-wide grep")
    gp.add_argument(
        "--cache",
        dest="cache_dir",
        default=None,
        help="Directory for incremental indexing cache",
    )
    gp.add_argument(
        "--embedding-model",
        choices=["model2vec", "onnx"],
        default=None,
    )
    fp = sub.add_parser("format-diff")
    fp.add_argument("--expand-context", action="store_true")
    sp = sub.add_parser("setup")
    sp.add_argument("--check", action="store_true")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--tier", choices=["full", "minimal"], default="minimal")
    sp.add_argument("--json", action="store_true", dest="json_output")
    sp.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    if args.command == "setup":
        result = cmd_setup(args)
        if isinstance(result, dict) and "exit_code" in result:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(result["exit_code"])
    elif args.command == "format-diff":
        diff_input = sys.stdin.read() if not sys.stdin.isatty() else ""
        formatted = cmd_format_diff(diff_input, expand_context=args.expand_context)
        sys.stdout.write(formatted)
        if formatted:
            sys.stdout.write("\n")
        return
    else:
        files = _read_stdin_files()
        dispatch = {
            "complexity": lambda: cmd_complexity(files),
            "functions": lambda: cmd_functions(files),
            "imports": lambda: cmd_imports(files),
            "exports": lambda: cmd_exports(files),
            "callers": lambda: cmd_callers(files, args.target),
            "patterns": lambda: cmd_patterns(files),
            "graph": lambda: cmd_graph(
                files,
                depth=args.depth,
                semantic=args.semantic,
                repo_root=getattr(args, "repo_root", None),
                cache_dir=getattr(args, "cache_dir", None),
                embedding_model=getattr(args, "embedding_model", None),
            ),
        }
        handler = dispatch.get(args.command)
        if not handler:
            parser.print_help()
            sys.exit(1)
        result = handler()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
