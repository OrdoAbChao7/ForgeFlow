from __future__ import annotations

import pytest

from forgeflow.domain.enums import RiskLevel
from forgeflow.domain.errors import CycleDetectedError, ValidationFailure
from forgeflow.domain.models import (
    AcceptanceCriterion,
    GoalSpec,
    TaskSpec,
    VerifierConfig,
)
from forgeflow.planner.compiler import compile_goal, compile_natural_language, load_goal_spec
from forgeflow.planner.graph import (
    apply_quick_mode,
    ready_task_keys,
    task_id_for,
    topological_waves,
    validate_ownership,
)


def _spec(tasks: list[TaskSpec], risk: RiskLevel = RiskLevel.STANDARD_CODE) -> GoalSpec:
    return GoalSpec(
        title="t",
        objective="o",
        risk=risk,
        acceptance=[
            AcceptanceCriterion(id="done", statement="done", gate="manual", required=True)
        ],
        tasks=tasks,
        verifier=VerifierConfig(),
    )


def test_cycle_is_rejected() -> None:
    spec = _spec(
        [
            TaskSpec(key="a", title="a", depends_on=["b"]),
            TaskSpec(key="b", title="b", depends_on=["a"]),
        ]
    )
    with pytest.raises(CycleDetectedError):
        topological_waves(spec)


def test_self_dependency_is_rejected_by_schema() -> None:
    with pytest.raises(ValueError):
        _spec([TaskSpec(key="a", title="a", depends_on=["a"])])


def test_topological_waves_and_ready_frontier() -> None:
    spec = _spec(
        [
            TaskSpec(key="a", title="a"),
            TaskSpec(key="b", title="b", depends_on=["a"]),
            TaskSpec(key="c", title="c", depends_on=["a", "b"]),
            TaskSpec(key="d", title="d"),
        ]
    )
    assert topological_waves(spec) == [["a", "d"], ["b"], ["c"]]
    statuses = {"a": "VERIFIED", "b": "READY", "c": "PENDING", "d": "PENDING"}
    assert ready_task_keys(spec, statuses) == ["b", "d"]
    statuses = {"a": "PENDING", "b": "PENDING", "c": "PENDING", "d": "READY"}
    assert ready_task_keys(spec, statuses) == ["a", "d"]


def test_soft_dependency_does_not_block_readiness() -> None:
    spec = _spec(
        [
            TaskSpec(key="a", title="a"),
            TaskSpec(key="b", title="b", soft_depends_on=["a"]),
        ]
    )
    assert ready_task_keys(spec, {"a": "PENDING", "b": "PENDING"}) == ["a", "b"]


def test_quick_mode_preserves_gates_and_rejects_critical_risk() -> None:
    spec = _spec([TaskSpec(key="a", title="a", max_attempts=5)])
    quick = apply_quick_mode(spec)
    assert quick.tasks[0].max_attempts == 1
    assert quick.acceptance == spec.acceptance
    critical = _spec([TaskSpec(key="a", title="a")], RiskLevel.CRITICAL_MODULE)
    with pytest.raises(ValidationFailure):
        apply_quick_mode(critical)


def test_yaml_compile_and_deterministic_text_compile(tmp_path) -> None:
    target = tmp_path / "goal.yaml"
    target.write_text(
        """
goal:
  title: Demo
  objective: Do a thing
  acceptance:
    - id: done
      statement: It is done
      gate: manual
  tasks:
    - key: do
      title: Do
      gates: [manual]
  verifier:
    commands: []
""",
        encoding="utf-8",
    )
    spec = load_goal_spec(target)
    assert spec.tasks[0].key == "do"
    compiled = compile_goal(file=target)
    assert compiled.title == "Demo"
    natural = compile_natural_language("Make the parser deterministic")
    assert natural.metadata["compiler"] == "deterministic_text_v1"
    assert natural.tasks[0].gates == ["manual"]


def test_invalid_task_references_are_rejected() -> None:
    target_tasks = [TaskSpec(key="a", title="a", depends_on=["missing"])]
    with pytest.raises(ValueError):
        _spec(target_tasks)


def test_ownership_overlap_warns() -> None:
    warnings = validate_ownership(
        [
            TaskSpec(key="a", title="a", owned_paths=["src/"]),
            TaskSpec(key="b", title="b", owned_paths=["src/"]),
        ]
    )
    assert warnings


def test_task_id_is_deterministic() -> None:
    assert task_id_for("goal_x", "parse") == task_id_for("goal_x", "parse")
    assert task_id_for("goal_x", "parse") != task_id_for("goal_x", "other")
