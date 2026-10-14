from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from recantor.meeting_essence.budget import EngineLimits
from recantor.meeting_essence.contracts import SourceSnapshotManifest
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.prompt_loader import PromptBundle

CHECKPOINT_BINDING_VERSION = "meeting-essence-checkpoint.v2"

_REQUIRED_FIELDS = (
    "binding_version",
    "snapshot_id",
    "snapshot_content_hash",
    "prompt_cache_key",
    "schema_identity",
    "algorithm_identity",
    "input_fingerprint",
    "session_id",
    "limits_fingerprint",
)


class CheckpointIncompatible(ValueError):
    """Fail closed before any checkpoint mutation."""


@dataclass(frozen=True)
class CheckpointBinding:
    binding_version: str
    snapshot_id: str
    snapshot_content_hash: str
    prompt_cache_key: str
    schema_identity: str
    algorithm_identity: str
    input_fingerprint: str
    session_id: str
    requested_model: str
    requested_reasoning_effort: str
    limits_fingerprint: str


def limits_fingerprint(
    limits: EngineLimits,
    *,
    requested_model: str | None,
    requested_reasoning_effort: str | None,
) -> str:
    payload = {
        "limits": asdict(limits),
        "requested_model": requested_model or "",
        "requested_reasoning_effort": requested_reasoning_effort or "",
        "binding_version": CHECKPOINT_BINDING_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def binding_from_run(
    manifest: SourceSnapshotManifest,
    bundle: PromptBundle,
    *,
    input_fingerprint: str,
    limits: EngineLimits,
    requested_model: str | None,
    requested_reasoning_effort: str | None,
) -> CheckpointBinding:
    return CheckpointBinding(
        binding_version=CHECKPOINT_BINDING_VERSION,
        snapshot_id=manifest.snapshot_id,
        snapshot_content_hash=manifest.snapshot_content_hash,
        prompt_cache_key=bundle.cache_key,
        schema_identity=bundle.schema_identity,
        algorithm_identity=bundle.algorithm_identity,
        input_fingerprint=input_fingerprint,
        session_id=str(manifest.session_id),
        requested_model=requested_model or "",
        requested_reasoning_effort=requested_reasoning_effort or "",
        limits_fingerprint=limits_fingerprint(
            limits,
            requested_model=requested_model,
            requested_reasoning_effort=requested_reasoning_effort,
        ),
    )


def _has_injected_state(checkpoint: EngineCheckpoint) -> bool:
    return bool(
        checkpoint.atomic_records
        or checkpoint.relations
        or checkpoint.layered_partial is not None
        or checkpoint.extract_fragment_cursor
        or checkpoint.extract_complete
        or checkpoint.reconcile_complete
        or checkpoint.check_complete
        or checkpoint.render_complete
        or checkpoint.inspected_segment_ids
        or checkpoint.completed_fragment_indices
        or checkpoint.reconcile_page_index
        or checkpoint.check_page_index
        or checkpoint.render_record_cursor
    )


def assert_checkpoint_compatible(checkpoint: EngineCheckpoint, binding: CheckpointBinding) -> None:
    if not checkpoint.binding_version:
        if _has_injected_state(checkpoint):
            msg = "unbound checkpoint contains ledger or cursor state"
            raise CheckpointIncompatible(msg)
        return
    for field_name in _REQUIRED_FIELDS:
        actual = getattr(checkpoint, field_name)
        expected = getattr(binding, field_name)
        if actual is None or actual == "":
            msg = f"checkpoint binding field {field_name} is blank"
            raise CheckpointIncompatible(msg)
        if actual != expected:
            msg = f"checkpoint incompatible with current run binding: {field_name}"
            raise CheckpointIncompatible(msg)
    for field_name in ("requested_model", "requested_reasoning_effort"):
        actual = getattr(checkpoint, field_name)
        expected = getattr(binding, field_name)
        if actual is None:
            msg = f"checkpoint binding field {field_name} is blank"
            raise CheckpointIncompatible(msg)
        if actual != expected:
            msg = f"checkpoint incompatible with current run binding: {field_name}"
            raise CheckpointIncompatible(msg)


def bind_checkpoint(checkpoint: EngineCheckpoint, binding: CheckpointBinding) -> None:
    checkpoint.binding_version = binding.binding_version
    checkpoint.snapshot_id = binding.snapshot_id
    checkpoint.snapshot_content_hash = binding.snapshot_content_hash
    checkpoint.prompt_cache_key = binding.prompt_cache_key
    checkpoint.schema_identity = binding.schema_identity
    checkpoint.algorithm_identity = binding.algorithm_identity
    checkpoint.input_fingerprint = binding.input_fingerprint
    checkpoint.session_id = binding.session_id
    checkpoint.requested_model = binding.requested_model
    checkpoint.requested_reasoning_effort = binding.requested_reasoning_effort
    checkpoint.limits_fingerprint = binding.limits_fingerprint
