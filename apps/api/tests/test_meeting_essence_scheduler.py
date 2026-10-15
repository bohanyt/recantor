from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from recantor.db import get_sessionmaker
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    EvidenceReference,
    RecordContentStatus,
    RecordKind,
)
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.persistence.checkpoint_store import deserialize_checkpoint
from recantor.meeting_essence.persistence.models import (
    MIAtomicRecordRow,
    MIAttemptCheckpoint,
)
from recantor.meeting_essence.scheduling.host_authority import reset_for_tests
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.jobs import (
    SummaryExecutionStatus,
    SummarySubmitStatus,
    cancel_summary_dispatch,
    execute_next_summary_job,
    reconcile_summary_work,
    submit_summary_work,
)
from recantor.meeting_essence.scheduling.models import (
    MIHostInvocationLease,
    MISummaryDispatch,
    MISummaryDispatchStatus,
    MISummaryRequestReservation,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.process_liveness import (
    ProcessIdentity,
    identity_for_pid,
)
from recantor.meeting_essence.scheduling.provider_factory import (
    get_summary_provider_factory,
    set_summary_provider_factory,
)
from recantor.meeting_essence.scheduling.triggers import (
    note_session_finalized,
    note_transcript_publication,
)
from recantor.models import (
    AudioCompleteness,
    RecordingGap,
    RecordingSession,
    SessionKind,
    SessionState,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
)
from recantor.settings import get_settings
from recantor.transcript import commit_transcript_segment

WITNESS = __file__.replace("test_meeting_essence_scheduler.py", "scheduling_permit_witness.py")
SESSION_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SESSION_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SESSION_C = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


@pytest.fixture(autouse=True)
async def _clean_summary_scheduler_tables(mi_db) -> None:
    del mi_db
    set_summary_provider_factory(None)
    reset_for_tests()
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(delete(MISummaryRequestReservation))
        await db.execute(delete(MISummaryDispatch))
        await db.execute(delete(MISummarySessionState))
        await db.execute(
            text(
                """
                INSERT INTO mi_summary_session_states (
                    session_id,
                    observed_publication_high_water,
                    highest_processed_publication,
                    work_intent
                )
                SELECT session_id, MAX(sequence), MAX(sequence), 'cancelled'
                FROM transcript_segments
                GROUP BY session_id
                ON CONFLICT (session_id) DO UPDATE
                SET observed_publication_high_water = EXCLUDED.observed_publication_high_water,
                    highest_processed_publication = EXCLUDED.highest_processed_publication,
                    work_intent = 'cancelled'
                """
            )
        )
        await db.execute(
            update(MIHostInvocationLease)
            .where(MIHostInvocationLease.id == 1)
            .values(
                holder_token=None,
                holder_pid=None,
                holder_pid_created_ms=None,
                holder_start_ticks=None,
                holder_entrypoint=None,
                lease_expires_at=None,
                acquired_at=None,
                active_invocations=0,
                holder_state="free",
                owned_runtime_json="[]",
                termination_acked=False,
                quarantine_reason=None,
                invocation_admitted=False,
                spawns_process_tree=False,
            )
        )
        await db.execute(text("DELETE FROM mi_summary_metrics"))
        await db.execute(
            text("UPDATE mi_summary_scan_cursor SET canonical_after = NULL, state_after = NULL")
        )
    yield
    set_summary_provider_factory(None)
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            delete(STTJob).where(STTJob.session_id.in_([SESSION_A, SESSION_B, SESSION_C]))
        )
        await db.execute(
            delete(TranscriptionUtterance).where(
                TranscriptionUtterance.session_id.in_([SESSION_A, SESSION_B, SESSION_C])
            )
        )
        await db.execute(
            delete(RecordingGap).where(
                RecordingGap.session_id.in_([SESSION_A, SESSION_B, SESSION_C])
            )
        )
        await db.execute(
            delete(TranscriptSegment).where(
                TranscriptSegment.session_id.in_([SESSION_A, SESSION_B, SESSION_C])
            )
        )


