from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.domain.enums import TrustLevel
from forgeflow.domain.errors import ExternalBlockedError, ForgeFlowError, VerificationError
from forgeflow.domain.models import Evidence, GoalSpec, new_id
from forgeflow.runner.command import CommandPolicy, CommandRunner, RunRequest, RunResult
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import (
    environment_digest,
    ignored_workspace_files,
    protected_fingerprint,
    snapshot_worktree,
    source_fingerprint,
    workspace_files,
)
from forgeflow.verification.gates import Binding, EvidenceRecorder


@dataclass
class CleanRoomReport:
    workspace: str
    success: bool
    binding: Binding | None
    results: list[RunResult] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    setup_result: RunResult | None = None
    errors: list[str] = field(default_factory=list)
    protected_restored: list[str] = field(default_factory=list)
    docker_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "success": self.success,
            "binding": self.binding.__dict__ if self.binding else None,
            "results": [result.as_dict() for result in self.results],
            "evidence_ids": self.evidence_ids,
            "setup_result": self.setup_result.as_dict() if self.setup_result else None,
            "errors": self.errors,
            "protected_restored": self.protected_restored,
            "docker_used": self.docker_used,
        }


def _git_bytes(root: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _extract(root: Path, commit: str, destination: Path, paths: list[str] | None = None) -> None:
    command = ["git", "archive", "--format=tar", commit]
    if paths:
        command.extend(paths)
    data = _git_bytes(root, command[1:])
    destination_resolved = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise VerificationError(f"unsafe archive member: {member.name}")
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination_resolved)
            except ValueError as exc:
                raise VerificationError(f"archive path escapes clean room: {member.name}") from exc
        try:
            archive.extractall(destination, filter="data")
        except TypeError:  # Python versions before extraction filters were backported
            for member in archive.getmembers():
                archive.extract(member, destination, set_attrs=False, numeric_owner=False)


