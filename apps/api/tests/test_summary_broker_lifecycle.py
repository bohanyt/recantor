"""Mandatory Redis 8 / Celery proof for the summary worker. Skipped only without Redis."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from recantor.db import get_sessionmaker
from recantor.meeting_essence.persistence.checkpoint_store import deserialize_checkpoint
from recantor.meeting_essence.persistence.models import MIArtifactVersion, MIAttemptCheckpoint
from recantor.meeting_essence.persistence.repository import MeetingIntelligenceRepository
from recantor.meeting_essence.scheduling.jobs import reconcile_summary_work
from recantor.meeting_essence.scheduling.models import (
    MISummaryDispatch,
    MISummaryDispatchStatus,
)
from recantor.meeting_essence.scheduling.tasks import ensure_summary_wake_capacity
from recantor.meeting_essence.scheduling.triggers import note_transcript_publication
from recantor.models import RecordingSession, SessionKind, SessionState, TranscriptSegment
from recantor.settings import get_settings


@pytest.mark.parametrize("record_id", ["a-01", "b-01", "c-01", "a-1"])
def test_broker_factory_validates_record_ids(record_id, tmp_path, monkeypatch) -> None:
    from pydantic import ValidationError
    from summary_broker_provider import factory

    control = tmp_path / "control.json"
    control.write_text(
        '{"log_path": '
        + _json(str(tmp_path / "calls.log"))
        + ', "records": ['
        + _record(record_id, uuid4(), "Bukti.", session_id=uuid4())
        + "]}",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECANTOR_SUMMARY_CONTROL", str(control))
    if len(record_id) < 4:
        with pytest.raises(ValidationError) as rejected:
            factory().build()
        assert rejected.value.errors()[0]["type"] == "string_too_short"
    else:
        assert factory().build().extraction_records[0].record_id == record_id


def _require_redis() -> None:
    from redis import Redis
    from redis.exceptions import RedisError

    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.4,
        socket_timeout=0.4,
    )
    try:
        client.ping()
    except (OSError, RedisError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"Redis 8 is required in CI for the summary worker proof: {exc}")
        pytest.skip("Redis is not installed on this host; CI runs the worker proof")
    finally:
        client.close()


@pytest.mark.asyncio
async def test_initial_broker_fixture_completes_and_reuses_without_redis(
    mi_db, tmp_path, monkeypatch
) -> None:
    from recantor.meeting_essence.scheduling.jobs import (
        SummaryExecutionStatus,
        execute_next_summary_job,
        submit_summary_work,
    )

    del mi_db
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    control = tmp_path / "control.json"
    log_path = tmp_path / "calls.log"
    a, b, sa, sb = await _seed_initial_broker_sessions()
    _write_initial_broker_control(control, log_path, a, b, sa, sb)
    monkeypatch.setenv("RECANTOR_SUMMARY_CONTROL", str(control))
    monkeypatch.setenv("RECANTOR_SUMMARY_PROVIDER_FACTORY", "summary_broker_provider:factory")
    get_settings.cache_clear()
    expected = {a: {"a-01"}, b: {"b-01"}}
    artifacts = {}
    for sid in expected:
        async with get_sessionmaker()() as db, db.begin():
            await submit_summary_work(db, session_id=sid, client_request_id=uuid4())
    for _ in range(40):
        async with get_sessionmaker()() as db:
            outcome = await execute_next_summary_job(db, entrypoint="offline-broker-fixture")
        sid = outcome.session_id
        if sid not in expected:
            await asyncio.sleep(0.05)
            continue
        assert sid in expected and sid not in artifacts
        assert outcome.status == SummaryExecutionStatus.SUCCEEDED, await _completion_diagnostic(sid)
        artifacts[sid] = await _assert_completed_artifact(sid, expected[sid])
        if len(artifacts) == len(expected):
            break
    assert artifacts.keys() == expected.keys()
    for sid, record_id in ((a, "a-01"), (b, "b-01")):
        calls = [
            line
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.startswith(str(sid))
        ]
        async with get_sessionmaker()() as db, db.begin():
            await submit_summary_work(db, session_id=sid, client_request_id=uuid4())
        for _ in range(40):
            async with get_sessionmaker()() as db:
                cached = await execute_next_summary_job(db, entrypoint="offline-broker-cache")
            if cached.session_id == sid:
                break
            await asyncio.sleep(0.05)
        assert cached.session_id == sid
        assert cached.status == SummaryExecutionStatus.SUCCEEDED
        assert cached.metrics.served_from_cache is True
        assert cached.metrics.provider_calls == 0
        assert await _assert_completed_artifact(sid, {record_id}) == artifacts[sid]
        assert [
            line
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.startswith(str(sid))
        ] == calls


async def _completion_diagnostic(session_id) -> str:
    async with get_sessionmaker()() as db:
        dispatch = await db.scalar(
            select(MISummaryDispatch)
            .where(MISummaryDispatch.session_id == session_id)
            .order_by(MISummaryDispatch.created_at.desc())
            .limit(1)
        )
        artifact = await db.scalar(
            select(MIArtifactVersion)
            .where(MIArtifactVersion.session_id == session_id)
            .order_by(MIArtifactVersion.created_at.desc())
            .limit(1)
        )
        return json.dumps(
            {
                "dispatch": None
                if dispatch is None
                else {
                    key: getattr(dispatch, key)
                    for key in ("status", "disposition", "failure_count", "last_error_code")
                },
                "artifact": None
                if artifact is None
                else {
                    "status": artifact.processing_status,
                    "limitation_codes": [
                        item["code"] for item in json.loads(artifact.limitations_json)
                    ],
                    "record_count": len(json.loads(artifact.records_ledger_json)),
                },
            },
            sort_keys=True,
        )


async def _assert_completed_artifact(session_id, record_ids: set[str]):
    async with get_sessionmaker()() as db:
        artifact = await db.scalar(
            select(MIArtifactVersion).where(
                MIArtifactVersion.session_id == session_id, MIArtifactVersion.is_current.is_(True)
            )
        )
        assert artifact is not None, await _completion_diagnostic(session_id)
        assert artifact.processing_status == "succeeded"
        assert artifact.is_stale is False
        assert json.loads(artifact.limitations_json) == []
        records = await MeetingIntelligenceRepository(db).load_ledger_records(
            artifact_id=artifact.id, session_id=session_id
        )
        assert {record.record_id for record in records} == record_ids
        assert len(records) == len(record_ids)
        for record in records:
            for evidence in record.evidence:
                segment = await db.get(TranscriptSegment, evidence.segment_id)
                assert segment.session_id == session_id
                assert (evidence.start_ms, evidence.end_ms) == (segment.start_ms, segment.end_ms)
        layered = json.loads(artifact.layered_result_json)
        coverage = layered["coverage"]
        for field in (
            "structural_check_status",
            "forward_semantic_check_status",
            "backward_semantic_check_status",
        ):
            assert coverage[field] == "completed"
        assert coverage["source_segments_enumerated"] == len(record_ids)
        assert coverage["source_segments_referenced"] == len(record_ids)
        body = await db.scalar(
            select(MIAttemptCheckpoint.body_json).where(
                MIAttemptCheckpoint.attempt_id == artifact.attempt_id
            )
        )
        checkpoint = deserialize_checkpoint(body)
        assert checkpoint.processing_status.value == "succeeded"
        assert checkpoint.extract_complete and checkpoint.check_complete
        assert checkpoint.reconcile_complete and checkpoint.render_complete
        assert not checkpoint.failed_check_fingerprints
        assert not checkpoint.pending_check_fingerprints
        assert checkpoint.acked_check_fingerprints
        assert {record.record_id for record in checkpoint.atomic_records} == record_ids
        return artifact.id


async def _wait_status(
    session_id, wanted: set[str], timeout: float, worker_log: Path | None = None
) -> str:
    import asyncio

    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        async with get_sessionmaker()() as db:
            status = await db.scalar(
                select(MISummaryDispatch.status).where(MISummaryDispatch.session_id == session_id)
            )
        if status in wanted:
            return str(status)
        await asyncio.sleep(0.2)
    detail = worker_log.read_text(encoding="utf-8")[-4000:] if worker_log else ""
    raise AssertionError(
        f"dispatch for {session_id} did not reach {wanted}; actual status={status}; "
        f"durable outcome={await _completion_diagnostic(session_id)}; "
        f"parent permit reason={await _busy_permit_reason()}\n{detail}"
    )


@pytest.mark.asyncio
async def test_redis_worker_reconcile_restart_cancel_and_isolation(
    mi_db, tmp_path: Path, monkeypatch
):
    del mi_db
    _require_redis()
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "2")
    monkeypatch.setenv("MEETING_ESSENCE_RECONCILE_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_NAME", f"summary-life-{uuid4().hex[:8]}")
    get_settings.cache_clear()
    queue = get_settings().meeting_essence_queue_name
    log_path = tmp_path / "calls.log"
    log_path.write_text("", encoding="utf-8")
    session_a, session_b, segment_a, segment_b = await _seed_initial_broker_sessions()
    control = tmp_path / "control.json"
    _write_initial_broker_control(control, log_path, session_a, session_b, segment_a, segment_b)
    from redis import Redis

    client = Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=0.4, socket_timeout=0.4
    )
    client.delete(queue)
    published = ensure_summary_wake_capacity(1)
    again = ensure_summary_wake_capacity(1)
    assert published == 1
    assert again == 0
    assert int(client.llen(queue)) == 1
    client.delete(queue)
    async with get_sessionmaker()() as db:
        created = await reconcile_summary_work(db)
        await db.commit()
    assert created.created >= 1
    env = os.environ.copy()
    env["RECANTOR_SUMMARY_PROVIDER_FACTORY"] = "summary_broker_provider:factory"
    env["RECANTOR_SUMMARY_CONTROL"] = str(control)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")),
            os.path.dirname(__file__),
        ]
    )
    await prepare_shared_broker_permit()
    worker_log = tmp_path / "worker.log"
    # Wakes carry no session id. Earlier suite sessions may be ahead of these
    # two; the normal reconciler must keep waking all durable pending work.
    reconciler, reconciler_handle = _start_reconciler(env, tmp_path / "reconciler.log")
    worker: subprocess.Popen[str] | None = None
    worker_handle: object | None = None
    try:
        worker, worker_handle = _start_worker(env, queue, worker_log)
        _wait_ready(worker_log)
        ensure_summary_wake_capacity(2)
        await _wait_status(session_a, {MISummaryDispatchStatus.COMPLETED.value}, 40, worker_log)
        await _wait_status(session_b, {MISummaryDispatchStatus.COMPLETED.value}, 40, worker_log)
        await _assert_completed_artifact(session_a, {"a-01"})
        await _assert_completed_artifact(session_b, {"b-01"})
        lines = log_path.read_text(encoding="utf-8")
        assert str(session_a) in lines and str(session_b) in lines
        async with get_sessionmaker()() as db:
            jobs = int(
                await db.scalar(
                    select(func.count())
                    .select_from(MISummaryDispatch)
                    .where(MISummaryDispatch.session_id.in_([session_a, session_b]))
                )
                or 0
            )
        assert jobs == 2
        _stop_process(worker)
        if worker_handle is not None:
            worker_handle.close()
            worker_handle = None
        control.write_text(
            control.read_text(encoding="utf-8").replace('"block": false', '"block": true'),
            encoding="utf-8",
        )
        session_c = uuid4()
        segment_c = uuid4()
        async with get_sessionmaker()() as db, db.begin():
            db.add(
                RecordingSession(
                    id=session_c,
                    client_request_id=uuid4(),
                    kind=SessionKind.LIVE.value,
                    state=SessionState.RECORDING.value,
                )
            )
            db.add(
                TranscriptSegment(
                    id=segment_c,
                    session_id=session_c,
                    sequence=1,
                    producer_key=f"broker-{segment_c}",
                    start_ms=0,
                    end_ms=400,
                    text="Batal.",
                )
            )
            await db.flush()
            await note_transcript_publication(db, session_id=session_c, publication_sequence=1)
        # Rebuild control so the blocked provider still has a record for the new segment.
        control.write_text(
            (
                '{"sleep_seconds": 0, "block": true, "log_path": '
                + _json(str(log_path))
                + ', "records": ['
                + _record("c-01", segment_c, "Batal.", session_id=session_c)
                + "]}"
            ),
            encoding="utf-8",
        )
        worker, worker_handle = _start_worker(env, queue, worker_log)
        _wait_ready(worker_log)
        async with get_sessionmaker()() as db:
            await reconcile_summary_work(db)
            await db.commit()
        ensure_summary_wake_capacity(1)
        deadline = time.monotonic() + 20
        while str(session_c) not in log_path.read_text(encoding="utf-8"):
            if time.monotonic() > deadline:
                raise AssertionError(worker_log.read_text(encoding="utf-8")[-4000:])
            time.sleep(0.1)
        async with get_sessionmaker()() as db, db.begin():
            from recantor.meeting_essence.scheduling.jobs import cancel_summary_dispatch

            dispatch_id = await db.scalar(
                select(MISummaryDispatch.id).where(MISummaryDispatch.session_id == session_c)
            )
            await cancel_summary_dispatch(db, dispatch_id=dispatch_id, session_id=session_c)
        await _wait_status(session_c, {MISummaryDispatchStatus.CANCELLED.value}, 20)
    finally:
        _stop_process(reconciler)
        reconciler_handle.close()
        _stop_process(worker)
        if worker_handle is not None:
            worker_handle.close()
        client.delete(queue)
        client.close()
        get_settings.cache_clear()


def _start_worker(
    env: dict[str, str], queue: str, log_path: Path
) -> tuple[subprocess.Popen[str], object]:
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "recantor.meeting_essence.scheduling.tasks:celery_app",
            "worker",
            "-Q",
            queue,
            "--concurrency=1",
            "--prefetch-multiplier=1",
            "--pool=solo",
            "--loglevel=INFO",
        ],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    return proc, handle


def _stop_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_ready(log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if log_path.is_file() and "ready" in log_path.read_text(encoding="utf-8").lower():
            return
        time.sleep(0.2)
    raise AssertionError(
        log_path.read_text(encoding="utf-8") if log_path.is_file() else "no worker log"
    )


@pytest.mark.asyncio
async def test_reconciler_crash_and_proven_dead_resume(mi_db, tmp_path: Path, monkeypatch) -> None:
    del mi_db
    _require_redis()
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "60")
    monkeypatch.setenv("MEETING_ESSENCE_RECONCILE_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_NAME", f"summary-crash-{uuid4().hex[:8]}")
    monkeypatch.setenv("MEETING_ESSENCE_RETRY_BASE_SECONDS", "0.1")
    monkeypatch.setenv("MEETING_ESSENCE_DISPATCH_LEASE_SECONDS", "5")
    get_settings.cache_clear()
    queue = get_settings().meeting_essence_queue_name
    log_path = tmp_path / "calls.log"
    log_path.write_text("", encoding="utf-8")
    session_id, other_id, segment_ids, other_segment = await _seed_crash_broker_sessions()
    segment_id = segment_ids[0]
    env = os.environ.copy()
    env["MEETING_ESSENCE_DEBOUNCE_SECONDS"] = "0"
    env["MEETING_ESSENCE_RECONCILE_INTERVAL_SECONDS"] = "1"
    env["MEETING_ESSENCE_QUEUE_NAME"] = queue
    env["MEETING_ESSENCE_RETRY_BASE_SECONDS"] = "0.1"
    env["MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS"] = "60"
    env["MEETING_ESSENCE_DISPATCH_LEASE_SECONDS"] = "5"
    env["RECANTOR_SUMMARY_PROVIDER_FACTORY"] = "summary_broker_provider:factory"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")),
            os.path.dirname(__file__),
        ]
    )
    control = tmp_path / "control.json"
    crash_records = _crash_broker_records(session_id, other_id, segment_ids, other_segment)
    control.write_text(
        json.dumps(
            {
                "sleep_seconds": 0,
                "block": True,
                "block_after_extracts": 1,
                "spawn_tree": True,
                "fail_once": False,
                "log_path": str(log_path),
                "records": crash_records,
            }
        ),
        encoding="utf-8",
    )
    env["RECANTOR_SUMMARY_CONTROL"] = str(control)
    await prepare_shared_broker_permit()
    reconciler_log = tmp_path / "reconciler.log"
    worker_log = tmp_path / "crash-worker.log"
    reconciler, reconciler_handle = _start_reconciler(env, reconciler_log)
    worker = None
    worker_handle = None
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        found = False
        while time.monotonic() < deadline:
            async with get_sessionmaker()() as db:
                found = (
                    await db.scalar(
                        select(MISummaryDispatch.id).where(
                            MISummaryDispatch.session_id == session_id
                        )
                    )
                    is not None
                )
            if found:
                break
            time.sleep(0.2)
        assert found is True
        worker, worker_handle = _start_worker(env, queue, worker_log)
        _wait_ready(worker_log)
        from recantor.meeting_essence.scheduling.tasks import ensure_summary_wake_capacity

        ensure_summary_wake_capacity(1)
        from summary_broker_provider import invocation_witness

        from recantor.meeting_essence.persistence.models import MIAttemptCheckpoint
        from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
        from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
        from recantor.meeting_essence.scheduling.process_liveness import (
            Liveness,
            ProcessIdentity,
            process_is_alive,
            process_liveness,
            terminate_process_tree,
        )

        deadline = time.monotonic() + 40
        registered = False
        stored: ProcessIdentity | None = None
        grandchild: ProcessIdentity | None = None
        identity_path = log_path.with_name("owned-identity.json")
        while time.monotonic() < deadline:
            saved = _saved_identities(log_path)
            if identity_path.is_file():
                raw = json.loads(identity_path.read_text(encoding="utf-8"))
                stored = _identity_from_saved(raw)
            for item in saved:
                if item.get("role") == "grandchild":
                    grandchild = _identity_from_saved(item)
            async with get_sessionmaker()() as db:
                owned_json = await db.scalar(select(MIHostInvocationLease.owned_runtime_json))
                checkpoint = await db.scalar(
                    select(MIAttemptCheckpoint.body_json).where(
                        MIAttemptCheckpoint.session_id == session_id
                    )
                )
            accepted = _checkpoint_page(checkpoint) >= 1
            if (
                stored
                and grandchild
                and owned_json
                and str(stored.pid) in str(owned_json)
                and accepted
            ):
                registered = True
                break
            time.sleep(0.1)
        assert registered is True, (
            f"parent permit reason={await _busy_permit_reason()}\n"
            + worker_log.read_text(encoding="utf-8")[-4000:]
        )
        assert stored is not None and grandchild is not None
        _stop_process(worker)
        worker = None
        assert process_is_alive(stored)
        assert process_is_alive(grandchild)
        permit = HostInvocationPermit()
        assert await permit.try_acquire(entrypoint="round6-during-crash") is None
        assert _competing_acquire(env) is False
        assert not log_path.with_name("competing-entered.txt").is_file()
        counter_path = log_path.with_name("invocation-counter.json")
        witness = invocation_witness(counter_path)
        assert witness["present"] is True
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) <= 1
        _kill_pid_only(stored.pid)
        _wait_identity_dead(stored)
        assert process_is_alive(grandchild)
        witness = invocation_witness(counter_path)
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) <= 1
        assert _competing_acquire(env) is False
        assert not log_path.with_name("competing-entered.txt").is_file()
        assert terminate_process_tree(stored) is True
        _wait_identity_dead(stored)
        _wait_identity_dead(grandchild)
        assert process_liveness(grandchild) == Liveness.DEAD
        assert stranger.poll() is None
        witness = invocation_witness(counter_path)
        assert witness["present"] is True
        assert int(witness["live"]) == 0
        assert int(witness["peak"]) <= 1
        time.sleep(5.2)
        recovered = await permit.recover_proven_dead()
        assert recovered is True
        assert _competing_acquire(env) is True
        assert log_path.with_name("competing-entered.txt").is_file()
        assert log_path.with_name("competing-exited.txt").is_file()
        admitted = invocation_witness(counter_path)
        assert admitted["present"] is True
        assert admitted["malformed"] is False
        assert int(admitted["admitted_work"]) >= 1
        assert int(admitted["peak"]) <= 1
        stages_before_resume = int(admitted["stage_calls"])
        resumed = json.loads(control.read_text(encoding="utf-8"))
        resumed["block"] = False
        resumed["spawn_tree"] = False
        resumed["fail_once"] = True
        resumed["fail_session_id"] = str(session_id)
        resumed["block_after_extracts"] = 0
        control.write_text(json.dumps(resumed), encoding="utf-8")
        if worker_handle is not None:
            worker_handle.close()
        worker, worker_handle = _start_worker(env, queue, worker_log)
        _wait_ready(worker_log)
        ensure_summary_wake_capacity(1)
        await _wait_status(session_id, {MISummaryDispatchStatus.COMPLETED.value}, 40, worker_log)
        await _wait_status(other_id, {MISummaryDispatchStatus.COMPLETED.value}, 40, worker_log)
        await _assert_completed_artifact(session_id, {f"crash-{index}" for index in range(13)})
        await _assert_completed_artifact(other_id, {"other-1"})
        lines = log_path.read_text(encoding="utf-8")
        assert str(other_segment) in lines
        first_extracts = [
            line for line in lines.splitlines() if " extract " in line and str(segment_id) in line
        ]
        assert len(first_extracts) == 1
        for seg in segment_ids:
            assert str(seg) in lines
        for line in lines.splitlines():
            if str(segment_id) in line:
                assert str(session_id) in line
            if str(other_segment) in line:
                assert str(other_id) in line
        fail_count = log_path.with_name("fail-count.txt")
        assert fail_count.is_file()
        assert int(fail_count.read_text(encoding="utf-8")) >= 1
        witness = invocation_witness(counter_path)
        assert witness["present"] is True
        assert int(witness["peak"]) <= 1
        assert int(witness["stage_calls"]) > stages_before_resume
        async with get_sessionmaker()() as db:
            failure_count = await db.scalar(
                select(MISummaryDispatch.failure_count).where(
                    MISummaryDispatch.session_id == session_id
                )
            )
        assert int(failure_count or 0) >= 1
    finally:
        _stop_process(reconciler)
        _stop_process(worker)
        if reconciler_handle is not None:
            reconciler_handle.close()
        if worker_handle is not None:
            worker_handle.close()
        _reap_saved_identities(log_path)
        if stranger.poll() is None:
            stranger.kill()
            stranger.wait(timeout=5)
        from redis import Redis

        client = Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=0.4, socket_timeout=0.4
        )
        try:
            client.delete(queue)
        finally:
            client.close()
        get_settings.cache_clear()


def _checkpoint_page(body: str | None) -> int:
    if not body:
        return 0
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 0
    try:
        return int(payload.get("extract_page_index") or 0)
    except (TypeError, ValueError):
        return 0


def _saved_identities(log_path: Path) -> list[dict[str, object]]:
    path = log_path.with_name("owned-identities.json")
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _identity_from_saved(raw: dict[str, object]) -> object:
    from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity

    created = raw.get("created_ms")
    ticks = raw.get("start_ticks")
    namespace = raw.get("namespace")
    pgid = raw.get("owned_pgid")
    sid = raw.get("owned_sid")
    return ProcessIdentity(
        pid=int(raw["pid"]),
        created_ms=None if created is None else int(created),
        start_ticks=None if ticks is None else int(ticks),
        namespace=None if namespace is None else str(namespace),
        owned_pgid=None if pgid is None else int(pgid),
        owned_sid=None if sid is None else int(sid),
    )


def _kill_pid_only(pid: int) -> None:
    """Stop one pid. Do not signal its children or the caller's process group."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return
    import signal

    os.kill(pid, signal.SIGKILL)