async def _seed_text(session_id: UUID, texts: list[str]) -> list[UUID]:
    ids: list[UUID] = []
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)
        )
        await db.execute(
            delete(MISummarySessionState).where(MISummarySessionState.session_id == session_id)
        )
        if await db.get(RecordingSession, session_id) is None:
            db.add(
                RecordingSession(
                    id=session_id,
                    client_request_id=uuid4(),
                    kind=SessionKind.LIVE.value,
                    state=SessionState.RECORDING.value,
                )
            )
        for index, text in enumerate(texts, start=1):
            segment_id = uuid4()
            ids.append(segment_id)
            db.add(
                TranscriptSegment(
                    id=segment_id,
                    session_id=session_id,
                    sequence=index,
                    producer_key=f"sched-{session_id}-{index}",
                    start_ms=index * 1000,
                    end_ms=index * 1000 + 800,
                    text=text,
                )
            )
    return ids


def _finding(
    segment_id: UUID, start_ms: int, end_ms: int, record_id: str, claim: str
) -> AtomicRecord:
    return AtomicRecord(
        record_id=record_id,
        topic="Rapat",
        kind=RecordKind.FINDING,
        claim_text=claim,
        content_status=RecordContentStatus.FINDING,
        evidence=[EvidenceReference(segment_id=segment_id, start_ms=start_ms, end_ms=end_ms)],
    )


class _CountingProvider(DeterministicMeetingEssenceProvider):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.extract_pages: list[tuple[UUID, ...]] = []
        self.records_by_session: dict[str, list[AtomicRecord]] = {}
        self.calls = 0
        self.calls_by_session: dict[str, int] = {}

    async def run_stage(self, **kwargs: object):  # type: ignore[no-untyped-def]
        request = kwargs["request"]
        operation = kwargs["operation"]
        job = kwargs["job"]
        self.calls += 1
        self.calls_by_session[str(job.session_id)] = (
            self.calls_by_session.get(str(job.session_id), 0) + 1
        )
        if job.session_id == SESSION_A:
            await asyncio.sleep(0.05)
        scoped = self.records_by_session.get(str(job.session_id))
        if scoped is not None:
            self.evaluation_records = scoped
        from recantor.meeting_essence.contracts import StageOperation
        from recantor.meeting_essence.stage_contracts import CheckStageRequest, ExtractStageRequest

        if operation == StageOperation.EXTRACT and isinstance(request, ExtractStageRequest):
            page = tuple(fragment.segment_id for fragment in request.segment_fragments)
            self.extract_pages.append(page)
        if operation == StageOperation.CHECK and isinstance(request, CheckStageRequest):
            saved = self.evaluation_records
            page_ids = {fragment.segment_id for fragment in request.source_fragments}
            self.evaluation_records = [
                record
                for record in saved or []
                if any(evidence.segment_id in page_ids for evidence in record.evidence)
            ]
            try:
                return await super().run_stage(**kwargs)  # type: ignore[arg-type]
            finally:
                self.evaluation_records = saved
        return await super().run_stage(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_host_permit_allows_only_one_active_witness(mi_db) -> None:
    del mi_db
    permit = HostInvocationPermit()
    first = await permit.acquire(entrypoint="test-a")
    assert await permit.read_active_witness() == 1
    assert await permit.try_acquire(entrypoint="test-b") is None
    assert await permit.release(first) is True
    assert await permit.read_active_witness() == 0


@pytest.mark.asyncio
async def test_dead_caller_does_not_admit_a_second_live_child(mi_db) -> None:
    del mi_db
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        owner.wait(timeout=5)
        permit = HostInvocationPermit()
        handle = await permit.acquire(
            entrypoint="simulated-dead-worker-owned-live-child",
            lease_seconds=0.02,
            identity=ProcessIdentity(owner.pid, None),
        )
        registered = await permit.register_owned_runtime(handle, [identity_for_pid(child.pid)])
        assert registered is True
        await asyncio.sleep(0.05)
        assert await permit.try_acquire(entrypoint="competing-helper") is None
        assert child.poll() is None
        assert await permit.read_active_witness() == 1
        child.kill()
        child.wait(timeout=5)
        assert await permit.acknowledge_termination(handle) is True
        assert await permit.read_active_witness() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.asyncio
async def test_request_alias_is_session_scoped_and_coalesced_ids_are_reserved(mi_db) -> None:
    del mi_db
    await _seed_text(SESSION_A, ["Satu."])
    await _seed_text(SESSION_C, ["Lain."])
    request_x = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        first = await submit_summary_work(db, session_id=SESSION_A, client_request_id=request_x)
    async with get_sessionmaker()() as db, db.begin():
        crossed = await submit_summary_work(db, session_id=SESSION_C, client_request_id=request_x)
    assert first.status == SummarySubmitStatus.ACCEPTED
    assert crossed.status == SummarySubmitStatus.CONFLICT
    assert crossed.dispatch_id is None
    second_id = uuid4()
    third_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        coalesced = await submit_summary_work(db, session_id=SESSION_A, client_request_id=second_id)
        again = await submit_summary_work(db, session_id=SESSION_A, client_request_id=third_id)
    assert coalesced.status == SummarySubmitStatus.COALESCED
    assert coalesced.dispatch_id == first.dispatch_id
    assert again.dispatch_id == first.dispatch_id
    async with get_sessionmaker()() as db:
        reserved = await db.get(MISummaryRequestReservation, second_id)
        assert reserved is not None
        assert reserved.dispatch_id == first.dispatch_id
        replay = await submit_summary_work(db, session_id=SESSION_A, client_request_id=second_id)
        await db.rollback()
    assert replay.status == SummarySubmitStatus.DUPLICATE
    assert replay.dispatch_id == first.dispatch_id


@pytest.mark.asyncio
async def test_publication_cursor_is_monotonic_and_freeze_is_not_the_later_boundary(mi_db) -> None:
    del mi_db
    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=SESSION_A, publication_sequence=7)
    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=SESSION_A, publication_sequence=6)
        await note_session_finalized(
            db,
            session_id=SESSION_A,
            audio_final_sequence=100,
            transcript_sequence=6,
            transcript_hash="ab" * 32,
        )
    async with get_sessionmaker()() as db:
        state = await db.get(MISummarySessionState, SESSION_A)
    assert state is not None
    assert state.observed_publication_high_water == 7
    assert state.frozen_artifact_publication == 6
    assert state.audio_final_sequence == 100
    assert state.frozen_transcript_sequence == 6
    assert state.frozen_transcript_hash == "ab" * 32


