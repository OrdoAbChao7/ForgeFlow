from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from forgeflow.domain.enums import ErrorCategory, GoalStatus, TaskStatus
from forgeflow.recovery.engine import (
    FailureClassifier,
    RecoveryEngine,
    RecoveryPolicyError,
)
from forgeflow.runtime import Harness, RunOptions
from forgeflow.state.store import Store
from tests.conftest import calc_spec


def test_failure_classifier_rules() -> None:
    classifier = FailureClassifier()
    assert (
        classifier.classify(exit_code=1, stderr="ModuleNotFoundError: no module named x")
        == ErrorCategory.EXTERNAL_BLOCKER
    )
    assert classifier.classify(exit_code=1, stderr="SyntaxError: bad") == ErrorCategory.COMPILE
    assert classifier.classify(exit_code=1, stderr="AssertionError: nope") == ErrorCategory.TEST
    assert classifier.classify(exit_code=124) == ErrorCategory.TIMEOUT
    assert classifier.classify(exit_code=1, stderr="unknown") == ErrorCategory.TEST


def test_deleted_test_files_are_rejected(project: Path) -> None:
    engine = RecoveryEngine(Store(project), project)
    (project / "tests" / "test_calc.py").unlink()
    with pytest.raises(RecoveryPolicyError) as exc:
        engine.guard_repair_diff()
    assert "deleted test files" in str(exc.value)


def test_oversized_repair_is_rejected(project: Path) -> None:
    engine = RecoveryEngine(Store(project), project)
    target = project / "calc.py"
    target.write_text("\n".join(f"VALUE_{i} = {i}" for i in range(2500)), encoding="utf-8")
    with pytest.raises(RecoveryPolicyError) as exc:
        engine.guard_repair_diff()
    assert "bounded" in str(exc.value)


def test_same_error_circuit_breaker_bounds_attempts(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec(mock_files={"calc.py": "def add(a, b):\n    return a * b\n"})
    spec = spec.model_copy(
        update={
            "tasks": [
                spec.tasks[0].model_copy(update={"max_attempts": 3}),
            ]
        }
    )
    goal = harness.create_goal(spec)
    harness.run(goal.id, options=RunOptions(agent_name="mock", recovery=True))
    attempts = harness.store.list_attempts(goal.id)
    assert len(attempts) == 3
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.FAILED
    recovery_events = [
        event
        for event in harness.store.list_events(goal.id)
        if event.type.value == "RecoveryAttempted"
    ]
    assert len(recovery_events) == 3


def test_recovery_disabled_stops_after_first_failure(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec(mock_files={"calc.py": "def add(a, b):\n    return a * b\n"})
    goal = harness.create_goal(spec)
    harness.run(goal.id, options=RunOptions(agent_name="mock", recovery=False))
    attempts = harness.store.list_attempts(goal.id)
    assert len(attempts) == 1
    assert harness.store.get_task(goal.id, "fix-add").status == TaskStatus.FAILED


def test_external_blocker_does_not_stop_independent_task(project: Path) -> None:
    harness = Harness(project)
    base = calc_spec()
    failure_task = base.tasks[0].model_copy(
        update={
            "key": "external-step",
            "priority": 90,
            "metadata": {"mock": {"failure": "permission denied: external service"}},
            "owned_paths": [],
        }
    )
    success_task = base.tasks[0].model_copy(
        update={
            "key": "independent-step",
            "priority": 10,
            "metadata": {
                "mock": {"files": {"calc.py": "def add(a, b):\n    return a + b\n"}}
            },
        }
    )
    spec = base.model_copy(update={"tasks": [failure_task, success_task]})
    goal = harness.create_goal(spec)
    harness.run(goal.id, options=RunOptions(agent_name="mock", recovery=True))
    statuses = harness.store.task_status_map(goal.id)
    assert statuses["external-step"] == TaskStatus.BLOCKED
    assert statuses["independent-step"] == TaskStatus.VERIFIED
    assert harness.store.get_goal(goal.id).status == GoalStatus.BLOCKED


def test_process_hard_kill_then_resume(project: Path) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    script = Path(__file__).resolve().parents[1] / "fixtures" / "crash_claim.py"
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    result = subprocess.run(
        [sys.executable, str(script), str(project), goal.id, "0.6"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, **env},
    )
    assert result.returncode == 17
    assert result.stdout.strip()
    time.sleep(0.9)
    harness = Harness(project)
    resumed = harness.resume(goal.id)
    assert resumed["interrupted"] == ["fix-add"]
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.READY
    assert harness.store.list_attempts(goal.id)[0].status.value == "INTERRUPTED"
    second = harness.store.claim_task(goal.id, owner="recovered", lease_seconds=30)
    assert second.task.key == "fix-add"
    harness.store.settle_success(
        goal.id,
        task_id=second.task.id,
        attempt_id=second.attempt.id,
        owner="recovered",
        lease_token=second.lease_token,
    )
    assert harness.store.projection_snapshot(goal.id) == harness.store.project_state(goal.id)


def test_transaction_rolls_back_on_event_failure(project: Path, monkeypatch) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())

    def explode(*args, **kwargs):
        raise RuntimeError("simulated crash while appending event")

    monkeypatch.setattr(store, "_append_event", explode)
    with pytest.raises(RuntimeError):
        store.claim_task(goal.id, owner="crash", lease_seconds=30)
    task = store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.READY
    assert task.attempts_count == 0
    assert store.list_attempts(goal.id) == []


def test_side_effect_unknown_never_replays(project: Path) -> None:
    store = Store(project)
    spec = calc_spec()
    spec = spec.model_copy(
        update={
            "tasks": [
                spec.tasks[0].model_copy(
                    update={"side_effecting": True, "idempotency_key": "charge-123"}
                )
            ]
        }
    )
    goal = store.create_goal(spec)
    store.claim_task(goal.id, owner="w", lease_seconds=0.05)
    time.sleep(0.1)
    harness = Harness(project, store=store)
    report = harness.resume(goal.id)
    assert harness.store.get_task(goal.id, "fix-add").status == TaskStatus.BLOCKED
    actions = report["next_actions"] + harness.recover(goal.id)["actions"]
    assert any("manual reconciliation" in action for action in actions)
