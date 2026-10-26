from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from forgeflow.agents.base import AgentAdapter, AgentRunRecord
from forgeflow.domain.enums import AgentRunStatus
from forgeflow.domain.errors import DependencyUnavailableError, ForgeFlowError
from forgeflow.domain.models import AgentSettlement, utcnow


class AgentProtocolError(ForgeFlowError):
    code = "AGENT_PROTOCOL_ERROR"


class PiRpcAdapter(AgentAdapter):
    """Pi coding agent over the documented `pi --mode rpc` JSONL protocol.

    Commands are JSON lines on stdin. Responses carry the request id; stream events are
    emitted on stdout. Prompt acceptance is deliberately distinct from `agent_settled`.
    """

    name = "pi_rpc"
    DEFAULT_ARGS = (
        "--mode",
        "rpc",
        "--no-session",
        "--no-context-files",
        "--no-extensions",
        "--no-mcp",
        "--no-approve",
    )

    def __init__(
        self,
        *,
        executable: str = "pi",
        extra_args: list[str] | None = None,
        base_args: list[str] | None = None,
        environment: dict[str, str] | None = None,
        startup_timeout: float = 15.0,
    ) -> None:
        super().__init__()
        self.executable = shutil.which(executable) or (executable if Path(executable).exists() else None)
        self.extra_args = list(extra_args or [])
        self.base_args = list(base_args) if base_args is not None else list(self.DEFAULT_ARGS)
        self.environment = environment or {}
        self.startup_timeout = startup_timeout
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._pending: dict[str, dict[str, queue.Queue[dict[str, Any]]]] = {}
        self._settled_events: dict[str, threading.Event] = {}
        self._reader_threads: dict[str, threading.Thread] = {}
        self._stderr: dict[str, list[str]] = {}
        self._request_counter: dict[str, int] = {}
        self._cancel_requested: set[str] = set()
        self._last_text: dict[str, str] = {}

    @staticmethod
    def probe(executable: str = "pi") -> dict[str, Any]:
        path = shutil.which(executable)
        if not path:
            return {"available": False, "executable": None, "version": None}
        try:
            result = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10
            )
            return {
                "available": True,
                "executable": path,
                "version": (result.stdout or result.stderr).strip().splitlines()[0],
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "executable": path, "version": f"error:{exc}"}

    def _start(self, record: AgentRunRecord) -> None:
        if not self.executable:
            record.status = AgentRunStatus.EXTERNAL_BLOCKED
            raise DependencyUnavailableError(
                "Pi executable is not installed; Pi RPC contract tests can still run against a fixture"
            )
        environment = os.environ.copy()
        environment.update(self.environment)
        args = [self.executable, *self.base_args, *self.extra_args]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                args,
                cwd=str(record.workspace),
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
        except OSError as exc:
            record.status = AgentRunStatus.EXTERNAL_BLOCKED
            raise DependencyUnavailableError(f"failed to start Pi RPC: {exc}") from exc
        self._processes[record.run_id] = process
        self._pending[record.run_id] = {}
        self._settled_events[record.run_id] = threading.Event()
        self._stderr[record.run_id] = []
        self._request_counter[record.run_id] = 0
        reader = threading.Thread(
            target=self._read_stdout, args=(record, process), daemon=True, name=f"pi-rpc-{record.run_id}"
        )
        stderr_reader = threading.Thread(
            target=self._read_stderr, args=(record, process), daemon=True, name=f"pi-stderr-{record.run_id}"
        )
        self._reader_threads[record.run_id] = reader
        reader.start()
        stderr_reader.start()
        record.trace.update({"pid": process.pid, "executable": self.executable, "args": args[1:]})
        # Handshake: prove the process speaks RPC without spending a model call.
        try:
            state = self._request(record, {"type": "get_state"}, timeout=self.startup_timeout)
        except Exception as exc:
            record.status = AgentRunStatus.EXTERNAL_BLOCKED
            self._terminate(record)
            raise DependencyUnavailableError(f"Pi RPC handshake failed: {exc}") from exc
        record.trace["handshake_ok"] = True
        record.trace["session_state"] = state
        record.add_event("rpc_handshake", {"ok": True})

    def _read_stdout(self, record: AgentRunRecord, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    record.add_event("protocol_invalid_json", {"line": line[:500]})
                    continue
                if message.get("type") == "response":
                    request_id = str(message.get("id"))
                    waiters = self._pending.get(record.run_id, {})
                    waiter = waiters.get(request_id)
                    if waiter is not None:
                        waiter.put(message)
                else:
                    self._handle_event(record, message)
        finally:
            remaining = process.poll()
            if remaining is None:
                try:
                    remaining = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    remaining = None
            if record.settlement is None:
                record.status = AgentRunStatus.CRASHED
                record.error = f"Pi RPC process exited with code {remaining}"
                record.settlement = AgentSettlement(
                    run_id=record.run_id,
                    status=AgentRunStatus.CRASHED,
                    success_claimed=False,
                    error=record.error,
                )
                record.finished_at = utcnow()
            self._settled_events[record.run_id].set()
            waiters = self._pending.get(record.run_id, {})
            for waiter in waiters.values():
                waiter.put(
                    {
                        "type": "response",
                        "success": False,
                        "error": f"process exited with code {remaining}",
                        "_transport_error": True,
                    }
                )

    def _read_stderr(self, record: AgentRunRecord, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        total = 0
        for line in process.stderr:
            if total < 64 * 1024:
                self._stderr[record.run_id].append(line)
                total += len(line)

    def _handle_event(self, record: AgentRunRecord, message: dict[str, Any]) -> None:
        event_type = str(message.get("type", "unknown"))
        payload = {key: value for key, value in message.items() if key != "type"}
        record.add_event(event_type, payload)
        self._update_usage(record, payload)
        text = _extract_assistant_text(payload)
        if text:
            self._last_text[record.run_id] = text
        if event_type == "agent_settled":
            aborted = bool(payload.get("aborted", False))
            cancelled = aborted or record.run_id in self._cancel_requested
            record.status = AgentRunStatus.CANCELLED if cancelled else AgentRunStatus.SETTLED
            record.settlement = AgentSettlement(
                run_id=record.run_id,
                status=record.status,
                success_claimed=not cancelled,
                error="aborted" if cancelled else None,
                cost_usd=record.cost_usd,
                tokens=record.tokens,
                output=self._last_text.get(record.run_id, ""),
                metadata={"aborted": aborted, "transport": "pi_rpc_jsonl"},
            )
            record.finished_at = utcnow()
            self._settled_events[record.run_id].set()

    def _update_usage(self, record: AgentRunRecord, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        tokens = usage.get("totalTokens") or usage.get("total_tokens")
        if isinstance(tokens, (int, float)):
            record.tokens = max(record.tokens, int(tokens))
        cost = usage.get("costUsd") or usage.get("cost_usd") or usage.get("totalCost")
        if isinstance(cost, (int, float)):
            record.cost_usd = max(record.cost_usd, float(cost))

    def _request(
        self, record: AgentRunRecord, command: dict[str, Any], *, timeout: float
    ) -> Any:
        process = self._processes.get(record.run_id)
        if process is None or process.poll() is not None:
            raise AgentProtocolError("Pi RPC process is not running")
        if process.stdin is None:
            raise AgentProtocolError("Pi RPC stdin is unavailable")
        self._request_counter[record.run_id] += 1
        request_id = str(self._request_counter[record.run_id])
        request = {**command, "id": request_id}
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[record.run_id][request_id] = waiter
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AgentProtocolError(f"failed to write Pi RPC request: {exc}") from exc
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            raise AgentProtocolError(
                f"timed out waiting for Pi response to {command.get('type')}"
            ) from exc
        finally:
            self._pending[record.run_id].pop(request_id, None)
        if not response.get("success", False):
            raise AgentProtocolError(str(response.get("error", "RPC request failed")))
        return response.get("data")

    def submit(self, run_id: str, prompt: str) -> str:
        record = self._get(run_id)
        if record.settlement is not None:
            raise AgentProtocolError("run already settled")
        record.status = AgentRunStatus.RUNNING
        record.accepted_at = utcnow()
        data = self._request(
            record,
            {"type": "prompt", "message": prompt},
            timeout=30.0,
        )
        disposition = data.get("disposition") if isinstance(data, dict) else None
        record.trace["prompt_disposition"] = disposition
        record.add_event("prompt_accepted", {"disposition": disposition})
        if disposition == "handled":
            record.status = AgentRunStatus.SETTLED
            record.settlement = AgentSettlement(
                run_id=run_id,
                status=AgentRunStatus.SETTLED,
                success_claimed=True,
                output="prompt handled synchronously by an extension",
            )
            record.finished_at = utcnow()
            self._settled_events[run_id].set()
        return str(disposition or "started")

    def cancel(self, run_id: str) -> None:
        record = self._get(run_id)
        if record.settlement is not None:
            return
        self._cancel_requested.add(run_id)
        try:
            self._request(record, {"type": "abort"}, timeout=10.0)
        except Exception:
            self._terminate(record)
            record.status = AgentRunStatus.CANCELLED
            record.settlement = AgentSettlement(
                run_id=run_id, status=AgentRunStatus.CANCELLED, error="aborted"
            )
            self._settled_events[run_id].set()

    def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0) -> AgentSettlement:
        record = self._get(run_id)
        event = self._settled_events[run_id]
        if not event.wait(timeout=timeout_seconds):
            self._terminate(record)
            record.status = AgentRunStatus.CRASHED
            record.error = f"agent did not settle within {timeout_seconds}s"
            record.settlement = AgentSettlement(
                run_id=run_id,
                status=AgentRunStatus.CRASHED,
                error=record.error,
            )
            self._settled_events[run_id].set()
        assert record.settlement is not None
        self._reader_threads[run_id].join(timeout=5)
        record.trace["stderr_excerpt"] = "".join(self._stderr.get(run_id, []))[-16000:]
        return record.settlement

    def _terminate(self, record: AgentRunRecord) -> None:
        process = self._processes.get(record.run_id)
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    def close(self) -> None:
        for record in list(self._runs.values()):
            if record.settlement is None:
                self.cancel(record.run_id)
            self._terminate(record)
        for process in self._processes.values():
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass


def _extract_assistant_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
            ]
            return "".join(parts)
    return ""