def _wait_identity_dead(identity: object) -> None:
    from recantor.meeting_essence.scheduling.process_liveness import Liveness, process_liveness

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process_liveness(identity) == Liveness.ALIVE:
        time.sleep(0.05)
    assert process_liveness(identity) == Liveness.DEAD


def _reap_saved_identities(log_path: Path) -> None:
    """Reap every identity stored at spawn. Do not re-identify the pid afterward."""
    from recantor.meeting_essence.scheduling.process_liveness import (
        Liveness,
        process_liveness,
        terminate_process_tree,
    )

    for raw in _saved_identities(log_path):
        identity = _identity_from_saved(raw)
        if process_liveness(identity) == Liveness.ALIVE:
            terminate_process_tree(identity)
        deadline = time.monotonic() + 5
        while process_liveness(identity) == Liveness.ALIVE and time.monotonic() < deadline:
            time.sleep(0.05)


def _competing_acquire(env: dict[str, str]) -> bool:
    """Independent entrypoint. Admitted work goes through the permit-bound provider."""
    code = (
        "import asyncio, json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.platform == 'win32':\n"
        "    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())\n"
        "from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit\n"
        "from recantor.meeting_essence.scheduling.permit_provider import (\n"
        "    PermitBoundMeetingEssenceProvider,\n"
        ")\n"
        "async def main():\n"
        "    permit = HostInvocationPermit()\n"
        "    handle = await permit.try_acquire(entrypoint='round7-competing-caller')\n"
        "    if handle is None:\n"
        "        print(json.dumps({'acquired': False}), flush=True)\n"
        "        return\n"
        "    control = json.loads(Path(os.environ['RECANTOR_SUMMARY_CONTROL']).read_text())\n"
        "    counter = Path(control['log_path']).with_name('invocation-counter.json')\n"
        "    class Work:\n"
        "        spawns_process_tree = False\n"
        "        async def run_stage(self, **kwargs):\n"
        "            from summary_broker_provider import (\n"
        "                finish_stage_observation, note_admitted_work,\n"
        "            )\n"
        "            work_id = note_admitted_work(counter)\n"
        "            entered = counter.with_name('competing-entered.txt')\n"
        "            exited = counter.with_name('competing-exited.txt')\n"
        "            entered.write_text('1', encoding='utf-8')\n"
        "            try:\n"
        "                await asyncio.sleep(0)\n"
        "            finally:\n"
        "                finish_stage_observation(counter, work_id)\n"
        "                exited.write_text('1', encoding='utf-8')\n"
        "    wrapper = PermitBoundMeetingEssenceProvider(\n"
        "        inner=Work(), permit=permit, entrypoint='round7-competing-caller',\n"
        "        shared_handle=handle,\n"
        "    )\n"
        "    await wrapper.run_stage(operation=None, request=None, job=None, prompt_bundle=None)\n"
        "    await wrapper.aclose()\n"
        "    print(json.dumps({'acquired': True}), flush=True)\n"
        "asyncio.run(main())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return bool(json.loads(completed.stdout)["acquired"])


def _start_reconciler(env: dict[str, str], log_path: Path) -> tuple[subprocess.Popen[str], object]:
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "recantor.meeting_essence.scheduling.reconciler"],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    return proc, handle


