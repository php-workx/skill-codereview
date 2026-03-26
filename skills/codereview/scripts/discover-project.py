#!/usr/bin/env python3
"""
discover-project.py — Raw project scanner for codereview skill.

Discovers a project's build system, quality commands, and monorepo structure.
This is the "raw scanner" layer: it finds facts, does NOT interpret them
(an LLM agent does interpretation later).

Input:  CHANGED_FILES on stdin (newline-delimited)
Output: JSON project profile to stdout

Python 3 stdlib only.
"""

import json
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Project root marker files, in detection priority order.
# First match wins when walking up from a changed file.
PROJECT_MARKERS = [
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "Makefile",
    "Justfile",
    "Taskfile.yml",
    "go.work",
]

# Marker → language mapping
MARKER_LANGUAGE = {
    "package.json": "typescript",
    "go.mod": "go",
    "go.work": "go",
    "Cargo.toml": "rust",
    "pyproject.toml": "python",
    "setup.cfg": "python",
    "setup.py": "python",
    "Makefile": "unknown",
    "Justfile": "unknown",
    "Taskfile.yml": "unknown",
}

# Tool config file patterns to detect.
# Each entry is (glob-style pattern, tool type).
TOOL_CONFIG_PATTERNS = [
    (".eslintrc", "eslint"),
    (".eslintrc.js", "eslint"),
    (".eslintrc.cjs", "eslint"),
    (".eslintrc.json", "eslint"),
    (".eslintrc.yml", "eslint"),
    (".eslintrc.yaml", "eslint"),
    ("eslint.config.js", "eslint"),
    ("eslint.config.mjs", "eslint"),
    ("eslint.config.cjs", "eslint"),
    ("eslint.config.ts", "eslint"),
    ("eslint.config.mts", "eslint"),
    (".golangci.yml", "golangci-lint"),
    (".golangci.yaml", "golangci-lint"),
    ("clippy.toml", "clippy"),
    (".clippy.toml", "clippy"),
    ("ruff.toml", "ruff"),
    (".ruff.toml", "ruff"),
    ("tsconfig.json", "typescript"),
    (".prettierrc", "prettier"),
    (".prettierrc.json", "prettier"),
    (".prettierrc.yml", "prettier"),
    (".prettierrc.yaml", "prettier"),
    (".prettierrc.js", "prettier"),
    (".prettierrc.cjs", "prettier"),
    (".prettierrc.toml", "prettier"),
    ("prettier.config.js", "prettier"),
    ("prettier.config.cjs", "prettier"),
    ("mypy.ini", "mypy"),
    (".mypy.ini", "mypy"),
]

# Monorepo orchestrator files (checked at repo root and context roots)
MONOREPO_ORCHESTRATORS = {
    "turbo.json": "turborepo",
    "nx.json": "nx",
    "pnpm-workspace.yaml": "pnpm",
    "lerna.json": "lerna",
    # Cargo [workspace] and go.work are detected inline during build file parsing
}

