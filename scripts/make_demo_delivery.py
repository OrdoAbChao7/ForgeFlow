"""Generate a reproducible READY delivery artifact for evidence/."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from forgeflow.delivery import build_delivery
from forgeflow.domain.enums import GateKind
from forgeflow.domain.models import (
    AcceptanceCriterion,
    GoalSpec,
    TaskSpec,
    VerifierCommand,
    VerifierConfig,
)
from forgeflow.runtime import Harness, RunOptions

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence" / "delivery" / "demo"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="forgeflow-delivery-demo-"))
    try:
        (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (work / "tests").mkdir()
        (work / "tests" / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (work / "conftest.py").write_text("", encoding="utf-8")
        (work / ".gitignore").write_text(".forge/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        git(work, "init", "-b", "main")
        git(work, "config", "user.email", "delivery@forgeflow.invalid")
        git(work, "config", "user.name", "ForgeFlow Delivery")
        git(work, "add", "-A")
        git(work, "commit", "-m", "delivery demo baseline")
        spec = GoalSpec(
            title="Delivery demo",
            objective="Produce a READY manifest from independently verified add().",
            acceptance=[
                AcceptanceCriterion(
                    id="adds", statement="add returns a+b", gate=GateKind.UNIT, required=True
                )
            ],
            tasks=[
                TaskSpec(
                    key="fix-add",
                    title="Fix add",
                    owned_paths=["calc.py"],
                    gates=[GateKind.UNIT],
                    acceptance_ids=["adds"],
                    metadata={
                        "mock": {"files": {"calc.py": "def add(a, b):\n    return a + b\n"}}
                    },
                )
            ],
            verifier=VerifierConfig(
                commands=[
                    VerifierCommand(
                        id="unit",
                        gate=GateKind.UNIT,
                        argv=[sys.executable, "-m", "pytest", "-q", "tests"],
                        scope=["adds"],
                    )
                ],
                protected_paths=["tests"],
                clean_room=False,
                policy_version="delivery-demo-v1",
            ),
        )
        harness = Harness(work)
        goal = harness.create_goal(spec)
        harness.run(goal.id, options=RunOptions(agent_name="mock"))
        verification = harness.verify_goal(goal.id)
        manifest, destination = build_delivery(
            harness.store, work, goal.id, output_dir=OUTPUT, mark_delivered=False
        )
        logs_dir = destination / "logs"
        logs_dir.mkdir(exist_ok=True)
        archive = {}
        for evidence in harness.store.list_evidence(goal.id):
            source = Path(evidence.log_dir).parent
            if source.is_dir():
                target = logs_dir / evidence.runner_id
                shutil.copytree(source, target, dirs_exist_ok=True)
                archive[evidence.id] = {
                    "source_log_dir": str(source),
                    "archived_log_dir": str(target.relative_to(ROOT)),
                    "files": {
                        file.name: sha256(file)
                        for file in sorted(target.iterdir())
                        if file.is_file()
                    },
                }
        (destination / "log_archive.json").write_text(
            json.dumps(archive, indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest_sha = sha256(destination / "manifest.json")
        result = {
            "status": "PASS",
            "goal_id": goal.id,
            "verification": verification["status"],
            "delivery_state": manifest.state,
            "manifest": str((destination / "manifest.json").relative_to(ROOT)),
            "manifest_sha256": manifest_sha,
            "manifest_sha256_file": str((destination / "manifest.sha256").relative_to(ROOT)),
            "log_archive": str((destination / "log_archive.json").relative_to(ROOT)),
            "test_exit_codes": [
                {
                    "id": command["id"],
                    "exit_codes": [item["exit_code"] for item in command["observations"]],
                }
                for command in manifest.test_commands
            ],
            "unverified": manifest.unverified,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["delivery_state"] != "READY" or verification["status"] != "PASS":
            raise SystemExit(1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
