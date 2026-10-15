from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from meeting_intelligence_db import SESSION_B
from meeting_intelligence_fixtures import FIXTURE_SESSION_ID, build_s01_s06_bundle
from sqlalchemy import delete, select
from test_summary_round4 import _silence_other_sessions

from recantor.db import get_sessionmaker
from recantor.main import app
from recantor.meeting_essence.contracts import AtomicRecord, ProcessingJobStatus, RecordRelation
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.engine import EngineRunConfig
from recantor.meeting_essence.http.pagination import ResultCursor
from recantor.meeting_essence.persistence.models import (
    MIArtifactVersion,
    MIAtomicRecordRow,
    MIIntelligenceJob,
    MIJobAttempt,
)
from recantor.meeting_essence.persistence.repository import (
    MeetingIntelligenceRepository,
    StaleGenerationError,
)
from recantor.meeting_essence.persistence.service import MeetingIntelligenceLedgerService
from recantor.meeting_essence.provider_adapters.factory import configure_summary_provider_factory
from recantor.meeting_essence.provider_adapters.structured_provider import (
    StructuredMeetingEssenceProvider,
)
from recantor.meeting_essence.scheduling.host_authority import reset_for_tests
from recantor.meeting_essence.scheduling.jobs import (
    SummaryExecutionStatus,
    execute_next_summary_job,
)
from recantor.meeting_essence.scheduling.models import (
    MISummaryDispatch,
    MISummaryRequestReservation,
    MISummarySessionState,
)
from recantor.meeting_essence.scheduling.provider_factory import set_summary_provider_factory
from recantor.meeting_essence.scheduling.source_material import (
    build_coverage_flags,
    load_session_reader,
)
from recantor.meeting_essence.scheduling.triggers import note_transcript_publication
from recantor.meeting_essence.source_reader import materialize_manifest_from_reader
from recantor.settings import get_settings

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "meeting_intelligence" / "s01_s06_example.json"
)


async def _deterministic_provider_for_session(
    session_id: UUID,
    *,
    boundary: int = 6,
) -> StructuredMeetingEssenceProvider:
    """Gold manifest must match the DB-backed reader the scheduler uses."""
    base = build_s01_s06_bundle()
    async with get_sessionmaker()() as db:
        reader = await load_session_reader(
            db, session_id=session_id, publication_boundary_sequence=boundary
        )
        reader.freeze_publication_boundary(boundary)
        coverage = await build_coverage_flags(
            db,
            session_id=session_id,
            publication_boundary_sequence=boundary,
            summary_state=None,
        )
        manifest, report = materialize_manifest_from_reader(
            reader,
            session_id=session_id,
            coverage=coverage,
            meeting_metadata={},
            captured_at=None,
        )
    assert report.ok, report.message
    bundle = base.model_copy(update={"manifest": manifest})
    inner = DeterministicMeetingEssenceProvider(gold=bundle)
    return StructuredMeetingEssenceProvider(inner=inner, adapter_id="deterministic-v1")


