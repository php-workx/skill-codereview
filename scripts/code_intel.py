#!/usr/bin/env python3
"""Shared code intelligence module -- language-agnostic structural analysis.

Subcommands: complexity, functions, imports, exports, callers, patterns, setup.
All read CHANGED_FILES from stdin (newline-delimited), output JSON to stdout.
Tree-sitter is optional; all subcommands fall back to regex when unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
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
        return {
            "dependencies": deps_list,
            "summary": {
                "installed": sum(1 for d in deps_list if d["installed"]),
                "total": len(deps_list),
                "missing_by_tier": {
                    "minimal": missing_minimal,
                    "full": missing_full + missing_minimal,
                },
            },
        }
    if getattr(args, "install", False):
        tier = getattr(args, "tier", "minimal")
        commands = []
        for d in _DEPS:
            if d["check"]() or (tier == "minimal" and d["tier"] != "minimal"):
                continue
            prefix = "pip install" if d["installer"] == "pip" else "go install"
            commands.append({"command": f"{prefix} {d['package']}", "name": d["name"]})
        return {"install_commands": commands, "tier": tier}
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


def cmd_graph(
    files: list[str], depth: int = 1, semantic: bool = False
) -> dict[str, Any]:
    """Build a dependency graph from changed files."""
    if semantic:
        print("info: semantic search not available", file=sys.stderr)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for fpath in files:
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
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "files_traversed": len(files),
            "depth": depth,
        },
    }


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


def cmd_format_diff(diff_text: str, expand_context: bool = False) -> str:
    """Transform a unified diff into LLM-optimized before/after blocks."""
    if not diff_text.strip():
        return ""
    if expand_context:
        print("info: --expand-context not yet implemented", file=sys.stderr)

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
    fp = sub.add_parser("format-diff")
    fp.add_argument("--expand-context", action="store_true")
    sp = sub.add_parser("setup")
    sp.add_argument("--check", action="store_true")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--tier", choices=["full", "minimal"], default="minimal")
    sp.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    if args.command == "setup":
        result = cmd_setup(args)
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
            "graph": lambda: cmd_graph(files, depth=args.depth, semantic=args.semantic),
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
