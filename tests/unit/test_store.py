from __future__ import annotations

import threading
import time

import pytest

from forgeflow.domain.enums import (
    AttemptStatus,
    ErrorCategory,
    EventType,
    GateKind,
    GateStatus,
    GoalStatus,
    TaskStatus,
    TrustLevel,
)
from forgeflow.domain.errors import ClaimConflictError, ConflictError, LeaseExpiredError
from forgeflow.domain.models import (
    AcceptanceCriterion,
    Evidence,
    GateEvaluation,
    GoalSpec,
    TaskSpec,
    VerifierConfig,
    new_id,
    utcnow,
)
from forgeflow.state.store import Store
from tests.conftest import calc_spec, python_command


def _dependency_spec() -> GoalSpec:
    return GoalSpec(
        title="dependent",
        objective="verify dependency release",
        acceptance=[
            AcceptanceCriterion(id="adds", statement="adds", gate="unit", required=True)
        ],
        tasks=[
            TaskSpec(
                key="first",
                title="first",
                gates=[GateKind.UNIT],
                acceptance_ids=["adds"],
                owned_paths=["calc.py"],
            ),
            TaskSpec(
                key="second",
                title="second",
                depends_on=["first"],
                gates=[GateKind.UNIT],
                acceptance_ids=["adds"],
            ),
        ],
        verifier=VerifierConfig(commands=[python_command()]),
    )


def _passing_evidence(store: Store, goal_id: str, task_id: str, attempt_id: str) -> Evidence:
    now = utcnow()
    evidence = Evidence(
        id=new_id("ev"),
        goal_id=goal_id,
        goal_revision=1,
        task_id=task_id,
        task_attempt_id=attempt_id,
        commit_sha="HEAD",
        tree_hash="tree",
        diff_hash="diff",
        source_fingerprint="source",
        protected_fingerprint="protected",
        env_digest="env",
        argv=["pytest"],
        cwd=".",
        exit_code=0,
        timed_out=False,
        stdout_sha256="a",
        stderr_sha256="b",
        report_sha256="c",
        log_dir=".",
        runner_id=new_id("run"),
        runner_kind="trusted",
        trust=TrustLevel.TRUSTED_RUNNER,
        gate=GateKind.UNIT,
        scope=["adds"],
        verifier_policy_version="v1",
        created_at=now,
    )
    store.add_evidence(evidence)
    return evidence


