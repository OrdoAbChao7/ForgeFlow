from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forgeflow.domain.enums import (
    AttemptStatus,
    ErrorCategory,
    EventType,
    GateKind,
    GateStatus,
    GoalStatus,
    RiskLevel,
    TaskStatus,
    TrustLevel,
)
from forgeflow.domain.errors import (
    ClaimConflictError,
    ConflictError,
    LeaseExpiredError,
    NotFoundError,
    ValidationFailure,
)
from forgeflow.domain.models import (
    Artifact,
    BenchmarkRecord,
    Checkpoint,
    Decision,
    Dependency,
    Event,
    Evidence,
    GateEvaluation,
    Goal,
    GoalRevision,
    GoalSpec,
    PolicyCandidate,
    Task,
    TaskAttempt,
    digest_json,
    new_id,
    utcnow,
)
from forgeflow.planner.graph import task_id_for, topological_waves
from forgeflow.state import db

_ACTIVE_GOAL_STATUSES = {
    GoalStatus.PLANNED.value,
    GoalStatus.RUNNING.value,
    GoalStatus.VERIFYING.value,
}
_ELIGIBLE_CLAIM_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.READY.value,
    TaskStatus.RETRY_WAIT.value,
    TaskStatus.INTERRUPTED.value,
}
_VERIFIED = TaskStatus.VERIFIED.value

_ALLOWED_GOAL_TRANSITIONS: dict[str, set[str]] = {
    GoalStatus.DRAFT.value: {
        GoalStatus.PLANNED.value,
        GoalStatus.RUNNING.value,
        GoalStatus.CANCELLED.value,
    },
    GoalStatus.PLANNED.value: {GoalStatus.RUNNING.value, GoalStatus.CANCELLED.value},
    GoalStatus.RUNNING.value: {
        GoalStatus.PAUSED.value,
        GoalStatus.VERIFYING.value,
        GoalStatus.BLOCKED.value,
        GoalStatus.FAILED.value,
        GoalStatus.CANCELLED.value,
    },
    GoalStatus.PAUSED.value: {GoalStatus.RUNNING.value, GoalStatus.CANCELLED.value},
    GoalStatus.VERIFYING.value: {
        GoalStatus.RUNNING.value,
        GoalStatus.DELIVERED.value,
        GoalStatus.BLOCKED.value,
        GoalStatus.FAILED.value,
        GoalStatus.CANCELLED.value,
    },
    GoalStatus.BLOCKED.value: {
        GoalStatus.RUNNING.value,
        GoalStatus.FAILED.value,
        GoalStatus.CANCELLED.value,
    },
    GoalStatus.FAILED.value: {GoalStatus.RUNNING.value, GoalStatus.CANCELLED.value},
    GoalStatus.DELIVERED.value: set(),
    GoalStatus.CANCELLED.value: set(),
}


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class Claim:
    task: Task
    attempt: TaskAttempt
    lease_token: str


