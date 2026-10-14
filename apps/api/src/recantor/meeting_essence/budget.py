from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from recantor.meeting_essence.contracts import StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.wire_payload import WirePayloadAudit
from recantor.meeting_essence.wire_serialize import (
    schema_json_utf8_bytes,
    serialized_utf8_byte_length,
)

# Conservative output side: each reserved token may expand to 4 UTF-8 bytes.
_OUTPUT_BYTES_PER_TOKEN = 4
_TOKENS_PER_OUTPUT_SLOT = 128


@dataclass(frozen=True)
class EngineLimits:
    """Positive bounds. Defaults fit a real response schema plus a minimum useful request."""

    max_wire_utf8_bytes_per_call: int = 128_000
    max_context_tokens: int = 128_000
    max_output_tokens: int = 8_000
    max_segments_per_extract_page: int = 12
    max_fragment_chars_per_segment: int = 4_000
    max_records_per_extract_response: int = 48
    max_records_per_reconcile_page: int = 64
    max_records_per_check_page: int = 64
    max_records_per_render_batch: int = 64
    max_overview_bullets: int = 7
    context_overlap_segments: int = 1
    max_explicit_gaps_on_wire: int = 32
    max_gap_sequence_span: int = 10_000
    max_meeting_metadata_keys: int = 64
    max_metadata_value_utf8_bytes: int = 4_000
    max_prior_relations_per_page: int = 32
    max_relations_on_check_page: int = 32
    max_source_fragments_per_check: int = 1
    max_output_claims_per_check: int = 8
    max_dependency_records: int = 16
    max_manifest_materialization_pages: int = 10_000
    max_provider_call_seconds: float = 60.0
    provider_retry_backoff_seconds: float = 0.05
    max_extract_continuations: int = 8

    def __post_init__(self) -> None:
        positive_fields = (
            "max_wire_utf8_bytes_per_call",
            "max_context_tokens",
            "max_output_tokens",
            "max_segments_per_extract_page",
            "max_fragment_chars_per_segment",
            "max_records_per_extract_response",
            "max_records_per_reconcile_page",
            "max_records_per_check_page",
            "max_records_per_render_batch",
            "max_overview_bullets",
            "max_explicit_gaps_on_wire",
            "max_gap_sequence_span",
            "max_meeting_metadata_keys",
            "max_metadata_value_utf8_bytes",
            "max_prior_relations_per_page",
            "max_relations_on_check_page",
            "max_source_fragments_per_check",
            "max_output_claims_per_check",
            "max_dependency_records",
            "max_manifest_materialization_pages",
            "max_provider_call_seconds",
            "provider_retry_backoff_seconds",
            "max_extract_continuations",
        )
        for name in positive_fields:
            if getattr(self, name) <= 0:
                msg = f"EngineLimits.{name} must be positive"
                raise ValueError(msg)
        if self.context_overlap_segments < 0:
            msg = "EngineLimits.context_overlap_segments must be non-negative"
            raise ValueError(msg)


def estimate_schema_utf8_bytes(operation: StageOperation, response_model: type[BaseModel]) -> int:
    """UTF-8 size of the committed response JSON schema, not an identity-string stand-in."""
    del operation
    return schema_json_utf8_bytes(response_model)


def conservative_token_upper_bound_from_utf8(utf8_bytes: int) -> int:
    """UTF-8 byte length is a token upper bound: a token spans at least one byte."""
    return max(0, utf8_bytes)


def _prompt_utf8_bytes_for_operation(bundle: PromptBundle, operation: StageOperation) -> int:
    for item in bundle.prompts:
        if item.identity.name == operation.value:
            return len(item.text.encode("utf-8"))
    return 0


def _default_response_model(operation: StageOperation) -> type[BaseModel]:
    from recantor.meeting_essence.stage_contracts import (
        CheckStageResponse,
        ExtractStageResponse,
        ReconcileStageResponse,
        RenderStageResponse,
    )

    return {
        StageOperation.EXTRACT: ExtractStageResponse,
        StageOperation.RECONCILE: ReconcileStageResponse,
        StageOperation.CHECK: CheckStageResponse,
        StageOperation.RENDER: RenderStageResponse,
    }[operation]


def output_token_reserve(cardinality: int, limits: EngineLimits) -> int:
    slots = max(1, cardinality)
    return min(limits.max_output_tokens, slots * _TOKENS_PER_OUTPUT_SLOT)