@pytest.mark.asyncio
async def test_reconcile_discovers_canonical_rows_without_a_prior_notice(mi_db) -> None:
    del mi_db
    await _seed_text(SESSION_A, ["Materi kanonik tanpa notice."])
    async with get_sessionmaker()() as db:
        assert await db.get(MISummarySessionState, SESSION_A) is None
        result = await reconcile_summary_work(db)
        await db.commit()
    assert result.scanned >= 1
    assert result.created >= 1
    async with get_sessionmaker()() as db:
        state = await db.get(MISummarySessionState, SESSION_A)
        assert state is not None
        assert state.observed_publication_high_water >= 1
    async with get_sessionmaker()() as db, db.begin():
        await cancel_summary_dispatch(
            db,
            dispatch_id=(
                await db.scalar(
                    select(MISummaryDispatch.id).where(MISummaryDispatch.session_id == SESSION_A)
                )
            ),
            session_id=SESSION_A,
        )
    async with get_sessionmaker()() as db:
        again = await reconcile_summary_work(db)
        await db.commit()
        open_count = await db.scalar(
            select(func.count())
            .select_from(MISummaryDispatch)
            .where(
                MISummaryDispatch.session_id == SESSION_A,
                MISummaryDispatch.status.in_(
                    [
                        MISummaryDispatchStatus.PENDING.value,
                        MISummaryDispatchStatus.CLAIMED.value,
                    ]
                ),
            )
        )
    assert again.created == 0
    assert int(open_count or 0) == 0


@pytest.mark.asyncio
async def test_unconfigured_provider_is_unavailable(mi_db, monkeypatch: pytest.MonkeyPatch) -> None:
    del mi_db
    await _seed_text(SESSION_A, ["Tidak ada adapter."])
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        result = await execute_next_summary_job(db, entrypoint="pytest-unconfigured")
    assert result.status == SummaryExecutionStatus.UNAVAILABLE
    assert get_summary_provider_factory().adapter_id == "unconfigured"


