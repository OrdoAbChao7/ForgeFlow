from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.domain.errors import ExternalBlockedError, PolicyDeniedError
from forgeflow.domain.models import TaskSpec
from forgeflow.worktree import WorktreeManager
from tests.conftest import git, init_repo


def _repo(path: Path) -> Path:
    return init_repo(
        path,
        {
            "a.py": "A = 1\n",
            "b.py": "B = 1\n",
            "shared.py": "VALUE = 1\n",
            ".gitignore": ".forge/\n",
        },
    )


def test_isolated_worktrees_merge_and_cleanup_only_owned(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    task_a = TaskSpec(key="a", title="a", owned_paths=["a.py"])
    task_b = TaskSpec(key="b", title="b", owned_paths=["b.py"])
    handle_a = manager.create("goal_parallel", task_a)
    handle_b = manager.create("goal_parallel", task_b)
    assert Path(handle_a.path).is_dir()
    assert Path(handle_b.path).is_dir()
    (Path(handle_a.path) / "a.py").write_text("A = 2\n", encoding="utf-8")
    (Path(handle_b.path) / "b.py").write_text("B = 2\n", encoding="utf-8")
    assert manager.changed_paths(task_a, handle_a) == ["a.py"]
    handle_a = manager.commit(task_a, handle_a)
    handle_b = manager.commit(task_b, handle_b)
    assert handle_a.commit and handle_b.commit
    first = manager.integrate(task_a, handle_a, verify_after_merge=lambda: True)
    second = manager.integrate(task_b, handle_b, verify_after_merge=lambda: True)
    assert first.merged and first.regression_ok
    assert second.merged and second.regression_ok
    assert "A = 2" in (repo / "a.py").read_text(encoding="utf-8")
    assert "B = 2" in (repo / "b.py").read_text(encoding="utf-8")
    unowned = repo / ".forge" / "worktrees" / "manual-not-owned"
    unowned.mkdir(parents=True)
    removed = manager.cleanup("goal_parallel", force=True)
    assert len(removed) == 2
    assert unowned.exists()


def test_worker_self_commit_is_not_lost(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    task = TaskSpec(key="a", title="a", owned_paths=["a.py"])
    handle = manager.create("goal_self_commit", task)
    worktree = Path(handle.path)
    (worktree / "a.py").write_text("A = 9\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "agent self commit")
    committed_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert manager.changed_paths(task, handle) == ["a.py"]
    handle = manager.commit(task, handle)
    assert handle.commit == committed_head
    assert handle.changed_files == ["a.py"]
    result = manager.integrate(task, handle)
    assert result.merged
    assert "A = 9" in (repo / "a.py").read_text(encoding="utf-8")


def test_ownership_violation_is_rejected_before_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    task = TaskSpec(key="a", title="a", owned_paths=["a.py"])
    handle = manager.create("goal_owned", task)
    (Path(handle.path) / "b.py").write_text("B = 99\n", encoding="utf-8")
    with pytest.raises(PolicyDeniedError):
        manager.changed_paths(task, handle)
    with pytest.raises(PolicyDeniedError):
        manager.commit(task, handle)


def test_conflicting_merges_report_and_abort(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    task_one = TaskSpec(key="one", title="one", owned_paths=["shared.py"])
    task_two = TaskSpec(key="two", title="two", owned_paths=["shared.py"])
    one = manager.create("goal_conflict", task_one)
    two = manager.create("goal_conflict", task_two)
    (Path(one.path) / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    (Path(two.path) / "shared.py").write_text("VALUE = 3\n", encoding="utf-8")
    one = manager.commit(task_one, one)
    two = manager.commit(task_two, two)
    assert manager.integrate(task_one, one).merged
    conflict = manager.integrate(task_two, two)
    assert not conflict.merged
    assert conflict.conflicts == ["shared.py"]
    status = git(repo, "status", "--porcelain").stdout
    assert "UU" not in status
    serial = manager.rebase_and_retry(task_two, two)
    assert not serial.merged
    assert "conflict" in (serial.error or "").lower()


def test_post_merge_regression_rolls_back(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    task = TaskSpec(key="a", title="a", owned_paths=["a.py"])
    handle = manager.create("goal_regression", task)
    (Path(handle.path) / "a.py").write_text("A = 7\n", encoding="utf-8")
    handle = manager.commit(task, handle)
    result = manager.integrate(task, handle, verify_after_merge=lambda: False)
    assert not result.merged
    assert result.rolled_back is True
    assert result.regression_ok is False
    assert "A = 1" in (repo / "a.py").read_text(encoding="utf-8")


def test_worktree_requires_git(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(ExternalBlockedError):
        WorktreeManager(folder)


def test_parallel_task_requires_owned_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo)
    with pytest.raises(PolicyDeniedError):
        manager.create("goal", TaskSpec(key="x", title="x"))
