from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError


class ProviderErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CONFIG = "config"
    AUTH = "auth"
    CONTAINMENT = "containment"
    VALIDATION = "validation"
    INVALID_OUTPUT = "invalid_output"
    PROVIDER_BUSY = "provider_busy"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    WIRE_BUDGET = "wire_budget"
    UNKNOWN = "unknown"


# Timeout and rate limit back off inside one call. Unavailable, auth, config,
# and containment do not: the scheduler records them as explicit non-retryable
# stops so a missing helper or a provider outage cannot storm.
_RETRYABLE = frozenset(
    {
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.RATE_LIMIT,
    }
)

_STABLE_MESSAGES = {
    ProviderErrorCategory.TIMEOUT: "provider call exceeded deadline",
    ProviderErrorCategory.CANCELLED: "provider call cancelled",
    ProviderErrorCategory.CONFIG: "provider configuration rejected the call",
    ProviderErrorCategory.AUTH: "provider authentication rejected the call",
    ProviderErrorCategory.CONTAINMENT: "provider containment rejected the call",
    ProviderErrorCategory.VALIDATION: "provider output failed validation",
    ProviderErrorCategory.INVALID_OUTPUT: "provider output was rejected",
    ProviderErrorCategory.PROVIDER_BUSY: "provider busy",
    ProviderErrorCategory.RATE_LIMIT: "provider rate limit",
    ProviderErrorCategory.UNAVAILABLE: "provider unavailable",
    ProviderErrorCategory.WIRE_BUDGET: "serialized wire payload exceeded configured budget",
    ProviderErrorCategory.UNKNOWN: "provider call failed",
}


@dataclass(frozen=True)
class SanitizedProviderError:
    category: ProviderErrorCategory
    message: str
    retryable: bool


class StageInvocationError(Exception):
    """Sanitized stage failure. The message is a stable category string, never provider text."""

    def __init__(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        quantum_expired: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.quantum_expired = quantum_expired


def _classify_text(lowered: str) -> ProviderErrorCategory | None:
    if "wire budget" in lowered or "wire_budget" in lowered:
        return ProviderErrorCategory.WIRE_BUDGET
    if "rate limit" in lowered or "rate_limit" in lowered or "too many requests" in lowered:
        return ProviderErrorCategory.RATE_LIMIT
    if "unavailable" in lowered or "service unavailable" in lowered:
        return ProviderErrorCategory.UNAVAILABLE
    if "busy" in lowered:
        return ProviderErrorCategory.PROVIDER_BUSY
    if "containment" in lowered or "privacy boundary" in lowered:
        return ProviderErrorCategory.CONTAINMENT
    if "auth" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return ProviderErrorCategory.AUTH
    if "config" in lowered:
        return ProviderErrorCategory.CONFIG
    if "invalid output" in lowered or "invalid_output" in lowered:
        return ProviderErrorCategory.INVALID_OUTPUT
    if "validation" in lowered:
        return ProviderErrorCategory.VALIDATION
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderErrorCategory.TIMEOUT
    if "cancelled" in lowered or "canceled" in lowered:
        return ProviderErrorCategory.CANCELLED
    return None


def classify_provider_error(exc: BaseException) -> SanitizedProviderError:
    """Map a provider exception to a stable category. Never echo exception text."""
    if isinstance(exc, TimeoutError):
        category = ProviderErrorCategory.TIMEOUT
    elif isinstance(exc, ValidationError):
        category = ProviderErrorCategory.INVALID_OUTPUT
    else:
        category = _classify_text(str(exc).casefold()) or ProviderErrorCategory.UNKNOWN
    return SanitizedProviderError(
        category=category,
        message=_STABLE_MESSAGES[category],
        retryable=category in _RETRYABLE,
    )
