from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forgeflow.domain.enums import GateKind
from forgeflow.domain.errors import ValidationFailure
from forgeflow.domain.models import (
    AcceptanceCriterion,
    GoalSpec,
    TaskSpec,
    VerifierConfig,
)
from forgeflow.parallel import ParallelWorktreeScheduler
from tests.conftest import init_repo


def _spec(tasks: list[TaskSpec]) -> GoalSpec:
    return GoalSpec(
        title="parallel",
        objective="parallel tasks",
        acceptance=[
            AcceptanceCriterion(id="done", statement="done", gate=GateKind.MANUAL)
        ],
        tasks=tasks,
        verifier=VerifierConfig(),
    )


def _repo(path: Path) -> Path:
    return init_repo(
        path,
        {
            "a.py": "A = 1\n",
            "b.py": "B = 1\n",
            "shared.py": "VALUE = 1\n",
            ".gitignore": ".forge/\n__pycache__/\n.pytest_cache/\n",
        },
    )


def test_parallel_scheduler_merges_independent_tasks(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tasks = [
        TaskSpec(key="a", title="a", owned_paths=["a.py"]),
        TaskSpec(key="b", title="b", owned_paths=["b.py"]),
    ]

    def worker(task: TaskSpec, worktree: Path) -> str:
        target = worktree / f"{task.key}.py"
        target.write_text(f"{task.key.upper()} = 2\n", encoding="utf-8")
        return task.key

    def regression() -> bool:
        result = subprocess.run(
            [
                "python",
                "-c",
                "import a, b; assert a.A in (1, 2) and b.B in (1, 2)",
            ],
            cwd=repo,
            capture_output=True,
        )
        return result.returncode == 0

    scheduler = ParallelWorktreeScheduler(
        repo, max_workers=2, verify_after_merge=regression
    )
    report = scheduler.schedule("goal_par", _spec(tasks), worker)
    assert sorted(report.merged) == ["a", "b"]
    assert report.conflicts == []
    assert all(result.regression_ok for result in report.results)
    assert "A = 2" in (repo / "a.py").read_text(encoding="utf-8")
    assert "B = 2" in (repo / "b.py").read_text(encoding="utf-8")
    assert len(list((repo / ".forge" / "worktrees").rglob("a.py"))) >= 1


def test_overlapping_ownership_is_serialized(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tasks = [
        TaskSpec(key="one", title="one", owned_paths=["shared.py"]),
        TaskSpec(key="two", title="two", owned_paths=["shared.py"]),
    ]

    def worker(task: TaskSpec, worktree: Path) -> str:
        (worktree / "shared.py").write_text(f"VALUE = '{task.key}'\n", encoding="utf-8")
        return task.key

    scheduler = ParallelWorktreeScheduler(repo, max_workers=2)
    report = scheduler.schedule("goal_overlap", _spec(tasks), worker)
    assert report.ownership_warnings
    assert sorted(report.merged) == ["one", "two"]
    assert report.conflicts == []


def test_conflict_retry_and_serial_fallback(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tasks = [
        TaskSpec(key="one", title="one", owned_paths=["shared.py"]),
        TaskSpec(key="two", title="two", owned_paths=["shared.py"]),
    ]

    def worker(task: TaskSpec, worktree: Path) -> str:
        (worktree / "shared.py").write_text(f"VALUE = '{task.key}'\n", encoding="utf-8")
        return task.key

    fallback_calls: list[str] = []

    def serial(task: TaskSpec, root: Path) -> bool:
        fallback_calls.append(task.key)
        (root / "shared.py").write_text("VALUE = 'serial'\n", encoding="utf-8")
        return True

    scheduler = ParallelWorktreeScheduler(
        repo,
        max_workers=2,
        max_conflict_retries=1,
        serialize_overlapping_ownership=False,
        serial_fallback=serial,
        verify_after_merge=lambda: True,
    )
    report = scheduler.schedule("goal_conflict", _spec(tasks), worker)
    assert "one" in report.merged
    assert report.conflicts == ["two"]
    assert report.results[1].serial_fallback_used
    assert fallback_calls == ["two"]


def test_dependency_failure_skips_downstream(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tasks = [
        TaskSpec(key="first", title="first", owned_paths=["a.py"]),
        TaskSpec(key="second", title="second", depends_on=["first"], owned_paths=["b.py"]),
    ]

    def worker(task: TaskSpec, worktree: Path) -> str:
        if task.key == "first":
            raise RuntimeError("simulated worker failure")
        (worktree / "b.py").write_text("B = 3\n", encoding="utf-8")
        return task.key

    scheduler = ParallelWorktreeScheduler(repo)
    report = scheduler.schedule("goal_dep", _spec(tasks), worker)
    assert report.merged == []
    second = next(item for item in report.results if item.task_key == "second")
    assert second.skipped_reason


def test_worker_limit_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailure):
        ParallelWorktreeScheduler(tmp_path, max_workers=5)