# CI file patterns to check (relative to repo root)
CI_PATTERNS = [
    (".github/workflows", "github"),
    (".gitlab-ci.yml", "gitlab"),
    (".circleci/config.yml", "circleci"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_project_root(filepath: str, repo_root: str) -> tuple:
    """Walk up from filepath to find nearest project root marker.

    Returns (root_dir_relative, marker_file) or (".", None) if none found.
    The root is relative to repo_root.
    """
    abs_repo = os.path.abspath(repo_root)
    # Start from the directory containing the file
    current = os.path.dirname(os.path.abspath(os.path.join(repo_root, filepath)))

    while True:
        for marker in PROJECT_MARKERS:
            candidate = os.path.join(current, marker)
            if os.path.exists(candidate):
                rel = os.path.relpath(current, abs_repo)
                if rel == ".":
                    return (".", marker)
                return (rel, marker)

        # Don't walk above repo root
        if os.path.abspath(current) == abs_repo:
            break

        parent = os.path.dirname(current)
        if parent == current:
            # Reached filesystem root without finding repo root
            break
        current = parent

    return (".", None)


def detect_language(marker: str, root: str, repo_root: str) -> str:
    """Determine language from the project marker file.

    When the marker itself maps to 'unknown' (Makefile, Justfile, Taskfile),
    look for more specific markers in the same directory.
    """
    if marker is None:
        return "unknown"

    lang = MARKER_LANGUAGE.get(marker, "unknown")
    if lang != "unknown":
        return lang

    # For generic build systems, try to detect language from other files
    root_abs = os.path.join(repo_root, root) if root != "." else repo_root
    for specific_marker, specific_lang in MARKER_LANGUAGE.items():
        if specific_lang == "unknown":
            continue
        if os.path.exists(os.path.join(root_abs, specific_marker)):
            return specific_lang

    return "unknown"


def extract_makefile_targets(filepath: str) -> list:
    """Extract target names from a Makefile using regex."""
    targets = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Match lines like "target-name:" but not ".PHONY:" etc.
                m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:", line)
                if m:
                    targets.append(m.group(1))
    except (OSError, IOError):
        pass
    return targets


def extract_justfile_recipes(filepath: str) -> list:
    """Extract recipe names from a Justfile using regex."""
    recipes = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:", line)
                if m:
                    recipes.append(m.group(1))
    except (OSError, IOError):
        pass
    return recipes


def extract_package_json_scripts(filepath: str) -> list:
    """Extract script names from package.json."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        scripts = data.get("scripts", {})
        if isinstance(scripts, dict):
            return list(scripts.keys())
    except (OSError, IOError, json.JSONDecodeError, ValueError):
        pass
    return []


def extract_pyproject_tool_sections(filepath: str) -> list:
    """Extract [tool.*] section names from pyproject.toml using regex."""
    sections = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Match [tool.xxx] and [tool.xxx.yyy] etc.
        for m in re.finditer(r"^\[(tool\.[^\]]+)\]", content, re.MULTILINE):
            sections.append(m.group(1))
    except (OSError, IOError):
        pass
    return sections


def extract_pyproject_project_scripts(filepath: str) -> list:
    """Extract [project.scripts] key names from pyproject.toml using regex."""
    keys = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Find the [project.scripts] section and extract key names
        in_section = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "[project.scripts]":
                in_section = True
                continue
            if in_section:
                if stripped.startswith("["):
                    break  # new section
                m = re.match(r'^([a-zA-Z][a-zA-Z0-9_-]*)\s*=', stripped)
                if m:
                    keys.append(m.group(1))
    except (OSError, IOError):
        pass
    return keys


def extract_cargo_info(filepath: str) -> dict:
    """Extract workspace presence and dev-dependency keys from Cargo.toml."""
    info = {"has_workspace": False, "dev_dependencies": [], "workspace_members": []}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Check for [workspace] section
        if re.search(r"^\[workspace\]", content, re.MULTILINE):
            info["has_workspace"] = True

        # Extract workspace members
        members_match = re.search(
            r"^\[workspace\]\s*\n((?:.*\n)*?)(?=^\[|\Z)",
            content,
            re.MULTILINE,
        )
        if members_match:
            block = members_match.group(1)
            # Look for members = [...]
            members_line = re.search(
                r'members\s*=\s*\[(.*?)\]', block, re.DOTALL
            )
            if members_line:
                for m in re.finditer(r'"([^"]+)"', members_line.group(1)):
                    info["workspace_members"].append(m.group(1))

        # Extract [dev-dependencies] keys
        in_dev_deps = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "[dev-dependencies]":
                in_dev_deps = True
                continue
            if in_dev_deps:
                if stripped.startswith("["):
                    break
                m = re.match(r'^([a-zA-Z][a-zA-Z0-9_-]*)\s*=', stripped)
                if m:
                    info["dev_dependencies"].append(m.group(1))
    except (OSError, IOError):
        pass
    return info


def extract_go_mod_module(filepath: str) -> str:
    """Extract module path from go.mod."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^module\s+(\S+)", line.strip())
                if m:
                    return m.group(1)
    except (OSError, IOError):
        pass
    return ""


def extract_go_work_uses(filepath: str) -> list:
    """Extract 'use' entries from go.work."""
    uses = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Match single-line use directives: use ./path
        for m in re.finditer(r"^use\s+(\S+)", content, re.MULTILINE):
            val = m.group(1).strip()
            if val != "(":
                uses.append(val)
        # Match block use directives: use ( ... )
        block = re.search(r"use\s*\((.*?)\)", content, re.DOTALL)
        if block:
            for line in block.group(1).splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("//"):
                    uses.append(stripped)
    except (OSError, IOError):
        pass
    return uses


def extract_setup_cfg_tool_sections(filepath: str) -> list:
    """Extract [tool:*] section names from setup.cfg."""
    sections = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\[(tool:[^\]]+)\]", line.strip())
                if m:
                    sections.append(m.group(1))
    except (OSError, IOError):
        pass
    return sections


def extract_taskfile_targets(filepath: str) -> list:
    """Extract task names from Taskfile.yml using regex."""
    targets = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # In Taskfile.yml, tasks are defined under 'tasks:' as YAML keys
        in_tasks = False
        for line in content.splitlines():
            if re.match(r"^tasks:\s*$", line):
                in_tasks = True
                continue
            if in_tasks:
                # Top-level key under tasks (2-space indent typically)
                m = re.match(r"^  ([a-zA-Z][a-zA-Z0-9_-]*):", line)
                if m:
                    targets.append(m.group(1))
                # Another top-level key means we're out of tasks
                elif re.match(r"^[a-zA-Z]", line):
                    break
    except (OSError, IOError):
        pass
    return targets


