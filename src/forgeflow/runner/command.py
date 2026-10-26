from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from forgeflow.domain.errors import PathEscapeError, PolicyDeniedError, ValidationFailure
from forgeflow.domain.models import canonical_json

_MAX_CAPTURE = 64 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunRequest:
    argv: list[str]
    cwd: str | Path
    purpose: str = "verifier"
    timeout_seconds: float = 120.0
    environment: dict[str, str] = field(default_factory=dict)
    network: bool = False
    irreversible: bool = False
    writes: bool = False
    run_id: str | None = None

    def fingerprint(self, root: Path) -> str:
        payload = {
            "argv": self.argv,
            "cwd": str(Path(self.cwd).resolve().relative_to(root)),
            "purpose": self.purpose,
            "timeout_seconds": self.timeout_seconds,
            "environment": self.environment,
            "network": self.network,
            "irreversible": self.irreversible,
            "writes": self.writes,
        }
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


@dataclass(frozen=True)
class Approval:
    approved_by: str
    reason: str
    request_fingerprint: str

    @classmethod
    def for_request(cls, request: RunRequest, root: Path, *, approved_by: str, reason: str) -> Approval:
        return cls(approved_by=approved_by, reason=reason, request_fingerprint=request.fingerprint(root))

    def applies_to(self, request: RunRequest, root: Path) -> bool:
        return self.request_fingerprint == request.fingerprint(root)


@dataclass
class RunResult:
    run_id: str
    argv: list[str]
    cwd: str
    purpose: str
    exit_code: int
    timed_out: bool
    duration_seconds: float
    started_at: datetime
    ended_at: datetime
    pid: int
    stdout_path: str
    stderr_path: str
    result_path: str
    stdout_sha256: str
    stderr_sha256: str
    result_sha256: str
    stdout_excerpt: str
    stderr_excerpt: str
    output_truncated: bool = False
    capture_incomplete: bool = False
    approved_by: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.capture_incomplete

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "argv": self.argv,
            "cwd": self.cwd,
            "purpose": self.purpose,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "pid": self.pid,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "result_path": self.result_path,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "result_sha256": self.result_sha256,
            "output_truncated": self.output_truncated,
            "capture_incomplete": self.capture_incomplete,
            "approved_by": self.approved_by,
        }


