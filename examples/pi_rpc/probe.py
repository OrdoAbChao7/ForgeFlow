"""Verify the local Pi RPC handshake without invoking a paid model completion."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from forgeflow.agents.pi_rpc import PiRpcAdapter
from forgeflow.domain.models import TaskSpec


def main() -> None:
    probe = PiRpcAdapter.probe()
    print(json.dumps({"probe": probe}, indent=2))
    if not probe["available"]:
        print("Pi is not installed; RPC contract is still covered by fixtures.")
        return
    workspace = Path(tempfile.mkdtemp(prefix="forgeflow-pi-rpc-"))
    adapter = PiRpcAdapter(startup_timeout=20)
    try:
        record = adapter.start("pi-handshake-demo", workspace, TaskSpec(key="demo", title="demo"))
        print(
            json.dumps(
                {
                    "handshake_ok": record.trace.get("handshake_ok"),
                    "session_state": record.trace.get("session_state"),
                    "model_completion": "NOT_RUN",
                    "reason": "no paid prompt was sent; use forge run --agent pi_rpc --allow-paid",
                },
                indent=2,
            )
        )
    finally:
        adapter.close()
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