@pytest.mark.asyncio
async def test_factory_failure_blocks_without_orphan_claim_or_permit(mi_db, tmp_path, monkeypatch):
    from summary_broker_provider import BrokerFactory

    del mi_db
    segment = (await _seed_text(SESSION_A, ["Invalid fixture."]))[0]
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "log_path": str(tmp_path / "calls.log"),
                "records": [
                    {
                        "record_id": "a-1",
                        "segment_id": str(segment),
                        "claim": "Invalid fixture.",
                        "start_ms": 1000,
                        "end_ms": 1800,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECANTOR_SUMMARY_CONTROL", str(control))
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    get_settings.cache_clear()

    class BrokenFactory(BrokerFactory):
        builds = 0

        def build(self):
            self.builds += 1
            return super().build()

    factory = BrokenFactory()
    set_summary_provider_factory(factory)
    async with get_sessionmaker()() as db:
        result = await execute_next_summary_job(db, entrypoint="pytest-invalid-factory")
    assert result.status == SummaryExecutionStatus.UNAVAILABLE
    assert result.session_id == SESSION_A
    async with get_sessionmaker()() as db:
        dispatch = await db.get(MISummaryDispatch, result.dispatch_id)
        assert dispatch.status == MISummaryDispatchStatus.CANCELLED.value
        assert dispatch.claim_token is None and dispatch.claim_expires_at is None
        assert dispatch.attempt_count == 1
        assert dispatch.last_error_category == "config"
        assert dispatch.last_error_code == "provider_construction_failed"
        state = await db.get(MISummarySessionState, SESSION_A)
        assert state.work_intent == "blocked"
        for _ in range(3):
            assert (await execute_next_summary_job(db)).status == SummaryExecutionStatus.NOT_CLAIMED
    assert factory.builds == 1
    assert not (tmp_path / "calls.log").exists()
    permit = HostInvocationPermit()
    assert await permit.blocking_reason() is None
    handle = await permit.acquire(entrypoint="pytest-after-invalid-factory")
    assert await permit.release(handle) is True


@pytest.mark.asyncio
async def test_completed_broker_tree_does_not_block_next_crash_session(
    mi_db, tmp_path, monkeypatch
):
    """Real worker child/PG, including an earlier completed spawning invocation."""
    from test_summary_broker_lifecycle import (
        _checkpoint_page,
        _identity_from_saved,
        _kill_pid_only,
        _reap_saved_identities,
        _saved_identities,
        _stop_process,
        prepare_shared_broker_permit,
    )

    from recantor.meeting_essence.scheduling.process_liveness import (
        Liveness,
        process_liveness,
        terminate_process_tree,
    )

    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "60")
    get_settings.cache_clear()
    records = []
    for session_id, count in ((SESSION_A, 1), (SESSION_B, 13)):
        segments = await _seed_text(session_id, [f"Butir {i}." for i in range(count)])
        records.extend(
            {
                "record_id": f"{session_id.hex[:4]}-{i}",
                "segment_id": str(segment),
                "claim": f"Butir {i}.",
                "start_ms": (i + 1) * 1000,
                "end_ms": (i + 1) * 1000 + 800,
            }
            for i, segment in enumerate(segments)
        )
        async with get_sessionmaker()() as db, db.begin():
            await note_transcript_publication(db, session_id=session_id, publication_sequence=count)
            await submit_summary_work(db, session_id=session_id, client_request_id=uuid4())
    control = tmp_path / "control.json"
    log_path = tmp_path / "calls.log"
    control.write_text(
        json.dumps(
            dict(
                log_path=str(log_path),
                records=records,
                spawn_tree=True,
                block=True,
                block_after_extracts=1,
            )
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["RECANTOR_SUMMARY_CONTROL"] = str(control)
    env["RECANTOR_SUMMARY_PROVIDER_FACTORY"] = "summary_broker_provider:factory"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]
    )
    completed = tmp_path / "completed.json"
    runtime_path = tmp_path / "worker-identity.json"
    code = """
import asyncio, json, os, sys
from dataclasses import asdict
from pathlib import Path
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling import tasks
from recantor.meeting_essence.scheduling.jobs import execute_next_summary_job
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.process_liveness import current_process_identity
Path(sys.argv[3]).write_text(json.dumps(asdict(current_process_identity())))
async def main():
    async with get_sessionmaker()() as db:
        first = await execute_next_summary_job(db, entrypoint='broker-lifecycle-regression')
        root = json.loads(Path(sys.argv[1]).read_text())
        Path(sys.argv[2]).write_text(json.dumps(dict(
            session=str(first.session_id), status=first.status.value, root=root)))
        # Bounded cleanup may settle asynchronously, just as between broker wakes.
        while await HostInvocationPermit().blocks_new_invocation():
            await asyncio.sleep(0.05)
        await execute_next_summary_job(db, entrypoint='broker-lifecycle-regression')
asyncio.run(main())
"""
    await prepare_shared_broker_permit()
    worker_log = tmp_path / "worker.log"
    root = None
    grandchild = None
    worker = None
    runtime = None
    try:
        with worker_log.open("w", encoding="utf-8") as output:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(tmp_path / "owned-identity.json"),
                    str(completed),
                    str(runtime_path),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert worker.poll() is None, worker_log.read_text(encoding="utf-8")
                saved = _saved_identities(log_path)
                roots = [item for item in saved if item.get("role") == "root"]
                children = [item for item in saved if item.get("role") == "grandchild"]
                async with get_sessionmaker()() as db:
                    checkpoint = await db.scalar(
                        select(MIAttemptCheckpoint.body_json).where(
                            MIAttemptCheckpoint.session_id == SESSION_B
                        )
                    )
                    lease = await db.get(MIHostInvocationLease, 1)
                if completed.is_file() and roots and children and _checkpoint_page(checkpoint) >= 1:
                    root = _identity_from_saved(roots[0])
                    grandchild = _identity_from_saved(children[0])
                    break
                await asyncio.sleep(0.05)
            assert root is not None and grandchild is not None, worker_log.read_text(
                encoding="utf-8"
            )
            first = json.loads(completed.read_text(encoding="utf-8"))
            assert first["session"] == str(SESSION_A)
            assert first["status"] == "succeeded"
            assert process_liveness(_identity_from_saved(first["root"])) == Liveness.DEAD
            # Windows venv python.exe is a launcher; the lease belongs to the
            # runtime that reports its original identity, not necessarily Popen.pid.
            runtime = _identity_from_saved(json.loads(runtime_path.read_text(encoding="utf-8")))
            assert lease.holder_pid == runtime.pid
            assert lease.holder_entrypoint == "broker-lifecycle-regression"
            assert root.pid not in {worker.pid, runtime.pid, os.getpid()}
            assert str(root.pid) in lease.owned_runtime_json
            assert process_liveness(root) == process_liveness(grandchild) == Liveness.ALIVE
            assert process_liveness(runtime) == Liveness.ALIVE
            _kill_pid_only(runtime.pid)
            worker.wait(timeout=5)
            assert process_liveness(root) == process_liveness(grandchild) == Liveness.ALIVE
            permit = HostInvocationPermit()
            assert await permit.try_acquire(entrypoint="before-proven-stop") is None
            assert await permit.recover_proven_dead() is False
            assert await asyncio.to_thread(terminate_process_tree, root) is True
            assert process_liveness(root) == process_liveness(grandchild) == Liveness.DEAD
            assert await permit.recover_proven_dead() is True
            handle = await permit.acquire(entrypoint="after-proven-stop")
            assert await permit.release(handle) is True
    finally:
        if runtime is None and runtime_path.is_file():
            runtime = _identity_from_saved(json.loads(runtime_path.read_text(encoding="utf-8")))
        if runtime is not None and process_liveness(runtime) == Liveness.ALIVE:
            _kill_pid_only(runtime.pid)
        _stop_process(worker)
        _reap_saved_identities(log_path)
        await HostInvocationPermit().recover_proven_dead()


@pytest.mark.asyncio
async def test_transcript_commit_does_not_wait_for_a_stalled_summary_notice(mi_db) -> None:
    del mi_db
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

    async def _stalled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await asyncio.sleep(2)

    from recantor.meeting_essence.scheduling import triggers

    original = triggers._write_publication
    triggers._write_publication = _stalled  # type: ignore[assignment]
    try:
        started = time.monotonic()
        async with get_sessionmaker()() as db:
            segment, replay = await commit_transcript_segment(
                db,
                session_id=session_id,
                producer_key="live-1",
                start_ms=0,
                end_ms=1000,
                text="Halo tim, kita mulai.",
            )
        elapsed = time.monotonic() - started
    finally:
        triggers._write_publication = original  # type: ignore[assignment]
    assert replay is False
    assert segment.sequence == 1
    assert elapsed < 0.5
    async with get_sessionmaker()() as db:
        stored = await db.get(TranscriptSegment, segment.id)
        assert stored is not None
        assert stored.text == "Halo tim, kita mulai."
        found = await reconcile_summary_work(db)
        await db.commit()
    assert found.scanned >= 1


@pytest.mark.asyncio
async def test_coverage_uses_stt_jobs_and_audio_gaps(mi_db) -> None:
    del mi_db
    from recantor.meeting_essence.scheduling.source_material import build_coverage_flags

    utterance_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            TranscriptionUtterance(
                id=utterance_id,
                session_id=SESSION_A,
                sequence=1,
                producer_key="utt-1",
                start_ms=0,
                end_ms=500,
                content_type="audio/wav",
                sha256="a" * 64,
                byte_length=16,
                storage_key="tests/utt-1.wav",
            )
        )
        await db.flush()
        db.add(
            STTJob(utterance_id=utterance_id, session_id=SESSION_A, state=STTJobState.PENDING.value)
        )
        session = await db.get(RecordingSession, SESSION_A)
        assert session is not None
        session.state = SessionState.COMPLETE.value
        session.audio_completeness = AudioCompleteness.FULL.value
        db.add(
            RecordingGap(
                session_id=SESSION_A,
                client_gap_id=uuid4(),
                writer_id="writer",
                capture_epoch=1,
                sequence_start=2,
                sequence_end=2,
                reason="missing_chunk",
            )
        )
        flags = await build_coverage_flags(
            db, session_id=SESSION_A, publication_boundary_sequence=1, summary_state=None
        )
    assert flags.stt_backlog_pending is True
    assert flags.recording_audio_complete is False
    assert flags.explicit_gaps


