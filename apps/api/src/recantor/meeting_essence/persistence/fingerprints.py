from __future__ import annotations

import hashlib
import json
from typing import Any

from recantor.meeting_essence.identity import canonical_meeting_metadata
from recantor.meeting_essence.stage_contracts import ExtractStageRequest
from recantor.meeting_essence.wire_payload import WireSegmentFragment
from recantor.meeting_essence.wire_serialize import serialized_json_text


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_run_fingerprint(
    *,
    source_content_hash: str,
    prompt_cache_key: str,
    schema_identity: str,
    algorithm_identity: str,
    limits_fingerprint: str,
    provider_adapter_id: str,
) -> str:
    """Full derived cache key: source plus prompts, schema, limits, and provider route."""
    body = {
        "source_content_hash": source_content_hash,
        "prompt_cache_key": prompt_cache_key,
        "schema_identity": schema_identity,
        "algorithm_identity": algorithm_identity,
        "limits_fingerprint": limits_fingerprint,
        "provider_adapter_id": provider_adapter_id,
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _fragment_identity(fragment: WireSegmentFragment) -> dict[str, Any]:
    return {
        "segment_id": str(fragment.segment_id),
        "sequence": fragment.sequence,
        "start_ms": fragment.start_ms,
        "end_ms": fragment.end_ms,
        "content_hash": fragment.content_hash,
        "text": fragment.text,
        "fragment_index": fragment.fragment_index,
        "fragment_count": fragment.fragment_count,
        "text_span_start_cp": fragment.text_span_start_cp,
        "text_span_end_cp": fragment.text_span_end_cp,
    }


def extraction_reuse_key(
    request: ExtractStageRequest,
    *,
    provider_adapter_id: str,
    prompt_cache_key: str,
    limits_fingerprint: str,
) -> str:
    """Stable extract identity.

    Publication boundary is not an input, so a later segment can reuse an unchanged
    page. Meeting date and timezone are inputs: relative deadlines normalize from them.
    """
    protocol = request.metadata.protocol
    body = {
        "segment_fragments": [_fragment_identity(item) for item in request.segment_fragments],
        "context_fragments": [_fragment_identity(item) for item in request.context_fragments],
        "meeting_metadata": canonical_meeting_metadata(dict(request.source.meeting_metadata)),
        "omitted_record_ids": list(request.omitted_record_ids),
        "budget": request.budget.model_dump(mode="json"),
        "prompt_bundle_id": protocol.prompt_bundle_id if protocol is not None else "",
        "schema_identity": protocol.schema_identity if protocol is not None else "",
        "algorithm_identity": protocol.algorithm_identity if protocol is not None else "",
        "prompt_cache_key": prompt_cache_key,
        "limits_fingerprint": limits_fingerprint,
        "requested_model": request.metadata.requested_model or "",
        "requested_reasoning_effort": request.metadata.requested_reasoning_effort or "",
        "provider_adapter_id": provider_adapter_id,
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def stage_invocation_key(request: Any) -> str:
    material = serialized_json_text(request)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
