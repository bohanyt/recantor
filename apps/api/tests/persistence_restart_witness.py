"""Separate-process witness for #72 resume. The parent test owns PostgreSQL start/stop."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("produce-extract", "consume", "produce-check", "consume-check"),
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--client-request-id", required=True)
    parser.add_argument("--session-id", required=True)
    return parser.parse_args()


async def _seed(session_id: UUID) -> None:
    from meeting_intelligence_fixtures import SEGMENT_SPECS

    from recantor.db import get_sessionmaker
    from recantor.models import RecordingSession, SessionKind, SessionState, TranscriptSegment

    async with get_sessionmaker()() as db:
        if await db.get(RecordingSession, session_id) is None:
            db.add(
                RecordingSession(
                    id=session_id,
                    client_request_id=uuid4(),
                    kind=SessionKind.LIVE.value,
                    state=SessionState.COMPLETE.value,
                )
            )
        for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS:
            if await db.get(TranscriptSegment, UUID(segment_id)) is None:
                db.add(
                    TranscriptSegment(
                        id=UUID(segment_id),
                        session_id=session_id,
                        sequence=sequence,
                        producer_key=f"witness-{segment_id}",
                        start_ms=start_ms,
                        end_ms=end_ms,
                        text=text,
                    )
                )
        await db.commit()


async def _produce(session_id: UUID, client_request_id: UUID) -> None:
    from meeting_intelligence_fixtures import SEGMENT_SPECS, build_s01_s06_bundle

    from recantor.db import get_sessionmaker
    from recantor.meeting_essence.contracts import SourceCoverageFlags
    from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
    from recantor.meeting_essence.engine import EngineRunConfig
    from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
    from recantor.meeting_essence.source_reader import (
        CanonicalSegmentInput,
        InMemoryCanonicalSourceReader,
    )

    bundle = build_s01_s06_bundle()
    reader = InMemoryCanonicalSourceReader(
        segments=[
            CanonicalSegmentInput(
                id=UUID(segment_id),
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
            for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
        ]
    )

    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)

        async def stop_after_extract(checkpoint) -> None:  # type: ignore[no-untyped-def]
            if checkpoint.extract_complete and not checkpoint.reconcile_complete:
                print(
                    json.dumps(
                        {
                            "phase": "produce-extract",
                            "extract_complete": True,
                            "reconcile_complete": False,
                        }
                    ),
                    flush=True,
                )
                os._exit(0)

        await service.run_engine(
            session_id=session_id,
            client_request_id=client_request_id,
            reader=reader,
            provider=DeterministicMeetingEssenceProvider(gold=bundle),
            publication_boundary_sequence=6,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
            config=EngineRunConfig(on_checkpoint_committed=stop_after_extract),
        )
    print(json.dumps({"phase": "produce-extract", "extract_complete": False}), flush=True)
    raise SystemExit(2)


async def _consume(session_id: UUID, client_request_id: UUID) -> None:
    from meeting_intelligence_fixtures import SEGMENT_SPECS, build_s01_s06_bundle

    from recantor.db import get_sessionmaker
    from recantor.meeting_essence.contracts import SourceCoverageFlags
    from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
    from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
    from recantor.meeting_essence.source_reader import (
        CanonicalSegmentInput,
        InMemoryCanonicalSourceReader,
    )

    bundle = build_s01_s06_bundle()
    reader = InMemoryCanonicalSourceReader(
        segments=[
            CanonicalSegmentInput(
                id=UUID(segment_id),
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
            for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
        ]
    )
    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        outcome = await service.run_engine(
            session_id=session_id,
            client_request_id=client_request_id,
            reader=reader,
            provider=DeterministicMeetingEssenceProvider(gold=bundle),
            publication_boundary_sequence=6,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
            recover=True,
        )
    print(
        json.dumps(
            {
                "phase": "consume",
                "job_id": str(outcome.job.id),
                "attempt_id": str(outcome.attempt.id),
                "status": outcome.result.processing_status.value,
                "provider_calls": outcome.provider_calls,
                "extract_provider_calls": outcome.extract_provider_calls,
                "cache_hits": outcome.cache_hits,
                "served_from_completed_cache": outcome.served_from_completed_cache,
                "extract_complete": outcome.result.checkpoint.extract_complete,
                "prompt_cache_key": outcome.result.trace.prompt_cache_key,
            }
        ),
        flush=True,
    )


async def _produce_check(session_id: UUID, client_request_id: UUID) -> None:
    from meeting_intelligence_fixtures import SEGMENT_SPECS, build_s01_s06_bundle

    from recantor.db import get_sessionmaker
    from recantor.meeting_essence.contracts import SourceCoverageFlags, StageOperation
    from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
    from recantor.meeting_essence.engine import EngineRunConfig
    from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
    from recantor.meeting_essence.source_reader import (
        CanonicalSegmentInput,
        InMemoryCanonicalSourceReader,
    )

    bundle = build_s01_s06_bundle()
    reader = InMemoryCanonicalSourceReader(
        segments=[
            CanonicalSegmentInput(
                id=UUID(segment_id),
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
            for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
        ]
    )
    seen: list[str] = []

    class _Counting(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            if kwargs.get("operation") == StageOperation.CHECK:
                request = kwargs["request"]
                seen.append(request.result_binding.request_fingerprint)  # type: ignore[attr-defined]
            return await super().run_stage(**kwargs)  # type: ignore[arg-type]

    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)

        async def stop_after_acked_check(checkpoint) -> None:  # type: ignore[no-untyped-def]
            if checkpoint.acked_check_fingerprints and not checkpoint.check_complete:
                print(
                    json.dumps(
                        {
                            "phase": "produce-check",
                            "acked": sorted(checkpoint.acked_check_fingerprints),
                            "extract_complete": checkpoint.extract_complete,
                            "check_invocations": seen,
                            "source_fragment_forward": len(checkpoint.source_fragment_forward),
                        }
                    ),
                    flush=True,
                )
                os._exit(0)

        await service.run_engine(
            session_id=session_id,
            client_request_id=client_request_id,
            reader=reader,
            provider=_Counting(gold=bundle),
            publication_boundary_sequence=6,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
            config=EngineRunConfig(on_checkpoint_committed=stop_after_acked_check),
        )
    print(json.dumps({"phase": "produce-check", "acked": []}), flush=True)
    raise SystemExit(2)


async def _consume_check(session_id: UUID, client_request_id: UUID) -> None:
    from meeting_intelligence_fixtures import SEGMENT_SPECS, build_s01_s06_bundle

    from recantor.db import get_sessionmaker
    from recantor.meeting_essence.contracts import SourceCoverageFlags, StageOperation
    from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
    from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
    from recantor.meeting_essence.source_reader import (
        CanonicalSegmentInput,
        InMemoryCanonicalSourceReader,
    )

    bundle = build_s01_s06_bundle()
    reader = InMemoryCanonicalSourceReader(
        segments=[
            CanonicalSegmentInput(
                id=UUID(segment_id),
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
            for segment_id, sequence, start_ms, end_ms, text in SEGMENT_SPECS
        ]
    )
    seen: list[str] = []

    class _Counting(DeterministicMeetingEssenceProvider):
        async def run_stage(self, **kwargs: object) -> object:
            if kwargs.get("operation") == StageOperation.CHECK:
                request = kwargs["request"]
                seen.append(request.result_binding.request_fingerprint)  # type: ignore[attr-defined]
            return await super().run_stage(**kwargs)  # type: ignore[arg-type]

    async with get_sessionmaker()() as db:
        service = MeetingIntelligenceLedgerService(db)
        outcome = await service.run_engine(
            session_id=session_id,
            client_request_id=client_request_id,
            reader=reader,
            provider=_Counting(gold=bundle),
            publication_boundary_sequence=6,
            coverage=SourceCoverageFlags(transcript_publication_complete=True),
            recover=True,
        )
    checkpoint = outcome.result.checkpoint
    print(
        json.dumps(
            {
                "phase": "consume-check",
                "status": outcome.result.processing_status.value,
                "extract_provider_calls": outcome.extract_provider_calls,
                "provider_calls": outcome.provider_calls,
                "cache_hits": outcome.cache_hits,
                "check_invocations": seen,
                "acked": sorted(checkpoint.acked_check_fingerprints),
                "source_fragment_forward": len(checkpoint.source_fragment_forward),
                "attempt_id": str(outcome.attempt.id),
                "generation": outcome.attempt.generation,
            }
        ),
        flush=True,
    )


def main() -> None:
    args = _parse()
    os.environ["DATABASE_URL"] = args.database_url
    session_id = UUID(args.session_id)
    client_request_id = UUID(args.client_request_id)

    async def run() -> None:
        from recantor.db import get_engine, get_sessionmaker
        from recantor.settings import get_settings

        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        await _seed(session_id)
        if args.phase == "produce-extract":
            await _produce(session_id, client_request_id)
        elif args.phase == "produce-check":
            await _produce_check(session_id, client_request_id)
        elif args.phase == "consume-check":
            await _consume_check(session_id, client_request_id)
        else:
            await _consume(session_id, client_request_id)

    asyncio.run(run())


if __name__ == "__main__":
    main()
