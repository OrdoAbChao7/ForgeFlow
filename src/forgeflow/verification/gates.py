from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from forgeflow.domain.enums import GateKind, GateStatus, TrustLevel
from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import Evidence, GateEvaluation, GoalSpec, new_id, utcnow
from forgeflow.runner.command import RunResult
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import (
    diff_fingerprint,
    environment_digest,
    git_commit,
    git_tree,
    protected_fingerprint,
    source_fingerprint,
)


@dataclass(frozen=True)
class Binding:
    commit_sha: str
    tree_hash: str
    diff_hash: str
    source_fingerprint: str
    protected_fingerprint: str
    env_digest: str


def capture_binding(workspace: str | Path, protected_paths: list[str]) -> Binding:
    root = Path(workspace).resolve()
    return Binding(
        commit_sha=git_commit(root),
        tree_hash=git_tree(root),
        diff_hash=diff_fingerprint(root),
        source_fingerprint=source_fingerprint(root, protected_paths=protected_paths),
        protected_fingerprint=protected_fingerprint(root, protected_paths),
        env_digest=environment_digest(root),
    )


class EvidenceRecorder:
    def __init__(self, store: Store, workspace: str | Path, spec: GoalSpec) -> None:
        self.store = store
        self.workspace = Path(workspace).resolve()
        self.spec = spec

    def record_command(
        self,
        *,
        goal_id: str,
        revision: int,
        gate: GateKind,
        result: RunResult,
        scope: list[str],
        task_id: str | None = None,
        attempt_id: str | None = None,
        trust: TrustLevel = TrustLevel.TRUSTED_RUNNER,
        runner_kind: str = "command_runner",
        binding: Binding | None = None,
        policy_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        if trust == TrustLevel.SELF_REPORTED:
            raise ValidationFailure(
                "self-reported agent text must never be recorded as verifier evidence"
            )
        binding = binding or capture_binding(self.workspace, self.spec.verifier.protected_paths)
        if not scope:
            raise ValidationFailure(
                "verifier evidence must declare the contract criteria it covers"
            )
        evidence = Evidence(
            id=new_id("ev"),
            goal_id=goal_id,
            goal_revision=revision,
            task_id=task_id,
            task_attempt_id=attempt_id,
            commit_sha=binding.commit_sha,
            tree_hash=binding.tree_hash,
            diff_hash=binding.diff_hash,
            source_fingerprint=binding.source_fingerprint,
            protected_fingerprint=binding.protected_fingerprint,
            env_digest=binding.env_digest,
            argv=list(result.argv),
            cwd=result.cwd,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_sha256=result.stdout_sha256,
            stderr_sha256=result.stderr_sha256,
            report_sha256=result.result_sha256,
            log_dir=result.result_path,
            runner_id=result.run_id,
            runner_kind=runner_kind,
            trust=trust,
            gate=gate,
            scope=sorted(set(scope)),
            verifier_policy_version=policy_version or self.spec.verifier.policy_version,
            created_at=utcnow(),
            metadata={
                **(metadata or {}),
                "cwd": result.cwd,
                "duration_seconds": result.duration_seconds,
                "purpose": result.purpose,
                "output_truncated": result.output_truncated,
                "capture_incomplete": result.capture_incomplete,
                "approved_by": result.approved_by,
            },
        )
        return self.store.add_evidence(evidence, actor=f"runner:{result.run_id}")

    def record_manual(
        self,
        *,
        goal_id: str,
        revision: int,
        gate: GateKind,
        scope: list[str],
        operator: str,
        reason: str,
        artifact_path: str | Path | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        signature: str | None = None,
        signature_verifier: Callable[[str, str], bool] | None = None,
    ) -> Evidence:
        if signature_verifier is None:
            raise ValidationFailure(
                "manual attestations require an independently configured signature verifier; "
                "ForgeFlow does not ship a trust root or accept a self-asserted boolean"
            )
        binding = capture_binding(self.workspace, self.spec.verifier.protected_paths)
        artifact_sha = ""
        if artifact_path:
            from forgeflow.verification.fingerprint import sha256_file

            artifact_sha = sha256_file(artifact_path)
        if not signature or not signature_verifier(signature, artifact_sha):
            raise ValidationFailure("manual attestation signature could not be verified")
        metadata: dict[str, Any] = {
            "operator": operator,
            "reason": reason,
            "manual": True,
            "signature_verified": True,
            "created": datetime.now().isoformat(),
            "artifact_sha256": artifact_sha,
        }
        evidence = Evidence(
            id=new_id("ev"),
            goal_id=goal_id,
            goal_revision=revision,
            task_id=task_id,
            task_attempt_id=attempt_id,
            commit_sha=binding.commit_sha,
            tree_hash=binding.tree_hash,
            diff_hash=binding.diff_hash,
            source_fingerprint=binding.source_fingerprint,
            protected_fingerprint=binding.protected_fingerprint,
            env_digest=binding.env_digest,
            argv=["<manual-attestation>"],
            cwd=str(self.workspace),
            exit_code=0,
            timed_out=False,
            stdout_sha256=artifact_sha,
            stderr_sha256="",
            report_sha256=artifact_sha,
            log_dir=str(artifact_path or ""),
            runner_id=new_id("manual"),
            runner_kind="manual_attestation",
            trust=TrustLevel.MANUAL_SIGNED,
            gate=gate,
            scope=sorted(set(scope)),
            verifier_policy_version=self.spec.verifier.policy_version,
            created_at=utcnow(),
            metadata=metadata,
        )
        return self.store.add_evidence(evidence, actor=f"manual:{operator}")


class GateEngine:
    def __init__(
        self,
        store: Store,
        workspace: str | Path,
        spec: GoalSpec,
        *,
        binding: Binding | None = None,
    ) -> None:
        self.store = store
        self.workspace = Path(workspace).resolve()
        self.spec = spec
        self.binding = binding or capture_binding(self.workspace, spec.verifier.protected_paths)

    def evaluate(
        self,
        goal_id: str,
        gate: GateKind,
        *,
        criterion_id: str | None = None,
        persist: bool = True,
        allowed_trust: set[TrustLevel] | None = None,
    ) -> GateEvaluation:
        goal = self.store.get_goal(goal_id)
        revision = goal.current_revision
        criteria = [
            item
            for item in self.spec.acceptance
            if item.gate == gate
            and item.required
            and (criterion_id is None or item.id == criterion_id)
        ]
        required_scope = {item.id for item in criteria}
        evidence = self.store.list_evidence(goal_id, gate=gate)
        evidence = [item for item in evidence if item.goal_revision == revision]
        trusted = [
            item
            for item in evidence
            if item.trust
            in {TrustLevel.TRUSTED_RUNNER, TrustLevel.CLEAN_ROOM, TrustLevel.MANUAL_SIGNED}
        ]
        if gate in {GateKind.MANUAL, GateKind.HARDWARE}:
            trusted = [item for item in trusted if item.trust == TrustLevel.MANUAL_SIGNED]
        if allowed_trust is not None:
            trusted = [item for item in trusted if item.trust in allowed_trust]
        if criterion_id is not None:
            trusted = [item for item in trusted if criterion_id in set(item.scope)]
        active = [
            item
            for item in trusted
            if item.source_fingerprint == self.binding.source_fingerprint
            and item.env_digest == self.binding.env_digest
            and item.protected_fingerprint == self.binding.protected_fingerprint
            and item.verifier_policy_version == self.spec.verifier.policy_version
        ]
        latest_by_command: dict[tuple[Any, ...], Evidence] = {}
        for item in active:
            signature = (
                item.task_id,
                tuple(item.argv),
                item.cwd,
                tuple(sorted(item.scope)),
            )
            current_best = latest_by_command.get(signature)
            if current_best is None or (item.created_at, item.id) > (
                current_best.created_at,
                current_best.id,
            ):
                latest_by_command[signature] = item
        latest = list(latest_by_command.values())
        passing = [item for item in latest if item.observed_pass]
        failing = [item for item in latest if not item.observed_pass]
        covered_scope = {criterion for item in passing for criterion in item.scope}
        if failing:
            status = GateStatus.FAIL
            reason = f"{len(failing)} latest trusted verifier run(s) failed"
            selected = failing
        elif passing and required_scope.issubset(covered_scope):
            status = GateStatus.PASS
            reason = f"{len(passing)} trusted evidence record(s) cover {sorted(required_scope) or 'gate'}"
            selected = passing
        elif trusted:
            status = GateStatus.STALE
            reasons = []
            if any(
                item.source_fingerprint != self.binding.source_fingerprint for item in trusted
            ):
                reasons.append("source fingerprint changed")
            if any(item.env_digest != self.binding.env_digest for item in trusted):
                reasons.append("environment digest changed")
            if any(
                item.protected_fingerprint != self.binding.protected_fingerprint
                for item in trusted
            ):
                reasons.append("protected verifier files changed")
            if any(
                item.verifier_policy_version != self.spec.verifier.policy_version
                for item in trusted
            ):
                reasons.append("verifier policy version changed")
            if not required_scope.issubset(covered_scope):
                reasons.append("evidence scope does not cover required contract criteria")
            status = GateStatus.STALE
            reason = "existing evidence invalidated: " + ", ".join(reasons or ["fingerprint mismatch"])
            selected = trusted
        elif evidence:
            status = GateStatus.BLOCKED
            reason = (
                "only self-reported or unverifiable evidence exists; "
                "agent text cannot satisfy a gate"
            )
            selected = evidence
        else:
            status = GateStatus.BLOCKED
            reason = "no trusted evidence recorded for this gate"
            selected = []
        evaluation = GateEvaluation(
            id=new_id("gate"),
            goal_id=goal_id,
            goal_revision=revision,
            gate=gate,
            criterion_id=criterion_id,
            status=status,
            reason=reason,
            evidence_ids=[item.id for item in selected],
            source_fingerprint=self.binding.source_fingerprint,
            policy_version=self.spec.verifier.policy_version,
            evaluated_at=utcnow(),
        )
        if persist:
            self.store.save_gate_evaluation(evaluation)
        return evaluation

    def evaluate_task_gates(self, goal_id: str, task_key: str) -> list[GateEvaluation]:
        task_spec = next((item for item in self.spec.tasks if item.key == task_key), None)
        if task_spec is None:
            raise ValidationFailure(f"unknown task: {task_key}")
        evaluations: list[GateEvaluation] = []
        for gate in task_spec.gates:
            if task_spec.acceptance_ids:
                relevant = [
                    item
                    for item in self.spec.acceptance
                    if item.id in task_spec.acceptance_ids and item.gate == gate
                ]
                if relevant:
                    evaluations.extend(
                        self.evaluate(goal_id, gate, criterion_id=item.id) for item in relevant
                    )
                else:
                    evaluations.append(self.evaluate(goal_id, gate))
            else:
                evaluations.append(self.evaluate(goal_id, gate))
        return evaluations

    def refresh_staleness(self, goal_id: str) -> list[GateEvaluation]:
        """Re-evaluate PASS gates against current source/env/protected bindings."""
        expired: list[GateEvaluation] = []
        for latest in self.store.latest_gates(goal_id):
            if latest.status != GateStatus.PASS:
                continue
            fresh = self.evaluate(
                goal_id,
                latest.gate,
                criterion_id=latest.criterion_id,
                persist=False,
            )
            if fresh.status == GateStatus.PASS:
                continue
            # Persist the fresh decision (usually STALE, possibly FAIL/BLOCKED) so status,
            # delivery and future runs cannot keep showing a stale PASS.
            self.store.save_gate_evaluation(fresh)
            expired.append(fresh)
        return expired

    def required_statuses(self, goal_id: str) -> dict[GateKind, GateStatus]:
        required = {item.gate for item in self.spec.acceptance if item.required}
        required |= {gate for task in self.spec.tasks for gate in task.gates}
        latest = {
            item.gate: item for item in self.store.latest_gates(goal_id) if item.criterion_id is None
        }
        return {
            gate: latest[gate].status if gate in latest else GateStatus.BLOCKED for gate in required
        }


def evidence_provenance(store: Store, goal_id: str, criterion_id: str) -> dict[str, Any]:
    """Answer: which test/run at which source/environment version supports this contract?"""
    spec = store.get_spec(goal_id)
    goal = store.get_goal(goal_id)
    criterion = next((item for item in spec.acceptance if item.id == criterion_id), None)
    if criterion is None:
        raise ValidationFailure(f"unknown criterion: {criterion_id}")
    gates = [
        gate
        for gate in store.latest_gates(goal_id)
        if gate.criterion_id == criterion_id
        and gate.gate == criterion.gate
        and gate.goal_revision == goal.current_revision
    ]
    latest = gates[0] if gates else None
    evidence = []
    if latest:
        evidence = [
            item
            for item in store.list_evidence(goal_id, gate=criterion.gate)
            if item.id in set(latest.evidence_ids)
        ]
    attempts = {attempt.id: attempt for attempt in store.list_attempts(goal_id)}
    tasks = {task.id: task for task in store.list_tasks(goal_id)}
    edges = []
    for item in evidence:
        attempt = attempts.get(item.task_attempt_id or "")
        task = tasks.get(item.task_id or "")
        edges.append(
            {
                "goal_id": goal_id,
                "goal_revision": goal.current_revision,
                "criterion_id": criterion.id,
                "criterion_statement": criterion.statement,
                "task_id": item.task_id,
                "task_key": task.key if task else None,
                "attempt_id": item.task_attempt_id,
                "attempt_no": attempt.attempt_no if attempt else None,
                "code": {
                    "commit_sha": item.commit_sha,
                    "tree_hash": item.tree_hash,
                    "diff_hash": item.diff_hash,
                    "source_fingerprint": item.source_fingerprint,
                    "protected_fingerprint": item.protected_fingerprint,
                },
                "test_run": {
                    "argv": item.argv,
                    "cwd": item.cwd,
                    "exit_code": item.exit_code,
                    "timed_out": item.timed_out,
                    "runner": item.runner_id,
                    "runner_kind": item.runner_kind,
                    "trust": item.trust.value,
                    "report_sha256": item.report_sha256,
                    "log": item.log_dir,
                },
                "environment_digest": item.env_digest,
                "evidence_id": item.id,
                "gate_id": latest.id if latest else None,
                "gate_status": latest.status.value if latest else GateStatus.BLOCKED.value,
                "policy_version": item.verifier_policy_version,
            }
        )
    return {
        "goal_id": goal_id,
        "goal_title": goal.title,
        "revision": goal.current_revision,
        "criterion": criterion.model_dump(mode="json"),
        "gate": latest.model_dump(mode="json") if latest else None,
        "edges": edges,
        "artifacts": [artifact.model_dump(mode="json") for artifact in store.list_artifacts(goal_id)],
        "answer": (
            "No gate has evaluated this criterion."
            if latest is None
            else latest.reason
        ),
        "trust_boundary": (
            "SHA-256 binds integrity, not signer identity. Agent self-report is never evidence."
        ),
    }
