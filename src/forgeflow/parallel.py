from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import GoalSpec, TaskSpec
from forgeflow.planner.graph import topological_waves, validate_ownership
from forgeflow.worktree import IntegrationResult, WorktreeHandle, WorktreeManager

Worker = Callable[[TaskSpec, Path], str | None]
SerialWorker = Callable[[TaskSpec, Path], bool]


@dataclass
class ParallelTaskResult:
    task_key: str
    worktree: str
    worker_ok: bool
    commit: str | None = None
    merged: bool = False
    conflicts: list[str] = field(default_factory=list)
    regression_ok: bool | None = None
    rolled_back: bool = False
    serial_fallback_used: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    changed_files: list[str] = field(default_factory=list)


@dataclass
class ParallelReport:
    goal_id: str
    max_workers: int
    waves: list[list[str]]
    ownership_warnings: list[str]
    results: list[ParallelTaskResult]
    duration_seconds: float = 0.0

    @property
    def merged(self) -> list[str]:
        return [result.task_key for result in self.results if result.merged]

    @property
    def conflicts(self) -> list[str]:
        return [result.task_key for result in self.results if result.conflicts]

    def as_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "max_workers": self.max_workers,
            "waves": self.waves,
            "ownership_warnings": self.ownership_warnings,
            "merged": self.merged,
            "conflicts": self.conflicts,
            "results": [result.__dict__ for result in self.results],
            "duration_seconds": self.duration_seconds,
        }


