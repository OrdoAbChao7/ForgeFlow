from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from forgeflow import __version__
from forgeflow.domain.enums import GateStatus, GoalStatus, TaskStatus
from forgeflow.domain.models import DeliveryManifest, GateEvaluation, utcnow
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import (
    environment_digest,
    environment_summary,
    git_commit,
    sha256_file,
    source_fingerprint,
)
from forgeflow.verification.gates import GateEngine, capture_binding


def build_delivery(
    store: Store,
    workspace: str | Path,
    goal_id: str,
    *,
    output_dir: str | Path | None = None,
    mark_delivered: bool = True,
) -> tuple[DeliveryManifest, Path]:
    root = Path(workspace).resolve()
    goal = store.get_goal(goal_id)
    spec = store.get_spec(goal_id)
    binding = capture_binding(root, spec.verifier.protected_paths)
    engine = GateEngine(store, root, spec, binding=binding)
    engine.refresh_staleness(goal_id)
    evaluations: list[GateEvaluation] = []
    required = [item for item in spec.acceptance if item.required]
    for criterion in required:
        evaluations.append(
            engine.evaluate(goal_id, criterion.gate, criterion_id=criterion.id)
        )
    required_gates = {item.gate for item in required} | {
        gate for task in spec.tasks for gate in task.gates
    }
    for gate in sorted(required_gates, key=lambda value: value.value):
        evaluations.append(engine.evaluate(goal_id, gate))
    tasks = store.list_tasks(goal_id)
    unverified: list[str] = []
    for criterion in required:
        matches = [
            item
            for item in evaluations
            if item.gate == criterion.gate
            and item.criterion_id == criterion.id
            and item.status == GateStatus.PASS
        ]
        if not matches:
            unverified.append(f"criterion {criterion.id}: gate {criterion.gate.value}")
    for task in tasks:
        if task.status != TaskStatus.VERIFIED:
            unverified.append(f"task {task.key}: {task.status.value}")
    for gate in required_gates:
        if not any(
            item.gate == gate
            and item.criterion_id is None
            and item.status == GateStatus.PASS
            for item in evaluations
        ):
            unverified.append(f"gate {gate.value}: no current PASS")
    if goal.status in {GoalStatus.CANCELLED, GoalStatus.FAILED}:
        unverified.append(f"goal status {goal.status.value}")
    if not engine.store.list_evidence(goal_id):
        state = "NOT_READY"
    elif unverified:
        state = "BLOCKED"
    else:
        state = "READY"
    artifacts = store.list_artifacts(goal_id)
    evidence_index = []
    for evidence in store.list_evidence(goal_id):
        evidence_index.append(
            {
                "id": evidence.id,
                "gate": evidence.gate.value,
                "scope": evidence.scope,
                "task_id": evidence.task_id,
                "attempt_id": evidence.task_attempt_id,
                "trust": evidence.trust.value,
                "runner_kind": evidence.runner_kind,
                "argv": evidence.argv,
                "exit_code": evidence.exit_code,
                "source_fingerprint": evidence.source_fingerprint,
                "env_digest": evidence.env_digest,
                "report_sha256": evidence.report_sha256,
                "log_dir": evidence.log_dir,
                "created_at": evidence.created_at.isoformat(),
            }
        )
    dependencies = {}
    for name in (
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "package-lock.json",
        "poetry.lock",
    ):
        path = root / name
        if path.exists():
            dependencies[name] = sha256_file(path)
    test_commands = []
    all_evidence = store.list_evidence(goal_id)
    for command in spec.verifier.commands:
        matching = [
            item
            for item in all_evidence
            if item.metadata.get("purpose") in {
                f"verifier:{command.id}",
                f"goal-verifier:{command.id}",
                f"cleanroom:{command.id}",
            }
        ]
        test_commands.append(
            {
                "id": command.id,
                "gate": command.gate.value,
                "argv": command.argv,
                "required": command.required,
                "scope": command.scope,
                "observations": [
                    {
                        "evidence_id": item.id,
                        "exit_code": item.exit_code,
                        "timed_out": item.timed_out,
                        "trust": item.trust.value,
                        "stdout_sha256": item.stdout_sha256,
                        "report_sha256": item.report_sha256,
                    }
                    for item in matching
                ],
            }
        )
    manifest = DeliveryManifest(
        generated_at=utcnow(),
        forgeflow_version=__version__,
        goal_id=goal.id,
        goal_title=goal.title,
        revision=goal.current_revision,
        git_sha=git_commit(root),
        source_fingerprint=source_fingerprint(
            root, protected_paths=spec.verifier.protected_paths
        ),
        state=state,
        gates=evaluations,
        artifacts=artifacts,
        evidence_index=evidence_index,
        test_commands=test_commands,
        feature_scope=[spec.objective, *spec.constraints],
        dependencies=dependencies,
        unverified=unverified,
        regeneration_commands=[
            f"forge verify {goal.id}",
            f"forge verify {goal.id} --clean-room",
            f"forge deliver {goal.id}",
        ],
        environment={
            **environment_summary(root),
            "environment_digest": environment_digest(root),
            "python_executable": sys.executable,
            "platform": platform.platform(),
        },
    )
    destination = Path(output_dir) if output_dir else root / "evidence" / "delivery" / goal.id
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "manifest.json"
    json_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digest = sha256_file(json_path)
    (destination / "manifest.sha256").write_text(digest + "\n", encoding="utf-8")
    markdown = _manifest_markdown(manifest, digest)
    (destination / "manifest.md").write_text(markdown, encoding="utf-8")
    if state == "READY" and mark_delivered:
        store.set_goal_status(
            goal_id,
            GoalStatus.DELIVERED,
            reason="delivery manifest READY with all required gates PASS",
            actor="delivery",
        )
    return manifest, destination


