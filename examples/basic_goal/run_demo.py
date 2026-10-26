"""Run ForgeFlow end-to-end on a disposable project with a real pytest verifier."""

from __future__ import annotations

import argparse
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


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean-room", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="forgeflow-demo-"))
    try:
        (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (root / "conftest.py").write_text("", encoding="utf-8")
        (root / ".gitignore").write_text(".forge/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        git(root, "init", "-b", "main")
        git(root, "config", "user.email", "demo@forgeflow.invalid")
        git(root, "config", "user.name", "ForgeFlow Demo")
        git(root, "add", "-A")
        git(root, "commit", "-m", "demo baseline")
        spec = GoalSpec(
            title="Demo: fix addition",
            objective="Make add return the arithmetic sum, proven by real pytest output.",
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
                clean_room=True,
            ),
        )
        harness = Harness(root)
        goal = harness.create_goal(spec)
        report = harness.run(goal.id, options=RunOptions(agent_name="mock"))
        verification = harness.verify_goal(goal.id, clean_room=args.clean_room)
        manifest, directory = build_delivery(
            harness.store, root, goal.id, mark_delivered=False
        )
        evidence = harness.store.list_evidence(goal.id)
        payload = {
            "mock_agent": True,
            "goal_id": goal.id,
            "goal_status": report.goal_status.value,
            "verification": verification["status"],
            "gates": verification["gates"],
            "clean_room": verification.get("cleanroom"),
            "delivery_state": manifest.state,
            "manifest": str(directory / "manifest.json"),
            "evidence": [
                {
                    "argv": item.argv,
                    "exit_code": item.exit_code,
                    "trust": item.trust.value,
                    "source_fingerprint": item.source_fingerprint,
                }
                for item in evidence
            ],
            "artifact_sha256": manifest.model_dump(mode="json")["source_fingerprint"],
        }
        print(json.dumps(payload, indent=2))
        if args.keep:
            print(f"kept workspace: {root}")
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
