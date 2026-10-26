from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forgeflow.agents.acp import AcpProtocolError, AcpV1Client
from forgeflow.agents.mcp import (
    MCP_PROTOCOL_VERSION,
    MCPClient,
    McpInputRequiredError,
)
from forgeflow.domain.errors import ExternalBlockedError, PolicyDeniedError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _mcp(mode: str = "modern", **kwargs) -> MCPClient:

    environment = kwargs.pop("env", {})
    return MCPClient(
        [sys.executable, str(FIXTURES / "mcp_server.py")],
        env={**environment, "MCP_TEST_MODE": mode},
        request_timeout=5,
        **kwargs,
    )


def test_mcp_modern_stateless_discovery_and_tools() -> None:
    with _mcp(allow_tools={"echo", "slow", "input"}) as client:
        assert client.protocol_version == MCP_PROTOCOL_VERSION
        assert client.compatible
        assert not client.legacy
        assert client.supports_tasks
        tools = client.list_tools()
        assert {tool["name"] for tool in tools} == {"echo", "slow", "input"}
        result = client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
        assert result["resultType"] == "complete"


def test_mcp_tool_calls_require_policy() -> None:
    with _mcp() as client:
        with pytest.raises(PolicyDeniedError):
            client.call_tool("echo", {"text": "x"})
    with _mcp(allow_tools={"echo"}) as client:
        with pytest.raises(PolicyDeniedError):
            client.call_tool("slow", {})


def test_mcp_tasks_extension_polling() -> None:
    with _mcp(allow_tools={"slow"}) as client:
        created = client.call_tool("slow", {})
        assert created["resultType"] == "task"
        task = client.wait_for_task(created["taskId"], timeout_seconds=5)
        assert task["status"] == "completed"
        assert client.describe()["supports_tasks"] is True


def test_mcp_input_required_is_surfaced() -> None:
    with _mcp(allow_tools={"input"}) as client:
        with pytest.raises(McpInputRequiredError):
            client.call_tool("input", {})


def test_mcp_bad_version_does_not_blindly_fallback() -> None:
    with pytest.raises(ExternalBlockedError):
        _mcp("bad-version", allow_tools=set()).start()


def test_mcp_legacy_fallback_is_explicit() -> None:
    client = _mcp("legacy", allow_tools={"echo"})
    client.start()
    try:
        assert client.legacy
        assert not client.compatible
        result = client.call_tool("echo", {"text": "legacy"})
        assert result["content"][0]["text"] == "legacy"
        assert client.discovery_metadata["legacy_fallback"] is True
    finally:
        client.close()


def test_acp_v1_session_contract() -> None:
    client = AcpV1Client([sys.executable, str(FIXTURES / "acp_server.py")])
    try:
        response = client.start()
        assert response["protocolVersion"] == 1
        session_id = client.new_session()
        assert session_id == "sess-1"
        response = client.prompt(session_id, "do work")
        assert response["stopReason"] == "end_turn"
        assert client.updates[0]["update"]["sessionUpdate"] == "agent_message_chunk"
        client.cancel(session_id)
    finally:
        client.close()


def test_acp_version_mismatch_is_rejected() -> None:
    import os

    client = AcpV1Client(
        [sys.executable, str(FIXTURES / "acp_server.py")],
    )
    old = os.environ.get("ACP_TEST_VERSION")
    os.environ["ACP_TEST_VERSION"] = "2"
    try:
        with pytest.raises(AcpProtocolError):
            client.start()
    finally:
        client.close()
        if old is None:
            os.environ.pop("ACP_TEST_VERSION", None)
        else:
            os.environ["ACP_TEST_VERSION"] = old