def build_accounted_audit(
    *,
    bundle: PromptBundle,
    operation: StageOperation,
    response_model: type[BaseModel],
    transport_utf8_bytes: int,
    metadata_utf8_bytes: int,
    cardinality: int,
    unicode_payload_codepoints: int,
    limits: EngineLimits,
) -> WirePayloadAudit:
    prompt_utf8 = _prompt_utf8_bytes_for_operation(bundle, operation)
    schema_utf8 = estimate_schema_utf8_bytes(operation, response_model)
    reserved_tokens = output_token_reserve(cardinality, limits)
    reserved_utf8 = reserved_tokens * _OUTPUT_BYTES_PER_TOKEN
    input_utf8 = prompt_utf8 + schema_utf8 + transport_utf8_bytes
    input_tokens = conservative_token_upper_bound_from_utf8(input_utf8)
    return WirePayloadAudit(
        prompt_chars=prompt_utf8,
        schema_chars=schema_utf8,
        metadata_chars=metadata_utf8_bytes,
        payload_body_chars=max(0, transport_utf8_bytes - metadata_utf8_bytes),
        reserved_output_chars=reserved_utf8,
        total_estimated_chars=input_utf8 + reserved_utf8,
        token_upper_bound=input_tokens + reserved_tokens,
        estimation_method="utf8-byte-token-upper-bound-v3",
        transport_utf8_bytes=transport_utf8_bytes,
        schema_utf8_bytes=schema_utf8,
        prompt_utf8_bytes=prompt_utf8,
        context_input_token_upper_bound=input_tokens,
        reserved_output_tokens=reserved_tokens,
        output_cardinality_limit=max(0, cardinality),
        unicode_payload_codepoints=max(0, unicode_payload_codepoints),
    )


def stub_wire_audit() -> WirePayloadAudit:
    return WirePayloadAudit(
        prompt_chars=0,
        schema_chars=0,
        metadata_chars=0,
        payload_body_chars=0,
        reserved_output_chars=0,
        total_estimated_chars=0,
        token_upper_bound=0,
        estimation_method="pending",
    )


def stabilize_audited_request(
    request: BaseModel,
    *,
    bundle: PromptBundle,
    operation: StageOperation,
    response_model: type[BaseModel],
    limits: EngineLimits,
    cardinality: int,
    unicode_payload_codepoints: int,
) -> tuple[BaseModel, WirePayloadAudit]:
    """Embed an audit whose transport field matches the serialized request that contains it."""
    audit = stub_wire_audit()
    candidate = request
    for _ in range(8):
        candidate = request.model_copy(update={"wire_audit": audit})
        transport = serialized_utf8_byte_length(candidate)
        metadata = getattr(candidate, "metadata", None)
        metadata_bytes = serialized_utf8_byte_length(metadata) if metadata is not None else 0
        audit = build_accounted_audit(
            bundle=bundle,
            operation=operation,
            response_model=response_model,
            transport_utf8_bytes=transport,
            metadata_utf8_bytes=metadata_bytes,
            cardinality=cardinality,
            unicode_payload_codepoints=unicode_payload_codepoints,
            limits=limits,
        )
        embedded = request.model_copy(update={"wire_audit": audit})
        embedded_size = serialized_utf8_byte_length(embedded)
        if embedded_size == transport == audit.transport_utf8_bytes:
            return embedded, audit
    msg = "wire audit did not reach a fixed point"
    raise RuntimeError(msg)


def fits_wire_budget(audit: WirePayloadAudit, limits: EngineLimits) -> bool:
    if audit.transport_utf8_bytes > limits.max_wire_utf8_bytes_per_call:
        return False
    context = audit.context_input_token_upper_bound + audit.reserved_output_tokens
    return context <= limits.max_context_tokens


def fits_serialized_request(request: BaseModel, limits: EngineLimits) -> bool:
    return serialized_utf8_byte_length(request) <= limits.max_wire_utf8_bytes_per_call


def wire_limit_bytes(limits: EngineLimits) -> int:
    return limits.max_wire_utf8_bytes_per_call


def build_wire_audit_compat(
    *,
    bundle: PromptBundle,
    operation: StageOperation,
    metadata_chars: int,
    payload_body_chars: int,
    reserved_output_chars: int = 0,
) -> WirePayloadAudit:
    """Helper for tests that build a request before the engine stabilizes the audit."""
    del reserved_output_chars
    limits = EngineLimits()
    response_model = _default_response_model(operation)
    transport = metadata_chars + payload_body_chars
    return build_accounted_audit(
        bundle=bundle,
        operation=operation,
        response_model=response_model,
        transport_utf8_bytes=transport,
        metadata_utf8_bytes=metadata_chars,
        cardinality=1,
        unicode_payload_codepoints=payload_body_chars,
        limits=limits,
    )


def audit_from_serialized_request(
    *,
    bundle: PromptBundle,
    operation: StageOperation,
    response_model: type[BaseModel],
    request: BaseModel,
    cardinality: int = 1,
    unicode_payload_codepoints: int = 0,
    limits: EngineLimits | None = None,
) -> WirePayloadAudit:
    active_limits = limits or EngineLimits()
    metadata = getattr(request, "metadata", None)
    metadata_bytes = serialized_utf8_byte_length(metadata) if metadata is not None else 0
    return build_accounted_audit(
        bundle=bundle,
        operation=operation,
        response_model=response_model,
        transport_utf8_bytes=serialized_utf8_byte_length(request),
        metadata_utf8_bytes=metadata_bytes,
        cardinality=cardinality,
        unicode_payload_codepoints=unicode_payload_codepoints,
        limits=active_limits,
    )