class Store:
    """SQLite event store and transactional projections.

    The events table is the audit source of truth. Task rows are a durable projection
    updated in the same explicit transaction as events. Every externally visible state
    transition is append-only and replayable.
    """

    def __init__(self, workspace: str | Path, db_path: str | Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(db_path) if db_path else self.workspace / ".forge" / "forgeflow.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.initialize(self.db_path)

    # ------------------------------------------------------------------ goals
    def create_goal(
        self,
        spec: GoalSpec,
        *,
        quick_mode: bool = False,
        actor: str = "cli",
        goal_id: str | None = None,
        correlation_id: str | None = None,
        baseline_commit: str = "UNKNOWN",
    ) -> Goal:
        topological_waves(spec)
        now = utcnow()
        goal_id = goal_id or new_id("goal")
        correlation = correlation_id or new_id("corr")
        tasks = []
        for task_spec in spec.tasks:
            initial = (
                TaskStatus.READY.value if not task_spec.depends_on else TaskStatus.PENDING.value
            )
            tasks.append((task_spec, task_id_for(goal_id, task_spec.key), initial))
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO goals(id,title,objective,status,risk,workspace,current_revision,
                   event_seq,quick_mode,baseline_commit,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id,
                    spec.title,
                    spec.objective,
                    GoalStatus.PLANNED.value,
                    spec.risk.value,
                    str(self.workspace),
                    1,
                    0,
                    int(quick_mode),
                    baseline_commit,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            revision_id = new_id("rev")
            connection.execute(
                """INSERT INTO goal_revisions(id,goal_id,revision,spec_json,contract_digest,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    revision_id,
                    goal_id,
                    1,
                    _json(spec.model_dump(mode="json")),
                    digest_json(spec.model_dump(mode="json")),
                    now.isoformat(),
                ),
            )
            for task_spec, tid, initial in tasks:
                connection.execute(
                    """INSERT INTO tasks(id,goal_id,task_key,title,description,status,priority,
                       max_attempts,attempts_count,version,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tid,
                        goal_id,
                        task_spec.key,
                        task_spec.title,
                        task_spec.description,
                        initial,
                        task_spec.priority,
                        task_spec.max_attempts,
                        0,
                        0,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                for dep in task_spec.depends_on:
                    connection.execute(
                        "INSERT INTO dependencies(goal_id,task_id,depends_on,hard) VALUES (?,?,?,1)",
                        (goal_id, tid, task_id_for(goal_id, dep)),
                    )
                for dep in task_spec.soft_depends_on:
                    connection.execute(
                        "INSERT INTO dependencies(goal_id,task_id,depends_on,hard) VALUES (?,?,?,0)",
                        (goal_id, tid, task_id_for(goal_id, dep)),
                    )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.GOAL_CREATED,
                payload={
                    "title": spec.title,
                    "objective": spec.objective,
                    "risk": spec.risk.value,
                    "workspace": str(self.workspace),
                },
                actor=actor,
                correlation_id=correlation,
                timestamp=now,
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.GOAL_PLANNED,
                payload={
                    "revision": 1,
                    "contract_digest": digest_json(spec.model_dump(mode="json")),
                    "tasks": [
                        {
                            "task_id": tid,
                            "key": task_spec.key,
                            "title": task_spec.title,
                            "status": initial,
                            "depends_on": task_spec.depends_on,
                            "soft_depends_on": task_spec.soft_depends_on,
                            "priority": task_spec.priority,
                            "max_attempts": task_spec.max_attempts,
                        }
                        for task_spec, tid, initial in tasks
                    ],
                },
                actor=actor,
                correlation_id=correlation,
                timestamp=now,
            )
            for task_spec, tid, initial in tasks:
                if initial == TaskStatus.READY.value:
                    self._append_event(
                        connection,
                        goal_id=goal_id,
                        task_id=tid,
                        event_type=EventType.TASK_READY,
                        payload={"task_id": tid, "key": task_spec.key, "status": initial},
                        actor=actor,
                        correlation_id=correlation,
                        timestamp=now,
                    )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> Goal:
        with db.transaction(self.db_path, immediate=False) as connection:
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        return Goal(
            id=row["id"],
            title=row["title"],
            objective=row["objective"],
            status=GoalStatus(row["status"]),
            risk=RiskLevel(row["risk"]),
            workspace=row["workspace"],
            current_revision=row["current_revision"],
            event_seq=row["event_seq"],
            quick_mode=bool(row["quick_mode"]),
            baseline_commit=row["baseline_commit"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def list_goals(self) -> list[Goal]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute("SELECT id FROM goals ORDER BY created_at").fetchall()
        return [self.get_goal(row["id"]) for row in rows]

    def get_revision(self, goal_id: str, revision: int | None = None) -> GoalRevision:
        with db.transaction(self.db_path, immediate=False) as connection:
            if revision is None:
                row = connection.execute(
                    """SELECT r.* FROM goal_revisions r JOIN goals g
                       ON g.id=r.goal_id AND g.current_revision=r.revision WHERE g.id=?""",
                    (goal_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM goal_revisions WHERE goal_id=? AND revision=?",
                    (goal_id, revision),
                ).fetchone()
        if row is None:
            raise NotFoundError(f"goal revision not found: {goal_id} rev {revision}")
        return GoalRevision(
            id=row["id"],
            goal_id=row["goal_id"],
            revision=row["revision"],
            spec=GoalSpec.model_validate_json(row["spec_json"]),
            contract_digest=row["contract_digest"],
            created_at=_dt(row["created_at"]),
        )

    def get_spec(self, goal_id: str, revision: int | None = None) -> GoalSpec:
        return self.get_revision(goal_id, revision).spec

    def set_goal_status(
        self, goal_id: str, status: GoalStatus, *, reason: str, actor: str = "system"
    ) -> Goal:
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"goal not found: {goal_id}")
            previous = GoalStatus(row["status"])
            if previous == status:
                return self.get_goal(goal_id)
            if status.value not in _ALLOWED_GOAL_TRANSITIONS.get(previous.value, set()):
                raise ConflictError(
                    f"illegal goal transition {previous.value} -> {status.value}"
                )
            connection.execute(
                "UPDATE goals SET status=?, updated_at=? WHERE id=?",
                (status.value, now.isoformat(), goal_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.GOAL_STATUS_CHANGED,
                payload={"from": previous.value, "to": status.value, "reason": reason},
                actor=actor,
                timestamp=now,
            )
        return self.get_goal(goal_id)

    # ------------------------------------------------------------------ tasks
    def _assert_fence(
        self,
        row: sqlite3.Row,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        if row["status"] not in {TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value}:
            raise LeaseExpiredError(
                f"task is no longer leased (status={row['status']})",
                details={"task_id": row["id"]},
            )
        if row["owner"] != owner or row["lease_token"] != lease_token:
            raise LeaseExpiredError(
                "stale owner or fencing token; refusing to write",
                details={"task_id": row["id"]},
            )
        expires = _dt(row["lease_expires_at"])
        if expires is None or expires <= now:
            raise LeaseExpiredError(
                "lease has expired; an expired worker cannot commit",
                details={"task_id": row["id"], "expired_at": expires.isoformat() if expires else None},
            )

    def _task_from_row(
        self, row: sqlite3.Row, dependencies: Iterable[sqlite3.Row] = ()
    ) -> Task:        return Task(
            id=row["id"],
            goal_id=row["goal_id"],
            key=row["task_key"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            max_attempts=row["max_attempts"],
            attempts_count=row["attempts_count"],
            owner=row["owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_dt(row["lease_expires_at"]),
            last_error=row["last_error"],
            version=row["version"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def list_tasks(self, goal_id: str) -> list[Task]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? ORDER BY priority DESC, created_at, task_key",
                (goal_id,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, goal_id: str, task_id_or_key: str) -> Task:
        with db.transaction(self.db_path, immediate=False) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND (id=? OR task_key=?)",
                (goal_id, task_id_or_key, task_id_or_key),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id_or_key}")
        return self._task_from_row(row)

    def task_status_map(self, goal_id: str) -> dict[str, TaskStatus]:
        return {task.key: task.status for task in self.list_tasks(goal_id)}

    def list_dependencies(self, goal_id: str) -> list[Dependency]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM dependencies WHERE goal_id=? ORDER BY task_id, depends_on",
                (goal_id,),
            ).fetchall()
            task_ids = {
                row["id"]: row["task_key"]
                for row in connection.execute(
                    "SELECT id, task_key FROM tasks WHERE goal_id=?", (goal_id,)
                ).fetchall()
            }
        return [
            Dependency(
                goal_id=row["goal_id"],
                task_id=task_ids[row["task_id"]],
                depends_on=task_ids[row["depends_on"]],
                hard=bool(row["hard"]),
            )
            for row in rows
        ]

    def ready_tasks(self, goal_id: str) -> list[Task]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                """SELECT t.* FROM tasks t
                   WHERE t.goal_id=? AND t.status IN ('PENDING','READY','RETRY_WAIT','INTERRUPTED')
                   AND NOT EXISTS (
                     SELECT 1 FROM dependencies d JOIN tasks dep ON dep.id=d.depends_on
                     WHERE d.task_id=t.id AND d.hard=1 AND dep.status!='VERIFIED')
                   ORDER BY t.priority DESC, t.created_at, t.task_key""",
                (goal_id,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def claim_task(
        self,
        goal_id: str,
        *,
        owner: str,
        lease_seconds: float = 300.0,
        task_key: str | None = None,
        agent: str = "mock",
    ) -> Claim:
        if lease_seconds <= 0:
            raise ValidationFailure("lease_seconds must be positive")
        now = utcnow()
        token = new_id("lease")
        with db.transaction(self.db_path) as connection:
            goal_row = connection.execute(
                "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if goal_row is None:
                raise NotFoundError(f"goal not found: {goal_id}")
            if goal_row["status"] not in _ACTIVE_GOAL_STATUSES:
                raise ClaimConflictError(
                    f"goal {goal_id} is {goal_row['status']}; cannot claim tasks"
                )
            placeholders = ",".join("?" for _ in _ELIGIBLE_CLAIM_STATUSES)
            task_filter = " AND t.task_key=?" if task_key else ""
            params: list[Any] = [goal_id, *sorted(_ELIGIBLE_CLAIM_STATUSES)]
            if task_key:
                params.append(task_key)
            rows = connection.execute(
                f"""SELECT t.* FROM tasks t
                    WHERE t.goal_id=? AND t.status IN ({placeholders})
                    AND NOT EXISTS (
                      SELECT 1 FROM dependencies d JOIN tasks dep ON dep.id=d.depends_on
                      WHERE d.task_id=t.id AND d.hard=1 AND dep.status!='VERIFIED')
                    {task_filter}
                    ORDER BY t.priority DESC, t.created_at, t.task_key LIMIT 2""",
                params,
            ).fetchall()
            if not rows:
                raise ClaimConflictError(
                    "no ready task available"
                    + (f" for key {task_key!r}" if task_key else "")
                )
            if len(rows) > 1 and task_key is None:
                # A claim still selects exactly one deterministically; extra row only signals contention.
                pass
            row = rows[0]
            task = self._task_from_row(row)
            attempt_no = task.attempts_count + 1
            if attempt_no > task.max_attempts:
                raise ClaimConflictError(f"task {task.key} exhausted max_attempts")
            attempt_id = new_id("attempt")
            expires = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """UPDATE tasks SET status=?,owner=?,lease_token=?,lease_expires_at=?,
                   attempts_count=?,version=version+1,updated_at=? WHERE id=? AND version=?""",
                (
                    TaskStatus.CLAIMED.value,
                    owner,
                    token,
                    expires.isoformat(),
                    attempt_no,
                    now.isoformat(),
                    task.id,
                    task.version,
                ),
            )
            spec = self.get_spec(goal_id)
            task_spec = next(item for item in spec.tasks if item.key == task.key)
            connection.execute(
                """INSERT INTO attempts(id,goal_id,task_id,attempt_no,status,owner,lease_token,
                   agent,started_at,side_effecting,idempotency_key,metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    goal_id,
                    task.id,
                    attempt_no,
                    AttemptStatus.STARTED.value,
                    owner,
                    token,
                    agent,
                    now.isoformat(),
                    int(task_spec.side_effecting),
                    task_spec.idempotency_key,
                    "{}",
                ),
            )
            correlation = new_id("corr")
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task.id,
                event_type=EventType.TASK_CLAIMED,
                payload={
                    "task_id": task.id,
                    "key": task.key,
                    "status": TaskStatus.CLAIMED.value,
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "owner": owner,
                    "lease_token_fingerprint": hashlib.sha256(token.encode()).hexdigest(),
                    "lease_expires_at": expires.isoformat(),
                },
                actor=owner,
                correlation_id=correlation,
                timestamp=now,
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task.id,
                event_type=EventType.ATTEMPT_STARTED,
                payload={
                    "task_id": task.id,
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "status": AttemptStatus.STARTED.value,
                },
                actor=owner,
                correlation_id=correlation,
                timestamp=now,
            )
            if goal_row["status"] == GoalStatus.PLANNED.value:
                connection.execute(
                    "UPDATE goals SET status=?,updated_at=? WHERE id=?",
                    (GoalStatus.RUNNING.value, now.isoformat(), goal_id),
                )
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    event_type=EventType.GOAL_STATUS_CHANGED,
                    payload={
                        "from": GoalStatus.PLANNED.value,
                        "to": GoalStatus.RUNNING.value,
                        "reason": "first task claimed",
                    },
                    actor=owner,
                    correlation_id=correlation,
                    timestamp=now,
                )
            connection.execute(
                "UPDATE goals SET updated_at=? WHERE id=?", (now.isoformat(), goal_id)
            )
            task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
            attempt_row = connection.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return Claim(
            task=self._task_from_row(task_row),
            attempt=self._attempt_from_row(attempt_row),
            lease_token=token,
        )

    def heartbeat(
        self,
        goal_id: str,
        *,
        task_id: str,
        owner: str,
        lease_token: str,
        lease_seconds: float = 300.0,
    ) -> Task:
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._assert_fence(row, owner=owner, lease_token=lease_token, now=now)
            connection.execute(
                """UPDATE tasks SET lease_expires_at=?,version=version+1,updated_at=?
                   WHERE id=?""",
                (expires.isoformat(), now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.TASK_LEASE_RENEWED,
                payload={"task_id": task_id, "lease_expires_at": expires.isoformat()},
                actor=owner,
                timestamp=now,
            )
            updated = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(updated)

    def settle_success(
        self,
        goal_id: str,
        *,
        task_id: str,
        attempt_id: str,
        owner: str,
        lease_token: str,
        cost_usd: float = 0.0,
        tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._assert_fence(row, owner=owner, lease_token=lease_token, now=now)
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id=? AND task_id=?", (attempt_id, task_id)
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"attempt not found: {attempt_id}")
            if attempt["status"] not in (AttemptStatus.STARTED.value, AttemptStatus.RUNNING.value):
                raise ConflictError(f"attempt is already {attempt['status']}")
            connection.execute(
                """UPDATE attempts SET status=?,ended_at=?,cost_usd=?,tokens=?,metadata_json=?
                   WHERE id=?""",
                (
                    AttemptStatus.SUCCEEDED.value,
                    now.isoformat(),
                    cost_usd,
                    tokens,
                    _json(metadata or {}),
                    attempt_id,
                ),
            )
            connection.execute(
                """UPDATE tasks SET status=?,owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   version=version+1,updated_at=? WHERE id=?""",
                (TaskStatus.VERIFYING.value, now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.ATTEMPT_SETTLED,
                payload={
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "attempt_status": AttemptStatus.SUCCEEDED.value,
                    "task_status": TaskStatus.VERIFYING.value,
                    "claimed_success": True,
                    "cost_usd": cost_usd,
                    "tokens": tokens,
                },
                actor=owner,
                timestamp=now,
            )
            updated = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(updated)

    def settle_failure(
        self,
        goal_id: str,
        *,
        task_id: str,
        attempt_id: str,
        owner: str,
        lease_token: str,
        error_category: ErrorCategory,
        error_message: str,
        cost_usd: float = 0.0,
        tokens: int = 0,
        retryable: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._assert_fence(row, owner=owner, lease_token=lease_token, now=now)
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id=? AND task_id=?", (attempt_id, task_id)
            ).fetchone()
            if attempt is None:
                raise NotFoundError(f"attempt not found: {attempt_id}")
            if error_category == ErrorCategory.EXTERNAL_BLOCKER:
                next_status = TaskStatus.BLOCKED
            elif retryable and row["attempts_count"] < row["max_attempts"]:
                next_status = TaskStatus.READY
            else:
                next_status = TaskStatus.FAILED
            connection.execute(
                """UPDATE attempts SET status=?,ended_at=?,error_category=?,error_message=?,
                   cost_usd=?,tokens=?,metadata_json=? WHERE id=?""",
                (
                    AttemptStatus.FAILED.value,
                    now.isoformat(),
                    error_category.value,
                    error_message[:4000],
                    cost_usd,
                    tokens,
                    _json(metadata or {}),
                    attempt_id,
                ),
            )
            connection.execute(
                """UPDATE tasks SET status=?,owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   last_error=?,version=version+1,updated_at=? WHERE id=?""",
                (next_status.value, error_message[:4000], now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.ATTEMPT_SETTLED,
                payload={
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "attempt_status": AttemptStatus.FAILED.value,
                    "task_status": next_status.value,
                    "claimed_success": False,
                    "error_category": error_category.value,
                    "error_message": error_message[:1000],
                },
                actor=owner,
                timestamp=now,
            )
            if next_status == TaskStatus.BLOCKED:
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    task_id=task_id,
                    event_type=EventType.TASK_BLOCKED,
                    payload={"task_id": task_id, "status": next_status.value, "reason": error_message},
                    actor=owner,
                    timestamp=now,
                )
            elif next_status == TaskStatus.FAILED:
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    task_id=task_id,
                    event_type=EventType.TASK_FAILED,
                    payload={"task_id": task_id, "status": next_status.value, "reason": error_message},
                    actor=owner,
                    timestamp=now,
                )
            else:
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    task_id=task_id,
                    event_type=EventType.TASK_READY,
                    payload={
                        "task_id": task_id,
                        "status": next_status.value,
                        "reason": "retry_after_failure",
                    },
                    actor=owner,
                    timestamp=now,
                )
            updated = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from_row(updated)

    def fail_verification(
        self,
        goal_id: str,
        *,
        task_id: str,
        attempt_id: str,
        reason: str,
        category: ErrorCategory = ErrorCategory.CONTRACT,
        retryable: bool = True,
        actor: str = "gate-engine",
    ) -> Task:
        """Record independent verification failure after an agent-claimed success."""
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            if row["status"] not in {
                TaskStatus.VERIFYING.value,
                TaskStatus.CLAIMED.value,
                TaskStatus.RUNNING.value,
            }:
                raise ConflictError(f"cannot fail verification from {row['status']}")
            if category == ErrorCategory.EXTERNAL_BLOCKER:
                next_status = TaskStatus.BLOCKED
            elif retryable and row["attempts_count"] < row["max_attempts"]:
                next_status = TaskStatus.READY
            else:
                next_status = TaskStatus.FAILED
            connection.execute(
                """UPDATE attempts SET status=?,ended_at=?,error_category=?,error_message=?
                   WHERE id=? AND task_id=?""",
                (
                    AttemptStatus.FAILED.value,
                    now.isoformat(),
                    category.value,
                    reason[:4000],
                    attempt_id,
                    task_id,
                ),
            )
            connection.execute(
                """UPDATE tasks SET status=?,last_error=?,version=version+1,updated_at=?
                   WHERE id=?""",
                (next_status.value, reason[:4000], now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.ATTEMPT_SETTLED,
                payload={
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "attempt_status": AttemptStatus.FAILED.value,
                    "task_status": next_status.value,
                    "claimed_success": True,
                    "verification_failed": True,
                    "error_category": category.value,
                    "error_message": reason[:1000],
                },
                actor=actor,
                timestamp=now,
            )
            event_type = (
                EventType.TASK_BLOCKED
                if next_status == TaskStatus.BLOCKED
                else EventType.TASK_FAILED
                if next_status == TaskStatus.FAILED
                else EventType.TASK_READY
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=event_type,
                payload={
                    "task_id": task_id,
                    "status": next_status.value,
                    "reason": reason[:1000],
                },
                actor=actor,
                timestamp=now,
            )
        return self.get_task(goal_id, task_id)

    def mark_task_verified(
        self,
        goal_id: str,
        *,
        task_id: str,
        attempt_id: str,
        evidence_ids: list[str],
        evaluated_gates: list[GateEvaluation],
        actor: str = "gate-engine",
    ) -> Task:
        if not evidence_ids:
            raise ValidationFailure("task verification requires at least one evidence record")
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if task_row is None:
                raise NotFoundError(f"task not found: {task_id}")
            if task_row["status"] not in (
                TaskStatus.VERIFYING.value,
                TaskStatus.CLAIMED.value,
                TaskStatus.RUNNING.value,
            ):
                raise ConflictError(f"task cannot be verified from {task_row['status']}")
            rows = connection.execute(
                f"SELECT id,trust,exit_code,timed_out,task_id,task_attempt_id,goal_id "
                f"FROM evidence WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                evidence_ids,
            ).fetchall()
            if len(rows) != len(set(evidence_ids)):
                raise ValidationFailure("one or more evidence ids do not exist")
            for item in rows:
                if item["goal_id"] != goal_id or item["task_id"] != task_id:
                    raise ValidationFailure("evidence is not bound to this task")
                if item["task_attempt_id"] != attempt_id:
                    raise ValidationFailure("evidence is not bound to the current attempt")
                if item["trust"] not in {
                    TrustLevel.TRUSTED_RUNNER.value,
                    TrustLevel.CLEAN_ROOM.value,
                    TrustLevel.MANUAL_SIGNED.value,
                }:
                    raise ValidationFailure("self-reported evidence cannot verify a task")
                if item["exit_code"] != 0 or item["timed_out"]:
                    raise ValidationFailure("failed evidence cannot verify a task")
            if any(gate.status != GateStatus.PASS for gate in evaluated_gates):
                raise ValidationFailure("all required task gates must PASS before verification")
            goal_row = connection.execute(
                "SELECT current_revision FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if goal_row is None:
                raise NotFoundError(f"goal not found: {goal_id}")
            persisted_gate_evidence: set[str] = set()
            for evaluation in evaluated_gates:
                gate_row = connection.execute(
                    "SELECT * FROM gates WHERE id=?", (evaluation.id,)
                ).fetchone()
                if gate_row is None:
                    raise ValidationFailure(
                        "gate evaluation is not persisted; verification must use the gate engine"
                    )
                if (
                    gate_row["goal_id"] != goal_id
                    or gate_row["goal_revision"] != goal_row["current_revision"]
                    or gate_row["status"] != GateStatus.PASS.value
                    or gate_row["gate"] != evaluation.gate.value
                    or gate_row["criterion_id"] != evaluation.criterion_id
                ):
                    raise ValidationFailure("gate evaluation does not match the current task/goal")
                persisted_gate_evidence.update(json.loads(gate_row["evidence_ids_json"]))
            if not set(evidence_ids).issubset(persisted_gate_evidence):
                raise ValidationFailure("task evidence is not covered by persisted gate evaluations")
            spec = self.get_spec(goal_id)
            task_spec = next(item for item in spec.tasks if item.key == task_row["task_key"])
            required = set(task_spec.gates)
            passed = {gate.gate for gate in evaluated_gates if gate.status == GateStatus.PASS}
            missing = required - passed
            if missing:
                raise ValidationFailure(f"task gates not evaluated PASS: {sorted(g.value for g in missing)}")
            connection.execute(
                """UPDATE tasks SET status=?,last_error=NULL,version=version+1,updated_at=?
                   WHERE id=?""",
                (TaskStatus.VERIFIED.value, now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.TASK_VERIFIED,
                payload={
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "status": TaskStatus.VERIFIED.value,
                    "evidence_ids": evidence_ids,
                    "gates": [gate.model_dump(mode="json") for gate in evaluated_gates],
                },
                actor=actor,
                timestamp=now,
            )
            self._release_dependents(connection, goal_id, task_id, actor=actor, at=now)
        return self.get_task(goal_id, task_id)

    def _release_dependents(
        self,
        connection: sqlite3.Connection,
        goal_id: str,
        verified_task_id: str,
        *,
        actor: str,
        at: datetime,
    ) -> None:
        children = connection.execute(
            "SELECT DISTINCT task_id FROM dependencies WHERE goal_id=? AND depends_on=? AND hard=1",
            (goal_id, verified_task_id),
        ).fetchall()
        for child in children:
            child_id = child["task_id"]
            blocked = connection.execute(
                """SELECT COUNT(*) AS n FROM dependencies d JOIN tasks dep ON dep.id=d.depends_on
                   WHERE d.task_id=? AND d.hard=1 AND dep.status!='VERIFIED'""",
                (child_id,),
            ).fetchone()["n"]
            if blocked == 0:
                child_row = connection.execute(
                    "SELECT * FROM tasks WHERE id=?", (child_id,)
                ).fetchone()
                if child_row["status"] == TaskStatus.PENDING.value:
                    connection.execute(
                        "UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=?",
                        (TaskStatus.READY.value, at.isoformat(), child_id),
                    )
                    self._append_event(
                        connection,
                        goal_id=goal_id,
                        task_id=child_id,
                        event_type=EventType.TASK_READY,
                        payload={
                            "task_id": child_id,
                            "key": child_row["task_key"],
                            "status": TaskStatus.READY.value,
                            "reason": f"dependency {verified_task_id} verified",
                        },
                        actor=actor,
                        timestamp=at,
                    )

    def block_task(self, goal_id: str, task_id: str, reason: str, *, actor: str = "system") -> Task:
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND id=?", (goal_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            if row["status"] == TaskStatus.VERIFIED.value:
                raise ConflictError("cannot block a verified task")
            connection.execute(
                """UPDATE tasks SET status=?,owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   last_error=?,version=version+1,updated_at=? WHERE id=?""",
                (TaskStatus.BLOCKED.value, reason[:4000], now.isoformat(), task_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                task_id=task_id,
                event_type=EventType.TASK_BLOCKED,
                payload={"task_id": task_id, "status": TaskStatus.BLOCKED.value, "reason": reason},
                actor=actor,
                timestamp=now,
            )
        return self.get_task(goal_id, task_id)

    def expire_leases(self, goal_id: str | None = None, *, actor: str = "recovery") -> list[Task]:
        now = utcnow()
        interrupted: list[Task] = []
        with db.transaction(self.db_path) as connection:
            if goal_id:
                rows = connection.execute(
                    """SELECT t.*, a.id AS attempt_id FROM tasks t
                       LEFT JOIN attempts a ON a.task_id=t.id AND a.attempt_no=t.attempts_count
                       WHERE t.goal_id=? AND t.status IN ('CLAIMED','RUNNING')
                       AND t.lease_expires_at IS NOT NULL AND t.lease_expires_at < ?""",
                    (goal_id, now.isoformat()),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT t.*, a.id AS attempt_id FROM tasks t
                       LEFT JOIN attempts a ON a.task_id=t.id AND a.attempt_no=t.attempts_count
                       WHERE t.status IN ('CLAIMED','RUNNING')
                       AND t.lease_expires_at IS NOT NULL AND t.lease_expires_at < ?""",
                    (now.isoformat(),),
                ).fetchall()
            for row in rows:
                spec = self.get_spec(row["goal_id"])
                task_spec = next(item for item in spec.tasks if item.key == row["task_key"])
                next_status = (
                    TaskStatus.BLOCKED if task_spec.side_effecting else TaskStatus.INTERRUPTED
                )
                reason = (
                    "lease expired during an irreversible/side-effecting attempt; "
                    "manual reconciliation required (no automatic replay)"
                    if task_spec.side_effecting
                    else "lease expired; attempt outcome UNKNOWN and marked INTERRUPTED"
                )
                if row["attempt_id"]:
                    connection.execute(
                        """UPDATE attempts SET status=?,ended_at=?,error_category=?,error_message=?
                           WHERE id=?""",
                        (
                            AttemptStatus.INTERRUPTED.value,
                            now.isoformat(),
                            ErrorCategory.TIMEOUT.value,
                            reason,
                            row["attempt_id"],
                        ),
                    )
                connection.execute(
                    """UPDATE tasks SET status=?,owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                       last_error=?,version=version+1,updated_at=? WHERE id=?""",
                    (next_status.value, reason, now.isoformat(), row["id"]),
                )
                self._append_event(
                    connection,
                    goal_id=row["goal_id"],
                    task_id=row["id"],
                    event_type=EventType.TASK_LEASE_EXPIRED,
                    payload={
                        "task_id": row["id"],
                        "status": next_status.value,
                        "reason": reason,
                        "automatic_replay": not task_spec.side_effecting,
                        "attempt_id": row["attempt_id"],
                        "attempt_status": AttemptStatus.INTERRUPTED.value,
                    },
                    actor=actor,
                    timestamp=now,
                )
                updated = connection.execute(
                    "SELECT * FROM tasks WHERE id=?", (row["id"],)
                ).fetchone()
                interrupted.append(self._task_from_row(updated))
        return interrupted

    def resume_goal(self, goal_id: str, *, actor: str = "recovery") -> Goal:
        self.expire_leases(goal_id, actor=actor)
        goal = self.get_goal(goal_id)
        if goal.status == GoalStatus.CANCELLED:
            raise ConflictError("cancelled goals cannot be resumed")
        if goal.status == GoalStatus.DELIVERED:
            raise ConflictError("delivered goals cannot be resumed")
        now = utcnow()
        actions: list[str] = []
        with db.transaction(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND status IN ('INTERRUPTED','UNKNOWN')",
                (goal_id,),
            ).fetchall()
            spec = self.get_spec(goal_id)
            by_key = {item.key: item for item in spec.tasks}
            for row in rows:
                task_spec = by_key[row["task_key"]]
                if task_spec.side_effecting:
                    actions.append(
                        f"{row['task_key']}: manual reconciliation required before retry"
                    )
                    continue
                if row["attempts_count"] >= row["max_attempts"]:
                    connection.execute(
                        "UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=?",
                        (TaskStatus.FAILED.value, now.isoformat(), row["id"]),
                    )
                    actions.append(f"{row['task_key']}: max attempts exhausted")
                    continue
                connection.execute(
                    "UPDATE tasks SET status=?,last_error=NULL,version=version+1,updated_at=? WHERE id=?",
                    (TaskStatus.READY.value, now.isoformat(), row["id"]),
                )
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    task_id=row["id"],
                    event_type=EventType.TASK_READY,
                    payload={
                        "task_id": row["id"],
                        "status": TaskStatus.READY.value,
                        "reason": "resume_after_interruption",
                    },
                    actor=actor,
                    timestamp=now,
                )
                actions.append(f"{row['task_key']}: retry scheduled")
            blocked_rows = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? AND status='BLOCKED'", (goal_id,)
            ).fetchall()
            for row in blocked_rows:
                task_spec = by_key[row["task_key"]]
                if task_spec.side_effecting:
                    actions.append(
                        f"{row['task_key']}: manual reconciliation required "
                        f"(idempotency key {task_spec.idempotency_key!r})"
                    )
            target = (
                GoalStatus.RUNNING
                if goal.status in {GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.FAILED}
                else goal.status
            )
            connection.execute(
                "UPDATE goals SET status=?,updated_at=? WHERE id=?",
                (target.value, now.isoformat(), goal_id),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.GOAL_RESUMED,
                payload={"status": target.value, "actions": actions},
                actor=actor,
                timestamp=now,
            )
        return self.get_goal(goal_id)

    def pause_goal(self, goal_id: str, *, actor: str = "cli") -> Goal:
        goal = self.get_goal(goal_id)
        if goal.status != GoalStatus.RUNNING:
            raise ConflictError(f"only RUNNING goals can be paused (got {goal.status})")
        return self.set_goal_status(goal_id, GoalStatus.PAUSED, reason="user pause", actor=actor)

    def cancel_goal(self, goal_id: str, *, reason: str, actor: str = "cli") -> Goal:
        now = utcnow()
        with db.transaction(self.db_path) as connection:
            row = connection.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"goal not found: {goal_id}")
            if row["status"] == GoalStatus.DELIVERED.value:
                raise ConflictError("delivered goals cannot be cancelled")
            cancelled_rows = connection.execute(
                """SELECT id, task_key FROM tasks WHERE goal_id=?
                   AND status NOT IN ('VERIFIED','FAILED','CANCELLED')""",
                (goal_id,),
            ).fetchall()
            connection.execute(
                """UPDATE tasks SET status=?,owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   version=version+1,updated_at=?
                   WHERE goal_id=? AND status NOT IN ('VERIFIED','FAILED','CANCELLED')""",
                (TaskStatus.CANCELLED.value, now.isoformat(), goal_id),
            )
            connection.execute(
                "UPDATE goals SET status=?,updated_at=? WHERE id=?",
                (GoalStatus.CANCELLED.value, now.isoformat(), goal_id),
            )
            for cancelled in cancelled_rows:
                self._append_event(
                    connection,
                    goal_id=goal_id,
                    task_id=cancelled["id"],
                    event_type=EventType.TASK_CANCELLED,
                    payload={
                        "task_id": cancelled["id"],
                        "key": cancelled["task_key"],
                        "status": TaskStatus.CANCELLED.value,
                        "reason": reason,
                    },
                    actor=actor,
                    timestamp=now,
                )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.GOAL_CANCELLED,
                payload={"status": GoalStatus.CANCELLED.value, "reason": reason},
                actor=actor,
                timestamp=now,
            )
        return self.get_goal(goal_id)

    # --------------------------------------------------------------- attempts
    def _attempt_from_row(self, row: sqlite3.Row) -> TaskAttempt:
        return TaskAttempt(
            id=row["id"],
            goal_id=row["goal_id"],
            task_id=row["task_id"],
            attempt_no=row["attempt_no"],
            status=AttemptStatus(row["status"]),
            owner=row["owner"],
            lease_token=row["lease_token"],
            agent=row["agent"],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            error_category=ErrorCategory(row["error_category"])
            if row["error_category"]
            else None,
            error_message=row["error_message"],
            cost_usd=row["cost_usd"],
            tokens=row["tokens"],
            side_effecting=bool(row["side_effecting"]),
            idempotency_key=row["idempotency_key"],
            metadata=json.loads(row["metadata_json"]),
        )

    def list_attempts(self, goal_id: str, task_id: str | None = None) -> list[TaskAttempt]:
        with db.transaction(self.db_path, immediate=False) as connection:
            if task_id:
                rows = connection.execute(
                    "SELECT * FROM attempts WHERE goal_id=? AND task_id=? ORDER BY attempt_no",
                    (goal_id, task_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM attempts WHERE goal_id=? ORDER BY started_at",
                    (goal_id,),
                ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    # ----------------------------------------------------------------- events
    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        goal_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        actor: str,
        timestamp: datetime,
        task_id: str | None = None,
        correlation_id: str | None = None,
        revision: int | None = None,
        event_id: str | None = None,
    ) -> Event:
        if event_id:
            existing = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing:
                return self._event_from_row(existing)
        goal_row = connection.execute(
            "SELECT event_seq,current_revision FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        if goal_row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        sequence = goal_row["event_seq"] + 1
        event_id = event_id or new_id("evt")
        event = Event(
            event_id=event_id,
            sequence=sequence,
            goal_id=goal_id,
            task_id=task_id,
            type=event_type,
            timestamp=timestamp,
            correlation_id=correlation_id or new_id("corr"),
            revision=revision or goal_row["current_revision"],
            actor=actor,
            payload=payload,
        )
        connection.execute("UPDATE goals SET event_seq=? WHERE id=?", (sequence, goal_id))
        connection.execute(
            """INSERT INTO events(event_id,goal_id,sequence,task_id,type,timestamp,correlation_id,
               revision,actor,payload_json,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.goal_id,
                event.sequence,
                event.task_id,
                event.type.value,
                event.timestamp.isoformat(),
                event.correlation_id,
                event.revision,
                event.actor,
                _json(event.payload),
                event.schema_version,
            ),
        )
        return event

    def append_event(
        self,
        goal_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        actor: str,
        task_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
    ) -> Event:
        with db.transaction(self.db_path) as connection:
            return self._append_event(
                connection,
                goal_id=goal_id,
                event_type=event_type,
                payload=payload,
                actor=actor,
                timestamp=utcnow(),
                task_id=task_id,
                correlation_id=correlation_id,
                event_id=event_id,
            )

    def _event_from_row(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            sequence=row["sequence"],
            goal_id=row["goal_id"],
            task_id=row["task_id"],
            type=EventType(row["type"]),
            timestamp=_dt(row["timestamp"]),
            correlation_id=row["correlation_id"],
            revision=row["revision"],
            actor=row["actor"],
            payload=json.loads(row["payload_json"]),
            schema_version=row["schema_version"],
        )

    def list_events(self, goal_id: str) -> list[Event]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE goal_id=? ORDER BY sequence", (goal_id,)
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def project_state(self, goal_id: str) -> dict[str, Any]:
        """Replay the event log into a status projection for audit/consistency checks."""
        goal_status: str | None = None
        tasks: dict[str, dict[str, Any]] = {}
        attempts: dict[str, str] = {}
        for event in self.list_events(goal_id):
            payload = event.payload
            if event.type == EventType.GOAL_CREATED:
                goal_status = GoalStatus.PLANNED.value
            elif event.type == EventType.GOAL_PLANNED:
                tasks = {
                    item["key"]: {
                        "task_id": item["task_id"],
                        "status": item["status"],
                        "attempts_count": 0,
                    }
                    for item in payload["tasks"]
                }
            elif event.type == EventType.GOAL_STATUS_CHANGED:
                goal_status = payload["to"]
            elif event.type == EventType.GOAL_RESUMED or event.type == EventType.GOAL_CANCELLED:
                goal_status = payload["status"]
            elif event.type == EventType.ATTEMPT_STARTED:
                attempts[payload["attempt_id"]] = AttemptStatus.STARTED.value
            elif event.type == EventType.ATTEMPT_SETTLED:
                attempts[payload["attempt_id"]] = payload["attempt_status"]
                task_id = payload["task_id"]
                for item in tasks.values():
                    if item["task_id"] == task_id:
                        item["status"] = payload["task_status"]
            elif event.type == EventType.TASK_LEASE_EXPIRED:
                if payload.get("attempt_id"):
                    attempts[payload["attempt_id"]] = payload.get(
                        "attempt_status", AttemptStatus.INTERRUPTED.value
                    )
                for item in tasks.values():
                    if item["task_id"] == payload.get("task_id", event.task_id):
                        item["status"] = payload.get("status", item["status"])
            elif event.type in {
                EventType.TASK_READY,
                EventType.TASK_CLAIMED,
                EventType.TASK_VERIFIED,
                EventType.TASK_BLOCKED,
                EventType.TASK_FAILED,
                EventType.TASK_CANCELLED,
                EventType.TASK_LEASE_EXPIRED,
            }:
                task_id = payload.get("task_id", event.task_id)
                for item in tasks.values():
                    if item["task_id"] == task_id:
                        item["status"] = payload.get("status", item["status"])
                        if event.type == EventType.TASK_CLAIMED:
                            item["attempts_count"] = payload.get("attempt_no", 0)
        return {"goal_status": goal_status, "tasks": tasks, "attempts": attempts}

    def projection_snapshot(self, goal_id: str) -> dict[str, Any]:
        goal = self.get_goal(goal_id)
        return {
            "goal_status": goal.status.value,
            "tasks": {
                task.key: {
                    "task_id": task.id,
                    "status": task.status.value,
                    "attempts_count": task.attempts_count,
                }
                for task in self.list_tasks(goal_id)
            },
            "attempts": {attempt.id: attempt.status.value for attempt in self.list_attempts(goal_id)},
        }

    # --------------------------------------------------------------- evidence
    def add_evidence(self, evidence: Evidence, *, actor: str = "runner") -> Evidence:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO evidence(id,goal_id,goal_revision,task_id,task_attempt_id,commit_sha,
                   tree_hash,diff_hash,source_fingerprint,protected_fingerprint,env_digest,argv_json,
                   cwd,exit_code,timed_out,stdout_sha256,stderr_sha256,report_sha256,log_dir,
                   runner_id,runner_kind,trust,gate,scope_json,verifier_policy_version,created_at,
                   metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence.id,
                    evidence.goal_id,
                    evidence.goal_revision,
                    evidence.task_id,
                    evidence.task_attempt_id,
                    evidence.commit_sha,
                    evidence.tree_hash,
                    evidence.diff_hash,
                    evidence.source_fingerprint,
                    evidence.protected_fingerprint,
                    evidence.env_digest,
                    _json(evidence.argv),
                    evidence.cwd,
                    evidence.exit_code,
                    int(evidence.timed_out),
                    evidence.stdout_sha256,
                    evidence.stderr_sha256,
                    evidence.report_sha256,
                    evidence.log_dir,
                    evidence.runner_id,
                    evidence.runner_kind,
                    evidence.trust.value,
                    evidence.gate.value,
                    _json(evidence.scope),
                    evidence.verifier_policy_version,
                    evidence.created_at.isoformat(),
                    _json(evidence.metadata),
                ),
            )
            self._append_event(
                connection,
                goal_id=evidence.goal_id,
                task_id=evidence.task_id,
                event_type=EventType.EVIDENCE_RECORDED,
                payload={
                    "evidence_id": evidence.id,
                    "gate": evidence.gate.value,
                    "exit_code": evidence.exit_code,
                    "trust": evidence.trust.value,
                    "scope": evidence.scope,
                    "source_fingerprint": evidence.source_fingerprint,
                },
                actor=actor,
                timestamp=utcnow(),
            )
        return evidence

    def _evidence_from_row(self, row: sqlite3.Row) -> Evidence:
        return Evidence(
            id=row["id"],
            goal_id=row["goal_id"],
            goal_revision=row["goal_revision"],
            task_id=row["task_id"],
            task_attempt_id=row["task_attempt_id"],
            commit_sha=row["commit_sha"],
            tree_hash=row["tree_hash"],
            diff_hash=row["diff_hash"],
            source_fingerprint=row["source_fingerprint"],
            protected_fingerprint=row["protected_fingerprint"],
            env_digest=row["env_digest"],
            argv=json.loads(row["argv_json"]),
            cwd=row["cwd"],
            exit_code=row["exit_code"],
            timed_out=bool(row["timed_out"]),
            stdout_sha256=row["stdout_sha256"],
            stderr_sha256=row["stderr_sha256"],
            report_sha256=row["report_sha256"],
            log_dir=row["log_dir"],
            runner_id=row["runner_id"],
            runner_kind=row["runner_kind"],
            trust=TrustLevel(row["trust"]),
            gate=GateKind(row["gate"]),
            scope=json.loads(row["scope_json"]),
            verifier_policy_version=row["verifier_policy_version"],
            created_at=_dt(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def list_evidence(
        self,
        goal_id: str,
        *,
        gate: GateKind | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list[Evidence]:
        clauses = ["goal_id=?"]
        params: list[Any] = [goal_id]
        if gate:
            clauses.append("gate=?")
            params.append(gate.value)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if attempt_id:
            clauses.append("task_attempt_id=?")
            params.append(attempt_id)
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                f"SELECT * FROM evidence WHERE {' AND '.join(clauses)} ORDER BY created_at",
                params,
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    # ------------------------------------------------------------------ gates
    def save_gate_evaluation(self, gate: GateEvaluation, *, actor: str = "gate-engine") -> None:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO gates(id,goal_id,goal_revision,gate,criterion_id,status,reason,
                   evidence_ids_json,source_fingerprint,policy_version,evaluated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    gate.id,
                    gate.goal_id,
                    gate.goal_revision,
                    gate.gate.value,
                    gate.criterion_id,
                    gate.status.value,
                    gate.reason,
                    _json(gate.evidence_ids),
                    gate.source_fingerprint,
                    gate.policy_version,
                    gate.evaluated_at.isoformat(),
                ),
            )
            self._append_event(
                connection,
                goal_id=gate.goal_id,
                event_type=EventType.GATE_EVALUATED,
                payload=gate.model_dump(mode="json"),
                actor=actor,
                timestamp=gate.evaluated_at,
            )

    def latest_gates(self, goal_id: str) -> list[GateEvaluation]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM gates WHERE goal_id=? ORDER BY evaluated_at DESC, rowid DESC",
                (goal_id,),
            ).fetchall()
        seen: set[tuple[str, str | None]] = set()
        latest: list[GateEvaluation] = []
        for row in rows:
            key = (row["gate"], row["criterion_id"])
            if key in seen:
                continue
            seen.add(key)
            latest.append(self._gate_from_row(row))
        return latest

    def _gate_from_row(self, row: sqlite3.Row) -> GateEvaluation:
        return GateEvaluation(
            id=row["id"],
            goal_id=row["goal_id"],
            goal_revision=row["goal_revision"],
            gate=GateKind(row["gate"]),
            criterion_id=row["criterion_id"],
            status=GateStatus(row["status"]),
            reason=row["reason"],
            evidence_ids=json.loads(row["evidence_ids_json"]),
            source_fingerprint=row["source_fingerprint"],
            policy_version=row["policy_version"],
            evaluated_at=_dt(row["evaluated_at"]),
        )

    # ------------------------------------------------------------- artifacts
    def add_artifact(self, artifact: Artifact, *, actor: str = "runner") -> Artifact:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO artifacts(id,goal_id,task_id,path,sha256,size,kind,created_at,
                   metadata_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    artifact.id,
                    artifact.goal_id,
                    artifact.task_id,
                    artifact.path,
                    artifact.sha256,
                    artifact.size,
                    artifact.kind,
                    artifact.created_at.isoformat(),
                    _json(artifact.metadata),
                ),
            )
            self._append_event(
                connection,
                goal_id=artifact.goal_id,
                task_id=artifact.task_id,
                event_type=EventType.ARTIFACT_BUILT,
                payload={
                    "artifact_id": artifact.id,
                    "path": artifact.path,
                    "sha256": artifact.sha256,
                    "kind": artifact.kind,
                },
                actor=actor,
                timestamp=utcnow(),
            )
        return artifact

    def list_artifacts(self, goal_id: str) -> list[Artifact]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE goal_id=? ORDER BY created_at", (goal_id,)
            ).fetchall()
        return [
            Artifact(
                id=row["id"],
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                path=row["path"],
                sha256=row["sha256"],
                size=row["size"],
                kind=row["kind"],
                created_at=_dt(row["created_at"]),
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    # ----------------------------------------------------------- checkpoints
    def create_checkpoint(
        self,
        goal_id: str,
        *,
        git_sha: str,
        state: dict[str, Any],
        next_actions: list[str],
        actor: str = "cli",
    ) -> Checkpoint:
        goal = self.get_goal(goal_id)
        checkpoint = Checkpoint(
            id=new_id("cp"),
            goal_id=goal_id,
            goal_revision=goal.current_revision,
            git_sha=git_sha,
            state=state,
            next_actions=next_actions,
            created_at=utcnow(),
        )
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO checkpoints(id,goal_id,goal_revision,git_sha,state_json,
                   next_actions_json,created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    checkpoint.id,
                    checkpoint.goal_id,
                    checkpoint.goal_revision,
                    checkpoint.git_sha,
                    _json(checkpoint.state),
                    _json(checkpoint.next_actions),
                    checkpoint.created_at.isoformat(),
                ),
            )
            self._append_event(
                connection,
                goal_id=goal_id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload={
                    "checkpoint_id": checkpoint.id,
                    "git_sha": git_sha,
                    "next_actions": next_actions,
                },
                actor=actor,
                timestamp=checkpoint.created_at,
            )
        return checkpoint

    def latest_checkpoint(self, goal_id: str) -> Checkpoint | None:
        with db.transaction(self.db_path, immediate=False) as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE goal_id=? ORDER BY created_at DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            id=row["id"],
            goal_id=row["goal_id"],
            goal_revision=row["goal_revision"],
            git_sha=row["git_sha"],
            state=json.loads(row["state_json"]),
            next_actions=json.loads(row["next_actions_json"]),
            created_at=_dt(row["created_at"]),
        )

    # ------------------------------------------------------------ benchmarks
    def add_benchmark_record(self, record: BenchmarkRecord) -> None:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO benchmark_records(id,benchmark,mode,case_id,repeat,agent,
                   verified_success,false_completion,duration_seconds,cost_usd,tokens,context_mode,
                   recovery_enabled,reviewer_enabled,parallel_enabled,error,metadata_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.id,
                    record.benchmark,
                    record.mode,
                    record.case_id,
                    record.repeat,
                    record.agent,
                    int(record.verified_success),
                    int(record.false_completion),
                    record.duration_seconds,
                    record.cost_usd,
                    record.tokens,
                    record.context_mode,
                    int(record.recovery_enabled),
                    int(record.reviewer_enabled),
                    int(record.parallel_enabled),
                    record.error,
                    _json(record.metadata),
                    record.created_at.isoformat(),
                ),
            )

    def list_benchmark_records(self, benchmark: str | None = None) -> list[BenchmarkRecord]:
        with db.transaction(self.db_path, immediate=False) as connection:
            if benchmark:
                rows = connection.execute(
                    "SELECT * FROM benchmark_records WHERE benchmark=? ORDER BY created_at",
                    (benchmark,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM benchmark_records ORDER BY created_at"
                ).fetchall()
        return [
            BenchmarkRecord(
                id=row["id"],
                benchmark=row["benchmark"],
                mode=row["mode"],
                case_id=row["case_id"],
                repeat=row["repeat"],
                agent=row["agent"],
                verified_success=bool(row["verified_success"]),
                false_completion=bool(row["false_completion"]),
                duration_seconds=row["duration_seconds"],
                cost_usd=row["cost_usd"],
                tokens=row["tokens"],
                context_mode=row["context_mode"],
                recovery_enabled=bool(row["recovery_enabled"]),
                reviewer_enabled=bool(row["reviewer_enabled"]),
                parallel_enabled=bool(row["parallel_enabled"]),
                error=row["error"],
                metadata=json.loads(row["metadata_json"]),
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    # -------------------------------------------------------------- policies
    def add_policy_candidate(self, candidate: PolicyCandidate) -> None:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO policy_candidates(id,name,version,config_json,active,
                   validation_json,created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    candidate.id,
                    candidate.name,
                    candidate.version,
                    _json(candidate.config),
                    int(candidate.active),
                    _json(candidate.validation),
                    candidate.created_at.isoformat(),
                ),
            )

    def activate_policy(self, candidate_id: str, *, actor: str = "cli") -> None:
        audit_goal = self.list_goals()
        with db.transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM policy_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"policy candidate not found: {candidate_id}")
            validation = json.loads(row["validation_json"])
            if not validation.get("approved", False):
                raise ValidationFailure("policy candidate has not passed offline approval thresholds")
            connection.execute(
                "UPDATE policy_candidates SET active=0 WHERE name=?", (row["name"],)
            )
            connection.execute(
                "UPDATE policy_candidates SET active=1 WHERE id=?", (candidate_id,)
            )
        if audit_goal:
            self.append_event(
                goal_id=audit_goal[0].id,
                event_type=EventType.POLICY_ACTIVATED,
                payload={
                    "candidate_id": candidate_id,
                    "name": row["name"],
                    "version": row["version"],
                },
                actor=actor,
            )

    def rollback_policy(self, name: str, *, actor: str = "cli") -> PolicyCandidate | None:
        audit_goal = self.list_goals()
        with db.transaction(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM policy_candidates WHERE name=? ORDER BY version DESC", (name,)
            ).fetchall()
            active = next((row for row in rows if row["active"]), None)
            previous = next(
                (
                    row
                    for row in rows
                    if not row["active"]
                    and (active is None or row["version"] < active["version"])
                ),
                None,
            )
            connection.execute("UPDATE policy_candidates SET active=0 WHERE name=?", (name,))
            if previous:
                connection.execute(
                    "UPDATE policy_candidates SET active=1 WHERE id=?", (previous["id"],)
                )
        if audit_goal:
            self.append_event(
                goal_id=audit_goal[0].id,
                event_type=EventType.POLICY_ROLLED_BACK,
                payload={"name": name, "restored_candidate": previous["id"] if previous else None},
                actor=actor,
            )
        return self._policy_from_row(previous) if previous else None

    def _policy_audit_goal(self) -> str:
        goals = self.list_goals()
        if goals:
            return goals[0].id
        raise ValidationFailure("policy activation requires an existing goal for the audit trail")

    def _policy_from_row(self, row: sqlite3.Row) -> PolicyCandidate:
        return PolicyCandidate(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            config=json.loads(row["config_json"]),
            created_at=_dt(row["created_at"]),
            active=bool(row["active"]),
            validation=json.loads(row["validation_json"]),
        )

    def list_policies(self) -> list[PolicyCandidate]:
        with db.transaction(self.db_path, immediate=False) as connection:
            rows = connection.execute(
                "SELECT * FROM policy_candidates ORDER BY name, version"
            ).fetchall()
        return [self._policy_from_row(row) for row in rows]

    # -------------------------------------------------------------- decisions
    def add_decision(self, decision: Decision) -> None:
        with db.transaction(self.db_path) as connection:
            connection.execute(
                """INSERT INTO decisions(id,title,context,decision,alternatives_json,rollback,
                   created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    decision.id,
                    decision.title,
                    decision.context,
                    decision.decision,
                    _json(decision.alternatives),
                    decision.rollback,
                    decision.created_at.isoformat(),
                ),
            )

    def get_artifact_hashes(self, goal_id: str) -> dict[str, str]:
        return {artifact.path: artifact.sha256 for artifact in self.list_artifacts(goal_id)}
