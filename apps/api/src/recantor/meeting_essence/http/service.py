from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.contracts import ProcessingJobStatus
from recantor.meeting_essence.http.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorBindingError,
    InvalidResultCursor,
    decode_cursor,
    paginate_layer,
)
from recantor.meeting_essence.http.schemas import (
    MeetingEssenceActionsPage,
    MeetingEssenceArtifactIdentity,
    MeetingEssenceCheckState,
    MeetingEssenceDerivationStatusResponse,
    MeetingEssenceDeriveResponse,
    MeetingEssenceEvidenceResponse,
    MeetingEssenceOpenItemsPage,
    MeetingEssenceOverviewPage,
    MeetingEssenceRecordsPage,
    MeetingEssenceRelationsPage,
    MeetingEssenceResultPageResponse,
    MeetingEssenceSourceState,
    MeetingEssenceTopicsPage,
)
from recantor.meeting_essence.persistence.models import MIArtifactVersion, MIIntelligenceJob
from recantor.meeting_essence.persistence.repository import (
    EvidenceOwnershipError,
    MeetingIntelligenceRepository,
    MeetingIntelligenceRepositoryError,
    PublishedArtifact,
    SessionIsolationError,
)
from recantor.meeting_essence.provider_adapters.operation_versions import (
    OPERATION_VERSION_V1,
    SUPPORTED_OPERATION_VERSIONS,
)
from recantor.meeting_essence.scheduling.jobs import (
    SummaryRetryDenied,
    SummarySubmitStatus,
    submit_summary_work,
)
from recantor.meeting_essence.scheduling.models import MISummaryDispatch, MISummarySessionState
from recantor.meeting_essence.scheduling.provider_factory import get_summary_provider_factory
from recantor.meeting_essence.scheduling.service import MeetingEssenceSchedulerService
from recantor.meeting_essence.scheduling.source_material import build_coverage_flags
from recantor.models import RecordingSession

_PAGE_TYPES = {
    "overview": MeetingEssenceOverviewPage,
    "topics": MeetingEssenceTopicsPage,
    "actions": MeetingEssenceActionsPage,
    "open_items": MeetingEssenceOpenItemsPage,
    "records": MeetingEssenceRecordsPage,
    "relations": MeetingEssenceRelationsPage,
}


class MeetingEssenceApiError(Exception):
    pass


class MeetingEssenceNotFound(MeetingEssenceApiError):
    pass


class MeetingEssenceConflict(MeetingEssenceApiError):
    pass


class MeetingEssenceBadRequest(MeetingEssenceApiError):
    pass


class MeetingEssenceQueueFull(MeetingEssenceApiError):
    pass


class MeetingEssenceRetryDenied(MeetingEssenceApiError):
    pass


def configured_operation_version() -> str:
    factory = get_summary_provider_factory()
    inner = getattr(factory, "_inner", None)
    version = getattr(inner, "operation_version", None)
    if isinstance(version, str) and version in SUPPORTED_OPERATION_VERSIONS:
        return version
    return OPERATION_VERSION_V1


