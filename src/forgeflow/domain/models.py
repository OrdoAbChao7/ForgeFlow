from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forgeflow.domain.enums import (
    AgentRunStatus,
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


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


_TASK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AcceptanceCriterion(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1)
    gate: GateKind
    required: bool = True

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _TASK_KEY_RE.match(value):
            raise ValueError("criterion id must match [a-z0-9][a-z0-9_-]{0,63}")
        return value


class VerifierCommand(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    gate: GateKind
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    scope: list[str] = Field(default_factory=list)
    required: bool = True
    cwd: str = "."
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def no_shell_string(cls, value: list[str]) -> list[str]:
        if any("\x00" in arg for arg in value):
            raise ValueError("argv must not contain NUL bytes")
        return value


class VerifierConfig(StrictModel):
    policy_version: str = "ff-verifier-v1"
    commands: list[VerifierCommand] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    clean_room: bool = True
    setup_argv: list[str] | None = None
    network: bool = False


class TaskSpec(StrictModel):
    key: str
    title: str = Field(min_length=1)
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    soft_depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)
    owned_paths: list[str] = Field(default_factory=list)
    gates: list[GateKind] = Field(default_factory=list)
    acceptance_ids: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=10)
    side_effecting: bool = False
    idempotency_key: str | None = None
    agent: str = "mock"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _TASK_KEY_RE.match(value):
            raise ValueError("task key must match [a-z0-9][a-z0-9_-]{0,63}")
        return value

    @model_validator(mode="after")
    def validate_side_effect(self) -> TaskSpec:
        if self.side_effecting and not self.idempotency_key:
            raise ValueError("side_effecting tasks require an idempotency_key")
        return self


