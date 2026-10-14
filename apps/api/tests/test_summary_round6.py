"""Negative proofs for #73 correction round 6. Fake processes only; no live model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine.url import make_url
from summary_broker_provider import invocation_witness, note_owned_invocation

from recantor.db import get_engine, get_sessionmaker
from recantor.meeting_essence.scheduling import host_authority, host_permit
from recantor.meeting_essence.scheduling.host_permit import (
    HostInvocationPermit,
    _canonical_host,
    _connected_endpoint,
)
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease, MISummaryMetrics
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import (
    ContainmentResult,
    Liveness,
    ProcessIdentity,
    bind_owned_session,
    identity_for_pid,
    owned_subprocess_kwargs,
    process_liveness,
    terminate_process_tree,
)
from recantor.models import RecordingSession, SessionKind, SessionState, TranscriptSegment
from recantor.settings import get_settings


def test_posix_fork_during_kill_is_not_reported_dead(monkeypatch) -> None:
    """A child forked by an earlier signal is signalled. Stop is not a proof here.

    This graph removes the process on every signal, so the freeze never becomes
    visible. The new child must still be signalled, and the result stays unproven.
    No OS signals and not a Linux execution.
    """
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_sigkill = hasattr(signal, "SIGKILL")
    old_sigkill = getattr(signal, "SIGKILL", None)
    had_sigstop = hasattr(signal, "SIGSTOP")
    old_sigstop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    root, child, new = 101001, 101002, 101003
    alive = {root, child}
    killed: list[int] = []

    def kill(pid: int, _sig: int) -> None:
        if pid == child:
            alive.add(new)
        killed.append(pid)
        alive.discard(pid)

    def descendants(pid: int) -> list[int]:
        if pid == root and root in alive:
            return [pid_ for pid_ in (child, new) if pid_ in alive]
        return []

    monkeypatch.setattr(module.os, "kill", kill)
    monkeypatch.setattr(module, "_posix_descendants", descendants)
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda identity: module.Liveness.ALIVE if identity.pid in alive else module.Liveness.DEAD,
    )
    monkeypatch.setattr(
        module,
        "_posix_proc_stat",
        lambda pid: ("S", 1) if pid in alive else (None, None),
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
    assert new not in alive
    assert result.proven is False
    assert result.ancestry_lost is True
    assert killed == [child, new, root]


def test_invocation_counter_survives_a_dead_worker_pid(tmp_path: Path) -> None:
    path = tmp_path / "invocation-counter.json"
    path.write_text(json.dumps({"active": 0, "peak": 0, "pid": 999999}), encoding="utf-8")
    first = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    second = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        note_owned_invocation(path, identity_for_pid(first.pid))
        witness = invocation_witness(path)
        assert witness["present"] is True
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) == 1
        note_owned_invocation(path, identity_for_pid(first.pid))
        assert int(invocation_witness(path)["peak"]) == 1
        note_owned_invocation(path, identity_for_pid(second.pid))
        raced = invocation_witness(path)
        assert int(raced["live"]) == 2
        assert int(raced["peak"]) == 2
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_double_cancel_does_not_release_a_suppressing_closer() -> None:
    state = {"active": 0, "released_while_active": False, "released": False, "quarantined": False}
    stop = asyncio.Event()
    began = asyncio.Event()

    class Inner:
        async def aclose(self) -> None:
            state["active"] += 1
            began.set()
            try:
                while not stop.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            finally:
                state["active"] -= 1

    class Permit:
        async def release(self, handle: object) -> bool:
            del handle
            state["released_while_active"] = state["active"] > 0
            state["released"] = True
            return True

        async def quarantine_unproven(self, handle: object, reason: str) -> bool:
            del handle, reason
            state["quarantined"] = True
            return False

    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(),
        permit=Permit(),  # type: ignore[arg-type]
        entrypoint="controlled",
        shared_handle=object(),  # type: ignore[arg-type]
    )
    task = asyncio.create_task(wrapper.aclose())
    await began.wait()
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert state["active"] == 1
    assert state["released"] is False
    assert state["released_while_active"] is False
    await asyncio.sleep(0.7)
    assert state["active"] == 1
    assert state["released"] is False
    assert state["quarantined"] is True
    stop.set()
    if wrapper._closer_task is not None:
        await asyncio.wait_for(wrapper._closer_task, 1)
    if wrapper._reaper is not None:
        await asyncio.wait_for(wrapper._reaper, 1)
    assert state["active"] == 0


@pytest.mark.asyncio
async def test_real_permit_stays_busy_across_a_repeated_close_cancel(mi_db) -> None:
    del mi_db
    active = 0
    stop = asyncio.Event()
    began = asyncio.Event()

    class Inner:
        async def aclose(self) -> None:
            nonlocal active
            active += 1
            began.set()
            try:
                while not stop.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            finally:
                active -= 1

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round6-double-cancel")
    await permit.mark_admitted(handle, spawns_process_tree=False)
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(),
        permit=permit,
        entrypoint="round6-double-cancel",
        shared_handle=handle,
    )
    task = asyncio.create_task(wrapper.aclose())
    try:
        await began.wait()
        task.cancel()
        await asyncio.sleep(0.03)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.7)
        assert active == 1
        assert await permit.read_active_witness() == 1
        assert await permit.try_acquire(entrypoint="round6-while-closer") is None
    finally:
        stop.set()
        if wrapper._closer_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(wrapper._closer_task, 1)
        if wrapper._reaper is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(wrapper._reaper, 1)
        await _free_lease()
    assert active == 0


@pytest.mark.asyncio
async def test_open_containment_blocks_recovery_after_the_root_is_dead(mi_db, monkeypatch) -> None:
    del mi_db
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    permit = HostInvocationPermit()
    handle = None
    try:
        identity = identity_for_pid(sleeper.pid)
        handle = await permit.try_acquire(entrypoint="round6-containment", identity=identity)
        assert handle is not None
        assert await permit.register_owned_runtime(handle, [identity]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)

        def lost(item: ProcessIdentity) -> ContainmentResult:
            return ContainmentResult(
                proven=False,
                identities=(
                    item,
                    ProcessIdentity(pid=101003, start_ticks=1, namespace=item.namespace),
                ),
                ancestry_lost=True,
            )

        monkeypatch.setattr(host_permit, "contain_owned_tree", lost)
        assert await permit.terminate_owned_tree(handle) is False
        sleeper.kill()
        sleeper.wait(timeout=5)
        assert process_liveness(identity) == Liveness.DEAD
        assert await permit.recover_proven_dead() is False
        permit._release_os(handle.token)
        competing = await HostInvocationPermit().try_acquire(entrypoint="round6-after-root-death")
        assert competing is None
        assert await permit.read_active_witness() == 1
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        if handle is not None:
            permit._release_os(handle.token)
        await _free_lease()


@pytest.mark.asyncio
async def test_connected_endpoint_and_bridge_alias_use_the_observed_address(
    mi_db, monkeypatch
) -> None:
    del mi_db
    async with get_sessionmaker()() as db:
        peer = await _connected_endpoint(db)
    assert peer
    assert _canonical_host(peer) == "127.0.0.1"
    identity_file = host_authority.identity_path()
    backup = identity_file.read_text(encoding="utf-8") if identity_file.is_file() else None
    real_observation = host_permit._server_observation

    async def bridge(db: object) -> tuple[str, str | None, int | None, str]:
        database, _server, port, system_identifier = await real_observation(db)  # type: ignore[arg-type]
        return database, "172.18.0.2", port, system_identifier

    current_url = os.environ["DATABASE_URL"]
    try:
        if identity_file.is_file():
            identity_file.unlink()
        monkeypatch.setattr(host_permit, "_server_observation", bridge)
        permit = HostInvocationPermit()
        assert await permit.authority_reachable() is True
        stored = host_authority.read_cluster_authority()
        assert stored is not None
        stored_payload = json.loads(identity_file.read_text(encoding="utf-8"))
        stored_payload["host"] = "postgres"
        identity_file.write_text(json.dumps(stored_payload), encoding="utf-8")
        assert await permit.authority_reachable() is True
        stored_payload["observed_host"] = "203.0.113.55"
        identity_file.write_text(json.dumps(stored_payload), encoding="utf-8")
        assert await permit.authority_reachable() is False
        monkeypatch.undo()
        wrong = make_url(current_url).set(host="recantor-missing.invalid")
        os.environ["DATABASE_URL"] = wrong.render_as_string(hide_password=False)
        get_settings.cache_clear()
        if identity_file.is_file():
            identity_file.unlink()
        started = time.monotonic()
        assert await HostInvocationPermit().authority_reachable() is False
        assert time.monotonic() - started < 2
    finally:
        os.environ["DATABASE_URL"] = current_url
        get_settings.cache_clear()
        if backup is None:
            if identity_file.is_file():
                identity_file.unlink()
        else:
            identity_file.write_text(backup, encoding="utf-8")
        await _free_lease()


@pytest.mark.asyncio
async def test_populated_metrics_downgrade_aggregates_duplicate_inputs(mi_db) -> None:
    del mi_db
    from meeting_intelligence_db import alembic_config

    from alembic import command

    session_id = uuid4()
    segment_id = uuid4()
    source = "s" * 64
    config = "c" * 64
    queued = datetime(2026, 10, 15, tzinfo=UTC)
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            RecordingSession(
                id=session_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.RECORDING.value,
            )
        )
        await db.flush()
        db.add(
            TranscriptSegment(
                id=segment_id,
                session_id=session_id,
                sequence=1,
                producer_key=f"round6-{segment_id}",
                start_ms=0,
                end_ms=400,
                text="Turun skema.",
                language="id",
            )
        )
        db.add(
            MISummaryMetrics(
                session_id=session_id,
                input_fingerprint="a" * 64,
                config_fingerprint=config,
                source_fingerprint=source,
                original_queued_at=queued,
                first_useful_at=datetime(2026, 10, 15, 0, 2, tzinfo=UTC),
                provider_calls=1,
                retries=1,
                cache_hits=1,
                failures=0,
                quantum_count=1,
                observed_event_keys=json.dumps(["cold"]),
                extract_ms=10,
            )
        )
        db.add(
            MISummaryMetrics(
                session_id=session_id,
                input_fingerprint="b" * 64,
                config_fingerprint=config,
                source_fingerprint=source,
                original_queued_at=datetime(2026, 10, 15, 1, tzinfo=UTC),
                first_useful_at=datetime(2026, 10, 15, 1, 2, tzinfo=UTC),
                final_result_at=datetime(2026, 10, 15, 1, 3, tzinfo=UTC),
                provider_calls=2,
                retries=1,
                cache_hits=0,
                failures=1,
                quantum_count=2,
                observed_event_keys=json.dumps(["stt"]),
                extract_ms=5,
            )
        )
    async with get_sessionmaker()() as db:
        transcripts_before = int(
            await db.scalar(
                select(func.count())
                .select_from(TranscriptSegment)
                .where(TranscriptSegment.session_id == session_id)
            )
            or 0
        )
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        command.downgrade(alembic_config(), "0015")
        async with get_sessionmaker()() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT provider_calls, retries, cache_hits, failures, quantum_count, "
                        "extract_ms, observed_tokens, observed_event_keys, original_queued_at "
                        "FROM mi_summary_metrics WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id},
                )
            ).all()
            transcripts_after = int(
                await db.scalar(
                    select(func.count())
                    .select_from(TranscriptSegment)
                    .where(TranscriptSegment.session_id == session_id)
                )
                or 0
            )
        assert transcripts_after == transcripts_before == 1
        assert len(rows) == 1
        calls, retries, hits, failures, quanta, extract_ms, tokens, events, queued_at = rows[0]
        assert int(calls) == 3
        assert int(retries) == 2
        assert int(hits) == 1
        assert int(failures) == 1
        assert int(quanta) == 3
        assert float(extract_ms) == 15
        assert tokens is None
        assert set(json.loads(events)) == {"cold", "stt"}
        assert queued_at == queued
        command.upgrade(alembic_config(), "head")
        async with get_sessionmaker()() as db:
            restored = list(
                await db.scalars(
                    select(MISummaryMetrics).where(MISummaryMetrics.session_id == session_id)
                )
            )
        assert len(restored) == 1
        assert restored[0].input_fingerprint == source
        assert int(restored[0].provider_calls) == 3
        assert restored[0].observed_tokens is None
    finally:
        command.upgrade(alembic_config(), "head")
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


@pytest.mark.asyncio
async def test_linux_crashed_root_keeps_a_live_descendant(mi_db, tmp_path: Path) -> None:
    del mi_db
    if sys.platform != "linux":
        if os.environ.get("CI"):
            pytest.fail("Linux CI must prove a crashed root does not clear live descendants")
        pytest.skip("crashed-root descendant proof runs on Linux")
    child_path = tmp_path / "child.txt"
    gate = tmp_path / "gate.txt"
    code = (
        "import pathlib,subprocess,sys,time\n"
        "gate=pathlib.Path(sys.argv[1]); child_path=pathlib.Path(sys.argv[2])\n"
        "deadline=time.time()+30\n"
        "while not gate.is_file() and time.time()<deadline:\n"
        "    time.sleep(0.05)\n"
        "if not gate.is_file():\n"
        "    raise SystemExit(0)\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "child_path.write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(gate), str(child_path)],
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
    child_identity: ProcessIdentity | None = None
    try:
        parent_identity = bind_owned_session(identity_for_pid(parent.pid))
        assert parent_identity is not None
        handle = await permit.acquire(entrypoint="round6-crashed-root")
        assert await permit.register_owned_runtime(handle, [parent_identity]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        gate.write_text("open", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not child_path.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        child_identity = identity_for_pid(int(child_path.read_text(encoding="utf-8")))
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        assert process_liveness(parent_identity) == Liveness.DEAD
        assert process_liveness(child_identity) == Liveness.ALIVE
        assert await permit.release(handle) is False
        assert await permit.read_active_witness() == 1
        assert await permit.try_acquire(entrypoint="round6-while-descendant") is None
        assert stranger.poll() is None
        assert terminate_process_tree(parent_identity) is True
        assert process_liveness(child_identity) == Liveness.DEAD
        assert await permit.release(handle) is True
        handle = None
    finally:
        if child_identity is not None and process_liveness(child_identity) == Liveness.ALIVE:
            terminate_process_tree(child_identity)
        for proc in (parent, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if handle is not None:
            await permit.release(handle)
        await _free_lease()


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