async def prepare_shared_broker_permit() -> None:
    """Use the real shared permit. Recover only a proven-dead holder.

    A private authority directory or a direct lease reset is not cleanup. An
    unknown owned identity, an occupied owned group, or this process's still
    running stage or closer stays busy and this setup fails.
    """
    from sqlalchemy import text

    from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit

    permit = HostInvocationPermit()
    async with get_sessionmaker()() as db:
        matched = await permit._matches_configured_authority(db)
        observed = await db.scalar(text("SELECT inet_server_addr()::text"))
    if not matched:
        raise AssertionError(
            f"broker authority does not match this database: inet_server_addr={observed!r}"
        )
    await permit.recover_proven_dead()
    if await permit.blocks_new_invocation():
        raise AssertionError(f"shared host permit is busy: {await _busy_permit_reason()}")


async def _busy_permit_reason() -> str:
    """Read why the shared permit blocks. Does not change the lease or sidecar."""
    from recantor.meeting_essence.scheduling import host_authority
    from recantor.meeting_essence.scheduling.host_permit import _runtime_state
    from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
    from recantor.meeting_essence.scheduling.process_liveness import (
        Liveness,
        owned_group_occupied,
        process_liveness,
    )

    snapshot = host_authority.read_owned()
    if not snapshot.readable:
        return "sidecar is unreadable"
    if snapshot.containment_open:
        return "sidecar containment is open"
    async with get_sessionmaker()() as db:
        row = await db.get(MIHostInvocationLease, 1)
    if row is None or not row.holder_token:
        return "sidecar or authority blocks without a lease holder"
    owned, containment_open = _runtime_state(row.owned_runtime_json)
    if containment_open:
        return "lease containment is open"
    for item in owned:
        liveness = process_liveness(item.as_identity())
        if liveness != Liveness.DEAD:
            return f"owned pid {item.pid} is {liveness.value}"
        if owned_group_occupied(item.as_identity()):
            return f"owned group for pid {item.pid} is still occupied"
    holder = process_liveness(_caller_identity(row))
    if holder != Liveness.DEAD:
        return f"holder pid {row.holder_pid} is {holder.value} entrypoint={row.holder_entrypoint}"
    return f"holder state {row.holder_state} active={row.active_invocations}"