@pytest.fixture
async def essence_client(mi_db, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    del mi_db  # noqa: ARG001 — mi_db seeds sessions/transcripts before client runs
    monkeypatch.setenv("MEETING_ESSENCE_DEV_API_ENABLED", "1")
    monkeypatch.setenv("MEETING_ESSENCE_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("MEETING_ESSENCE_RETRY_BASE_SECONDS", "0.1")
    monkeypatch.setenv("MEETING_ESSENCE_ENGINE_QUANTUM_SECONDS", "120")
    monkeypatch.setenv("RECANTOR_MEETING_ESSENCE_PROVIDER", "deterministic")
    monkeypatch.setenv("RECANTOR_MEETING_ESSENCE_FIXTURE", str(FIXTURE_PATH))
    get_settings.cache_clear()
    set_summary_provider_factory(None)
    reset_for_tests()
    configure_summary_provider_factory()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _prepare_session(session_id: UUID) -> None:
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
        # This session's canonical fixture was reseeded by mi_db. Its previous
        # scheduler boundary may belong to a longer fixture from another test.
        await db.execute(
            delete(MISummarySessionState).where(MISummarySessionState.session_id == session_id)
        )
        db.add(MISummarySessionState(session_id=session_id))
        await note_transcript_publication(db, session_id=session_id, publication_sequence=6)


async def _execute_configured(session_id: UUID):
    """Run the worker entry with the configured factory. No injected provider."""
    trace: list[str] = []
    outcome = None
    for _ in range(40):
        async with get_sessionmaker()() as db:
            outcome = await execute_next_summary_job(db, entrypoint="configured-fixture-worker")
            await db.commit()
        if outcome.session_id not in {None, session_id}:
            trace.append(f"other:{outcome.status.value}")
            await asyncio.sleep(0.05)
            continue
        trace.append(outcome.status.value)
        if outcome.status == SummaryExecutionStatus.SUCCEEDED:
            return outcome
        if outcome.status in {
            SummaryExecutionStatus.FAILED,
            SummaryExecutionStatus.CANCELLED,
            SummaryExecutionStatus.UNAVAILABLE,
            SummaryExecutionStatus.QUARANTINED,
        }:
            break
        await asyncio.sleep(0.05)
    raise AssertionError(trace)


@pytest.mark.asyncio
async def test_scheduler_engine_smoke_with_db_reader(
    mi_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    del mi_db
    get_settings.cache_clear()
    reset_for_tests()
    await _prepare_session(FIXTURE_SESSION_ID)
    boundary = 6
    async with get_sessionmaker()() as db:
        reader = await load_session_reader(
            db, session_id=FIXTURE_SESSION_ID, publication_boundary_sequence=boundary
        )
        coverage = await build_coverage_flags(
            db,
            session_id=FIXTURE_SESSION_ID,
            publication_boundary_sequence=boundary,
            summary_state=None,
        )
        provider = await _deterministic_provider_for_session(FIXTURE_SESSION_ID)
        service = MeetingIntelligenceLedgerService(db)
        outcome = await service.run_engine(
            session_id=FIXTURE_SESSION_ID,
            client_request_id=uuid4(),
            reader=reader,
            provider=provider,
            publication_boundary_sequence=boundary,
            coverage=coverage,
            provider_adapter_id="deterministic-v1",
            config=EngineRunConfig(max_wall_clock_seconds=120.0),
        )
        await db.commit()
    assert outcome.result.processing_status == ProcessingJobStatus.SUCCEEDED, [
        (item.code, item.message) for item in outcome.result.limitations
    ]


@pytest.mark.asyncio
async def test_dev_api_disabled_by_default() -> None:
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations",
            json={"client_request_id": str(uuid4())},
        )
    assert response.status_code == 403


async def _http_derive(client: AsyncClient, session_id: UUID, request_id: UUID | None = None):
    request_id = request_id or uuid4()
    response = await client.post(
        f"/api/v1/sessions/{session_id}/meeting-essence/derivations",
        json={
            "client_request_id": str(request_id),
            "operation_version": "meeting-essence.operation/v1",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] in {"accepted", "coalesced", "duplicate"}
    assert body["operation_version"] == "meeting-essence.operation/v1"
    return UUID(body["dispatch_id"]), request_id, body


@pytest.mark.asyncio
async def test_http_submit_idempotence(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    request_id = uuid4()
    first_id, _, first = await _http_derive(essence_client, FIXTURE_SESSION_ID, request_id)
    second_id, _, second = await _http_derive(essence_client, FIXTURE_SESSION_ID, request_id)
    assert second_id == first_id
    assert second["reused"] is True or second["status"] in {"duplicate", "coalesced"}
    assert first["dispatch_id"] == second["dispatch_id"]


@pytest.mark.asyncio
async def test_configured_http_derive_status_results_evidence_and_cache(
    essence_client: AsyncClient,
) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    first = await _execute_configured(FIXTURE_SESSION_ID)
    assert first.metrics is not None
    assert first.metrics.provider_calls > 0
    status = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["current_artifact"] is not None
    assert body["current_artifact"]["is_current"] is True
    assert body["source"]["stt_backlog_pending"] is False
    assert body["checks"]["structural_check_status"]
    assert body["checks"]["forward_semantic_check_status"]
    assert body["checks"]["backward_semantic_check_status"]
    artifact_id = body["current_artifact"]["artifact_id"]
    page = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records",
        params={"limit": 64},
    )
    assert page.status_code == 200, page.text
    records = page.json()["items"]
    assert records
    evidence = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}"
        f"/records/{records[0]['record_id']}/evidence"
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"]
    await _http_derive(essence_client, FIXTURE_SESSION_ID)
    cached = await _execute_configured(FIXTURE_SESSION_ID)
    assert cached.status == SummaryExecutionStatus.SUCCEEDED
    assert cached.metrics is not None
    assert cached.metrics.served_from_cache is True
    assert cached.metrics.provider_calls == 0


@pytest.mark.asyncio
async def test_cross_session_artifact_is_rejected(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    status = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    artifact_id = status.json()["current_artifact"]["artifact_id"]
    foreign = await essence_client.get(
        f"/api/v1/sessions/{SESSION_B}/meeting-essence/artifacts/{artifact_id}/results/overview"
    )
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_unclaimed_dispatch_keeps_prior_artifact(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    done = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    artifact_id = done.json()["current_artifact"]["artifact_id"]
    later_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    pending = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{later_id}"
    )
    assert pending.status_code == 200
    body = pending.json()
    assert body["job_id"] is None
    assert body["current_artifact"] is None
    prior = body["prior_useful_artifact"]
    assert prior is not None
    assert prior["artifact_id"] == artifact_id
    assert prior["is_stale"] is False


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_claimed_job_without_artifact_keeps_prior_useful_artifact(
    essence_client: AsyncClient,
) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    first_dispatch, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    done = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{first_dispatch}"
    )
    artifact_id = done.json()["current_artifact"]["artifact_id"]
    second_dispatch, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    async with get_sessionmaker()() as db:
        prev_job = await db.scalar(
            select(MIIntelligenceJob)
            .where(MIIntelligenceJob.session_id == FIXTURE_SESSION_ID)
            .order_by(MIIntelligenceJob.created_at.desc())
        )
        assert prev_job is not None and prev_job.current_artifact_id is not None
        dispatch = await db.get(MISummaryDispatch, second_dispatch)
        assert dispatch is not None
        inflight = MIIntelligenceJob(
            session_id=prev_job.session_id,
            client_request_id=uuid4(),
            snapshot_id=prev_job.snapshot_id,
            source_content_hash=prev_job.source_content_hash,
            source_epoch=prev_job.source_epoch,
            run_fingerprint=hashlib.sha256(
                f"{prev_job.run_fingerprint}:inflight".encode()
            ).hexdigest()[:64],
            prompt_cache_key=prev_job.prompt_cache_key,
            schema_identity=prev_job.schema_identity,
            algorithm_identity=prev_job.algorithm_identity,
            limits_fingerprint=prev_job.limits_fingerprint,
            provider_adapter_id=prev_job.provider_adapter_id,
            status="running",
            active_generation=prev_job.active_generation + 1,
            current_artifact_id=None,
            stale_artifact_id=None,
        )
        db.add(inflight)
        await db.flush()
        dispatch.job_id = inflight.id
        await db.commit()
    pending = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{second_dispatch}"
    )
    body = pending.json()
    assert body["job_id"] is not None
    assert body["current_artifact"] is None
    prior = body["prior_useful_artifact"]
    assert prior is not None
    assert prior["artifact_id"] == artifact_id
    assert prior["is_stale"] is False


@pytest.mark.asyncio
async def test_http_pending_cancel_retry_then_worker_completes(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    first_dispatch, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    first_status = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{first_dispatch}"
    )
    artifact_id = first_status.json()["current_artifact"]["artifact_id"]
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    cancelled = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"] is True
    pending = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    assert pending.status_code == 200
    assert pending.json()["dispatch_status"] == "cancelled"
    assert pending.json()["prior_useful_artifact"]["artifact_id"] == artifact_id
    assert pending.json()["prior_useful_artifact"]["is_stale"] is False
    retried = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}/retry"
    )
    assert retried.status_code == 200, retried.text
    retry_body = retried.json()
    assert retry_body["dispatch_id"] == str(dispatch_id)
    assert retry_body["accepted"] is True
    outcome = await _execute_configured(FIXTURE_SESSION_ID)
    assert outcome.metrics is not None
    assert outcome.metrics.served_from_cache is True
    assert outcome.metrics.provider_calls == 0
    done = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    assert done.status_code == 200
    body = done.json()
    assert body["current_artifact"] is not None
    assert body["current_artifact"]["processing_status"] == ProcessingJobStatus.SUCCEEDED.value
    assert body["current_artifact"]["artifact_id"] == artifact_id
    assert body["checks"]["structural_check_status"]
    page = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records"
    )
    assert page.status_code == 200
    record = page.json()["items"][0]
    evidence = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}"
        f"/records/{record['record_id']}/evidence"
    )
    assert evidence.status_code == 200
    assert evidence.json()["evidence"]


