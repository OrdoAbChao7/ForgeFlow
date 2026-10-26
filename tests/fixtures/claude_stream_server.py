"""Test-only Claude Code stream-json peer."""

from __future__ import annotations

import json
import os
import sys


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = os.environ.get("CLAUDE_TEST_MODE", "success")
    if mode == "crash":
        sys.exit(2)
    emit({"type": "system", "subtype": "init", "session_id": "test"})
    emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "implemented"}],
            },
        }
    )
    emit(
        {
            "type": "result",
            "subtype": "success" if mode == "success" else "error",
            "is_error": mode != "success",
            "result": "done" if mode == "success" else "model failure",
            "total_cost_usd": 0.0042,
            "usage": {"total_tokens": 99},
        }
    )


if __name__ == "__main__":
    main()
