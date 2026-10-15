"""Round 9: a cancelled admission is not a provider call."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from recantor.db import get_sessionmaker
from recantor.meeting_essence.contracts import StageOperation
from recantor.meeting_essence.persistence.caching_provider import (
    CachedMeetingEssenceProvider,
    EnteredProviderCalls,
    entered_call_counts,
)
from recantor.meeting_essence.scheduling.host_permit import PermitHandle
from recantor.meeting_essence.scheduling.models import MISummaryMetrics
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity
from recantor.meeting_essence.scheduling.telemetry import persist_integrated_metrics
from recantor.models import RecordingSession, SessionKind, SessionState


class _BlockingPermit:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def authority_reachable(self) -> bool:
        self.started.set()
        await self.release.wait()
        return True

    async def renew(self, handle: object) -> None:
        del handle

    async def mark_admitted(self, handle: object, **kwargs: object) -> None:
        del handle, kwargs


class _Inner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_stage(self, **kwargs: object) -> str:
        del kwargs
        self.calls += 1
        return "ran"


def _handle() -> PermitHandle:
    return PermitHandle(
        token="round9",
        identity=ProcessIdentity(pid=1, created_ms=1, start_ticks=1),
        entrypoint="round9-pre-inner",
        acquired_at=datetime.now(tz=UTC),
        generation=1,
    )


def _stack(
    permit: _BlockingPermit,
) -> tuple[CachedMeetingEssenceProvider, EnteredProviderCalls, _Inner]:
    inner = _Inner()
    entered = EnteredProviderCalls(inner)
    bound = PermitBoundMeetingEssenceProvider(
        inner=entered,
        permit=permit,  # type: ignore[arg-type]
        entrypoint="round9-pre-inner",
        shared_handle=_handle(),
    )
    cached = CachedMeetingEssenceProvider(
        inner=bound,
        repository=None,  # type: ignore[arg-type]
        session_id=str(uuid4()),
        run_fingerprint="r",
        source_content_hash="s",
        provider_adapter_id="injected",
        prompt_cache_key="p",
        limits_fingerprint="l",
    )
    return cached, entered, inner


async def _call(cached: CachedMeetingEssenceProvider) -> None:
    await cached.run_stage(
        operation=StageOperation.CHECK,
        request=None,  # type: ignore[arg-type]
        job=None,  # type: ignore[arg-type]
        prompt_bundle=None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_cancel_before_provider_entry_is_not_a_call() -> None:
    permit = _BlockingPermit()
    cached, entered, inner = _stack(permit)
    task = asyncio.create_task(_call(cached))
    await asyncio.wait_for(permit.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.calls == 0
    assert entered.provider_calls == 0
    assert entered_call_counts(cached) == (0, 0)
    assert cached.provider_calls == 0


@pytest.mark.asyncio
async def test_entered_call_matches_the_provider_and_is_stored_once(mi_db) -> None:
    del mi_db
    permit = _BlockingPermit()
    permit.release.set()
    cached, entered, inner = _stack(permit)
    await _call(cached)
    assert inner.calls == 1
    assert entered_call_counts(cached) == (1, 0)
    session_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            RecordingSession(
                id=session_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.RECORDING.value,
            )
        )
    queued = datetime(2026, 10, 15, tzinfo=UTC)
    async with get_sessionmaker()() as db, db.begin():
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=queued,
            finished_at=queued,
            result=None,
            provider_calls=0,
            cache_hits=0,
            retries=0,
            source_fingerprint="s",
            config_fingerprint="c" * 64,
            input_fingerprint="i" * 64,
            job_id="job-round9",
            attempt_id="attempt-round9",
        )
        calls, _extract = entered_call_counts(cached)
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=queued,
            finished_at=queued,
            result=None,
            provider_calls=calls,
            cache_hits=0,
            retries=0,
            source_fingerprint="s",
            config_fingerprint="c" * 64,
            input_fingerprint="i" * 64,
            job_id="job-round9",
            attempt_id="attempt-round9",
        )
    async with get_sessionmaker()() as db:
        row = await db.scalar(
            select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
        )
    assert row is not None
    assert int(row.provider_calls) == 1
