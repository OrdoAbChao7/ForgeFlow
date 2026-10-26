"""Domain exceptions with stable, machine-readable codes."""

from __future__ import annotations


class ForgeFlowError(Exception):
    code = "FORGEFLOW_ERROR"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(ForgeFlowError):
    code = "NOT_FOUND"


class ConflictError(ForgeFlowError):
    code = "CONFLICT"


class ValidationFailure(ForgeFlowError):
    code = "VALIDATION_FAILED"


class CycleDetectedError(ValidationFailure):
    code = "DEPENDENCY_CYCLE"


class LeaseExpiredError(ConflictError):
    code = "LEASE_EXPIRED"


class ClaimConflictError(ConflictError):
    code = "CLAIM_CONFLICT"


class PolicyDeniedError(ForgeFlowError):
    code = "POLICY_DENIED"


class PathEscapeError(PolicyDeniedError):
    code = "PATH_ESCAPE"


class VerificationError(ForgeFlowError):
    code = "VERIFICATION_FAILED"


class ExternalBlockedError(ForgeFlowError):
    code = "EXTERNAL_BLOCKED"


class DependencyUnavailableError(ExternalBlockedError):
    code = "DEPENDENCY_UNAVAILABLE"
