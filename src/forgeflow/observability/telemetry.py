from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "prompt",
    "raw_prompt",
    "messages",
    "code",
    "source_code",
    "content",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_password",
    "_secret",
    "_credential",
    "_access_token",
    "_refresh_token",
)
_SAFE_GENAI_KEYS = {
    "gen_ai.system",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.agent.name",
    "gen_ai.tool.name",
}


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    short = lowered.rsplit(".", 1)[-1]
    return short in SENSITIVE_KEYS or any(
        short.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES
    )


def scrub(value: Any, *, key: str = "", max_string: int = 300) -> Any:
    """Remove credentials and redact prompt/code content before trace persistence."""
    if key and key in _SAFE_GENAI_KEYS:
        return value
    if key and _sensitive(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if _sensitive(str(child_key)) and str(child_key) not in _SAFE_GENAI_KEYS:
                cleaned[str(child_key)] = "[REDACTED]"
            else:
                cleaned[str(child_key)] = scrub(child_value, key=str(child_key), max_string=max_string)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub(item, max_string=max_string) for item in value[:100]]
    if isinstance(value, str):
        value = re.sub(
            r"(?i)\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{16,}\b",
            "[REDACTED]",
            value,
        )
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer [REDACTED]", value)
        return value if len(value) <= max_string else value[:max_string] + "...[truncated]"
    return value


class _JsonlSpanExporter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        with self.path.open("a", encoding="utf-8") as handle:
            for span in spans:
                context = span.get_span_context()
                record = {
                    "event": "otel_span",
                    "trace_id": f"{context.trace_id:032x}",
                    "span_id": f"{context.span_id:016x}",
                    "parent_span_id": (
                        f"{span.parent.span_id:016x}" if span.parent is not None else None
                    ),
                    "name": span.name,
                    "start_time_unix_nano": span.start_time,
                    "end_time_unix_nano": span.end_time,
                    "status": span.status.status_code.name,
                    "attributes": scrub(dict(span.attributes or {})),
                }
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class Telemetry:
    """OTel-first tracing with a local JSONL exporter and no external collector.

    Prompt text and source code never reach the trace sink. If the OTel SDK is
    unavailable, a deterministic local span writer keeps observability functional.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        service_name: str = "forgeflow",
        retention_days: int = 14,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.path = self.root / ".forge" / "traces" / "spans.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.service_name = service_name
        self.retention_days = retention_days
        self._provider: Any = None
        self._tracer: Any = None
        self._otel = False
        if enabled:
            self._purge()
            try:
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                exporter = _JsonlSpanExporter(self.path)
                provider = TracerProvider()
                provider.add_span_processor(SimpleSpanProcessor(exporter))
                self._provider = provider
                self._tracer = provider.get_tracer("forgeflow", "2.0.0")
                self._otel = True
            except Exception:
                self._otel = False

    def _purge(self) -> None:
        if not self.path.exists() or self.retention_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        kept: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                start_ns = record.get("start_time_unix_nano")
                if start_ns and datetime.fromtimestamp(start_ns / 1e9, UTC) < cutoff:
                    continue
            except (json.JSONDecodeError, ValueError, OSError):
                continue
            kept.append(line)
        if len(kept) != len(self.path.read_text(encoding="utf-8").splitlines()):
            self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    @property
    def mode(self) -> str:
        return "opentelemetry" if self._otel else "local_jsonl_fallback"

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        if not self.enabled:
            yield {}
            return
        safe_attributes = scrub(attributes or {})
        state: dict[str, Any] = {}
        if self._otel and self._tracer is not None:
            with self._tracer.start_as_current_span(name) as span:
                for key, value in safe_attributes.items():
                    span.set_attribute(key, value)
                try:
                    yield state
                except Exception as exc:
                    try:
                        from opentelemetry.trace import Status, StatusCode

                        span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
                    except Exception:
                        pass
                    raise
                finally:
                    for key, value in state.items():
                        span.set_attribute(key, scrub(value))
        else:
            start_ns = time.time_ns()
            trace_id = uuid.uuid4().hex
            span_id = uuid.uuid4().hex[:16]
            status = "OK"
            error: str | None = None
            try:
                yield state
            except Exception as exc:
                status = "ERROR"
                error = str(exc)[:300]
                raise
            finally:
                record = {
                    "event": "span",
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": None,
                    "name": name,
                    "start_time_unix_nano": start_ns,
                    "end_time_unix_nano": time.time_ns(),
                    "status": status,
                    "attributes": scrub({**safe_attributes, **state}),
                    "error": scrub(error or ""),
                }
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")

    def shutdown(self) -> None:
        if self._provider is not None:
            try:
                self._provider.shutdown()
            except Exception:
                pass


def read_spans(root: str | Path) -> list[dict[str, Any]]:
    path = Path(root).resolve() / ".forge" / "traces" / "spans.jsonl"
    if not path.exists():
        return []
    spans: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            spans.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return spans
