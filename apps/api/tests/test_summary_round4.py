"""Negative proofs for #73 correction round 4. Fake processes only; no live model."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from test_meeting_essence_scheduler import SESSION_A, SESSION_B, _finding, _seed_text

from recantor.db import get_sessionmaker
from recantor.meeting_essence.engine import EngineRunResult
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.jobs import (
    SummaryExecutionStatus,
    execute_next_summary_job,
    reconcile_summary_work,
    retry_summary_dispatch,
    submit_summary_work,
)
from recantor.meeting_essence.scheduling.models import (
    MISummaryDispatch,
    MISummaryDispatchStatus,
    MISummaryMetrics,
    MISummaryRequestReservation,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    ProcessIdentity,
    bind_owned_session,
    identity_for_pid,
    owned_subprocess_kwargs,
    process_liveness,
    terminate_process_tree,
)
from recantor.meeting_essence.scheduling.telemetry import useful_presentation
from recantor.meeting_essence.scheduling.triggers import note_transcript_publication
from recantor.models import RecordingSession, SessionState, TranscriptSegment
from recantor.settings import get_settings


async def _silence_other_sessions(*keep: UUID) -> None:
    """Stop fixture sessions from being claimed ahead of the session under test."""
    kept = set(keep)
    async with get_sessionmaker()() as db, db.begin():
        session_ids = list(await db.scalars(select(TranscriptSegment.session_id).distinct()))
        for session_id in session_ids:
            if session_id in kept:
                continue
            state = await db.get(MISummarySessionState, session_id)
            if state is None:
                db.add(MISummarySessionState(session_id=session_id, work_intent="cancelled"))
            else:
                state.work_intent = "cancelled"
        await db.execute(
            update(MISummaryDispatch)
            .where(
                MISummaryDispatch.session_id.notin_(kept),
                MISummaryDispatch.status.in_(
                    [
                        MISummaryDispatchStatus.PENDING.value,
                        MISummaryDispatchStatus.CLAIMED.value,
                    ]
                ),
            )
            .values(status=MISummaryDispatchStatus.CANCELLED.value, claim_token=None)
        )


def test_same_thread_host_lock_is_not_reentrant(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path))
    first = host_authority.try_hold()
    second = host_authority.try_hold()
    other: list[bool] = []

    def _other() -> None:
        held = host_authority.try_hold()
        other.append(held is not None)
        if held is not None:
            held.release()

    thread = threading.Thread(target=_other)
    thread.start()
    thread.join(timeout=2)
    try:
        assert first is not None
        assert second is None
        assert other == [False]
    finally:
        if first is not None:
            first.release()


@pytest.mark.asyncio
async def test_corrupt_sidecar_and_wrong_cluster_cannot_start_a_process(
    mi_db, tmp_path: Path, monkeypatch
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path))
    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round4-authority")
    await permit.release(handle)
    sidecar = host_authority.sidecar_path()
    sidecar.write_text("{", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert await permit.try_acquire(entrypoint="corrupt-sidecar") is None
        assert child.poll() is None
        host_authority.write_owned([])
        identity = host_authority.identity_path()
        stored = json.loads(identity.read_text(encoding="utf-8"))
        stored["system_identifier"] = "1"
        identity.write_text(json.dumps(stored), encoding="utf-8")
        assert await permit.try_acquire(entrypoint="wrong-cluster") is None
        assert child.poll() is None
        assert await permit.read_active_witness() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_cooperative_cancel_releases_and_suppressing_cancel_quarantines(mi_db) -> None:
    del mi_db
    active = 0
    peak = 0

    class _Cooperative:
        spawns_process_tree = False
        started = asyncio.Event()

        async def run_stage(self, **kwargs: object) -> None:
            nonlocal active, peak
            del kwargs
            active += 1
            peak = max(peak, active)
            self.started.set()
            try:
                await asyncio.sleep(30)
            finally:
                active -= 1

    permit = HostInvocationPermit()
    cooperative = _Cooperative()
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=cooperative, permit=permit, entrypoint="round4-cooperative"
    )
    task = asyncio.create_task(
        wrapper.run_stage(operation=None, request=None, job=None, prompt_bundle=None)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(cooperative.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wrapper.aclose() is True
    assert active == 0
    assert await permit.read_active_witness() == 0
    again = await permit.try_acquire(entrypoint="round4-after-cooperative")
    assert again is not None
    await permit.release(again)

    class _Suppress:
        spawns_process_tree = False

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stop = False

        async def run_stage(self, **kwargs: object) -> None:
            nonlocal active, peak
            del kwargs
            active += 1
            peak = max(peak, active)
            self.started.set()
            try:
                while not self.stop:
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        continue
            finally:
                active -= 1

    permit = HostInvocationPermit()
    suppressing = _Suppress()
    held = PermitBoundMeetingEssenceProvider(
        inner=suppressing, permit=permit, entrypoint="round4-suppressing"
    )
    running = asyncio.create_task(
        held.run_stage(operation=None, request=None, job=None, prompt_bundle=None)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(suppressing.started.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await held.aclose() is False
    assert active == 1
    assert await permit.read_active_witness() == 1
    assert await permit.try_acquire(entrypoint="round4-while-suppressing") is None
    assert peak == 1
    suppressing.stop = True
    deadline = time.monotonic() + 2
    while active != 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert active == 0
    deadline = time.monotonic() + 2
    while await permit.read_active_witness() != 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert await permit.read_active_witness() == 0
    follow = await permit.try_acquire(entrypoint="round4-after-suppressing")
    assert follow is not None
    await permit.release(follow)


@pytest.mark.asyncio
async def test_aclose_still_releases_when_inner_close_raises(mi_db) -> None:
    del mi_db

    class _Inner:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs

        async def aclose(self) -> None:
            raise RuntimeError("inner close failed")

    permit = HostInvocationPermit()
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=_Inner(), permit=permit, entrypoint="round4-close"
    )
    await wrapper.run_stage(operation=None, request=None, job=None, prompt_bundle=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="inner close failed"):
        await wrapper.aclose()
    assert await permit.read_active_witness() == 0


@pytest.mark.asyncio
async def test_owned_tree_stop_is_fenced_and_does_not_kill_unknown_pids(
    mi_db, tmp_path: Path
) -> None:
    del mi_db
    grandchild_path = tmp_path / "grandchild.txt"
    code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(grandchild_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **owned_subprocess_kwargs(),
    )
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    permit = HostInvocationPermit()
    handle = None
    try:
        deadline = time.monotonic() + 5
        while not grandchild_path.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        grandchild_pid = int(grandchild_path.read_text(encoding="utf-8"))
        parent_identity = bind_owned_session(identity_for_pid(parent.pid))
        assert parent_identity is not None
        grandchild_identity = identity_for_pid(grandchild_pid)
        assert process_liveness(ProcessIdentity(parent.pid, None)) == Liveness.UNKNOWN
        assert terminate_process_tree(ProcessIdentity(parent.pid, None)) is False
        assert parent.poll() is None
        mismatch = (
            ProcessIdentity(parent.pid, created_ms=1)
            if sys.platform == "win32"
            else ProcessIdentity(parent.pid, start_ticks=1)
        )
        # A reused or mismatched pid is not signalled and is not tree closure.
        assert terminate_process_tree(mismatch) is False
        assert parent.poll() is None
        handle = await permit.acquire(entrypoint="round4-tree")
        assert await permit.register_owned_runtime(handle, [parent_identity]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.try_acquire(entrypoint="round4-while-tree") is None
        assert await permit.terminate_owned_tree(handle) is True
        parent.wait(timeout=5)
        assert process_liveness(grandchild_identity) == Liveness.DEAD
        assert process_liveness(parent_identity) == Liveness.DEAD
        assert stranger.poll() is None
        assert await permit.release(handle) is True
        handle = None
        assert await permit.read_active_witness() == 0
    finally:
        for proc in (parent, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if handle is not None:
            await permit.release(handle)


@pytest.mark.asyncio
async def test_cancel_before_registration_does_not_leave_the_child(mi_db) -> None:
    del mi_db
    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round4-preregister")

    class _Inner:
        spawns_process_tree = True

    wrapper = PermitBoundMeetingEssenceProvider(
        inner=_Inner(), permit=permit, entrypoint="round4-preregister", shared_handle=handle
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wrapper.request_cancel()
        assert await wrapper.prepare_owned_spawn([identity_for_pid(child.pid)]) is False
        child.wait(timeout=5)
        assert await permit.read_active_witness() == 1
        await permit.release(handle)
        assert await permit.read_active_witness() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_provider_timeout_retries_and_quantum_expiry_does_not(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MEETING_ESSENCE_RETRY_BASE_SECONDS", "0.1")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "30")
    get_settings.cache_clear()
    session_id = UUID("12121212-1212-4121-8121-121212121212")
    await _seed_text(session_id, ["Batas waktu penyedia."])
    calls = 0

    class _Timeout:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            nonlocal calls
            del kwargs
            calls += 1
            raise TimeoutError

    await _silence_other_sessions(session_id)
    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=session_id, publication_sequence=1)
        await submit_summary_work(db, session_id=session_id, client_request_id=uuid4())
    for _ in range(4):
        async with get_sessionmaker()() as db:
            outcome = await execute_next_summary_job(
                db, entrypoint="round4-timeout", provider=_Timeout()
            )
        if outcome.session_id not in {None, session_id}:
            continue
        if outcome.status == SummaryExecutionStatus.NOT_CLAIMED:
            await asyncio.sleep(0.4)
            continue
        await asyncio.sleep(0.4)
    stalled = calls
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        outcome = await execute_next_summary_job(
            db, entrypoint="round4-timeout", provider=_Timeout()
        )
    assert outcome.status == SummaryExecutionStatus.NOT_CLAIMED
    assert calls == stalled
    async with get_sessionmaker()() as db:
        state = await db.get(MISummarySessionState, session_id)
        dispatch = await db.scalar(
            select(MISummaryDispatch)
            .where(MISummaryDispatch.session_id == session_id)
            .order_by(MISummaryDispatch.created_at.desc())
        )
    assert state is not None and state.work_intent == "blocked"
    assert state.blocked_disposition == "retry_budget_exhausted"
    assert dispatch is not None
    assert dispatch.last_error_category == "timeout", (
        dispatch.status,
        dispatch.last_error_category,
        dispatch.last_error_code,
        dispatch.failure_count,
        state.blocked_category,
    )
    async with get_sessionmaker()() as db, db.begin():
        await retry_summary_dispatch(db, dispatch_id=dispatch.id, session_id=session_id)
    async with get_sessionmaker()() as db:
        await execute_next_summary_job(db, entrypoint="round4-retry", provider=_Timeout())
    assert calls > stalled

    quantum_session = UUID("34343434-3434-4343-8343-343434343434")
    await _seed_text(quantum_session, ["Kuantum habis."])
    await _silence_other_sessions(quantum_session)
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "0.05")
    get_settings.cache_clear()

    class _Slow:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs
            await asyncio.sleep(2)

    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=quantum_session, publication_sequence=1)
        await submit_summary_work(db, session_id=quantum_session, client_request_id=uuid4())
    async with get_sessionmaker()() as db:
        quantum = await execute_next_summary_job(db, entrypoint="round4-quantum", provider=_Slow())
    assert quantum.status == SummaryExecutionStatus.PARTIAL
    async with get_sessionmaker()() as db:
        row = await db.scalar(
            select(MISummaryDispatch).where(MISummaryDispatch.session_id == quantum_session)
        )
    assert row is not None
    assert int(row.failure_count) == 0
    assert row.last_error_category != "timeout"


def test_withheld_layered_draft_is_not_a_useful_presentation() -> None:
    checkpoint = EngineCheckpoint()
    checkpoint.layered_partial = object()  # type: ignore[assignment]
    result = EngineRunResult(
        manifest=None,  # type: ignore[arg-type]
        layered_result=None,
        checkpoint=checkpoint,
        trace=None,  # type: ignore[arg-type]
        processing_status=None,  # type: ignore[arg-type]
    )
    assert useful_presentation(result) is False


@pytest.mark.asyncio
async def test_parked_reactivation_obeys_the_same_depth(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_MAX_DEPTH", "1")
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "model-a")
    get_settings.cache_clear()
    await _silence_other_sessions(SESSION_A)

    class _Idle:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs
            raise AssertionError("a session with no transcript must not call the provider")

    async with get_sessionmaker()() as db, db.begin():
        if await db.get(RecordingSession, SESSION_A) is None:
            db.add(
                RecordingSession(
                    id=SESSION_A,
                    client_request_id=uuid4(),
                    kind="live",
                    state=SessionState.RECORDING.value,
                )
            )
        await db.execute(delete(TranscriptSegment).where(TranscriptSegment.session_id == SESSION_A))
        existing = await db.get(MISummarySessionState, SESSION_A)
        if existing is not None:
            existing.observed_publication_high_water = 0
            existing.work_intent = "open"
        await db.execute(
            delete(MISummaryRequestReservation).where(
                MISummaryRequestReservation.session_id == SESSION_A
            )
        )
        await db.execute(delete(MISummaryDispatch).where(MISummaryDispatch.session_id == SESSION_A))
        parked = await submit_summary_work(db, session_id=SESSION_A, client_request_id=uuid4())
    async with get_sessionmaker()() as db:
        parked_run = await execute_next_summary_job(db, entrypoint="round4-park", provider=_Idle())
    assert parked_run.status == SummaryExecutionStatus.PARKED, parked_run
    await _seed_text(SESSION_B, ["Sesi berjalan."])
    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=SESSION_B, publication_sequence=1)
        running = await submit_summary_work(db, session_id=SESSION_B, client_request_id=uuid4())
    assert parked.status.value == "accepted"
    assert running.status.value == "accepted"
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "model-b")
    get_settings.cache_clear()
    async with get_sessionmaker()() as db, db.begin():
        changed = await submit_summary_work(db, session_id=SESSION_A, client_request_id=uuid4())
    assert changed.status.value == "coalesced"
    async with get_sessionmaker()() as db:
        runnable = int(
            await db.scalar(
                select(func.count())
                .select_from(MISummaryDispatch)
                .where(
                    MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
                    MISummaryDispatch.disposition == "runnable",
                )
            )
            or 0
        )
        parked_row = await db.scalar(
            select(MISummaryDispatch).where(MISummaryDispatch.session_id == SESSION_A)
        )
    assert runnable == 1
    assert parked_row is not None and parked_row.capacity_deferred is True
    async with get_sessionmaker()() as db, db.begin():
        from recantor.meeting_essence.scheduling.jobs import cancel_summary_dispatch

        await cancel_summary_dispatch(db, dispatch_id=running.dispatch_id, session_id=SESSION_B)
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        revived = await db.scalar(
            select(MISummaryDispatch).where(MISummaryDispatch.session_id == SESSION_A)
        )
        cancelled = await db.get(MISummarySessionState, SESSION_B)
    assert revived is not None and revived.disposition == "runnable"
    assert cancelled is not None and cancelled.work_intent == "cancelled"


@pytest.mark.asyncio
async def test_stop_freezes_transcript_not_the_audio_cursor(mi_db) -> None:
    del mi_db
    audio_high = UUID("56565656-5656-4565-8565-565656565656")
    transcript_high = UUID("78787878-7878-4787-8787-787878787878")
    async with get_sessionmaker()() as db, db.begin():
        for existing_id in (audio_high, transcript_high):
            await db.execute(
                delete(MISummaryRequestReservation).where(
                    MISummaryRequestReservation.session_id == existing_id
                )
            )
            await db.execute(
                delete(MISummaryMetrics).where(MISummaryMetrics.session_id == existing_id)
            )
            await db.execute(
                delete(MISummaryDispatch).where(MISummaryDispatch.session_id == existing_id)
            )
            await db.execute(
                delete(MISummarySessionState).where(MISummarySessionState.session_id == existing_id)
            )
            await db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.session_id == existing_id)
            )
            await db.execute(delete(RecordingSession).where(RecordingSession.id == existing_id))
        for session_id, final_sequence, count in (
            (audio_high, 100, 7),
            (transcript_high, 7, 3),
        ):
            db.add(
                RecordingSession(
                    id=session_id,
                    client_request_id=uuid4(),
                    kind="live",
                    state=SessionState.COMPLETE.value,
                    final_sequence=final_sequence,
                    audio_completeness="full",
                )
            )
            for index in range(count):
                db.add(
                    TranscriptSegment(
                        id=uuid4(),
                        session_id=session_id,
                        sequence=index + 1,
                        producer_key=f"freeze-{session_id}-{index}",
                        start_ms=index,
                        end_ms=index + 1,
                        text=f"teks {index}",
                    )
                )
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        high_audio = await db.get(MISummarySessionState, audio_high)
        high_transcript = await db.get(MISummarySessionState, transcript_high)
    assert high_audio is not None and high_transcript is not None
    assert high_audio.audio_final_sequence == 100
    assert high_audio.frozen_transcript_sequence == 7
    assert high_audio.frozen_artifact_publication == 7
    frozen_hash = high_audio.frozen_transcript_hash
    assert high_transcript.audio_final_sequence == 7
    assert high_transcript.frozen_transcript_sequence == 3
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            TranscriptSegment(
                id=uuid4(),
                session_id=audio_high,
                sequence=8,
                producer_key="freeze-late",
                start_ms=8,
                end_ms=9,
                text="terlambat",
            )
        )
        await note_transcript_publication(db, session_id=audio_high, publication_sequence=8)
    async with get_sessionmaker()() as db:
        updated = await db.get(MISummarySessionState, audio_high)
    assert updated is not None
    assert updated.frozen_transcript_sequence == 7
    assert updated.frozen_transcript_hash == frozen_hash
    assert updated.observed_publication_high_water == 8
    assert updated.audio_final_sequence == 100


@pytest.mark.asyncio
async def test_two_jobs_keep_separate_metrics(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "30")
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "metrics-a")
    get_settings.cache_clear()
    session_id = UUID("90909090-9090-4909-8909-909090909090")
    ids = await _seed_text(session_id, ["Metrik pertama."])
    records = [_finding(ids[0], 1000, 1800, "metric-1", "Metrik pertama.")]

    class _Once:
        spawns_process_tree = False

        def __init__(self) -> None:
            from recantor.meeting_essence.deterministic_provider import (
                DeterministicMeetingEssenceProvider,
            )

            self.inner = DeterministicMeetingEssenceProvider(
                extraction_records=records,
                evaluation_records=list(records),
                extract_by_segment={ids[0]: records},
            )
            self.calls = 0

        async def run_stage(self, **kwargs: object):  # type: ignore[no-untyped-def]
            self.calls += 1
            return await self.inner.run_stage(**kwargs)

    provider = _Once()
    await _silence_other_sessions(session_id)
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            delete(MISummaryRequestReservation).where(
                MISummaryRequestReservation.session_id == session_id
            )
        )
        await db.execute(
            delete(MISummaryDispatch).where(MISummaryDispatch.session_id == session_id)
        )
        await db.execute(delete(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id))
        await note_transcript_publication(db, session_id=session_id, publication_sequence=1)
        await submit_summary_work(db, session_id=session_id, client_request_id=uuid4())
    async with get_sessionmaker()() as db:
        ready = await db.get(MISummarySessionState, session_id)
    assert ready is not None and int(ready.observed_publication_high_water) == 1
    async with get_sessionmaker()() as db:
        first = await execute_next_summary_job(db, entrypoint="round4-metrics", provider=provider)
    assert first.status == SummaryExecutionStatus.SUCCEEDED, (
        first.status,
        first.session_id,
        first.dispatch_id,
    )
    async with get_sessionmaker()() as db:
        before = list(
            await db.scalars(
                select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
            )
        )
    assert len(before) == 1
    assert before[0].final_result_at is not None
    assert before[0].observed_tokens is None
    assert before[0].provider_calls == provider.calls
    calls_after_first = provider.calls
    monkeypatch.setenv("MEETING_ESSENCE_REQUESTED_MODEL", "metrics-b")
    get_settings.cache_clear()
    async with get_sessionmaker()() as db, db.begin():
        await submit_summary_work(db, session_id=session_id, client_request_id=uuid4())
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
            )
        )
        pending = await db.scalar(
            select(MISummaryDispatch).where(
                MISummaryDispatch.session_id == session_id,
                MISummaryDispatch.status == MISummaryDispatchStatus.PENDING.value,
            )
        )
    assert pending is not None
    assert len(rows) == 1
    assert rows[0].final_result_at == before[0].final_result_at
    async with get_sessionmaker()() as db:
        second = await execute_next_summary_job(
            db, entrypoint="round4-metrics-b", provider=provider
        )
    assert second.status == SummaryExecutionStatus.SUCCEEDED
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
            )
        )
    assert len(rows) == 2
    assert {row.config_fingerprint for row in rows} != {rows[0].config_fingerprint}
    assert sum(row.provider_calls for row in rows) == provider.calls
    assert provider.calls > calls_after_first
    assert all(row.observed_tokens is None for row in rows)
    assert all(row.retries == 0 for row in rows)