@pytest.mark.asyncio
async def test_fairness_keeps_accepted_extract_pages_across_a_turn(mi_db, monkeypatch) -> None:
    del mi_db
    quantum_seconds = 2.0
    cleanup_allowance_seconds = 8.0
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", str(quantum_seconds))
    monkeypatch.setenv("MEETING_ESSENCE_FAIRNESS_YIELD_SECONDS", "0.3")
    get_settings.cache_clear()
    long_text = ("Catatan rapat yang cukup panjang untuk mengisi halaman. " * 40)[:1800]
    texts_a = [f"{long_text} butir {index:03d}." for index in range(70)]
    ids_a = await _seed_text(SESSION_A, texts_a)
    ids_b = await _seed_text(SESSION_B, ["Sesi lain singkat.", "Masih singkat.", "Cukup."])
    async with get_sessionmaker()() as db, db.begin():
        for session_id, final_sequence in ((SESSION_A, 70), (SESSION_B, 3)):
            session = await db.get(RecordingSession, session_id)
            assert session is not None
            session.state = SessionState.COMPLETE.value
            session.final_sequence = final_sequence
            session.audio_completeness = "full"
    assert sum(len(text) for text in texts_a) >= 120_000
    records = [
        _finding(
            segment_id,
            (index + 1) * 1000,
            (index + 1) * 1000 + 800,
            f"rec-{index:04d}",
            f"Butir {index:03d}.",
        )
        for index, segment_id in enumerate(ids_a)
    ]
    records.extend(
        _finding(
            segment_id,
            (index + 1) * 1000,
            (index + 1) * 1000 + 800,
            f"alt-{index:04d}",
            f"Lain {index}.",
        )
        for index, segment_id in enumerate(ids_b)
    )
    assert len(records) >= 70
    provider = _CountingProvider(
        extraction_records=records,
        evaluation_records=list(records),
        extract_by_segment={},
    )
    mapping: dict[UUID, list[AtomicRecord]] = {}
    for record in records:
        for evidence in record.evidence:
            mapping.setdefault(evidence.segment_id, []).append(record)
    provider.extract_by_segment = mapping
    provider.records_by_session = {
        str(SESSION_A): records[: len(ids_a)],
        str(SESSION_B): records[len(ids_a) :],
    }
    async with get_sessionmaker()() as db, db.begin():
        await note_transcript_publication(db, session_id=SESSION_A, publication_sequence=70)
        await note_transcript_publication(db, session_id=SESSION_B, publication_sequence=3)
        await submit_summary_work(db, session_id=SESSION_A, client_request_id=uuid4())
        await submit_summary_work(db, session_id=SESSION_B, client_request_id=uuid4())
    first_page: tuple[UUID, ...] | None = None
    saw_b_between = False
    partial_metrics = None
    finished: dict[UUID, str] = {}
    order: list[tuple[str, str]] = []
    turn_bounds: list[float] = []
    a_partials = 0
    for _ in range(100):
        if SESSION_A in finished and SESSION_B in finished:
            break
        assert get_settings().meeting_essence_engine_quantum_seconds == quantum_seconds
        async with get_sessionmaker()() as db:
            outcome = await execute_next_summary_job(
                db, entrypoint="pytest-fairness", provider=provider
            )
        if (
            outcome.metrics is not None
            and outcome.metrics.execution_ms is not None
            and outcome.session_id == SESSION_A
        ):
            turn_bounds.append(outcome.metrics.execution_ms)
        if outcome.session_id is None:
            if outcome.status == SummaryExecutionStatus.NOT_CLAIMED:
                await asyncio.sleep(0.35)
            continue
        order.append((str(outcome.session_id)[:8], outcome.status.value))
        if (
            outcome.status == SummaryExecutionStatus.PARTIAL
            and outcome.session_id == SESSION_A
            and partial_metrics is None
        ):
            partial_metrics = outcome.metrics
            a_partials += 1
            if provider.extract_pages:
                first_page = provider.extract_pages[0]
        elif outcome.status == SummaryExecutionStatus.PARTIAL and outcome.session_id == SESSION_A:
            a_partials += 1
        if outcome.status == SummaryExecutionStatus.SUCCEEDED and outcome.session_id is not None:
            finished[outcome.session_id] = outcome.status.value
        if (
            first_page is not None
            and SESSION_A not in finished
            and outcome.session_id == SESSION_B
            and outcome.status == SummaryExecutionStatus.SUCCEEDED
        ):
            saw_b_between = True
    assert first_page is not None
    assert partial_metrics is not None
    assert a_partials >= 3, (order, a_partials)
    assert turn_bounds
    assert max(turn_bounds) <= (quantum_seconds + cleanup_allowance_seconds) * 1000.0
    assert get_settings().meeting_essence_engine_quantum_seconds == quantum_seconds
    assert partial_metrics.execution_ms is not None
    assert partial_metrics.extract_ms is not None
    assert partial_metrics.first_result_ms is None
    assert partial_metrics.final_result_ms is None
    assert partial_metrics.observed_tokens is None
    assert saw_b_between is True, (order, finished)
    if finished.get(SESSION_A) != "succeeded":
        async with get_sessionmaker()() as db:
            body = await db.scalar(
                select(MIAttemptCheckpoint.body_json)
                .where(MIAttemptCheckpoint.session_id == SESSION_A)
                .order_by(MIAttemptCheckpoint.generation.desc())
                .limit(1)
            )
        stuck = deserialize_checkpoint(body or "")
        raise AssertionError(
            (
                a_partials,
                provider.calls,
                len(set(provider.extract_pages)),
                None
                if stuck is None
                else (
                    stuck.extract_complete,
                    stuck.reconcile_complete,
                    stuck.check_complete,
                    stuck.render_complete,
                    len(stuck.atomic_records),
                ),
            )
        )
    assert finished.get(SESSION_B) == "succeeded"
    assert provider.extract_pages.count(first_page) == 1
    assert len(provider.extract_pages) == len(set(provider.extract_pages))
    async with get_sessionmaker()() as db:
        checkpoint = deserialize_checkpoint(
            await db.scalar(
                select(MIAttemptCheckpoint.body_json)
                .where(MIAttemptCheckpoint.session_id == SESSION_A)
                .order_by(MIAttemptCheckpoint.generation.desc())
                .limit(1)
            )
            or ""
        )
        checkpoint_b = deserialize_checkpoint(
            await db.scalar(
                select(MIAttemptCheckpoint.body_json)
                .where(MIAttemptCheckpoint.session_id == SESSION_B)
                .order_by(MIAttemptCheckpoint.generation.desc())
                .limit(1)
            )
            or ""
        )
        from recantor.meeting_essence.contracts import CheckStatus
        from recantor.meeting_essence.scheduling.models import MISummaryMetrics

        metrics_rows = list(
            await db.scalars(
                select(MISummaryMetrics).where(MISummaryMetrics.session_id == SESSION_A)
            )
        )
        metrics = metrics_rows[0] if len(metrics_rows) == 1 else None
        if len(metrics_rows) > 1:
            metrics = next(row for row in metrics_rows if row.final_result_at is not None)
        stored = await db.scalar(
            select(func.count())
            .select_from(MIAtomicRecordRow)
            .where(MIAtomicRecordRow.session_id == SESSION_A)
        )
    assert checkpoint is not None and checkpoint_b is not None
    assert checkpoint.extract_complete is True
    assert checkpoint.reconcile_complete is True
    assert checkpoint.check_complete is True
    assert checkpoint.render_complete is True
    assert checkpoint.forward_semantic_check_status == CheckStatus.COMPLETED
    assert checkpoint.backward_semantic_check_status == CheckStatus.COMPLETED
    assert checkpoint_b.extract_complete is True
    assert checkpoint_b.check_complete is True
    assert int(stored or 0) >= 70
    assert metrics is not None
    assert metrics.original_queued_at is not None
    assert metrics.first_useful_at is not None
    assert metrics.final_result_at is not None
    assert metrics.observed_tokens is None
    assert metrics.quantum_count >= 4
    assert metrics.final_result_at >= metrics.original_queued_at
    assert metrics.provider_calls == provider.calls_by_session[str(SESSION_A)]
    covered = {
        evidence.segment_id for record in checkpoint.atomic_records for evidence in record.evidence
    }
    assert set(ids_a) <= covered
    assert metrics.observed_tokens is None


