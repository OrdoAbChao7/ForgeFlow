from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.agents.base import AgentAdapter
from forgeflow.agents.mock import MockAdapter
from forgeflow.context.engine import ContextBundle, ContextEngine
from forgeflow.domain.enums import (
    AgentRunStatus,
    ErrorCategory,
    GateStatus,
    GoalStatus,
    TaskStatus,
    TrustLevel,
)
from forgeflow.domain.errors import (
    DependencyUnavailableError,
    ExternalBlockedError,
    ForgeFlowError,
    PolicyDeniedError,
    ValidationFailure,
)
from forgeflow.domain.models import (
    GateEvaluation,
    Goal,
    GoalSpec,
    StatusReport,
    Task,
    TaskSpec,
    new_id,
)
from forgeflow.observability.telemetry import Telemetry
from forgeflow.recovery.engine import RecoveryEngine, RecoveryPolicyError
from forgeflow.runner.command import CommandPolicy, CommandRunner, RunRequest
from forgeflow.state.store import Claim, Store
from forgeflow.verification.cleanroom import CleanRoomReport, CleanRoomVerifier
from forgeflow.verification.fingerprint import (
    diff_fingerprint,
    environment_digest,
    git_commit,
    protected_files_match_ref,
    source_fingerprint,
)
from forgeflow.verification.gates import EvidenceRecorder, GateEngine, capture_binding


@dataclass
class RunOptions:
    agent_name: str = "mock"
    context_mode: str = "structure"
    token_budget: int = 4000
    max_tasks: int = 50
    task_timeout_seconds: float = 600.0
    lease_seconds: float = 300.0
    verify: bool = True
    recovery: bool = True
    reviewer: bool = False
    policy_name: str | None = None


@dataclass
class TaskResult:
    task_key: str
    task_id: str
    status: TaskStatus
    attempt_id: str | None = None
    agent_claimed_success: bool = False
    verified: bool = False
    false_completion: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    recovery_retry: bool = False
    context_tokens: int = 0
    agent_cost_usd: float = 0.0


@dataclass
class RunReport:
    goal_id: str
    goal_status: GoalStatus
    tasks: list[TaskResult]
    context_tokens: int = 0
    agent_cost_usd: float = 0.0
    goal_verification: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    policy: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "goal_status": self.goal_status.value,
            "tasks": [
                {
                    **result.__dict__,
                    "status": result.status.value,
                }
                for result in self.tasks
            ],
            "context_tokens": self.context_tokens,
            "agent_cost_usd": self.agent_cost_usd,
            "goal_verification": self.goal_verification,
            "errors": self.errors,
            "policy": self.policy,
        }


@dataclass
class TaskVerification:
    verified: bool
    evidence_ids: list[str]
    evaluations: list[GateEvaluation]
    error: str | None = None
    category: ErrorCategory = ErrorCategory.CONTRACT
    failing_command: str = ""
    blocked: bool = False


