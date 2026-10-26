from __future__ import annotations

from enum import StrEnum


class GoalStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class AttemptStatus(StrEnum):
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class GateStatus(StrEnum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    NOT_RUN = "NOT_RUN"


class GateKind(StrEnum):
    STATIC_TYPE = "static_type"
    LINT = "lint"
    UNIT = "unit"
    INTEGRATION = "integration"
    REGRESSION = "regression"
    CONTRACT = "contract"
    BUILD = "build"
    SECURITY = "security"
    ARTIFACT = "artifact"
    MANUAL = "manual"
    HARDWARE = "hardware"


class RiskLevel(StrEnum):
    DOCS = "docs"
    STANDARD_CODE = "standard_code"
    CRITICAL_MODULE = "critical_module"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class EventType(StrEnum):
    GOAL_CREATED = "GoalCreated"
    GOAL_PLANNED = "GoalPlanned"
    GOAL_STATUS_CHANGED = "GoalStatusChanged"
    TASK_READY = "TaskReady"
    TASK_CLAIMED = "TaskClaimed"
    TASK_LEASE_RENEWED = "TaskLeaseRenewed"
    TASK_LEASE_EXPIRED = "TaskLeaseExpired"
    ATTEMPT_STARTED = "AttemptStarted"
    ATTEMPT_SETTLED = "AttemptSettled"
    ARTIFACT_BUILT = "ArtifactBuilt"
    EVIDENCE_RECORDED = "EvidenceRecorded"
    GATE_EVALUATED = "GateEvaluated"
    TASK_BLOCKED = "TaskBlocked"
    TASK_VERIFIED = "TaskVerified"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    CHECKPOINT_CREATED = "CheckpointCreated"
    GOAL_RESUMED = "GoalResumed"
    GOAL_CANCELLED = "GoalCancelled"
    RECOVERY_ATTEMPTED = "RecoveryAttempted"
    POLICY_ACTIVATED = "PolicyActivated"
    POLICY_ROLLED_BACK = "PolicyRolledBack"


class ErrorCategory(StrEnum):
    COMPILE = "compile"
    TYPE = "type"
    LINT = "lint"
    TEST = "test"
    DEPENDENCY = "dependency"
    ENVIRONMENT = "environment"
    NETWORK = "network"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    PERMISSION = "permission"
    CONTRACT = "contract"
    EXTERNAL_BLOCKER = "external_blocker"
    UNKNOWN = "unknown"


class TrustLevel(StrEnum):
    SELF_REPORTED = "self_reported"
    TRUSTED_RUNNER = "trusted_runner"
    CLEAN_ROOM = "clean_room"
    MANUAL_SIGNED = "manual_signed"


class AgentRunStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CRASHED = "CRASHED"
    EXTERNAL_BLOCKED = "EXTERNAL_BLOCKED"
