from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from forgeflow.domain.enums import GateKind, RiskLevel
from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import (
    AcceptanceCriterion,
    GoalSpec,
    TaskSpec,
    VerifierCommand,
    VerifierConfig,
    digest_json,
)
from forgeflow.planner.graph import (
    apply_quick_mode,
    ready_task_keys,
    topological_waves,
    validate_ownership,
)


def _coerce_spec(data: dict[str, Any]) -> GoalSpec:
    if not isinstance(data, dict):
        raise ValidationFailure("goal document must be a YAML/JSON mapping")
    # Accept both a flat GoalSpec and a compact {goal: ..., tasks: ...} envelope.
    if "goal" in data:
        envelope = dict(data)
        goal = envelope.pop("goal")
        if not isinstance(goal, dict):
            raise ValidationFailure("goal must be a mapping")
        merged = {**goal, **envelope}
    else:
        merged = dict(data)
    try:
        return GoalSpec.model_validate(merged)
    except ValidationError as exc:
        raise ValidationFailure(f"invalid goal spec: {exc}") from exc


def load_goal_spec(path: str | Path) -> GoalSpec:
    source = Path(path)
    if not source.exists():
        raise ValidationFailure(f"goal file not found: {source}")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailure(f"invalid YAML: {exc}") from exc
    return _coerce_spec(data)


def load_goal_spec_from_json(value: str) -> GoalSpec:
    try:
        return _coerce_spec(json.loads(value))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"invalid JSON: {exc}") from exc


def compile_natural_language(
    text: str,
    *,
    risk: RiskLevel = RiskLevel.STANDARD_CODE,
    quick: bool = False,
) -> GoalSpec:
    """Deterministic offline compiler.

    This intentionally does not pretend a model was used. An LLM-backed compiler can
    implement the same contract and must record its model identity in metadata.
    """
    if not text.strip():
        raise ValidationFailure("goal text must not be empty")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-") or "goal"
    spec = GoalSpec(
        title=text.strip().splitlines()[0][:100],
        objective=text.strip(),
        risk=risk,
        constraints=["Offline deterministic compilation: no model call was made."],
        acceptance=[
            AcceptanceCriterion(
                id="manual-acceptance",
                statement="A human reviews the delivered change against the stated objective.",
                gate=GateKind.MANUAL,
                required=True,
            )
        ],
        tasks=[
            TaskSpec(
                key="implement",
                title="Implement the requested change",
                description=text.strip(),
                gates=[GateKind.MANUAL],
                acceptance_ids=["manual-acceptance"],
                owned_paths=["src/"],
            )
        ],
        verifier=VerifierConfig(
            commands=[], protected_paths=["tests/"], clean_room=False
        ),
        metadata={"compiler": "deterministic_text_v1", "source_slug": slug},
    )
    if quick:
        spec = apply_quick_mode(spec)
    return spec


def compile_goal(
    *,
    file: str | Path | None = None,
    text: str | None = None,
    risk: RiskLevel = RiskLevel.STANDARD_CODE,
    quick: bool = False,
) -> GoalSpec:
    if bool(file) == bool(text):
        raise ValidationFailure("provide exactly one of --file or --text")
    spec = load_goal_spec(file) if file else compile_natural_language(text or "", risk=risk)
    if quick:
        spec = apply_quick_mode(spec)
    # Offline structural validation before persisting anything.
    topological_waves(spec)
    validate_ownership(spec.tasks)
    return spec


def spec_digest(spec: GoalSpec) -> str:
    return digest_json(spec.model_dump(mode="json"))


def summarize_plan(spec: GoalSpec, statuses: dict[str, str] | None = None) -> dict[str, Any]:
    waves = topological_waves(spec)
    statuses = statuses or {task.key: "PENDING" for task in spec.tasks}
    return {
        "waves": waves,
        "ready": ready_task_keys(spec, statuses),
        "warnings": validate_ownership(spec.tasks),
        "task_count": len(spec.tasks),
        "acceptance": len(spec.acceptance),
        "verifier_commands": len(spec.verifier.commands),
    }


def default_verifier_command(
    command_id: str,
    gate: GateKind,
    argv: list[str],
    *,
    scope: list[str],
) -> VerifierCommand:
    return VerifierCommand(id=command_id, gate=gate, argv=argv, scope=scope)