class CommandPolicy:
    DEFAULT_ALLOWED = frozenset(
        {
            "python",
            "python3",
            "python.exe",
            "py",
            "pytest",
            "pytest.exe",
            "ruff",
            "ruff.exe",
            "pyright",
            "pyright.exe",
            "mypy",
            "mypy.exe",
            "coverage",
            "coverage.exe",
            "git",
            "git.exe",
            "uv",
            "uv.exe",
            "node",
            "node.exe",
            "npm",
            "npm.cmd",
            "npx",
            "npx.cmd",
            "iverilog",
            "vvp",
            "verilator",
        }
    )
    NETWORK_EXECUTABLES = frozenset({"curl", "wget", "ssh", "scp", "ftp", "nc", "ncat"})
    SHELL_EXECUTABLES = frozenset(
        {"sh", "bash", "zsh", "fish", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"}
    )
    FORBIDDEN_ENV = frozenset({"LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PYTHONSTARTUP"})
    IMMUTABLE_ENV = frozenset({"PATH", "PATHEXT", "PYTHONPATH", "PYTHONHOME", "NODE_PATH"})
    NETWORK_GIT = frozenset({"push", "fetch", "pull", "clone", "remote", "ls-remote"})
    IRREVERSIBLE_GIT = frozenset({"clean", "reset"})

    def __init__(
        self,
        *,
        allowed_executables: set[str] | frozenset[str] | None = None,
        allow_workspace_writes: bool = True,
        allow_network: bool = False,
        allow_shell: bool = False,
    ) -> None:
        self.allowed_executables = frozenset(
            self.DEFAULT_ALLOWED if allowed_executables is None else allowed_executables
        )
        self.allow_workspace_writes = allow_workspace_writes
        self.allow_network = allow_network
        self.allow_shell = allow_shell

    def authorization_required(self, request: RunRequest) -> list[str]:
        reasons: list[str] = []
        if not request.argv:
            raise ValidationFailure("argv must not be empty")
        executable = Path(request.argv[0]).name.lower()
        if executable not in self.allowed_executables:
            reasons.append(f"executable not allowlisted: {executable}")
        if executable in self.NETWORK_EXECUTABLES or request.network:
            reasons.append("network access")
        if executable in self.SHELL_EXECUTABLES and not self.allow_shell:
            reasons.append("shell execution")
        if request.irreversible:
            reasons.append("irreversible operation")
        if request.writes and not self.allow_workspace_writes:
            reasons.append("workspace write")
        if executable.startswith("git"):
            subcommands = {arg.lower() for arg in request.argv[1:]}
            if subcommands & self.NETWORK_GIT:
                reasons.append("network git operation")
            if subcommands & self.IRREVERSIBLE_GIT:
                reasons.append("irreversible git operation")
        if request.environment and self.FORBIDDEN_ENV & set(request.environment):
            reasons.append("forbidden environment override")
        if self.allow_network and "network access" in reasons and executable in self.NETWORK_EXECUTABLES:
            # Network tools still need explicit approval unless policy intentionally allows.
            reasons.remove("network access")
        return reasons

    def authorize(self, request: RunRequest, root: Path, approval: Approval | None) -> str | None:
        if not request.argv or any("\x00" in item for item in request.argv):
            raise ValidationFailure("argv must be non-empty and contain no NUL bytes")
        if request.timeout_seconds <= 0 or request.timeout_seconds > 3600:
            raise ValidationFailure("timeout must be in (0, 3600]")
        if any(key.startswith("FORGE_") for key in request.environment):
            raise PolicyDeniedError("verifier environment may not override FORGE_* variables")
        reasons = self.authorization_required(request)
        if reasons:
            if approval is None or not approval.applies_to(request, root):
                raise PolicyDeniedError(
                    "command requires explicit approval: " + ", ".join(sorted(set(reasons))),
                    details={"reasons": sorted(set(reasons)), "argv": request.argv},
                )
            return approval.approved_by
        return None


class CommandRunner:
    def __init__(
        self,
        root: str | Path,
        *,
        log_root: str | Path | None = None,
        policy: CommandPolicy | None = None,
        max_log_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        self.log_root = Path(log_root).resolve() if log_root else self.root / ".forge" / "runs"
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or CommandPolicy()
        self.max_log_bytes = max_log_bytes

    def _resolve_cwd(self, cwd: str | Path) -> Path:
        candidate = Path(cwd)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathEscapeError(f"cwd escapes workspace root: {resolved}") from exc
        if not resolved.is_dir():
            raise ValidationFailure(f"cwd is not a directory: {resolved}")
        return resolved

    def _environment(self, request_environment: dict[str, str]) -> dict[str, str]:
        allowlist = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "COMSPEC",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "VIRTUAL_ENV",
            "UV_CACHE_DIR",
            "UV_OFFLINE",
            "PYTHONIOENCODING",
            "PYTHONUTF8",
            "PYTHONPATH",
            "NODE_PATH",
            "CI",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowlist}
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUTF8", "1")
        for key, value in request_environment.items():
            if not key or not key.replace("_", "").isalnum():
                raise ValidationFailure(f"invalid environment key: {key!r}")
            if (
                key in CommandPolicy.FORBIDDEN_ENV
                or key in CommandPolicy.IMMUTABLE_ENV
                or key.startswith("FORGE_")
            ):
                raise PolicyDeniedError(f"environment override denied: {key}")
            environment[key] = value
        return environment

    def run(self, request: RunRequest, *, approval: Approval | None = None) -> RunResult:
        cwd = self._resolve_cwd(request.cwd)
        approved_by = self.policy.authorize(request, self.root, approval)
        run_id = request.run_id or f"run_{uuid.uuid4().hex}"
        log_dir = self.log_root / run_id
        log_dir.mkdir(parents=True, exist_ok=False)
        stdout_path = log_dir / "stdout.txt"
        stderr_path = log_dir / "stderr.txt"
        result_path = log_dir / "result.json"
        environment = self._environment(request.environment)
        # Resolve with the harness's trusted PATH, not a verifier-supplied suggestion.
        executable = shutil.which(request.argv[0])
        if executable is None:
            raise ValidationFailure(f"executable not found: {request.argv[0]}")
        started_at = _now()
        monitor = time.monotonic()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [executable, *request.argv[1:]],
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        truncated = False
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            lock = threading.Lock()
            state = {"truncated": False}

            def pump(pipe: TextIO | Any, handle: Any) -> None:
                nonlocal state
                written = 0
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        break
                    if written < self.max_log_bytes:
                        allowed = chunk[: self.max_log_bytes - written]
                        handle.write(allowed)
                        written += len(allowed)
                        if len(allowed) < len(chunk):
                            with lock:
                                state["truncated"] = True
                    else:
                        with lock:
                            state["truncated"] = True
                pipe.close()

            stdout_thread = threading.Thread(
                target=pump, args=(process.stdout, stdout_handle), daemon=True
            )
            stderr_thread = threading.Thread(
                target=pump, args=(process.stderr, stderr_handle), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()
            timed_out = False
            try:
                process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_tree(process)
                process.wait(timeout=10)
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            capture_incomplete = stdout_thread.is_alive() or stderr_thread.is_alive()
            if capture_incomplete:
                self._kill_tree(process, force_group=True)
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)
                capture_incomplete = stdout_thread.is_alive() or stderr_thread.is_alive()
            else:
                capture_incomplete = False
            truncated = state["truncated"]
        ended_at = _now()
        duration = time.monotonic() - monitor
        stdout_sha = _hash_file(stdout_path)
        stderr_sha = _hash_file(stderr_path)
        result = RunResult(
            run_id=run_id,
            argv=list(request.argv),
            cwd=str(cwd),
            purpose=request.purpose,
            exit_code=process.returncode,
            timed_out=timed_out,
            duration_seconds=round(duration, 6),
            started_at=started_at,
            ended_at=ended_at,
            pid=process.pid,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            result_path=str(result_path),
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
            result_sha256="",
            stdout_excerpt=_excerpt(stdout_path),
            stderr_excerpt=_excerpt(stderr_path),
            output_truncated=truncated,
            capture_incomplete=capture_incomplete,
            approved_by=approved_by,
        )
        payload = result.as_dict()
        payload["command_display"] = shlex.join(request.argv)
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(result_path)
        result.result_sha256 = _hash_file(result_path)
        # Include the final report hash in a second atomic write.
        payload["result_sha256"] = result.result_sha256
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(result_path)
        result.result_sha256 = _hash_file(result_path)
        return result

    def _kill_tree(self, process: subprocess.Popen[Any], *, force_group: bool = False) -> None:
        if process.poll() is not None and not force_group:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:
                    process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excerpt(path: Path, limit: int = _MAX_CAPTURE) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += "\n...[truncated excerpt; full log retained]"
    return text
