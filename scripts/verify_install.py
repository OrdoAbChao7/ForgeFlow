"""Build a wheel and verify install, CLI, doctor and a mock E2E in a fresh venv."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[dict[str, Any]] = []


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    started = __import__("datetime").datetime.now(__import__("datetime").UTC)
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    RESULTS.append(
        {
            "argv": command,
            "cwd": str(cwd),
            "exit_code": result.returncode,
            "started_at": started.isoformat(),
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-2000:],
        }
    )
    if check and result.returncode != 0:
        print(json.dumps(RESULTS, indent=2), file=sys.stderr)
        raise SystemExit(f"command failed: {command}\n{result.stderr}")
    return result


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    temp_root = Path(tempfile.mkdtemp(prefix="forgeflow-clean-install-")).resolve()
    dist = temp_root / "dist"
    venv = temp_root / "venv"
    demo = temp_root / "demo"
    demo.mkdir()
    run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=ROOT)
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, got {wheels}")
    wheel = wheels[0]
    run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT)
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    forge = scripts / ("forge.exe" if os.name == "nt" else "forge")
    run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel), "pytest"],
        cwd=temp_root,
        timeout=900,
    )
    run([str(forge), "--version"], cwd=demo)
    help_result = run([str(forge), "--help"], cwd=demo)
    for command in ("init", "goal", "run", "verify", "deliver", "doctor", "benchmark"):
        if command not in help_result.stdout:
            raise SystemExit(f"CLI help missing {command}")
    run([str(forge), "init", "--workspace", str(demo), "--json"], cwd=demo)
    (demo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (demo / "tests").mkdir()
    (demo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (demo / "conftest.py").write_text("", encoding="utf-8")
    (demo / ".gitignore").write_text(".forge/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "clean@forgeflow.invalid"),
        ("config", "user.name", "ForgeFlow Clean"),
        ("add", "-A"),
        ("commit", "-m", "clean fixture"),
    ):
        run(["git", *args], cwd=demo)
    goal = {
        "title": "Clean install E2E",
        "objective": "Verify add using real pytest in a fresh venv",
        "acceptance": [
            {
                "id": "adds",
                "statement": "add returns a+b",
                "gate": "unit",
                "required": True,
            }
        ],
        "tasks": [
            {
                "key": "fix-add",
                "title": "Fix add",
                "owned_paths": ["calc.py"],
                "gates": ["unit"],
                "acceptance_ids": ["adds"],
                "metadata": {
                    "mock": {"files": {"calc.py": "def add(a, b):\n    return a + b\n"}}
                },
            }
        ],
        "verifier": {
            "commands": [
                {
                    "id": "unit",
                    "gate": "unit",
                    "argv": [str(python), "-m", "pytest", "-q", "tests"],
                    "scope": ["adds"],
                    "timeout_seconds": 60,
                }
            ],
            "protected_paths": ["tests"],
            "clean_room": False,
            "policy_version": "clean-install-v1",
        },
    }
    write_json(demo / "goal.json", goal)
    created = run(
        [str(forge), "goal", "--file", str(demo / "goal.json"), "--workspace", str(demo), "--json"],
        cwd=demo,
    )
    goal_id = json.loads(created.stdout)["goal_id"]
    run(
        [str(forge), "run", goal_id, "--agent", "mock", "--workspace", str(demo), "--json"],
        cwd=demo,
        timeout=300,
    )
    verified = run(
        [str(forge), "verify", goal_id, "--workspace", str(demo), "--json"],
        cwd=demo,
        timeout=300,
    )
    verification = json.loads(verified.stdout)
    if verification["status"] != "PASS":
        raise SystemExit(f"clean-install verification did not pass: {verification}")
    delivered = run(
        [str(forge), "deliver", goal_id, "--workspace", str(demo), "--json"],
        cwd=demo,
        timeout=300,
    )
    delivery = json.loads(delivered.stdout)
    if delivery["state"] != "READY":
        raise SystemExit(f"clean-install delivery not READY: {delivery}")
    doctor = run([str(forge), "doctor", "--workspace", str(demo), "--json"], cwd=demo)
    doctor_payload = json.loads(doctor.stdout)
    output = {
        "status": "PASS",
        "wheel": {
            "name": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        },
        "venv_python": str(python),
        "installed_version": run([str(forge), "--version"], cwd=demo).stdout.strip(),
        "doctor_status": doctor_payload["status"],
        "goal_id": goal_id,
        "verification": verification["status"],
        "delivery_state": delivery["state"],
        "manifest": delivery["manifest"],
        "commands": RESULTS,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
