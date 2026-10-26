from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from forgeflow import __version__
from forgeflow.verification.cleanroom import docker_available
from forgeflow.verification.fingerprint import (
    environment_digest,
    environment_summary,
    git_commit,
    is_git_repo,
    tool_version,
)


def run_doctor(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: Any, remediation: str | None = None) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "remediation": remediation,
            }
        )

    add("python", "PASS" if sys.version_info >= (3, 11) else "FAIL", sys.version)
    add("forgeflow_version", "PASS", __version__)
    add(
        "workspace_writable",
        "PASS" if os.access(root, os.W_OK) else "FAIL",
        str(root),
        "choose a writable workspace with --workspace",
    )
    git = shutil.which("git")
    add(
        "git",
        "PASS" if git else "FAIL",
        tool_version("git"),
        "install Git; durable clean-room snapshots require it",
    )
    add(
        "git_repository",
        "PASS" if is_git_repo(root) else "WARN",
        git_commit(root),
        "run `git init` for commit-bound evidence and clean-room verification",
    )
    db_path = root / ".forge" / "forgeflow.db"
    if db_path.exists():
        try:
            connection = sqlite3.connect(str(db_path))
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            connection.close()
            add(
                "database",
                "PASS" if integrity == "ok" else "FAIL",
                {"path": str(db_path), "integrity": integrity, "schema_version": version},
            )
        except sqlite3.Error as exc:
            add("database", "FAIL", f"{db_path}: {exc}", "run `forge init` or restore the state DB")
    else:
        add(
            "database",
            "NOT_INITIALIZED",
            str(db_path),
            "run `forge init`",
        )
    tools = {}
    for name in (
        "uv",
        "pytest",
        "ruff",
        "tree-sitter",
        "node",
        "npm",
        "pi",
        "claude",
        "harbor",
        "iverilog",
        "verilator",
    ):
        if name == "tree-sitter":
            try:
                import tree_sitter

                tools[name] = getattr(tree_sitter, "__version__", "installed")
            except ImportError:
                tools[name] = "MISSING"
        else:
            tools[name] = tool_version(name)
    add("tools", "PASS", tools)
    add(
        "docker",
        "PASS" if docker_available() else "BLOCKED",
        tool_version("docker", ("version", "--format", "{{.Server.Version}}")),
        "start Docker Desktop to enable optional clean-room containers; local isolated rooms still work",
    )
    external = {
        "pi_rpc": {
            "status": "AVAILABLE_UNVERIFIED_MODEL"
            if shutil.which("pi")
            else "EXTERNAL_BLOCKED",
            "note": "RPC handshake/contract can run locally; paid model completion was not invoked",
        },
        "claude_cli": {
            "status": "AVAILABLE_UNVERIFIED_MODEL"
            if shutil.which("claude")
            else "EXTERNAL_BLOCKED",
            "note": "non-interactive paid completion was not invoked",
        },
        "harbor": {
            "status": "EXTERNAL_BLOCKED" if not shutil.which("harbor") else "AVAILABLE",
            "note": "internal fixture benchmark is used; no Harbor comparison is fabricated",
        },
        "fpga_hardware": {
            "status": "EXTERNAL_BLOCKED",
            "note": "Icarus/hardware unavailable; fixture parsers are testable but hardware PASS is never inferred",
        },
    }
    add("external_integrations", "BLOCKED", external)
    add(
        "environment",
        "PASS",
        {**environment_summary(root), "environment_digest": environment_digest(root)},
    )
    overall = "PASS"
    if any(check["status"] == "FAIL" for check in checks):
        overall = "FAIL"
    elif any(check["status"] in {"BLOCKED", "WARN", "NOT_INITIALIZED"} for check in checks):
        overall = "PASS_WITH_BLOCKERS"
    return {
        "status": overall,
        "workspace": str(root),
        "forgeflow_version": __version__,
        "python": sys.version,
        "platform": platform.platform(),
        "checks": checks,
    }


def doctor_text(report: dict[str, Any]) -> str:
    lines = [f"ForgeFlow doctor: {report['status']} ({report['workspace']})"]
    for check in report["checks"]:
        detail = check["detail"]
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        lines.append(f"[{check['status']:16}] {check['name']}: {detail}")
        if check.get("remediation"):
            lines.append(f"    -> {check['remediation']}")
    return "\n".join(lines)
