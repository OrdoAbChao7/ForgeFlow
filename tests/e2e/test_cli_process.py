from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import calc_project, calc_spec


def _cli(project: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "forgeflow", *args, "--workspace", str(project), "--json"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout)


def test_process_level_cli_goal_run_verify_deliver(tmp_path: Path) -> None:
    project = calc_project(tmp_path / "project")
    assert _cli(project, "init")["status"] == "initialized"
    goal_file = project / "goal.json"
    goal_file.write_text(
        json.dumps(calc_spec().model_dump(mode="json")), encoding="utf-8"
    )
    created = _cli(project, "goal", "--file", str(goal_file))
    goal_id = created["goal_id"]
    run = _cli(project, "run", goal_id, "--agent", "mock")
    assert run["goal_status"] == "VERIFYING"
    assert run["tasks"][0]["verified"] is True
    assert _cli(project, "verify", goal_id)["status"] == "PASS"
    explain = _cli(project, "explain", "adds", "--goal", goal_id)
    assert explain["edges"][0]["test_run"]["exit_code"] == 0
    delivery = _cli(project, "deliver", goal_id)
    assert delivery["state"] == "READY"
    assert Path(delivery["manifest"]).is_file()