class MeetingEssenceApiService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = MeetingIntelligenceRepository(db)
        self._scheduler = MeetingEssenceSchedulerService(db)

    async def _require_session(self, session_id: UUID) -> RecordingSession:
        row = await self._db.get(RecordingSession, session_id)
        if row is None:
            raise MeetingEssenceNotFound("recording session not found")
        return row

    async def _load_session_artifact(
        self, artifact_id: UUID, *, session_id: UUID
    ) -> PublishedArtifact:
        try:
            return await self._repo.load_artifact(artifact_id, session_id=session_id)
        except SessionIsolationError as exc:
            raise MeetingEssenceNotFound("artifact not found for this session") from exc
        except MeetingIntelligenceRepositoryError as exc:
            if "not found" in str(exc).casefold():
                raise MeetingEssenceNotFound("artifact not found for this session") from exc
            raise

    def _identity(self, artifact: PublishedArtifact) -> MeetingEssenceArtifactIdentity:
        return MeetingEssenceArtifactIdentity(
            artifact_id=artifact.id,
            is_current=artifact.is_current,
            is_stale=artifact.is_stale,
            snapshot_id=artifact.snapshot_id,
            snapshot_content_hash=artifact.snapshot_content_hash,
            processing_status=artifact.processing_status,
        )

    async def _latest_useful_artifact_identity(
        self,
        session_id: UUID,
        *,
        skip_artifact_id: UUID | None = None,
    ) -> MeetingEssenceArtifactIdentity | None:
        artifact_ids = await self._db.scalars(
            select(MIArtifactVersion.id)
            .where(
                MIArtifactVersion.session_id == session_id,
                MIArtifactVersion.layered_result_json.isnot(None),
            )
            .order_by(MIArtifactVersion.created_at.desc())
            .limit(12)
        )
        for artifact_id in artifact_ids:
            if skip_artifact_id is not None and artifact_id == skip_artifact_id:
                continue
            artifact = await self._load_session_artifact(artifact_id, session_id=session_id)
            if artifact.layered is None:
                continue
            if not (artifact.layered.overview_bullets or artifact.layered.topics):
                continue
            return self._identity(artifact)
        return None

    @staticmethod
    def _checks(artifact: PublishedArtifact) -> MeetingEssenceCheckState:
        layered = artifact.layered
        if layered is None:
            return MeetingEssenceCheckState()
        coverage = layered.coverage
        return MeetingEssenceCheckState(
            structural_check_status=coverage.structural_check_status,
            forward_semantic_check_status=coverage.forward_semantic_check_status,
            backward_semantic_check_status=coverage.backward_semantic_check_status,
            semantic_check_method=coverage.semantic_check_method,
        )

    async def submit_derivation(
        self,
        *,
        session_id: UUID,
        client_request_id: UUID,
        operation_version: str | None = None,
    ) -> MeetingEssenceDeriveResponse:
        await self._require_session(session_id)
        selected = configured_operation_version()
        if operation_version is not None:
            if operation_version not in SUPPORTED_OPERATION_VERSIONS:
                raise MeetingEssenceBadRequest("unknown meeting essence operation version")
            if operation_version != selected:
                raise MeetingEssenceConflict(
                    "operation version does not match the configured provider"
                )
        result = await submit_summary_work(
            self._db, session_id=session_id, client_request_id=client_request_id
        )
        if result.status == SummarySubmitStatus.CONFLICT:
            await self._db.rollback()
            raise MeetingEssenceConflict("derivation idempotency conflict")
        if result.status == SummarySubmitStatus.QUEUE_FULL or result.dispatch_id is None:
            await self._db.rollback()
            raise MeetingEssenceQueueFull("derivation queue is full")
        await self._db.commit()
        return MeetingEssenceDeriveResponse(
            dispatch_id=result.dispatch_id,
            client_request_id=client_request_id,
            status=result.status.value,
            reused=result.status == SummarySubmitStatus.DUPLICATE,
            operation_version=selected,
        )

    async def derivation_status(
        self, *, session_id: UUID, dispatch_id: UUID
    ) -> MeetingEssenceDerivationStatusResponse:
        await self._require_session(session_id)
        dispatch = await self._db.get(MISummaryDispatch, dispatch_id)
        if dispatch is None or dispatch.session_id != session_id:
            raise MeetingEssenceNotFound("derivation not found for this session")
        summary_state = await self._db.get(MISummarySessionState, session_id)
        high_water = int(summary_state.observed_publication_high_water) if summary_state else 0
        coverage = await build_coverage_flags(
            self._db,
            session_id=session_id,
            publication_boundary_sequence=high_water,
            summary_state=summary_state,
        )
        source = MeetingEssenceSourceState(
            publication_high_water=high_water,
            highest_processed_publication=int(summary_state.highest_processed_publication)
            if summary_state
            else 0,
            work_intent=summary_state.work_intent if summary_state else "open",
            audio_final_sequence=summary_state.audio_final_sequence if summary_state else None,
            frozen_transcript_sequence=summary_state.frozen_transcript_sequence
            if summary_state
            else None,
            transcript_publication_complete=coverage.transcript_publication_complete,
            recording_audio_complete=coverage.recording_audio_complete,
            stt_backlog_pending=coverage.stt_backlog_pending,
            source_pending=coverage.source_pending,
        )
        job: MIIntelligenceJob | None = None
        if dispatch.job_id is not None:
            job = await self._db.get(MIIntelligenceJob, dispatch.job_id)
        processing: ProcessingJobStatus | None = None
        current_artifact = None
        stale_artifact = None
        prior_useful = None
        stale_id = None
        checks = MeetingEssenceCheckState()
        limitations = []
        if job is not None and job.session_id == session_id:
            stale_id = job.stale_artifact_id
            if job.current_artifact_id is not None:
                artifact = await self._load_session_artifact(
                    job.current_artifact_id, session_id=session_id
                )
                processing = artifact.processing_status
                current_artifact = self._identity(artifact)
                checks = self._checks(artifact)
                limitations = list(artifact.limitations)
            if job.stale_artifact_id is not None:
                stale = await self._load_session_artifact(
                    job.stale_artifact_id, session_id=session_id
                )
                stale_artifact = self._identity(stale)
                if current_artifact is None:
                    processing = stale.processing_status
                    checks = self._checks(stale)
                    limitations = list(stale.limitations)
            if current_artifact is None and prior_useful is None:
                prior_useful = await self._latest_useful_artifact_identity(session_id)
                if prior_useful is not None and processing is None:
                    previous = await self._load_session_artifact(
                        prior_useful.artifact_id, session_id=session_id
                    )
                    processing = previous.processing_status
                    checks = self._checks(previous)
                    limitations = list(previous.limitations)
        else:
            latest = await self.latest_job_with_usable_artifact(session_id)
            if latest is None:
                latest = await self.latest_job_for_session(session_id)
            if latest is not None:
                previous_id = latest.current_artifact_id or latest.stale_artifact_id
                if previous_id is not None:
                    previous = await self._load_session_artifact(previous_id, session_id=session_id)
                    prior_useful = self._identity(previous)
                    processing = previous.processing_status
                    checks = self._checks(previous)
                    limitations = list(previous.limitations)
                    if latest.stale_artifact_id is not None and stale_id is None:
                        stale_id = latest.stale_artifact_id
            if prior_useful is None:
                prior_useful = await self._latest_useful_artifact_identity(session_id)
        session = await self._require_session(session_id)
        return MeetingEssenceDerivationStatusResponse(
            dispatch_id=dispatch.id,
            session_id=session_id,
            dispatch_status=dispatch.status,
            disposition=dispatch.disposition,
            job_id=dispatch.job_id,
            attempt_count=int(dispatch.attempt_count),
            last_error_category=dispatch.last_error_category,
            last_error_code=dispatch.last_error_code,
            processing_status=processing,
            source=source,
            checks=checks,
            limitations=limitations,
            current_artifact=current_artifact,
            stale_artifact=stale_artifact,
            prior_useful_artifact=prior_useful,
            stale_artifact_id=stale_id,
            recording_state=session.state,
            operation_version=configured_operation_version(),
            updated_at=dispatch.updated_at,
        )

    async def paginated_results(
        self,
        *,
        session_id: UUID,
        artifact_id: UUID,
        layer: str,
        cursor: str | None,
        limit: int,
    ) -> MeetingEssenceResultPageResponse:
        await self._require_session(session_id)
        page_type = _PAGE_TYPES.get(layer)
        if page_type is None:
            raise MeetingEssenceBadRequest("unknown result layer")
        try:
            decoded = decode_cursor(cursor)
        except InvalidResultCursor as exc:
            raise MeetingEssenceBadRequest(str(exc)) from exc
        artifact = await self._load_session_artifact(artifact_id, session_id=session_id)
        if artifact.layered is None:
            raise MeetingEssenceNotFound("artifact has no layered result")
        layered = artifact.layered
        layer_items = {
            "overview": list(layered.overview_bullets),
            "topics": list(layered.topics),
            "actions": list(layered.actions),
            "open_items": list(layered.open_items),
            "records": list(layered.atomic_records),
            "relations": list(layered.relations),
        }[layer]
        try:
            page, next_cursor = paginate_layer(
                session_id=session_id,
                artifact_id=artifact_id,
                layer=layer,
                items=layer_items,
                cursor=decoded,
                limit=limit or DEFAULT_PAGE_SIZE,
            )
        except InvalidResultCursor as exc:
            raise MeetingEssenceBadRequest(str(exc)) from exc
        except CursorBindingError as exc:
            raise MeetingEssenceConflict(str(exc)) from exc
        return page_type(
            session_id=session_id,
            artifact_id=artifact_id,
            items=page,
            next_cursor=next_cursor,
            total_count=len(layer_items),
            processing_status=artifact.processing_status,
            limitations=artifact.limitations,
            is_current=artifact.is_current,
            is_stale=artifact.is_stale,
        )

    async def resolve_evidence(
        self,
        *,
        session_id: UUID,
        artifact_id: UUID,
        record_id: str,
    ) -> MeetingEssenceEvidenceResponse:
        await self._require_session(session_id)
        artifact = await self._load_session_artifact(artifact_id, session_id=session_id)
        try:
            records = await self._repo.load_ledger_records(
                artifact_id=artifact_id, session_id=session_id
            )
        except SessionIsolationError as exc:
            raise MeetingEssenceNotFound("artifact not found for this session") from exc
        except MeetingIntelligenceRepositoryError as exc:
            raise MeetingEssenceConflict(str(exc)) from exc
        match = next((item for item in records if item.record_id == record_id), None)
        if match is None:
            raise MeetingEssenceNotFound("record not found on this artifact")
        try:
            await self._repo.resolve_record_evidence(
                session_id=session_id,
                snapshot_content_hash=artifact.snapshot_content_hash,
                record=match,
            )
        except (EvidenceOwnershipError, SessionIsolationError) as exc:
            raise MeetingEssenceConflict(str(exc)) from exc
        return MeetingEssenceEvidenceResponse(
            session_id=session_id,
            snapshot_content_hash=artifact.snapshot_content_hash,
            record_id=record_id,
            evidence=match.evidence,
            record=match,
        )

    async def cancel(self, *, session_id: UUID, dispatch_id: UUID) -> None:
        await self._require_session(session_id)
        try:
            await self._scheduler.cancel(dispatch_id=dispatch_id, session_id=session_id)
        except LookupError as exc:
            raise MeetingEssenceNotFound("derivation not found for this session") from exc

    async def retry(self, *, session_id: UUID, dispatch_id: UUID) -> None:
        await self._require_session(session_id)
        dispatch = await self._db.get(MISummaryDispatch, dispatch_id)
        if dispatch is None or dispatch.session_id != session_id:
            raise MeetingEssenceNotFound("derivation not found for this session")
        try:
            await self._scheduler.retry(dispatch_id=dispatch_id, session_id=session_id)
        except LookupError as exc:
            raise MeetingEssenceNotFound("derivation not found for this session") from exc
        except SummaryRetryDenied as exc:
            raise MeetingEssenceRetryDenied(str(exc)) from exc

    async def latest_job_for_session(self, session_id: UUID) -> MIIntelligenceJob | None:
        return await self._db.scalar(
            select(MIIntelligenceJob)
            .where(MIIntelligenceJob.session_id == session_id)
            .order_by(MIIntelligenceJob.created_at.desc())
            .limit(1)
        )

    async def latest_job_with_usable_artifact(self, session_id: UUID) -> MIIntelligenceJob | None:
        jobs = await self._db.scalars(
            select(MIIntelligenceJob)
            .where(MIIntelligenceJob.session_id == session_id)
            .order_by(MIIntelligenceJob.created_at.desc())
            .limit(24)
        )
        for job in jobs:
            artifact_id = job.current_artifact_id or job.stale_artifact_id
            if artifact_id is None:
                continue
            row = await self._db.get(MIArtifactVersion, artifact_id)
            if row is None or row.layered_result_json is None:
                continue
            return job
        return None
