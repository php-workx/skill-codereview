#!/usr/bin/env bash
# test-orchestrate-integration.sh - Integration coverage for the orchestrator pipeline.
#
# Exercises a round-trip prepare -> post-explorers -> finalize flow with fixture-backed
# data plus negative cases for missing prompt files, malformed explorer output, and
# invalid judge output.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export REPO_ROOT
python3 - <<'PY'
import json
import os
import shutil
import subprocess
import tempfile
import atexit
from argparse import Namespace
from pathlib import Path
from unittest import mock

from scripts import orchestrate as orch

REPO_ROOT = Path(os.environ["REPO_ROOT"])
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "orchestrate"
PROMPTS = REPO_ROOT / "skills" / "codereview" / "prompts"
SKILL_SCRIPTS = REPO_ROOT / "skills" / "codereview" / "scripts"
_TEMP_DIRS = []


@atexit.register
def _cleanup_tempdirs() -> None:
    for tmpdir in _TEMP_DIRS:
        tmpdir.cleanup()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def build_repo(copy_missing_prompt: str | None = None) -> Path:
    tmpdir = tempfile.TemporaryDirectory(prefix="sc-0it0-")
    _TEMP_DIRS.append(tmpdir)
    tmpdir_path = Path(tmpdir.name)
    repo = tmpdir_path / "repo"
    (repo / ".git").mkdir(parents=True)
    prompt_dir = repo / "skills" / "codereview" / "prompts"
    scripts_dir = repo / "skills" / "codereview" / "scripts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for prompt in PROMPTS.glob("reviewer-*.md"):
        if copy_missing_prompt and prompt.name == copy_missing_prompt:
            continue
        shutil.copy2(prompt, prompt_dir / prompt.name)
    for script in SKILL_SCRIPTS.iterdir():
        if script.is_file():
            shutil.copy2(script, scripts_dir / script.name)
    return repo


def write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def fake_context_result(script_name: str):
    if script_name == "discover-project.py":
        return {
            "monorepo": False,
            "contexts": [{"path": ".", "language": "python"}],
            "orchestrator": "codereview",
        }
    if script_name == "complexity.sh":
        return {
            "hotspots": [{"file": "scripts/run.sh", "score": "C"}],
            "tool_status": {"radon": {"status": "skipped", "version": "n/a", "finding_count": 0, "note": "not python"}},
        }
    if script_name == "git-risk.sh":
        return {
            "tiers": [{"file": "scripts/run.sh", "tier": "high"}],
            "tool_status": {"git-risk": {"status": "ran", "version": "n/a", "finding_count": 1, "note": None}},
        }
    if script_name == "run-scans.sh":
        return {
            "findings": [{"tool": "semgrep", "severity": "high", "confidence": 0.93}],
            "tool_status": {"semgrep": {"status": "ran", "version": "1.0.0", "finding_count": 1, "note": None}},
        }
    if script_name == "coverage-collect.py":
        return {"coverage": [{"file": "scripts/run.sh", "lines": 100}]}
    raise AssertionError(f"unexpected helper script: {script_name}")


def fake_finalize_result(script_name: str):
    if script_name == "enrich-findings.py":
        return {
            "findings": [
                {
                    "id": "security-allowlist-01",
                    "source": "ai",
                    "pass": "security",
                    "severity": "high",
                    "confidence": 0.93,
                    "file": "scripts/orchestrate.py",
                    "line": 89,
                    "summary": "Subprocess command is assembled from untrusted input",
                    "failure_mode": "A crafted review task could execute arbitrary commands.",
                    "fix": "Require allowlisted commands before dispatch.",
                    "action_tier": "must_fix",
                }
            ],
            "tier_summary": {"must_fix": 1, "should_fix": 0, "consider": 0},
            "dropped": {"below_confidence_floor": 0},
        }
    if script_name == "lifecycle.py":
        return {
            "findings": [
                {
                    "id": "security-allowlist-01",
                    "source": "ai",
                    "pass": "security",
                    "severity": "high",
                    "confidence": 0.93,
                    "file": "scripts/orchestrate.py",
                    "line": 89,
                    "summary": "Subprocess command is assembled from untrusted input",
                    "failure_mode": "A crafted review task could execute arbitrary commands.",
                    "fix": "Require allowlisted commands before dispatch.",
                    "action_tier": "must_fix",
                }
            ],
            "suppressed_findings": [],
            "lifecycle_summary": {
                "new": 1,
                "recurring": 0,
                "rejected": 0,
                "deferred": 0,
                "deferred_resurfaced": 0,
            },
        }
    raise AssertionError(f"unexpected finalize helper script: {script_name}")


