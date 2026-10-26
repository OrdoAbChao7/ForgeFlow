from __future__ import annotations

import shutil
import time
from pathlib import Path

from forgeflow.agents.base import AgentAdapter, AgentRunRecord
from forgeflow.domain.enums import AgentRunStatus
from forgeflow.domain.models import AgentSettlement, utcnow


class MockAdapter(AgentAdapter):
    """Deterministic, credential-free adapter for E2E and failure-injection tests.

    This adapter is explicitly a mock. It never manufactures verifier evidence and its
    self-reported success is still subject to independent test execution.
    """

    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self._prompts: dict[str, str] = {}

    def submit(self, run_id: str, prompt: str) -> str:
        record = self._get(run_id)
        self._prompts[run_id] = prompt
        record.add_event("prompt_accepted", {"mock": True, "prompt_sha256": _digest(prompt)})
        record.accepted_at = record.started_at
        self._execute(record)
        return "started"

    def _execute(self, record: AgentRunRecord) -> None:
        metadata = record.task.metadata.get("mock", {})
        if not isinstance(metadata, dict):
            metadata = {}
        record.status = AgentRunStatus.RUNNING
        record.add_event("agent_start", {"mock": True})
        delay = float(metadata.get("delay_seconds", 0))
        if delay:
            time.sleep(min(delay, 30))
        failure = metadata.get("failure")
        if failure:
            record.status = AgentRunStatus.FAILED
            record.error = str(failure)
            record.finished_at = utcnow()
            record.add_event("agent_error", {"error": str(failure)})
            record.settlement = AgentSettlement(
                run_id=record.run_id,
                status=AgentRunStatus.FAILED,
                success_claimed=False,
                error=str(failure),
            )
            self._settle(record)
            return
        try:
            self._apply_actions(record, metadata)
        except Exception as exc:
            record.status = AgentRunStatus.FAILED
            record.error = f"mock action failed: {exc}"
            record.finished_at = utcnow()
            record.add_event("agent_error", {"error": record.error})
            record.settlement = AgentSettlement(
                run_id=record.run_id,
                status=AgentRunStatus.FAILED,
                success_claimed=False,
                error=record.error,
            )
            self._settle(record)
            return
        claimed = bool(metadata.get("claim_success", True))
        record.status = AgentRunStatus.SETTLED
        record.finished_at = utcnow()
        record.add_event(
            "agent_settled",
            {"aborted": False, "mock": True, "success_claimed": claimed},
        )
        record.settlement = AgentSettlement(
            run_id=record.run_id,
            status=AgentRunStatus.SETTLED,
            success_claimed=claimed,
            output=str(metadata.get("output", "mock agent completed")),
            cost_usd=0.0,
            tokens=0,
        )
        self._settle(record)

    def _settle(self, record: AgentRunRecord) -> None:
        record.trace["prompt_retained"] = False

    def _apply_actions(self, record: AgentRunRecord, metadata: dict) -> None:
        files = metadata.get("files", {})
        if not isinstance(files, dict):
            raise ValueError("mock.files must be a mapping")
        for relative, content in files.items():
            target = (record.workspace / str(relative)).resolve()
            try:
                target.relative_to(record.workspace)
            except ValueError as exc:
                raise ValueError(f"mock file path escapes workspace: {relative}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            record.add_event("file_written", {"path": str(relative), "mock": True})
        deletions = metadata.get("delete", [])
        if not isinstance(deletions, list):
            raise ValueError("mock.delete must be a list")
        for relative in deletions:
            target = (record.workspace / str(relative)).resolve()
            try:
                target.relative_to(record.workspace)
            except ValueError as exc:
                raise ValueError(f"mock delete escapes workspace: {relative}") from exc
            if target.is_file():
                target.unlink()
                record.add_event("file_deleted", {"path": str(relative), "mock": True})
        copies = metadata.get("copy", {})
        if isinstance(copies, dict):
            for source, relative in copies.items():
                source_path = Path(str(source)).resolve()
                target = (record.workspace / str(relative)).resolve()
                try:
                    target.relative_to(record.workspace)
                except ValueError as exc:
                    raise ValueError(f"mock copy target escapes workspace: {relative}") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                record.add_event("file_copied", {"path": str(relative), "mock": True})

    def cancel(self, run_id: str) -> None:
        record = self._get(run_id)
        if record.status in {AgentRunStatus.SETTLED, AgentRunStatus.FAILED}:
            return
        record.status = AgentRunStatus.CANCELLED
        record.error = "cancelled by harness"
        record.add_event("agent_settled", {"aborted": True})
        record.settlement = AgentSettlement(
            run_id=run_id,
            status=AgentRunStatus.CANCELLED,
            success_claimed=False,
            error=record.error,
        )

    def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0) -> AgentSettlement:
        record = self._get(run_id)
        if record.settlement:
            return record.settlement
        # Mock submission is synchronous.
        if record.status == AgentRunStatus.ACCEPTED:
            self.submit(run_id, self._prompts.get(run_id, ""))
        if record.settlement:
            return record.settlement
        return AgentSettlement(
            run_id=run_id, status=AgentRunStatus.FAILED, error="mock did not settle"
        )


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
