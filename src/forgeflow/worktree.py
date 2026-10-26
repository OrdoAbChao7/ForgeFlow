from __future__ import annotations

import fnmatch
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow.domain.errors import ExternalBlockedError, PolicyDeniedError, ValidationFailure
from forgeflow.domain.models import TaskSpec, utcnow
from forgeflow.verification.fingerprint import is_git_repo


@dataclass
class WorktreeHandle:
    goal_id: str
    task_key: str
    path: str
    branch: str
    base: str
    created_at: str
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)


@dataclass
class IntegrationResult:
    task_key: str
    merged: bool
    commit: str | None = None
    conflicts: list[str] = field(default_factory=list)
    regression_ok: bool | None = None
    rolled_back: bool = False
    error: str | None = None


class WorktreeManager:
    """Git worktree isolation with ownership checks and conservative cleanup."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not is_git_repo(self.root):
            raise ExternalBlockedError("worktree scheduling requires a Git repository")
        self.worktree_root = self.root / ".forge" / "worktrees"
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / ".forge" / "worktrees.json"
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict[str, dict]:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_index(self) -> None:
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._index, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.index_path)

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if check and result.returncode != 0:
            raise ValidationFailure(
                f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def create(self, goal_id: str, task: TaskSpec, *, base: str = "HEAD") -> WorktreeHandle:
        if not task.owned_paths:
            raise PolicyDeniedError(
                f"parallel task {task.key!r} must declare owned_paths before worktree isolation"
            )
        key = f"{goal_id}:{task.key}"
        existing = self._index.get(key)
        if existing:
            path = Path(existing["path"])
            if path.is_dir():
                return WorktreeHandle(**existing)
            self._index.pop(key, None)
        goal_slug = "".join(character for character in goal_id if character.isalnum())[-10:]
        branch = f"forge/{goal_slug}/{task.key}"
        path = self.worktree_root / goal_slug / task.key
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path not in self.worktree_root.resolve().parents and not str(path).startswith(
                str(self.worktree_root.resolve())
            ):
                raise PolicyDeniedError(f"worktree path is outside ForgeFlow-owned root: {path}")
            raise PolicyDeniedError(
                f"unowned directory already exists at worktree path: {path}; refusing to delete it"
            )
        self._git("worktree", "add", "-b", branch, str(path), base)
        commit = self._git("rev-parse", "HEAD", cwd=path).stdout.strip()
        handle = WorktreeHandle(
            goal_id=goal_id,
            task_key=task.key,
            path=str(path),
            branch=branch,
            base=commit,
            created_at=utcnow().isoformat(),
        )
        self._index[key] = handle.__dict__.copy()
        self._save_index()
        return handle

    def changed_paths(self, task: TaskSpec, handle: WorktreeHandle) -> list[str]:
        worktree = Path(handle.path)
        result = self._git(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=worktree
        )
        paths: list[str] = []
        for line in result.stdout.splitlines():
            path = line[3:].strip().split(" -> ")[-1]
            if path:
                paths.append(path.replace("\\", "/"))
        committed = self._git(
            "diff", "--name-only", f"{handle.base}..HEAD", cwd=worktree, check=False
        )
        for path in committed.stdout.splitlines():
            normalized = path.strip().replace("\\", "/")
            if normalized and normalized not in paths:
                paths.append(normalized)
        violations = [
            path for path in paths if not _path_owned(path, task.owned_paths)
        ]
        if violations:
            raise PolicyDeniedError(
                f"task {task.key} modified paths outside its ownership: {violations}",
                details={"violations": violations, "owned_paths": task.owned_paths},
            )
        return paths

    def commit(self, task: TaskSpec, handle: WorktreeHandle, *, message: str | None = None) -> WorktreeHandle:
        worktree = Path(handle.path)
        dirty = self._git(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=worktree
        ).stdout.strip()
        changed = self.changed_paths(task, handle)
        if not changed:
            handle.changed_files = []
            handle.commit = None
            return handle
        if dirty:
            self._git("add", "-A", cwd=worktree)
            self._git(
                "commit",
                "-m",
                message or f"forge: {task.title} [{task.key}]",
                cwd=worktree,
            )
        # A worker may have committed its own work. Accept that commit only after the
        # ownership check above; never report a clean-but-unmerged worktree as merged.
        handle.commit = self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        if handle.commit == handle.base:
            handle.commit = None
        handle.changed_files = changed
        key = f"{handle.goal_id}:{handle.task_key}"
        self._index[key] = handle.__dict__.copy()
        self._save_index()
        return handle

    def integrate(
        self,
        task: TaskSpec,
        handle: WorktreeHandle,
        *,
        verify_after_merge: Callable[[], bool] | None = None,
    ) -> IntegrationResult:
        if not handle.commit:
            return IntegrationResult(
                task.key,
                False,
                error="worker produced no committed changes; nothing to merge",
            )
        dirty = self._git("status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
        if dirty:
            return IntegrationResult(
                task.key,
                False,
                error="main worktree is dirty; commit or stash changes before integration",
            )
        pre_merge = self._git("rev-parse", "HEAD").stdout.strip()
        merge = self._git(
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            f"forge: integrate {task.key}",
            handle.branch,
            check=False,
        )
        if merge.returncode != 0:
            conflicts_result = self._git(
                "diff", "--name-only", "--diff-filter=U", check=False
            )
            conflicts = [line for line in conflicts_result.stdout.splitlines() if line]
            self._git("merge", "--abort", check=False)
            return IntegrationResult(
                task.key,
                False,
                conflicts=conflicts,
                error="merge conflict; serial retry or rebase required",
            )
        commit = self._git("rev-parse", "HEAD").stdout.strip()
        result = IntegrationResult(task.key, True, commit)
        if verify_after_merge is not None:
            try:
                result.regression_ok = bool(verify_after_merge())
            except Exception as exc:
                result.regression_ok = False
                result.error = f"post-merge regression failed: {exc}"
            if result.regression_ok is False:
                self._git("reset", "--hard", pre_merge, check=False)
                result.rolled_back = True
                result.merged = False
                result.error = (
                    (result.error or "")
                    + " ; merge rolled back to pre-merge commit"
                ).strip(" ;")
        return result

    def rebase_and_retry(
        self, task: TaskSpec, handle: WorktreeHandle, *, verify_after_merge: Callable[[], bool] | None = None
    ) -> IntegrationResult:
        worktree = Path(handle.path)
        main_head = self._git("rev-parse", "HEAD").stdout.strip()
        rebase = self._git("rebase", main_head, cwd=worktree, check=False)
        if rebase.returncode != 0:
            self._git("rebase", "--abort", cwd=worktree, check=False)
            return IntegrationResult(task.key, False, error="rebase conflict; serial fallback required")
        handle.commit = self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        if handle.commit == handle.base:
            handle.commit = None
            return IntegrationResult(
                task.key, False, error="rebase produced no task commit; serial fallback required"
            )
        return self.integrate(task, handle, verify_after_merge=verify_after_merge)

    def cleanup(self, goal_id: str | None = None, *, force: bool = False) -> list[str]:
        removed: list[str] = []
        for key, item in list(self._index.items()):
            if goal_id is not None and item.get("goal_id") != goal_id:
                continue
            path = Path(item["path"]).resolve()
            if not _is_owned_path(path, self.worktree_root.resolve()):
                continue
            branch = item.get("branch", "")
            if not branch.startswith("forge/"):
                continue
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(path))
            self._git(*args, check=False)
            self._git("branch", "-D", branch, check=False)
            self._index.pop(key, None)
            removed.append(str(path))
        self._save_index()
        return removed

    def list_owned(self) -> list[WorktreeHandle]:
        handles: list[WorktreeHandle] = []
        for item in self._index.values():
            payload = {
                key: item.get(key)
                for key in WorktreeHandle.__dataclass_fields__
            }
            payload["changed_files"] = payload.get("changed_files") or []
            handles.append(WorktreeHandle(**payload))
        return handles


def _path_owned(path: str, owned_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in owned_paths:
        pattern_normalized = pattern.replace("\\", "/").rstrip("/")
        if fnmatch.fnmatch(normalized, pattern_normalized):
            return True
        if normalized == pattern_normalized or normalized.startswith(pattern_normalized + "/"):
            return True
    return False


def _is_owned_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
