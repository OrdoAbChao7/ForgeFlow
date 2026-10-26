from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import BenchmarkRecord, new_id, utcnow
from forgeflow.evaluation.benchmark import (
    MODES,
    BenchmarkSuite,
    HarborBridge,
)
from forgeflow.evaluation.policy import PolicyOptimizer, ValidationThresholds
from forgeflow.runtime import Harness, RunOptions
from forgeflow.state.store import Store
from tests.conftest import calc_spec


@pytest.mark.slow
def test_benchmark_is_reproducible_and_records_not_run_baselines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selected = tuple(
        mode for mode in MODES if mode.key in {"C_core", "ablation_no_context", "ablation_no_recovery"}
    )
    suite = BenchmarkSuite(
        workspace,
        cases_dir=Path("benchmarks/cases").resolve(),
        output_root=workspace / "evidence" / "benchmarks",
        repeats=1,
        token_budget=900,
        modes=selected,
    )
    summary = suite.run(case_ids=["add_fix", "recover_once"])
    output = Path(summary["output_dir"])
    assert (output / "runs.jsonl").is_file()
    assert (output / "summary.csv").is_file()
    assert (output / "summary.md").is_file()
    assert (output / "manifest.json").is_file()
    rows = {row["mode"]: row for row in summary["rows"]}
    assert rows["C_core"]["verified_success_rate"] == 1.0
    assert rows["ablation_no_context"]["verified_success_rate"] < 1.0
    assert rows["ablation_no_context"]["false_completion_rate"] > 0
    assert rows["ablation_no_recovery"]["verified_success_rate"] < 1.0
    assert {item["status"] for item in summary["baselines"]} == {"NOT_RUN"}
    assert summary["harbor"]["status"] in {"EXTERNAL_BLOCKED", "NOT_RUN"}
    raw = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(raw) == 2 * 3
    assert all(record["agent"] == "context_oracle_sim" for record in raw)
    missing = [record for record in raw if not record.get("evidence")]
    assert not missing, [record.get("error") for record in missing]
    archived = [
        artifact
        for record in raw
        for artifact in record["evidence"]
        if artifact.get("archived_hashes")
    ]
    assert archived
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["git_sha"]
    assert manifest["artifacts"]
    assert summary["parallel_ablation"]["status"] == "PASS"
    assert manifest["parallel_ablation"]["status"] == "PASS"
    assert set(manifest["not_run"]) == {"A_native_agent", "B_openspec_gsd"}


def test_harbor_bridge_never_fakes_availability() -> None:
    status = HarborBridge().status()
    assert status["status"] in {"EXTERNAL_BLOCKED", "NOT_RUN"}
    if status["status"] == "EXTERNAL_BLOCKED":
        assert "not installed" in status["reason"]


def _record(
    store: Store,
    *,
    name: str,
    context_mode: str,
    recovery: bool,
    success: bool,
    false_completion: bool = False,
) -> None:
    store.add_benchmark_record(
        BenchmarkRecord(
            id=new_id(name),
            benchmark="policy-test",
            mode=name,
            case_id="recover_once",
            repeat=1,
            agent="context_oracle_sim",
            verified_success=success,
            false_completion=false_completion,
            duration_seconds=0.1,
            context_mode=context_mode,
            recovery_enabled=recovery,
            reviewer_enabled=False,
            parallel_enabled=False,
            metadata={"case_id": "recover_once"},
            created_at=utcnow(),
        )
    )


def test_policy_validation_activation_and_rollback(tmp_path: Path) -> None:
    store = Store(tmp_path)
    goal = store.create_goal(calc_spec())
    for _ in range(4):
        _record(
            store,
            name="structure",
            context_mode="structure",
            recovery=True,
            success=True,
        )
    for _ in range(2):
        _record(
            store,
            name="none",
            context_mode="none",
            recovery=True,
            success=False,
            false_completion=True,
        )
    optimizer = PolicyOptimizer(
        store,
        ValidationThresholds(
            min_runs=4, min_verified_success_rate=0.9, max_false_completion_rate=0.05
        ),
    )
    bad = optimizer.propose("context", {"context_mode": "none", "recovery": True})
    assert bad.validation["approved"] is False
    assert bad.validation["failures"]
    with pytest.raises(ValidationFailure):
        optimizer.activate(bad.id)
    good = optimizer.propose("context", {"context_mode": "structure", "recovery": True})
    assert good.validation["approved"] is True
    optimizer.activate(good.id)
    active = optimizer.active("context")
    assert active is not None and active.id == good.id
    second = optimizer.propose("context", {"context_mode": "structure", "recovery": True})
    assert second.version == good.version + 1
    optimizer.activate(second.id)
    assert optimizer.active("context").id == second.id  # type: ignore[union-attr]
    rolled = optimizer.rollback("context")
    assert rolled is not None and rolled.id == good.id
    assert optimizer.active("context").id == good.id  # type: ignore[union-attr]
    events = store.list_events(goal.id)
    assert any(event.type.value == "PolicyActivated" for event in events)
    assert any(event.type.value == "PolicyRolledBack" for event in events)


def test_policy_rejects_unvalidated_activation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.create_goal(calc_spec())
    optimizer = PolicyOptimizer(store, ValidationThresholds(min_runs=99))
    candidate = optimizer.propose("strict", {"context_mode": "structure", "recovery": True})
    assert candidate.validation["approved"] is False
    with pytest.raises(ValidationFailure):
        optimizer.activate(candidate.id)


def test_active_policy_is_applied_to_runtime(project: Path) -> None:
    store = Store(project)
    harness = Harness(project, store=store)
    goal = harness.create_goal(calc_spec())
    for _ in range(4):
        _record(
            store,
            name="structure",
            context_mode="structure",
            recovery=True,
            success=True,
        )
    optimizer = PolicyOptimizer(store, ValidationThresholds(min_runs=4))
    candidate = optimizer.propose("runtime", {"context_mode": "structure", "recovery": True})
    assert candidate.validation["approved"]
    optimizer.activate(candidate.id)
    report = harness.run(
        goal.id,
        options=RunOptions(agent_name="mock", policy_name="runtime"),
    )
    assert report.policy == {"name": "runtime"}
    assert report.tasks[0].verified
    assert report.context_tokens > 0
    other = harness.create_goal(calc_spec())
    with pytest.raises(ValidationFailure):
        harness.run(other.id, options=RunOptions(agent_name="mock", policy_name="missing"))
