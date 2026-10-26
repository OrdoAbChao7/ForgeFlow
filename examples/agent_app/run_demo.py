"""Demo of the generic agent-app adapter facade."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from forgeflow.adapters.agent_app import AgentAppAdapter
from forgeflow.agents.mock import MockAdapter
from forgeflow.domain.models import TaskSpec


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="forgeflow-agent-app-"))
    task = TaskSpec(
        key="app-demo",
        title="Agent app demo",
        metadata={"mock": {"files": {"result.txt": "hello from agent\n"}}},
    )
    run = AgentAppAdapter(MockAdapter(), workspace=root).execute(task, "write a result")
    print(json.dumps(run.as_dict(), indent=2))
    print("This adapter exposes trajectory/risk, not verification PASS.")


if __name__ == "__main__":
    main()