def test_goal_creation_events_and_projection(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    tasks = store.list_tasks(goal.id)
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.READY
    events = store.list_events(goal.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].type == EventType.GOAL_CREATED
    assert store.project_state(goal.id)["tasks"]["fix-add"]["status"] == TaskStatus.READY.value
    assert store.projection_snapshot(goal.id) == store.project_state(goal.id)


def test_claim_is_atomic_across_two_stores(project) -> None:
    store_one = Store(project)
    store_two = Store(project / ".." / project.name, db_path=store_one.db_path)
    goal = store_one.create_goal(calc_spec())
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(store: Store) -> None:
        barrier.wait()
        try:
            claim = store.claim_task(goal.id, owner="w", lease_seconds=30)
            results.append(claim.task.key)
        except ClaimConflictError:
            results.append("conflict")

    threads = [
        threading.Thread(target=worker, args=(store_one,)),
        threading.Thread(target=worker, args=(store_two,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict", "fix-add"]


def test_expired_lease_prevents_write_and_is_interrupted(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    claim = store.claim_task(goal.id, owner="w", lease_seconds=0.05)
    time.sleep(0.1)
    with pytest.raises(LeaseExpiredError):
        store.settle_success(
            goal.id,
            task_id=claim.task.id,
            attempt_id=claim.attempt.id,
            owner="w",
            lease_token=claim.lease_token,
        )
    expired = store.expire_leases(goal.id)
    assert expired[0].status == TaskStatus.INTERRUPTED
    attempt = store.list_attempts(goal.id)[0]
    assert attempt.status == AttemptStatus.INTERRUPTED
    resumed = store.resume_goal(goal.id)
    assert resumed.status == GoalStatus.RUNNING
    assert store.get_task(goal.id, "fix-add").status == TaskStatus.READY


def test_fencing_token_mismatch_is_rejected(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    claim = store.claim_task(goal.id, owner="w")
    with pytest.raises(LeaseExpiredError):
        store.settle_success(
            goal.id,
            task_id=claim.task.id,
            attempt_id=claim.attempt.id,
            owner="w",
            lease_token="wrong-token",
        )


def test_failure_retry_then_terminal(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    first = store.claim_task(goal.id, owner="w")
    task = store.settle_failure(
        goal.id,
        task_id=first.task.id,
        attempt_id=first.attempt.id,
        owner="w",
        lease_token=first.lease_token,
        error_category=ErrorCategory.TEST,
        error_message="assertion failed",
    )
    assert task.status == TaskStatus.READY
    second = store.claim_task(goal.id, owner="w")
    task = store.settle_failure(
        goal.id,
        task_id=second.task.id,
        attempt_id=second.attempt.id,
        owner="w",
        lease_token=second.lease_token,
        error_category=ErrorCategory.TEST,
        error_message="assertion failed again",
    )
    assert task.status == TaskStatus.FAILED


def test_dependency_not_released_until_verified(project) -> None:
    store = Store(project)
    goal = store.create_goal(_dependency_spec())
    assert [task.key for task in store.ready_tasks(goal.id)] == ["first"]
    with pytest.raises(ClaimConflictError):
        store.claim_task(goal.id, owner="w", task_key="second")
    claim = store.claim_task(goal.id, owner="w", task_key="first")
    store.settle_success(
        goal.id,
        task_id=claim.task.id,
        attempt_id=claim.attempt.id,
        owner="w",
        lease_token=claim.lease_token,
    )
    evidence = _passing_evidence(store, goal.id, claim.task.id, claim.attempt.id)
    evaluation = GateEvaluation(
        id=new_id("gate"),
        goal_id=goal.id,
        goal_revision=1,
        gate=GateKind.UNIT,
        criterion_id="adds",
        status=GateStatus.PASS,
        reason="ok",
        evidence_ids=[evidence.id],
        source_fingerprint="source",
        policy_version="v1",
        evaluated_at=utcnow(),
    )
    store.save_gate_evaluation(evaluation)
    store.mark_task_verified(
        goal.id,
        task_id=claim.task.id,
        attempt_id=claim.attempt.id,
        evidence_ids=[evidence.id],
        evaluated_gates=[evaluation],
    )
    statuses = store.task_status_map(goal.id)
    assert statuses["first"] == TaskStatus.VERIFIED
    assert statuses["second"] == TaskStatus.READY
    assert [task.key for task in store.ready_tasks(goal.id)] == ["second"]
    assert store.projection_snapshot(goal.id) == store.project_state(goal.id)


def test_side_effecting_unknown_is_blocked_not_replayed(project) -> None:
    store = Store(project)
    spec = calc_spec()
    spec = spec.model_copy(
        update={
            "tasks": [
                spec.tasks[0].model_copy(
                    update={
                        "side_effecting": True,
                        "idempotency_key": "charge-123",
                    }
                )
            ]
        }
    )
    goal = store.create_goal(spec)
    store.claim_task(goal.id, owner="w", lease_seconds=0.05)
    time.sleep(0.1)
    expired = store.expire_leases(goal.id)
    assert expired[0].status == TaskStatus.BLOCKED
    store.resume_goal(goal.id)
    assert store.get_task(goal.id, "fix-add").status == TaskStatus.BLOCKED


def test_illegal_goal_transition_is_rejected(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    with pytest.raises(ConflictError):
        store.set_goal_status(goal.id, GoalStatus.DELIVERED, reason="skip work")


def test_event_idempotency(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    event_id = "evt_fixed"
    first = store.append_event(
        goal.id,
        EventType.GOAL_STATUS_CHANGED,
        {"from": "PLANNED", "to": "PLANNED", "reason": "noop"},
        actor="test",
        event_id=event_id,
    )
    second = store.append_event(
        goal.id,
        EventType.GOAL_STATUS_CHANGED,
        {"from": "PLANNED", "to": "PLANNED", "reason": "noop"},
        actor="test",
        event_id=event_id,
    )
    assert first.event_id == second.event_id
    assert len([event for event in store.list_events(goal.id) if event.event_id == event_id]) == 1


def test_pause_cancel_and_projection_after_verification(project) -> None:
    store = Store(project)
    goal = store.create_goal(_dependency_spec())
    store.claim_task(goal.id, owner="w", task_key="first")
    store.pause_goal(goal.id)
    assert store.get_goal(goal.id).status == GoalStatus.PAUSED
    cancelled = store.cancel_goal(goal.id, reason="test cancel")
    assert cancelled.status == GoalStatus.CANCELLED
    assert store.get_task(goal.id, "first").status == TaskStatus.CANCELLED
    assert store.projection_snapshot(goal.id) == store.project_state(goal.id)
