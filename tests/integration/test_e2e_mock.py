from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow.delivery import build_delivery
from forgeflow.domain.enums import GateStatus, GoalStatus, TaskStatus, TrustLevel
from forgeflow.runtime import Harness, RunOptions
from forgeflow.verification.gates import capture_binding
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

def test_clean_room_auto_reinstalls_locked_dependencies(tmp_path: Path) -> None:
    import shutil
    import subprocess

    from forgeflow.domain.enums import GateKind
    from forgeflow.domain.models import (
        AcceptanceCriterion,
        GoalSpec,
        TaskSpec,
        VerifierCommand,
        VerifierConfig,
    )
    from forgeflow.verification.cleanroom import CleanRoomVerifier
    from forgeflow.verification.gates import GateEngine
    from tests.conftest import git, init_repo

    if not shutil.which("uv"):
        pytest.skip("uv is required for locked clean-room setup")
    project = init_repo(
        tmp_path / "locked",
        {
            "pyproject.toml": (
                "[project]\nname=\"cleanfixture\"\nversion=\"0.1.0\"\n"
                "requires-python=\">=3.11\"\ndependencies=[]\n"
            ),
            ".gitignore": ".venv/\n.forge/\n__pycache__/\n.pytest_cache/\n",
        },
    )
    lock = subprocess.run(
        ["uv", "lock", "--offline"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert lock.returncode == 0, lock.stderr
    git(project, "add", "uv.lock")
    git(project, "commit", "-m", "lock fixture")
    spec = GoalSpec(
        title="Locked clean room",
        objective="Reinstall locked dependencies in isolation",
        acceptance=[
            AcceptanceCriterion(
                id="env", statement="locked venv is present", gate=GateKind.UNIT, required=True
            )
        ],
        tasks=[
            TaskSpec(
                key="env-check",
                title="Check environment",
                owned_paths=["result.txt"],
                gates=[GateKind.UNIT],
                acceptance_ids=["env"],
            )
        ],
        verifier=VerifierConfig(
            commands=[
                VerifierCommand(
                    id="env-check",
                    gate=GateKind.UNIT,
                    argv=[
                        "uv",
                        "run",
                        "--frozen",
                        "--no-sync",
                        "python",
                        "-c",
                        "import sys; from pathlib import Path; "
                        "assert '.venv' in sys.prefix; assert Path('result.txt').read_text() == 'ok'",
                    ],
                    scope=["env"],
                )
            ],
            protected_paths=[],
            clean_room=True,
        ),
    )
    harness = Harness(project)
    goal = harness.create_goal(spec)
    (project / "result.txt").write_text("ok", encoding="utf-8")
    verifier = CleanRoomVerifier(harness.store, project, spec)
    report = verifier.verify(goal.id)
    assert report.success, report.as_dict()
    assert report.setup_result is not None and report.setup_result.succeeded
    assert report.setup_mode == "uv-frozen"
    assert report.locked_dependencies_reinstalled is True
    assert report.results and report.results[0].succeeded
    evidence = harness.store.list_evidence(goal.id)
    assert evidence and evidence[0].trust.value == "clean_room"
    engine = GateEngine(
        harness.store,
        project,
        spec,
        binding=capture_binding(project, spec.verifier.protected_paths),
    )
    evaluation = engine.evaluate(
        goal.id,
        GateKind.UNIT,
        criterion_id="env",
        allowed_trust={TrustLevel.CLEAN_ROOM},
    )
    assert evaluation.status == GateStatus.PASS