def _caller_identity(row: object):
    from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity

    created = getattr(row, "holder_pid_created_ms", None)
    ticks = getattr(row, "holder_start_ticks", None)
    namespace = getattr(row, "holder_namespace", None)
    return ProcessIdentity(
        pid=int(getattr(row, "holder_pid", 0) or 0),
        created_ms=None if created is None else int(created),
        start_ticks=None if ticks is None else int(ticks),
        namespace=None if namespace is None else str(namespace),
    )


def _json(value: str) -> str:
    return '"' + value.replace("\\", "\\\\") + '"'


def _record(record_id: str, segment_id, claim: str, *, session_id, end_ms=400) -> str:
    return json.dumps(
        dict(
            record_id=record_id,
            session_id=str(session_id),
            segment_id=str(segment_id),
            claim=claim,
            start_ms=0,
            end_ms=end_ms,
        )
    )


async def _seed_initial_broker_sessions():
    session_a = uuid4()
    session_b = uuid4()
    segment_a = uuid4()
    segment_b = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        for session_id, segment_id, text in (
            (session_a, segment_a, "Sesi satu."),
            (session_b, segment_b, "Sesi dua."),
        ):
            db.add(
                RecordingSession(
                    id=session_id,
                    client_request_id=uuid4(),
                    kind=SessionKind.LIVE.value,
                    state=SessionState.RECORDING.value,
                )
            )
            db.add(
                TranscriptSegment(
                    id=segment_id,
                    session_id=session_id,
                    sequence=1,
                    producer_key=f"broker-{segment_id}",
                    start_ms=0,
                    end_ms=500,
                    text=text,
                )
            )
            await db.flush()
            await note_transcript_publication(db, session_id=session_id, publication_sequence=1)
    return session_a, session_b, segment_a, segment_b


