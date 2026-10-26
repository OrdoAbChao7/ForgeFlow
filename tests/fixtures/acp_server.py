"""Test-only ACP v1 stdio agent fixture."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

version = 2 if os.environ.get("ACP_TEST_VERSION") == "2" else 1


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id: Any, payload: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})
    if method == "initialize":
        result(
            request_id,
            {
                "protocolVersion": version,
                "agentCapabilities": {"loadSession": False, "promptCapabilities": {"image": False}},
                "agentInfo": {"name": "forgeflow-test-acp", "version": "1.0"},
                "authMethods": [],
            },
        )
    elif method == "session/new":
        result(request_id, {"sessionId": "sess-1"})
    elif method == "session/prompt":
        session_id = params.get("sessionId")
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "working"},
                    },
                },
            }
        )
        result(request_id, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        return
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        )


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