def test_cross_process_permit_subprocess_blocks(mi_db) -> None:
    del mi_db
    if os.environ.get("RECANTOR_PG_OWNED") != "1":
        pytest.skip("cross-process permit witness requires RECANTOR_PG_OWNED=1")
    database_url = os.environ["DATABASE_URL"]
    token_path = os.path.join(os.environ.get("TEMP", "."), f"recantor-permit-{uuid4().hex}.txt")
    api_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    env = os.environ.copy()
    env["PYTHONPATH"] = api_src
    proc = subprocess.Popen(
        [
            sys.executable,
            WITNESS,
            "--database-url",
            database_url,
            "--token-out",
            token_path,
            "--hold-seconds",
            "8",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not os.path.isfile(token_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert os.path.isfile(token_path)

        async def _try_second() -> int:
            permit = HostInvocationPermit()
            handle = await permit.try_acquire(entrypoint="parent-competitor")
            witness = await permit.read_active_witness()
            if handle is not None:
                await permit.release(handle)
            return witness if handle is None else -1

        assert asyncio.run(_try_second()) == 1
    finally:
        proc.wait(timeout=15)
        if os.path.isfile(token_path):
            os.remove(token_path)


def test_summary_wake_reaches_redis_when_broker_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis import Redis
    from redis.exceptions import RedisError

    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_NAME", f"summary-test-{uuid4().hex[:8]}")
    get_settings.cache_clear()
    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=0.3,
        socket_timeout=0.3,
    )
    try:
        client.ping()
    except (OSError, RedisError):
        pytest.skip("Redis is not reachable on this host; CI runs this against Redis 8")
    from recantor.meeting_essence.scheduling.tasks import ensure_summary_wake_capacity

    queue = settings.meeting_essence_queue_name
    client.delete(queue)
    try:
        published = ensure_summary_wake_capacity(1)
        assert published == 1
        assert int(client.llen(queue)) == 1
        payload = client.lindex(queue, 0)
        assert payload is not None
        assert b"recantor.process_summary_wake" in payload
    finally:
        client.delete(queue)
        client.close()
        get_settings.cache_clear()
