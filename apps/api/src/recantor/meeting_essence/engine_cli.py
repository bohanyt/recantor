from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from recantor.meeting_essence.contracts import MeetingIntelligenceExampleBundle
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.engine import MeetingEssenceEngine, engine_trace_json
from recantor.meeting_essence.source_reader import (
    CanonicalSegmentInput,
    InMemoryCanonicalSourceReader,
)


def _bundle_from_path(path: Path) -> MeetingIntelligenceExampleBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return MeetingIntelligenceExampleBundle.model_validate(payload)


def _reader_from_manifest(
    bundle: MeetingIntelligenceExampleBundle,
) -> InMemoryCanonicalSourceReader:
    segments = [
        CanonicalSegmentInput(
            id=segment.id,
            sequence=segment.sequence,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text=segment.text,
            language=segment.language,
            created_at=segment.created_at,
        )
        for segment in bundle.manifest.segments
    ]
    return InMemoryCanonicalSourceReader(segments=segments)


async def _run_fixture(bundle: MeetingIntelligenceExampleBundle) -> int:
    reader = _reader_from_manifest(bundle)
    provider = DeterministicMeetingEssenceProvider(gold=bundle)
    engine = MeetingEssenceEngine(provider=provider)
    result = await engine.run_from_reader(
        reader,
        session_id=bundle.manifest.session_id,
        publication_boundary_sequence=bundle.manifest.publication_boundary_sequence,
        coverage=bundle.manifest.coverage.model_copy(deep=True),
        meeting_metadata=dict(bundle.manifest.meeting_metadata),
    )
    print(f"snapshot_id={result.manifest.snapshot_id}")
    print(f"snapshot_content_hash={result.manifest.snapshot_content_hash}")
    print(f"processing_status={result.processing_status.value}")
    print(f"schema_identity={result.trace.schema_identity}")
    print(f"prompt_cache_key={result.trace.prompt_cache_key}")
    print(f"stage_calls={len(result.trace.stage_calls)}")
    print(engine_trace_json(result.trace))
    if result.layered_result is None:
        return 2
    return 0 if result.processing_status.value in {"succeeded", "partial"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline meeting essence engine on a fixture.")
    parser.add_argument("fixture", type=Path, help="Path to meeting intelligence example JSON")
    args = parser.parse_args(argv)
    bundle = _bundle_from_path(args.fixture)
    return asyncio.run(_run_fixture(bundle))


if __name__ == "__main__":
    raise SystemExit(main())