def round_trip() -> None:
    repo = build_repo()
    session_dir = repo / "session"
    diff_result = orch.DiffResult(
        mode="base",
        base_ref="main",
        merge_base="abc123",
        changed_files=["scripts/run.sh"],
        diff_text="@@\n+#!/usr/bin/env bash\n+set -euo pipefail\n",
    )

    def run_subprocess_json(command, cwd=None, timeout=None, input_text=None):
        script_name = next(Path(part).name for part in command if part.endswith((".py", ".sh")))
        return fake_context_result(script_name)

    with (
        mock.patch.object(orch, "detect_repo_root", return_value=repo),
        mock.patch.object(orch, "SKILL_DIR", repo / "skills" / "codereview"),
        mock.patch.object(orch, "extract_diff", return_value=diff_result),
        mock.patch.object(orch, "run_subprocess_json", side_effect=run_subprocess_json),
    ):
        rc = orch.prepare(
            Namespace(
                session_dir=session_dir,
                no_config=True,
                spec=None,
                spec_scope=None,
                base="main",
                mode="base",
                timeout=1200,
            )
        )
    assert_equal(rc, 0, "prepare should succeed")

    launch = json.loads((session_dir / "launch.json").read_text(encoding="utf-8"))
    tasks = launch["waves"][0]["tasks"]
    task_names = [task["name"] for task in tasks]
    assert_true("correctness" in task_names, "core expert should be scheduled")
    assert_true("shell-script" in task_names, "shell expert should be scheduled for shell diffs")
    assert_true((session_dir / "explorer-shell-script-prompt.md").exists(), "shell prompt should be written")

    correctness = json.loads((FIXTURES / "mock-explorer-correctness.json").read_text(encoding="utf-8"))
    security = json.loads((FIXTURES / "mock-explorer-security.json").read_text(encoding="utf-8"))
    test_adequacy = {
        "pass": "test-adequacy",
        "status": "done",
        "findings": [
            {
                "severity": "medium",
                "confidence": 0.55,
                "file": "tests/test_orchestrate.py",
                "line": 1,
                "summary": "Round trip should verify the finalize path",
                "evidence": "The pipeline needs end-to-end coverage.",
                "fix": "Keep the round-trip test in place.",
            }
        ],
        "requirements": [{"id": "req-1", "text": "The pipeline should emit a report"}],
    }
    shell_script = [
        {
            "severity": "high",
            "confidence": 0.60,
            "file": "scripts/orchestrate.py",
            "line": 89,
            "summary": "Shell wrapper should reject untrusted commands",
            "evidence": "Duplicate of the security finding with lower confidence.",
            "fix": "Use the allowlist before execution.",
        },
        {
            "severity": "high",
            "confidence": 0.88,
            "file": "scripts/orchestrate.py",
            "line": 89,
            "summary": "Shell wrapper should reject untrusted commands",
            "evidence": "Same finding with a higher confidence score.",
            "fix": "Use the allowlist before execution.",
        },
    ]

    outputs = {
        "correctness": json.dumps(correctness),
        "security": json.dumps(security),
        "test-adequacy": json.dumps(test_adequacy),
        "shell-script": json.dumps(shell_script),
    }
    for task in tasks:
        write(Path(task["output_file"]), outputs.get(task["name"], "[]"))

    post_rc = orch.post_explorers(Namespace(session_dir=session_dir))
    assert_equal(post_rc, 0, "post-explorers should succeed")

    judge_input = json.loads((session_dir / "judge-input.json").read_text(encoding="utf-8"))
    assert_equal(judge_input["status"], "ready_for_judge", "judge input should be ready")
    assert_equal(judge_input["explorer_status"]["correctness"]["status"], "ok", "correctness explorer should be ok")
    assert_equal(judge_input["explorer_status"]["shell-script"]["status"], "ok", "shell explorer should be ok")
    assert_true(
        judge_input["explorer_finding_count"] < judge_input["raw_finding_count"],
        "dedup/filtering should reduce the raw finding count",
    )

    judge_output_path = Path(launch["judge"]["output_file"])
    write(judge_output_path, (FIXTURES / "mock-judge-output.json").read_text(encoding="utf-8"))

    with (
        mock.patch.object(orch, "detect_repo_root", return_value=repo),
        mock.patch.object(orch, "run_subprocess_json", side_effect=lambda command, cwd=None, timeout=None: fake_finalize_result(Path(command[1]).name)),
        mock.patch.object(orch.subprocess, "run", return_value=subprocess.CompletedProcess(["bash"], 0, "", "")),
    ):
        fin_rc = orch.finalize(Namespace(session_dir=session_dir, judge_output=None))
    assert_equal(fin_rc, 0, "finalize should succeed")

    report = json.loads((session_dir / "report.json").read_text(encoding="utf-8"))
    assert_equal(report["verdict"], "FAIL", "final report should surface the blocking finding")
    assert_true((session_dir / "report.md").exists(), "markdown report should be written")
    artifact_dir = repo / ".agents" / "reviews"
    assert_true(any(artifact_dir.glob("*.json")), "finalize should write a JSON artifact")
    assert_true(any(artifact_dir.glob("*.md")), "finalize should write a markdown artifact")


