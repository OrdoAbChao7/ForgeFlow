from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from forgeflow.domain.errors import ExternalBlockedError, ForgeFlowError, PolicyDeniedError

ACP_V1_VERSION = 1


class AcpProtocolError(ForgeFlowError):
    code = "ACP_PROTOCOL_ERROR"


class AcpV1Client:
    """Optional ACP v1 IDE/agent connection boundary.

    ACP connects an IDE/client to a coding agent. It is not an agent-to-agent scheduler
    and ForgeFlow never uses it to grant verification authority. This client implements
    the NDJSON JSON-RPC subset needed for a contract test and boundary demo; external
    ACP binaries are not bundled.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        request_timeout: float = 60.0,
    ) -> None:
        self.command = command
        self.cwd = str(Path(cwd).resolve()) if cwd else None
        self.request_timeout = request_timeout
        self.process: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self._updates: list[dict[str, Any]] = []
        self._stderr: list[str] = []
        self.agent_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: int | None = None

    def start(self) -> dict[str, Any]:
        executable = shutil.which(self.command[0])
        if executable is None and not Path(self.command[0]).exists():
            raise ExternalBlockedError(f"ACP executable not found: {self.command[0]}")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            [executable or self.command[0], *self.command[1:]],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        response = self._request(
            "initialize",
            {
                "protocolVersion": ACP_V1_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                "clientInfo": {"name": "forgeflow", "version": "2.0.0"},
            },
        )
        self.protocol_version = response.get("protocolVersion")
        self.agent_info = response.get("agentInfo", {})
        self.capabilities = response.get("agentCapabilities", {})
        if self.protocol_version != ACP_V1_VERSION:
            raise AcpProtocolError(
                f"unsupported ACP protocol version: {self.protocol_version!r}"
            )
        return response

    def new_session(self, *, mcp_servers: list[dict[str, Any]] | None = None) -> str:
        response = self._request(
            "session/new",
            {"cwd": self.cwd or str(Path.cwd()), "mcpServers": mcp_servers or []},
        )
        session_id = response.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError("session/new did not return a sessionId")
        return session_id

    def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        return self._request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )

    def cancel(self, session_id: str) -> None:
        self._notify("session/cancel", {"sessionId": session_id})

    @property
    def updates(self) -> list[dict[str, Any]]:
        return list(self._updates)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise AcpProtocolError("ACP process is not running")
        self._next_id += 1
        request_id = str(self._next_id)
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[request_id] = waiter
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcpProtocolError(f"failed to write ACP request: {exc}") from exc
        try:
            response = waiter.get(timeout=self.request_timeout)
        except queue.Empty as exc:
            raise AcpProtocolError(f"ACP request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise AcpProtocolError(f"ACP error for {method}: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise AcpProtocolError(f"ACP response for {method} has no object result")
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            return
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )
        self.process.stdin.flush()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                waiter = self._pending.get(str(message["id"]))
                if waiter:
                    waiter.put(message)
            elif message.get("method") == "session/update":
                params = message.get("params", {})
                self._updates.append(params)
                # ACP may issue permission requests; this demo client denies by default.
                if any(
                    isinstance(item, dict) and item.get("method") == "session/request_permission"
                    for item in [params]
                ):
                    raise PolicyDeniedError("ACP permission request denied by default policy")
            elif "id" in message and "method" in message:
                if message["method"] == "session/request_permission":
                    # Deny-by-default, explicit ACP cancelled outcome.
                    if self.process.stdin:
                        self.process.stdin.write(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "result": {"outcome": {"outcome": "cancelled"}},
                                }
                            )
                            + "\n"
                        )
                        self.process.stdin.flush()
                elif self.process.stdin:
                    # Handle client-bound request with an explicit JSON-RPC error.
                    self.process.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "ForgeFlow ACP client does not support this request",
                                },
                            }
                        )
                        + "\n"
                    )
                    self.process.stdin.flush()
        for waiter in self._pending.values():
            waiter.put({"error": {"message": "ACP process exited"}})

    def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            if len("".join(self._stderr)) < 64 * 1024:
                self._stderr.append(line)

    @property
    def stderr_excerpt(self) -> str:
        return "".join(self._stderr)[-16000:]

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    self.process.kill()
                except OSError:
                    pass

    def __enter__(self) -> AcpV1Client:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