def docker_available() -> bool:
    executable = shutil.which("docker")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class CleanRoomVerifier:
    """Rebuild a source snapshot independently and run trusted verifier config.

    A private-index Git snapshot captures the worktree without mutating the real index.
    Protected verifier files are restored from the goal's immutable baseline commit
    before setup and verification, so an agent cannot make tests pass by editing or
    committing them. Docker is optional and only reported when the daemon is usable.
    """

    def __init__(
        self,
        store: Store,
        workspace: str | Path,
        spec: GoalSpec,
        *,
        use_docker: bool = False,
    ) -> None:
        self.store = store
        self.workspace = Path(workspace).resolve()
        self.spec = spec
        self.use_docker = use_docker

    def verify(
        self,
        goal_id: str,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
        command_ids: list[str] | None = None,
    ) -> CleanRoomReport:
        if self.use_docker and not docker_available():
            raise ExternalBlockedError(
                "clean-room Docker requested but Docker daemon is unavailable",
                details={"fallback": "run with use_docker=False"},
            )
        if self.use_docker:
            # The Docker path is intentionally not silently emulated.
            raise ExternalBlockedError(
                "Docker clean-room execution is not enabled in this build; "
                "use the local isolated workspace path instead"
            )
        snapshot = snapshot_worktree(self.workspace)
        goal = self.store.get_goal(goal_id)
        revision = goal.current_revision
        trusted_ref = goal.baseline_commit
        report = CleanRoomReport(
            workspace="", success=False, binding=None, docker_used=False
        )
        if trusted_ref in {"UNKNOWN", "UNBORN", "NO_GIT"}:
            report.errors.append(
                "goal has no immutable Git baseline commit; protected verifier files "
                "cannot be trusted"
            )
            return report
        with tempfile.TemporaryDirectory(prefix="forge-cleanroom-") as temp:
            clean = Path(temp).resolve()
            report.workspace = str(clean)
            _extract(self.workspace, snapshot["commit"], clean)
            for relative, source in ignored_workspace_files(self.workspace):
                target = (clean / relative).resolve()
                target.relative_to(clean)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            self._restore_protected(trusted_ref, clean, report)
            current_source = source_fingerprint(
                self.workspace, protected_paths=self.spec.verifier.protected_paths
            )
            clean_source = source_fingerprint(
                clean, protected_paths=self.spec.verifier.protected_paths
            )
            if current_source != clean_source:
                report.errors.append(
                    "clean-room source fingerprint differs from live workspace; "
                    "refusing to issue clean-room evidence"
                )
                return report
            expected_protected = protected_fingerprint(
                clean, self.spec.verifier.protected_paths
            )
            current_protected = protected_fingerprint(
                self.workspace, self.spec.verifier.protected_paths
            )
            if expected_protected != current_protected:
                report.errors.append(
                    "protected verifier files differ from the immutable goal baseline; "
                    "restore tests/config before clean-room verification"
                )
                return report
            binding = Binding(
                commit_sha=snapshot["commit"],
                tree_hash=snapshot["tree"],
                diff_hash="cleanroom-snapshot",
                source_fingerprint=clean_source,
                protected_fingerprint=expected_protected,
                env_digest=environment_digest(self.workspace),
            )
            report.binding = binding
            log_root = self.workspace / ".forge" / "cleanroom" / new_id("cr")
            policy = CommandPolicy(
                # The clean room only runs the trusted goal verifier config.
                allow_workspace_writes=True,
                allow_network=self.spec.verifier.network,
            )
            runner = CommandRunner(clean, log_root=log_root, policy=policy)
            if self.spec.verifier.setup_argv:
                env = {"UV_OFFLINE": "1"} if not self.spec.verifier.network else {}
                try:
                    setup = runner.run(
                        RunRequest(
                            argv=list(self.spec.verifier.setup_argv),
                            cwd=clean,
                            purpose="cleanroom_setup",
                            timeout_seconds=600,
                            environment=env,
                            network=self.spec.verifier.network,
                            writes=True,
                        )
                    )
                except ForgeFlowError as exc:
                    report.errors.append(f"clean-room dependency setup failed: {exc}")
                    return report
                report.setup_result = setup
                if not setup.succeeded:
                    report.errors.append(
                        f"clean-room dependency setup failed with exit code {setup.exit_code}"
                    )
                    return report
            commands = self.spec.verifier.commands
            if command_ids:
                commands = [command for command in commands if command.id in set(command_ids)]
            recorder = EvidenceRecorder(self.store, self.workspace, self.spec)
            for command in commands:
                cwd = (clean / command.cwd).resolve() if command.cwd != "." else clean
                try:
                    result = runner.run(
                        RunRequest(
                            argv=list(command.argv),
                            cwd=cwd,
                            purpose=f"cleanroom:{command.id}",
                            timeout_seconds=command.timeout_seconds,
                            environment=command.environment,
                            network=self.spec.verifier.network,
                            writes=True,
                        )
                    )
                except Exception as exc:
                    report.errors.append(f"{command.id}: {exc}")
                    report.success = False
                    continue
                report.results.append(result)
                scope = command.scope or [
                    item.id
                    for item in self.spec.acceptance
                    if item.required and item.gate == command.gate
                ]
                if not scope:
                    report.errors.append(
                        f"{command.id}: no contract scope for clean-room evidence"
                    )
                    continue
                evidence: Evidence = recorder.record_command(
                    goal_id=goal_id,
                    revision=revision,
                    gate=command.gate,
                    result=result,
                    scope=scope,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    trust=TrustLevel.CLEAN_ROOM,
                    runner_kind="cleanroom",
                    binding=binding,
                    metadata={
                        "cleanroom": True,
                        "trusted_commit": snapshot["commit"],
                        "protected_restored": report.protected_restored,
                        "docker_used": False,
                        "isolated_workspace": str(clean),
                    },
                )
                report.evidence_ids.append(evidence.id)
            report.success = (
                not report.errors
                and bool(report.results)
                and all(result.succeeded for result in report.results)
            )
        return report

    def _restore_protected(
        self, trusted_ref: str, clean: Path, report: CleanRoomReport
    ) -> None:
        if not self.spec.verifier.protected_paths:
            return
        for relative in self.spec.verifier.protected_paths:
            target = (clean / relative).resolve()
            try:
                target.relative_to(clean)
            except ValueError as exc:
                raise VerificationError(f"protected path escapes clean room: {relative}") from exc
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if trusted_ref in {"UNBORN", "NO_GIT"}:
                continue
            listed = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", trusted_ref, "--", relative],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if listed.stdout.strip():
                _extract(self.workspace, trusted_ref, clean, [relative])
                report.protected_restored.append(relative)
            else:
                report.protected_restored.append(
                    f"{relative} (absent in trusted baseline, removed)"
                )


def clean_room_environment(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    files = [path for path, _ in workspace_files(root)]
    return {
        "file_count": len(files),
        "env_digest": environment_digest(root),
        "docker_available": docker_available(),
    }
