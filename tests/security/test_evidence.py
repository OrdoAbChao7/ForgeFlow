from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forgeflow.domain.enums import GateKind, GateStatus, TrustLevel
from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import Evidence, new_id, utcnow
from forgeflow.runner.command import CommandRunner, RunRequest
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import (
    environment_digest,
    git_commit,
    protected_fingerprint,
    source_fingerprint,
)
from forgeflow.verification.gates import (
    Binding,
    EvidenceRecorder,
    GateEngine,
    capture_binding,
)
from tests.conftest import calc_spec


def _context(project: Path):
    store = Store(project)
    spec = calc_spec()
    goal = store.create_goal(spec)
    runner = CommandRunner(project)
    binding = capture_binding(project, spec.verifier.protected_paths)
    recorder = EvidenceRecorder(store, project, spec)
    return store, spec, goal, runner, binding, recorder


def _command(project: Path, runner: CommandRunner, exit_code: int = 0):
    code = f"import sys; print('PASS'); sys.exit({exit_code})"
    return runner.run(
        RunRequest(
            argv=[sys.executable, "-c", code],
            cwd=project,
            purpose="evidence-test",
            timeout_seconds=30,
            writes=False,
        )
    )


def test_trusted_evidence_passes_then_stales_after_source_change(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    result = _command(project, runner)
    evidence = recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=result,
        scope=["adds"],
        binding=binding,
        trust=TrustLevel.TRUSTED_RUNNER,
    )
    assert evidence.observed_pass
    engine = GateEngine(store, project, spec, binding=binding)
    evaluation = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert evaluation.status == GateStatus.PASS
    (project / "calc.py").write_text("def add(a, b):\n    return 99\n", encoding="utf-8")
    engine = GateEngine(
        store,
        project,
        spec,
        binding=capture_binding(project, spec.verifier.protected_paths),
    )
    stale = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert stale.status == GateStatus.STALE


def test_stderr_pass_text_does_not_override_exit_code(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    result = _command(project, runner, exit_code=7)
    evidence = recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=result,
        scope=["adds"],
        binding=binding,
    )
    assert evidence.exit_code == 7
    assert evidence.stdout_sha256
    engine = GateEngine(store, project, spec, binding=binding)
    evaluation = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert evaluation.status == GateStatus.FAIL


def test_environment_mismatch_invalidates_evidence(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    result = _command(project, runner)
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=result,
        scope=["adds"],
        binding=binding,
    )
    changed = Binding(
        commit_sha=binding.commit_sha,
        tree_hash=binding.tree_hash,
        diff_hash=binding.diff_hash,
        source_fingerprint=binding.source_fingerprint,
        protected_fingerprint=binding.protected_fingerprint,
        env_digest="different-environment",
    )
    engine = GateEngine(store, project, spec, binding=changed)
    evaluation = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert evaluation.status == GateStatus.STALE
    assert "environment" in evaluation.reason


def test_protected_file_change_invalidates_evidence(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    result = _command(project, runner)
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=result,
        scope=["adds"],
        binding=binding,
    )
    (project / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n", encoding="utf-8"
    )
    engine = GateEngine(
        store,
        project,
        spec,
        binding=capture_binding(project, spec.verifier.protected_paths),
    )
    evaluation = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert evaluation.status == GateStatus.STALE
    assert "protected" in evaluation.reason


def test_self_reported_evidence_cannot_pass(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    with pytest.raises(ValidationFailure):
        recorder.record_command(
            goal_id=goal.id,
            revision=1,
            gate=GateKind.UNIT,
            result=_command(project, runner),
            scope=["adds"],
            trust=TrustLevel.SELF_REPORTED,
        )
    now = utcnow()
    fake = Evidence(
        id=new_id("ev"),
        goal_id=goal.id,
        goal_revision=1,
        task_id=None,
        task_attempt_id=None,
        commit_sha=git_commit(project),
        tree_hash="t",
        diff_hash="d",
        source_fingerprint=binding.source_fingerprint,
        protected_fingerprint=binding.protected_fingerprint,
        env_digest=binding.env_digest,
        argv=["agent-said-pass"],
        cwd=str(project),
        exit_code=0,
        timed_out=False,
        stdout_sha256="a",
        stderr_sha256="b",
        report_sha256="c",
        log_dir=".",
        runner_id="agent",
        runner_kind="agent_claim",
        trust=TrustLevel.SELF_REPORTED,
        gate=GateKind.UNIT,
        scope=["adds"],
        verifier_policy_version=spec.verifier.policy_version,
        created_at=now,
    )
    store.add_evidence(fake)
    engine = GateEngine(store, project, spec, binding=binding)
    evaluation = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert evaluation.status == GateStatus.BLOCKED
    assert "self-reported" in evaluation.reason


def test_empty_scope_is_rejected(project: Path) -> None:
    _store, _spec, goal, runner, binding, recorder = _context(project)
    with pytest.raises(ValidationFailure):
        recorder.record_command(
            goal_id=goal.id,
            revision=1,
            gate=GateKind.UNIT,
            result=_command(project, runner),
            scope=[],
            binding=binding,
        )


def test_gate_requires_union_scope_across_multiple_evidence(project: Path) -> None:
    store = Store(project)
    spec = calc_spec()
    goal = store.create_goal(spec)
    binding = capture_binding(project, spec.verifier.protected_paths)
    recorder = EvidenceRecorder(store, project, spec)
    runner = CommandRunner(project)
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=_command(project, runner),
        scope=["other"],
        binding=binding,
    )
    engine = GateEngine(store, project, spec, binding=binding)
    missing = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert missing.status == GateStatus.BLOCKED
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=_command(project, runner),
        scope=["adds"],
        binding=binding,
    )
    covered = engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds")
    assert covered.status == GateStatus.PASS


