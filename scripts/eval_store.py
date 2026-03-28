"""
SQLite data store for code review evaluation.

Stores all benchmark data — runs, findings, judge verdicts, council classifications,
timing — in a queryable format for tracking progress, regressions, and improvement
opportunities across skill iterations and benchmarks.

Usage:
    from eval_store import EvalStore
    store = EvalStore(".eval/eval.db")
    store.save_run(run_data)
    store.save_findings(run_id, pr_id, findings)
    store.query_progress()
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
-- ─── Static benchmark data ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS benchmarks (
    id              TEXT PRIMARY KEY,       -- e.g. "martian-offline"
    name            TEXT NOT NULL,
    url             TEXT,
    description     TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS benchmark_prs (
    id              TEXT PRIMARY KEY,       -- e.g. "martian:sentry-93824"
    benchmark_id    TEXT NOT NULL REFERENCES benchmarks(id),
    external_id     TEXT,                   -- PR id within the benchmark
    repo_key        TEXT NOT NULL,
    language        TEXT NOT NULL,
    pr_title        TEXT,
    pr_number       INTEGER,
    commit_sha      TEXT,
    diff_files      INTEGER,               -- number of files changed
    diff_additions  INTEGER,               -- lines added
    diff_deletions  INTEGER,               -- lines removed
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS golden_comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_pr_id TEXT NOT NULL REFERENCES benchmark_prs(id),
    comment         TEXT NOT NULL,
    severity        TEXT,
    category        TEXT                    -- optional: bug type classification
);

-- ─── Evaluation runs ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,       -- timestamp-based
    benchmark_id    TEXT NOT NULL REFERENCES benchmarks(id),
    timestamp       TEXT NOT NULL,
    -- Config snapshot
    orchestrator_model TEXT,
    explorer_model  TEXT,
    judge_model     TEXT,
    classify_model  TEXT,
    skill_git_hash  TEXT,
    config_json     TEXT,
    -- Aggregate metrics
    prs_evaluated   INTEGER,
    prs_failed      INTEGER,
    precision       REAL,
    recall          REAL,
    f1              REAL,
    adjusted_precision  REAL,
    inclusive_precision  REAL,
    total_findings  INTEGER,
    total_golden    INTEGER,
    total_tp        INTEGER,
    -- Timing
    total_wall_s    REAL,
    total_cost_usd  REAL,
    avg_turns       REAL,
    notes           TEXT,
    benchmark_metrics_json TEXT
);

-- ─── Per-PR results within a run ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS run_prs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    benchmark_pr_id TEXT NOT NULL REFERENCES benchmark_prs(id),
    -- Metrics
    precision       REAL,
    recall          REAL,
    f1              REAL,
    findings_count  INTEGER,
    tp              INTEGER,
    fp              INTEGER,
    fn              INTEGER,
    -- Timing
    wall_s          REAL,
    api_s           REAL,
    num_turns       INTEGER,
    cost_usd        REAL,
    -- Review metadata
    status          TEXT DEFAULT 'completed',
    verdict         TEXT,                   -- PASS, WARN, FAIL
    explorer_raw_count INTEGER,             -- findings before judge filtering
    tools_ran       TEXT,                   -- comma-separated tool names that ran
    tools_missing   TEXT,                   -- comma-separated tool names that were missing
    UNIQUE(run_id, benchmark_pr_id)
);

-- ─── Individual findings ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    benchmark_pr_id TEXT NOT NULL REFERENCES benchmark_prs(id),
    finding_index   INTEGER NOT NULL,       -- position within the PR's findings list
    -- Content
    summary         TEXT NOT NULL,
    severity        TEXT,
    file            TEXT,
    line            INTEGER,
    evidence        TEXT,
    pass_name       TEXT,                   -- correctness, security, reliability, testing, etc.
    confidence      REAL,
    source          TEXT DEFAULT 'ai',      -- ai, deterministic
    tool            TEXT,                   -- semgrep, shellcheck, etc.
    -- Full JSON for anything else
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_pr ON findings(benchmark_pr_id);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
CREATE INDEX IF NOT EXISTS idx_findings_pass ON findings(pass_name);

-- ─── Judge verdicts (benchmark matching) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS judge_verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id      INTEGER NOT NULL REFERENCES findings(id),
    golden_id       INTEGER REFERENCES golden_comments(id),
    run_id          TEXT NOT NULL REFERENCES runs(id),
    judge_model     TEXT,
    is_match        BOOLEAN NOT NULL,
    confidence      REAL,
    reasoning       TEXT
);

CREATE INDEX IF NOT EXISTS idx_judge_finding ON judge_verdicts(finding_id);
CREATE INDEX IF NOT EXISTS idx_judge_run ON judge_verdicts(run_id);

-- ─── Council classifications ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS classifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id      INTEGER NOT NULL REFERENCES findings(id),
    run_id          TEXT NOT NULL REFERENCES runs(id),
    -- Merged verdict
    category        TEXT NOT NULL,          -- confirmed_bug, confirmed_vuln, valid_concern, nitpick, speculative, wrong
    relevance       REAL,
    confidence      REAL,
    agreement       TEXT,                   -- agree, soft_disagree, disputed, single_claude, single_codex
    -- Claude verdict
    claude_category TEXT,
    claude_relevance REAL,
    claude_confidence REAL,
    claude_reasoning TEXT,
    -- Codex verdict
    codex_category  TEXT,
    codex_relevance REAL,
    codex_confidence REAL,
    codex_reasoning TEXT
);

CREATE INDEX IF NOT EXISTS idx_class_finding ON classifications(finding_id);
CREATE INDEX IF NOT EXISTS idx_class_category ON classifications(category);
CREATE INDEX IF NOT EXISTS idx_class_run ON classifications(run_id);

-- ─── Per-turn session data ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    benchmark_pr_id TEXT NOT NULL REFERENCES benchmark_prs(id),
    session_id      TEXT,
    turn_number     INTEGER NOT NULL,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    has_thinking    BOOLEAN DEFAULT FALSE,
    thinking_chars  INTEGER DEFAULT 0,
    tools_used      TEXT,                   -- comma-separated tool names
    is_subagent     BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_turns_run ON session_turns(run_id);
CREATE INDEX IF NOT EXISTS idx_turns_pr ON session_turns(benchmark_pr_id);

-- ─── Schema metadata ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class EvalStore:
    """SQLite store for evaluation data."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def __enter__(self) -> "EvalStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        current_version_row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ("version",),
        ).fetchone()
        if current_version_row:
            current_version = int(current_version_row["value"])
        elif "benchmark_metrics_json" in columns:
            current_version = SCHEMA_VERSION
        else:
            current_version = 1
        migrations: dict[int, list[str]] = {}
        if "benchmark_metrics_json" not in columns:
            migrations[2] = ["ALTER TABLE runs ADD COLUMN benchmark_metrics_json TEXT"]
        for version in sorted(migrations):
            if version <= current_version:
                continue
            for statement in migrations[version]:
                self.conn.execute(statement)
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ─── Benchmark setup ─────────────────────────────────────────────────

    def ensure_benchmark(
        self, benchmark_id: str, name: str, url: str = "", desc: str = ""
    ):
        self.conn.execute(
            "INSERT OR IGNORE INTO benchmarks(id, name, url, description) VALUES (?,?,?,?)",
            (benchmark_id, name, url, desc),
        )
        self.conn.commit()

    def ensure_benchmark_pr(self, benchmark_id: str, pr: dict):
        pr_id = f"{benchmark_id}:{pr['pr_id']}"
        self.conn.execute(
            """INSERT OR IGNORE INTO benchmark_prs
               (id, benchmark_id, external_id, repo_key, language, pr_title, pr_number, commit_sha)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                pr_id,
                benchmark_id,
                pr["pr_id"],
                pr["repo_key"],
                pr["language"],
                pr.get("pr_title", ""),
                pr.get("pr_number", 0),
                pr.get("commit_sha", ""),
            ),
        )
        self.conn.commit()
        return pr_id

    def ensure_golden_comments(self, benchmark_pr_id: str, comments: list[dict]):
        # Only insert if not already present
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM golden_comments WHERE benchmark_pr_id = ?",
            (benchmark_pr_id,),
        ).fetchone()[0]
        if existing > 0:
            return
        for c in comments:
            self.conn.execute(
                "INSERT INTO golden_comments(benchmark_pr_id, comment, severity) VALUES (?,?,?)",
                (benchmark_pr_id, c["comment"], c.get("severity", "")),
            )
        self.conn.commit()

    # ─── Run management ──────────────────────────────────────────────────

    def _get_skill_git_hash(self) -> str:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def create_run(self, benchmark_id: str, config: dict | None = None) -> str:
        run_id = datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )  # include microseconds for uniqueness
        self.conn.execute(
            """INSERT INTO runs(id, benchmark_id, timestamp, skill_git_hash, config_json,
               orchestrator_model, explorer_model, judge_model)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                run_id,
                benchmark_id,
                datetime.now().isoformat(),
                self._get_skill_git_hash(),
                json.dumps(config) if config else None,
                (config or {}).get("orchestrator_model", ""),
                (config or {}).get("explorer_model", ""),
                (config or {}).get("judge_model", ""),
            ),
        )
        self.conn.commit()
        return run_id

    def update_run_metrics(self, run_id: str, metrics: dict):
        sets = []
        vals = []
        for key in [
            "prs_evaluated",
            "prs_failed",
            "precision",
            "recall",
            "f1",
            "adjusted_precision",
            "inclusive_precision",
            "total_findings",
            "total_golden",
            "total_tp",
            "total_wall_s",
            "total_cost_usd",
            "avg_turns",
            "notes",
            "benchmark_metrics_json",
        ]:
            if key in metrics:
                sets.append(f"{key} = ?")
                value = metrics[key]
                if key == "benchmark_metrics_json":
                    value = json.dumps(value) if value is not None else None
                vals.append(value)
        if "benchmark_metrics" in metrics and "benchmark_metrics_json" not in metrics:
            sets.append("benchmark_metrics_json = ?")
            vals.append(
                json.dumps(metrics["benchmark_metrics"])
                if metrics["benchmark_metrics"] is not None
                else None
            )
        if sets:
            vals.append(run_id)
            self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)
            self.conn.commit()

    # ─── Findings ────────────────────────────────────────────────────────

    def save_findings(
        self, run_id: str, benchmark_pr_id: str, findings: list[dict]
    ) -> list[int]:
        """Insert findings and return their database IDs."""
        ids = []
        for i, f in enumerate(findings):
            cur = self.conn.execute(
                """INSERT INTO findings
                   (run_id, benchmark_pr_id, finding_index,
                    summary, severity, file, line, evidence,
                    pass_name, confidence, source, tool, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    benchmark_pr_id,
                    i,
                    f.get("summary", ""),
                    f.get("severity", ""),
                    f.get("file", ""),
                    f.get("line", 0),
                    f.get("evidence", ""),
                    f.get("pass", f.get("pass_name", "")),
                    f.get("confidence"),
                    f.get("source", "ai"),
                    f.get("tool", ""),
                    json.dumps(f),
                ),
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def save_run_pr(self, run_id: str, benchmark_pr_id: str, data: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO run_prs
               (run_id, benchmark_pr_id, precision, recall, f1,
                findings_count, tp, fp, fn,
                wall_s, api_s, num_turns, cost_usd, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                benchmark_pr_id,
                data.get("precision"),
                data.get("recall"),
                data.get("f1"),
                data.get("findings_count", 0),
                data.get("tp", 0),
                data.get("fp", 0),
                data.get("fn", 0),
                data.get("wall_s"),
                data.get("api_s"),
                data.get("num_turns"),
                data.get("cost_usd"),
                data.get("status", "completed"),
            ),
        )
        self.conn.commit()

    # ─── Judge verdicts ──────────────────────────────────────────────────

    def save_judge_verdict(
        self,
        finding_id: int,
        run_id: str,
        is_match: bool,
        confidence: float = 0,
        reasoning: str = "",
        judge_model: str = "",
        golden_id: int | None = None,
    ):
        self.conn.execute(
            """INSERT INTO judge_verdicts
               (finding_id, golden_id, run_id, judge_model, is_match, confidence, reasoning)
               VALUES (?,?,?,?,?,?,?)""",
            (
                finding_id,
                golden_id,
                run_id,
                judge_model,
                is_match,
                confidence,
                reasoning,
            ),
        )
        self.conn.commit()

    # ─── Classifications ─────────────────────────────────────────────────

    def save_classification(self, finding_id: int, run_id: str, data: dict):
        self.conn.execute(
            """INSERT INTO classifications
               (finding_id, run_id, category, relevance, confidence, agreement,
                claude_category, claude_relevance, claude_confidence, claude_reasoning,
                codex_category, codex_relevance, codex_confidence, codex_reasoning)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                finding_id,
                run_id,
                data.get("category", ""),
                data.get("relevance"),
                data.get("confidence"),
                data.get("agreement", ""),
                (data.get("claude") or {}).get("category"),
                (data.get("claude") or {}).get("relevance"),
                (data.get("claude") or {}).get("confidence"),
                (data.get("claude") or {}).get("reasoning"),
                (data.get("codex") or {}).get("category"),
                (data.get("codex") or {}).get("relevance"),
                (data.get("codex") or {}).get("confidence"),
                (data.get("codex") or {}).get("reasoning"),
            ),
        )
        self.conn.commit()

    # ─── Diff stats ───────────────────────────────────────────────────────

    def update_pr_diff_stats(
        self, benchmark_pr_id: str, files: int, additions: int, deletions: int
    ):
        self.conn.execute(
            "UPDATE benchmark_prs SET diff_files=?, diff_additions=?, diff_deletions=? WHERE id=?",
            (files, additions, deletions, benchmark_pr_id),
        )
        self.conn.commit()

    # ─── Session turn data ───────────────────────────────────────────────

    def save_session_turns(
        self, run_id: str, benchmark_pr_id: str, session_id: str, turns: list[dict]
    ):
        # Don't re-import if we already have turns for this PR+run
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM session_turns WHERE run_id=? AND benchmark_pr_id=?",
            (run_id, benchmark_pr_id),
        ).fetchone()[0]
        if existing > 0:
            return

        for t in turns:
            self.conn.execute(
                """INSERT INTO session_turns
                   (run_id, benchmark_pr_id, session_id, turn_number,
                    model, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens,
                    has_thinking, thinking_chars, tools_used, is_subagent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    benchmark_pr_id,
                    session_id,
                    t.get("turn"),
                    t.get("model"),
                    t.get("input_tokens"),
                    t.get("output_tokens"),
                    t.get("cache_read"),
                    t.get("cache_write"),
                    t.get("has_thinking", False),
                    t.get("thinking_chars", 0),
                    t.get("tools_used", ""),
                    t.get("is_subagent", False),
                ),
            )
        self.conn.commit()

    # ─── Session turn analytics ──────────────────────────────────────────

    def query_turn_summary(self, run_id: str | None = None) -> list[dict]:
        """Per-PR turn stats: total turns, tokens, thinking, tool usage."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT bp.repo_key, bp.language, st.benchmark_pr_id,
                      COUNT(*) AS turns,
                      SUM(st.output_tokens) AS total_output_tokens,
                      SUM(st.cache_read_tokens) AS total_cache_read,
                      SUM(CASE WHEN st.has_thinking THEN 1 ELSE 0 END) AS thinking_turns,
                      SUM(st.thinking_chars) AS total_thinking_chars,
                      GROUP_CONCAT(DISTINCT st.model) AS models_used
               FROM session_turns st
               JOIN benchmark_prs bp ON st.benchmark_pr_id = bp.id
               WHERE st.run_id = ?
               GROUP BY st.benchmark_pr_id
               ORDER BY total_output_tokens DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_model_usage(self, run_id: str | None = None) -> list[dict]:
        """Which models are used how much across turns?"""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT st.model,
                      COUNT(*) AS turns,
                      SUM(st.input_tokens) AS total_input,
                      SUM(st.output_tokens) AS total_output,
                      SUM(st.cache_read_tokens) AS total_cache_read,
                      SUM(CASE WHEN st.has_thinking THEN 1 ELSE 0 END) AS thinking_turns,
                      ROUND(AVG(st.output_tokens), 0) AS avg_output_per_turn
               FROM session_turns st
               WHERE st.run_id = ?
               GROUP BY st.model
               ORDER BY turns DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_tool_frequency(self, run_id: str | None = None) -> list[dict]:
        """Which tools are called most frequently across turns?"""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        # tools_used is comma-separated, need to split
        rows = self.conn.execute(
            "SELECT tools_used FROM session_turns WHERE run_id=? AND tools_used != ''",
            (run_id,),
        ).fetchall()
        tool_counts: dict[str, int] = {}
        for row in rows:
            for tool in row["tools_used"].split(","):
                tool = tool.strip()
                if tool:
                    tool_counts[tool] = tool_counts.get(tool, 0) + 1
        return [
            {"tool": t, "count": c}
            for t, c in sorted(tool_counts.items(), key=lambda x: -x[1])
        ]

    # ─── Bulk import from existing JSON results ──────────────────────────

    def import_from_json(
        self,
        benchmark_id: str,
        results_json: dict,
        reviews_dir: Path | None = None,
        classify_json: dict | None = None,
    ):
        """Import a complete evaluation run from the JSON files produced by eval-martian.py."""
        run_id = self.create_run(
            benchmark_id,
            config={
                "orchestrator_model": results_json.get("review_model", ""),
                "judge_model": results_json.get("judge_model", ""),
            },
        )

        agg = results_json.get("aggregate", {})
        self.update_run_metrics(
            run_id,
            {
                "prs_evaluated": results_json.get("prs_evaluated", 0),
                "precision": agg.get("precision"),
                "recall": agg.get("recall"),
                "f1": agg.get("f1"),
                "total_findings": agg.get("total_candidates", 0),
                "total_golden": agg.get("total_golden", 0),
                "total_tp": agg.get("true_positives", 0),
            },
        )

        # Compute aggregate timing from raw files
        total_wall = 0.0
        total_cost = 0.0
        total_turns = 0
        pr_count_with_timing = 0

        # Import per-PR data
        for pr_data in results_json.get("per_pr", []):
            bp_id = f"{benchmark_id}:{pr_data['pr_id']}"
            self.ensure_benchmark_pr(benchmark_id, pr_data)

            # Load timing from .raw.json if available
            timing = {}
            if reviews_dir:
                raw_file = reviews_dir / f"{pr_data['pr_id']}.raw.json"
                if raw_file.exists():
                    try:
                        with open(raw_file, encoding="utf-8") as rf:
                            raw = json.load(rf)
                        meta = raw.get("claude_meta", {})
                        timing = {
                            "wall_s": raw.get("elapsed_s"),
                            "api_s": meta.get("duration_api_ms", 0) / 1000
                            if meta.get("duration_api_ms")
                            else None,
                            "num_turns": meta.get("num_turns"),
                            "cost_usd": meta.get("total_cost_usd"),
                        }
                        if timing.get("wall_s"):
                            total_wall += timing["wall_s"]
                        if timing.get("cost_usd"):
                            total_cost += timing["cost_usd"]
                        if timing.get("num_turns"):
                            total_turns += timing["num_turns"]
                            pr_count_with_timing += 1
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Save PR-level metrics + timing
            pr_data_with_timing = {**pr_data, **timing}
            self.save_run_pr(run_id, bp_id, pr_data_with_timing)

            # Save individual findings — try all_findings from results, fall back to review file
            all_findings = pr_data.get("all_findings", [])
            if not all_findings and reviews_dir:
                review_file = reviews_dir / f"{pr_data['pr_id']}.json"
                if review_file.exists():
                    try:
                        with open(review_file, encoding="utf-8") as rf:
                            all_findings = json.load(rf)
                        if not isinstance(all_findings, list):
                            all_findings = []
                    except (json.JSONDecodeError, TypeError):
                        all_findings = []
            if all_findings:
                finding_ids = self.save_findings(run_id, bp_id, all_findings)

                # Save judge verdicts for matched findings
                for tp in pr_data.get("true_positives", []):
                    # Find the matching finding by summary
                    for fid, f in zip(finding_ids, all_findings, strict=True):
                        cand = f.get("summary", "")
                        if cand and cand in tp.get("candidate", ""):
                            self.save_judge_verdict(
                                fid,
                                run_id,
                                True,
                                tp.get("confidence", 0),
                                tp.get("reasoning", ""),
                            )
                            break

        # Import classifications if available
        if classify_json:
            for pr_cls in classify_json.get("per_pr", []):
                bp_id = f"{benchmark_id}:{pr_cls['pr_id']}"
                # Match classifications to findings by index
                findings = self.conn.execute(
                    "SELECT id, finding_index FROM findings WHERE run_id=? AND benchmark_pr_id=? ORDER BY finding_index",
                    (run_id, bp_id),
                ).fetchall()
                fid_map = {row["finding_index"]: row["id"] for row in findings}

                for cls in pr_cls.get("classifications", []):
                    fid = fid_map.get(cls.get("finding_index"))
                    if fid:
                        self.save_classification(fid, run_id, cls)

            # Update adjusted precision
            cls_agg = classify_json.get("aggregate", {})
            self.update_run_metrics(
                run_id,
                {
                    "adjusted_precision": cls_agg.get("adjusted_precision"),
                    "inclusive_precision": cls_agg.get("inclusive_precision"),
                },
            )

        # Update aggregate timing on the run
        timing_update = {}
        if total_wall > 0:
            timing_update["total_wall_s"] = total_wall
        if total_cost > 0:
            timing_update["total_cost_usd"] = total_cost
        if pr_count_with_timing > 0:
            timing_update["avg_turns"] = total_turns / pr_count_with_timing
        if timing_update:
            self.update_run_metrics(run_id, timing_update)

        return run_id

    # ─── Analytics queries ───────────────────────────────────────────────

    def query_progress(
        self, benchmark_id: str | None = None, limit: int = 10
    ) -> list[dict]:
        """F1/precision/recall over recent runs."""
        query = """SELECT id, timestamp, precision, recall, f1,
                      adjusted_precision, inclusive_precision,
                      prs_evaluated, total_findings, total_wall_s, total_cost_usd,
                      skill_git_hash
               FROM runs"""
        params: list[str | int] = []
        if benchmark_id is not None:
            query += " WHERE benchmark_id = ?"
            params.append(benchmark_id)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def query_by_language(self, run_id: str | None = None) -> list[dict]:
        """Findings breakdown by language for a run (or latest)."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT bp.language,
                      COUNT(f.id) AS total_findings,
                      SUM(CASE WHEN c.category IN ('confirmed_bug','confirmed_vuln','valid_concern') THEN 1 ELSE 0 END) AS real_issues,
                      SUM(CASE WHEN c.category = 'nitpick' THEN 1 ELSE 0 END) AS nitpicks,
                      SUM(CASE WHEN c.category IN ('speculative','wrong') THEN 1 ELSE 0 END) AS noise,
                      SUM(CASE WHEN c.agreement = 'disputed' THEN 1 ELSE 0 END) AS disputed
               FROM findings f
               JOIN benchmark_prs bp ON f.benchmark_pr_id = bp.id
               LEFT JOIN classifications c ON c.finding_id = f.id
               WHERE f.run_id = ?
               GROUP BY bp.language
               ORDER BY total_findings DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_by_category(self, run_id: str | None = None) -> list[dict]:
        """Classification category distribution for a run."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT c.category, COUNT(*) AS count,
                      ROUND(AVG(c.relevance), 1) AS avg_relevance,
                      ROUND(AVG(c.confidence), 2) AS avg_confidence,
                      SUM(CASE WHEN c.agreement = 'agree' THEN 1 ELSE 0 END) AS agreed,
                      SUM(CASE WHEN c.agreement = 'disputed' THEN 1 ELSE 0 END) AS disputed
               FROM classifications c
               WHERE c.run_id = ?
               GROUP BY c.category
               ORDER BY count DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_by_severity(self, run_id: str | None = None) -> list[dict]:
        """Findings by severity x classification category."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT f.severity,
                      COUNT(f.id) AS total,
                      SUM(CASE WHEN c.category IN ('confirmed_bug','confirmed_vuln') THEN 1 ELSE 0 END) AS bugs,
                      SUM(CASE WHEN c.category = 'valid_concern' THEN 1 ELSE 0 END) AS concerns,
                      SUM(CASE WHEN c.category = 'nitpick' THEN 1 ELSE 0 END) AS nitpicks,
                      SUM(CASE WHEN c.category = 'wrong' THEN 1 ELSE 0 END) AS wrong
               FROM findings f
               LEFT JOIN classifications c ON c.finding_id = f.id
               WHERE f.run_id = ?
               GROUP BY f.severity
               ORDER BY CASE f.severity
                   WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                   WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END""",
                (run_id,),
            ).fetchall()
        ]

    def query_by_pass(self, run_id: str | None = None) -> list[dict]:
        """Which explorer passes find the most real bugs?"""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT f.pass_name,
                      COUNT(f.id) AS total,
                      SUM(CASE WHEN c.category IN ('confirmed_bug','confirmed_vuln','valid_concern') THEN 1 ELSE 0 END) AS real_issues,
                      SUM(CASE WHEN c.category IN ('speculative','wrong') THEN 1 ELSE 0 END) AS noise,
                      ROUND(
                        CAST(SUM(CASE WHEN c.category IN ('confirmed_bug','confirmed_vuln','valid_concern') THEN 1 ELSE 0 END) AS REAL)
                        / NULLIF(COUNT(f.id), 0), 2
                      ) AS precision_rate
               FROM findings f
               LEFT JOIN classifications c ON c.finding_id = f.id
               WHERE f.run_id = ? AND f.pass_name != ''
               GROUP BY f.pass_name
               ORDER BY real_issues DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_missed_golden(self, run_id: str | None = None) -> list[dict]:
        """Golden comments we consistently miss (false negatives across runs)."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT gc.comment, gc.severity, bp.repo_key, bp.language
               FROM golden_comments gc
               JOIN benchmark_prs bp ON gc.benchmark_pr_id = bp.id
               WHERE gc.id NOT IN (
                   SELECT jv.golden_id FROM judge_verdicts jv
                   WHERE jv.run_id = ? AND jv.is_match = 1 AND jv.golden_id IS NOT NULL
               )
               AND bp.id IN (SELECT benchmark_pr_id FROM run_prs WHERE run_id = ?)
               ORDER BY gc.severity DESC, bp.language""",
                (run_id, run_id),
            ).fetchall()
        ]

    def query_speed_trend(self, benchmark_id: str | None = None) -> list[dict]:
        """Review speed over time."""
        query = """SELECT r.id, r.timestamp, r.skill_git_hash,
                      r.total_wall_s, r.total_cost_usd, r.avg_turns,
                      r.prs_evaluated,
                      ROUND(r.total_wall_s / NULLIF(r.prs_evaluated, 0), 0) AS avg_wall_per_pr,
                      ROUND(r.total_cost_usd / NULLIF(r.prs_evaluated, 0), 2) AS avg_cost_per_pr
               FROM runs r"""
        params: list[str] = []
        if benchmark_id is not None:
            query += " WHERE r.benchmark_id = ?"
            params.append(benchmark_id)
        query += " ORDER BY r.timestamp DESC LIMIT 20"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def query_timing_detail(self, run_id: str | None = None) -> list[dict]:
        """Per-PR timing breakdown for a run."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT bp.repo_key, bp.language, rp.benchmark_pr_id,
                      rp.findings_count,
                      ROUND(rp.wall_s, 0) AS wall_s,
                      ROUND(rp.api_s, 0) AS api_s,
                      rp.num_turns,
                      ROUND(rp.cost_usd, 2) AS cost_usd,
                      rp.status,
                      CASE WHEN rp.wall_s > 0 AND rp.api_s > 0
                           THEN ROUND(rp.api_s / rp.wall_s, 2) ELSE NULL END AS parallelism_ratio
               FROM run_prs rp
               JOIN benchmark_prs bp ON rp.benchmark_pr_id = bp.id
               WHERE rp.run_id = ?
               ORDER BY rp.wall_s DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_timing_by_language(self, run_id: str | None = None) -> list[dict]:
        """Average timing metrics by language."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT bp.language,
                      COUNT(*) AS prs,
                      ROUND(AVG(rp.wall_s), 0) AS avg_wall_s,
                      ROUND(AVG(rp.api_s), 0) AS avg_api_s,
                      ROUND(AVG(rp.num_turns), 1) AS avg_turns,
                      ROUND(AVG(rp.cost_usd), 2) AS avg_cost,
                      ROUND(AVG(rp.findings_count), 1) AS avg_findings,
                      ROUND(SUM(rp.cost_usd), 2) AS total_cost
               FROM run_prs rp
               JOIN benchmark_prs bp ON rp.benchmark_pr_id = bp.id
               WHERE rp.run_id = ? AND rp.wall_s IS NOT NULL
               GROUP BY bp.language
               ORDER BY avg_wall_s DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_disputed_findings(
        self, run_id: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Findings where Claude and Codex disagreed — needs human review."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        query = """SELECT f.summary, f.severity, f.file, f.line,
                      bp.repo_key, bp.language,
                      c.claude_category, c.claude_confidence, c.claude_reasoning,
                      c.codex_category, c.codex_confidence, c.codex_reasoning
               FROM classifications c
               JOIN findings f ON c.finding_id = f.id
               JOIN benchmark_prs bp ON f.benchmark_pr_id = bp.id
               WHERE c.run_id = ? AND c.agreement = 'disputed'
               ORDER BY f.severity DESC"""
        params: list[str | int] = [run_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return [
            dict(r)
            for r in self.conn.execute(
                query,
                params,
            ).fetchall()
        ]

    def query_wrong_findings(
        self, run_id: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Findings classified as wrong — false positive patterns to fix."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        query = """SELECT f.summary, f.severity, f.file, f.pass_name,
                      bp.repo_key, bp.language,
                      c.claude_reasoning, c.codex_reasoning
               FROM classifications c
               JOIN findings f ON c.finding_id = f.id
               JOIN benchmark_prs bp ON f.benchmark_pr_id = bp.id
               WHERE c.run_id = ? AND c.category = 'wrong'
               ORDER BY bp.language, f.pass_name"""
        params: list[str | int] = [run_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return [
            dict(r)
            for r in self.conn.execute(
                query,
                params,
            ).fetchall()
        ]

    def query_cost_per_real_finding(self, run_id: str | None = None) -> list[dict]:
        """Cost efficiency: cost per real finding found."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """WITH pr_stats AS (
                       SELECT rp.run_id, rp.benchmark_pr_id, rp.cost_usd, rp.findings_count,
                              COUNT(DISTINCT CASE WHEN c.category IN ('confirmed_bug','confirmed_vuln','valid_concern') THEN f.id END) AS real_findings
                       FROM run_prs rp
                       LEFT JOIN findings f ON f.run_id = rp.run_id AND f.benchmark_pr_id = rp.benchmark_pr_id
                       LEFT JOIN classifications c ON c.finding_id = f.id
                       WHERE rp.run_id = ?
                       GROUP BY rp.run_id, rp.benchmark_pr_id, rp.cost_usd, rp.findings_count
                   )
                   SELECT bp.language,
                      COUNT(DISTINCT ps.benchmark_pr_id) AS prs,
                      SUM(ps.cost_usd) AS total_cost,
                      SUM(ps.findings_count) AS total_findings,
                      SUM(ps.real_findings) AS real_findings,
                      ROUND(SUM(ps.cost_usd) / NULLIF(SUM(ps.real_findings), 0), 2) AS cost_per_real_finding
               FROM pr_stats ps
               JOIN benchmark_prs bp ON ps.benchmark_pr_id = bp.id
               GROUP BY bp.language
               ORDER BY cost_per_real_finding DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_finding_density(self, run_id: str | None = None) -> list[dict]:
        """Finding density vs diff size — are we calibrated?"""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT rp.benchmark_pr_id,
                      bp.language,
                      bp.diff_additions + bp.diff_deletions AS diff_lines,
                      bp.diff_files,
                      rp.findings_count,
                      ROUND(CAST(rp.findings_count AS REAL) / NULLIF(bp.diff_additions + bp.diff_deletions, 0) * 100, 1) AS findings_per_100_lines,
                      ROUND(rp.wall_s, 0) AS wall_s
               FROM run_prs rp
               JOIN benchmark_prs bp ON rp.benchmark_pr_id = bp.id
               WHERE rp.run_id = ? AND bp.diff_additions IS NOT NULL
               ORDER BY findings_per_100_lines DESC""",
                (run_id,),
            ).fetchall()
        ]

    def query_severity_calibration(self, run_id: str | None = None) -> list[dict]:
        """Do our severity ratings match reality? High-severity findings that are wrong = problem."""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT f.severity,
                      c.category,
                      COUNT(*) AS count
               FROM findings f
               JOIN classifications c ON c.finding_id = f.id
               WHERE f.run_id = ?
               GROUP BY f.severity, c.category
               ORDER BY CASE f.severity
                   WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                   WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END,
                   c.category""",
                (run_id,),
            ).fetchall()
        ]

    def query_stability(self, benchmark_id: str | None = None) -> list[dict]:
        """Cross-run finding stability — same PR, different runs: how many findings overlap?"""
        query = """WITH per_run AS (
                       SELECT run_id, benchmark_pr_id, COUNT(*) AS total
                       FROM findings GROUP BY run_id, benchmark_pr_id
                   )
                   SELECT f1.benchmark_pr_id,
                      r1.id AS run_a, r2.id AS run_b,
                      pa.total AS findings_a,
                      pb.total AS findings_b,
                      COUNT(DISTINCT f1.id) AS exact_overlap
               FROM findings f1
               JOIN findings f2 ON f1.benchmark_pr_id = f2.benchmark_pr_id
                                AND f1.summary = f2.summary
                                AND f1.run_id != f2.run_id
               JOIN runs r1 ON f1.run_id = r1.id
               JOIN runs r2 ON f2.run_id = r2.id
               JOIN per_run pa ON pa.run_id = f1.run_id AND pa.benchmark_pr_id = f1.benchmark_pr_id
               JOIN per_run pb ON pb.run_id = f2.run_id AND pb.benchmark_pr_id = f2.benchmark_pr_id"""
        params: list[str] = []
        if benchmark_id is not None:
            query += """ WHERE r1.benchmark_id = ? AND r2.benchmark_id = ?
                 AND r1.timestamp < r2.timestamp"""
            params.extend([benchmark_id, benchmark_id])
        else:
            query += " WHERE r1.timestamp < r2.timestamp"
        query += """
               GROUP BY f1.benchmark_pr_id, r1.id, r2.id
               ORDER BY exact_overlap DESC
               LIMIT 20"""
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def query_golden_by_type(self, run_id: str | None = None) -> list[dict]:
        """What types of golden comments do we catch vs miss?"""
        if not run_id:
            run_id = self._latest_run_id()
        if not run_id:
            return []
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT gc.severity,
                      bp.language,
                      COUNT(*) AS total,
                      SUM(CASE WHEN jv.is_match = 1 THEN 1 ELSE 0 END) AS caught,
                      SUM(CASE WHEN jv.is_match IS NULL OR jv.is_match = 0 THEN 1 ELSE 0 END) AS missed,
                      ROUND(CAST(SUM(CASE WHEN jv.is_match = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*), 2) AS catch_rate
               FROM golden_comments gc
               JOIN benchmark_prs bp ON gc.benchmark_pr_id = bp.id
               LEFT JOIN judge_verdicts jv ON jv.golden_id = gc.id AND jv.run_id = ?
               WHERE bp.id IN (SELECT benchmark_pr_id FROM run_prs WHERE run_id = ?)
               GROUP BY gc.severity, bp.language
               ORDER BY catch_rate ASC""",
                (run_id, run_id),
            ).fetchall()
        ]

    def _latest_run_id(self, benchmark_id: str | None = None) -> str | None:
        if benchmark_id is None:
            row = self.conn.execute(
                "SELECT id FROM runs ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM runs WHERE benchmark_id = ? ORDER BY timestamp DESC LIMIT 1",
                (benchmark_id,),
            ).fetchone()
        return row["id"] if row else None

    # ─── Raw SQL for ad-hoc queries ──────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run arbitrary SQL. For interactive exploration."""
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
