from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from forgeflow.agents.base import AgentAdapter, AgentRunRecord
from forgeflow.domain.enums import AgentRunStatus
from forgeflow.domain.errors import DependencyUnavailableError
from forgeflow.domain.models import AgentSettlement, utcnow


class ClaudeCliAdapter(AgentAdapter):
    """Second real-agent adapter using Claude Code non-interactive stream-json output.

    Credential-independent contract tests exercise the JSONL parser with a fixture
    executable. A paid model run is never triggered by tests or by the default CLI.
    """

    name = "claude_cli"

    def __init__(
        self,
        *,
        executable: str = "claude",
        extra_args: list[str] | None = None,
        base_args: list[str] | None = None,
        max_budget_usd: float = 1.0,
        allowed_tools: str = "Read,Write,Edit,Glob,Grep",
        permission_mode: str = "acceptEdits",
    ) -> None:
        super().__init__()
        self.executable = shutil.which(executable) or (executable if Path(executable).exists() else None)
        self.extra_args = list(extra_args or [])
        self.base_args = list(base_args or [])
        self.max_budget_usd = max_budget_usd
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stderr: dict[str, list[str]] = {}
        self._settled: dict[str, threading.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._last_text: dict[str, str] = {}

    @staticmethod
    def probe(executable: str = "claude") -> dict[str, Any]:
        path = shutil.which(executable)
        if not path:
            return {"available": False, "executable": None, "version": None}
        try:
            version = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10
            )
            auth: dict[str, Any] | None = None
            try:
                auth_result = subprocess.run(
                    [path, "auth", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if auth_result.returncode == 0 and auth_result.stdout.strip():
                    auth = json.loads(auth_result.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                auth = None
            return {
                "available": True,
                "executable": path,
                "version": (version.stdout or version.stderr).strip().splitlines()[0],
                "auth": auth,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "executable": path, "version": f"error:{exc}"}

    def submit(self, run_id: str, prompt: str) -> str:
        record = self._get(run_id)
        if not self.executable:
            record.status = AgentRunStatus.EXTERNAL_BLOCKED
            raise DependencyUnavailableError("Claude Code CLI is not installed")
        if record.settlement is not None:
            raise RuntimeError("run already settled")
        environment = os.environ.copy()
        # Prevent nested session metadata leakage; credentials remain in the user's
        # normal credential store and are never copied into ForgeFlow state.
        environment.pop("CLAUDECODE", None)
        args = [
            self.executable,
            *self.base_args,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            self.permission_mode,
            "--allowedTools",
            self.allowed_tools,
            "--max-budget-usd",
            str(self.max_budget_usd),
            *self.extra_args,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                args,
                cwd=str(record.workspace),
                env=environment,
                stdin=subprocess.DEVNULL,
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
            raise DependencyUnavailableError(f"failed to start Claude CLI: {exc}") from exc
        self._processes[run_id] = process
        self._stderr[run_id] = []
        self._settled[run_id] = threading.Event()
        record.status = AgentRunStatus.RUNNING
        record.accepted_at = utcnow()
        record.add_event("prompt_accepted", {"transport": "claude_stream_json"})
        record.trace.update(
            {
                "pid": process.pid,
                "max_budget_usd": self.max_budget_usd,
                "permission_mode": self.permission_mode,
                "allowed_tools": self.allowed_tools,
            }
        )
        reader = threading.Thread(target=self._read, args=(record, process), daemon=True)
        stderr_reader = threading.Thread(
            target=self._read_stderr, args=(record, process), daemon=True
        )
        self._threads[run_id] = reader
        reader.start()
        stderr_reader.start()
        return "started"

    def _read(self, record: AgentRunRecord, process: subprocess.Popen[str]) -> None:
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
                event_type = str(message.get("type", "unknown"))
                record.add_event(event_type, message)
                if event_type == "assistant":
                    text = _claude_text(message)
                    if text:
                        self._last_text[record.run_id] = text
                elif event_type == "result":
                    self._results[record.run_id] = message
                    cost = message.get("total_cost_usd")
                    if isinstance(cost, (int, float)):
                        record.cost_usd = float(cost)
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        tokens = usage.get("total_tokens") or 0
                        if isinstance(tokens, (int, float)):
                            record.tokens = int(tokens)
        finally:
            code = process.wait()
            result = self._results.get(record.run_id, {})
            is_error = bool(result.get("is_error", False))
            if code == 0 and not is_error:
                record.status = AgentRunStatus.SETTLED
                error = None
            elif record.run_id in self._cancelled:
                record.status = AgentRunStatus.CANCELLED
                error = "cancelled"
            else:
                record.status = AgentRunStatus.FAILED
                error = str(result.get("result") or f"Claude CLI exited with code {code}")
            record.error = error
            record.settlement = AgentSettlement(
                run_id=record.run_id,
                status=record.status,
                success_claimed=code == 0 and not is_error,
                error=error,
                cost_usd=record.cost_usd,
                tokens=record.tokens,
                output=self._last_text.get(record.run_id, ""),
                metadata={"exit_code": code, "result": result, "transport": "claude_stream_json"},
            )
            record.finished_at = utcnow()
            self._settled[record.run_id].set()

    @property
    def _cancelled(self) -> set[str]:
        if not hasattr(self, "_cancelled_runs"):
            self._cancelled_runs: set[str] = set()
        return self._cancelled_runs

    def _read_stderr(self, record: AgentRunRecord, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        total = 0
        for line in process.stderr:
            if total < 64 * 1024:
                self._stderr[record.run_id].append(line)
                total += len(line)

    def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if process is None or process.poll() is not None:
            return
        self._cancelled.add(run_id)
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            process.kill()

    def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0) -> AgentSettlement:
        record = self._get(run_id)
        if run_id not in self._settled:
            raise RuntimeError("run was not submitted")
        if not self._settled[run_id].wait(timeout=timeout_seconds):
            self.cancel(run_id)
            if not self._settled[run_id].wait(timeout=5):
                record.status = AgentRunStatus.CRASHED
                record.settlement = AgentSettlement(
                    run_id=run_id,
                    status=AgentRunStatus.CRASHED,
                    error=f"Claude CLI did not settle within {timeout_seconds}s",
                )
        assert record.settlement is not None
        record.trace["stderr_excerpt"] = "".join(self._stderr.get(run_id, []))[-16000:]
        return record.settlement

    def close(self) -> None:
        for run_id in list(self._processes):
            self.cancel(run_id)


def _claude_text(message: dict[str, Any]) -> str:
    message_body = message.get("message")
    if isinstance(message_body, dict):
        content = message_body.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    result = message.get("result")
    return result if isinstance(result, str) else ""
