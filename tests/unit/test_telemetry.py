from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forgeflow.observability.telemetry import Telemetry, read_spans, scrub
from forgeflow.runtime import Harness, RunOptions
from tests.conftest import calc_spec


def test_scrubber_redacts_secrets_prompts_and_code() -> None:
    cleaned = scrub(
        {
            "api_key": "sk-live-1234567890",
            "authorization": "Bearer abc",
            "nested": {"prompt": "private prompt", "code": "secret code"},
            "gen_ai.system": "pi_rpc",
            "gen_ai.usage.input_tokens": 10,
            "message": "Bearer supersecrettoken and sk-abcdefghijklmnop",
        }
    )
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["authorization"] == "[REDACTED]"
    assert cleaned["nested"]["prompt"] == "[REDACTED]"
    assert cleaned["nested"]["code"] == "[REDACTED]"
    assert cleaned["gen_ai.system"] == "pi_rpc"
    assert cleaned["gen_ai.usage.input_tokens"] == 10
    assert "supersecrettoken" not in cleaned["message"]


def test_otel_spans_are_written_and_scrubbed(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    try:
        with telemetry.span(
            "goal.run",
            {"forge.goal_id": "g1", "api_key": "secret", "prompt": "do not log"},
        ):
            with telemetry.span("task.run", {"forge.task_id": "t1"}):
                pass
    finally:
        telemetry.shutdown()
    spans = read_spans(tmp_path)
    assert len(spans) == 2
    names = {span["name"] for span in spans}
    assert names == {"goal.run", "task.run"}
    serialized = json.dumps(spans)
    assert "do not log" not in serialized
    assert "secret" not in serialized
    task_span = next(span for span in spans if span["name"] == "task.run")
    parent_span = next(span for span in spans if span["name"] == "goal.run")
    assert task_span["parent_span_id"] == parent_span["span_id"]
    assert task_span["trace_id"] == parent_span["trace_id"]


def test_local_fallback_still_records(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry._otel = False
    telemetry._tracer = None
    try:
        with telemetry.span("fallback", {"safe": "value"}):
            pass
    finally:
        telemetry.shutdown()
    spans = read_spans(tmp_path)
    assert spans[-1]["name"] == "fallback"
    assert spans[-1]["attributes"]["safe"] == "value"


def test_retention_purges_old_spans(tmp_path: Path) -> None:
    path = tmp_path / ".forge" / "traces" / "spans.jsonl"
    path.parent.mkdir(parents=True)
    old = {
        "event": "span",
        "name": "old",
        "start_time_unix_nano": int(
            (datetime.now(UTC) - timedelta(days=60)).timestamp() * 1e9
        ),
    }
    fresh = {
        "event": "span",
        "name": "fresh",
        "start_time_unix_nano": int(datetime.now(UTC).timestamp() * 1e9),
    }
    path.write_text(json.dumps(old) + "\n" + json.dumps(fresh) + "\n", encoding="utf-8")
    telemetry = Telemetry(tmp_path, retention_days=14)
    telemetry.shutdown()
    names = {span["name"] for span in read_spans(tmp_path)}
    assert "old" not in names
    assert "fresh" in names


def test_runtime_emits_goal_task_agent_verifier_tool_spans(project: Path) -> None:
    harness = Harness(project)
    goal = harness.create_goal(calc_spec())
    harness.run(goal.id, options=RunOptions(agent_name="mock"))
    harness.telemetry.shutdown()
    spans = read_spans(project)
    names = {span["name"] for span in spans}
    assert {"goal.run", "task.run", "agent.run", "verifier.run", "tool.run"}.issubset(names)
    serialized = json.dumps(spans)
    assert "Make add return the sum" not in serialized
    assert "def add" not in serialized
    usage = [
        span
        for span in spans
        if span["name"] == "agent.run" and "gen_ai.usage.input_tokens" in span.get("attributes", {})
    ]
    assert usage
