from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from forgeflow import __version__
from forgeflow.agents import create_agent_adapter
from forgeflow.context.engine import ContextEngine
from forgeflow.delivery import build_delivery
from forgeflow.doctor import doctor_text, run_doctor
from forgeflow.domain.enums import RiskLevel
from forgeflow.domain.errors import ForgeFlowError
from forgeflow.evaluation.benchmark import BenchmarkSuite
from forgeflow.evaluation.policy import PolicyOptimizer, ValidationThresholds
from forgeflow.planner.compiler import compile_goal, summarize_plan
from forgeflow.planner.graph import task_id_for
from forgeflow.runtime import Harness, RunOptions
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import git_commit

app = typer.Typer(
    name="forge",
    help="ForgeFlow 2.0 - durable, verifiable and evaluable AI coding harness.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
policy_app = typer.Typer(help="Offline policy validation, activation and rollback.")
app.add_typer(policy_app, name="policy")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    return None


def _workspace(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise typer.BadParameter(f"workspace is not a directory: {resolved}")
    return resolved


def _resolve_goal(store: Store, goal_id: str | None) -> str:
    if goal_id:
        return goal_id
    goals = store.list_goals()
    if not goals:
        raise typer.BadParameter("no goals exist; create one with `forge goal --file ...`")
    return goals[-1].id


def _json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _fail(exc: ForgeFlowError, as_json: bool) -> None:
    if as_json:
        _json(exc.as_dict())
    else:
        console.print(f"[red]{exc.code}[/red]: {exc.message}")
    raise typer.Exit(1)


@app.command()
def init(
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="Project workspace."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Initialize ForgeFlow state without overwriting project files."""
    root = _workspace(workspace)
    try:
        store = Store(root)
        info = {
            "workspace": str(root),
            "database": str(store.db_path),
            "git_sha": git_commit(root),
            "version": __version__,
            "status": "initialized",
        }
        if json_output:
            _json(info)
        else:
            console.print(f"ForgeFlow initialized at [bold]{store.db_path}[/bold]")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command("goal")
def goal_create(
    file: Path | None = typer.Option(None, "--file", "-f", exists=True, help="YAML/JSON goal."),
    text: str | None = typer.Option(None, "--text", "-t", help="Natural-language goal (offline compiler)."),
    risk: RiskLevel = typer.Option(RiskLevel.STANDARD_CODE, "--risk"),
    quick: bool = typer.Option(False, "--quick", help="Risk-scoped quick planning."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compile and persist a goal, revision and dependency DAG."""
    root = _workspace(workspace)
    try:
        spec = compile_goal(file=file, text=text, risk=risk, quick=quick)
        harness = Harness(root)
        created = harness.create_goal(spec, quick_mode=quick)
        payload = {
            "goal_id": created.id,
            "revision": created.current_revision,
            "status": created.status.value,
            "title": created.title,
            "tasks": [
                {"key": task.key, "id": task_id_for(created.id, task.key)}
                for task in spec.tasks
            ],
            "git_sha": git_commit(root),
        }
        if json_output:
            _json(payload)
        else:
            console.print(f"Goal [bold]{created.id}[/bold]: {created.title}")
            console.print(f"Revision {created.current_revision}; {len(spec.tasks)} task(s)")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def plan(
    goal_id: str | None = typer.Argument(None),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the hard-dependency DAG, waves and ready frontier."""
    root = _workspace(workspace)
    try:
        store = Store(root)
        resolved = _resolve_goal(store, goal_id)
        spec = store.get_spec(resolved)
        statuses = {key: status.value for key, status in store.task_status_map(resolved).items()}
        summary = summarize_plan(spec, statuses)
        payload = {"goal_id": resolved, **summary}
        if json_output:
            _json(payload)
        else:
            table = Table(title=f"Plan {resolved}")
            table.add_column("Wave")
            table.add_column("Tasks")
            for index, wave in enumerate(summary["waves"], 1):
                table.add_row(str(index), ", ".join(wave))
            table.add_row("ready", ", ".join(summary["ready"]) or "-")
            console.print(table)
            for warning in summary["warnings"]:
                console.print(f"[yellow]warning[/yellow]: {warning}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def run(
    goal_id: str | None = typer.Argument(None),
    agent: str = typer.Option("mock", "--agent", help="mock, pi_rpc or claude_cli."),
    context_mode: str = typer.Option(
        "structure", "--context", help="structure, full_file, keyword, diff_only or none."
    ),
    token_budget: int = typer.Option(4000, "--token-budget", min=64),
    max_tasks: int = typer.Option(50, "--max-tasks", min=1),
    timeout: float = typer.Option(600.0, "--task-timeout", min=1),
    recovery: bool = typer.Option(True, "--recovery/--no-recovery"),
    reviewer: bool = typer.Option(False, "--reviewer/--no-reviewer"),
    policy: str | None = typer.Option(
        None, "--policy", help="Apply an active offline-validated policy by name."
    ),
    allow_paid: bool = typer.Option(False, "--allow-paid", help="Authorize a real paid agent/model run."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Execute the ready frontier with independent verification."""
    root = _workspace(workspace)
    if agent not in {"mock", "pi_rpc", "pi", "claude_cli", "claude"} and not allow_paid:
        console.print(f"[red]unknown agent[/red]: {agent}")
        raise typer.Exit(2)
    if agent not in {"mock"} and not allow_paid:
        message = (
            "real agent runs can incur model charges; pass --allow-paid to authorize "
            "a bounded run (adapter max budget applies to Claude Code)"
        )
        if json_output:
            _json({"status": "REFUSED", "reason": message})
        else:
            console.print(f"[yellow]refused[/yellow]: {message}")
        raise typer.Exit(2)
    try:
        harness = Harness(root)
        resolved = _resolve_goal(harness.store, goal_id)
        adapter = None
        if agent != "mock":
            adapter = create_agent_adapter(agent)
        options = RunOptions(
            agent_name=agent,
            context_mode=context_mode,
            token_budget=token_budget,
            max_tasks=max_tasks,
            task_timeout_seconds=timeout,
            recovery=recovery,
            reviewer=reviewer,
            policy_name=policy,
        )
        report = harness.run(resolved, adapter=adapter, options=options)
        if json_output:
            _json(report.as_dict())
        else:
            table = Table(title=f"Run {resolved} → {report.goal_status.value}")
            table.add_column("Task")
            table.add_column("Status")
            table.add_column("Verified")
            table.add_column("Evidence")
            for item in report.tasks:
                table.add_row(
                    item.task_key,
                    item.status.value,
                    str(item.verified),
                    str(len(item.evidence_ids)),
                )
            console.print(table)
            console.print(
                f"context≈{report.context_tokens} tokens; cost=${report.agent_cost_usd:.4f}"
            )
            for error in report.errors:
                console.print(f"[red]error[/red]: {error}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def quick(
    text: str | None = typer.Option(None, "--text", "-t"),
    file: Path | None = typer.Option(None, "--file", "-f", exists=True),
    risk: RiskLevel = typer.Option(RiskLevel.STANDARD_CODE, "--risk"),
    agent: str = typer.Option("mock", "--agent"),
    allow_paid: bool = typer.Option(False, "--allow-paid"),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compile a risk-scoped quick plan and run it (gates are never skipped)."""
    root = _workspace(workspace)
    if agent != "mock" and not allow_paid:
        console.print("[yellow]refused[/yellow]: pass --allow-paid for a real agent run")
        raise typer.Exit(2)
    try:
        spec = compile_goal(file=file, text=text, risk=risk, quick=True)
        harness = Harness(root)
        created = harness.create_goal(spec, quick_mode=True)
        adapter = None if agent == "mock" else create_agent_adapter(agent)
        report = harness.run(
            created.id,
            adapter=adapter,
            options=RunOptions(agent_name=agent, context_mode="structure", recovery=True),
        )
        payload = {
            "goal_id": created.id,
            "quick_mode": True,
            "risk": spec.risk.value,
            "report": report.as_dict(),
            "gates_skipped": False,
        }
        if json_output:
            _json(payload)
        else:
            console.print(
                f"Quick goal {created.id}: {report.goal_status.value}; "
                "risk checks, evidence and gates were retained"
            )
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def status(
    goal_id: str | None = typer.Argument(None),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show durable task, gate and recovery status."""
    root = _workspace(workspace)
    try:
        harness = Harness(root)
        resolved = _resolve_goal(harness.store, goal_id)
        report = harness.status(resolved)
        if json_output:
            _json(report)
        else:
            console.print(f"[bold]{report.title}[/bold] {report.goal_id}")
            console.print(
                f"status={report.status.value} revision={report.revision} git={report.git_sha}"
            )
            console.print("tasks: " + ", ".join(f"{key}={value}" for key, value in sorted(report.tasks.items())))
            console.print(
                "gates: "
                + ", ".join(
                    f"{key}={value.value}" for key, value in sorted(report.gates.items())
                )
            )
            if report.next_actions:
                console.print("next: " + "; ".join(report.next_actions))
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def verify(
    goal_id: str | None = typer.Argument(None),
    clean_room: bool | None = typer.Option(
        None,
        "--clean-room/--no-clean-room",
        help="Rebuild from a trusted snapshot (default: goal verifier config).",
    ),
    commands: str | None = typer.Option(None, "--commands", help="Comma-separated verifier ids."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run trusted verifier commands and evaluate all contract gates."""
    root = _workspace(workspace)
    try:
        harness = Harness(root)
        resolved = _resolve_goal(harness.store, goal_id)
        command_ids = [item.strip() for item in commands.split(",") if item.strip()] if commands else None
        use_clean_room = (
            clean_room
            if clean_room is not None
            else harness.store.get_spec(resolved).verifier.clean_room
        )
        result = harness.verify_goal(
            resolved, clean_room=use_clean_room, command_ids=command_ids
        )
        if json_output:
            _json({"goal_id": resolved, **result})
        else:
            console.print(f"Verification: [bold]{result['status']}[/bold]")
            for gate, value in result.get("gates", {}).items():
                console.print(f"  {gate}: {value}")
            for failure in result.get("failures", []):
                console.print(f"  [red]failure[/red]: {failure}")
            if result.get("cleanroom"):
                console.print(
                    f"  clean-room success={result['cleanroom']['success']} "
                    f"evidence={len(result['cleanroom']['evidence_ids'])}"
                )
        if result["status"] != "PASS":
            raise typer.Exit(1)
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def resume(
    goal_id: str | None = typer.Argument(None),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recover expired leases and requeue safely resumable work."""
    root = _workspace(workspace)
    try:
        harness = Harness(root)
        resolved = _resolve_goal(harness.store, goal_id)
        payload = harness.resume(resolved)
        if json_output:
            _json(payload)
        else:
            console.print(f"Resumed {resolved}: ready={payload['ready']}")
            for action in payload["next_actions"]:
                console.print(f"  next: {action}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def recover(
    goal_id: str | None = typer.Argument(None),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect interrupted/blocked tasks and required manual actions."""
    root = _workspace(workspace)
    try:
        harness = Harness(root)
        resolved = _resolve_goal(harness.store, goal_id)
        payload = harness.recover(resolved)
        if json_output:
            _json(payload)
        else:
            console.print(f"Recovery report for {resolved}")
            for action in payload["actions"]:
                console.print(f"  {action}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def context(
    task_key: str = typer.Option(..., "--task", help="Task key."),
    goal_id: str | None = typer.Option(None, "--goal"),
    mode: str = typer.Option("structure", "--mode"),
    token_budget: int = typer.Option(4000, "--token-budget", min=64),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Build an explainable, budgeted context bundle."""
    root = _workspace(workspace)
    try:
        store = Store(root)
        resolved = _resolve_goal(store, goal_id)
        spec = store.get_spec(resolved)
        task = next((item for item in spec.tasks if item.key == task_key), None)
        if task is None:
            raise typer.BadParameter(f"unknown task key: {task_key}")
        engine = ContextEngine(root)
        try:
            bundle = engine.build(spec, task, token_budget=token_budget, mode=mode)
        finally:
            engine.close()
        if json_output:
            _json(bundle.as_dict())
        else:
            console.print(
                f"{mode}: {bundle.used_tokens}/{bundle.budget} estimated tokens, "
                f"{len(bundle.chunks)} chunk(s), {len(bundle.omitted)} omitted"
            )
            for chunk in bundle.chunks:
                console.print(
                    f"  {chunk.path}:{chunk.start_line}-{chunk.end_line} "
                    f"{chunk.symbol or chunk.kind} score={chunk.score} "
                    f"why={'; '.join(chunk.reasons[:2])}"
                )
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def explain(
    criterion_id: str = typer.Argument(...),
    goal_id: str | None = typer.Option(None, "--goal"),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Trace which test and source version support one acceptance criterion."""
    root = _workspace(workspace)
    try:
        from forgeflow.verification.gates import evidence_provenance

        store = Store(root)
        resolved = _resolve_goal(store, goal_id)
        payload = evidence_provenance(store, resolved, criterion_id)
        if json_output:
            _json(payload)
        else:
            console.print(
                f"Criterion [bold]{criterion_id}[/bold]: {payload['answer']} "
                f"(gate={payload['gate']['status'] if payload['gate'] else 'BLOCKED'})"
            )
            for edge in payload["edges"]:
                console.print(
                    f"  {edge['evidence_id']} {edge['test_run']['argv']} "
                    f"exit={edge['test_run']['exit_code']} "
                    f"source={edge['code']['source_fingerprint'][:12]}"
                )
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def pause(
    goal_id: str | None = typer.Argument(None),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Pause a running goal without losing task leases or events."""
    try:
        store = Store(_workspace(workspace))
        resolved = _resolve_goal(store, goal_id)
        goal = store.pause_goal(resolved)
        payload = {"goal_id": resolved, "status": goal.status.value}
        if json_output:
            _json(payload)
        else:
            console.print(f"Goal {resolved}: {goal.status.value}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def cancel(
    goal_id: str | None = typer.Argument(None),
    reason: str = typer.Option("cancelled by user", "--reason"),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Cancel a goal and all unfinished tasks."""
    try:
        store = Store(_workspace(workspace))
        resolved = _resolve_goal(store, goal_id)
        goal = store.cancel_goal(resolved, reason=reason)
        payload = {"goal_id": resolved, "status": goal.status.value, "reason": reason}
        if json_output:
            _json(payload)
        else:
            console.print(f"Goal {resolved}: {goal.status.value}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def benchmark(
    repeats: int = typer.Option(2, "--repeats", min=1, max=20),
    cases: str | None = typer.Option(None, "--cases", help="Comma-separated case ids."),
    token_budget: int = typer.Option(900, "--token-budget", min=64),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the reproducible internal fixture benchmark and ablations."""
    root = _workspace(workspace)
    try:
        suite = BenchmarkSuite(root, repeats=repeats, token_budget=token_budget)
        case_ids = [item.strip() for item in cases.split(",") if item.strip()] if cases else None
        summary = suite.run(case_ids=case_ids)
        if json_output:
            _json(summary)
        else:
            table = Table(title=f"Benchmark {summary['run_id']}")
            table.add_column("Mode")
            table.add_column("Context")
            table.add_column("Recovery")
            table.add_column("Runs")
            table.add_column("Verified")
            table.add_column("False completion")
            table.add_column("Latency")
            for row in summary["rows"]:
                table.add_row(
                    row["mode"],
                    row["context_mode"],
                    str(row["recovery_enabled"]),
                    str(row["runs"]),
                    f"{row['verified_success_rate']:.0%}",
                    f"{row['false_completion_rate']:.0%}",
                    f"{row['avg_latency_seconds']:.3f}s",
                )
            console.print(table)
            for baseline in summary["baselines"]:
                console.print(
                    f"[yellow]{baseline['mode']}: {baseline['status']}[/yellow] — {baseline['reason']}"
                )
            console.print(f"Artifacts: {summary.get('output_dir', 'evidence/benchmarks')}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def deliver(
    goal_id: str | None = typer.Argument(None),
    output: Path | None = typer.Option(None, "--output", "-o"),
    mark_delivered: bool = typer.Option(True, "--mark-delivered/--no-mark-delivered"),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Build a READY/BLOCKED delivery manifest and evidence index."""
    root = _workspace(workspace)
    try:
        store = Store(root)
        resolved = _resolve_goal(store, goal_id)
        manifest, destination = build_delivery(
            store,
            root,
            resolved,
            output_dir=output,
            mark_delivered=mark_delivered,
        )
        payload = {
            "state": manifest.state,
            "goal_id": manifest.goal_id,
            "output_dir": str(destination),
            "manifest": str(destination / "manifest.json"),
            "sha256_file": str(destination / "manifest.sha256"),
            "gates": {gate.gate.value: gate.status.value for gate in manifest.gates if gate.criterion_id is None},
            "unverified": manifest.unverified,
        }
        if json_output:
            _json(payload)
        else:
            console.print(
                f"Delivery [bold]{manifest.state}[/bold] — {destination / 'manifest.json'}"
            )
            for item in manifest.unverified:
                console.print(f"  [red]unverified[/red]: {item}")
        if manifest.state != "READY":
            raise typer.Exit(1)
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@app.command()
def doctor(
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check toolchain, persistence, git and external integration readiness."""
    report = run_doctor(_workspace(workspace))
    if json_output:
        _json(report)
    else:
        console.print(doctor_text(report))
    if report["status"] == "FAIL":
        raise typer.Exit(1)


@app.command()
def events(
    goal_id: str | None = typer.Argument(None),
    tail: int = typer.Option(20, "--tail", min=1, max=1000),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect append-only domain events."""
    root = _workspace(workspace)
    try:
        store = Store(root)
        resolved = _resolve_goal(store, goal_id)
        items = [
            {
                "sequence": event.sequence,
                "type": event.type.value,
                "task_id": event.task_id,
                "actor": event.actor,
                "timestamp": event.timestamp.isoformat(),
                "payload": event.payload,
            }
            for event in store.list_events(resolved)[-tail:]
        ]
        if json_output:
            _json(items)
        else:
            for item in items:
                console.print(
                    f"{item['sequence']:04d} {item['timestamp']} {item['type']} "
                    f"task={item['task_id'] or '-'} actor={item['actor']}"
                )
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@policy_app.command("list")
def policy_list(
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List policy candidates and active versions."""
    store = Store(_workspace(workspace))
    items = store.list_policies()
    payload = [
        {
            "id": item.id,
            "name": item.name,
            "version": item.version,
            "active": item.active,
            "config": item.config,
            "approved": bool(item.validation.get("approved")),
            "runs": item.validation.get("runs", 0),
        }
        for item in items
    ]
    if json_output:
        _json(payload)
    else:
        table = Table(title="Policy candidates")
        for column in ("Name", "Version", "Active", "Approved", "Runs", "ID"):
            table.add_column(column)
        for item in payload:
            table.add_row(
                item["name"],
                str(item["version"]),
                str(item["active"]),
                str(item["approved"]),
                str(item["runs"]),
                item["id"],
            )
        console.print(table)


@policy_app.command("propose")
def policy_propose(
    name: str = typer.Option(..., "--name"),
    config: str = typer.Option(..., "--config", help="JSON policy config."),
    min_runs: int = typer.Option(4, "--min-runs"),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate a config against recorded benchmarks and propose a version."""
    try:
        config_data = json.loads(config)
        store = Store(_workspace(workspace))
        optimizer = PolicyOptimizer(
            store,
            ValidationThresholds(min_runs=min_runs, min_verified_success_rate=0.9, max_false_completion_rate=0.05),
        )
        candidate = optimizer.propose(name, config_data)
        payload = candidate.model_dump(mode="json")
        if json_output:
            _json(payload)
        else:
            console.print(
                f"Proposed {candidate.name} v{candidate.version}: "
                f"approved={candidate.validation['approved']} runs={candidate.validation['runs']}"
            )
            for failure in candidate.validation["failures"]:
                console.print(f"  [yellow]{failure}[/yellow]")
    except (ForgeFlowError, json.JSONDecodeError, ValueError) as exc:
        if json_output:
            _json({"status": "ERROR", "error": str(exc)})
        else:
            console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(1) from exc


@policy_app.command("activate")
def policy_activate(
    candidate_id: str = typer.Argument(...),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Activate an approved candidate after offline validation."""
    try:
        store = Store(_workspace(workspace))
        PolicyOptimizer(store).activate(candidate_id)
        payload = {"status": "ACTIVE", "candidate_id": candidate_id}
        if json_output:
            _json(payload)
        else:
            console.print(f"Activated {candidate_id}")
    except ForgeFlowError as exc:
        _fail(exc, json_output)


@policy_app.command("rollback")
def policy_rollback(
    name: str = typer.Argument(...),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Roll back to the prior validated version."""
    try:
        store = Store(_workspace(workspace))
        candidate = PolicyOptimizer(store).rollback(name)
        payload = {
            "status": "ROLLED_BACK",
            "restored": candidate.model_dump(mode="json") if candidate else None,
        }
        if json_output:
            _json(payload)
        else:
            console.print(
                f"Rolled back {name} to "
                f"{candidate.version if candidate else 'no prior version'}"
            )
    except ForgeFlowError as exc:
        _fail(exc, json_output)


if __name__ == "__main__":
    app()
