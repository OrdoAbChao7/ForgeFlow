from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.agents.base import AgentAdapter
from forgeflow.context.engine import ContextBundle
from forgeflow.domain.models import TaskSpec, new_id, utcnow
from forgeflow.verification.reviewer import StaticReviewer


@dataclass
class AgentAppRun:
    run_id: str
    task_key: str
    agent: str
    status: str
    settlement_status: str
    accepted: bool
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    risk_gate: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    output: str = ""
    error: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__ | {"created_at": self.created_at}


class AgentAppAdapter:
    """Generic agent-application adapter.

    It turns an agent run into an auditable app trajectory: retrieval explanation,
    tool/results, risk gate, and settlement. It does not verify tasks; ForgeFlow gates
    remain the only source of PASS evidence.
    """

    name = "agent_app"

    def __init__(self, adapter: AgentAdapter, *, workspace: str | Path) -> None:
        self.adapter = adapter
        self.workspace = Path(workspace).resolve()

    def execute(
        self,
        task: TaskSpec,
        prompt: str,
        *,
        context: ContextBundle | None = None,
        timeout_seconds: float = 300.0,
    ) -> AgentAppRun:
        run_id = new_id("app")
        trajectory: list[dict[str, Any]] = []
        self.adapter.start(run_id, self.workspace, task)
        trajectory.append({"type": "started", "agent": self.adapter.name, "task": task.key})
        disposition = self.adapter.submit(run_id, prompt)
        trajectory.append({"type": "accepted", "disposition": disposition})
        settlement = self.adapter.wait_until_settled(run_id, timeout_seconds=timeout_seconds)
        for event in self.adapter.events(run_id):
            trajectory.append(
                {
                    "type": event.type,
                    "sequence": event.sequence,
                    "payload_keys": sorted(event.payload),
                }
            )
        review = StaticReviewer().review(self.workspace)
        tool_results = [
            {"type": event.type, "payload": event.payload}
            for event in self.adapter.events(run_id)
            if event.type in {"tool_call", "tool_result", "file_written", "mock_patch"}
        ]
        return AgentAppRun(
            run_id=run_id,
            task_key=task.key,
            agent=self.adapter.name,
            status="SETTLED" if settlement.success_claimed else "FAILED",
            settlement_status=settlement.status.value,
            accepted=bool(disposition),
            trajectory=trajectory,
            retrieval=context.as_dict() if context else {"mode": "none"},
            tool_results=tool_results,
            risk_gate={"blocked": review.blocked, **review.as_dict()},
            cost_usd=settlement.cost_usd,
            tokens=settlement.tokens,
            output=settlement.output,
            error=settlement.error,
        )
