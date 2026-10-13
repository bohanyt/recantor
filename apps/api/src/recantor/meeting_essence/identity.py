from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from recantor.meeting_essence.contracts import ManifestSegmentEntry, SourceSnapshotManifest


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def segment_content_hash(text: str) -> str:
    """SHA-256 of exact UTF-8 segment text (preserves CRLF and outer whitespace)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def canonical_meeting_metadata(metadata: dict[str, str]) -> dict[str, str]:
    return {key: metadata[key] for key in sorted(metadata)}


def manifest_segment_fingerprint(segment: ManifestSegmentEntry) -> dict[str, Any]:
    """Stable segment material for snapshot hashing (excludes mutable capture metadata)."""
    return {
        "id": str(segment.id),
        "sequence": segment.sequence,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "language": segment.language,
        "created_at": _iso_or_none(segment.created_at),
        "content_hash": segment.content_hash,
    }


def compute_snapshot_content_hash(manifest: SourceSnapshotManifest) -> str:
    body = {
        "contract_version": manifest.contract_version,
        "session_id": str(manifest.session_id),
        "publication_boundary_sequence": manifest.publication_boundary_sequence,
        "meeting_metadata": canonical_meeting_metadata(manifest.meeting_metadata),
        "segments": [
            manifest_segment_fingerprint(segment)
            for segment in sorted(manifest.segments, key=lambda item: item.sequence)
        ],
        "coverage": manifest.coverage.model_dump(mode="json"),
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def compute_input_fingerprint(manifest: SourceSnapshotManifest) -> str:
    """Cache/job input identity: snapshot body without volatile capture timestamps."""
    return compute_snapshot_content_hash(manifest)


def compute_snapshot_id(session_id: str, content_hash: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{content_hash}".encode()).hexdigest()
    return f"snap_{digest[:24]}"


def compute_record_identity(topic: str, kind: str, first_segment_id: str, claim_text: str) -> str:
    material = f"{topic}|{kind}|{first_segment_id}|{claim_text.strip()}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"rec_{digest[:20]}"


def finalize_snapshot_manifest(manifest: SourceSnapshotManifest) -> SourceSnapshotManifest:
    content_hash = compute_snapshot_content_hash(manifest)
    snapshot_id = compute_snapshot_id(str(manifest.session_id), content_hash)
    return manifest.model_copy(
        update={"snapshot_content_hash": content_hash, "snapshot_id": snapshot_id},
    )