def missing_prompt_failure() -> None:
    repo = build_repo(copy_missing_prompt="reviewer-correctness-pass.md")
    session_dir = repo / "session"
    diff_result = orch.DiffResult(
        mode="base",
        base_ref="main",
        merge_base="abc123",
        changed_files=["scripts/run.sh"],
        diff_text="@@\n+#!/usr/bin/env bash\n",
    )

    with (
        mock.patch.object(orch, "detect_repo_root", return_value=repo),
        mock.patch.object(orch, "extract_diff", return_value=diff_result),
        mock.patch.object(orch, "SKILL_DIR", repo / "skills" / "codereview"),
    ):
        try:
            orch.prepare(
                Namespace(
                    session_dir=session_dir,
                    no_config=True,
                    spec=None,
                    spec_scope=None,
                    base="main",
                    mode="base",
                    timeout=1200,
                )
            )
        except FileNotFoundError as exc:
            assert_true("reviewer-correctness-pass.md" in str(exc), "missing prompt should be named in the failure")
        else:
            raise AssertionError("prepare should fail when a required prompt file is missing")


def malformed_explorer_failure() -> None:
    repo = build_repo()
    session_dir = repo / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    launch_packet = {
        "session_dir": str(session_dir),
        "waves": [
            {
                "wave": 1,
                "tasks": [
                    {
                        "name": "correctness",
                        "output_file": str((session_dir / "explorer-correctness.json").absolute()),
                    },
                    {
                        "name": "security",
                        "output_file": str((session_dir / "explorer-security.json").absolute()),
                    },
                    {
                        "name": "missing",
                        "output_file": str((session_dir / "explorer-missing.json").absolute()),
                    },
                ],
            }
        ],
        "judge": {
            "prompt_file": str((PROMPTS / "reviewer-judge.md").absolute()),
            "output_file": str((session_dir / "judge.json").absolute()),
        },
        "scan_results": {"findings": []},
        "config": {"confidence_floor": 0.65},
    }
    write(session_dir / "launch.json", json.dumps(launch_packet))
    write(session_dir / "explorer-correctness.json", "{\"pass\":\"correctness\",\"findings\":[{\"summary\":\"ok\"}]}")
    write(session_dir / "explorer-security.json", "not json at all")

    with mock.patch.object(orch, "detect_repo_root", return_value=repo):
        rc = orch.post_explorers(Namespace(session_dir=session_dir))
    assert_equal(rc, 0, "post-explorers should continue despite malformed outputs")

    judge_input = json.loads((session_dir / "judge-input.json").read_text(encoding="utf-8"))
    assert_equal(judge_input["explorer_status"]["security"]["status"], "invalid_json", "malformed explorer output should be flagged")
    assert_equal(judge_input["explorer_status"]["missing"]["status"], "missing", "missing explorer output should be flagged")


def invalid_judge_output_failure() -> None:
    repo = build_repo()
    session_dir = repo / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    write(session_dir / "changed-files.txt", "scripts/orchestrate.py\n")
    launch_packet = {
        "review_id": "review-123",
        "session_dir": str(session_dir),
        "scope": "branch",
        "base_ref": "main",
        "head_ref": "feature",
        "mode": "standard",
        "config": {"confidence_floor": 0.65},
        "tool_status": {"semgrep": {"status": "ran", "finding_count": 1, "note": None}},
        "diff_result": {"changed_files": ["scripts/orchestrate.py"]},
        "scan_results": {"findings": []},
        "judge": {
            "prompt_file": str((PROMPTS / "reviewer-judge.md").absolute()),
            "output_file": str((session_dir / "judge.json").absolute()),
        },
    }
    write(session_dir / "launch.json", json.dumps(launch_packet))
    write(session_dir / "judge.json", "this is not json")

    with mock.patch.object(orch, "detect_repo_root", return_value=repo):
        rc = orch.finalize(Namespace(session_dir=session_dir, judge_output=None))
        assert_true(rc == 1, "finalize should return 1 on invalid judge output")


round_trip()
missing_prompt_failure()
malformed_explorer_failure()
invalid_judge_output_failure()
print("integration checks passed")
PY
