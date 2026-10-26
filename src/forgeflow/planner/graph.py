from __future__ import annotations

import hashlib
from collections import defaultdict, deque

from forgeflow.domain.errors import CycleDetectedError, ValidationFailure
from forgeflow.domain.models import GoalSpec, TaskSpec


def task_id_for(goal_id: str, task_key: str) -> str:
    raw = hashlib.sha256(f"{goal_id}:{task_key}".encode()).hexdigest()[:20]
    return f"task_{raw}"


def topological_waves(spec: GoalSpec) -> list[list[str]]:
    """Return hard-dependency waves. Soft dependencies never gate execution."""
    indegree = {task.key: len(set(task.depends_on)) for task in spec.tasks}
    children: dict[str, set[str]] = defaultdict(set)
    for task in spec.tasks:
        for dep in set(task.depends_on):
            children[dep].add(task.key)
        if task.key not in children:
            children[task.key] = children[task.key]
    ready = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    waves: list[list[str]] = []
    visited = 0
    while ready:
        wave = sorted(ready)
        ready.clear()
        waves.append(wave)
        visited += len(wave)
        for key in wave:
            for child in sorted(children.get(key, ())):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
    if visited != len(indegree):
        cycle = sorted(key for key, degree in indegree.items() if degree > 0)
        raise CycleDetectedError(f"dependency cycle detected among: {cycle}", details={"nodes": cycle})
    return waves


def ready_task_keys(
    spec: GoalSpec,
    statuses: dict[str, str],
) -> list[str]:
    """Hard dependencies must be VERIFIED; pending tasks with satisfied deps are ready."""
    ready: list[str] = []
    for task in spec.tasks:
        if statuses.get(task.key) not in {"PENDING", "READY", "RETRY_WAIT", "INTERRUPTED"}:
            continue
        hard_ok = all(statuses.get(dep) == "VERIFIED" for dep in task.depends_on)
        if hard_ok:
            ready.append(task.key)
    return sorted(ready, key=lambda key: (-_priority(spec, key), key))


def _priority(spec: GoalSpec, key: str) -> int:
    return next(task.priority for task in spec.tasks if task.key == key)


def validate_ownership(specs: list[TaskSpec]) -> list[str]:
    """Detect obvious mutable-path overlap among concurrently plausible tasks."""
    warnings: list[str] = []
    owners: dict[str, str] = {}
    for task in specs:
        for path in task.owned_paths:
            normalized = path.rstrip("/")
            if normalized in owners and owners[normalized] != task.key:
                warnings.append(
                    f"path ownership overlap: {normalized!r} owned by {owners[normalized]} and {task.key}"
                )
            owners[normalized] = task.key
    return warnings


def apply_quick_mode(spec: GoalSpec) -> GoalSpec:
    if spec.risk.value in {"critical_module", "external_side_effect"}:
        raise ValidationFailure(
            "quick mode is disabled for critical_module and external_side_effect goals; "
            "risk gates may not be skipped"
        )
    data = spec.model_dump(mode="python")
    for task in data["tasks"]:
        task["max_attempts"] = 1
    data["metadata"] = {**data.get("metadata", {}), "quick_mode": True}
    return GoalSpec.model_validate(data)
