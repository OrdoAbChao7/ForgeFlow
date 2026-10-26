"""Show explainable structure-aware context selection under a token budget."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from forgeflow.context.engine import ContextEngine
from forgeflow.domain.enums import GateKind
from forgeflow.domain.models import AcceptanceCriterion, GoalSpec, TaskSpec, VerifierConfig
from forgeflow.verification.fingerprint import git_commit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--budget", type=int, default=600)
    args = parser.parse_args()
    root = args.workspace.resolve()
    spec = GoalSpec(
        title="Context demo",
        objective="Inspect how ForgeFlow retrieves context for the runtime module.",
        acceptance=[
            AcceptanceCriterion(id="review", statement="reviewed", gate=GateKind.MANUAL)
        ],
        tasks=[
            TaskSpec(
                key="context",
                title="Understand execution runtime",
                description="Review run, verification and evidence binding.",
                owned_paths=["src/forgeflow/runtime.py"],
                gates=[GateKind.MANUAL],
                acceptance_ids=["review"],
            )
        ],
        verifier=VerifierConfig(),
    )
    engine = ContextEngine(root)
    try:
        output = {
            "git_sha": git_commit(root),
            "budget": args.budget,
            "modes": {
                mode: engine.build(
                    spec, spec.tasks[0], token_budget=args.budget, mode=mode
                ).as_dict()
                for mode in ("structure", "full_file", "keyword", "diff_only", "none")
            },
        }
    finally:
        engine.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
