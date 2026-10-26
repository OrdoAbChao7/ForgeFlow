"""Test-only MCP 2026-07-28 stdio server with stateless metadata and tasks extension."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

VERSION = "2026-07-28"
LEGACY = os.environ.get("MCP_TEST_MODE") == "legacy"
BAD_VERSION = os.environ.get("MCP_TEST_MODE") == "bad-version"
_task_polls = 0


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def error(request_id: Any, code: int, message: str, data: dict | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    send(payload)


def complete(request_id: Any, result: dict[str, Any]) -> None:
    result.setdefault("resultType", "complete")
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def meta_ok(params: dict[str, Any]) -> bool:
    meta = params.get("_meta", {}) if isinstance(params, dict) else {}
    return (
        meta.get("io.modelcontextprotocol/protocolVersion") == VERSION
        and isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict)
    )


def handle(message: dict[str, Any]) -> None:
    global _task_polls
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})
    if method == "server/discover":
        if LEGACY:
            error(request_id, -32601, "method not found")
            return
        if BAD_VERSION:
            error(
                request_id,
                -32022,
                "unsupported protocol version",
                {"supported": ["2025-01-01"], "requested": VERSION},
            )
            return
        if not meta_ok(params):
            error(request_id, -32602, "malformed _meta")
            return
        complete(
            request_id,
            {
                "supportedVersions": [VERSION],
                "capabilities": {
                    "tools": {"listChanged": False},
                    "extensions": {"io.modelcontextprotocol/tasks": {"polling": True}},
                },
                "serverInfo": {"name": "forgeflow-test-mcp", "version": "1.0"},
                "ttlMs": 1000,
                "cacheScope": "private",
            },
        )
        return
    if LEGACY and method == "initialize":
        complete(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "forgeflow-legacy-test", "version": "1.0"},
            },
        )
        return
    if LEGACY and method == "notifications/initialized":
        return
    if not LEGACY and not meta_ok(params):
        error(request_id, -32602, "missing or malformed per-request _meta")
        return
    if method == "tools/list":
        complete(
            request_id,
            {
                "tools": [
                    {"name": "echo", "description": "echo text", "inputSchema": {"type": "object"}},
                    {"name": "slow", "description": "task-backed tool", "inputSchema": {"type": "object"}},
                    {"name": "input", "description": "MRTR tool", "inputSchema": {"type": "object"}},
                ]
            },
        )
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "echo":
            complete(
                request_id,
                {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]},
            )
        elif name == "slow":
            complete(
                request_id,
                {"resultType": "task", "taskId": "task-1", "ttlMs": 5000, "pollIntervalMs": 40},
            )
        elif name == "input":
            complete(request_id, {"resultType": "input_required", "inputRequests": [{"id": "x"}]})
        else:
            error(request_id, -32602, "unknown tool")
    elif method == "tasks/get":
        _task_polls += 1
        status = "completed" if _task_polls >= 2 else "working"
        complete(
            request_id,
            {"taskId": params.get("taskId"), "status": status, "pollIntervalMs": 40},
        )
    elif method == "tasks/cancel":
        complete(request_id, {"taskId": params.get("taskId"), "status": "cancelled"})
    elif method == "tasks/update":
        complete(request_id, {"taskId": params.get("taskId"), "status": "working"})
    else:
        error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            handle(message)
            continue
        handle(message)


if __name__ == "__main__":
    main()