def build_file_entry(abs_path: str, rel_path: str, marker: str) -> dict:
    """Create a build_files entry for a given marker file."""
    entry = {"path": rel_path}

    if marker == "Makefile":
        entry["type"] = "makefile"
        entry["targets"] = extract_makefile_targets(abs_path)
    elif marker == "Justfile":
        entry["type"] = "justfile"
        entry["targets"] = extract_justfile_recipes(abs_path)
    elif marker == "Taskfile.yml":
        entry["type"] = "taskfile"
        entry["targets"] = extract_taskfile_targets(abs_path)
    elif marker == "package.json":
        entry["type"] = "package_json"
        entry["scripts"] = extract_package_json_scripts(abs_path)
    elif marker == "pyproject.toml":
        entry["type"] = "pyproject"
        entry["tool_sections"] = extract_pyproject_tool_sections(abs_path)
        scripts = extract_pyproject_project_scripts(abs_path)
        if scripts:
            entry["project_scripts"] = scripts
    elif marker == "setup.cfg":
        entry["type"] = "setup_cfg"
        entry["tool_sections"] = extract_setup_cfg_tool_sections(abs_path)
    elif marker == "setup.py":
        entry["type"] = "setup_py"
    elif marker == "Cargo.toml":
        cargo_info = extract_cargo_info(abs_path)
        entry["type"] = "cargo"
        if cargo_info["has_workspace"]:
            entry["workspace"] = True
            if cargo_info["workspace_members"]:
                entry["workspace_members"] = cargo_info["workspace_members"]
        if cargo_info["dev_dependencies"]:
            entry["dev_dependencies"] = cargo_info["dev_dependencies"]
    elif marker == "go.mod":
        entry["type"] = "go_mod"
        module_path = extract_go_mod_module(abs_path)
        if module_path:
            entry["module"] = module_path
    elif marker == "go.work":
        entry["type"] = "go_work"
        uses = extract_go_work_uses(abs_path)
        if uses:
            entry["use"] = uses
    else:
        entry["type"] = "unknown"

    return entry


def find_tool_configs(root_abs: str, root_rel: str) -> list:
    """Find tool configuration files in a project root."""
    configs = []
    seen_types = set()
    try:
        entries = os.listdir(root_abs)
    except OSError:
        return configs

    for pattern_name, tool_type in TOOL_CONFIG_PATTERNS:
        if pattern_name in entries:
            rel_path = (
                os.path.join(root_rel, pattern_name)
                if root_rel != "."
                else pattern_name
            )
            # Only add if we haven't seen a config for this tool type in this dir
            key = (tool_type, rel_path)
            if key not in seen_types:
                seen_types.add(key)
                configs.append({"path": rel_path, "type": tool_type})

    return configs


