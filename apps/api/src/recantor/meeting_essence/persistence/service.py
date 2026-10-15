from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.budget import EngineLimits
from recantor.meeting_essence.checkpoint_binding import limits_fingerprint
from recantor.meeting_essence.contracts import (
    ProcessingJobStatus,
    SourceCoverageFlags,
    SourceSnapshotManifest,
    StageOperation,
)
from recantor.meeting_essence.engine import (
    EngineRunConfig,
    EngineRunResult,
    EngineRunTrace,
    MeetingEssenceEngine,
)
from recantor.meeting_essence.persistence.caching_provider import (
    CachedMeetingEssenceProvider,
    entered_call_counts,
)
from recantor.meeting_essence.persistence.fingerprints import (
    compute_run_fingerprint,
    extraction_reuse_key,
)
from recantor.meeting_essence.persistence.invariants import checkpoint_accepts_success
from recantor.meeting_essence.persistence.models import MIJobStatus
from recantor.meeting_essence.persistence.repository import (
    AttemptBusyError,
    MeetingIntelligenceRepository,
    PersistedAttempt,
    PersistedJob,
    PublishedArtifact,
    UnresolvedWorkPage,
)
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.prompt_loader import PromptBundle, load_prompt_bundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.source_reader import (
    CanonicalSourceReader,
    materialize_manifest_from_reader,
)
from recantor.meeting_essence.stage_contracts import ExtractStageRequest
from recantor.meeting_essence.wire_serialize import serialized_json_text


@dataclass(frozen=True)
class IntelligenceRunContext:
    job: PersistedJob
    attempt: PersistedAttempt
    run_fingerprint: str
    binding_limits_fingerprint: str


@dataclass(frozen=True)
class IntelligenceRunOutcome:
    result: EngineRunResult
    job: PersistedJob
    attempt: PersistedAttempt
    provider_calls: int
    cache_hits: int
    extract_provider_calls: int
    served_from_completed_cache: bool


class MeetingIntelligenceLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._repo = MeetingIntelligenceRepository(db)
        self._db = db

    @property
    def repository(self) -> MeetingIntelligenceRepository:
        return self._repo

    async def register_snapshot(
        self,
        *,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        supersede_previous: bool = True,
        expected_source_epoch: int | None = None,
    ) -> SourceSnapshotManifest:
        stored = await self._repo.upsert_snapshot(
            session_id=session_id,
            manifest=manifest,
            supersede_previous=supersede_previous,
            expected_source_epoch=expected_source_epoch,
        )
        return stored.manifest

    async def prepare_run(
        self,
        *,
        session_id: UUID,
        client_request_id: UUID,
        manifest: SourceSnapshotManifest,
        provider_adapter_id: str,
        prompt_bundle: PromptBundle | None = None,
        limits: EngineLimits | None = None,
        requested_model: str | None = None,
        requested_reasoning_effort: str | None = None,
    ) -> IntelligenceRunContext:
        bundle = prompt_bundle or load_prompt_bundle()
        engine_limits = limits or EngineLimits()
        observed_epoch = await self._repo.observed_source_epoch(session_id)
        await self.register_snapshot(
            session_id=session_id,
            manifest=manifest,
            expected_source_epoch=observed_epoch,
        )
        limits_fp = limits_fingerprint(
            engine_limits,
            requested_model=requested_model,
            requested_reasoning_effort=requested_reasoning_effort,
        )
        run_fp = compute_run_fingerprint(
            source_content_hash=manifest.snapshot_content_hash,
            prompt_cache_key=bundle.cache_key,
            schema_identity=bundle.schema_identity,
            algorithm_identity=bundle.algorithm_identity,
            limits_fingerprint=limits_fp,
            provider_adapter_id=provider_adapter_id,
        )
        job = await self._repo.create_or_get_job(
            session_id=session_id,
            client_request_id=client_request_id,
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint=run_fp,
            prompt_cache_key=bundle.cache_key,
            schema_identity=bundle.schema_identity,
            algorithm_identity=bundle.algorithm_identity,
            limits_fingerprint=limits_fp,
            provider_adapter_id=provider_adapter_id,
            requested_model=requested_model or "",
            requested_reasoning_effort=requested_reasoning_effort or "",
        )
        try:
            attempt = await self._repo.begin_attempt(job_id=job.id, session_id=session_id)
        except AttemptBusyError:
            await self._db.rollback()
            raise
        await self._db.commit()
        return IntelligenceRunContext(
            job=job,
            attempt=attempt,
            run_fingerprint=run_fp,
            binding_limits_fingerprint=limits_fp,
        )

    async def try_served_completed_cache(
        self,
        *,
        session_id: UUID,
        run_fingerprint: str,
    ) -> IntelligenceRunOutcome | None:
        job = await self._repo.get_job_by_run_fingerprint(
            session_id=session_id, run_fingerprint=run_fingerprint
        )
        if job is None or job.status != MIJobStatus.SUCCEEDED or job.current_artifact_id is None:
            return None
        current_hash = await self._repo.current_source_hash(session_id)
        if current_hash != job.source_content_hash:
            return None
        published = await self._repo.load_current_publication(job_id=job.id, session_id=session_id)
        if published is None or not published.is_current or published.is_stale:
            return None
        if (
            published.processing_status != ProcessingJobStatus.SUCCEEDED
            or published.layered is None
        ):
            return None
        checkpoint = await self._repo.load_checkpoint(
            attempt_id=published.attempt_id, session_id=session_id
        )
        if checkpoint is None or not checkpoint_accepts_success(checkpoint):
            return None
        attempt = await self._repo.get_attempt(published.attempt_id, session_id=session_id)
        manifest = await self._repo.get_snapshot(
            session_id=session_id, snapshot_content_hash=job.source_content_hash
        )
        if manifest is None:
            return None
        trace = EngineRunTrace(
            manifest=manifest.manifest,
            prompt_cache_key=job.prompt_cache_key,
            schema_identity=job.schema_identity,
            algorithm_identity=job.algorithm_identity,
        )
        result = EngineRunResult(
            manifest=manifest.manifest,
            layered_result=published.layered,
            checkpoint=checkpoint,
            trace=trace,
            processing_status=published.processing_status,
            limitations=published.limitations,
        )
        return IntelligenceRunOutcome(
            result=result,
            job=job,
            attempt=attempt,
            provider_calls=0,
            cache_hits=1,
            extract_provider_calls=0,
            served_from_completed_cache=True,
        )

    async def run_engine(
        self,
        *,
        session_id: UUID,
        client_request_id: UUID,
        reader: CanonicalSourceReader,
        provider: MeetingEssenceProvider,
        publication_boundary_sequence: int,
        coverage: SourceCoverageFlags,
        meeting_metadata: dict[str, str] | None = None,
        provider_adapter_id: str = "deterministic-v1",
        prompt_bundle: PromptBundle | None = None,
        config: EngineRunConfig | None = None,
        resume_checkpoint: EngineCheckpoint | None = None,
        recover: bool = False,
        retry: bool = False,
        on_attempt_started: object | None = None,
    ) -> IntelligenceRunOutcome:
        supplied = config or EngineRunConfig()
        caller_hook = supplied.on_checkpoint_committed
        caller_validated = supplied.on_validated_stage
        reader.freeze_publication_boundary(publication_boundary_sequence)
        observed_epoch = await self._repo.observed_source_epoch(session_id)
        manifest, _coverage = materialize_manifest_from_reader(
            reader,
            session_id=session_id,
            coverage=coverage,
            meeting_metadata=meeting_metadata,
        )
        bundle = prompt_bundle or load_prompt_bundle()
        await self.register_snapshot(
            session_id=session_id,
            manifest=manifest,
            expected_source_epoch=observed_epoch,
        )
        limits_fp = limits_fingerprint(
            supplied.limits,
            requested_model=supplied.requested_model,
            requested_reasoning_effort=supplied.requested_reasoning_effort,
        )
        run_fp = compute_run_fingerprint(
            source_content_hash=manifest.snapshot_content_hash,
            prompt_cache_key=bundle.cache_key,
            schema_identity=bundle.schema_identity,
            algorithm_identity=bundle.algorithm_identity,
            limits_fingerprint=limits_fp,
            provider_adapter_id=provider_adapter_id,
        )
        job = await self._repo.create_or_get_job(
            session_id=session_id,
            client_request_id=client_request_id,
            snapshot_id=manifest.snapshot_id,
            source_content_hash=manifest.snapshot_content_hash,
            run_fingerprint=run_fp,
            prompt_cache_key=bundle.cache_key,
            schema_identity=bundle.schema_identity,
            algorithm_identity=bundle.algorithm_identity,
            limits_fingerprint=limits_fp,
            provider_adapter_id=provider_adapter_id,
            requested_model=supplied.requested_model or "",
            requested_reasoning_effort=supplied.requested_reasoning_effort or "",
        )
        if resume_checkpoint is None:
            cached = await self.try_served_completed_cache(
                session_id=session_id, run_fingerprint=run_fp
            )
            if cached is not None:
                await self._db.commit()
                return cached
        try:
            attempt = await self._repo.begin_attempt(
                job_id=job.id,
                session_id=session_id,
                recover=recover,
                retry=retry,
            )
        except AttemptBusyError:
            await self._db.rollback()
            raise
        await self._db.commit()
        if on_attempt_started is not None:
            await on_attempt_started(job, attempt)  # type: ignore[operator]
        checkpoint = self._usable_resume(
            resume_checkpoint, manifest=manifest, session_id=session_id
        )
        if checkpoint is None:
            checkpoint = await self._repo.load_checkpoint(
                attempt_id=attempt.id, session_id=session_id
            )
        cached_provider = CachedMeetingEssenceProvider(
            inner=provider,
            repository=self._repo,
            session_id=str(session_id),
            run_fingerprint=run_fp,
            source_content_hash=manifest.snapshot_content_hash,
            provider_adapter_id=provider_adapter_id,
            prompt_cache_key=bundle.cache_key,
            limits_fingerprint=limits_fp,
        )

        async def on_validated(
            operation: StageOperation, request: BaseModel, response: BaseModel
        ) -> None:
            if operation == StageOperation.EXTRACT and isinstance(request, ExtractStageRequest):
                key = extraction_reuse_key(
                    request,
                    provider_adapter_id=provider_adapter_id,
                    prompt_cache_key=bundle.cache_key,
                    limits_fingerprint=limits_fp,
                )
                await self._repo.put_validated_extraction(
                    session_id=session_id,
                    extraction_key=key,
                    response_json=serialized_json_text(response),
                    prompt_cache_key=bundle.cache_key,
                    schema_identity=bundle.schema_identity,
                    algorithm_identity=bundle.algorithm_identity,
                    limits_fingerprint=limits_fp,
                    provider_adapter_id=provider_adapter_id,
                    requested_model=supplied.requested_model or "",
                    requested_reasoning_effort=supplied.requested_reasoning_effort or "",
                    attempt_id=attempt.id,
                    job_id=job.id,
                    generation=attempt.generation,
                )
                await self._db.commit()
            if caller_validated is not None:
                await caller_validated(operation, request, response)

        async def on_commit(saved: EngineCheckpoint) -> None:
            await self._db.rollback()
            await self._repo.save_checkpoint(
                attempt_id=attempt.id,
                job_id=job.id,
                session_id=session_id,
                generation=attempt.generation,
                checkpoint=saved,
            )
            if saved.atomic_records:
                await self._repo.persist_atomic_records(
                    session_id=session_id,
                    snapshot_id=manifest.snapshot_id,
                    snapshot_content_hash=manifest.snapshot_content_hash,
                    records=saved.atomic_records,
                )
            await self._db.commit()
            if caller_hook is not None:
                await caller_hook(saved)

        isolated = replace(
            supplied,
            on_checkpoint_committed=on_commit,
            on_validated_stage=on_validated,
        )
        engine = MeetingEssenceEngine(
            provider=cached_provider, prompt_bundle=bundle, config=isolated
        )
        result = await engine.run_from_reader(
            reader,
            session_id=session_id,
            publication_boundary_sequence=publication_boundary_sequence,
            coverage=coverage,
            meeting_metadata=meeting_metadata,
            checkpoint=checkpoint,
        )
        publish = result.processing_status == ProcessingJobStatus.SUCCEEDED
        await self._repo.finalize_attempt(
            job_id=job.id,
            attempt_id=attempt.id,
            session_id=session_id,
            generation=attempt.generation,
            processing_status=result.processing_status,
            layered=result.layered_result,
            limitations=result.limitations,
            snapshot_id=manifest.snapshot_id,
            snapshot_content_hash=manifest.snapshot_content_hash,
            run_fingerprint=run_fp,
            publish_current=publish,
            mark_stale_previous=False,
            records=result.checkpoint.atomic_records,
            relations=result.checkpoint.relations,
        )
        await self._db.commit()
        refreshed = await self._repo.get_job(job.id, session_id=session_id)
        finished = await self._repo.get_attempt(attempt.id, session_id=session_id)
        provider_calls, extract_calls = entered_call_counts(cached_provider)
        return IntelligenceRunOutcome(
            result=result,
            job=refreshed,
            attempt=finished,
            provider_calls=provider_calls,
            cache_hits=cached_provider.cache_hits,
            extract_provider_calls=extract_calls,
            served_from_completed_cache=False,
        )

    @staticmethod
    def _usable_resume(
        checkpoint: EngineCheckpoint | None,
        *,
        manifest: SourceSnapshotManifest,
        session_id: UUID,
    ) -> EngineCheckpoint | None:
        if checkpoint is None:
            return None
        if checkpoint.snapshot_content_hash != manifest.snapshot_content_hash:
            return None
        if checkpoint.session_id not in {None, str(session_id)}:
            return None
        if checkpoint.snapshot_id not in {None, manifest.snapshot_id}:
            return None
        return checkpoint

    async def list_unresolved_work(
        self,
        *,
        session_id: UUID | None = None,
        limit: int = 32,
        cursor: str | None = None,
    ) -> UnresolvedWorkPage:
        return await self._repo.list_unresolved_work(
            session_id=session_id, limit=limit, cursor=cursor
        )

    async def select_visible_result(
        self, *, job_id: UUID, session_id: UUID
    ) -> PublishedArtifact | None:
        return await self._repo.select_visible_result(job_id=job_id, session_id=session_id)

    async def cancel_work(self, *, attempt_id: UUID, session_id: UUID, generation: int) -> None:
        await self._repo.cancel_attempt(
            attempt_id=attempt_id, session_id=session_id, generation=generation
        )
        await self._db.commit()
