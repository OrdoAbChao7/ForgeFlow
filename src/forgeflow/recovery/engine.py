from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow.domain.enums import ErrorCategory, EventType
from forgeflow.domain.errors import ForgeFlowError
from forgeflow.state.store import Store


class RecoveryPolicyError(ForgeFlowError):
    code = "RECOVERY_POLICY_DENIED"


_RULES: list[tuple[ErrorCategory, tuple[str, ...]]] = [
    (
        ErrorCategory.EXTERNAL_BLOCKER,
        (
            "no module named",
            "modulenotfounderror",
            "could not find a version",
            "network is unreachable",
            "connection refused",
            "name or service not known",
            "temporary failure in name resolution",
            "permission denied",
            "access is denied",
            "no space left",
            "out of memory",
            "cannot allocate memory",
        ),
    ),
    (ErrorCategory.TIMEOUT, ("timed out", "timeout", "deadline exceeded")),
    (ErrorCategory.COMPILE, ("syntaxerror", "indentationerror", "compile error")),
    (ErrorCategory.TYPE, ("typeerror", "mypy", "pyright", "type error")),
    (ErrorCategory.LINT, ("ruff", "flake8", "eslint", "lint")),
    (ErrorCategory.DEPENDENCY, ("dependency", "cannot resolve", "version conflict", "uv.lock")),
    (ErrorCategory.CONTRACT, ("contract", "schema mismatch", "acceptance", "gate failed")),
    (ErrorCategory.TEST, ("assertionerror", "failed", "pytest", "test failure", "e2e")),
    (ErrorCategory.ENVIRONMENT, ("environment", "virtualenv", "python version")),
    (ErrorCategory.NETWORK, ("http status", "ssl", "proxy")),
    (ErrorCategory.RESOURCE, ("memory", "disk", "resource")),
    (ErrorCategory.PERMISSION, ("forbidden", "unauthorized", "not permitted")),
]


class FailureClassifier:
    def classify(
        self,
        *,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        declared: ErrorCategory | None = None,
        message: str = "",
    ) -> ErrorCategory:
        if declared:
            return declared
        haystack = f"{message}\n{stderr}\n{stdout}".lower()
        for category, patterns in _RULES:
            if any(pattern in haystack for pattern in patterns):
                return category
        if exit_code == 124:
            return ErrorCategory.TIMEOUT
        if exit_code not in (0, None):
            return ErrorCategory.TEST
        return ErrorCategory.UNKNOWN

    def signature(self, category: ErrorCategory, text: str) -> str:
        normalized = text.lower()
        normalized = re.sub(r"0x[0-9a-f]+", "0xADDR", normalized)
        normalized = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s|sec|seconds)\b", "TIME", normalized)
        normalized = re.sub(r"line \d+", "line N", normalized)
        normalized = re.sub(r"[A-Za-z]:\\[^\s:]+|/[^\s:]+", "PATH", normalized)
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        core = "\n".join(lines[:20])
        return hashlib.sha256(f"{category.value}\n{core}".encode()).hexdigest()[:24]


@dataclass
class FailureAssessment:
    category: ErrorCategory
    signature: str
    excerpt: str
    retry_allowed: bool
    reason: str
    previous_same_error: int = 0
    history: list[dict] = field(default_factory=list)


@dataclass
class RepairDiff:
    changed_files: list[str]
    added_lines: int
    removed_lines: int
    deleted_test_files: list[str]

    @property
    def ok(self) -> bool:
        return not self.deleted_test_files

    @property
    def size(self) -> int:
        return self.added_lines + self.removed_lines


