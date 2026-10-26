from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgeflow.domain.errors import ExternalBlockedError, ForgeFlowError, PolicyDeniedError

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_VERSION = "2025-11-25"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


class McpProtocolError(ForgeFlowError):
    code = "MCP_PROTOCOL_ERROR"


class McpInputRequiredError(McpProtocolError):
    code = "MCP_INPUT_REQUIRED"


class MCPClient:
    """MCP 2026-07-28 stdio client.

    The modern protocol is stateless: no initialize handshake and no session id.
    Version/capability metadata travels in ``_meta`` on every request, and
    ``server/discover`` is probed first. Legacy initialize is an explicit, recorded
    fallback only when discovery is unavailable.

    MCP is a tool boundary, never a scheduler or verification authority.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        request_timeout: float = 30.0,
        allow_tools: set[str] | None = None,
        approval_callback: Callable[[str, dict[str, Any]], bool] | None = None,
        allow_legacy_fallback: bool = True,
        enable_tasks: bool = True,
        trace_meta: dict[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("MCP command must not be empty")
        self.command = command
        self.cwd = str(Path(cwd).resolve()) if cwd else None
        self.env = env or {}
        self.request_timeout = request_timeout
        self.allow_tools = set(allow_tools or [])
        self.approval_callback = approval_callback
        self.allow_legacy_fallback = allow_legacy_fallback
        self.enable_tasks = enable_tasks
        self.trace_meta = trace_meta or {}
        self.process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self._stderr: list[str] = []
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.supported_versions: list[str] = []
        self.compatible: bool = False
        self.legacy: bool = False
        self.tools: list[dict[str, Any]] = []
        self.supports_tasks = False
        self.discovery_metadata: dict[str, Any] = {}
        self._closed = False

    def __enter__(self) -> MCPClient:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def start(self) -> None:
        executable = shutil.which(self.command[0])
        if executable is None and not Path(self.command[0]).exists():
            raise ExternalBlockedError(f"MCP server executable not found: {self.command[0]}")
        environment = os.environ.copy()
        environment.update(self.env)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            [executable or self.command[0], *self.command[1:]],
            cwd=self.cwd,
            env=environment,
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
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()
        self.discover()

    def discover(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Probe server/discover, with a logged legacy fallback for older servers."""
        try:
            result = self._raw_request(
                "server/discover",
                {},
                timeout=timeout or min(self.request_timeout, 10.0),
            )
        except McpProtocolError as exc:
            if not self.allow_legacy_fallback:
                raise
            return self._legacy_initialize(discovery_error=str(exc))
        if "error" in result:
            error = result["error"]
            code = error.get("code") if isinstance(error, dict) else None
            # -32022 is a modern version error. Do not blindly fall back when a
            # supported-version list is present.
            if code == -32022:
                data = error.get("data", {}) if isinstance(error, dict) else {}
                supported = data.get("supported", []) if isinstance(data, dict) else []
                self.supported_versions = list(supported)
                raise ExternalBlockedError(
                    f"MCP server does not support {MCP_PROTOCOL_VERSION}; supported={supported}",
                    details={"rpc_code": code, "supported": supported},
                )
            if not self.allow_legacy_fallback:
                raise McpProtocolError(f"server/discover failed: {error}")
            return self._legacy_initialize(discovery_error=str(error))
        payload = self._complete_result(result, "server/discover")
        self.supported_versions = list(payload.get("supportedVersions", []))
        if MCP_PROTOCOL_VERSION not in self.supported_versions:
            raise ExternalBlockedError(
                "MCP server discovery does not include the required protocol version",
                details={"supported": self.supported_versions},
            )
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.compatible = True
        self.capabilities = payload.get("capabilities", {})
        self.server_info = payload.get("serverInfo", {})
        self.discovery_metadata = {
            "instructions": payload.get("instructions"),
            "ttlMs": payload.get("ttlMs"),
            "cacheScope": payload.get("cacheScope"),
        }
        extensions = self.capabilities.get("extensions", {}) if isinstance(self.capabilities, dict) else {}
        self.supports_tasks = (
            self.enable_tasks
            and isinstance(extensions, dict)
            and TASKS_EXTENSION in extensions
        )
        return payload

    def _legacy_initialize(self, *, discovery_error: str) -> dict[str, Any]:
        self.legacy = True
        self.compatible = False
        result = self._raw_request(
            "initialize",
            {
                "protocolVersion": MCP_LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "forgeflow", "version": "2.0.0"},
            },
            legacy=True,
        )
        if "error" in result:
            raise ExternalBlockedError(
                "MCP server is neither a modern 2026-07-28 server nor a usable legacy server",
                details={"discovery_error": discovery_error, "initialize_error": result["error"]},
            )
        payload = result.get("result", {})
        self.protocol_version = payload.get("protocolVersion")
        self.capabilities = payload.get("capabilities", {})
        self.server_info = payload.get("serverInfo", {})
        self.discovery_metadata = {
            "legacy_fallback": True,
            "discovery_error": discovery_error,
        }
        self._notify("notifications/initialized", {}, legacy=True)
        extensions = self.capabilities.get("extensions", {}) if isinstance(self.capabilities, dict) else {}
        self.supports_tasks = (
            self.enable_tasks
            and isinstance(extensions, dict)
            and TASKS_EXTENSION in extensions
        )
        return payload

    def list_tools(self) -> list[dict[str, Any]]:
        response = self._request("tools/list", {})
        tools = response.get("tools", [])
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list returned a non-list")
        self.tools = tools
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.allow_tools and name not in self.allow_tools:
            raise PolicyDeniedError(f"MCP tool is not allowlisted: {name}")
        if self.approval_callback is not None and not self.approval_callback(name, arguments or {}):
            raise PolicyDeniedError(f"MCP tool call was not approved: {name}")
        if not self.allow_tools and self.approval_callback is None:
            raise PolicyDeniedError(
                "MCP tool calls require an allowlist or an explicit approval callback"
            )
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("resultType") == "task":
            return result
        return result

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout_seconds: float = 600.0,
        default_poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        if not self.supports_tasks:
            raise ExternalBlockedError("MCP server does not advertise the tasks extension")
        deadline = time.monotonic() + timeout_seconds
        poll_interval = default_poll_interval
        while time.monotonic() < deadline:
            task = self.get_task(task_id)
            status = task.get("status")
            if status == "completed":
                return task
            if status == "failed":
                raise McpProtocolError(f"MCP task failed: {task.get('error')}")
            if status == "cancelled":
                raise McpProtocolError("MCP task was cancelled")
            if status == "input_required":
                raise McpInputRequiredError(
                    f"MCP task {task_id} requires client input", details={"task": task}
                )
            interval_ms = task.get("pollIntervalMs")
            if isinstance(interval_ms, (int, float)):
                poll_interval = max(0.05, float(interval_ms) / 1000.0)
            time.sleep(poll_interval)
        raise McpProtocolError(f"MCP task timed out: {task_id}")

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not self.supports_tasks:
            raise ExternalBlockedError("MCP server does not advertise the tasks extension")
        return self._request("tasks/get", {"taskId": task_id})

    def update_task(self, task_id: str, response: dict[str, Any]) -> dict[str, Any]:
        if not self.supports_tasks:
            raise ExternalBlockedError("MCP server does not advertise the tasks extension")
        return self._request("tasks/update", {"taskId": task_id, "response": response})

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        if not self.supports_tasks:
            raise ExternalBlockedError("MCP server does not advertise the tasks extension")
        return self._request("tasks/cancel", {"taskId": task_id})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._raw_request(method, params)
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            data = error.get("data") if isinstance(error, dict) else None
            raise McpProtocolError(
                f"MCP error for {method}: {message}",
                details={"rpc_code": code, "data": data, "method": method},
            )
        if self.legacy:
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}
        return self._complete_result(response, method)

    def _complete_result(self, response: dict[str, Any], method: str) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP response for {method} has no object result")
        result_type = result.get("resultType", "complete")
        if result_type == "complete":
            return result
        if result_type == "task":
            return result
        if result_type == "input_required":
            raise McpInputRequiredError(
                f"MCP {method} requires input (MRTR)",
                details={"result": result},
            )
        raise McpProtocolError(f"unknown MCP resultType: {result_type!r}")

    def _raw_request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None, legacy: bool = False
    ) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise McpProtocolError("MCP process is not running")
        self._next_id += 1
        request_id = str(self._next_id)
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[request_id] = waiter
        request_params = dict(params)
        if not legacy and not self.legacy:
            meta = dict(request_params.get("_meta", {}))
            meta.update(
                {
                    "io.modelcontextprotocol/protocolVersion": self.protocol_version
                    or MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": self._client_capabilities(),
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "forgeflow",
                        "version": "2.0.0",
                    },
                }
            )
            meta.update(self.trace_meta)
            request_params["_meta"] = meta
        message = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": request_params,
        }
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._pending.pop(request_id, None)
            raise McpProtocolError(f"failed to write MCP request: {exc}") from exc
        try:
            response = waiter.get(timeout=timeout or self.request_timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise McpProtocolError(f"MCP request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        return response

    def _client_capabilities(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {"roots": {"listChanged": False}}
        if self.enable_tasks:
            capabilities["extensions"] = {TASKS_EXTENSION: {"polling": True}}
        return capabilities

    def _notify(self, method: str, params: dict[str, Any], *, legacy: bool = False) -> None:
        if self.process is None or self.process.stdin is None:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not legacy and not self.legacy:
            message["params"] = {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": self.protocol_version
                    or MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": self._client_capabilities(),
                },
            }
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                waiter = self._pending.get(str(message["id"]))
                if waiter:
                    waiter.put(message)
            else:
                self.server_info.setdefault("notifications", []).append(message)
        for waiter in self._pending.values():
            waiter.put({"error": {"message": "MCP process exited"}})

    def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        total = 0
        for line in self.process.stderr:
            if total < 64 * 1024:
                self._stderr.append(line)
                total += len(line)

    @property
    def stderr_excerpt(self) -> str:
        return "".join(self._stderr)[-16000:]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is None:
            return
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    try:
                        self.process.kill()
                    except OSError:
                        pass
        if self._reader:
            self._reader.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)

    def describe(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "requested_version": MCP_PROTOCOL_VERSION,
            "supported_versions": self.supported_versions,
            "compatible": self.compatible,
            "legacy_fallback": self.legacy,
            "server_info": self.server_info,
            "capabilities": self.capabilities,
            "supports_tasks": self.supports_tasks,
            "tool_count": len(self.tools),
            "discovery": self.discovery_metadata,
        }