@pytest.mark.asyncio
async def test_http_errors_are_bounded(
    essence_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    missing = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{uuid4()}/results/records"
    )
    assert missing.status_code == 404
    bad_cursor = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{uuid4()}/results/records",
        params={"cursor": "not-a-valid-cursor"},
    )
    assert bad_cursor.status_code == 400
    unknown_version = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations",
        json={
            "client_request_id": str(uuid4()),
            "operation_version": "meeting-essence.operation/v9",
        },
    )
    assert unknown_version.status_code == 400
    unknown_cancel = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{uuid4()}/cancel"
    )
    assert unknown_cancel.status_code == 404
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    denied = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}/retry"
    )
    assert denied.status_code == 409
    monkeypatch.setenv("MEETING_ESSENCE_QUEUE_MAX_DEPTH", "1")
    get_settings.cache_clear()
    opened = await essence_client.post(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations",
        json={"client_request_id": str(uuid4())},
    )
    assert opened.status_code == 202
    queued = await essence_client.post(
        f"/api/v1/sessions/{SESSION_B}/meeting-essence/derivations",
        json={"client_request_id": str(uuid4())},
    )
    assert queued.status_code == 429


@pytest.mark.asyncio
async def test_long_result_pages_evidence_and_stale_read(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    status = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    artifact_id = UUID(status.json()["current_artifact"]["artifact_id"])
    async with get_sessionmaker()() as db:
        artifact = await db.get(MIArtifactVersion, artifact_id)
        assert artifact is not None and artifact.layered_result_json
        layered = json.loads(artifact.layered_result_json)
        base_record = AtomicRecord.model_validate(layered["atomic_records"][0])
        base_relation = (
            RecordRelation.model_validate(layered["relations"][0]) if layered["relations"] else None
        )
        ledger = json.loads(artifact.records_ledger_json or "[]")
        clones: list[dict[str, object]] = []
        relations: list[dict[str, object]] = []
        for index in range(70):
            text = base_record.claim_text
            if index == 64:
                text = f"{text} 𐍈"
            clone = base_record.model_copy(
                update={"record_id": f"page{index:03d}", "claim_text": text}
            )
            payload = clone.model_dump(mode="json")
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            clones.append(payload)
            db.add(
                MIAtomicRecordRow(
                    session_id=artifact.session_id,
                    snapshot_id=artifact.snapshot_id,
                    record_id=clone.record_id,
                    record_json=clone.model_dump_json(),
                    content_fingerprint=fingerprint,
                )
            )
            ledger.append({"record_id": clone.record_id, "content_fingerprint": fingerprint})
            if base_relation is not None and index + 1 < 70:
                relation = base_relation.model_copy(
                    update={
                        "from_record_id": f"page{index:03d}",
                        "to_record_id": f"page{index + 1:03d}",
                    }
                )
                relations.append(relation.model_dump(mode="json"))
        layered["atomic_records"] = clones
        if relations:
            layered["relations"] = relations
        artifact.layered_result_json = json.dumps(layered, ensure_ascii=False)
        artifact.records_ledger_json = json.dumps(ledger, ensure_ascii=False)
        artifact.is_current = False
        artifact.is_stale = True
        await db.commit()
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 64}
        if cursor:
            params["cursor"] = cursor
        page = await essence_client.get(
            f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records",
            params=params,
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        assert payload["is_stale"] is True
        assert payload["is_current"] is False
        seen.extend(item["record_id"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"page{index:03d}" for index in range(70)]
    unicode_page = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records",
        params={"limit": 1, "cursor": _cursor_for_offset(artifact_id, "records", 64)},
    )
    assert unicode_page.status_code == 200
    assert "𐍈" in unicode_page.json()["items"][0]["claim_text"]
    if relations:
        relation_ids: list[str] = []
        cursor = None
        while True:
            params = {"limit": 64}
            if cursor:
                params["cursor"] = cursor
            page = await essence_client.get(
                f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/relations",
                params=params,
            )
            assert page.status_code == 200, page.text
            relation_ids.extend(
                f"{item['from_record_id']}->{item['to_record_id']}" for item in page.json()["items"]
            )
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert len(relation_ids) == len(set(relation_ids)) == 69
    evidence = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/records/page064/evidence"
    )
    assert evidence.status_code == 200, evidence.text
    assert "𐍈" in evidence.json()["record"]["claim_text"]
    mismatched = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records",
        params={
            "cursor": ResultCursor(
                session_id=SESSION_B,
                artifact_id=artifact_id,
                layer="records",
                offset=0,
            ).encode()
        },
    )
    assert mismatched.status_code == 409
    negative = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/artifacts/{artifact_id}/results/records",
        params={"cursor": _cursor_for_offset(artifact_id, "records", -1)},
    )
    assert negative.status_code == 400