class RecoveryEngine:
    """Bounded, rule-based failure assessment and repair-diff guard.

    An LLM debugger can implement the same protocol, but it never decides PASS and
    cannot skip the verifier. Deleting tests or weakening assertions is rejected.
    """

    MAX_SAME_ERROR = 2
    MAX_REPAIR_FILES = 20
    MAX_REPAIR_LINES = 2000

    def __init__(self, store: Store, root: str | Path) -> None:
        self.store = store
        self.root = Path(root).resolve()
        self.classifier = FailureClassifier()

    def assess(
        self,
        *,
        goal_id: str,
        task_id: str,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        message: str = "",
        declared: ErrorCategory | None = None,
    ) -> FailureAssessment:
        category = self.classifier.classify(
            exit_code=exit_code, stdout=stdout, stderr=stderr, message=message, declared=declared
        )
        signature = self.classifier.signature(category, f"{message}\n{stderr}\n{stdout}")
        history = self._history(goal_id, task_id)
        previous = sum(
            1
            for item in history
            if item.get("signature") == signature
            and item.get("category") == category.value
        )
        excerpt = (stderr or stdout or message or "")[-6000:]
        if category == ErrorCategory.EXTERNAL_BLOCKER:
            return FailureAssessment(
                category,
                signature,
                excerpt,
                False,
                "external blocker requires human intervention; independent tasks may continue",
                previous,
                history,
            )
        if category == ErrorCategory.PERMISSION:
            return FailureAssessment(
                category,
                signature,
                excerpt,
                False,
                "permission failure is not auto-retried",
                previous,
                history,
            )
        if previous >= self.MAX_SAME_ERROR:
            return FailureAssessment(
                category,
                signature,
                excerpt,
                False,
                f"same failure signature observed {previous} times; circuit breaker opened",
                previous,
                history,
            )
        return FailureAssessment(
            category,
            signature,
            excerpt,
            True,
            "retry allowed within bounded policy",
            previous,
            history,
        )

    def record_attempt(
        self,
        *,
        goal_id: str,
        task_id: str,
        attempt_id: str,
        assessment: FailureAssessment,
        failing_command: str,
        evidence_ids: list[str],
    ) -> None:
        self.store.append_event(
            goal_id,
            EventType.RECOVERY_ATTEMPTED,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "category": assessment.category.value,
                "signature": assessment.signature,
                "reason": assessment.reason,
                "failing_command": failing_command,
                "evidence_ids": evidence_ids,
                "previous_same_error": assessment.previous_same_error,
            },
            actor="recovery-engine",
            task_id=task_id,
        )

    def _history(self, goal_id: str, task_id: str) -> list[dict]:
        return [
            event.payload
            for event in self.store.list_events(goal_id)
            if event.type == EventType.RECOVERY_ATTEMPTED
            and event.payload.get("task_id") == task_id
        ]

    def inspect_repair_diff(self) -> RepairDiff:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        changed: list[str] = []
        deleted_tests: list[str] = []
        added = 0
        removed = 0
        for line in diff.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_raw, remove_raw, path = parts[0], parts[1], parts[2]
            changed.append(path)
            if add_raw.isdigit():
                added += int(add_raw)
            if remove_raw.isdigit():
                removed += int(remove_raw)
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                path = line[3:].strip().split(" -> ")[-1]
                if path and path not in changed:
                    changed.append(path)
                if "D" in line[:2] and _looks_like_test(path):
                    deleted_tests.append(path)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for path in untracked.stdout.splitlines():
            try:
                added += (self.root / path).read_bytes().count(b"\n") + 1
            except OSError:
                pass
            if path not in changed:
                changed.append(path)
        return RepairDiff(changed, added, removed, deleted_tests)

    def guard_repair_diff(self) -> RepairDiff:
        diff = self.inspect_repair_diff()
        if diff.deleted_test_files:
            raise RecoveryPolicyError(
                "repair deleted test files; deleting tests is not an acceptable fix",
                details={"deleted_test_files": diff.deleted_test_files},
            )
        if len(diff.changed_files) > self.MAX_REPAIR_FILES or diff.size > self.MAX_REPAIR_LINES:
            raise RecoveryPolicyError(
                "repair diff exceeds bounded self-healing limits",
                details={
                    "changed_files": len(diff.changed_files),
                    "changed_lines": diff.size,
                    "limits": [self.MAX_REPAIR_FILES, self.MAX_REPAIR_LINES],
                },
            )
        return diff

    def repair_prompt(self, assessment: FailureAssessment, failing_command: str) -> str:
        return (
            "A previous attempt failed independent verification.\n"
            f"Failure category: {assessment.category.value}\n"
            f"Failing command: {failing_command}\n"
            f"Failure signature: {assessment.signature}\n"
            "Apply the smallest code change that fixes the failure. Do not delete or weaken "
            "tests, do not edit protected verifier files, and do not claim success. "
            "The harness will rerun failing and regression gates.\n"
            f"Captured failure:\n```\n{assessment.excerpt[-4000:]}\n```"
        )


def _looks_like_test(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized}"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
    )
