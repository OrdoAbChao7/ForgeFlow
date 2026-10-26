from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from forgeflow.agents.claude_cli import ClaudeCliAdapter
from forgeflow.agents.mock import MockAdapter
from forgeflow.agents.pi_rpc import PiRpcAdapter
from forgeflow.domain.enums import AgentRunStatus
from forgeflow.domain.models import TaskSpec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _task(**metadata) -> TaskSpec:
    return TaskSpec(key="task", title="task", metadata=metadata)


def test_mock_adapter_is_deterministic_and_rejects_path_escape(tmp_path: Path) -> None:
    adapter = MockAdapter()
    task = _task(mock={"files": {"out.txt": "hello"}, "claim_success": True})
    adapter.start("run-1", tmp_path, task)
    assert adapter.submit("run-1", "prompt") == "started"
    settlement = adapter.wait_until_settled("run-1")
    assert settlement.success_claimed
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"
    assert adapter.cost("run-1") == 0.0
    traversal = _task(mock={"files": {"../escape.txt": "bad"}})
    adapter.start("run-2", tmp_path, traversal)
    adapter.submit("run-2", "prompt")
    settlement = adapter.wait_until_settled("run-2")
    assert not settlement.success_claimed
    assert "escapes workspace" in (settlement.error or "")


def test_pi_rpc_contract_distinguishes_acceptance_and_settlement(tmp_path: Path) -> None:
    adapter = PiRpcAdapter(
        executable=sys.executable,
        base_args=[str(FIXTURES / "pi_rpc_server.py")],
        startup_timeout=5,
    )
    try:
        adapter.start("run-pi", tmp_path, _task())
        disposition = adapter.submit("run-pi", "do work")
        assert disposition == "started"
        settlement = adapter.wait_until_settled("run-pi", timeout_seconds=10)
        assert settlement.status == AgentRunStatus.SETTLED
        assert settlement.success_claimed
        assert "implemented" in settlement.output
        assert settlement.tokens == 42
        assert settlement.cost_usd == 0.002
        assert adapter.trace("run-pi")["handshake_ok"] is True
    finally:
        adapter.close()


def test_pi_rpc_crash_is_not_a_success(tmp_path: Path) -> None:
    adapter = PiRpcAdapter(
        executable=sys.executable,
        base_args=[str(FIXTURES / "pi_rpc_server.py")],
    )
    try:
        adapter.start("run-crash", tmp_path, _task())
        adapter.submit("run-crash", "crash")
        settlement = adapter.wait_until_settled("run-crash", timeout_seconds=10)
        assert settlement.status == AgentRunStatus.CRASHED
        assert not settlement.success_claimed
    finally:
        adapter.close()


def test_pi_rpc_cancel_maps_to_aborted_settlement(tmp_path: Path) -> None:
    adapter = PiRpcAdapter(
        executable=sys.executable,
        base_args=[str(FIXTURES / "pi_rpc_server.py")],
    )
    try:
        adapter.start("run-cancel", tmp_path, _task())
        adapter.submit("run-cancel", "slow")
        adapter.cancel("run-cancel")
        settlement = adapter.wait_until_settled("run-cancel", timeout_seconds=10)
        assert settlement.status == AgentRunStatus.CANCELLED
        assert not settlement.success_claimed
    finally:
        adapter.close()


def test_pi_rpc_real_handshake_without_model_call(tmp_path: Path) -> None:
    executable = shutil.which("pi")
    if not executable:
        pytest.skip("pi is not installed")
    adapter = PiRpcAdapter(executable=executable, startup_timeout=20)
    try:
        record = adapter.start("real-handshake", tmp_path, _task())
        assert record.trace.get("handshake_ok") is True
        assert record.trace.get("session_state")
    finally:
        adapter.close()


def _claude(mode: str = "success") -> ClaudeCliAdapter:
    os.environ["CLAUDE_TEST_MODE"] = mode
    return ClaudeCliAdapter(
        executable=sys.executable,
        base_args=[str(FIXTURES / "claude_stream_server.py")],
        max_budget_usd=0.01,
    )


def test_claude_cli_contract_parses_stream_and_cost(tmp_path: Path) -> None:
    adapter = _claude()
    try:
        adapter.start("run-claude", tmp_path, _task())
        assert adapter.submit("run-claude", "work") == "started"
        settlement = adapter.wait_until_settled("run-claude", timeout_seconds=10)
        assert settlement.status == AgentRunStatus.SETTLED
        assert settlement.success_claimed
        assert settlement.cost_usd == pytest.approx(0.0042)
        assert settlement.tokens == 99
        assert "implemented" in settlement.output
    finally:
        adapter.close()
        os.environ.pop("CLAUDE_TEST_MODE", None)


def test_claude_cli_error_result_is_failure(tmp_path: Path) -> None:
    adapter = _claude("error")
    try:
        adapter.start("run-error", tmp_path, _task())
        adapter.submit("run-error", "work")
        settlement = adapter.wait_until_settled("run-error", timeout_seconds=10)
        assert settlement.status == AgentRunStatus.FAILED
        assert not settlement.success_claimed
    finally:
        adapter.close()
        os.environ.pop("CLAUDE_TEST_MODE", None)


def test_claude_cli_probe_is_credential_free() -> None:
    executable = shutil.which("claude")
    if not executable:
        pytest.skip("claude is not installed")
    probe = ClaudeCliAdapter.probe(executable)
    assert probe["available"] is True
    assert probe["version"]


def test_agent_app_adapter_trajectory_and_risk_gate(tmp_path: Path) -> None:
    from forgeflow.adapters.agent_app import AgentAppAdapter

    task = _task(mock={"files": {"ok.py": "VALUE = 1\n"}})
    service = AgentAppAdapter(MockAdapter(), workspace=tmp_path)
    run = service.execute(task, "prompt")
    assert run.accepted
    assert run.settlement_status == "SETTLED"
    assert run.trajectory
    assert run.tool_results
    assert run.retrieval["mode"] == "none"
    assert run.risk_gate["blocked"] is False