def _write_initial_broker_control(control, log_path, session_a, session_b, segment_a, segment_b):
    control.write_text(
        (
            '{"sleep_seconds": 0.05, "block": false, "log_path": '
            + _json(str(log_path))
            + ', "records": ['
            + _record("a-01", segment_a, "Sesi satu.", session_id=session_a, end_ms=500)
            + ","
            + _record("b-01", segment_b, "Sesi dua.", session_id=session_b, end_ms=500)
            + "]}"
        ),
        encoding="utf-8",
    )


async def _seed_crash_broker_sessions():
    session_id = uuid4()
    other_id = uuid4()
    segment_ids = [uuid4() for _ in range(13)]
    other_segment = uuid4()
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            RecordingSession(
                id=session_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.RECORDING.value,
            )
        )
        db.add(
            RecordingSession(
                id=other_id,
                client_request_id=uuid4(),
                kind=SessionKind.LIVE.value,
                state=SessionState.RECORDING.value,
            )
        )
        for index, seg in enumerate(segment_ids):
            db.add(
                TranscriptSegment(
                    id=seg,
                    session_id=session_id,
                    sequence=index + 1,
                    producer_key=f"crash-{seg}",
                    start_ms=index,
                    end_ms=index + 1,
                    text=f"Halaman {index}.",
                )
            )
        db.add(
            TranscriptSegment(
                id=other_segment,
                session_id=other_id,
                sequence=1,
                producer_key=f"crash-{other_segment}",
                start_ms=0,
                end_ms=400,
                text="Sesi lain.",
            )
        )
    return session_id, other_id, segment_ids, other_segment


def _crash_broker_records(session_id, other_id, segment_ids, other_segment):
    crash_records = [
        {
            "record_id": f"crash-{index}",
            "session_id": str(session_id),
            "segment_id": str(seg),
            "claim": f"Halaman {index}.",
            "start_ms": index,
            "end_ms": index + 1,
        }
        for index, seg in enumerate(segment_ids)
    ]
    crash_records.append(
        {
            "record_id": "other-1",
            "session_id": str(other_id),
            "segment_id": str(other_segment),
            "claim": "Sesi lain.",
            "start_ms": 0,
            "end_ms": 400,
        }
    )
    return crash_records