def test_manual_attestation_requires_external_verifier(project: Path) -> None:
    _store, _spec, goal, _runner, _binding, recorder = _context(project)
    with pytest.raises(ValidationFailure):
        recorder.record_manual(
            goal_id=goal.id,
            revision=1,
            gate=GateKind.MANUAL,
            scope=["adds"],
            operator="human",
            reason="reviewed",
        )
    with pytest.raises(ValidationFailure):
        recorder.record_manual(
            goal_id=goal.id,
            revision=1,
            gate=GateKind.MANUAL,
            scope=["adds"],
            operator="human",
            reason="reviewed",
            signature="not-really",
            signature_verifier=lambda signature, digest: False,
        )
    verified = recorder.record_manual(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.MANUAL,
        scope=["adds"],
        operator="human",
        reason="reviewed",
        signature="fixture-signature",
        signature_verifier=lambda signature, digest: signature == "fixture-signature",
    )
    assert verified.trust == TrustLevel.MANUAL_SIGNED


def test_binding_contains_real_git_and_env_data(project: Path) -> None:
    binding = capture_binding(project, ["tests"])
    assert binding.commit_sha == git_commit(project)
    assert binding.source_fingerprint == source_fingerprint(project, protected_paths=["tests"])
    assert binding.protected_fingerprint == protected_fingerprint(project, ["tests"])
    assert binding.env_digest == environment_digest(project)

def test_newer_failing_command_overrides_older_pass(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    (project / ".forge").mkdir(exist_ok=True)
    code = "import pathlib,sys; sys.exit(int(pathlib.Path('.forge/exit.txt').read_text()))"
    (project / ".forge" / "exit.txt").write_text("0", encoding="utf-8")
    passing = runner.run(
        RunRequest(
            argv=[sys.executable, "-c", code],
            cwd=project,
            timeout_seconds=30,
        )
    )
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=passing,
        scope=["adds"],
        binding=binding,
    )
    first = GateEngine(store, project, spec, binding=binding).evaluate(
        goal.id, GateKind.UNIT, criterion_id="adds"
    )
    assert first.status == GateStatus.PASS
    (project / ".forge" / "exit.txt").write_text("1", encoding="utf-8")
    failing = runner.run(
        RunRequest(
            argv=[sys.executable, "-c", code],
            cwd=project,
            timeout_seconds=30,
        )
    )
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=failing,
        scope=["adds"],
        binding=binding,
    )
    latest = GateEngine(store, project, spec, binding=binding).evaluate(
        goal.id, GateKind.UNIT, criterion_id="adds"
    )
    assert latest.status == GateStatus.FAIL

def test_refresh_staleness_detects_env_and_protected_changes(project: Path) -> None:
    store, spec, goal, runner, binding, recorder = _context(project)
    result = _command(project, runner)
    recorder.record_command(
        goal_id=goal.id,
        revision=1,
        gate=GateKind.UNIT,
        result=result,
        scope=["adds"],
        binding=binding,
    )
    engine = GateEngine(store, project, spec, binding=binding)
    assert engine.evaluate(goal.id, GateKind.UNIT, criterion_id="adds").status == GateStatus.PASS
    env_changed = Binding(
        commit_sha=binding.commit_sha,
        tree_hash=binding.tree_hash,
        diff_hash=binding.diff_hash,
        source_fingerprint=binding.source_fingerprint,
        protected_fingerprint=binding.protected_fingerprint,
        env_digest="different",
    )
    refreshed = GateEngine(store, project, spec, binding=env_changed).refresh_staleness(goal.id)
    assert refreshed
    assert any(item.status == GateStatus.STALE for item in refreshed)

def test_gitignored_protected_file_is_still_guarded(project: Path) -> None:
    from forgeflow.runtime import Harness
    from forgeflow.verification.fingerprint import protected_files_match_ref
    from tests.conftest import git

    (project / ".gitignore").write_text(
        "tests/generated_*.py\n", encoding="utf-8"
    )
    git(project, "add", ".gitignore")
    git(project, "commit", "-m", "ignore generated tests")
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    ignored = project / "tests" / "generated_cheat.py"
    ignored.write_text(
        "def test_fake():\n    assert True\n", encoding="utf-8"
    )
    assert ignored.is_file()
    assert not protected_files_match_ref(
        project, ["tests"], goal.baseline_commit
    )
