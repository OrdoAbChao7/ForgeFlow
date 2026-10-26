from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from forgeflow.cli import app
from tests.conftest import calc_project, calc_spec

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def test_cli_end_to_end_and_help(tmp_path: Path) -> None:
    project = calc_project(tmp_path / "project")
    help_result = _invoke("--help")
    assert help_result.exit_code == 0
    for command in (
        "init",
        "goal",
        "plan",
        "run",
        "quick",
        "status",
        "verify",
        "resume",
        "recover",
        "context",
        "benchmark",
        "deliver",
        "doctor",
    ):
        assert command in help_result.stdout

    init = _invoke("init", "--workspace", str(project), "--json")
    assert init.exit_code == 0, init.stdout
    assert json.loads(init.stdout)["status"] == "initialized"

    goal_file = project / "goal.yaml"
    goal_file.write_text(
        yaml.safe_dump(calc_spec().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    created = _invoke(
        "goal", "--file", str(goal_file), "--workspace", str(project), "--json"
    )
    assert created.exit_code == 0, created.stdout
    goal_id = json.loads(created.stdout)["goal_id"]

    plan = _invoke("plan", goal_id, "--workspace", str(project), "--json")
    assert plan.exit_code == 0
    assert json.loads(plan.stdout)["ready"] == ["fix-add"]

    refused = _invoke(
        "run",
        goal_id,
        "--agent",
        "claude",
        "--workspace",
        str(project),
        "--json",
    )
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["status"] == "REFUSED"

    run = _invoke("run", goal_id, "--agent", "mock", "--workspace", str(project), "--json")
    assert run.exit_code == 0, run.stdout
    report = json.loads(run.stdout)
    assert report["goal_status"] == "VERIFYING"
    assert report["tasks"][0]["verified"] is True

    status = _invoke("status", goal_id, "--workspace", str(project), "--json")
    assert status.exit_code == 0
    assert json.loads(status.stdout)["status"] == "VERIFYING"

    context = _invoke(
        "context",
        "--task",
        "fix-add",
        "--goal",
        goal_id,
        "--workspace",
        str(project),
        "--json",
    )
    assert context.exit_code == 0, context.stdout
    bundle = json.loads(context.stdout)
    assert bundle["chunks"]
    assert bundle["budget"] >= bundle["used_tokens"] or bundle["parser_notes"]

    verify = _invoke("verify", goal_id, "--workspace", str(project), "--json")
    assert verify.exit_code == 0, verify.stdout
    assert json.loads(verify.stdout)["status"] == "PASS"

    explain = _invoke(
        "explain", "adds", "--goal", goal_id, "--workspace", str(project), "--json"
    )
    assert explain.exit_code == 0, explain.stdout
    provenance = json.loads(explain.stdout)
    assert provenance["edges"]
    assert provenance["edges"][0]["test_run"]["exit_code"] == 0
    assert provenance["edges"][0]["code"]["source_fingerprint"]

    clean = _invoke(
        "verify", goal_id, "--clean-room", "--workspace", str(project), "--json"
    )
    assert clean.exit_code == 0, clean.stdout
    assert json.loads(clean.stdout)["cleanroom"]["success"] is True

    deliver = _invoke("deliver", goal_id, "--workspace", str(project), "--json")
    assert deliver.exit_code == 0, deliver.stdout
    delivery = json.loads(deliver.stdout)
    assert delivery["state"] == "READY"
    assert Path(delivery["manifest"]).is_file()
    assert Path(delivery["sha256_file"]).is_file()

    status_after = _invoke("status", goal_id, "--workspace", str(project), "--json")
    assert json.loads(status_after.stdout)["status"] == "DELIVERED"

    events = _invoke("events", goal_id, "--workspace", str(project), "--json")
    assert events.exit_code == 0
    assert json.loads(events.stdout)

    (project / "calc.py").write_text(
        "def add(a, b):\n    return a * b\n", encoding="utf-8"
    )
    failed_verify = _invoke(
        "verify", goal_id, "--no-clean-room", "--workspace", str(project), "--json"
    )
    assert failed_verify.exit_code == 1
    assert json.loads(failed_verify.stdout)["status"] == "FAIL"
    blocked_delivery = _invoke("deliver", goal_id, "--workspace", str(project), "--json")
    assert blocked_delivery.exit_code == 1
    assert json.loads(blocked_delivery.stdout)["state"] != "READY"

    doctor = _invoke("doctor", "--workspace", str(project), "--json")
    assert doctor.exit_code == 0, doctor.stdout
    report = json.loads(doctor.stdout)
    assert report["status"] in {"PASS", "PASS_WITH_BLOCKERS"}
    assert any(check["name"] == "external_integrations" for check in report["checks"])


def test_cli_policy_commands_without_records(tmp_path: Path) -> None:
    project = calc_project(tmp_path / "project")
    _invoke("init", "--workspace", str(project), "--json")
    goal_file = project / "goal.yaml"
    goal_file.write_text(
        yaml.safe_dump(calc_spec().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    _invoke("goal", "--file", str(goal_file), "--workspace", str(project), "--json")
    result = _invoke(
        "policy",
        "propose",
        "--name",
        "context",
        "--config",
        '{"context_mode":"structure","recovery":true}',
        "--workspace",
        str(project),
        "--json",
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["validation"]["approved"] is False
    listing = _invoke("policy", "list", "--workspace", str(project), "--json")
    assert listing.exit_code == 0
    assert json.loads(listing.stdout)
