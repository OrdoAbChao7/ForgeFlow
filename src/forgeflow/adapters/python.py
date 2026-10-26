from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forgeflow.domain.errors import ExternalBlockedError
from forgeflow.runner.command import CommandPolicy, CommandRunner, RunRequest
from forgeflow.verification.fingerprint import tool_version


@dataclass
class PythonCheck:
    name: str
    status: str
    exit_code: int | None
    command: list[str]
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    report_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "exit_code": self.exit_code,
            "command": self.command,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "report_path": self.report_path,
        }


class PythonAdapter:
    """Python/GitHub quality adapter with separate pytest, ruff, typecheck, coverage."""

    name = "python"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.runner = CommandRunner(
            self.root,
            log_root=self.root / ".forge" / "runs",
            policy=CommandPolicy(allow_workspace_writes=True),
        )

    def detect(self) -> dict[str, str]:
        return {
            "python": sys.version.split()[0],
            "pytest": tool_version("pytest"),
            "ruff": tool_version("ruff"),
            "pyright": tool_version("pyright"),
            "mypy": tool_version("mypy"),
            "coverage": tool_version("coverage"),
        }

    def run_checks(
        self,
        *,
        test_path: str = "tests",
        include_coverage: bool = False,
    ) -> dict[str, Any]:
        checks = []
        pytest_exe = shutil.which("pytest")
        if pytest_exe:
            result = self.runner.run(
                RunRequest(
                    argv=[pytest_exe, "-q", test_path],
                    cwd=self.root,
                    purpose="python:pytest",
                    timeout_seconds=300,
                    writes=True,
                )
            )
            checks.append(
                PythonCheck(
                    "pytest",
                    "PASS" if result.succeeded else "FAIL",
                    result.exit_code,
                    result.argv,
                    result.stdout_excerpt,
                    result.stderr_excerpt,
                )
            )
        else:
            checks.append(PythonCheck("pytest", "NOT_RUN", None, [], stderr_excerpt="pytest missing"))
        ruff_exe = shutil.which("ruff")
        if ruff_exe:
            result = self.runner.run(
                RunRequest(
                    argv=[ruff_exe, "check", "."],
                    cwd=self.root,
                    purpose="python:ruff",
                    timeout_seconds=120,
                    writes=False,
                )
            )
            checks.append(
                PythonCheck(
                    "ruff",
                    "PASS" if result.succeeded else "FAIL",
                    result.exit_code,
                    result.argv,
                    result.stdout_excerpt,
                    result.stderr_excerpt,
                )
            )
        else:
            checks.append(PythonCheck("ruff", "NOT_RUN", None, [], stderr_excerpt="ruff missing"))
        typechecker = shutil.which("pyright") or shutil.which("mypy")
        if typechecker:
            argv = (
                [typechecker]
                if Path(typechecker).name.lower().startswith("pyright")
                else [typechecker, "src"]
            )
            result = self.runner.run(
                RunRequest(
                    argv=argv,
                    cwd=self.root,
                    purpose="python:typecheck",
                    timeout_seconds=300,
                    writes=False,
                )
            )
            checks.append(
                PythonCheck(
                    "typecheck",
                    "PASS" if result.succeeded else "FAIL",
                    result.exit_code,
                    result.argv,
                    result.stdout_excerpt,
                    result.stderr_excerpt,
                )
            )
        else:
            checks.append(
                PythonCheck(
                    "typecheck",
                    "NOT_RUN",
                    None,
                    [],
                    stderr_excerpt="neither pyright nor mypy is installed",
                )
            )
        if include_coverage:
            checks.append(self._coverage())
        return {
            "adapter": self.name,
            "checks": [check.as_dict() for check in checks],
            "separate_reports": True,
            "github": "NOT_CONNECTED",
        }

    def _coverage(self) -> PythonCheck:
        coverage_exe = shutil.which("coverage")
        if not coverage_exe:
            return PythonCheck("coverage", "NOT_RUN", None, [], stderr_excerpt="coverage missing")
        report = self.root / ".forge" / "coverage.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        run = self.runner.run(
            RunRequest(
                argv=[coverage_exe, "run", "-m", "pytest", "-q"],
                cwd=self.root,
                purpose="python:coverage-run",
                timeout_seconds=300,
                writes=True,
            )
        )
        if not run.succeeded:
            return PythonCheck(
                "coverage",
                "FAIL",
                run.exit_code,
                run.argv,
                run.stdout_excerpt,
                run.stderr_excerpt,
            )
        export = self.runner.run(
            RunRequest(
                argv=[coverage_exe, "json", "-o", str(report)],
                cwd=self.root,
                purpose="python:coverage-report",
                timeout_seconds=120,
                writes=True,
            )
        )
        if not export.succeeded:
            return PythonCheck(
                "coverage",
                "FAIL",
                export.exit_code,
                export.argv,
                export.stdout_excerpt,
                export.stderr_excerpt,
            )
        data = json.loads(report.read_text(encoding="utf-8"))
        percent = float(data.get("totals", {}).get("percent_covered", 0.0))
        return PythonCheck(
            "coverage",
            "PASS",
            0,
            export.argv,
            f"percent_covered={percent:.2f}",
            report_path=str(report),
        )


class GitHubPRAdapter:
    """Local CI simulation + GitHub contract boundary.

    No network is attempted here; a real CI integration must supply an authenticated
    connector and distinct evidence for each check.
    """

    def __init__(self, root: str | Path, *, token: str | None = None, repository: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.token = token
        self.repository = repository

    @property
    def connected(self) -> bool:
        return bool(self.token and self.repository)

    def simulate_ci(self, checks: dict[str, Any]) -> dict[str, Any]:
        failures = [
            check["name"]
            for check in checks.get("checks", [])
            if check["status"] == "FAIL"
        ]
        not_run = [
            check["name"]
            for check in checks.get("checks", [])
            if check["status"] == "NOT_RUN"
        ]
        return {
            "provider": "local_simulated_ci",
            "status": "FAIL" if failures else "PASS",
            "failures": failures,
            "not_run": not_run,
            "github_connected": self.connected,
            "external_status": "NOT_CONNECTED" if not self.connected else "CONNECTED",
        }

    def require_connection(self) -> None:
        if not self.connected:
            raise ExternalBlockedError(
                "GitHub PR adapter requires an authenticated connector; no credential was used"
            )
