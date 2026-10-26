from __future__ import annotations

import json
from pathlib import Path

from forgeflow.delivery import build_delivery
from forgeflow.domain.enums import GateStatus, GoalStatus, TaskStatus
from forgeflow.runtime import Harness, RunOptions
from tests.conftest import calc_spec


def test_mock_goal_to_delivery_with_real_pytest_output(project: Path) -> None:
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    report = harness.run(goal.id, options=RunOptions(agent_name="mock", context_mode="structure"))
    assert report.goal_status == GoalStatus.VERIFYING
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.VERIFIED
    evidence = harness.store.list_evidence(goal.id)
    assert evidence
    assert all(item.exit_code == 0 for item in evidence)
    assert all(item.trust.value == "trusted_runner" for item in evidence)
    # Real pytest output, not an agent-authored success string.
    assert "passed" in Path(evidence[0].log_dir).parent.joinpath("stdout.txt").read_text(
        encoding="utf-8", errors="replace"
    ).lower()

    verification = harness.verify_goal(goal.id)
    assert verification["status"] == "PASS"
    assert all(value == "PASS" for value in verification["gates"].values())

    manifest, destination = build_delivery(harness.store, project, goal.id)
    assert manifest.state == "READY"
    assert manifest.gates
    assert "manifest.json" in {path.name for path in destination.iterdir()}
    assert harness.store.get_goal(goal.id).status == GoalStatus.DELIVERED
    assert harness.store.projection_snapshot(goal.id) == harness.store.project_state(goal.id)


