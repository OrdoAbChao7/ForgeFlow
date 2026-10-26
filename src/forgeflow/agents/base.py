from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from forgeflow.domain.enums import AgentRunStatus
from forgeflow.domain.models import AgentEvent, AgentSettlement, TaskSpec, utcnow


@dataclass
class AgentRunRecord:
    run_id: str
    workspace: Path
    task: TaskSpec
    status: AgentRunStatus = AgentRunStatus.ACCEPTED
    events: list[AgentEvent] = field(default_factory=list)
    settlement: AgentSettlement | None = None
    accepted_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    error: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utcnow)

    def add_event(self, event_type: str, payload: dict[str, Any] | None = None) -> AgentEvent:
        event = AgentEvent(
            run_id=self.run_id,
            type=event_type,
            payload=payload or {},
            sequence=len(self.events) + 1,
        )
        self.events.append(event)
        return event


class AgentAdapter(ABC):
    """Standard agent boundary.

    ``submit`` only confirms command acceptance. Task success is established later by
    the harness's independent verifier, never by the adapter's self-report.
    """

    name: str

    def __init__(self) -> None:
        self._runs: dict[str, AgentRunRecord] = {}

    @property
    def runs(self) -> dict[str, AgentRunRecord]:
        return self._runs

    def start(self, run_id: str, workspace: str | Path, task: TaskSpec) -> AgentRunRecord:
        if run_id in self._runs:
            raise ValueError(f"run already exists: {run_id}")
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace_path}")
        record = AgentRunRecord(run_id=run_id, workspace=workspace_path, task=task)
        self._runs[run_id] = record
        record.add_event("run_started", {"agent": self.name, "task": task.key})
        self._start(record)
        return record

    def _start(self, record: AgentRunRecord) -> None:
        return None

    @abstractmethod
    def submit(self, run_id: str, prompt: str) -> str:
        """Submit prompt; return an acceptance disposition, never completion."""

    @abstractmethod
    def cancel(self, run_id: str) -> None: ...

    def events(self, run_id: str, *, clear: bool = False) -> list[AgentEvent]:
        events = list(self._get(run_id).events)
        if clear:
            self._get(run_id).events.clear()
        return events

    @abstractmethod
    def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0) -> AgentSettlement: ...

    def status(self, run_id: str) -> AgentRunStatus:
        return self._get(run_id).status

    def cost(self, run_id: str) -> float:
        return self._get(run_id).cost_usd

    def trace(self, run_id: str) -> dict[str, Any]:
        record = self._get(run_id)
        return {
            **record.trace,
            "run_id": record.run_id,
            "agent": self.name,
            "status": record.status.value,
            "cost_usd": record.cost_usd,
            "tokens": record.tokens,
            "event_count": len(record.events),
        }

    def close(self) -> None:
        return None

    def _get(self, run_id: str) -> AgentRunRecord:
        if run_id not in self._runs:
            raise KeyError(f"unknown agent run: {run_id}")
        return self._runs[run_id]
