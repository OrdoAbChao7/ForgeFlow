from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

from forgeflow.domain.errors import ExternalBlockedError, ValidationFailure

_DEFAULT_EXCLUDES = {
    ".git",
    ".forge",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    "htmlcov",
    "evidence/runs",
    "evidence/tmp",
}

_DEPENDENCY_PACKAGES = (
    "pydantic",
    "typer",
    "rich",
    "PyYAML",
    "tree-sitter",
    "tree-sitter-python",
    "tree-sitter-typescript",
    "opentelemetry-api",
    "opentelemetry-sdk",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_TEXT_LIKE_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".md",
    ".txt",
    ".rst",
    ".toml",
    ".lock",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".v",
    ".sv",
    ".sh",
    ".ps1",
    ".sql",
    ".html",
    ".css",
}


def _normalized_digest(path: str | Path, data: bytes | None = None) -> str:
    """Hash source text with LF-normalized line endings.

    Git with core.autocrlf checks out CRLF on Windows while HEAD blobs and clean-room
    archives use LF. Normalizing text prevents a false STALE verdict for the same
    logical file; binary files continue to hash exact bytes.
    """
    raw = Path(path).read_bytes() if data is None else data
    if Path(path).suffix.lower() in _TEXT_LIKE_SUFFIXES:
        raw = raw.replace(b"\r\n", b"\n")
    return sha256_bytes(raw)


def _is_excluded(relative: str, excludes: set[str]) -> bool:
    posix = relative.replace("\\", "/")
    parts = posix.split("/")
    if any(part in excludes for part in parts):
        return True
    return any(posix == item or posix.startswith(item + "/") for item in excludes)


def workspace_files(
    root: str | Path,
    *,
    excludes: set[str] | None = None,
    include_untracked: bool = True,
    include_ignored: bool = False,
) -> list[tuple[str, Path]]:
    root_path = Path(root).resolve()
    excluded = _DEFAULT_EXCLUDES | (excludes or set())
    files: list[tuple[str, Path]] = []
    tracked: list[str] | None = None
    if is_git_repo(root_path):
        args = ["ls-files", "-z", "--cached"]
        if include_untracked:
            args.extend(["--others", "--exclude-standard"])
        listed = git(root_path, *args, check=False)
        if listed.returncode == 0:
            tracked = [item for item in listed.stdout.split("\0") if item]
        if include_ignored:
            ignored = git(
                root_path,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                check=False,
            )
            if ignored.returncode == 0:
                tracked = list(
                    dict.fromkeys(
                        [
                            *(tracked or []),
                            *(item for item in ignored.stdout.split("\0") if item),
                        ]
                    )
                )
    if tracked is not None:
        candidates = [(relative, root_path / relative) for relative in tracked]
    else:
        candidates = [
            (path.relative_to(root_path).as_posix(), path)
            for path in root_path.rglob("*")
            if path.is_file()
        ]
    for relative, path in candidates:
        if not path.is_file():
            continue
        if _is_excluded(relative, excluded):
            continue
        if path.name.endswith((".pyc", ".pyo")):
            continue
        files.append((relative, path))
    return sorted(files, key=lambda item: item[0])


def source_fingerprint(
    root: str | Path,
    *,
    excludes: set[str] | None = None,
    protected_paths: list[str] | None = None,
) -> str:
    """Hash all source-like files, with protected files represented separately.

    This is a content fingerprint, not a cryptographic identity/signature.
    """
    root_path = Path(root).resolve()
    protected = _normalize_protected(protected_paths or [])
    digest = hashlib.sha256()
    for relative, path in workspace_files(
        root_path, excludes=excludes, include_ignored=True
    ):
        if _matches_protected(relative, protected):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(_normalized_digest(path).encode("ascii"))
        except OSError:
            digest.update(b"UNREADABLE")
        digest.update(b"\0")
    return digest.hexdigest()


