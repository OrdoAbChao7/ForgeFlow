"""Run two isolated worktree tasks with bounded concurrency and post-merge checks."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from forgeflow.domain.enums import GateKind
from forgeflow.domain.models import AcceptanceCriterion, GoalSpec, TaskSpec, VerifierConfig
from forgeflow.parallel import ParallelWorktreeScheduler


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def main() -> None:
    repo = Path(tempfile.mkdtemp(prefix="forgeflow-parallel-"))
    (repo / "a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("B = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".forge/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "parallel@forgeflow.invalid")
    git(repo, "config", "user.name", "ForgeFlow Parallel")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "parallel baseline")
    tasks = [
        TaskSpec(key="a", title="Update A", owned_paths=["a.py"]),
        TaskSpec(key="b", title="Update B", owned_paths=["b.py"]),
    ]
    spec = GoalSpec(
        title="Parallel demo",
        objective="Update independent files",
        acceptance=[
            AcceptanceCriterion(id="done", statement="both merged", gate=GateKind.MANUAL)
        ],
        tasks=tasks,
        verifier=VerifierConfig(),
    )

    def worker(task: TaskSpec, worktree: Path) -> str:
        (worktree / f"{task.key}.py").write_text(
            f"{task.key.upper()} = 2\n", encoding="utf-8"
        )
        return f"updated {task.key}.py"

    def verify() -> bool:
        return subprocess.run(
            ["python", "-c", "import a, b; assert a.A in (1, 2) and b.B in (1, 2)"],
            cwd=repo,
            capture_output=True,
        ).returncode == 0

    report = ParallelWorktreeScheduler(
        repo, max_workers=2, verify_after_merge=verify
    ).schedule("goal_parallel_demo", spec, worker)
    print(json.dumps(report.as_dict(), indent=2))
    print("worktrees retained under", repo / ".forge" / "worktrees")
    print("use WorktreeManager.cleanup(goal_id) to remove only ForgeFlow-owned worktrees")


if __name__ == "__main__":
    main()
