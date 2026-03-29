#!/usr/bin/env python3
"""Cross-file context planner -- generates search queries for related code.

Reads JSON from stdin, optionally calls an LLM for intelligent query planning,
falls back to deterministic query generation from function data.
Executes queries via grep. Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

VALID_CATEGORIES = {"symmetric", "consumers", "test_impl", "configuration", "upstream"}
MAX_QUERIES = 10
MAX_RESULTS_PER_QUERY = 5
TOKEN_BUDGET_CHARS = 5_000 * 4  # ~5k tokens in chars


def main() -> None:
    input_data = json.load(sys.stdin)
    diff_summary = input_data.get("diff_summary", "")
    graph_data = input_data.get("graph_data")
    functions_data = input_data.get("functions_data")
    model = input_data.get("model", "haiku")
    prompt_path = input_data.get("prompt_path", "")

    # Try LLM planning
    queries = _try_llm_planning(diff_summary, graph_data, model, prompt_path)
    llm_used = queries is not None

    # Fallback to deterministic if LLM failed
    if queries is None:
        queries = _deterministic_queries(functions_data, graph_data)

    # Execute queries via grep
    results = _execute_queries(queries)

    # Budget enforcement
    results = _enforce_budget(results, queries)

    # Format output
    output = _format_output(queries, results, llm_used=llm_used)
    json.dump(output, sys.stdout, indent=2)


def _try_llm_planning(
    diff_summary: str,
    graph_data: dict[str, Any] | None,
    model: str,
    prompt_path: str,
) -> list[dict[str, Any]] | None:
    """Try to call LLM for query planning. Returns None on failure."""
    if not prompt_path or not Path(prompt_path).exists():
        return None
    try:
        prompt_template = Path(prompt_path).read_text(encoding="utf-8")

        context_parts = [prompt_template, "\n\n## Diff Summary\n", diff_summary]
        if graph_data:
            context_parts.append("\n\n## Dependency Graph\n")
            context_parts.append(json.dumps(graph_data, indent=2))

        full_prompt = "".join(context_parts)

        # Attempt to use the Anthropic SDK
        import anthropic  # noqa: F811

        client = anthropic.Anthropic()
        model_map = {
            "haiku": "claude-haiku-4-20250414",
            "sonnet": "claude-sonnet-4-20250514",
        }
        api_model = model_map.get(model, model)

        response = client.messages.create(
            model=api_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": full_prompt}],
        )

        # Extract text content from response
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # Parse JSON from response -- look for array or object with "queries" key
        json_match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if not json_match:
            return None

        raw_queries = json.loads(json_match.group())
        # Accept both {"queries": [...]} and bare [...]
        if isinstance(raw_queries, dict):
            raw_queries = raw_queries.get("queries", [])
        if not isinstance(raw_queries, list):
            return None

        # Validate and normalize each query
        queries: list[dict[str, Any]] = []
        for q in raw_queries[:MAX_QUERIES]:
            if not isinstance(q, dict) or "pattern" not in q:
                continue
            category = q.get("category", "consumers")
            if category not in VALID_CATEGORIES:
                category = "consumers"
            queries.append(
                {
                    "pattern": str(q["pattern"]),
                    "rationale": str(q.get("rationale", "")),
                    "risk_level": str(q.get("risk_level", "medium")),
                    "category": category,
                    "symbol_name": str(q.get("symbol_name", "")),
                    "file_glob": q.get("file_glob"),
                }
            )
        return queries if queries else None

    except Exception:
        return None


def _deterministic_queries(
    functions_data: dict[str, Any] | None,
    graph_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Generate search queries from function data without LLM."""
    queries: list[dict[str, Any]] = []
    if not functions_data:
        return queries

    funcs = functions_data.get("functions", [])
    for fn in funcs[:MAX_QUERIES]:
        name = fn.get("name", "")
        file_path = fn.get("file", "")
        if not name or len(name) < 3:
            continue

        # Query: callers of this function
        queries.append(
            {
                "pattern": rf"\b{re.escape(name)}\s*\(",
                "rationale": f"callers of {name}() changed in {file_path}",
                "risk_level": "high",
                "category": "consumers",
                "symbol_name": name,
                "file_glob": None,
            }
        )

    # If we have room and graph data, add upstream queries
    if graph_data and len(queries) < MAX_QUERIES:
        imports = graph_data.get("imports", {})
        for src_file, targets in list(imports.items())[: MAX_QUERIES - len(queries)]:
            if isinstance(targets, list):
                for target in targets[:1]:  # one query per source file
                    if isinstance(target, str) and len(target) > 2:
                        queries.append(
                            {
                                "pattern": re.escape(target),
                                "rationale": f"upstream dependency {target} imported by {src_file}",
                                "risk_level": "medium",
                                "category": "upstream",
                                "symbol_name": target,
                                "file_glob": None,
                            }
                        )

    return queries[:MAX_QUERIES]


_GREP_EXCLUDE_DIRS = [
    ".git",
    ".eval",
    ".agents",
    ".tickets",
    ".ruff_cache",
    ".idea",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "vendor",
    "coverage",
]


def _execute_queries(queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute grep queries and collect results."""
    results: dict[str, Any] = {}
    exclude_args: list[str] = []
    for d in _GREP_EXCLUDE_DIRS:
        exclude_args.extend(["--exclude-dir", d])

    for i, q in enumerate(queries):
        pattern = q["pattern"]
        try:
            cmd = ["grep", "-rnl", "-E"] + exclude_args
            if q.get("file_glob"):
                cmd.extend(["--include", q["file_glob"]])
            cmd.extend([pattern, "."])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=".",
                check=False,
            )
            matches = [
                line.lstrip("./")
                for line in proc.stdout.strip().split("\n")
                if line.strip()
            ][:MAX_RESULTS_PER_QUERY]
            if matches:
                results[str(i)] = {
                    "query": q,
                    "matches": matches,
                }
        except (subprocess.TimeoutExpired, Exception):
            continue
    return results


def _enforce_budget(
    results: dict[str, Any], queries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Drop low-risk results if total exceeds token budget."""
    total_chars = sum(len(json.dumps(v)) for v in results.values())
    if total_chars <= TOKEN_BUDGET_CHARS:
        return results

    # Sort by risk_level (drop low first, then medium)
    risk_order = {"low": 0, "medium": 1, "high": 2}
    sorted_keys = sorted(
        results.keys(),
        key=lambda k: risk_order.get(results[k]["query"].get("risk_level", "low"), 0),
    )

    for key in sorted_keys:
        if total_chars <= TOKEN_BUDGET_CHARS:
            break
        total_chars -= len(json.dumps(results[key]))
        del results[key]

    return results


def _format_output(
    queries: list[dict[str, Any]],
    results: dict[str, Any],
    *,
    llm_used: bool = False,
) -> dict[str, Any]:
    """Format final output with sections tagged by category."""
    sections: list[dict[str, Any]] = []
    for _idx_str, data in results.items():
        q = data["query"]
        category = q.get("category", "consumers")
        if category not in VALID_CATEGORIES:
            category = "consumers"
        sections.append(
            {
                "category": category,
                "header": f"{q.get('symbol_name', '?')} — {q.get('rationale', '')}",
                "matches": data["matches"],
                "risk_level": q.get("risk_level", "medium"),
            }
        )

    return {
        "sections": sections,
        "stats": {
            "queries_planned": len(queries),
            "queries_executed": len(results),
            "total_matches": sum(len(s["matches"]) for s in sections),
            "llm_used": llm_used,
        },
    }


if __name__ == "__main__":
    main()