def _manifest_markdown(manifest: DeliveryManifest, digest: str) -> str:
    lines = [
        f"# ForgeFlow delivery: {manifest.goal_title}",
        "",
        f"- State: **{manifest.state}**",
        f"- Goal: `{manifest.goal_id}` revision {manifest.revision}",
        f"- ForgeFlow: {manifest.forgeflow_version}",
        f"- Git SHA: `{manifest.git_sha}`",
        f"- Source fingerprint: `{manifest.source_fingerprint}`",
        f"- Manifest SHA-256: `{digest}`",
        f"- Generated: {manifest.generated_at.isoformat()}",
        "",
        "## Gates",
        "",
        "| Gate | Criterion | Status | Evidence | Reason |",
        "|---|---|---|---|---|",
    ]
    for gate in manifest.gates:
        lines.append(
            f"| {gate.gate.value} | {gate.criterion_id or '-'} | **{gate.status.value}** | "
            f"{', '.join(gate.evidence_ids) or '-'} | {gate.reason} |"
        )
    lines.extend(
        [
            "",
            "## Test commands",
            "",
            *[
                f"- `{item['id']}` ({item['gate']}): `{' '.join(item['argv'])}` "
                f"— exits {[obs['exit_code'] for obs in item['observations']]}"
                for item in manifest.test_commands
            ],
            "",
            "## Feature scope",
            "",
            *[f"- {item}" for item in manifest.feature_scope],
            "",
            "## Dependencies",
            "",
            *[f"- `{name}`: `{sha}`" for name, sha in manifest.dependencies.items()],
            "",
            "## Unverified / blockers",
            "",
            *([f"- {item}" for item in manifest.unverified] or ["- None"]),
            "",
            "## Regeneration",
            "",
            *[f"- `{command}`" for command in manifest.regeneration_commands],
            "",
            "> SHA-256 establishes integrity, not signer identity. This manifest is not a\n"
            "> cryptographic signature and must not be treated as a trust root.",
            "",
        ]
    )
    return "\n".join(lines)
