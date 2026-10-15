from __future__ import annotations

from recantor.meeting_essence.contracts import StageOperation
from recantor.meeting_essence.identities import SUPPORTED_SCHEMA_IDENTITIES
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider_errors import ProviderErrorCategory, StageInvocationError

OPERATION_VERSION_V1 = "meeting-essence.operation/v1"

_V1_OPERATIONS = frozenset(
    {
        StageOperation.EXTRACT,
        StageOperation.RECONCILE,
        StageOperation.CHECK,
        StageOperation.RENDER,
    }
)

SUPPORTED_OPERATION_VERSIONS: dict[str, frozenset[StageOperation]] = {
    OPERATION_VERSION_V1: _V1_OPERATIONS,
}


def assert_supported_operation_version(
    *,
    prompt_bundle: PromptBundle,
    operation: StageOperation,
    requested_version: str,
) -> str:
    """Select one explicit operation version. Unknown versions are configuration errors."""
    version = requested_version.strip()
    allowed = SUPPORTED_OPERATION_VERSIONS.get(version)
    if allowed is None:
        raise StageInvocationError(
            ProviderErrorCategory.CONFIG,
            "unknown meeting essence operation version",
        )
    if operation not in allowed:
        raise StageInvocationError(
            ProviderErrorCategory.CONFIG,
            "operation is not part of the requested version",
        )
    if prompt_bundle.schema_identity not in SUPPORTED_SCHEMA_IDENTITIES:
        raise StageInvocationError(
            ProviderErrorCategory.CONFIG,
            "unsupported meeting essence schema identity",
        )
    return version