class GoalSpec(StrictModel):
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    risk: RiskLevel = RiskLevel.STANDARD_CODE
    constraints: list[str] = Field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = Field(min_length=1)
    tasks: list[TaskSpec] = Field(min_length=1)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    revision_note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph_references(self) -> GoalSpec:
        keys = [task.key for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate task keys")
        known = set(keys)
        for task in self.tasks:
            unknown = (set(task.depends_on) | set(task.soft_depends_on)) - known
            if unknown:
                raise ValueError(f"task {task.key!r} references unknown tasks: {sorted(unknown)}")
            if task.key in task.depends_on or task.key in task.soft_depends_on:
                raise ValueError(f"task {task.key!r} cannot depend on itself")
        criteria = {criterion.id for criterion in self.acceptance}
        task_criteria = {item for task in self.tasks for item in task.acceptance_ids}
        unknown_criteria = task_criteria - criteria
        if unknown_criteria:
            raise ValueError(f"unknown acceptance ids: {sorted(unknown_criteria)}")
        command_scopes = {item for cmd in self.verifier.commands for item in cmd.scope}
        unknown_scope = command_scopes - criteria
        if unknown_scope:
            raise ValueError(f"verifier commands reference unknown criteria: {sorted(unknown_scope)}")
        return self


class Goal(StrictModel):
    id: str
    title: str
    objective: str
    status: GoalStatus
    risk: RiskLevel
    workspace: str
    current_revision: int = 1
    event_seq: int = 0
    quick_mode: bool = False
    baseline_commit: str = "UNKNOWN"
    created_at: datetime
    updated_at: datetime


class GoalRevision(StrictModel):
    id: str
    goal_id: str
    revision: int
    spec: GoalSpec
    contract_digest: str
    created_at: datetime


class Task(StrictModel):
    id: str
    goal_id: str
    key: str
    title: str
    description: str = ""
    status: TaskStatus
    priority: int
    max_attempts: int
    attempts_count: int = 0
    owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    version: int = 0
    created_at: datetime
    updated_at: datetime


class Dependency(StrictModel):
    goal_id: str
    task_id: str
    depends_on: str
    hard: bool = True


class TaskAttempt(StrictModel):
    id: str
    goal_id: str
    task_id: str
    attempt_no: int
    status: AttemptStatus
    owner: str
    lease_token: str
    agent: str
    started_at: datetime
    ended_at: datetime | None = None
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    side_effecting: bool = False
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(StrictModel):
    id: str
    goal_id: str
    task_id: str | None = None
    path: str
    sha256: str
    size: int
    kind: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(StrictModel):
    id: str
    goal_id: str
    goal_revision: int
    task_id: str | None = None
    task_attempt_id: str | None = None
    commit_sha: str
    tree_hash: str
    diff_hash: str
    source_fingerprint: str
    protected_fingerprint: str
    env_digest: str
    argv: list[str]
    cwd: str
    exit_code: int
    timed_out: bool = False
    stdout_sha256: str
    stderr_sha256: str
    report_sha256: str
    log_dir: str
    runner_id: str
    runner_kind: str
    trust: TrustLevel
    gate: GateKind
    scope: list[str] = Field(default_factory=list)
    verifier_policy_version: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def observed_pass(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class GateEvaluation(StrictModel):
    id: str
    goal_id: str
    goal_revision: int
    gate: GateKind
    criterion_id: str | None = None
    status: GateStatus
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    source_fingerprint: str
    policy_version: str
    evaluated_at: datetime


class Checkpoint(StrictModel):
    id: str
    goal_id: str
    goal_revision: int
    git_sha: str
    state: dict[str, Any]
    next_actions: list[str]
    created_at: datetime


class Event(StrictModel):
    event_id: str
    sequence: int
    goal_id: str
    task_id: str | None = None
    type: EventType
    timestamp: datetime
    correlation_id: str
    revision: int
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


class BenchmarkRecord(StrictModel):
    id: str
    benchmark: str
    mode: str
    case_id: str
    repeat: int
    agent: str
    verified_success: bool
    false_completion: bool
    duration_seconds: float
    cost_usd: float = 0.0
    tokens: int = 0
    context_mode: str = "structure"
    recovery_enabled: bool = False
    reviewer_enabled: bool = False
    parallel_enabled: bool = False
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PolicyCandidate(StrictModel):
    id: str
    name: str
    version: int
    config: dict[str, Any]
    created_at: datetime
    active: bool = False
    validation: dict[str, Any] = Field(default_factory=dict)


class Decision(StrictModel):
    id: str
    title: str
    context: str
    decision: str
    alternatives: list[str] = Field(default_factory=list)
    rollback: str = ""
    created_at: datetime


class AgentEvent(StrictModel):
    run_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int = 0
    timestamp: datetime = Field(default_factory=utcnow)


class AgentSettlement(StrictModel):
    run_id: str
    status: AgentRunStatus
    success_claimed: bool = False
    error: str | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    duration_seconds: float = 0.0
    output: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class GateDefinition(StrictModel):
    gate: GateKind
    required: bool = True
    criterion_ids: list[str] = Field(default_factory=list)
    command_ids: list[str] = Field(default_factory=list)


class PlanSummary(StrictModel):
    goal_id: str
    revision: int
    tasks: list[str]
    ready: list[str]
    waves: list[list[str]]
    risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StatusReport(StrictModel):
    goal_id: str
    title: str
    status: GoalStatus
    revision: int
    tasks: dict[str, int]
    gates: dict[str, GateStatus]
    blocked: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    git_sha: str = ""
    source_fingerprint: str = ""


class DeliveryManifest(StrictModel):
    format_version: int = 1
    generated_at: datetime
    forgeflow_version: str
    goal_id: str
    goal_title: str
    revision: int
    git_sha: str
    source_fingerprint: str
    state: Literal["READY", "BLOCKED", "NOT_READY"]
    gates: list[GateEvaluation]
    artifacts: list[Artifact]
    evidence_index: list[dict[str, Any]]
    test_commands: list[dict[str, Any]]
    feature_scope: list[str] = Field(default_factory=list)
    dependencies: dict[str, str] = Field(default_factory=dict)
    unverified: list[str] = Field(default_factory=list)
    regeneration_commands: list[str] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