class ParallelWorktreeScheduler:
    """Bounded multi-worker scheduling over isolated Git worktrees.

    Workers run concurrently within a dependency wave, but integration is serial.
    Path ownership overlap causes deterministic serialization. Merge conflicts are
    retried via rebase, then optionally resolved by a serial worker in the main tree.
    Regression runs after every successful merge.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_workers: int = 2,
        max_conflict_retries: int = 1,
        serialize_overlapping_ownership: bool = True,
        serial_fallback: SerialWorker | None = None,
        verify_after_merge: Callable[[], bool] | None = None,
    ) -> None:
        if max_workers < 1 or max_workers > 4:
            raise ValidationFailure("max_workers must be between 1 and 4")
        self.root = Path(workspace).resolve()
        self.manager = WorktreeManager(self.root)
        self.max_workers = max_workers
        self.max_conflict_retries = max_conflict_retries
        self.serialize_overlapping_ownership = serialize_overlapping_ownership
        self.serial_fallback = serial_fallback
        self.verify_after_merge = verify_after_merge

    def schedule(self, goal_id: str, spec: GoalSpec, worker: Worker) -> ParallelReport:
        started = time.monotonic()
        warnings = validate_ownership(spec.tasks)
        waves = topological_waves(spec)
        by_key = {task.key: task for task in spec.tasks}
        success: dict[str, bool] = {}
        results: dict[str, ParallelTaskResult] = {}
        for wave in waves:
            runnable: list[TaskSpec] = []
            for key in wave:
                task = by_key[key]
                if any(not success.get(dep, False) for dep in task.depends_on):
                    results[key] = ParallelTaskResult(
                        key,
                        "",
                        False,
                        skipped_reason="hard dependency did not merge",
                    )
                    success[key] = False
                else:
                    runnable.append(task)
            batches = (
                _serialize_overlapping(runnable)
                if self.serialize_overlapping_ownership
                else ([runnable] if runnable else [])
            )
            for batch in batches:
                handles: dict[str, WorktreeHandle] = {}
                for task in batch:
                    try:
                        handles[task.key] = self.manager.create(goal_id, task)
                    except Exception as exc:
                        results[task.key] = ParallelTaskResult(
                            task.key, "", False, error=str(exc)
                        )
                        success[task.key] = False
                active = [task for task in batch if task.key in handles]
                with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(active)))) as pool:
                    futures = {
                        pool.submit(self._run_worker, task, handles[task.key], worker): task
                        for task in active
                    }
                    for future in as_completed(futures):
                        task = futures[future]
                        handle = handles[task.key]
                        outcome = future.result()
                        results[task.key] = outcome
                for task in active:
                    outcome = results[task.key]
                    handle = handles[task.key]
                    if not outcome.worker_ok:
                        success[task.key] = False
                        continue
                    try:
                        handle = self.manager.commit(task, handle)
                        outcome.commit = handle.commit
                        outcome.changed_files = handle.changed_files
                    except Exception as exc:
                        outcome.worker_ok = False
                        outcome.error = f"commit failed: {exc}"
                        success[task.key] = False
                        continue
                    integration = self.manager.integrate(
                        task, handle, verify_after_merge=self.verify_after_merge
                    )
                    original_conflicts = list(integration.conflicts)
                    if not integration.merged and self.max_conflict_retries > 0:
                        integration = self.manager.rebase_and_retry(
                            task, handle, verify_after_merge=self.verify_after_merge
                        )
                    if not integration.merged and self.serial_fallback is not None:
                        try:
                            fallback_ok = self.serial_fallback(task, self.root)
                            outcome.serial_fallback_used = True
                            integration = IntegrationResult(
                                task.key,
                                merged=fallback_ok,
                                regression_ok=self.verify_after_merge()
                                if fallback_ok and self.verify_after_merge
                                else None,
                                error=None if fallback_ok else "serial fallback failed",
                                conflicts=original_conflicts,
                            )
                        except Exception as exc:
                            integration = IntegrationResult(
                                task.key,
                                False,
                                conflicts=original_conflicts,
                                error=f"serial fallback error: {exc}",
                            )
                    if original_conflicts and not integration.conflicts:
                        integration.conflicts = original_conflicts
                    outcome.merged = integration.merged and not integration.rolled_back
                    outcome.commit = integration.commit or outcome.commit
                    outcome.conflicts = integration.conflicts
                    outcome.regression_ok = integration.regression_ok
                    outcome.rolled_back = integration.rolled_back
                    outcome.error = integration.error or outcome.error
                    success[task.key] = bool(
                        integration.merged
                        and not integration.rolled_back
                        and integration.regression_ok is not False
                    )
                    if success[task.key] and not outcome.duration_seconds:
                        outcome.duration_seconds = 0.0
        return ParallelReport(
            goal_id=goal_id,
            max_workers=self.max_workers,
            waves=waves,
            ownership_warnings=warnings,
            results=[results[key] for key in by_key if key in results],
            duration_seconds=round(time.monotonic() - started, 4),
        )

    def _run_worker(
        self, task: TaskSpec, handle: WorktreeHandle, worker: Worker
    ) -> ParallelTaskResult:
        started = time.monotonic()
        try:
            worker(task, Path(handle.path))
            return ParallelTaskResult(
                task_key=task.key,
                worktree=handle.path,
                worker_ok=True,
                duration_seconds=round(time.monotonic() - started, 4),
                error=None,
            )
        except Exception as exc:
            return ParallelTaskResult(
                task_key=task.key,
                worktree=handle.path,
                worker_ok=False,
                duration_seconds=round(time.monotonic() - started, 4),
                error=str(exc),
            )


def _serialize_overlapping(tasks: list[TaskSpec]) -> list[list[TaskSpec]]:
    """Partition a wave so overlapping ownership never runs concurrently."""
    batches: list[list[TaskSpec]] = []
    for task in tasks:
        placed = False
        for batch in batches:
            if all(not _overlap(task.owned_paths, other.owned_paths) for other in batch):
                batch.append(task)
                placed = True
                break
        if not placed:
            batches.append([task])
    return batches


def _overlap(left: list[str], right: list[str]) -> bool:
    for first in left:
        for second in right:
            a = first.replace("\\", "/").rstrip("/")
            b = second.replace("\\", "/").rstrip("/")
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False
