from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow import __version__
from forgeflow.agents.base import AgentAdapter
from forgeflow.domain.enums import AgentRunStatus, GateKind, TaskStatus
from forgeflow.domain.models import (
    AcceptanceCriterion,
    AgentSettlement,
    BenchmarkRecord,
    GoalSpec,
    TaskSpec,
    VerifierCommand,
    VerifierConfig,
    new_id,
    utcnow,
)
from forgeflow.parallel import ParallelWorktreeScheduler
from forgeflow.runtime import Harness, RunOptions
from forgeflow.state.store import Store
from forgeflow.verification.fingerprint import (
    environment_digest,
    environment_summary,
    git_commit,
    source_fingerprint,
)

BENCHMARK_VERSION = "internal-fixture-v1"


@dataclass(frozen=True)
class BenchmarkMode:
    key: str
    group: str
    context_mode: str
    recovery: bool
    reviewer: bool
    description: str


MODES = (
    BenchmarkMode("C_core", "C", "structure", True, False, "ForgeFlow core: structure context + recovery"),
    BenchmarkMode("D_full", "D", "structure", True, True, "ForgeFlow full: core + static reviewer"),
    BenchmarkMode("ablation_no_context", "ablation", "none", True, False, "context disabled"),
    BenchmarkMode("ablation_no_recovery", "ablation", "structure", False, False, "recovery disabled"),
    BenchmarkMode("ablation_full_file", "ablation", "full_file", True, False, "whole-file context"),
    BenchmarkMode("ablation_keyword", "ablation", "keyword", True, False, "keyword context"),
    BenchmarkMode("ablation_diff_only", "ablation", "diff_only", True, False, "diff-only context"),
)


@dataclass
class CaseDefinition:
    id: str
    title: str
    objective: str
    marker: str
    files: dict[str, str]
    solution: dict[str, str]
    bug: dict[str, str]
    tests: dict[str, str]
    protected_paths: list[str]
    acceptance: list[dict[str, Any]]
    task: dict[str, Any]
    fail_first: bool = False
    digest: str = ""

    @classmethod
    def load(cls, path: Path) -> CaseDefinition:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = dict(data)
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        return cls(**payload, digest=digest)


