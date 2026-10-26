from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    risk TEXT NOT NULL,
    workspace TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    event_seq INTEGER NOT NULL DEFAULT 0,
    quick_mode INTEGER NOT NULL DEFAULT 0,
    baseline_commit TEXT NOT NULL DEFAULT 'UNKNOWN',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_revisions (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(goal_id, revision)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    attempts_count INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(goal_id, task_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks(goal_id, status);

CREATE TABLE IF NOT EXISTS dependencies (
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL,
    hard INTEGER NOT NULL,
    PRIMARY KEY(task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_dependencies_goal ON dependencies(goal_id);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    agent TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_category TEXT,
    error_message TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    side_effecting INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(task_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    task_id TEXT,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(goal_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_events_goal_seq ON events(goal_id, sequence);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    goal_revision INTEGER NOT NULL,
    task_id TEXT,
    task_attempt_id TEXT,
    commit_sha TEXT NOT NULL,
    tree_hash TEXT NOT NULL,
    diff_hash TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    protected_fingerprint TEXT NOT NULL,
    env_digest TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    cwd TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    timed_out INTEGER NOT NULL,
    stdout_sha256 TEXT NOT NULL,
    stderr_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    log_dir TEXT NOT NULL,
    runner_id TEXT NOT NULL,
    runner_kind TEXT NOT NULL,
    trust TEXT NOT NULL,
    gate TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    verifier_policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_evidence_goal_gate ON evidence(goal_id, gate);

CREATE TABLE IF NOT EXISTS gates (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    goal_revision INTEGER NOT NULL,
    gate TEXT NOT NULL,
    criterion_id TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gates_goal_gate ON gates(goal_id, gate, evaluated_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id TEXT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    goal_revision INTEGER NOT NULL,
    git_sha TEXT NOT NULL,
    state_json TEXT NOT NULL,
    next_actions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_records (
    id TEXT PRIMARY KEY,
    benchmark TEXT NOT NULL,
    mode TEXT NOT NULL,
    case_id TEXT NOT NULL,
    repeat INTEGER NOT NULL,
    agent TEXT NOT NULL,
    verified_success INTEGER NOT NULL,
    false_completion INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    cost_usd REAL NOT NULL,
    tokens INTEGER NOT NULL,
    context_mode TEXT NOT NULL,
    recovery_enabled INTEGER NOT NULL,
    reviewer_enabled INTEGER NOT NULL,
    parallel_enabled INTEGER NOT NULL,
    error TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    active INTEGER NOT NULL,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    context TEXT NOT NULL,
    decision TEXT NOT NULL,
    alternatives_json TEXT NOT NULL,
    rollback TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path: str | Path, *, immediate: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    if immediate:
        connection.execute("BEGIN IMMEDIATE")
    return connection


@contextmanager
def transaction(path: str | Path, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    connection = connect(path, immediate=immediate)
    try:
        yield connection
        if connection.in_transaction:
            connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(target, immediate=True)
    try:
        connection.executescript(SCHEMA_SQL)
        from forgeflow.domain.models import utcnow

        now = utcnow().isoformat()
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(goals)").fetchall()
        }
        if "baseline_commit" not in columns:
            connection.execute(
                "ALTER TABLE goals ADD COLUMN baseline_commit TEXT NOT NULL DEFAULT 'UNKNOWN'"
            )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (now,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            (now,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