def find_ci_files(repo_root: str) -> list:
    """Find CI workflow files relative to repo root."""
    ci_files = []

    # GitHub Actions
    workflows_dir = os.path.join(repo_root, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        try:
            for entry in sorted(os.listdir(workflows_dir)):
                if entry.endswith((".yml", ".yaml")):
                    ci_files.append(
                        os.path.join(".github", "workflows", entry)
                    )
        except OSError:
            pass

    # GitLab CI
    gitlab_ci = os.path.join(repo_root, ".gitlab-ci.yml")
    if os.path.isfile(gitlab_ci):
        ci_files.append(".gitlab-ci.yml")

    # CircleCI
    circleci = os.path.join(repo_root, ".circleci", "config.yml")
    if os.path.isfile(circleci):
        ci_files.append(os.path.join(".circleci", "config.yml"))

    return ci_files


def detect_monorepo_orchestrator(repo_root: str) -> dict:
    """Detect monorepo orchestrator at the repo root."""
    for filename, orch_type in MONOREPO_ORCHESTRATORS.items():
        filepath = os.path.join(repo_root, filename)
        if os.path.isfile(filepath):
            result = {"type": orch_type, "config_file": filename}

            # Extract additional data depending on orchestrator
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                if orch_type == "turborepo":
                    data = json.loads(content)
                    # Turbo v1 uses "pipeline", v2 uses "tasks"
                    pipeline = data.get("tasks", data.get("pipeline", {}))
                    if isinstance(pipeline, dict):
                        result["tasks"] = list(pipeline.keys())

                elif orch_type == "nx":
                    data = json.loads(content)
                    targets = data.get("targetDefaults", {})
                    if isinstance(targets, dict):
                        result["targets"] = list(targets.keys())

                elif orch_type == "pnpm":
                    # Extract package patterns from pnpm-workspace.yaml
                    patterns = []
                    for line in content.splitlines():
                        m = re.match(r"\s*-\s+['\"]?([^'\"]+)['\"]?", line)
                        if m:
                            patterns.append(m.group(1).strip())
                    if patterns:
                        result["packages"] = patterns

                elif orch_type == "lerna":
                    data = json.loads(content)
                    packages = data.get("packages", [])
                    if isinstance(packages, list):
                        result["packages"] = packages

            except (json.JSONDecodeError, OSError, IOError, ValueError):
                pass

            return result

    # Check for Cargo workspace (already parsed in build_files, but also
    # flag it at the orchestrator level)
    cargo_path = os.path.join(repo_root, "Cargo.toml")
    if os.path.isfile(cargo_path):
        cargo_info = extract_cargo_info(cargo_path)
        if cargo_info["has_workspace"]:
            result = {"type": "cargo", "config_file": "Cargo.toml"}
            if cargo_info["workspace_members"]:
                result["members"] = cargo_info["workspace_members"]
            return result

    # Check for go.work
    go_work_path = os.path.join(repo_root, "go.work")
    if os.path.isfile(go_work_path):
        uses = extract_go_work_uses(go_work_path)
        result = {"type": "go_work", "config_file": "go.work"}
        if uses:
            result["modules"] = uses
        return result

    return {}


def collect_build_files(root_abs: str, root_rel: str) -> list:
    """Collect all build/marker files in a project root directory."""
    build_files = []
    try:
        entries = os.listdir(root_abs)
    except OSError:
        return build_files

    for marker in PROJECT_MARKERS:
        if marker in entries:
            abs_path = os.path.join(root_abs, marker)
            rel_path = (
                os.path.join(root_rel, marker)
                if root_rel != "."
                else marker
            )
            if os.path.isfile(abs_path):
                build_files.append(
                    build_file_entry(abs_path, rel_path, marker)
                )

    return build_files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Read changed files from stdin
    changed_files = []
    for line in sys.stdin:
        line = line.strip()
        if line:
            changed_files.append(line)

    if not changed_files:
        # No input — output an empty profile
        json.dump(
            {
                "monorepo": False,
                "orchestrator": None,
                "contexts": [],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return

    # Determine repo root (cwd)
    repo_root = os.getcwd()

    # Group changed files by their project root
    # context_key → {"root": str, "marker": str, "files": list}
    contexts = {}
    for filepath in changed_files:
        root_rel, marker = find_project_root(filepath, repo_root)
        if root_rel not in contexts:
            contexts[root_rel] = {"root": root_rel, "marker": marker, "files": []}
        else:
            # If we found a more specific marker for this root, update it
            # (prefer language-specific markers over generic ones)
            if marker and contexts[root_rel]["marker"] is None:
                contexts[root_rel]["marker"] = marker
        contexts[root_rel]["files"].append(filepath)

    # Detect monorepo orchestrator at repo root
    orchestrator = detect_monorepo_orchestrator(repo_root)
    is_monorepo = bool(orchestrator) or len(contexts) > 1

    # Find CI files (shared across all contexts)
    ci_files = find_ci_files(repo_root)

    # Build context entries
    context_list = []
    for root_rel, ctx_data in sorted(contexts.items()):
        root_abs = (
            os.path.join(repo_root, root_rel)
            if root_rel != "."
            else repo_root
        )
        marker = ctx_data["marker"]
        language = detect_language(marker, root_rel, repo_root)

        # Collect build files in this context root
        build_files = collect_build_files(root_abs, root_rel)

        # If no build files were found (e.g., the marker was found in a
        # parent but we have root="."), ensure we still report something
        if not build_files and marker:
            marker_abs = os.path.join(root_abs, marker)
            marker_rel = (
                os.path.join(root_rel, marker) if root_rel != "." else marker
            )
            if os.path.isfile(marker_abs):
                build_files.append(
                    build_file_entry(marker_abs, marker_rel, marker)
                )

        # Find tool configs
        tool_configs = find_tool_configs(root_abs, root_rel)

        # CI files: include all repo-level CI files for each context
        # (in a monorepo, the agent will filter to relevant ones)
        context_entry = {
            "root": root_rel,
            "language": language,
            "build_files": build_files,
            "tool_configs": tool_configs,
            "ci_files": ci_files,
            "changed_files": sorted(ctx_data["files"]),
        }
        context_list.append(context_entry)

    # Build output
    output = {
        "monorepo": is_monorepo,
        "orchestrator": orchestrator if orchestrator else None,
        "contexts": context_list,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
