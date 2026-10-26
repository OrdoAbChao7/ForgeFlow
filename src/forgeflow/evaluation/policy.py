from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgeflow.domain.models import BenchmarkRecord, PolicyCandidate, new_id, utcnow
from forgeflow.state.store import Store


@dataclass(frozen=True)
class ValidationThresholds:
    min_runs: int = 4
    min_verified_success_rate: float = 0.9
    max_false_completion_rate: float = 0.05
    min_recovery_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_runs": self.min_runs,
            "min_verified_success_rate": self.min_verified_success_rate,
            "max_false_completion_rate": self.max_false_completion_rate,
            "min_recovery_rate": self.min_recovery_rate,
        }


class PolicyOptimizer:
    """Offline policy validation with explicit thresholds and reversible rollout.

    A policy is never declared effective from a single run or from an untested task.
    Activation is a separate explicit action; rollback restores the prior version.
    """

    def __init__(self, store: Store, thresholds: ValidationThresholds | None = None) -> None:
        self.store = store
        self.thresholds = thresholds or ValidationThresholds()

    def validate(
        self,
        config: dict[str, Any],
        *,
        benchmark: str | None = None,
    ) -> dict[str, Any]:
        records = self.store.list_benchmark_records(benchmark)
        selected = [record for record in records if _matches(record, config)]
        count = len(selected)
        verified = sum(1 for record in selected if record.verified_success)
        false_completion = sum(1 for record in selected if record.false_completion)
        recovery_records = [
            record for record in selected if record.metadata.get("case_id") == "recover_once"
        ]
        recovery_rate = (
            round(
                sum(1 for record in recovery_records if record.verified_success)
                / len(recovery_records),
                4,
            )
            if recovery_records
            else None
        )
        success_rate = verified / count if count else 0.0
        false_rate = false_completion / count if count else 0.0
        failures: list[str] = []
        if count < self.thresholds.min_runs:
            failures.append(f"insufficient runs: {count} < {self.thresholds.min_runs}")
        if success_rate < self.thresholds.min_verified_success_rate:
            failures.append(
                f"verified success rate {success_rate:.3f} < "
                f"{self.thresholds.min_verified_success_rate}"
            )
        if false_rate > self.thresholds.max_false_completion_rate:
            failures.append(
                f"false completion rate {false_rate:.3f} > "
                f"{self.thresholds.max_false_completion_rate}"
            )
        if (
            self.thresholds.min_recovery_rate is not None
            and recovery_rate is not None
            and recovery_rate < self.thresholds.min_recovery_rate
        ):
            failures.append(
                f"recovery rate {recovery_rate:.3f} < {self.thresholds.min_recovery_rate}"
            )
        return {
            "approved": not failures,
            "config": config,
            "runs": count,
            "verified_success_rate": round(success_rate, 4),
            "false_completion_rate": round(false_rate, 4),
            "recovery_rate": recovery_rate,
            "thresholds": self.thresholds.as_dict(),
            "failures": failures,
            "validated_at": utcnow().isoformat(),
            "benchmark_records": [record.id for record in selected],
        }

    def propose(
        self,
        name: str,
        config: dict[str, Any],
        *,
        benchmark: str | None = None,
    ) -> PolicyCandidate:
        if not config:
            raise ValueError("policy config must not be empty")
        validation = self.validate(config, benchmark=benchmark)
        existing = [item for item in self.store.list_policies() if item.name == name]
        version = max((item.version for item in existing), default=0) + 1
        candidate = PolicyCandidate(
            id=new_id("policy"),
            name=name,
            version=version,
            config=config,
            created_at=utcnow(),
            active=False,
            validation=validation,
        )
        self.store.add_policy_candidate(candidate)
        return candidate

    def activate(self, candidate_id: str) -> None:
        self.store.activate_policy(candidate_id)

    def rollback(self, name: str) -> PolicyCandidate | None:
        return self.store.rollback_policy(name)

    def active(self, name: str) -> PolicyCandidate | None:
        return next(
            (item for item in self.store.list_policies() if item.name == name and item.active),
            None,
        )


def _matches(record: BenchmarkRecord, config: dict[str, Any]) -> bool:
    if "context_mode" in config and record.context_mode != config["context_mode"]:
        return False
    if "recovery" in config and record.recovery_enabled != bool(config["recovery"]):
        return False
    if "reviewer" in config and record.reviewer_enabled != bool(config["reviewer"]):
        return False
    if "parallel" in config and record.parallel_enabled != bool(config["parallel"]):
        return False
    return not ("case_id" in config and record.case_id != config["case_id"])