class Harness:
    def __init__(
        self,
        workspace: str | Path,
        *,
        store: Store | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or Store(self.workspace)
        self.telemetry = telemetry or Telemetry(self.workspace)
        self.recovery = RecoveryEngine(self.store, self.workspace)
        self._runner: CommandRunner | None = None

    @property
    def runner(self) -> CommandRunner:
        if self._runner is None:
            self._runner = CommandRunner(
                self.workspace,
                log_root=self.workspace / ".forge" / "runs",
                policy=CommandPolicy(allow_workspace_writes=True),
            )
        return self._runner

    def create_goal(self, spec: GoalSpec, *, quick_mode: bool = False) -> Goal:
        goal = self.store.create_goal(
            spec, quick_mode=quick_mode, baseline_commit=git_commit(self.workspace)
        )
        self.store.create_checkpoint(
            goal.id,
            git_sha=git_commit(self.workspace),
            state={"phase": "planned", "spec_digest": self.store.get_revision(goal.id).contract_digest},
            next_actions=["claim ready tasks"],
        )
        return goal

    def _apply_policy(self, options: RunOptions) -> RunOptions:
        if not options.policy_name:
            return options
        active = next(
            (
                policy
                for policy in self.store.list_policies()
                if policy.name == options.policy_name and policy.active
            ),
            None,
        )
        if active is None:
            raise ValidationFailure(f"no active policy named {options.policy_name!r}")
        allowed = {"context_mode", "token_budget", "recovery", "reviewer"}
        unknown = set(active.config) - allowed
        if unknown:
            raise ValidationFailure(f"policy has unsupported runtime keys: {sorted(unknown)}")
        merged = RunOptions(**options.__dict__)
        if "context_mode" in active.config:
            merged.context_mode = str(active.config["context_mode"])
        if "token_budget" in active.config:
            merged.token_budget = int(active.config["token_budget"])
        if "recovery" in active.config:
            merged.recovery = bool(active.config["recovery"])
        if "reviewer" in active.config:
            merged.reviewer = bool(active.config["reviewer"])
        return merged

    def run(
        self,
        goal_id: str,
        *,
        adapter: AgentAdapter | None = None,
        options: RunOptions | None = None,
    ) -> RunReport:
        options = options or RunOptions()
        options = self._apply_policy(options)
        goal = self.store.get_goal(goal_id)
        spec = self.store.get_spec(goal_id)
        owned_adapter = adapter is None
        if adapter is None:
            if options.agent_name == "mock":
                adapter = MockAdapter()
            else:
                from forgeflow.agents import create_agent_adapter

                adapter = create_agent_adapter(options.agent_name)
        report = RunReport(
            goal_id=goal_id,
            goal_status=goal.status,
            tasks=[],
            policy=(
                {"name": options.policy_name}
                if options.policy_name
                else None
            ),
        )
        self.store.expire_leases(goal_id, actor="harness-startup")
        if goal.status in {GoalStatus.CANCELLED, GoalStatus.DELIVERED}:
            raise ValidationFailure(f"cannot run goal in status {goal.status}")
        if goal.status in {GoalStatus.PLANNED, GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.FAILED}:
            self.store.set_goal_status(goal_id, GoalStatus.RUNNING, reason="harness run")
        context_engine = ContextEngine(self.workspace)
        attempt_results: dict[str, TaskResult] = {}
        try:
            with self.telemetry.span(
                "goal.run",
                {
                    "forge.goal_id": goal_id,
                    "forge.agent": adapter.name,
                    "forge.context_mode": options.context_mode,
                    "forge.policy": options.policy_name,
                },
            ):
                processed = 0
                while processed < options.max_tasks:
                    ready = self.store.ready_tasks(goal_id)
                    if not ready:
                        break
                    task_key = ready[0].key
                    try:
                        claim = self.store.claim_task(
                            goal_id,
                            owner=f"{adapter.name}-{new_id('worker')}",
                            lease_seconds=options.lease_seconds,
                            task_key=task_key,
                            agent=adapter.name,
                        )
                    except Exception as exc:
                        report.errors.append(f"could not claim {task_key}: {exc}")
                        break
                    processed += 1
                    task_spec = next(item for item in spec.tasks if item.key == task_key)
                    result = self._run_claimed_task(
                        goal_id,
                        spec,
                        task_spec,
                        claim,
                        adapter,
                        options,
                        context_engine,
                    )
                    attempt_results[task_key] = result
                    report.tasks.append(result)
                    self._checkpoint(goal_id)
                    if result.recovery_retry and options.recovery:
                        # The ready frontier is re-read; retry is bounded by max_attempts.
                        continue
        finally:
            context_engine.close()
            if owned_adapter:
                adapter.close()
        # Re-check state after loop.
        goal = self.store.get_goal(goal_id)
        statuses = self.store.task_status_map(goal_id)
        if statuses and all(status == TaskStatus.VERIFIED for status in statuses.values()):
            if goal.status == GoalStatus.RUNNING:
                self.store.set_goal_status(
                    goal_id, GoalStatus.VERIFYING, reason="all tasks have verified evidence"
                )
        elif not self.store.ready_tasks(goal_id):
            if (
                any(status == TaskStatus.BLOCKED for status in statuses.values())
                or any(
                    status in {TaskStatus.PENDING, TaskStatus.INTERRUPTED, TaskStatus.UNKNOWN}
                    for status in statuses.values()
                )
            ) and goal.status == GoalStatus.RUNNING:
                self.store.set_goal_status(
                    goal_id, GoalStatus.BLOCKED, reason="ready frontier exhausted with blocked work"
                )
            elif any(status == TaskStatus.FAILED for status in statuses.values()) and (
                goal.status == GoalStatus.RUNNING
            ):
                self.store.set_goal_status(
                    goal_id, GoalStatus.FAILED, reason="ready frontier exhausted with failures"
                )
        report.goal_status = self.store.get_goal(goal_id).status
        report.context_tokens = sum(result.context_tokens for result in report.tasks)
        report.agent_cost_usd = sum(result.agent_cost_usd for result in report.tasks)
        self._checkpoint(goal_id)
        return report

    def _run_claimed_task(
        self,
        goal_id: str,
        spec: GoalSpec,
        task_spec: TaskSpec,
        claim: Claim,
        adapter: AgentAdapter,
        options: RunOptions,
        context_engine: ContextEngine,
    ) -> TaskResult:
        task = claim.task
        result = TaskResult(
            task_key=task.key,
            task_id=task.id,
            status=task.status,
            attempt_id=claim.attempt.id,
        )
        baseline = _workspace_state(self.workspace)
        attempt_base_head = git_commit(self.workspace)
        with self.telemetry.span(
            "task.run",
            {
                "forge.goal_id": goal_id,
                "forge.task_id": task.id,
                "forge.task_key": task.key,
                "forge.attempt_id": claim.attempt.id,
            },
        ) as task_state:
            context: ContextBundle | None = None
            context_text = ""
            try:
                if options.context_mode != "none":
                    context = context_engine.build(
                        spec,
                        task_spec,
                        token_budget=options.token_budget,
                        mode=options.context_mode,
                    )
                    context_text = context.to_prompt()
                    result.context_tokens = context.used_tokens
                    task_state["forge.context_tokens"] = context.used_tokens
            except Exception as exc:
                result.error = f"context build failed: {exc}"
                self._fail_agent_attempt(
                    goal_id, spec, task, claim, ErrorCategory.ENVIRONMENT, result.error
                )
                result.status = self.store.get_task(goal_id, task.id).status
                return result
            prompt = self._prompt(goal_id, spec, task_spec, context_text)
            run_id = new_id("agentrun")
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(
                    goal_id,
                    task.id,
                    claim.attempt.owner,
                    claim.lease_token,
                    options.lease_seconds,
                    heartbeat_stop,
                ),
                daemon=True,
            )
            heartbeat.start()
            try:
                with self.telemetry.span(
                    "agent.run",
                    {
                        "forge.goal_id": goal_id,
                        "forge.task_id": task.id,
                        "forge.agent": adapter.name,
                        "gen_ai.system": adapter.name,
                        "gen_ai.operation.name": "coding_task",
                        "forge.prompt_sha256": _sha(prompt),
                    },
                ) as agent_state:
                    adapter.start(run_id, self.workspace, task_spec)
                    disposition = adapter.submit(run_id, prompt)
                    settlement = adapter.wait_until_settled(
                        run_id, timeout_seconds=options.task_timeout_seconds
                    )
                    agent_state["forge.agent_disposition"] = disposition
                    agent_state["forge.agent_status"] = settlement.status.value
                    agent_state["gen_ai.usage.input_tokens"] = settlement.tokens
                    agent_state["forge.agent_cost_usd"] = settlement.cost_usd
                result.agent_claimed_success = settlement.success_claimed
                result.agent_cost_usd = settlement.cost_usd
                if settlement.status != AgentRunStatus.SETTLED:
                    if settlement.status == AgentRunStatus.EXTERNAL_BLOCKED:
                        category = ErrorCategory.EXTERNAL_BLOCKER
                    elif settlement.status in {AgentRunStatus.CRASHED, AgentRunStatus.CANCELLED}:
                        category = ErrorCategory.TIMEOUT
                    else:
                        category = None
                    result.error = settlement.error or f"agent ended {settlement.status}"
                    assessment = self.recovery.assess(
                        goal_id=goal_id,
                        task_id=task.id,
                        exit_code=None,
                        stdout=settlement.output,
                        stderr=settlement.error or "",
                        message=result.error,
                        declared=category,
                    )
                    self.recovery.record_attempt(
                        goal_id=goal_id,
                        task_id=task.id,
                        attempt_id=claim.attempt.id,
                        assessment=assessment,
                        failing_command="<agent run>",
                        evidence_ids=[],
                    )
                    self._fail_agent_attempt(
                        goal_id,
                        spec,
                        task,
                        claim,
                        assessment.category,
                        result.error,
                        retryable=assessment.retry_allowed and options.recovery,
                        cost_usd=settlement.cost_usd,
                        tokens=settlement.tokens,
                    )
                    result.status = self.store.get_task(goal_id, task.id).status
                    result.recovery_retry = result.status == TaskStatus.READY
                    return result
                # Agent self-report is only a claim; settle to VERIFYING first.
                self.store.settle_success(
                    goal_id,
                    task_id=task.id,
                    attempt_id=claim.attempt.id,
                    owner=claim.attempt.owner,
                    lease_token=claim.lease_token,
                    cost_usd=settlement.cost_usd,
                    tokens=settlement.tokens,
                    metadata={
                        "agent_status": settlement.status.value,
                        "self_reported_success": settlement.success_claimed,
                        "agent_output_present": bool(settlement.output),
                    },
                )
                if not settlement.success_claimed:
                    result.error = settlement.error or "agent did not claim success"
                    self._fail_verification(
                        goal_id,
                        spec,
                        task,
                        claim,
                        result,
                        ErrorCategory.CONTRACT,
                        result.error,
                        retryable=True,
                    )
                    return result
                if not options.verify:
                    result.status = TaskStatus.VERIFYING
                    return result
                verification = self._verify_task_attempt(
                    goal_id,
                    spec,
                    task_spec,
                    claim,
                    reviewer=options.reviewer,
                    baseline=baseline,
                    base_head=attempt_base_head,
                )
                result.evidence_ids = verification.evidence_ids
                result.gates = {
                    _gate_key(evaluation): evaluation.status.value
                    for evaluation in verification.evaluations
                }
                if verification.verified:
                    result.status = TaskStatus.VERIFIED
                    result.verified = True
                    task_state["forge.verified"] = True
                    task_state["forge.evidence_count"] = len(verification.evidence_ids)
                else:
                    result.false_completion = True
                    result.error = verification.error or "independent verification failed"
                    assessment = self.recovery.assess(
                        goal_id=goal_id,
                        task_id=task.id,
                        exit_code=1,
                        stderr=verification.error or "",
                        declared=verification.category,
                    )
                    self.recovery.record_attempt(
                        goal_id=goal_id,
                        task_id=task.id,
                        attempt_id=claim.attempt.id,
                        assessment=assessment,
                        failing_command=verification.failing_command,
                        evidence_ids=verification.evidence_ids,
                    )
                    if verification.blocked:
                        self.store.block_task(
                            goal_id,
                            task.id,
                            verification.error or "verification blocked",
                            actor="gate-engine",
                        )
                        result.status = TaskStatus.BLOCKED
                        result.recovery_retry = False
                        return result
                    self._fail_verification(
                        goal_id,
                        spec,
                        task,
                        claim,
                        result,
                        assessment.category,
                        verification.error or "verification failed",
                        retryable=assessment.retry_allowed and options.recovery,
                    )
                    result.status = self.store.get_task(goal_id, task.id).status
                    result.recovery_retry = result.status == TaskStatus.READY
                return result
            except Exception as exc:
                result.error = f"harness error: {exc}"
                declared = (
                    ErrorCategory.EXTERNAL_BLOCKER
                    if isinstance(exc, (ExternalBlockedError, DependencyUnavailableError))
                    else None
                )
                assessment = self.recovery.assess(
                    goal_id=goal_id,
                    task_id=task.id,
                    exit_code=None,
                    stderr=result.error,
                    message=result.error,
                    declared=declared,
                )
                try:
                    self.recovery.record_attempt(
                        goal_id=goal_id,
                        task_id=task.id,
                        attempt_id=claim.attempt.id,
                        assessment=assessment,
                        failing_command="<harness/agent>",
                        evidence_ids=[],
                    )
                except Exception:
                    pass
                try:
                    current = self.store.get_task(goal_id, task.id)
                    if current.status in {TaskStatus.CLAIMED, TaskStatus.RUNNING}:
                        self._fail_agent_attempt(
                            goal_id,
                            spec,
                            task,
                            claim,
                            assessment.category,
                            result.error,
                            retryable=assessment.retry_allowed,
                        )
                    elif current.status == TaskStatus.VERIFYING:
                        self.store.fail_verification(
                            goal_id,
                            task_id=task.id,
                            attempt_id=claim.attempt.id,
                            reason=result.error,
                            category=assessment.category,
                            retryable=assessment.retry_allowed,
                        )
                    elif current.status == TaskStatus.VERIFIED:
                        result.verified = True
                except Exception:
                    pass
                result.status = self.store.get_task(goal_id, task.id).status
                result.recovery_retry = result.status == TaskStatus.READY
                return result
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=5)

    def _heartbeat_loop(
        self,
        goal_id: str,
        task_id: str,
        owner: str,
        lease_token: str,
        lease_seconds: float,
        stop: threading.Event,
    ) -> None:
        interval = max(5.0, lease_seconds / 3)
        while not stop.wait(interval):
            try:
                self.store.heartbeat(
                    goal_id,
                    task_id=task_id,
                    owner=owner,
                    lease_token=lease_token,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                return

    def _prompt(
        self, goal_id: str, spec: GoalSpec, task_spec: TaskSpec, context_text: str
    ) -> str:
        acceptance = "\n".join(
            f"- [{item.id}] {item.statement} (gate: {item.gate.value})"
            for item in spec.acceptance
            if not task_spec.acceptance_ids or item.id in task_spec.acceptance_ids
        )
        task = self.store.get_task(goal_id, task_spec.key)
        recovery_section = ""
        if task.last_error:
            recovery_section = (
                "\nPrevious attempt failure (the harness will rerun verification):\n"
                f"```\n{task.last_error[-4000:]}\n```\n"
            )
        return (
            "You are executing one task for the ForgeFlow 2.0 verifiable coding harness.\n"
            f"Goal: {spec.title}\n{spec.objective}\n\n"
            f"Attempt: {task.attempts_count} of {task.max_attempts}\n"
            f"Task: {task_spec.title}\n{task_spec.description}\n"
            f"Owned paths: {', '.join(task_spec.owned_paths) or '(unspecified; keep edits minimal)'}\n"
            f"Required gates: {', '.join(gate.value for gate in task_spec.gates) or '(none configured)'}\n"
            f"Acceptance contract:\n{acceptance}\n"
            "Constraints:\n"
            + "\n".join(f"- {item}" for item in spec.constraints)
            + "\n"
            "Rules: make code changes in the workspace; do not modify protected verifier files "
            "or delete/weaken tests; do not use network or irreversible commands; do not claim "
            "that tests passed as proof. Your self-report is not evidence. The harness runs "
            "independent commands and binds outputs to source fingerprints.\n"
            f"{recovery_section}"
            f"{context_text}"
        )

    def _select_commands(
        self, spec: GoalSpec, task_spec: TaskSpec
    ) -> list[Any]:
        effective_ids = set(task_spec.acceptance_ids) or {
            item.id for item in spec.acceptance if item.gate in set(task_spec.gates)
        }
        selected = []
        for command in spec.verifier.commands:
            if command.gate not in task_spec.gates:
                continue
            if command.scope and not set(command.scope) & effective_ids:
                continue
            selected.append(command)
        return selected

    def _verify_task_attempt(
        self,
        goal_id: str,
        spec: GoalSpec,
        task_spec: TaskSpec,
        claim: Claim,
        *,
        reviewer: bool = False,
        baseline: dict[str, str] | None = None,
        base_head: str = "NO_GIT",
    ) -> TaskVerification:
        if not task_spec.gates:
            return TaskVerification(
                False,
                [],
                [],
                "task has no independent verification gates; refusing to auto-verify",
                ErrorCategory.CONTRACT,
                blocked=True,
            )
        goal = self.store.get_goal(goal_id)
        trusted_ref = goal.baseline_commit
        if trusted_ref == "UNKNOWN":
            return TaskVerification(
                False,
                [],
                [],
                "goal has no immutable baseline commit for protected verifier files; "
                "recreate the goal before verification",
                ErrorCategory.CONTRACT,
                "<baseline-guard>",
                blocked=True,
            )
        if not protected_files_match_ref(
            self.workspace, spec.verifier.protected_paths, trusted_ref
        ):
            return TaskVerification(
                False,
                [],
                [],
                "agent modified protected verifier files relative to the goal baseline; "
                "restore them before verification",
                ErrorCategory.CONTRACT,
                "<protected-path-guard>",
                blocked=True,
            )
        ownership_violations = _ownership_violations(
            self.workspace, task_spec.owned_paths, baseline or {}, base_head
        )
        if ownership_violations:
            return TaskVerification(
                False,
                [],
                [],
                "agent modified paths outside task ownership: "
                + ", ".join(ownership_violations),
                ErrorCategory.CONTRACT,
                "<ownership-guard>",
                blocked=True,
            )
        try:
            self.recovery.guard_repair_diff()
        except RecoveryPolicyError as exc:
            return TaskVerification(
                False,
                [],
                [],
                f"repair policy rejected the change: {exc}",
                ErrorCategory.CONTRACT,
                "<repair-diff-guard>",
                blocked=True,
            )
        if reviewer:
            from forgeflow.verification.reviewer import StaticReviewer

            review = StaticReviewer().review(self.workspace)
            if review.blocked:
                return TaskVerification(
                    False,
                    [],
                    [],
                    "static reviewer rejected high-risk change: "
                    + "; ".join(
                        f"{finding.rule}@{finding.path}" for finding in review.findings
                    ),
                    ErrorCategory.CONTRACT,
                    "<static-reviewer>",
                    blocked=True,
                )
        commands = self._select_commands(spec, task_spec)
        if not commands:
            return TaskVerification(
                False,
                [],
                [],
                "no trusted verifier command is configured for this task's gates",
                ErrorCategory.CONTRACT,
                blocked=True,
            )
        covered_gates = {command.gate for command in commands}
        missing = set(task_spec.gates) - covered_gates
        if missing:
            return TaskVerification(
                False,
                [],
                [],
                f"no verifier command covers required gates: {sorted(g.value for g in missing)}",
                ErrorCategory.CONTRACT,
                blocked=True,
            )
        binding = capture_binding(self.workspace, spec.verifier.protected_paths)
        recorder = EvidenceRecorder(self.store, self.workspace, spec)
        evidence_ids: list[str] = []
        failures: list[str] = []
        failing_command = ""
        with self.telemetry.span(
            "verifier.run",
            {
                "forge.goal_id": goal_id,
                "forge.task_id": claim.task.id,
                "forge.attempt_id": claim.attempt.id,
                "forge.command_count": len(commands),
            },
        ) as verifier_state:
            for command in commands:
                effective_scope = list(command.scope) or (
                    list(task_spec.acceptance_ids)
                    or [
                        item.id
                        for item in spec.acceptance
                        if item.gate == command.gate
                    ]
                )
                if not effective_scope:
                    failures.append(f"{command.id}: no acceptance scope")
                    failing_command = " ".join(command.argv)
                    continue
                with self.telemetry.span(
                    "tool.run",
                    {
                        "forge.goal_id": goal_id,
                        "forge.task_id": claim.task.id,
                        "forge.command_id": command.id,
                        "forge.gate": command.gate.value,
                        "gen_ai.tool.name": command.argv[0],
                        "forge.argv": command.argv,
                    },
                ) as tool_state:
                    try:
                        result = self.runner.run(
                            RunRequest(
                                argv=list(command.argv),
                                cwd=(self.workspace / command.cwd).resolve(),
                                purpose=f"verifier:{command.id}",
                                timeout_seconds=command.timeout_seconds,
                                environment=command.environment,
                                network=spec.verifier.network,
                                writes=True,
                            )
                        )
                    except PolicyDeniedError as exc:
                        failures.append(f"{command.id}: policy denied: {exc}")
                        failing_command = " ".join(command.argv)
                        tool_state["forge.policy_denied"] = True
                        continue
                    except ForgeFlowError as exc:
                        failures.append(f"{command.id}: runner error: {exc}")
                        failing_command = " ".join(command.argv)
                        continue
                    tool_state["forge.exit_code"] = result.exit_code
                    tool_state["forge.timed_out"] = result.timed_out
                    evidence = recorder.record_command(
                        goal_id=goal_id,
                        revision=self.store.get_goal(goal_id).current_revision,
                        gate=command.gate,
                        result=result,
                        scope=effective_scope,
                        task_id=claim.task.id,
                        attempt_id=claim.attempt.id,
                        trust=TrustLevel.TRUSTED_RUNNER,
                        binding=binding,
                    )
                    evidence_ids.append(evidence.id)
                    if not result.succeeded:
                        failures.append(
                            f"{command.id}: exit={result.exit_code} timed_out={result.timed_out}\n"
                            f"{(result.stderr_excerpt or result.stdout_excerpt)[-3000:]}"
                        )
                        if not failing_command:
                            failing_command = " ".join(command.argv)
            engine = GateEngine(self.store, self.workspace, spec, binding=binding)
            evaluations: list[GateEvaluation] = []
            for gate in task_spec.gates:
                criteria = [
                    item
                    for item in spec.acceptance
                    if item.gate == gate
                    and (
                        not task_spec.acceptance_ids
                        or item.id in task_spec.acceptance_ids
                    )
                ]
                if criteria:
                    evaluations.extend(
                        engine.evaluate(goal_id, gate, criterion_id=item.id)
                        for item in criteria
                    )
                else:
                    evaluations.append(engine.evaluate(goal_id, gate))
            verifier_state["forge.evidence_count"] = len(evidence_ids)
            verifier_state["forge.failures"] = len(failures)
            gate_pass = all(
                any(
                    evaluation.gate == gate and evaluation.status == GateStatus.PASS
                    for evaluation in evaluations
                )
                for gate in task_spec.gates
            )
            if not gate_pass:
                failed_gate = next(
                    (
                        evaluation
                        for evaluation in evaluations
                        if evaluation.status != GateStatus.PASS
                    ),
                    None,
                )
                reason = (
                    failed_gate.reason
                    if failed_gate
                    else failures[0]
                    if failures
                    else "required gate did not pass"
                )
                return TaskVerification(
                    False,
                    evidence_ids,
                    evaluations,
                    reason,
                    ErrorCategory.TEST,
                    failing_command,
                )
            try:
                self.store.mark_task_verified(
                    goal_id,
                    task_id=claim.task.id,
                    attempt_id=claim.attempt.id,
                    evidence_ids=evidence_ids,
                    evaluated_gates=evaluations,
                )
            except Exception as exc:
                return TaskVerification(
                    False,
                    evidence_ids,
                    evaluations,
                    f"verification state transition failed: {exc}",
                    ErrorCategory.CONTRACT,
                    failing_command,
                )
            verifier_state["forge.verified"] = True
        return TaskVerification(True, evidence_ids, evaluations)

    def _fail_verification(
        self,
        goal_id: str,
        spec: GoalSpec,
        task: Task,
        claim: Claim,
        result: TaskResult,
        category: ErrorCategory,
        reason: str,
        *,
        retryable: bool,
    ) -> None:
        updated = self.store.fail_verification(
            goal_id,
            task_id=task.id,
            attempt_id=claim.attempt.id,
            reason=reason,
            category=category,
            retryable=retryable,
        )
        result.status = updated.status
        result.recovery_retry = updated.status == TaskStatus.READY

    def _fail_agent_attempt(
        self,
        goal_id: str,
        spec: GoalSpec,
        task: Task,
        claim: Claim,
        category: ErrorCategory,
        reason: str,
        *,
        retryable: bool = True,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> None:
        self.store.settle_failure(
            goal_id,
            task_id=task.id,
            attempt_id=claim.attempt.id,
            owner=claim.attempt.owner,
            lease_token=claim.lease_token,
            error_category=category,
            error_message=reason,
            retryable=retryable,
            cost_usd=cost_usd,
            tokens=tokens,
        )

    def verify_goal(
        self,
        goal_id: str,
        *,
        clean_room: bool = False,
        command_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        spec = self.store.get_spec(goal_id)
        goal = self.store.get_goal(goal_id)
        commands = spec.verifier.commands
        if command_ids:
            commands = [command for command in commands if command.id in set(command_ids)]
        if not commands:
            return {"status": "BLOCKED", "reason": "no verifier commands configured", "gates": {}}
        trusted_ref = goal.baseline_commit
        if trusted_ref == "UNKNOWN":
            return {
                "status": "BLOCKED",
                "reason": "goal has no immutable baseline commit; recreate the goal",
                "gates": {},
            }
        if not protected_files_match_ref(
            self.workspace, spec.verifier.protected_paths, trusted_ref
        ):
            return {
                "status": "BLOCKED",
                "reason": "protected verifier files differ from the immutable goal baseline",
                "gates": {},
            }
        binding = capture_binding(self.workspace, spec.verifier.protected_paths)
        evidence_ids: list[str] = []
        failures: list[str] = []
        cleanroom_report: CleanRoomReport | None = None
        with self.telemetry.span(
            "verification.goal",
            {
                "forge.goal_id": goal_id,
                "forge.clean_room": clean_room,
                "forge.command_count": len(commands),
            },
        ) as span_state:
            if clean_room:
                verifier = CleanRoomVerifier(self.store, self.workspace, spec)
                cleanroom_report = verifier.verify(goal_id, command_ids=command_ids)
                evidence_ids.extend(cleanroom_report.evidence_ids)
                failures.extend(cleanroom_report.errors)
            else:
                recorder = EvidenceRecorder(self.store, self.workspace, spec)
                for command in commands:
                    scope = list(command.scope) or [
                        item.id for item in spec.acceptance if item.gate == command.gate
                    ]
                    if not scope:
                        failures.append(f"{command.id}: no contract scope")
                        continue
                    try:
                        result = self.runner.run(
                            RunRequest(
                                argv=list(command.argv),
                                cwd=(self.workspace / command.cwd).resolve(),
                                purpose=f"goal-verifier:{command.id}",
                                timeout_seconds=command.timeout_seconds,
                                environment=command.environment,
                                network=spec.verifier.network,
                                writes=True,
                            )
                        )
                    except ForgeFlowError as exc:
                        failures.append(f"{command.id}: {exc}")
                        continue
                    evidence = recorder.record_command(
                        goal_id=goal_id,
                        revision=goal.current_revision,
                        gate=command.gate,
                        result=result,
                        scope=scope,
                        binding=binding,
                    )
                    evidence_ids.append(evidence.id)
                    if not result.succeeded:
                        failures.append(
                            f"{command.id}: exit={result.exit_code} timed_out={result.timed_out}"
                        )
            engine = GateEngine(self.store, self.workspace, spec, binding=binding)
            trust_filter = {TrustLevel.CLEAN_ROOM} if clean_room else None
            evaluations: list[GateEvaluation] = []
            for criterion in spec.acceptance:
                evaluations.append(
                    engine.evaluate(
                        goal_id,
                        criterion.gate,
                        criterion_id=criterion.id,
                        allowed_trust=trust_filter,
                    )
                )
            gates = sorted({criterion.gate for criterion in spec.acceptance}, key=lambda g: g.value)
            for gate in gates:
                evaluations.append(
                    engine.evaluate(goal_id, gate, allowed_trust=trust_filter)
                )
            statuses = {
                gate.value: engine.evaluate(
                    goal_id, gate, allowed_trust=trust_filter
                ).status.value
                for gate in gates
            }
            required = [item for item in spec.acceptance if item.required]
            all_pass = all(
                any(
                    evaluation.gate == item.gate
                    and evaluation.criterion_id == item.id
                    and evaluation.status == GateStatus.PASS
                    for evaluation in evaluations
                )
                for item in required
            )
            all_tasks_verified = (
                bool(self.store.list_tasks(goal_id))
                and all(
                    task.status == TaskStatus.VERIFIED
                    for task in self.store.list_tasks(goal_id)
                )
            )
            cleanroom_ok = (
                cleanroom_report is not None
                and cleanroom_report.success
                and bool(cleanroom_report.evidence_ids)
            )
            complete = (
                all_pass
                and all_tasks_verified
                and not failures
                and (not clean_room or cleanroom_ok)
            )
            if complete:
                self.store.set_goal_status(
                    goal_id,
                    GoalStatus.VERIFYING,
                    reason="all required contract gates have current trusted evidence",
                    actor="gate-engine",
                )
            span_state["forge.gates"] = statuses
            span_state["forge.complete"] = complete
        return {
            "status": "PASS" if complete else "FAIL",
            "gates": statuses,
            "evidence_ids": evidence_ids,
            "failures": failures,
            "cleanroom": cleanroom_report.as_dict() if cleanroom_report else None,
        }

    def status(self, goal_id: str) -> StatusReport:
        goal = self.store.get_goal(goal_id)
        spec = self.store.get_spec(goal_id)
        GateEngine(
            self.store,
            self.workspace,
            spec,
            binding=capture_binding(self.workspace, spec.verifier.protected_paths),
        ).refresh_staleness(goal_id)
        tasks = self.store.list_tasks(goal_id)
        latest = {
            evaluation.gate.value: evaluation.status
            for evaluation in self.store.latest_gates(goal_id)
            if evaluation.criterion_id is None
        }
        counts = Counter(task.status.value for task in tasks)
        ready = self.store.ready_tasks(goal_id)
        next_actions: list[str] = []
        if goal.status.value in {"PLANNED", "RUNNING"}:
            next_actions.extend(f"claim {task.key}" for task in ready)
        next_actions.extend(
            f"recover {task.key}" for task in tasks if task.status == TaskStatus.INTERRUPTED
        )
        next_actions.extend(
            f"manual review {task.key}" for task in tasks if task.status == TaskStatus.BLOCKED
        )
        next_actions.extend(
            f"verify {task.key}" for task in tasks if task.status == TaskStatus.VERIFYING
        )
        return StatusReport(
            goal_id=goal.id,
            title=goal.title,
            status=goal.status,
            revision=goal.current_revision,
            tasks=dict(counts),
            gates=latest,
            blocked=[task.key for task in tasks if task.status == TaskStatus.BLOCKED],
            next_actions=next_actions,
            git_sha=git_commit(self.workspace),
            source_fingerprint=source_fingerprint(
                self.workspace, protected_paths=spec.verifier.protected_paths
            ),
        )

    def _checkpoint(self, goal_id: str) -> None:
        report = self.status(goal_id)
        next_actions = report.next_actions or (
            ["run forge verify"] if report.status == GoalStatus.VERIFYING else ["inspect status"]
        )
        self.store.create_checkpoint(
            goal_id,
            git_sha=report.git_sha,
            state={
                "status": report.status.value,
                "tasks": {key: value.value for key, value in self.store.task_status_map(goal_id).items()},
                "gates": {gate: status.value for gate, status in report.gates.items()},
                "source_fingerprint": report.source_fingerprint,
                "env_digest": environment_digest(self.workspace),
                "diff_hash": diff_fingerprint(self.workspace),
            },
            next_actions=next_actions,
        )

    def resume(self, goal_id: str) -> dict[str, Any]:
        expired = self.store.expire_leases(goal_id, actor="resume")
        goal = self.store.resume_goal(goal_id)
        ready = self.store.ready_tasks(goal_id)
        checkpoint = self.store.latest_checkpoint(goal_id)
        recovery = self.recover(goal_id)
        next_actions = [f"claim {task.key}" for task in ready] + recovery["actions"]
        return {
            "goal_id": goal_id,
            "status": goal.status.value,
            "interrupted": [task.key for task in expired],
            "ready": [task.key for task in ready],
            "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
            "next_actions": next_actions,
            "manual_actions": recovery["actions"],
        }

    def recover(self, goal_id: str) -> dict[str, Any]:
        expired = self.store.expire_leases(goal_id, actor="recover")
        tasks = self.store.list_tasks(goal_id)
        blocked = [task for task in tasks if task.status == TaskStatus.BLOCKED]
        unknown = [task for task in tasks if task.status == TaskStatus.UNKNOWN]
        actions = []
        for task in blocked:
            spec_task = next(
                (item for item in self.store.get_spec(goal_id).tasks if item.key == task.key), None
            )
            if spec_task and spec_task.side_effecting:
                actions.append(
                    f"{task.key}: manual reconciliation required — reconcile external side "
                    f"effect using idempotency key {spec_task.idempotency_key!r}, then "
                    "explicitly requeue"
                )
            else:
                actions.append(f"{task.key}: resolve blocker and resume")
        for task in unknown:
            actions.append(f"{task.key}: manual outcome confirmation required")
        return {
            "goal_id": goal_id,
            "expired_leases": [task.key for task in expired],
            "blocked": [task.key for task in blocked],
            "unknown": [task.key for task in unknown],
            "actions": actions,
        }


def _gate_key(evaluation: GateEvaluation) -> str:
    return (
        f"{evaluation.gate.value}:{evaluation.criterion_id}"
        if evaluation.criterion_id
        else evaluation.gate.value
    )


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _ownership_violations(
    workspace: Path,
    owned_paths: list[str],
    baseline: dict[str, str],
    base_head: str,
) -> list[str]:
    if not owned_paths:
        return []
    import fnmatch
    import subprocess

    current = _workspace_state(workspace)
    changed = {
        path
        for path, digest in current.items()
        if baseline.get(path) != digest
    }
    changed |= {
        path for path in baseline if path not in current
    }
    if base_head not in {"NO_GIT", "UNBORN", "UNKNOWN"}:
        committed = subprocess.run(
            ["git", "diff", "--name-only", base_head, "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if committed.returncode == 0:
            changed.update(
                path.strip().replace("\\", "/")
                for path in committed.stdout.splitlines()
                if path.strip()
            )
    violations: list[str] = []
    for path in sorted(changed):
        if not any(
            fnmatch.fnmatch(path, pattern.replace("\\", "/").rstrip("/"))
            or path == pattern.replace("\\", "/").rstrip("/")
            or path.startswith(pattern.replace("\\", "/").rstrip("/") + "/")
            for pattern in owned_paths
        ):
            violations.append(path)
    return violations


def _workspace_state(workspace: Path) -> dict[str, str]:
    """Hash all dirty/untracked paths so agent changes can be isolated from user edits."""
    import subprocess

    from forgeflow.verification.fingerprint import _normalized_digest

    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return {}
    state: dict[str, str] = {}
    for line in result.stdout.splitlines():
        path = line[3:].strip().split(" -> ")[-1].replace("\\", "/")
        if not path:
            continue
        if path.startswith((".forge/", "evidence/runs/", "__pycache__/", ".pytest_cache/")):
            continue
        if "/__pycache__/" in path or "/.pytest_cache/" in path:
            continue
        target = workspace / path
        try:
            state[path] = _normalized_digest(target) if target.is_file() else "<deleted>"
        except OSError:
            state[path] = "<unreadable>"
    return state