def _cursor_for_offset(artifact_id: UUID, layer: str, offset: int) -> str:
    return ResultCursor(
        session_id=FIXTURE_SESSION_ID,
        artifact_id=artifact_id,
        layer=layer,
        offset=offset,
    ).encode()


@pytest.mark.asyncio
async def test_older_attempt_cannot_replace_http_artifact(essence_client: AsyncClient) -> None:
    await _prepare_session(FIXTURE_SESSION_ID)
    dispatch_id, _, _ = await _http_derive(essence_client, FIXTURE_SESSION_ID)
    await _execute_configured(FIXTURE_SESSION_ID)
    status = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    artifact_id = status.json()["current_artifact"]["artifact_id"]
    job_id = UUID(status.json()["job_id"])
    async with get_sessionmaker()() as db:
        attempt = await db.scalar(
            select(MIJobAttempt)
            .where(MIJobAttempt.job_id == job_id)
            .order_by(MIJobAttempt.generation)
        )
        assert attempt is not None
        repo = MeetingIntelligenceRepository(db)
        with pytest.raises(StaleGenerationError):
            await repo.finalize_attempt(
                job_id=job_id,
                attempt_id=attempt.id,
                session_id=FIXTURE_SESSION_ID,
                generation=0,
                processing_status=ProcessingJobStatus.SUCCEEDED,
                layered=None,
                limitations=[],
                snapshot_id=attempt.snapshot_id,
                snapshot_content_hash=attempt.source_content_hash,
                run_fingerprint=attempt.run_fingerprint,
                publish_current=True,
                mark_stale_previous=False,
            )
        await db.rollback()
    again = await essence_client.get(
        f"/api/v1/sessions/{FIXTURE_SESSION_ID}/meeting-essence/derivations/{dispatch_id}"
    )
    assert again.json()["current_artifact"]["artifact_id"] == artifact_id
