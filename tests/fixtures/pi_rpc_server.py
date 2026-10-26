"""Test-only Pi RPC JSONL peer implementing the documented subset."""

from __future__ import annotations

import json
import os
import sys
import time


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(request_id: str | None, command: str, data: dict | None = None) -> None:
    payload = {"id": request_id, "type": "response", "command": command, "success": True}
    if data is not None:
        payload["data"] = data
    send(payload)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        request_id = message.get("id")
        command = message.get("type")
        if command == "get_state":
            response(
                request_id,
                "get_state",
                {
                    "sessionId": "session-1",
                    "isStreaming": False,
                    "isCompacting": False,
                    "thinkingLevel": "off",
                    "steeringMode": "all",
                    "followUpMode": "all",
                    "autoCompactionEnabled": False,
                    "messageCount": 0,
                    "pendingMessageCount": 0,
                },
            )
        elif command == "prompt":
            text = str(message.get("message", ""))
            response(request_id, "prompt", {"disposition": "started"})
            if text == "crash":
                os._exit(3)
            send({"type": "agent_start"})
            if text == "slow":
                continue
            if text == "fail":
                send(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "failed self-report"}],
                        },
                        "usage": {"totalTokens": 12, "costUsd": 0.001},
                    }
                )
                send({"type": "agent_settled", "aborted": False})
                continue
            time.sleep(0.15)
            send(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "implemented the task"}],
                    },
                    "usage": {"totalTokens": 42, "costUsd": 0.002},
                }
            )
            send({"type": "agent_settled", "aborted": False})
        elif command == "abort":
            response(request_id, "abort")
            send({"type": "agent_settled", "aborted": True})
        else:
            send(
                {
                    "id": request_id,
                    "type": "response",
                    "command": command,
                    "success": False,
                    "error": f"unknown command {command}",
                }
            )


if __name__ == "__main__":
    main()