def protected_fingerprint(root: str | Path, protected_paths: list[str]) -> str:
    """Hash protected files including gitignored fixture/config files.

    Protected paths are walked directly rather than through `git ls-files`, so an agent
    cannot hide a protected test by adding it to .gitignore. Runtime caches are omitted.
    """
    root_path = Path(root).resolve()
    protected = _normalize_protected(protected_paths)
    digest = hashlib.sha256()
    seen: set[str] = set()
    for prefix in protected:
        target = (root_path / prefix).resolve()
        try:
            target.relative_to(root_path)
        except ValueError as exc:
            raise ValidationFailure(f"protected path escapes workspace: {prefix}") from exc
        candidates = (
            [target]
            if target.is_file()
            else list(target.rglob("*"))
            if target.is_dir()
            else []
        )
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(root_path).as_posix()
            if _is_excluded(relative, _DEFAULT_EXCLUDES) or relative in seen:
                continue
            seen.add(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_normalized_digest(path).encode("ascii"))
            digest.update(b"\0")
    if not protected:
        digest.update(b"NO_PROTECTED_PATHS")
    return digest.hexdigest()


def _validated_ref(ref: str) -> str:
    if ref in {"HEAD", "UNBORN", "NO_GIT"}:
        return ref
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValidationFailure(f"invalid trusted Git ref: {ref!r}")
    return ref


def protected_fingerprint_at_ref(
    root: str | Path, protected_paths: list[str], ref: str
) -> str:
    """Fingerprint protected verifier files at an immutable trusted commit."""
    root_path = Path(root).resolve()
    protected = _normalize_protected(protected_paths)
    ref = _validated_ref(ref)
    digest = hashlib.sha256()
    if not is_git_repo(root_path) or ref in {"UNBORN", "NO_GIT"}:
        return protected_fingerprint(root_path, protected_paths)
    for relative in protected:
        listed = git(
            root_path,
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            ref,
            "--",
            relative,
            check=False,
        )
        for path in sorted(item for item in listed.stdout.split("\0") if item):
            blob = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                cwd=root_path,
                capture_output=True,
                timeout=60,
            )
            if blob.returncode != 0:
                raise ValidationFailure(
                    f"trusted ref {ref} does not contain protected path {path}"
                )
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_normalized_digest(path, blob.stdout).encode())
            digest.update(b"\0")
    if not protected:
        digest.update(b"NO_PROTECTED_PATHS")
    return digest.hexdigest()


def protected_fingerprint_at_head(root: str | Path, protected_paths: list[str]) -> str:
    return protected_fingerprint_at_ref(root, protected_paths, "HEAD")


def protected_files_match_ref(
    root: str | Path, protected_paths: list[str], ref: str
) -> bool:
    if not protected_paths:
        return True
    if ref in {"UNKNOWN", "UNBORN", "NO_GIT"}:
        return False
    return protected_fingerprint(root, protected_paths) == protected_fingerprint_at_ref(
        root, protected_paths, ref
    )


def protected_files_match_head(root: str | Path, protected_paths: list[str]) -> bool:
    return protected_files_match_ref(root, protected_paths, "HEAD")


def ignored_workspace_files(
    root: str | Path, *, excludes: set[str] | None = None
) -> list[tuple[str, Path]]:
    root_path = Path(root).resolve()
    excluded = _DEFAULT_EXCLUDES | (excludes or set())
    if not is_git_repo(root_path):
        return []
    listed = git(
        root_path,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        check=False,
    )
    if listed.returncode != 0:
        return []
    files: list[tuple[str, Path]] = []
    for relative in listed.stdout.split("\0"):
        if not relative or _is_excluded(relative, excluded):
            continue
        path = root_path / relative
        if path.is_file():
            files.append((relative, path))
    return files


def _normalize_protected(paths: list[str]) -> list[str]:
    return [path.replace("\\", "/").rstrip("/") for path in paths if path.strip()]


def _matches_protected(relative: str, protected: list[str]) -> bool:
    return any(relative == path or relative.startswith(path + "/") for path in protected)


def git(root: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=check,
    )


def is_git_repo(root: str | Path) -> bool:
    try:
        return git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def git_commit(root: str | Path) -> str:
    if not is_git_repo(root):
        return "NO_GIT"
    result = git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else "UNBORN"


def git_tree(root: str | Path) -> str:
    if not is_git_repo(root):
        return "NO_GIT"
    result = git(root, "rev-parse", "HEAD^{tree}", check=False)
    return result.stdout.strip() if result.returncode == 0 else "UNBORN"


