from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def init_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "core.longpaths", "true")
    git(path, "config", "user.email", "tests@forgeflow.invalid")
    git(path, "config", "user.name", "ForgeFlow Tests")
    for relative, content in (files or {"README.md": "fixture\n"}).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-m", "fixture baseline")
    return path


def calc_project(path: Path) -> Path:
    return init_repo(
        path,
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": (
                "from calc import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
            ),
            "conftest.py": "",
        },
    )


def python_command(*args: str, scope: list[str] | None = None):
    from forgeflow.domain.enums import GateKind
    from forgeflow.domain.models import VerifierCommand

    return VerifierCommand(
        id="unit",
        gate=GateKind.UNIT,
        argv=[sys.executable, "-m", "pytest", "-q", "tests"],
        timeout_seconds=120,
        scope=scope or ["adds"],
    )


def calc_spec(*, mock_files: dict[str, str] | None = None):
    from forgeflow.domain.enums import GateKind
    from forgeflow.domain.models import (
        AcceptanceCriterion,
        GoalSpec,
        TaskSpec,
        VerifierConfig,
    )

    return GoalSpec(
        title="Fix add",
        objective="Make add return the sum.",
        acceptance=[
            AcceptanceCriterion(
                id="adds",
                statement="add returns a+b",
                gate=GateKind.UNIT,
                required=True,
            )
        ],
        tasks=[
            TaskSpec(
                key="fix-add",
                title="Fix add",
                description="Implement arithmetic addition.",
                owned_paths=["calc.py"],
                gates=[GateKind.UNIT],
                acceptance_ids=["adds"],
                max_attempts=2,
                metadata={
                    "mock": {
                        "files": mock_files
                        or {"calc.py": "def add(a, b):\n    return a + b\n"}
                    }
                },
            )
        ],
        verifier=VerifierConfig(
            commands=[python_command()],
            protected_paths=["tests"],
            clean_room=True,
            policy_version="test-policy-v1",
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return calc_project(tmp_path / "project")


@pytest.fixture
def spec():
    return calc_spec()