class BenchmarkOracleAdapter(AgentAdapter):
    """Simulated context-sensitive agent used only for harness/benchmark evaluation.

    It writes the known solution only if the contract marker is present in the assembled
    prompt. This is explicitly not a model-quality comparison; it measures whether the
    harness context and recovery loops deliver the required contract. Records label the
    agent as ``context_oracle_sim``.
    """

    name = "context_oracle_sim"

    def __init__(self, case: CaseDefinition) -> None:
        super().__init__()
        self.case = case
        self._prompts: dict[str, str] = {}

    def submit(self, run_id: str, prompt: str) -> str:
        record = self._get(run_id)
        self._prompts[run_id] = prompt
        record.status = AgentRunStatus.RUNNING
        attempt_match = __import__("re").search(r"Attempt:\s*(\d+)", prompt)
        attempt = int(attempt_match.group(1)) if attempt_match else 1
        marker_visible = self.case.marker in prompt
        had_failure = "Previous attempt failure" in prompt
        if self.case.fail_first and attempt == 1:
            files = self.case.bug
            reason = "simulated first-attempt bug"
        elif marker_visible and (not self.case.fail_first or had_failure or attempt > 1):
            files = self.case.solution
            reason = "contract marker visible"
        else:
            files = self.case.bug
            reason = "contract marker absent from prompt"
        for relative, content in files.items():
            target = (record.workspace / relative).resolve()
            target.relative_to(record.workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        record.add_event("mock_patch", {"files": sorted(files), "reason": reason})
        record.status = AgentRunStatus.SETTLED
        record.finished_at = utcnow()
        record.tokens = max(1, len(prompt) // 4)
        record.settlement = AgentSettlement(
            run_id=run_id,
            status=AgentRunStatus.SETTLED,
            success_claimed=True,
            cost_usd=0.0,
            tokens=record.tokens,
            output=f"simulated agent: {reason}",
            metadata={"simulated": True, "marker_visible": marker_visible},
        )
        record.add_event("agent_settled", {"aborted": False, "simulated": True})
        return "started"

    def cancel(self, run_id: str) -> None:
        record = self._get(run_id)
        record.status = AgentRunStatus.CANCELLED
        record.settlement = AgentSettlement(
            run_id=run_id, status=AgentRunStatus.CANCELLED, error="cancelled"
        )

    def wait_until_settled(self, run_id: str, timeout_seconds: float = 600.0) -> AgentSettlement:
        record = self._get(run_id)
        if record.settlement is None:
            self.submit(run_id, self._prompts.get(run_id, ""))
        assert record.settlement is not None
        return record.settlement


class HarborBridge:
    """Harbor is an external evaluation framework, not a local scheduler.

    If Harbor is not installed/configured, ForgeFlow runs its internal fixture benchmark
    and reports Harbor as EXTERNAL_BLOCKED instead of fabricating comparative results.
    """

    def __init__(self) -> None:
        self.executable = shutil.which("harbor")

    def status(self) -> dict[str, Any]:
        if not self.executable:
            return {
                "status": "EXTERNAL_BLOCKED",
                "reason": "harbor CLI is not installed; internal fixture benchmark used",
                "harbor_version": None,
            }
        try:
            result = subprocess.run(
                [self.executable, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return {
                "status": "NOT_RUN",
                "reason": "harbor CLI detected; external suite was not invoked because no "
                "verified Harbor job configuration was supplied",
                "exit_code": result.returncode,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "EXTERNAL_BLOCKED", "reason": str(exc)}


class BenchmarkSuite:
    def __init__(
        self,
        workspace: str | Path,
        *,
        cases_dir: str | Path | None = None,
        output_root: str | Path | None = None,
        repeats: int = 2,
        token_budget: int = 900,
        modes: tuple[BenchmarkMode, ...] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.cases_dir = Path(cases_dir or self.workspace / "benchmarks" / "cases").resolve()
        self.output_root = Path(output_root or self.workspace / "evidence" / "benchmarks").resolve()
        self.repeats = max(1, repeats)
        self.token_budget = token_budget
        self.modes = modes or MODES
        self.store = Store(self.workspace)

    def load_cases(self) -> list[CaseDefinition]:
        cases = [
            CaseDefinition.load(path)
            for path in sorted(self.cases_dir.glob("*.json"))
        ]
        if not cases:
            raise RuntimeError(f"no benchmark cases found in {self.cases_dir}")
        return cases

    def run(self, *, case_ids: list[str] | None = None) -> dict[str, Any]:
        cases = self.load_cases()
        if case_ids:
            selected = set(case_ids)
            cases = [case for case in cases if case.id in selected]
            if not cases:
                raise RuntimeError("no matching benchmark cases")
        run_id = f"bench_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{new_id('r')[-8:]}"
        output = self.output_root / run_id
        output.mkdir(parents=True, exist_ok=False)
        records: list[dict[str, Any]] = []
        started = time.time()
        for mode in self.modes:
            for case in cases:
                for repeat in range(1, self.repeats + 1):
                    record = self._run_case(mode, case, repeat, output)
                    records.append(record)
                    self.store.add_benchmark_record(
                        BenchmarkRecord(
                            id=record["id"],
                            benchmark=BENCHMARK_VERSION,
                            mode=mode.key,
                            case_id=case.id,
                            repeat=repeat,
                            agent=record["agent"],
                            verified_success=record["verified_success"],
                            false_completion=record["false_completion"],
                            duration_seconds=record.get("duration_seconds", 0.0),
                            cost_usd=record.get("cost_usd", 0.0),
                            tokens=record.get("tokens", 0),
                            context_mode=mode.context_mode,
                            recovery_enabled=mode.recovery,
                            reviewer_enabled=mode.reviewer,
                            parallel_enabled=False,
                            error=record.get("error"),
                            metadata=record,
                            created_at=utcnow(),
                        )
                    )
        summary = self._summarize(records, cases, run_id, output)
        summary["baselines"] = self._baselines()
        summary["harbor"] = HarborBridge().status()
        summary["parallel_ablation"] = _parallel_ablation()
        summary["duration_seconds"] = round(time.time() - started, 3)
        summary["output_dir"] = str(output)
        self._write_outputs(output, records, summary)
        return summary

    def _run_case(
        self, mode: BenchmarkMode, case: CaseDefinition, repeat: int, output: Path
    ) -> dict[str, Any]:
        run_id = f"{case.id}_{mode.key}_r{repeat}_{new_id('b')[-6:]}"
        work = Path(tempfile.mkdtemp(prefix="ffbench-")).resolve()
        harness: Harness | None = None
        started = time.monotonic()
        record: dict[str, Any] = {
            "id": run_id,
            "benchmark": BENCHMARK_VERSION,
            "case_id": case.id,
            "mode": mode.key,
            "group": mode.group,
            "repeat": repeat,
            "agent": "context_oracle_sim",
            "context_mode": mode.context_mode,
            "recovery_enabled": mode.recovery,
            "reviewer_enabled": mode.reviewer,
            "parallel_enabled": False,
            "verified_success": False,
            "false_completion": False,
            "error": None,
        }
        try:
            self._prepare_workspace(work, case)
            goal_spec = self._goal_spec(case)
            harness = Harness(work)
            goal = harness.create_goal(goal_spec)
            adapter = BenchmarkOracleAdapter(case)
            options = RunOptions(
                agent_name=adapter.name,
                context_mode=mode.context_mode,
                token_budget=self.token_budget,
                max_tasks=8,
                task_timeout_seconds=120,
                recovery=mode.recovery,
                reviewer=mode.reviewer,
            )
            report = harness.run(goal.id, adapter=adapter, options=options)
            verification = harness.verify_goal(goal.id)
            tasks = harness.store.list_tasks(goal.id)
            all_tasks_verified = bool(tasks) and all(
                task.status == TaskStatus.VERIFIED for task in tasks
            )
            record.update(
                {
                    "verified_success": all_tasks_verified
                    and verification.get("status") == "PASS",
                    "false_completion": any(
                        item.agent_claimed_success and not item.verified
                        for item in report.tasks
                    ),
                    "task_statuses": [task.status.value for task in tasks],
                    "attempts": [
                        {
                            "task_id": attempt.task_id,
                            "attempt_no": attempt.attempt_no,
                            "status": attempt.status.value,
                            "error_category": attempt.error_category.value
                            if attempt.error_category
                            else None,
                        }
                        for attempt in harness.store.list_attempts(goal.id)
                    ],
                    "context_tokens": report.context_tokens,
                    "cost_usd": report.agent_cost_usd,
                    "tokens": sum(
                        attempt.tokens for attempt in harness.store.list_attempts(goal.id)
                    ),
                    "goal_status": report.goal_status.value,
                    "verification": verification,
                    "source_fingerprint": source_fingerprint(
                        work, protected_paths=case.protected_paths
                    ),
                    "git_sha": git_commit(work),
                    "env_digest": environment_digest(work),
                    "evidence": self._collect_evidence(harness, goal.id, output, run_id),
                }
            )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 4)
            if harness is not None:
                harness.telemetry.shutdown()
            shutil.rmtree(work, ignore_errors=True)
        return record

    def _prepare_workspace(self, work: Path, case: CaseDefinition) -> None:
        files = {
            **case.files,
            **case.tests,
            ".gitignore": ".forge/\n__pycache__/\n.pytest_cache/\n",
        }
        for relative, content in files.items():
            target = (work / relative).resolve()
            target.relative_to(work)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        env = os.environ.copy()
        self._git_command(work, "init", "-b", "main", check=True)
        self._git_command(work, "config", "core.longpaths", "true", check=True)
        self._git_command(work, "config", "user.email", "benchmark@forgeflow.invalid", check=True)
        self._git_command(work, "config", "user.name", "ForgeFlow Benchmark", check=True)
        self._git_command(work, "add", "-A", check=True, retries=3)
        self._git_command(
            work, "commit", "-m", "benchmark fixture baseline", check=True, env=env
        )

    def _git_command(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        retries: int = 1,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        last: subprocess.CompletedProcess[str] | None = None
        for attempt in range(retries):
            last = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=60,
            )
            if last.returncode == 0:
                return last
            if attempt + 1 < retries:
                time.sleep(0.2 * (attempt + 1))
        if check and last is not None:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({last.returncode}): "
                f"{last.stderr.strip() or last.stdout.strip()}"
            )
        assert last is not None
        return last

    def _goal_spec(self, case: CaseDefinition) -> GoalSpec:
        acceptance = [
            AcceptanceCriterion(**item)
            for item in case.acceptance
        ]
        task = TaskSpec(**case.task)
        scope = task.acceptance_ids or [item.id for item in acceptance]
        command = VerifierCommand(
            id="unit-tests",
            gate=GateKind.UNIT,
            argv=[sys.executable, "-m", "pytest", "-q", "tests"],
            timeout_seconds=120,
            scope=scope,
        )
        return GoalSpec(
            title=case.title,
            objective=case.objective,
            acceptance=acceptance,
            tasks=[task],
            verifier=VerifierConfig(
                commands=[command],
                protected_paths=case.protected_paths,
                clean_room=False,
                policy_version="benchmark-verifier-v1",
            ),
            metadata={"benchmark_case": case.id, "case_digest": case.digest},
        )

    def _collect_evidence(
        self, harness: Harness, goal_id: str, output: Path, run_id: str
    ) -> list[dict[str, Any]]:
        items = []
        logs_dir = output / "logs" / run_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        for evidence in harness.store.list_evidence(goal_id):
            result: dict[str, Any] = {
                "id": evidence.id,
                "gate": evidence.gate.value,
                "argv": evidence.argv,
                "exit_code": evidence.exit_code,
                "timed_out": evidence.timed_out,
                "source_fingerprint": evidence.source_fingerprint,
                "env_digest": evidence.env_digest,
                "stdout_sha256": evidence.stdout_sha256,
                "stderr_sha256": evidence.stderr_sha256,
                "report_sha256": evidence.report_sha256,
                "runner_kind": evidence.runner_kind,
                "trust": evidence.trust.value,
            }
            log_dir = Path(evidence.log_dir).parent
            if log_dir.is_dir():
                destination = logs_dir / evidence.runner_id
                shutil.copytree(log_dir, destination, dirs_exist_ok=True)
                result["archived_log_dir"] = str(destination.relative_to(output))
                result["archived_hashes"] = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(destination.iterdir())
                    if path.is_file()
                }
            items.append(result)
        return items

    def _summarize(
        self,
        records: list[dict[str, Any]],
        cases: list[CaseDefinition],
        run_id: str,
        output: Path,
    ) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            groups.setdefault(record["mode"], []).append(record)
        summary_rows = []
        for mode in self.modes:
            selected = groups.get(mode.key, [])
            count = len(selected)
            verified = sum(1 for item in selected if item["verified_success"])
            false_completion = sum(1 for item in selected if item["false_completion"])
            cost = sum(item.get("cost_usd", 0.0) for item in selected)
            summary_rows.append(
                {
                    "mode": mode.key,
                    "group": mode.group,
                    "description": mode.description,
                    "context_mode": mode.context_mode,
                    "recovery_enabled": mode.recovery,
                    "reviewer_enabled": mode.reviewer,
                    "parallel_enabled": False,
                    "runs": count,
                    "verified_successes": verified,
                    "verified_success_rate": round(verified / count, 4) if count else 0.0,
                    "false_completion_rate": round(false_completion / count, 4) if count else 0.0,
                    "cost_usd": round(cost, 6),
                    "cost_per_verified_task": round(cost / verified, 6) if verified else None,
                    "avg_tokens": round(
                        sum(item.get("tokens", 0) for item in selected) / count, 2
                    )
                    if count
                    else 0.0,
                    "avg_latency_seconds": round(
                        sum(item.get("duration_seconds", 0) for item in selected) / count, 4
                    )
                    if count
                    else 0.0,
                    "recovery_case_success_rate": _recovery_rate(selected),
                }
            )
        return {
            "benchmark": BENCHMARK_VERSION,
            "run_id": run_id,
            "generated_at": utcnow().isoformat(),
            "forgeflow_version": __version__,
            "git_sha": git_commit(self.workspace),
            "workspace": str(self.workspace),
            "repeats": self.repeats,
            "token_budget": self.token_budget,
            "cases": [{"id": case.id, "digest": case.digest} for case in cases],
            "modes": [asdict(mode) for mode in self.modes],
            "rows": summary_rows,
            "records": len(records),
            "environment": environment_summary(self.workspace),
            "environment_digest": environment_digest(self.workspace),
            "limitations": [
                "Agent is a deterministic context-sensitive simulator, not a real LLM.",
                "No native-agent or OpenSpec/GSD baseline was run without approved credentials/tools.",
                "Costs are zero for the simulated agent; token figures are prompt approximations.",
                "Static reviewer had no findings on these benign fixtures; reviewer ablation is therefore neutral.",
                "Parallel ablation measures harness/worktree overhead with a simulated worker, "
                "not LLM-agent concurrency.",
            ],
        }

    def _baselines(self) -> list[dict[str, Any]]:
        pi_path = shutil.which("pi")
        claude_path = shutil.which("claude")
        return [
            {
                "mode": "A_native_agent",
                "status": "NOT_RUN",
                "reason": "no approved paid model baseline; CLI presence is not a benchmark result",
                "pi_installed": bool(pi_path),
                "claude_installed": bool(claude_path),
            },
            {
                "mode": "B_openspec_gsd",
                "status": "NOT_RUN",
                "reason": "OpenSpec/GSD baseline tools were not configured or executed",
            },
        ]

    def _write_outputs(
        self, output: Path, records: list[dict[str, Any]], summary: dict[str, Any]
    ) -> None:
        (output / "runs.jsonl").write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )
        with (output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary["rows"][0].keys()))
            writer.writeheader()
            writer.writerows(summary["rows"])
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
        markdown = [
            f"# ForgeFlow benchmark {summary['run_id']}",
            "",
            "- Agent: `context_oracle_sim` (simulated, not LLM quality)",
            f"- Git SHA: `{summary['git_sha']}`",
            f"- Repeats: {summary['repeats']}",
            f"- Token budget: {summary['token_budget']}",
            "",
            "| Mode | Context | Recovery | Reviewer | Runs | Verified | False completion | Avg tokens | Latency (s) |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
        for row in summary["rows"]:
            markdown.append(
                f"| {row['mode']} | {row['context_mode']} | {row['recovery_enabled']} | "
                f"{row['reviewer_enabled']} | {row['runs']} | {row['verified_success_rate']:.2%} | "
                f"{row['false_completion_rate']:.2%} | {row['avg_tokens']:.1f} | "
                f"{row['avg_latency_seconds']:.3f} |"
            )
        markdown.extend(
            [
                "",
                "## Baselines",
                "",
                *[
                    f"- `{item['mode']}`: **{item['status']}** — {item['reason']}"
                    for item in summary.get("baselines", [])
                ],
                "",
                "## Limitations",
                "",
                *[f"- {item}" for item in summary["limitations"]],
                "",
            ]
        )
        (output / "summary.md").write_text("\n".join(markdown), encoding="utf-8")
        files = []
        for name in ("runs.jsonl", "summary.csv", "summary.json", "summary.md"):
            path = output / name
            files.append(
                {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
        manifest = {
            "format_version": 1,
            "generated_at": utcnow().isoformat(),
            "run_id": summary["run_id"],
            "git_sha": summary["git_sha"],
            "environment": summary["environment"],
            "environment_digest": summary["environment_digest"],
            "cases": summary["cases"],
            "modes": summary["modes"],
            "artifacts": files,
            "baselines": summary.get("baselines"),
            "harbor": summary.get("harbor"),
            "parallel_ablation": summary.get("parallel_ablation"),
            "not_run": [
                item["mode"] for item in summary.get("baselines", []) if item["status"] == "NOT_RUN"
            ],
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )


def _recovery_rate(records: list[dict[str, Any]]) -> float | None:
    recover_cases = [item for item in records if item["case_id"] == "recover_once"]
    if not recover_cases:
        return None
    return round(
        sum(1 for item in recover_cases if item["verified_success"]) / len(recover_cases), 4
    )


def _parallel_ablation() -> dict[str, Any]:
    """Compare serial and two-worker worktree scheduling with a deterministic worker."""
    results: dict[str, Any] = {"status": "PASS", "mode": "worktree_serial_vs_parallel"}

    def run(workers: int) -> dict[str, Any]:
        repo = Path(tempfile.mkdtemp(prefix="ffparallel-ablation-"))
        try:
            for name, content in {
                "a.py": "A = 1\n",
                "b.py": "B = 1\n",
                ".gitignore": ".forge/\n__pycache__/\n.pytest_cache/\n",
            }.items():
                (repo / name).write_text(content, encoding="utf-8")
            for args in (
                ("init", "-b", "main"),
                ("config", "core.longpaths", "true"),
                ("config", "user.email", "benchmark@forgeflow.invalid"),
                ("config", "user.name", "ForgeFlow Benchmark"),
                ("add", "-A"),
                ("commit", "-m", "parallel baseline"),
            ):
                subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
            tasks = [
                TaskSpec(key="a", title="a", owned_paths=["a.py"]),
                TaskSpec(key="b", title="b", owned_paths=["b.py"]),
            ]
            spec = GoalSpec(
                title="parallel ablation",
                objective="schedule independent files",
                acceptance=[
                    AcceptanceCriterion(
                        id="done", statement="both merged", gate=GateKind.MANUAL
                    )
                ],
                tasks=tasks,
                verifier=VerifierConfig(),
            )

            def worker(task: TaskSpec, worktree: Path) -> str:
                time.sleep(0.25)
                (worktree / f"{task.key}.py").write_text(
                    f"{task.key.upper()} = 2\n", encoding="utf-8"
                )
                return task.key

            def regression() -> bool:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import a, b; assert a.A in (1, 2) and b.B in (1, 2)",
                    ],
                    cwd=repo,
                    capture_output=True,
                )
                return result.returncode == 0

            started = time.monotonic()
            report = ParallelWorktreeScheduler(
                repo, max_workers=workers, verify_after_merge=regression
            ).schedule(f"goal_parallel_{workers}", spec, worker)
            elapsed = time.monotonic() - started
            return {
                "workers": workers,
                "merged": report.merged,
                "conflicts": report.conflicts,
                "duration_seconds": round(elapsed, 4),
                "success": sorted(report.merged) == ["a", "b"] and not report.conflicts,
            }
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    try:
        serial = run(1)
        parallel = run(2)
        results.update({"serial": serial, "parallel": parallel})
        if not serial["success"] or not parallel["success"]:
            results["status"] = "FAIL"
        if parallel["duration_seconds"] > 0:
            results["speedup"] = round(
                serial["duration_seconds"] / parallel["duration_seconds"], 4
            )
    except Exception as exc:
        results.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
    results["note"] = (
        "Simulated worker measures scheduling and merge overhead; it is not a model-quality "
        "comparison and does not imply LLM agents should run five-way concurrent."
    )
    return results
