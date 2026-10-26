"""Demonstrate lease expiry, interruption detection and explicit resume."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from forgeflow.domain.enums import GateKind
from forgeflow.domain.models import AcceptanceCriterion, GoalSpec, TaskSpec, VerifierConfig
from forgeflow.runtime import Harness


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="forgeflow-resume-"))
    (root / "README.md").write_text("resume demo\n", encoding="utf-8")
    spec = GoalSpec(
        title="Resume demo",
        objective="Exercise expired leases",
        acceptance=[
            AcceptanceCriterion(id="manual", statement="reviewed", gate=GateKind.MANUAL)
        ],
        tasks=[TaskSpec(key="work", title="work")],
        verifier=VerifierConfig(),
    )
    harness = Harness(root)
    goal = harness.create_goal(spec)
    claim = harness.store.claim_task(goal.id, owner="crashed-worker", lease_seconds=0.2)
    print(f"claimed task={claim.task.key} attempt={claim.attempt.id}; simulating crash...")
    time.sleep(0.4)
    resumed = harness.resume(goal.id)
    print(json.dumps(resumed, indent=2))
    print("manual attestation is intentionally not auto-satisfied")
    print("Real process-kill coverage: tests/integration/test_recovery_resume.py")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
