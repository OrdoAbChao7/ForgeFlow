from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow.verification.fingerprint import git


@dataclass
class ReviewFinding:
    severity: str
    path: str
    rule: str
    message: str


@dataclass
class ReviewReport:
    findings: list[ReviewFinding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(finding.severity == "high" for finding in self.findings)

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "findings": [finding.__dict__ for finding in self.findings],
        }


class StaticReviewer:
    """Deterministic reviewer for risky patterns and test weakening.

    This is not an LLM reviewer and does not grant verification authority. A second
    agent may be plugged in with the same report contract, but must remain advisory.
    """

    HIGH_RULES = (
        (
            "test-skip",
            re.compile(r"pytest\.mark\.(skip|xfail)|@unittest\.skip|test\.skip\("),
            "test skip/xfail added",
        ),
        (
            "assert-true",
            re.compile(r"assert\s+True\b"),
            "vacuous assertion added",
        ),
        (
            "dangerous-exec",
            re.compile(r"\b(eval|exec)\s*\(|os\.system\(|shell\s*=\s*True"),
            "dangerous execution added",
        ),
        (
            "test-delete",
            re.compile(r"^deleted file mode", re.MULTILINE),
            "file deletion in diff",
        ),
    )
    MEDIUM_RULES = (
        (
            "type-ignore",
            re.compile(r"#\s*type:\s*ignore|#\s*noqa"),
            "static-check suppression added",
        ),
        (
            "network-call",
            re.compile(r"\b(requests\.(get|post)|urllib\.request|httpx\.)"),
            "network call added",
        ),
    )

    def review(self, root: str | Path) -> ReviewReport:
        root_path = Path(root).resolve()
        diff = git(root_path, "diff", "--no-color", "HEAD", check=False).stdout
        findings: list[ReviewFinding] = []
        current_path = "<unknown>"
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current_path = line[6:]
                continue
            if line.startswith("--- ") or line.startswith("+++"):
                continue
            if not line.startswith("+"):
                continue
            added = line[1:]
            for rule, pattern, message in self.HIGH_RULES:
                if pattern.search(added):
                    if rule == "test-delete":
                        continue
                    findings.append(
                        ReviewFinding("high", current_path, rule, message)
                    )
            for rule, pattern, message in self.MEDIUM_RULES:
                if pattern.search(added):
                    findings.append(
                        ReviewFinding("medium", current_path, rule, message)
                    )
        if re.search(r"^deleted file mode", diff, re.MULTILINE):
            for block in re.split(r"^diff --git ", diff, flags=re.MULTILINE):
                if "deleted file mode" in block and re.search(
                    r"(^|/)tests?/|test_.*\.py$", block
                ):
                    path = block.splitlines()[0] if block else "<unknown>"
                    findings.append(
                        ReviewFinding("high", path, "test-delete", "test file deleted")
                    )
        untracked = git(root_path, "ls-files", "--others", "--exclude-standard", check=False)
        for relative in untracked.stdout.splitlines():
            try:
                text = (root_path / relative).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for rule, pattern, message in self.HIGH_RULES:
                if pattern.search(text):
                    findings.append(ReviewFinding("high", relative, rule, message))
        # De-duplicate stable findings.
        seen = set()
        unique = []
        for finding in findings:
            key = (finding.severity, finding.path, finding.rule)
            if key not in seen:
                seen.add(key)
                unique.append(finding)
        return ReviewReport(unique)