def test_false_success_is_rejected_and_retried_then_failed(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec(mock_files={"calc.py": "def add(a, b):\n    return a * b\n"})
    goal = harness.create_goal(spec)
    report = harness.run(
        goal.id,
        options=RunOptions(agent_name="mock", context_mode="structure", recovery=True),
    )
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.FAILED
    assert report.tasks
    assert any(item.false_completion for item in report.tasks)
    assert all(not item.verified for item in report.tasks)
    evidence = harness.store.list_evidence(goal.id)
    assert evidence
    assert all(item.exit_code != 0 for item in evidence)
    manifest, _ = build_delivery(harness.store, project, goal.id, mark_delivered=False)
    assert manifest.state == "BLOCKED"
    assert manifest.unverified


def test_self_report_with_no_trusted_verifier_is_blocked(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec()
    spec = spec.model_copy(update={"verifier": spec.verifier.model_copy(update={"commands": []})})
    goal = harness.create_goal(spec)
    report = harness.run(goal.id, options=RunOptions(agent_name="mock"))
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.BLOCKED
    assert report.tasks[0].false_completion
    assert report.tasks[0].agent_claimed_success
    assert not harness.store.list_evidence(goal.id)
    manifest, _ = build_delivery(harness.store, project, goal.id, mark_delivered=False)
    assert manifest.state == "NOT_READY"


def test_modified_tests_are_blocked_by_protected_path_guard(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec(
        mock_files={
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": "def test_add():\n    assert True\n",
        }
    )
    goal = harness.create_goal(spec)
    report = harness.run(goal.id, options=RunOptions(agent_name="mock"))
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.BLOCKED
    assert report.tasks[0].false_completion
    assert "protected" in (task.last_error or "").lower()
    assert not any(item.exit_code == 0 for item in harness.store.list_evidence(goal.id))


def test_owned_path_violation_is_blocked(project: Path) -> None:
    harness = Harness(project)
    spec = calc_spec(
        mock_files={
            "calc.py": "def add(a, b):\n    return a + b\n",
            "extra.py": "VALUE = 1\n",
        }
    )
    goal = harness.create_goal(spec)
    harness.run(goal.id, options=RunOptions(agent_name="mock"))
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.BLOCKED
    assert "outside task ownership" in (task.last_error or "")


def test_evidence_goes_stale_after_source_change(project: Path) -> None:
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    harness.run(goal.id, options=RunOptions(agent_name="mock"))
    assert harness.verify_goal(goal.id)["status"] == "PASS"
    manifest, _ = build_delivery(harness.store, project, goal.id, mark_delivered=False)
    assert manifest.state == "READY"
    calc = project / "calc.py"
    calc.write_text(calc.read_text() + "\n# changed after verification\n", encoding="utf-8")
    from forgeflow.verification.gates import GateEngine, capture_binding

    spec = harness.store.get_spec(goal.id)
    engine = GateEngine(
        harness.store,
        project,
        spec,
        binding=capture_binding(project, spec.verifier.protected_paths),
    )
    stale = engine.refresh_staleness(goal.id)
    assert stale
    manifest, _ = build_delivery(harness.store, project, goal.id, mark_delivered=False)
    assert manifest.state == "BLOCKED"
    assert any(gate.status == GateStatus.STALE for gate in manifest.gates)


def test_clean_room_reverification(project: Path) -> None:
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    harness.run(goal.id, options=RunOptions(agent_name="mock"))
    result = harness.verify_goal(goal.id, clean_room=True)
    assert result["cleanroom"]["success"], json.dumps(result["cleanroom"], indent=2)
    assert result["cleanroom"]["protected_restored"]
    cleanroom_evidence = [
        item for item in harness.store.list_evidence(goal.id) if item.runner_kind == "cleanroom"
    ]
    assert cleanroom_evidence
    assert all(item.trust.value == "clean_room" for item in cleanroom_evidence)


def test_clean_room_setup_failure_blocks_evidence(project: Path) -> None:
    import sys

    harness = Harness(project)
    spec = calc_spec()
    spec = spec.model_copy(
        update={
            "verifier": spec.verifier.model_copy(
                update={
                    "setup_argv": [
                        sys.executable,
                        "-c",
                        "raise SystemExit(9)",
                    ]
                }
            )
        }
    )
    goal = harness.create_goal(spec)
    result = harness.verify_goal(goal.id, clean_room=True)
    assert result["status"] == "FAIL"
    assert result["cleanroom"]["success"] is False
    assert any("setup failed" in error for error in result["cleanroom"]["errors"])
    assert not [
        item for item in harness.store.list_evidence(goal.id) if item.runner_kind == "cleanroom"
    ]


def test_newer_failure_cannot_be_masked_by_previous_pass(project: Path) -> None:
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    harness.run(goal.id, options=RunOptions(agent_name="mock"))
    assert harness.verify_goal(goal.id)["status"] == "PASS"
    (project / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    result = harness.verify_goal(goal.id)
    assert result["status"] == "FAIL"
    assert result["failures"]
    assert result["gates"]["unit"] == "FAIL"


def test_committed_protected_test_tampering_is_blocked(project: Path) -> None:
    from forgeflow.agents.base import AgentAdapter
    from forgeflow.domain.enums import AgentRunStatus
    from forgeflow.domain.models import AgentSettlement

    class CommittingTamper(AgentAdapter):
        name = "committing_tamper"

        def submit(self, run_id: str, prompt: str) -> str:
            record = self._get(run_id)
            record.workspace.joinpath("calc.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            record.workspace.joinpath("tests", "test_calc.py").write_text(
                "def test_add():\n    assert True\n", encoding="utf-8"
            )
            import subprocess

            subprocess.run(["git", "add", "-A"], cwd=record.workspace, check=True)
            subprocess.run(
                ["git", "commit", "-m", "weaken tests"],
                cwd=record.workspace,
                check=True,
                capture_output=True,
            )
            record.status = AgentRunStatus.SETTLED
            record.settlement = AgentSettlement(
                run_id=run_id, status=AgentRunStatus.SETTLED, success_claimed=True
            )
            return "started"

        def cancel(self, run_id: str) -> None:
            return None

        def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0):
            assert self._get(run_id).settlement is not None
            return self._get(run_id).settlement

    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    report = harness.run(goal.id, adapter=CommittingTamper(), options=RunOptions(agent_name="committing_tamper"))
    task = harness.store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.BLOCKED
    assert "protected verifier" in (task.last_error or "")
    assert report.tasks[0].false_completion
    assert not harness.store.list_evidence(goal.id)