def diff_fingerprint(root: str | Path) -> str:
    if not is_git_repo(root):
        return source_fingerprint(root)
    digest = hashlib.sha256()
    diff = git(root, "diff", "--binary", "HEAD", check=False)
    digest.update(diff.stdout.encode("utf-8", errors="replace"))
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    digest.update(status.stdout.encode("utf-8", errors="replace"))
    untracked = git(root, "ls-files", "--others", "--exclude-standard", check=False)
    for relative in sorted(line for line in untracked.stdout.splitlines() if line):
        path = Path(root) / relative
        try:
            digest.update(relative.encode())
            digest.update(sha256_file(path).encode())
        except OSError:
            digest.update(b"UNREADABLE")
    return digest.hexdigest()


def snapshot_worktree(root: str | Path) -> dict[str, str]:
    """Create a temporary git commit representing the full worktree without mutation.

    Uses a private index so the real index, HEAD and branches are untouched. Returns
    ``{"commit": ..., "tree": ...}``. Raises ExternalBlockedError outside git.
    """
    if not is_git_repo(root):
        raise ExternalBlockedError("clean-room snapshot requires a Git repository")
    root_path = Path(root).resolve()
    head = git_commit(root_path)
    with tempfile.TemporaryDirectory(prefix="forge-snapshot-") as temp:
        index = Path(temp) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index)
        if head not in {"UNBORN", "NO_GIT"}:
            read = subprocess.run(
                ["git", "read-tree", "HEAD"],
                cwd=root_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if read.returncode != 0:
                raise ValidationFailure(f"git read-tree failed: {read.stderr.strip()}")
        added = subprocess.run(
            ["git", "add", "-A"],
            cwd=root_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if added.returncode != 0:
            raise ValidationFailure(f"git add -A snapshot failed: {added.stderr.strip()}")
        tree_result = subprocess.run(
            ["git", "write-tree"],
            cwd=root_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if tree_result.returncode != 0:
            raise ValidationFailure(f"git write-tree failed: {tree_result.stderr.strip()}")
        tree = tree_result.stdout.strip()
        commit_args = ["git", "commit-tree", tree, "-m", "forgeflow trusted snapshot"]
        if head not in {"UNBORN", "NO_GIT"}:
            commit_args.extend(["-p", head])
        commit_result = subprocess.run(
            commit_args,
            cwd=root_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if commit_result.returncode != 0:
            raise ValidationFailure(f"git commit-tree failed: {commit_result.stderr.strip()}")
        return {"commit": commit_result.stdout.strip(), "tree": tree}


def tool_version(executable: str, args: tuple[str, ...] = ("--version",)) -> str:
    found = shutil.which(executable)
    if not found:
        return "MISSING"
    try:
        result = subprocess.run(
            [found, *args],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0][:200] if output else f"exit={result.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR:{exc.__class__.__name__}"


def environment_digest(root: str | Path, *, include_tools: bool = True) -> str:
    root_path = Path(root).resolve()
    lock_hashes: dict[str, str] = {}
    for name in ("pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock", "package-lock.json"):
        path = root_path / name
        if path.exists():
            lock_hashes[name] = sha256_file(path)
    packages: dict[str, str] = {}
    for package in _DEPENDENCY_PACKAGES:
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "MISSING"
    tools = {}
    if include_tools:
        for executable in ("git", "uv", "python", "pytest", "ruff", "node", "iverilog"):
            if executable == "python":
                tools[executable] = sys.version.split()[0]
            else:
                tools[executable] = tool_version(executable)
    payload: dict[str, Any] = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "locks": lock_hashes,
        "packages": packages,
        "tools": tools,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def environment_summary(root: str | Path) -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "git": tool_version("git"),
        "uv": tool_version("uv"),
        "node": tool_version("node"),
        "pytest": tool_version("pytest"),
        "ruff": tool_version("ruff"),
        "docker": tool_version("docker", ("version", "--format", "{{.Server.Version}}")),
        "iverilog": tool_version("iverilog"),
        "verilator": tool_version("verilator"),
        "pi": tool_version("pi"),
        "claude": tool_version("claude"),
        "workspace": str(Path(root).resolve()),
    }
