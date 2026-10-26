from forgeflow.agents.acp import ACP_V1_VERSION, AcpV1Client
from forgeflow.agents.base import AgentAdapter, AgentRunRecord
from forgeflow.agents.claude_cli import ClaudeCliAdapter
from forgeflow.agents.mcp import MCP_PROTOCOL_VERSION, MCPClient
from forgeflow.agents.mock import MockAdapter
from forgeflow.agents.pi_rpc import PiRpcAdapter

__all__ = [
    "ACP_V1_VERSION",
    "MCP_PROTOCOL_VERSION",
    "AcpV1Client",
    "AgentAdapter",
    "AgentRunRecord",
    "ClaudeCliAdapter",
    "MCPClient",
    "MockAdapter",
    "PiRpcAdapter",
    "create_agent_adapter",
]


def create_agent_adapter(name: str, **kwargs: object) -> AgentAdapter:
    normalized = name.lower().replace("-", "_")
    if normalized in {"mock", "deterministic"}:
        return MockAdapter()
    if normalized in {"pi", "pi_rpc"}:
        return PiRpcAdapter(**kwargs)  # type: ignore[arg-type]
    if normalized in {"claude", "claude_cli", "claude_code"}:
        return ClaudeCliAdapter(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown agent adapter: {name}")
