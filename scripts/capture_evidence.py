"""Run real verification commands and produce a hash-bound evidence manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def now() -> str:
    return datetime.now(UTC).isoformat()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


PHASES: dict[str, list[list[str]]] = {
    "phase0": [
        ["git", "status", "--short", "--branch"],
        ["git", "log", "-1", "--format=%H%n%s"],
        ["uv", "run", "forge", "--help"],
        ["uv", "run", "forge", "doctor", "--json"],
        [
            "uv",
            "run",
            "python",
            "-c",
            (
                "from pathlib import Path; import json; "
                "required=['ARCHITECTURE.md','TASKS.md','STATUS.md','DECISIONS.md','PROTOCOL_NOTES.md']; "
                "missing=[x for x in required if not (Path('docs/forgeflow-v2')/x).is_file()]; "
                "print(json.dumps({'missing':missing})); "
                "raise SystemExit(1 if missing else 0)"
            ),
        ],
    ],
    "phase1": [
        [
            "uv",
            "run",
            "pytest",
            "tests/unit/test_planner.py",
            "tests/unit/test_store.py",
            "tests/unit/test_runner.py",
            "tests/integration/test_recovery_resume.py",
            "-q",
        ],
    ],
    "phase2": [
        [
            "uv",
            "run",
            "pytest",
            "tests/security/test_evidence.py",
            "tests/integration/test_e2e_mock.py",
            "-q",
        ],
    ],
    "phase3": [
        [
            "uv",
            "run",
            "pytest",
            "tests/unit/test_context.py",
            "tests/integration/test_agents.py",
            "tests/integration/test_mcp_acp.py",
            "-q",
        ],
    ],
    "phase4": [
        [
            "uv",
            "run",
            "pytest",
            "tests/unit/test_telemetry.py",
            "tests/integration/test_parallel.py",
            "tests/integration/test_benchmark_policy.py",
            "-q",
        ],
        ["uv", "run", "forge", "benchmark", "--repeats", "2", "--workspace", "."],
    ],
    "phase5": [
        [
            "uv",
            "run",
            "pytest",
            "tests/integration/test_domain_adapters.py",
            "tests/integration/test_cli.py",
            "-q",
        ],
        ["uv", "run", "python", "scripts/verify_install.py"],
    ],
    "delivery": [
        ["uv", "run", "python", "scripts/make_demo_delivery.py"],
    ],
    "final": [
        ["uv", "run", "ruff", "check", "src", "tests"],
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "--junitxml=evidence/final/junit.xml",
            "--cov=forgeflow",
            "--cov-report=json:evidence/final/coverage.json",
            "--cov-report=xml:evidence/final/coverage.xml",
        ],
        ["uv", "run", "forge", "doctor", "--json"],
        ["git", "status", "--short"],
        ["git", "rev-parse", "HEAD"],
    ],
}


def collect_artifacts() -> list[dict[str, Any]]:
    patterns = [
        "evidence/final/coverage.json",
        "evidence/final/coverage.xml",
        "evidence/final/junit.xml",
        "evidence/delivery/demo/manifest.json",
        "evidence/delivery/demo/manifest.md",
        "evidence/delivery/demo/manifest.sha256",
        "evidence/delivery/demo/log_archive.json",
    ]
    benchmark_runs = sorted(
        (ROOT / "evidence" / "benchmarks").glob("bench_*"), key=lambda path: path.name
    )
    if benchmark_runs:
        for name in ("manifest.json", "runs.jsonl", "summary.csv", "summary.json", "summary.md"):
            patterns.append(str((benchmark_runs[-1] / name).relative_to(ROOT)))
    artifacts = []
    for relative in patterns:
        path = ROOT / relative
        if path.is_file():
            artifacts.append(
                {
                    "path": relative,
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    return artifacts


def run_phase(name: str, commands: list[list[str]]) -> dict[str, Any]:
    directory = EVIDENCE / name
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for index, command in enumerate(commands, 1):
        slug = f"{index:02d}_{command[0].replace('/', '_')}_{command[-1].replace('/', '_')[:30]}"
        slug = "".join(character if character.isalnum() or character in "._-" else "_" for character in slug)
        started = now()
        before_sha = git("rev-parse", "HEAD")
        before_status = git("status", "--short")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            timeout=1800,
        )
        stdout_path = directory / f"{slug}.stdout.log"
        stderr_path = directory / f"{slug}.stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        record = {
            "index": index,
            "argv": command,
            "cwd": str(ROOT),
            "started_at": started,
            "ended_at": now(),
            "exit_code": completed.returncode,
            "git_sha_before": before_sha,
            "git_sha_after": git("rev-parse", "HEAD"),
            "git_dirty_before": bool(before_status),
            "git_dirty_after": bool(git("status", "--short")),
            "stdout": {
                "path": str(stdout_path.relative_to(ROOT)),
                "sha256": sha256(stdout_path),
                "bytes": stdout_path.stat().st_size,
            },
            "stderr": {
                "path": str(stderr_path.relative_to(ROOT)),
                "sha256": sha256(stderr_path),
                "bytes": stderr_path.stat().st_size,
            },
            "status": "PASS" if completed.returncode == 0 else "FAIL",
        }
        records.append(record)
        print(f"[{record['status']}] {name} {' '.join(command)}", flush=True)
        if completed.returncode != 0:
            print(completed.stdout[-4000:], flush=True)
            print(completed.stderr[-4000:], file=sys.stderr, flush=True)
    return {
        "phase": name,
        "status": "PASS" if all(item["exit_code"] == 0 for item in records) else "FAIL",
        "commands": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phases", nargs="*", choices=[*PHASES, "all"], default=["all"])
    parser.add_argument("--artifacts-only", action="store_true")
    args = parser.parse_args()
    selected = [] if args.artifacts_only else list(PHASES) if "all" in args.phases else args.phases
    EVIDENCE.mkdir(exist_ok=True)
    manifest_path = EVIDENCE / "manifest.json"
    previous: dict[str, Any] = {"phases": []}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {"phases": []}
    existing = {item["phase"]: item for item in previous.get("phases", [])}
    for name in selected:
        existing[name] = run_phase(name, PHASES[name])
    phases = [existing[name] for name in PHASES if name in existing]
    manifest = {
        "format_version": 1,
        "generated_at": now(),
        "git_sha": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "workspace": str(ROOT),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "executable": sys.executable,
        },
        "status": "PASS" if all(item["status"] == "PASS" for item in phases) else "INCOMPLETE_OR_FAIL",
        "phases": phases,
        "artifacts": collect_artifacts(),
        "notes": [
            "Each command log is retained locally with SHA-256.",
            "A PASS means the recorded command exited 0; it does not imply external credentials or hardware passed.",
            "Benchmark baselines A/B are NOT_RUN and Harbor is EXTERNAL_BLOCKED unless separately proven.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "status": manifest["status"]}))


if __name__ == "__main__":
    main()
