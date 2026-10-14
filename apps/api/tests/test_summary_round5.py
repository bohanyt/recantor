"""Negative proofs for #73 correction round 5. Fake processes only; no live model."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.engine.url import make_url

from recantor.db import get_sessionmaker
from recantor.meeting_essence.contracts import ProcessingJobStatus, StageOperation
from recantor.meeting_essence.engine import EngineRunResult, EngineRunTrace
from recantor.meeting_essence.identity import finalize_snapshot_manifest, segment_content_hash
from recantor.meeting_essence.progress import EngineCheckpoint, StageCallTrace
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import (
    HostInvocationBusy,
    HostInvocationPermit,
    _endpoint_matches,
)
from recantor.meeting_essence.scheduling.jobs import (
    cancel_summary_dispatch,
    reconcile_summary_work,
    retry_summary_dispatch,
    submit_summary_work,
)
from recantor.meeting_essence.scheduling.models import (
    MIHostInvocationLease,
    MISummaryDispatch,
    MISummaryDispatchStatus,
    MISummaryMetrics,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    ProcessIdentity,
    bind_owned_session,
    identity_for_pid,
    local_process_namespace,
    owned_subprocess_kwargs,
    posix_signal_order,
    process_is_alive,
    process_liveness,
    terminate_process_tree,
)
from recantor.meeting_essence.scheduling.source_material import load_material_snapshot
from recantor.meeting_essence.scheduling.telemetry import (
    metrics_input_fingerprint,
    persist_integrated_metrics,
)
from recantor.models import (
    RecordingSession,
    SessionKind,
    SessionState,
    STTJob,
    STTJobState,
    TranscriptionUtterance,
    TranscriptSegment,
)
from recantor.settings import get_settings

_SESSION = UUID("51515151-5151-4515-8515-515151515151")
_OTHER = UUID("52525252-5252-4525-8525-525252525252")
_THIRD = UUID("53535353-5353-4535-8535-535353535353")


def test_posix_signal_order_is_descendants_before_the_root() -> None:
    order = posix_signal_order(101001, [101002, 101003])
    assert order == [101003, 101002, 101001]
    assert order[-1] == 101001


def test_virtual_posix_graph_does_not_report_a_live_child_dead(monkeypatch) -> None:
    """Controlled freeze. This does not execute a Linux process."""
    import signal

    from recantor.meeting_essence.scheduling import process_liveness as module

    had_sigkill = hasattr(signal, "SIGKILL")
    old_sigkill = getattr(signal, "SIGKILL", None)
    had_sigstop = hasattr(signal, "SIGSTOP")
    old_sigstop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    root, child, grandchild = 101001, 101002, 101003
    alive = {root, child, grandchild}
    state = {root: "S", child: "S", grandchild: "S"}
    killed: list[int] = []

    def descendants(pid: int) -> list[int]:
        if pid == root and root in alive:
            return [child, grandchild]
        if pid == child and child in alive:
            return [grandchild]
        return []

    def fake_signal(pid: int, sig: int) -> bool:
        if sig == signal.SIGSTOP and pid in alive:
            state[pid] = "T"
            return True
        if sig == signal.SIGKILL and pid in alive:
            killed.append(pid)
            alive.discard(pid)
            return True
        return False

    monkeypatch.setattr(module, "_posix_signal", fake_signal)
    monkeypatch.setattr(module, "_posix_descendants", descendants)
    monkeypatch.setattr(
        module,
        "_posix_proc_stat",
        lambda pid: (state.get(pid), 1) if pid in alive else (None, None),
    )
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda identity: module.Liveness.ALIVE if identity.pid in alive else module.Liveness.DEAD,
    )
    try:
        result = module._posix_contain_tree(ProcessIdentity(root, start_ticks=1))
    finally:
        if had_sigkill:
            signal.SIGKILL = old_sigkill
        elif hasattr(signal, "SIGKILL"):
            delattr(signal, "SIGKILL")
        if had_sigstop:
            signal.SIGSTOP = old_sigstop
        elif hasattr(signal, "SIGSTOP"):
            delattr(signal, "SIGSTOP")
    assert result.proven is True
    assert result.ancestry_lost is False
    assert killed == [grandchild, child, root]
    assert alive == set()


def _require_linux_namespace() -> None:
    if sys.platform == "linux":
        return
    if os.environ.get("CI"):
        pytest.fail("Linux CI must run the nested owned-process proof")
    pytest.skip("POSIX nested tree proof runs on Linux CI; this host is not Linux")


@pytest.mark.asyncio
async def test_linux_nested_owned_tree_reaps_every_descendant(mi_db, tmp_path: Path) -> None:
    del mi_db
    _require_linux_namespace()
    grandchild_path = tmp_path / "grandchild.txt"
    code = (
        "import pathlib,subprocess,sys,time\n"
        "path=pathlib.Path(sys.argv[1])\n"
        "stopped=[sys.executable,'-c',"
        "'import os,signal,time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(60)']\n"
        "running=[sys.executable,'-c','import time; time.sleep(60)']\n"
        "kids=[]\n"
        "for cmd in (stopped, running, running):\n"
        "    kids.append(subprocess.Popen(cmd,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))\n"
        "path.write_text(','.join(str(kid.pid) for kid in kids))\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(grandchild_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **owned_subprocess_kwargs(),
    )
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    permit = HostInvocationPermit()
    handle = None
    child_identities: list[ProcessIdentity] = []
    try:
        deadline = time.monotonic() + 5
        while not grandchild_path.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        child_text = grandchild_path.read_text(encoding="utf-8")
        child_identities = [
            identity_for_pid(int(part)) for part in child_text.split(",") if part.strip()
        ]
        assert len(child_identities) == 3
        parent_identity = bind_owned_session(identity_for_pid(parent.pid))
        assert parent_identity is not None
        assert process_is_alive(parent_identity)
        assert all(process_is_alive(item) for item in child_identities)
        handle = await permit.acquire(entrypoint="round5-linux-tree")
        assert await permit.register_owned_runtime(handle, [parent_identity]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.read_active_witness() == 1
        assert await permit.try_acquire(entrypoint="round5-while-nested") is None
        assert await permit.terminate_owned_tree(handle) is True
        parent.wait(timeout=5)
        assert process_liveness(parent_identity) == Liveness.DEAD
        assert all(process_liveness(item) == Liveness.DEAD for item in child_identities)
        assert stranger.poll() is None
        assert await permit.release(handle) is True
        handle = None
        assert await permit.read_active_witness() == 0
    finally:
        for item in child_identities:
            if process_liveness(item) == Liveness.ALIVE:
                terminate_process_tree(item)
        if not child_identities and grandchild_path.is_file():
            for part in grandchild_path.read_text(encoding="utf-8").split(","):
                try:
                    early_pid = int(part or "0")
                except ValueError:
                    early_pid = 0
                if early_pid <= 0:
                    continue
                early = identity_for_pid(early_pid)
                if process_liveness(early) == Liveness.ALIVE:
                    terminate_process_tree(early)
        for proc in (parent, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if handle is not None:
            await permit.release(handle)


@pytest.mark.asyncio
async def test_cancelled_and_hanging_closers_still_clean_the_permit(mi_db) -> None:
    del mi_db
    permit = HostInvocationPermit()
    active = 0

    class _Hang:
        async def aclose(self) -> None:
            await asyncio.sleep(30)

    handle = await permit.acquire(entrypoint="round5-cancel-close")
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=_Hang(), permit=permit, entrypoint="round5-cancel-close", shared_handle=handle
    )
    task = asyncio.create_task(wrapper.aclose())
    await asyncio.sleep(0.03)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert await permit.read_active_witness() == 0
    assert active == 0

    class _Suppress:
        def __init__(self) -> None:
            self.stop = False

        async def aclose(self) -> None:
            nonlocal active
            active += 1
            try:
                while not self.stop:
                    try:
                        await asyncio.sleep(0.05)
                    except asyncio.CancelledError:
                        continue
            finally:
                active -= 1

    suppressor = _Suppress()
    handle = await permit.acquire(entrypoint="round5-suppress-close")
    held = PermitBoundMeetingEssenceProvider(
        inner=suppressor,
        permit=permit,
        entrypoint="round5-suppress-close",
        shared_handle=handle,
    )
    started = time.monotonic()
    assert await held.aclose() is False
    assert time.monotonic() - started < 2
    assert await permit.read_active_witness() == 1
    assert await permit.try_acquire(entrypoint="round5-while-closer") is None
    assert active == 1
    suppressor.stop = True
    deadline = time.monotonic() + 2
    while await permit.read_active_witness() != 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert active == 0
    assert await permit.read_active_witness() == 0

    class _Boom:
        async def aclose(self) -> None:
            raise RuntimeError("closer failed")

    handle = await permit.acquire(entrypoint="round5-runtime-close")
    broken = PermitBoundMeetingEssenceProvider(
        inner=_Boom(), permit=permit, entrypoint="round5-runtime-close", shared_handle=handle
    )
    with pytest.raises(RuntimeError, match="closer failed"):
        await broken.aclose()
    assert await permit.read_active_witness() == 0


@pytest.mark.asyncio
async def test_explicit_retry_uses_the_same_depth(mi_db, monkeypatch) -> None:
    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_MAX_DEPTH", "1")
    get_settings.cache_clear()
    await _sessions(_SESSION, _OTHER, _THIRD)
    await _silence_other_sessions(_SESSION, _OTHER, _THIRD)
    dispatch_a, dispatch_b = uuid4(), uuid4()
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            delete(MISummaryDispatch).where(
                MISummaryDispatch.session_id.in_((_SESSION, _OTHER, _THIRD))
            )
        )
        for session_id, intent in (
            (_SESSION, "open"),
            (_OTHER, "open"),
            (_THIRD, "cancelled"),
        ):
            state = await db.get(MISummarySessionState, session_id)
            if state is None:
                db.add(MISummarySessionState(session_id=session_id, work_intent=intent))
            else:
                state.work_intent = intent
        db.add(
            MISummaryDispatch(
                id=dispatch_a,
                session_id=_SESSION,
                client_request_id=uuid4(),
                status="pending",
                disposition="awaiting_material",
            )
        )
        db.add(
            MISummaryDispatch(
                id=dispatch_b,
                session_id=_OTHER,
                client_request_id=uuid4(),
                status="pending",
                disposition="runnable",
            )
        )
    async with get_sessionmaker()() as db:
        before = await _runnable_count(db)
        await retry_summary_dispatch(db, dispatch_id=dispatch_a, session_id=_SESSION)
        await db.commit()
        after = await _runnable_count(db)
        parked = await db.get(MISummaryDispatch, dispatch_a)
    assert before == 1
    assert after == 1
    assert parked is not None and parked.capacity_deferred is True
    assert parked.disposition != "runnable"
    async with get_sessionmaker()() as db:
        await retry_summary_dispatch(db, dispatch_id=dispatch_a, session_id=_SESSION)
        await db.commit()
        rows = int(
            await db.scalar(
                select(func.count())
                .select_from(MISummaryDispatch)
                .where(MISummaryDispatch.session_id == _SESSION)
            )
            or 0
        )
        assert await _runnable_count(db) == 1
    assert rows == 1
    async with get_sessionmaker()() as db, db.begin():
        claimed = await db.get(MISummaryDispatch, dispatch_b)
        assert claimed is not None
        claimed.status = MISummaryDispatchStatus.CLAIMED.value
        claimed.claim_token = "held"
    async with get_sessionmaker()() as db:
        await retry_summary_dispatch(db, dispatch_id=dispatch_a, session_id=_SESSION)
        await db.commit()
        assert await _runnable_count(db) == 1
        still = await db.get(MISummaryDispatch, dispatch_a)
    assert still is not None and still.disposition != "runnable"

    async def _retry() -> None:
        async with get_sessionmaker()() as db:
            await retry_summary_dispatch(db, dispatch_id=dispatch_a, session_id=_SESSION)
            await db.commit()

    async def _submit() -> None:
        async with get_sessionmaker()() as db:
            await submit_summary_work(db, session_id=_THIRD, client_request_id=uuid4())
            await db.commit()

    await asyncio.gather(_retry(), _submit())
    async with get_sessionmaker()() as db:
        assert await _runnable_count(db) <= 1
        cancelled = await db.get(MISummarySessionState, _THIRD)
    assert cancelled is not None and cancelled.work_intent == "cancelled"
    async with get_sessionmaker()() as db, db.begin():
        other = await db.get(MISummaryDispatch, dispatch_b)
        assert other is not None
        other.status = MISummaryDispatchStatus.CANCELLED.value
        other.claim_token = None
        state = await db.get(MISummarySessionState, _OTHER)
        assert state is not None
        state.work_intent = "cancelled"
    async with get_sessionmaker()() as db:
        await reconcile_summary_work(db)
        await db.commit()
        revived = await db.get(MISummaryDispatch, dispatch_a)
        left = await db.get(MISummarySessionState, _OTHER)
    assert revived is not None and revived.disposition == "runnable"
    assert left is not None and left.work_intent == "cancelled"
    async with get_sessionmaker()() as db:
        await retry_summary_dispatch(db, dispatch_id=dispatch_b, session_id=_OTHER)
        await db.commit()
        resumed = await db.get(MISummaryDispatch, dispatch_b)
        assert await _runnable_count(db) == 1
    assert resumed is not None and resumed.capacity_deferred is True
    assert resumed.disposition != "runnable"
    async with get_sessionmaker()() as db:
        await cancel_summary_dispatch(db, dispatch_id=dispatch_a, session_id=_SESSION)
        await db.commit()
        await reconcile_summary_work(db)
        await db.commit()
        opened = await db.get(MISummaryDispatch, dispatch_b)
        still_cancelled = await db.get(MISummarySessionState, _SESSION)
    assert opened is not None and opened.disposition == "runnable"
    assert still_cancelled is not None and still_cancelled.work_intent == "cancelled"


@pytest.mark.asyncio
async def test_foreign_namespace_is_not_cleared_by_a_local_pid(mi_db) -> None:
    del mi_db
    local = local_process_namespace()
    assert local
    host_authority.write_owned([])
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = identity_for_pid(sleeper.pid)
    permit = HostInvocationPermit()
    try:
        sleeper.kill()
        sleeper.wait(timeout=5)
        async with get_sessionmaker()() as db, db.begin():
            row = await db.get(MIHostInvocationLease, 1)
            if row is None:
                row = MIHostInvocationLease(id=1)
                db.add(row)
                await db.flush()
            row.holder_token = "foreign"
            row.holder_pid = identity.pid
            row.holder_pid_created_ms = identity.created_ms
            row.holder_start_ticks = identity.start_ticks
            row.holder_namespace = "linux:other-boot:pid:[1]"
            row.holder_state = "held"
            row.active_invocations = 1
            row.owned_runtime_json = "[]"
            row.spawns_process_tree = False
            row.termination_acked = False
        assert process_liveness(identity) == Liveness.DEAD
        assert await permit.recover_proven_dead() is False
        assert await permit.try_acquire(entrypoint="round5-foreign") is None
        assert await permit.read_active_witness() == 1
        async with get_sessionmaker()() as db, db.begin():
            row = await db.get(MIHostInvocationLease, 1)
            assert row is not None
            row.holder_namespace = local
        assert await permit.recover_proven_dead() is True
        assert await permit.read_active_witness() == 0
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        await _free_lease()


@pytest.mark.asyncio
async def test_dns_alias_matches_the_server_and_a_wrong_alias_does_not(mi_db, monkeypatch) -> None:
    del mi_db
    started = time.monotonic()
    assert await _endpoint_matches("recantor-missing.invalid", "127.0.0.1", port=55472) is False
    assert time.monotonic() - started < 2
    async with get_sessionmaker()() as db:
        observed = await db.scalar(text("SELECT inet_server_addr()::text"))
    assert observed
    identity_file = host_authority.identity_path()
    backup = identity_file.read_text(encoding="utf-8") if identity_file.is_file() else None
    alias = "recantor-pg-alias"
    wrong = "recantor-pg-wrong"
    try:
        if not _map_alias(alias, str(observed).split("%", 1)[0]):
            if os.environ.get("CI") and sys.platform == "linux":
                pytest.fail("Linux CI could not resolve the PostgreSQL DNS alias")
            permit = HostInvocationPermit()
            assert await permit.authority_reachable() is True
            return
        _map_alias(wrong, "203.0.113.55")
        url = make_url(get_settings().database_url).set(host=alias)
        monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
        get_settings.cache_clear()
        if identity_file.is_file():
            identity_file.unlink()
        permit = HostInvocationPermit()

        class _Inner:
            spawns_process_tree = False

            async def run_stage(self, **kwargs: object) -> str:
                del kwargs
                return "ran"

        wrapper = PermitBoundMeetingEssenceProvider(
            inner=_Inner(), permit=permit, entrypoint="round5-dns-alias"
        )
        ran = await wrapper.run_stage(
            operation=None,  # type: ignore[arg-type]
            request=None,  # type: ignore[arg-type]
            job=None,  # type: ignore[arg-type]
            prompt_bundle=None,  # type: ignore[arg-type]
        )
        assert ran == "ran"
        assert await wrapper.aclose() is True
        wrong_url = make_url(get_settings().database_url).set(host=wrong)
        monkeypatch.setenv("DATABASE_URL", wrong_url.render_as_string(hide_password=False))
        get_settings.cache_clear()
        refused = HostInvocationPermit()
        with pytest.raises(HostInvocationBusy):
            refused_wrapper = PermitBoundMeetingEssenceProvider(
                inner=_Inner(), permit=refused, entrypoint="round5-wrong-alias"
            )
            await refused_wrapper.run_stage(
                operation=None,  # type: ignore[arg-type]
                request=None,  # type: ignore[arg-type]
                job=None,  # type: ignore[arg-type]
                prompt_bundle=None,  # type: ignore[arg-type]
            )
    finally:
        get_settings.cache_clear()
        if backup is None:
            if identity_file.is_file():
                identity_file.unlink()
        else:
            identity_file.write_text(backup, encoding="utf-8")
        await _free_lease()


@pytest.mark.asyncio
async def test_metrics_follow_input_identity_not_a_mutable_clock(mi_db) -> None:
    del mi_db
    session_id = uuid4()
    await _sessions(session_id)
    segment_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            TranscriptSegment(
                id=segment_id,
                session_id=session_id,
                sequence=1,
                producer_key=f"metrics-{segment_id}",
                start_ms=0,
                end_ms=400,
                text="Sama batasnya.",
                language="id",
            )
        )
    async with get_sessionmaker()() as db:
        before = await load_material_snapshot(
            db, session_id=session_id, publication_boundary_sequence=1
        )
    utterance_id = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            TranscriptionUtterance(
                id=utterance_id,
                session_id=session_id,
                sequence=1,
                producer_key=f"utt-{utterance_id}",
                start_ms=0,
                end_ms=400,
                content_type="audio/wav",
                sha256="a" * 64,
                byte_length=8,
                storage_key=f"utt/{utterance_id}",
            )
        )
        await db.flush()
        db.add(
            STTJob(
                utterance_id=utterance_id,
                session_id=session_id,
                state=STTJobState.PENDING.value,
            )
        )
    async with get_sessionmaker()() as db:
        after = await load_material_snapshot(
            db, session_id=session_id, publication_boundary_sequence=1
        )
    assert before.source_fingerprint == after.source_fingerprint
    assert before.coverage_fingerprint != after.coverage_fingerprint
    assert before.stt_fingerprint != after.stt_fingerprint
    cold = _manifest(segment_id, session_id, stt_pending=False)
    changed = _manifest(segment_id, session_id, stt_pending=True)
    cold_input = metrics_input_fingerprint(manifest=cold)
    changed_input = metrics_input_fingerprint(manifest=changed)
    assert cold_input != changed_input
    config = "c" * 64
    queued = datetime(2026, 10, 15, tzinfo=UTC)
    finished = datetime(2026, 10, 15, 0, 1, tzinfo=UTC)
    async with get_sessionmaker()() as db, db.begin():
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=queued,
            finished_at=finished,
            result=_result(cold, calls=1),
            provider_calls=1,
            cache_hits=0,
            retries=1,
            source_fingerprint=before.source_fingerprint,
            config_fingerprint=config,
            input_fingerprint=cold_input,
            job_id="job-cold",
            attempt_id="attempt-1",
        )
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=datetime(2026, 10, 15, 0, 5, tzinfo=UTC),
            finished_at=datetime(2026, 10, 15, 0, 6, tzinfo=UTC),
            result=_result(cold, calls=1),
            provider_calls=1,
            cache_hits=0,
            retries=1,
            source_fingerprint=before.source_fingerprint,
            config_fingerprint=config,
            input_fingerprint=cold_input,
            job_id="job-cold",
            attempt_id="attempt-1",
        )
    async with get_sessionmaker()() as db:
        cold_row = await db.scalar(
            select(MISummaryMetrics).where(
                MISummaryMetrics.session_id == session_id,
                MISummaryMetrics.input_fingerprint == cold_input,
            )
        )
    assert cold_row is not None
    assert cold_row.original_queued_at == queued
    assert int(cold_row.provider_calls) == 1
    assert int(cold_row.retries) == 1
    assert int(cold_row.quantum_count) == 1
    assert cold_row.observed_tokens is None
    async with get_sessionmaker()() as db, db.begin():
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=datetime(2026, 10, 15, 1, tzinfo=UTC),
            finished_at=datetime(2026, 10, 15, 1, 1, tzinfo=UTC),
            result=_result(changed, calls=2, failed=True),
            provider_calls=2,
            cache_hits=0,
            retries=1,
            source_fingerprint=before.source_fingerprint,
            config_fingerprint=config,
            input_fingerprint=changed_input,
            job_id="job-stt",
            attempt_id="attempt-stt",
        )
        await persist_integrated_metrics(
            db,
            session_id=session_id,
            queued_at=queued,
            finished_at=finished,
            result=None,
            provider_calls=0,
            cache_hits=1,
            retries=0,
            source_fingerprint=before.source_fingerprint,
            config_fingerprint=config,
            input_fingerprint=cold_input,
            job_id="job-cold",
            attempt_id="attempt-cache",
            served_from_cache=True,
        )
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
            )
        )
        cold_row = await db.scalar(
            select(MISummaryMetrics).where(
                MISummaryMetrics.session_id == session_id,
                MISummaryMetrics.input_fingerprint == cold_input,
            )
        )
        changed_row = await db.scalar(
            select(MISummaryMetrics).where(
                MISummaryMetrics.session_id == session_id,
                MISummaryMetrics.input_fingerprint == changed_input,
            )
        )
    assert len(rows) == 2
    assert cold_row is not None and changed_row is not None
    assert cold_row.original_queued_at == queued
    assert cold_row.final_result_at is None
    assert int(cold_row.cache_hits) == 1
    assert int(cold_row.provider_calls) == 1
    assert changed_row.original_queued_at != queued
    assert int(changed_row.provider_calls) == 2
    assert int(changed_row.retries) == 1
    assert int(changed_row.failures) == 1
    assert float(changed_row.extract_ms) > 0
    assert changed_row.observed_tokens is None


def _manifest(segment_id: UUID, session_id: UUID, *, stt_pending: bool):
    from recantor.meeting_essence.contracts import (
        ManifestSegmentEntry,
        SourceCoverageFlags,
        SourceSnapshotManifest,
    )

    text_value = "Sama batasnya."
    draft = SourceSnapshotManifest(
        session_id=session_id,
        snapshot_id="snap_round5metricsdraft",
        snapshot_content_hash="b" * 64,
        publication_boundary_sequence=1,
        segments=[
            ManifestSegmentEntry(
                id=segment_id,
                sequence=1,
                start_ms=0,
                end_ms=400,
                text=text_value,
                language="id",
                content_hash=segment_content_hash(text_value),
            )
        ],
        coverage=SourceCoverageFlags(
            transcript_publication_complete=True,
            stt_backlog_pending=stt_pending,
        ),
    )
    return finalize_snapshot_manifest(draft)


def _result(manifest, *, calls: int, failed: bool = False) -> EngineRunResult:
    started = datetime(2026, 10, 15, 0, 0, 1, tzinfo=UTC)
    traces = []
    for index in range(calls):
        traces.append(
            StageCallTrace(
                operation=StageOperation.EXTRACT,
                started_at=started.replace(microsecond=index + 1),
                finished_at=started.replace(microsecond=index + 2),
                error_category="timeout" if failed and index == 0 else None,
                attempt_count=index + 1,
                serialized_utf8_bytes=32,
            )
        )
    trace = EngineRunTrace(
        manifest=manifest,
        prompt_cache_key="prompt",
        schema_identity="schema",
        algorithm_identity="algorithm",
        stage_calls=traces,
    )
    return EngineRunResult(
        manifest=manifest,
        layered_result=None,
        checkpoint=EngineCheckpoint(),
        trace=trace,
        processing_status=ProcessingJobStatus.PARTIAL,
    )


async def _silence_other_sessions(*keep: UUID) -> None:
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            update(MISummaryDispatch)
            .where(
                MISummaryDispatch.session_id.notin_(keep),
                MISummaryDispatch.status.in_(("pending", "claimed")),
            )
            .values(status=MISummaryDispatchStatus.CANCELLED.value, claim_token=None)
        )


async def _sessions(*session_ids: UUID) -> None:
    async with get_sessionmaker()() as db, db.begin():
        for session_id in session_ids:
            if await db.get(RecordingSession, session_id) is None:
                db.add(
                    RecordingSession(
                        id=session_id,
                        client_request_id=uuid4(),
                        kind=SessionKind.LIVE.value,
                        state=SessionState.RECORDING.value,
                    )
                )


async def _runnable_count(db) -> int:
    counted = await db.scalar(
        select(func.count())
        .select_from(MISummaryDispatch)
        .where(
            or_(
                MISummaryDispatch.status == MISummaryDispatchStatus.CLAIMED.value,
                MISummaryDispatch.disposition == "runnable",
            ),
            MISummaryDispatch.status.in_(("pending", "claimed")),
        )
    )
    return int(counted or 0)


async def _free_lease() -> None:
    host_authority.write_owned([])
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(
            update(MIHostInvocationLease)
            .where(MIHostInvocationLease.id == 1)
            .values(
                holder_token=None,
                holder_pid=None,
                holder_pid_created_ms=None,
                holder_start_ticks=None,
                holder_namespace=None,
                holder_entrypoint=None,
                holder_state="free",
                active_invocations=0,
                owned_runtime_json="[]",
                termination_acked=False,
                quarantine_reason=None,
                invocation_admitted=False,
                spawns_process_tree=False,
            )
        )


def _map_alias(name: str, ip: str) -> bool:
    if _resolves_to(name, ip):
        return True
    if not os.environ.get("CI") or sys.platform == "win32":
        return False
    line = f"{ip} {name}\n"
    completed = subprocess.run(
        ["sudo", "tee", "-a", "/etc/hosts"],
        input=line,
        text=True,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr)
    return _resolves_to(name, ip)


def _resolves_to(name: str, ip: str) -> bool:
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    wanted = ip.split("%", 1)[0]
    return any(str(item[4][0]).split("%", 1)[0] == wanted for item in infos)
