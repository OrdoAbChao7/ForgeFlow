from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from forgeflow.domain.errors import PathEscapeError, PolicyDeniedError
from forgeflow.runner.command import Approval, CommandPolicy, CommandRunner, RunRequest


def test_runner_captures_exit_logs_hashes_and_truncates_excerpt(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    code = (
        "import sys; "
        "sys.stdout.write('x'*100000); "
        "sys.stderr.write('err-line\\n'); "
        "sys.exit(3)"
    )
    result = runner.run(
        RunRequest(
            argv=[sys.executable, "-c", code],
            cwd=tmp_path,
            purpose="test",
            timeout_seconds=30,
            writes=True,
        )
    )
    assert result.exit_code == 3
    assert not result.timed_out
    assert len(result.stdout_excerpt) < 100000
    assert "truncated excerpt" in result.stdout_excerpt
    assert result.stdout_sha256
    assert result.stderr_excerpt.strip() == "err-line"
    assert Path(result.stdout_path).stat().st_size == 100000
    assert Path(result.result_path).is_file()


def test_runner_blocks_path_escape(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path / "root")
    with pytest.raises(PathEscapeError):
        runner.run(
            RunRequest(
                argv=[sys.executable, "-c", "print(1)"],
                cwd=tmp_path,
            )
        )


def test_policy_blocks_unknown_network_and_shell(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    with pytest.raises(PolicyDeniedError):
        runner.run(
            RunRequest(
                argv=["curl", "https://example.invalid"],
                cwd=tmp_path,
                network=True,
            )
        )
    with pytest.raises(PolicyDeniedError):
        runner.run(
            RunRequest(
                argv=["bash", "-c", "echo hi"],
                cwd=tmp_path,
                writes=True,
            )
        )
    with pytest.raises(PolicyDeniedError):
        runner.run(
            RunRequest(
                argv=[sys.executable, "-c", "print(1)"],
                cwd=tmp_path,
                irreversible=True,
            )
        )


def test_explicit_approval_is_bound_to_exact_request(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    request = RunRequest(
        argv=[sys.executable, "-c", "print('approved')"],
        cwd=tmp_path,
        network=True,
        timeout_seconds=30,
    )
    with pytest.raises(PolicyDeniedError):
        runner.run(request)
    approval = Approval.for_request(
        request, tmp_path, approved_by="tester", reason="unit test authorization"
    )
    result = runner.run(request, approval=approval)
    assert result.succeeded
    assert result.approved_by == "tester"
    other = RunRequest(
        argv=[sys.executable, "-c", "print('different')"],
        cwd=tmp_path,
        network=True,
    )
    with pytest.raises(PolicyDeniedError):
        runner.run(other, approval=approval)


def test_environment_allowlist_and_forbidden_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FORGE_TEST_SECRET", "do-not-leak")
    runner = CommandRunner(tmp_path)
    result = runner.run(
        RunRequest(
            argv=[sys.executable, "-c", "import os; print(os.environ.get('FORGE_TEST_SECRET'))"],
            cwd=tmp_path,
            writes=False,
        )
    )
    assert result.succeeded
    assert "do-not-leak" not in result.stdout_excerpt
    with pytest.raises(PolicyDeniedError):
        runner.run(
            RunRequest(
                argv=[sys.executable, "-c", "print(1)"],
                cwd=tmp_path,
                environment={"FORGE_API_KEY": "secret"},
            )
        )
    for key in ("PATH", "PYTHONPATH", "NODE_PATH"):
        with pytest.raises(PolicyDeniedError):
            runner.run(
                RunRequest(
                    argv=[sys.executable, "-c", "print(1)"],
                    cwd=tmp_path,
                    environment={key: str(tmp_path)},
                )
            )


def test_timeout_kills_process(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    started = time.monotonic()
    result = runner.run(
        RunRequest(
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=0.4,
        )
    )
    assert result.timed_out
    assert time.monotonic() - started < 15
    assert result.exit_code != 0


def test_policy_allowlist_can_be_narrowed(tmp_path: Path) -> None:
    policy = CommandPolicy(allowed_executables={"custom"})
    runner = CommandRunner(tmp_path, policy=policy)
    with pytest.raises(PolicyDeniedError):
        runner.run(
            RunRequest(
                argv=["git", "--version"],
                cwd=tmp_path,
            )
        )
