from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from recantor.meeting_essence.checkpoint_binding import CHECKPOINT_BINDING_VERSION
from recantor.meeting_essence.contracts import (
    AtomicRecord,
    CoverageLimitation,
    EvidenceReference,
    LayeredMeetingResult,
    ProcessingJobStatus,
    RecordRelation,
    SourceSnapshotManifest,
)
from recantor.meeting_essence.identity import (
    canonical_meeting_metadata,
    compute_input_fingerprint,
    compute_snapshot_content_hash,
    compute_snapshot_id,
    segment_content_hash,
)
from recantor.meeting_essence.persistence.checkpoint_store import (
    deserialize_checkpoint,
    serialize_checkpoint,
)
from recantor.meeting_essence.persistence.invariants import (
    checkpoint_accepts_success,
    checkpoint_regresses,
    is_success_coverage,
    layered_claims_match,
    partial_coverage_claims_success,
    records_match,
    relations_match,
)
from recantor.meeting_essence.persistence.models import (
    MIArtifactVersion,
    MIAtomicRecordRow,
    MIAttemptCheckpoint,
    MIAttemptStatus,
    MIIntelligenceJob,
    MIJobAttempt,
    MIJobRequestAlias,
    MIJobStatus,
    MIRecordRelationRow,
    MISessionSourceFence,
    MISourceSnapshot,
    MIValidatedExtraction,
)
from recantor.meeting_essence.progress import EngineCheckpoint
from recantor.meeting_essence.validation import (
    validate_record_ledger,
    validate_result_against_manifest,
)
from recantor.models import TranscriptSegment


class MeetingIntelligenceRepositoryError(RuntimeError):
    pass


class SessionIsolationError(MeetingIntelligenceRepositoryError):
    pass


class StaleGenerationError(MeetingIntelligenceRepositoryError):
    pass


class AttemptFenceError(MeetingIntelligenceRepositoryError):
    pass


class IdempotencyConflictError(MeetingIntelligenceRepositoryError):
    pass


class SnapshotImmutableError(MeetingIntelligenceRepositoryError):
    pass


class EvidenceOwnershipError(MeetingIntelligenceRepositoryError):
    pass


class ArtifactConflictError(MeetingIntelligenceRepositoryError):
    pass


class ExtractionConflictError(MeetingIntelligenceRepositoryError):
    pass


class AttemptBusyError(MeetingIntelligenceRepositoryError):
    """A live attempt already owns this job. The caller does not receive that identity."""

    def __init__(self, job_id: UUID, attempt_id: UUID, generation: int) -> None:
        super().__init__("a live attempt already holds this job")
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.generation = generation


class CheckpointRegressionError(AttemptFenceError):
    pass


class PublicationIntegrityError(MeetingIntelligenceRepositoryError):
    """A durable write does not match an accepted checkpoint, ledger, or manifest."""


_UNRESOLVED_PAGE_DEFAULT = 32
_UNRESOLVED_PAGE_MAX = 64


@dataclass(frozen=True)
class UnresolvedWork:
    job: PersistedJob
    live_attempt_id: UUID | None
    live_generation: int | None


@dataclass(frozen=True)
class UnresolvedWorkPage:
    """One stable page of unresolved jobs. Fair dispatch remains #73."""

    items: tuple[UnresolvedWork, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class PersistedSnapshot:
    id: UUID
    session_id: UUID
    snapshot_id: str
    snapshot_content_hash: str
    manifest: SourceSnapshotManifest


@dataclass(frozen=True)
class PersistedJob:
    id: UUID
    session_id: UUID
    client_request_id: UUID
    run_fingerprint: str
    source_content_hash: str
    snapshot_id: str
    status: MIJobStatus
    active_generation: int
    current_artifact_id: UUID | None
    stale_artifact_id: UUID | None
    source_epoch: int
    prompt_cache_key: str
    schema_identity: str
    algorithm_identity: str
    limits_fingerprint: str
    provider_adapter_id: str
    requested_model: str
    requested_reasoning_effort: str


@dataclass(frozen=True)
class PersistedAttempt:
    id: UUID
    job_id: UUID
    session_id: UUID
    generation: int
    status: MIAttemptStatus
    source_epoch: int
    snapshot_id: str
    source_content_hash: str
    run_fingerprint: str


@dataclass(frozen=True)
class PublishedArtifact:
    id: UUID
    attempt_id: UUID
    job_id: UUID
    session_id: UUID
    is_current: bool
    is_stale: bool
    snapshot_id: str
    snapshot_content_hash: str
    processing_status: ProcessingJobStatus
    layered: LayeredMeetingResult | None
    limitations: list[CoverageLimitation]
    records_ledger: list[dict[str, str]]
    content_fingerprint: str


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _metadata_fingerprint(metadata: dict[str, str]) -> str:
    return _sha256(canonical_meeting_metadata(metadata))


def _snapshot_material(manifest: SourceSnapshotManifest) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("captured_at", None)
    return _canonical(payload)


_CHECKPOINT_REQUIRED = (
    "binding_version",
    "snapshot_id",
    "snapshot_content_hash",
    "prompt_cache_key",
    "schema_identity",
    "algorithm_identity",
    "input_fingerprint",
    "limits_fingerprint",
    "session_id",
)


def record_content_fingerprint(record: AtomicRecord) -> str:
    return _sha256(record.model_dump(mode="json"))


def relation_content_fingerprint(relation: RecordRelation) -> str:
    return _sha256(relation.model_dump(mode="json"))


def _layered_evidence(layered: LayeredMeetingResult) -> list[EvidenceReference]:
    evidence: list[EvidenceReference] = []
    for bullet in layered.overview_bullets:
        evidence.extend(bullet.evidence)
    for topic in layered.topics:
        for point in topic.points:
            evidence.extend(point.evidence)
    for action in layered.actions:
        evidence.extend(action.evidence)
    for item in layered.open_items:
        evidence.extend(item.evidence)
    for record in layered.atomic_records:
        evidence.extend(record.evidence)
    for relation in layered.relations:
        evidence.extend(relation.evidence)
    return evidence


def _parse_unresolved_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        stamp, raw_id = cursor.rsplit("|", 1)
        parsed = datetime.fromisoformat(stamp)
        job_id = UUID(raw_id)
    except (ValueError, AttributeError) as exc:
        msg = "unresolved-work cursor is invalid"
        raise MeetingIntelligenceRepositoryError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, job_id


def _status_value(status: ProcessingJobStatus) -> str:
    if status == ProcessingJobStatus.RUNNING:
        return ProcessingJobStatus.PARTIAL.value
    return status.value


class MeetingIntelligenceRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def _assert_session(self, expected: UUID, actual: UUID, *, context: str) -> None:
        if expected != actual:
            msg = f"{context}: session isolation violation"
            raise SessionIsolationError(msg)

    async def _lock_fence(self, session_id: UUID) -> MISessionSourceFence:
        await self._db.execute(
            insert(MISessionSourceFence)
            .values(session_id=session_id, source_epoch=0)
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        fence = await self._db.scalar(
            select(MISessionSourceFence)
            .where(MISessionSourceFence.session_id == session_id)
            .with_for_update()
        )
        if fence is None:
            msg = "session source fence missing"
            raise MeetingIntelligenceRepositoryError(msg)
        return fence

    async def current_source_hash(self, session_id: UUID) -> str | None:
        fence = await self._db.scalar(
            select(MISessionSourceFence).where(MISessionSourceFence.session_id == session_id)
        )
        if fence is None:
            return None
        return fence.current_snapshot_content_hash

    async def observed_source_epoch(self, session_id: UUID) -> int:
        """Epoch visible to this transaction. A later promotion must send this value back."""
        fence = await self._db.scalar(
            select(MISessionSourceFence).where(MISessionSourceFence.session_id == session_id)
        )
        if fence is None:
            return 0
        return fence.source_epoch

    async def upsert_snapshot(
        self,
        *,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        supersede_previous: bool = False,
        expected_source_epoch: int | None = None,
    ) -> PersistedSnapshot:
        if manifest.session_id != session_id:
            msg = "snapshot manifest session does not match the requested owner"
            raise SessionIsolationError(msg)
        self._assert_manifest_identity(manifest)
        await self._assert_canonical_ownership(session_id, manifest)
        body = manifest.model_dump_json()
        inserted = await self._db.execute(
            insert(MISourceSnapshot)
            .values(
                id=uuid4(),
                session_id=session_id,
                manifest_session_id=manifest.session_id,
                snapshot_id=manifest.snapshot_id,
                snapshot_content_hash=manifest.snapshot_content_hash,
                publication_boundary_sequence=manifest.publication_boundary_sequence,
                manifest_json=body,
                superseded_by_snapshot_id=None,
            )
            .on_conflict_do_nothing(index_elements=["session_id", "snapshot_content_hash"])
            .returning(MISourceSnapshot.id)
        )
        newly_inserted = inserted.scalar_one_or_none() is not None
        row = await self._db.scalar(
            select(MISourceSnapshot).where(
                MISourceSnapshot.session_id == session_id,
                MISourceSnapshot.snapshot_content_hash == manifest.snapshot_content_hash,
            )
        )
        if row is None:
            msg = "failed to persist source snapshot"
            raise MeetingIntelligenceRepositoryError(msg)
        if row.snapshot_id != manifest.snapshot_id or row.session_id != session_id:
            msg = "snapshot identity is immutable"
            raise SnapshotImmutableError(msg)
        stored = SourceSnapshotManifest.model_validate_json(row.manifest_json)
        self._assert_same_snapshot_material(stored, manifest)
        if (
            stored.session_id != session_id
            or stored.snapshot_content_hash != row.snapshot_content_hash
        ):
            msg = "stored snapshot does not belong to this session"
            raise SessionIsolationError(msg)
        await self._advance_source_epoch(
            session_id=session_id,
            snapshot_id=row.snapshot_id,
            content_hash=row.snapshot_content_hash,
            publication_boundary=row.publication_boundary_sequence,
            metadata_fingerprint=_metadata_fingerprint(stored.meeting_metadata),
            newly_inserted=newly_inserted,
            supersede_previous=supersede_previous,
            expected_source_epoch=expected_source_epoch,
        )
        await self._db.refresh(row)
        stored = SourceSnapshotManifest.model_validate_json(row.manifest_json)
        return PersistedSnapshot(
            id=row.id,
            session_id=row.session_id,
            snapshot_id=row.snapshot_id,
            snapshot_content_hash=row.snapshot_content_hash,
            manifest=stored,
        )

    @staticmethod
    def _assert_manifest_identity(manifest: SourceSnapshotManifest) -> None:
        expected_hash = compute_snapshot_content_hash(manifest)
        expected_id = compute_snapshot_id(str(manifest.session_id), expected_hash)
        if manifest.snapshot_content_hash != expected_hash or manifest.snapshot_id != expected_id:
            msg = "snapshot hash or id does not match the manifest body"
            raise SnapshotImmutableError(msg)

    @staticmethod
    def _assert_same_snapshot_material(
        stored: SourceSnapshotManifest, supplied: SourceSnapshotManifest
    ) -> None:
        if _snapshot_material(stored) != _snapshot_material(supplied):
            msg = "snapshot content hash already stores different immutable material"
            raise SnapshotImmutableError(msg)

    async def _assert_canonical_ownership(
        self, session_id: UUID, manifest: SourceSnapshotManifest
    ) -> None:
        """Manifest segments must match canonical rows. The transcript is not written."""
        if not manifest.segments:
            if manifest.publication_boundary_sequence != 0:
                msg = "publication boundary exceeds canonical sequences"
                raise SnapshotImmutableError(msg)
            return
        rows = (
            await self._db.scalars(
                select(TranscriptSegment).where(
                    TranscriptSegment.id.in_([segment.id for segment in manifest.segments])
                )
            )
        ).all()
        by_id = {row.id: row for row in rows}
        for segment in manifest.segments:
            row = by_id.get(segment.id)
            if (
                row is None
                or row.session_id != session_id
                or row.sequence != segment.sequence
                or row.start_ms != segment.start_ms
                or row.end_ms != segment.end_ms
                or row.text != segment.text
                or segment_content_hash(row.text) != segment.content_hash
                or (segment.language is not None and row.language != segment.language)
            ):
                msg = "manifest segment does not match a canonical transcript row in this session"
                raise SnapshotImmutableError(msg)
        max_sequence = await self._db.scalar(
            select(func.max(TranscriptSegment.sequence)).where(
                TranscriptSegment.session_id == session_id
            )
        )
        if manifest.publication_boundary_sequence > int(max_sequence or 0):
            msg = "publication boundary exceeds canonical sequences"
            raise SnapshotImmutableError(msg)

    async def _advance_source_epoch(
        self,
        *,
        session_id: UUID,
        snapshot_id: str,
        content_hash: str,
        publication_boundary: int,
        metadata_fingerprint: str,
        newly_inserted: bool,
        supersede_previous: bool,
        expected_source_epoch: int | None,
    ) -> MISessionSourceFence:
        """Promote source authority only when the candidate is newer than the fence.

        A higher publication boundary may become current. Equal-boundary metadata
        becomes current only when ``expected_source_epoch`` is the epoch the caller
        observed. A stale read cannot replace a newer accepted metadata version
        merely because its own variant was not stored yet. Spoken time is not an
        ordering key.
        """
        fence = await self._lock_fence(session_id)
        if fence.current_snapshot_content_hash == content_hash:
            return fence
        await self._hydrate_authority(fence)
        candidate = await self._db.scalar(
            select(MISourceSnapshot).where(
                MISourceSnapshot.session_id == session_id,
                MISourceSnapshot.snapshot_content_hash == content_hash,
            )
        )
        if candidate is None:
            msg = "candidate snapshot disappeared before authority compare"
            raise MeetingIntelligenceRepositoryError(msg)
        current_hash = fence.current_snapshot_content_hash
        current_boundary = fence.authority_publication_boundary
        if current_hash is None or current_boundary is None:
            await self._cas_source_authority(
                fence,
                snapshot_id=snapshot_id,
                content_hash=content_hash,
                publication_boundary=publication_boundary,
                metadata_fingerprint=metadata_fingerprint,
                supersede_previous=supersede_previous,
            )
            return fence
        if publication_boundary < current_boundary or (
            publication_boundary == current_boundary and not newly_inserted
        ):
            if candidate.superseded_by_snapshot_id is None and fence.current_snapshot_id:
                candidate.superseded_by_snapshot_id = fence.current_snapshot_id
            await self._db.flush()
            return fence
        already_superseded = candidate.superseded_by_snapshot_id not in {
            None,
            fence.current_snapshot_id,
        }
        if already_superseded and not newly_inserted and publication_boundary <= current_boundary:
            return fence
        if (
            publication_boundary == current_boundary
            and newly_inserted
            and expected_source_epoch != fence.source_epoch
        ):
            if candidate.superseded_by_snapshot_id is None and fence.current_snapshot_id:
                candidate.superseded_by_snapshot_id = fence.current_snapshot_id
            await self._db.flush()
            return fence
        await self._cas_source_authority(
            fence,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            publication_boundary=publication_boundary,
            metadata_fingerprint=metadata_fingerprint,
            supersede_previous=supersede_previous,
        )
        return fence

    async def _hydrate_authority(self, fence: MISessionSourceFence) -> None:
        if fence.current_snapshot_content_hash is None:
            return
        if (
            fence.authority_publication_boundary is not None
            and fence.authority_metadata_fingerprint is not None
        ):
            return
        current = await self._db.scalar(
            select(MISourceSnapshot).where(
                MISourceSnapshot.session_id == fence.session_id,
                MISourceSnapshot.snapshot_content_hash == fence.current_snapshot_content_hash,
            )
        )
        if current is None:
            return
        stored = SourceSnapshotManifest.model_validate_json(current.manifest_json)
        fence.authority_publication_boundary = current.publication_boundary_sequence
        fence.authority_metadata_fingerprint = _metadata_fingerprint(stored.meeting_metadata)
        await self._db.flush()

    async def _cas_source_authority(
        self,
        fence: MISessionSourceFence,
        *,
        snapshot_id: str,
        content_hash: str,
        publication_boundary: int,
        metadata_fingerprint: str,
        supersede_previous: bool,
    ) -> None:
        expected_epoch = fence.source_epoch
        expected_hash = fence.current_snapshot_content_hash
        new_epoch = 1 if expected_hash is None and expected_epoch == 0 else expected_epoch + 1
        if expected_hash is not None:
            await self._invalidate_previous_source(
                session_id=fence.session_id, content_hash=content_hash
            )
        hash_match = (
            MISessionSourceFence.current_snapshot_content_hash.is_(None)
            if expected_hash is None
            else MISessionSourceFence.current_snapshot_content_hash == expected_hash
        )
        result = await self._db.execute(
            update(MISessionSourceFence)
            .where(
                MISessionSourceFence.session_id == fence.session_id,
                MISessionSourceFence.source_epoch == expected_epoch,
                hash_match,
            )
            .values(
                source_epoch=new_epoch,
                current_snapshot_id=snapshot_id,
                current_snapshot_content_hash=content_hash,
                authority_publication_boundary=publication_boundary,
                authority_metadata_fingerprint=metadata_fingerprint,
                updated_at=_utcnow(),
            )
        )
        if result.rowcount != 1:
            msg = "source authority compare-and-swap lost to a newer publication"
            raise StaleGenerationError(msg)
        if supersede_previous:
            await self._db.execute(
                update(MISourceSnapshot)
                .where(
                    MISourceSnapshot.session_id == fence.session_id,
                    MISourceSnapshot.snapshot_content_hash != content_hash,
                    MISourceSnapshot.superseded_by_snapshot_id.is_(None),
                )
                .values(superseded_by_snapshot_id=snapshot_id)
            )
        await self._db.refresh(fence)

    async def _invalidate_previous_source(self, *, session_id: UUID, content_hash: str) -> None:
        await self._db.execute(
            update(MIArtifactVersion)
            .where(
                MIArtifactVersion.session_id == session_id,
                MIArtifactVersion.is_current.is_(True),
                MIArtifactVersion.snapshot_content_hash != content_hash,
            )
            .values(is_current=False, is_stale=True)
        )
        await self._db.execute(
            update(MIIntelligenceJob)
            .where(
                MIIntelligenceJob.session_id == session_id,
                MIIntelligenceJob.source_content_hash != content_hash,
                MIIntelligenceJob.current_artifact_id.is_not(None),
            )
            .values(
                stale_artifact_id=func.coalesce(
                    MIIntelligenceJob.stale_artifact_id,
                    MIIntelligenceJob.current_artifact_id,
                ),
                current_artifact_id=None,
            )
        )
        await self._db.execute(
            update(MIIntelligenceJob)
            .where(
                MIIntelligenceJob.session_id == session_id,
                MIIntelligenceJob.source_content_hash != content_hash,
                MIIntelligenceJob.status.in_(
                    [
                        MIJobStatus.SUCCEEDED.value,
                        MIJobStatus.PARTIAL.value,
                        MIJobStatus.RUNNING.value,
                    ]
                ),
            )
            .values(status=MIJobStatus.PARTIAL.value)
        )

    async def get_snapshot(
        self, *, session_id: UUID, snapshot_content_hash: str
    ) -> PersistedSnapshot | None:
        row = await self._db.scalar(
            select(MISourceSnapshot).where(
                MISourceSnapshot.session_id == session_id,
                MISourceSnapshot.snapshot_content_hash == snapshot_content_hash,
            )
        )
        if row is None:
            return None
        manifest = SourceSnapshotManifest.model_validate_json(row.manifest_json)
        if manifest.session_id != session_id:
            msg = "stored snapshot manifest crossed sessions"
            raise SessionIsolationError(msg)
        return PersistedSnapshot(
            id=row.id,
            session_id=row.session_id,
            snapshot_id=row.snapshot_id,
            snapshot_content_hash=row.snapshot_content_hash,
            manifest=manifest,
        )

    async def create_or_get_job(
        self,
        *,
        session_id: UUID,
        client_request_id: UUID,
        snapshot_id: str,
        source_content_hash: str,
        run_fingerprint: str,
        prompt_cache_key: str,
        schema_identity: str,
        algorithm_identity: str,
        limits_fingerprint: str,
        provider_adapter_id: str,
        requested_model: str = "",
        requested_reasoning_effort: str = "",
    ) -> PersistedJob:
        expected = {
            "snapshot_id": snapshot_id,
            "source_content_hash": source_content_hash,
            "run_fingerprint": run_fingerprint,
            "prompt_cache_key": prompt_cache_key,
            "schema_identity": schema_identity,
            "algorithm_identity": algorithm_identity,
            "limits_fingerprint": limits_fingerprint,
            "provider_adapter_id": provider_adapter_id,
            "requested_model": requested_model,
            "requested_reasoning_effort": requested_reasoning_effort,
        }
        alias = await self._db.scalar(
            select(MIJobRequestAlias).where(
                MIJobRequestAlias.client_request_id == client_request_id
            )
        )
        if alias is not None:
            self._assert_session(session_id, alias.session_id, context="client_request_id")
            self._assert_alias(alias, expected)
            return await self.get_job(alias.job_id, session_id=session_id)
        existing = await self._db.scalar(
            select(MIIntelligenceJob).where(
                MIIntelligenceJob.client_request_id == client_request_id
            )
        )
        if existing is not None:
            self._assert_session(session_id, existing.session_id, context="client_request_id")
            self._assert_same_request(existing, **expected)
            await self._bind_request_alias(
                client_request_id=client_request_id,
                job=existing,
                expected=expected,
            )
            return self._job_view(existing)
        by_fingerprint = await self.get_job_by_run_fingerprint(
            session_id=session_id, run_fingerprint=run_fingerprint
        )
        if by_fingerprint is not None:
            row = await self._db.scalar(
                select(MIIntelligenceJob).where(MIIntelligenceJob.id == by_fingerprint.id)
            )
            if row is None:
                msg = "fingerprinted job disappeared"
                raise MeetingIntelligenceRepositoryError(msg)
            self._assert_same_request(row, **expected)
            await self._bind_request_alias(
                client_request_id=client_request_id,
                job=row,
                expected=expected,
            )
            return self._job_view(row)
        fence = await self._lock_fence(session_id)
        job_id = uuid4()
        try:
            async with self._db.begin_nested():
                self._db.add(
                    MIIntelligenceJob(
                        id=job_id,
                        session_id=session_id,
                        client_request_id=client_request_id,
                        snapshot_id=snapshot_id,
                        source_content_hash=source_content_hash,
                        source_epoch=fence.source_epoch,
                        run_fingerprint=run_fingerprint,
                        prompt_cache_key=prompt_cache_key,
                        schema_identity=schema_identity,
                        algorithm_identity=algorithm_identity,
                        limits_fingerprint=limits_fingerprint,
                        provider_adapter_id=provider_adapter_id,
                        requested_model=requested_model,
                        requested_reasoning_effort=requested_reasoning_effort,
                        status=MIJobStatus.PENDING.value,
                    )
                )
                await self._db.flush()
        except IntegrityError:
            raced = await self._db.scalar(
                select(MIIntelligenceJob).where(
                    MIIntelligenceJob.client_request_id == client_request_id
                )
            )
            if raced is None:
                raced = await self._db.scalar(
                    select(MIIntelligenceJob).where(
                        MIIntelligenceJob.session_id == session_id,
                        MIIntelligenceJob.run_fingerprint == run_fingerprint,
                    )
                )
            if raced is None:
                raise
            self._assert_session(session_id, raced.session_id, context="job")
            self._assert_same_request(raced, **expected)
            await self._bind_request_alias(
                client_request_id=client_request_id,
                job=raced,
                expected=expected,
            )
            return self._job_view(raced)
        row = await self._db.scalar(select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id))
        if row is None:
            msg = "failed to create intelligence job"
            raise MeetingIntelligenceRepositoryError(msg)
        await self._bind_request_alias(
            client_request_id=client_request_id,
            job=row,
            expected=expected,
        )
        return self._job_view(row)

    async def _bind_request_alias(
        self,
        *,
        client_request_id: UUID,
        job: MIIntelligenceJob,
        expected: dict[str, str],
    ) -> None:
        values = {
            "client_request_id": client_request_id,
            "job_id": job.id,
            "session_id": job.session_id,
            **expected,
        }
        await self._db.execute(
            insert(MIJobRequestAlias)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["client_request_id"])
        )
        alias = await self._db.scalar(
            select(MIJobRequestAlias).where(
                MIJobRequestAlias.client_request_id == client_request_id
            )
        )
        if alias is None:
            msg = "failed to reserve client request id"
            raise MeetingIntelligenceRepositoryError(msg)
        if alias.job_id != job.id:
            msg = "client_request_id is already reserved for a different job"
            raise IdempotencyConflictError(msg)
        self._assert_alias(alias, expected)

    @staticmethod
    def _assert_alias(alias: MIJobRequestAlias, expected: dict[str, str]) -> None:
        actual = {
            "snapshot_id": alias.snapshot_id,
            "source_content_hash": alias.source_content_hash,
            "run_fingerprint": alias.run_fingerprint,
            "prompt_cache_key": alias.prompt_cache_key,
            "schema_identity": alias.schema_identity,
            "algorithm_identity": alias.algorithm_identity,
            "limits_fingerprint": alias.limits_fingerprint,
            "provider_adapter_id": alias.provider_adapter_id,
            "requested_model": alias.requested_model,
            "requested_reasoning_effort": alias.requested_reasoning_effort,
        }
        if actual != expected:
            msg = "client_request_id was reused with a different source or configuration"
            raise IdempotencyConflictError(msg)

    def _assert_same_request(self, row: MIIntelligenceJob, **expected: str) -> None:
        actual = {
            "snapshot_id": row.snapshot_id,
            "source_content_hash": row.source_content_hash,
            "run_fingerprint": row.run_fingerprint,
            "prompt_cache_key": row.prompt_cache_key,
            "schema_identity": row.schema_identity,
            "algorithm_identity": row.algorithm_identity,
            "limits_fingerprint": row.limits_fingerprint,
            "provider_adapter_id": row.provider_adapter_id,
            "requested_model": row.requested_model,
            "requested_reasoning_effort": row.requested_reasoning_effort,
        }
        if actual != expected:
            msg = "client_request_id was reused with a different source or configuration"
            raise IdempotencyConflictError(msg)

    async def get_job_by_run_fingerprint(
        self, *, session_id: UUID, run_fingerprint: str
    ) -> PersistedJob | None:
        row = await self._db.scalar(
            select(MIIntelligenceJob).where(
                MIIntelligenceJob.session_id == session_id,
                MIIntelligenceJob.run_fingerprint == run_fingerprint,
            )
        )
        if row is None:
            return None
        return self._job_view(row)

    async def get_job(self, job_id: UUID, *, session_id: UUID) -> PersistedJob:
        row = await self._db.scalar(select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id))
        if row is None:
            msg = "intelligence job not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, row.session_id, context="get_job")
        return self._job_view(row)

    async def begin_attempt(
        self,
        *,
        job_id: UUID,
        session_id: UUID,
        reclaim: bool = False,
        recover: bool = False,
        retry: bool = False,
    ) -> PersistedAttempt:
        """Acquire an exclusive attempt.

        ``reclaim`` does not share a live identity. A live attempt returns
        ``AttemptBusyError``. ``recover`` fences a new generation and copies the
        validated checkpoint. ``retry`` is required to start work after user cancel.
        Lock order is session fence, then job, then attempt.
        """
        del reclaim
        fence = await self._lock_fence(session_id)
        job = await self._db.scalar(
            select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id).with_for_update()
        )
        if job is None:
            msg = "intelligence job not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, job.session_id, context="begin_attempt")
        if job.status == MIJobStatus.CANCELLED.value and not retry:
            msg = "cancelled job requires an explicit retry"
            raise AttemptFenceError(msg)
        running = await self._db.scalar(
            select(MIJobAttempt)
            .where(
                MIJobAttempt.job_id == job.id,
                MIJobAttempt.session_id == session_id,
                MIJobAttempt.status == MIAttemptStatus.RUNNING.value,
            )
            .with_for_update()
        )
        prior_body: str | None = None
        if running is not None and not recover:
            raise AttemptBusyError(job.id, running.id, running.generation)
        if running is not None and recover:
            prior = await self._db.scalar(
                select(MIAttemptCheckpoint).where(MIAttemptCheckpoint.attempt_id == running.id)
            )
            if prior is not None:
                checkpoint = deserialize_checkpoint(prior.body_json)
                await self._assert_checkpoint_bindings(checkpoint, job=job, attempt=running)
                prior_body = prior.body_json
            running.status = MIAttemptStatus.SUPERSEDED.value
            running.finished_at = _utcnow()
        generation = job.active_generation + 1
        result = await self._db.execute(
            update(MIIntelligenceJob)
            .where(
                MIIntelligenceJob.id == job.id,
                MIIntelligenceJob.session_id == session_id,
                MIIntelligenceJob.active_generation == job.active_generation,
            )
            .values(
                active_generation=generation,
                status=MIJobStatus.RUNNING.value,
                updated_at=_utcnow(),
            )
        )
        if result.rowcount != 1:
            msg = "attempt acquire lost the generation compare-and-swap"
            raise StaleGenerationError(msg)
        attempt = MIJobAttempt(
            id=uuid4(),
            job_id=job.id,
            session_id=session_id,
            generation=generation,
            source_epoch=fence.source_epoch,
            snapshot_id=job.snapshot_id,
            source_content_hash=job.source_content_hash,
            run_fingerprint=job.run_fingerprint,
            status=MIAttemptStatus.RUNNING.value,
        )
        self._db.add(attempt)
        await self._db.flush()
        if prior_body is not None:
            copied = deserialize_checkpoint(prior_body)
            self._db.add(
                MIAttemptCheckpoint(
                    attempt_id=attempt.id,
                    session_id=attempt.session_id,
                    job_id=attempt.job_id,
                    generation=attempt.generation,
                    checkpoint_id=copied.checkpoint_id,
                    body_json=prior_body,
                )
            )
            await self._db.flush()
        return self._attempt_view(attempt)

    async def cancel_attempt(self, *, attempt_id: UUID, session_id: UUID, generation: int) -> None:
        await self._lock_fence(session_id)
        preview = await self._db.scalar(select(MIJobAttempt).where(MIJobAttempt.id == attempt_id))
        if preview is None:
            msg = "attempt not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, preview.session_id, context="cancel_attempt")
        job = await self._db.scalar(
            select(MIIntelligenceJob)
            .where(MIIntelligenceJob.id == preview.job_id)
            .with_for_update()
        )
        attempt = await self._db.scalar(
            select(MIJobAttempt).where(MIJobAttempt.id == attempt_id).with_for_update()
        )
        if job is None or attempt is None or job.session_id != session_id:
            msg = "cancel could not lock the job fence"
            raise AttemptFenceError(msg)
        if attempt.generation != generation or job.active_generation != generation:
            msg = "cancel generation does not match the attempt"
            raise StaleGenerationError(msg)
        if attempt.status == MIAttemptStatus.CANCELLED.value:
            if job.status == MIJobStatus.RUNNING.value:
                job.status = MIJobStatus.CANCELLED.value
                job.updated_at = _utcnow()
                await self._db.flush()
            return
        if attempt.status != MIAttemptStatus.RUNNING.value:
            msg = "only a running attempt can be cancelled"
            raise AttemptFenceError(msg)
        attempt.status = MIAttemptStatus.CANCELLED.value
        attempt.finished_at = _utcnow()
        job.status = MIJobStatus.CANCELLED.value
        job.updated_at = _utcnow()
        await self._db.flush()

    async def get_attempt(self, attempt_id: UUID, *, session_id: UUID) -> PersistedAttempt:
        attempt = await self._db.scalar(select(MIJobAttempt).where(MIJobAttempt.id == attempt_id))
        if attempt is None:
            msg = "attempt not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, attempt.session_id, context="get_attempt")
        return self._attempt_view(attempt)

    async def load_checkpoint(
        self, *, attempt_id: UUID, session_id: UUID
    ) -> EngineCheckpoint | None:
        attempt = await self._db.scalar(select(MIJobAttempt).where(MIJobAttempt.id == attempt_id))
        if attempt is None:
            return None
        self._assert_session(session_id, attempt.session_id, context="load_checkpoint")
        row = await self._db.scalar(
            select(MIAttemptCheckpoint).where(
                MIAttemptCheckpoint.attempt_id == attempt_id,
                MIAttemptCheckpoint.session_id == session_id,
                MIAttemptCheckpoint.job_id == attempt.job_id,
                MIAttemptCheckpoint.generation == attempt.generation,
            )
        )
        if row is None:
            return None
        job = await self._db.scalar(
            select(MIIntelligenceJob).where(MIIntelligenceJob.id == attempt.job_id)
        )
        if job is None:
            return None
        checkpoint = deserialize_checkpoint(row.body_json)
        await self._assert_checkpoint_bindings(checkpoint, job=job, attempt=attempt)
        return checkpoint

    async def _running_attempt(
        self, *, attempt_id: UUID, job_id: UUID, session_id: UUID, generation: int
    ) -> tuple[MIJobAttempt, MIIntelligenceJob]:
        await self._lock_fence(session_id)
        job = await self._db.scalar(
            select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id).with_for_update()
        )
        attempt = await self._db.scalar(
            select(MIJobAttempt).where(MIJobAttempt.id == attempt_id).with_for_update()
        )
        if attempt is None:
            msg = "attempt not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, attempt.session_id, context="attempt fence")
        if attempt.job_id != job_id:
            msg = "attempt is not owned by the supplied job"
            raise AttemptFenceError(msg)
        if job is None or job.session_id != session_id:
            msg = "job does not own this attempt"
            raise AttemptFenceError(msg)
        if attempt.generation != generation or job.active_generation != generation:
            msg = "attempt generation mismatch"
            raise StaleGenerationError(msg)
        if (
            attempt.status != MIAttemptStatus.RUNNING.value
            or job.status == MIJobStatus.CANCELLED.value
        ):
            msg = "attempt is not running"
            raise AttemptFenceError(msg)
        if (
            job.snapshot_id != attempt.snapshot_id
            or job.source_content_hash != attempt.source_content_hash
            or job.run_fingerprint != attempt.run_fingerprint
        ):
            msg = "attempt is not bound to the job source"
            raise AttemptFenceError(msg)
        return attempt, job

    async def _assert_checkpoint_bindings(
        self,
        checkpoint: EngineCheckpoint,
        *,
        job: MIIntelligenceJob,
        attempt: MIJobAttempt,
    ) -> None:
        if checkpoint.binding_version != CHECKPOINT_BINDING_VERSION:
            if not checkpoint.binding_version:
                msg = "checkpoint binding binding_version is unbound"
                raise AttemptFenceError(msg)
            msg = "checkpoint binding version is not recognized"
            raise AttemptFenceError(msg)
        for name in _CHECKPOINT_REQUIRED:
            value = getattr(checkpoint, name)
            if value is None or value == "":
                msg = f"checkpoint binding {name} is unbound"
                raise AttemptFenceError(msg)
        if checkpoint.requested_model is None or checkpoint.requested_reasoning_effort is None:
            msg = "checkpoint model or effort is unbound"
            raise AttemptFenceError(msg)
        snapshot = await self.get_snapshot(
            session_id=attempt.session_id,
            snapshot_content_hash=attempt.source_content_hash,
        )
        if snapshot is None or snapshot.snapshot_id != attempt.snapshot_id:
            msg = "checkpoint snapshot is not stored for this session"
            raise AttemptFenceError(msg)
        if checkpoint.input_fingerprint != compute_input_fingerprint(snapshot.manifest):
            msg = "checkpoint input fingerprint does not match the frozen manifest"
            raise AttemptFenceError(msg)
        if (
            checkpoint.snapshot_id != attempt.snapshot_id
            or checkpoint.snapshot_content_hash != attempt.source_content_hash
            or checkpoint.session_id != str(attempt.session_id)
            or checkpoint.prompt_cache_key != job.prompt_cache_key
            or checkpoint.schema_identity != job.schema_identity
            or checkpoint.algorithm_identity != job.algorithm_identity
            or checkpoint.limits_fingerprint != job.limits_fingerprint
            or checkpoint.requested_model != job.requested_model
            or checkpoint.requested_reasoning_effort != job.requested_reasoning_effort
        ):
            msg = "checkpoint bindings do not match the owning job"
            raise AttemptFenceError(msg)

    async def save_checkpoint(
        self,
        *,
        attempt_id: UUID,
        job_id: UUID,
        session_id: UUID,
        generation: int,
        checkpoint: EngineCheckpoint,
    ) -> None:
        attempt, job = await self._running_attempt(
            attempt_id=attempt_id, job_id=job_id, session_id=session_id, generation=generation
        )
        await self._assert_checkpoint_bindings(checkpoint, job=job, attempt=attempt)
        existing = await self._db.scalar(
            select(MIAttemptCheckpoint).where(MIAttemptCheckpoint.attempt_id == attempt.id)
        )
        if existing is not None:
            previous = deserialize_checkpoint(existing.body_json)
            if checkpoint_regresses(previous, checkpoint):
                msg = "checkpoint would move an acked cursor backwards"
                raise CheckpointRegressionError(msg)
        body = serialize_checkpoint(checkpoint)
        await self._db.execute(
            insert(MIAttemptCheckpoint)
            .values(
                attempt_id=attempt.id,
                session_id=attempt.session_id,
                job_id=attempt.job_id,
                generation=attempt.generation,
                checkpoint_id=checkpoint.checkpoint_id,
                body_json=body,
            )
            .on_conflict_do_update(
                index_elements=["attempt_id"],
                set_={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "body_json": body,
                    "generation": attempt.generation,
                    "job_id": attempt.job_id,
                    "session_id": attempt.session_id,
                    "updated_at": _utcnow(),
                },
            )
        )

    async def get_validated_extraction(
        self, *, session_id: UUID, extraction_key: str
    ) -> str | None:
        return await self._db.scalar(
            select(MIValidatedExtraction.response_json).where(
                MIValidatedExtraction.session_id == session_id,
                MIValidatedExtraction.extraction_key == extraction_key,
            )
        )

    async def put_validated_extraction(
        self,
        *,
        session_id: UUID,
        extraction_key: str,
        response_json: str,
        prompt_cache_key: str,
        schema_identity: str,
        algorithm_identity: str,
        limits_fingerprint: str,
        provider_adapter_id: str,
        requested_model: str,
        requested_reasoning_effort: str,
        attempt_id: UUID,
        job_id: UUID,
        generation: int,
    ) -> None:
        await self._running_attempt(
            attempt_id=attempt_id, job_id=job_id, session_id=session_id, generation=generation
        )
        existing = await self._db.scalar(
            select(MIValidatedExtraction).where(
                MIValidatedExtraction.session_id == session_id,
                MIValidatedExtraction.extraction_key == extraction_key,
            )
        )
        if existing is not None:
            if existing.response_json != response_json:
                msg = "validated extraction key already stores different content"
                raise ExtractionConflictError(msg)
            return
        self._db.add(
            MIValidatedExtraction(
                id=uuid4(),
                session_id=session_id,
                extraction_key=extraction_key,
                prompt_cache_key=prompt_cache_key,
                schema_identity=schema_identity,
                algorithm_identity=algorithm_identity,
                limits_fingerprint=limits_fingerprint,
                provider_adapter_id=provider_adapter_id,
                requested_model=requested_model,
                requested_reasoning_effort=requested_reasoning_effort,
                response_json=response_json,
                attempt_id=attempt_id,
            )
        )
        await self._db.flush()

    async def resolve_record_evidence(
        self,
        *,
        session_id: UUID,
        snapshot_content_hash: str,
        record: AtomicRecord,
    ) -> None:
        snapshot = await self.get_snapshot(
            session_id=session_id, snapshot_content_hash=snapshot_content_hash
        )
        if snapshot is None:
            msg = "evidence snapshot is not owned by this session"
            raise EvidenceOwnershipError(msg)
        segments = {segment.id: segment for segment in snapshot.manifest.segments}
        for evidence in record.evidence:
            await self._resolve_one_evidence(
                session_id=session_id, evidence=evidence, segments=segments
            )

    async def _resolve_one_evidence(
        self,
        *,
        session_id: UUID,
        evidence: EvidenceReference,
        segments: dict[UUID, Any],
    ) -> None:
        manifest_segment = segments.get(evidence.segment_id)
        row = await self._db.get(TranscriptSegment, evidence.segment_id)
        if (
            manifest_segment is None
            or row is None
            or row.session_id != session_id
            or row.start_ms != evidence.start_ms
            or row.end_ms != evidence.end_ms
            or manifest_segment.start_ms != evidence.start_ms
            or manifest_segment.end_ms != evidence.end_ms
            or row.text != manifest_segment.text
            or segment_content_hash(row.text) != manifest_segment.content_hash
        ):
            msg = "evidence does not resolve to a canonical segment in this session"
            raise EvidenceOwnershipError(msg)
        if evidence.text_span is not None:
            span = evidence.text_span
            if span.end_cp > len(row.text):
                msg = "evidence span is outside the canonical segment text"
                raise EvidenceOwnershipError(msg)

    async def persist_atomic_records(
        self,
        *,
        session_id: UUID,
        snapshot_id: str,
        snapshot_content_hash: str,
        records: list[AtomicRecord],
    ) -> None:
        for record in records:
            await self.resolve_record_evidence(
                session_id=session_id,
                snapshot_content_hash=snapshot_content_hash,
                record=record,
            )
            fingerprint = record_content_fingerprint(record)
            await self._db.execute(
                insert(MIAtomicRecordRow)
                .values(
                    session_id=session_id,
                    snapshot_id=snapshot_id,
                    record_id=record.record_id,
                    record_json=record.model_dump_json(),
                    content_fingerprint=fingerprint,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "session_id",
                        "snapshot_id",
                        "record_id",
                        "content_fingerprint",
                    ]
                )
            )

    async def _assert_snapshot_owner(
        self, *, session_id: UUID, snapshot_id: str, snapshot_content_hash: str
    ) -> SourceSnapshotManifest:
        snapshot = await self.get_snapshot(
            session_id=session_id, snapshot_content_hash=snapshot_content_hash
        )
        if snapshot is None or snapshot.snapshot_id != snapshot_id:
            msg = "source snapshot is not owned by this session"
            raise EvidenceOwnershipError(msg)
        self._assert_manifest_identity(snapshot.manifest)
        if snapshot.manifest.session_id != session_id:
            msg = "stored snapshot manifest crossed sessions"
            raise SessionIsolationError(msg)
        return snapshot.manifest

    async def _assert_ledger_and_relations(
        self,
        *,
        session_id: UUID,
        snapshot_id: str,
        snapshot_content_hash: str,
        records: list[AtomicRecord],
        relations: list[RecordRelation],
    ) -> None:
        manifest = await self._assert_snapshot_owner(
            session_id=session_id,
            snapshot_id=snapshot_id,
            snapshot_content_hash=snapshot_content_hash,
        )
        segments = {segment.id: segment for segment in manifest.segments}
        ledger_ids: set[str] = set()
        for record in records:
            fingerprint = record_content_fingerprint(record)
            row = await self._db.scalar(
                select(MIAtomicRecordRow).where(
                    MIAtomicRecordRow.session_id == session_id,
                    MIAtomicRecordRow.snapshot_id == snapshot_id,
                    MIAtomicRecordRow.record_id == record.record_id,
                    MIAtomicRecordRow.content_fingerprint == fingerprint,
                )
            )
            if row is None:
                msg = "ledger record version was not persisted for this snapshot"
                raise EvidenceOwnershipError(msg)
            stored = AtomicRecord.model_validate_json(row.record_json)
            if stored.model_dump(mode="json") != record.model_dump(mode="json"):
                msg = "persisted record body does not match the ledger claim"
                raise EvidenceOwnershipError(msg)
            await self.resolve_record_evidence(
                session_id=session_id,
                snapshot_content_hash=snapshot_content_hash,
                record=record,
            )
            ledger_ids.add(record.record_id)
        for relation in relations:
            if relation.from_record_id not in ledger_ids or relation.to_record_id not in ledger_ids:
                msg = "relation endpoint is not in the persisted record ledger"
                raise EvidenceOwnershipError(msg)
            for evidence in relation.evidence:
                await self._resolve_one_evidence(
                    session_id=session_id, evidence=evidence, segments=segments
                )

    async def load_ledger_records(
        self, *, artifact_id: UUID, session_id: UUID
    ) -> list[AtomicRecord]:
        artifact = await self._db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.id == artifact_id)
        )
        if artifact is None or artifact.session_id != session_id:
            msg = "artifact ledger is not owned by this session"
            raise SessionIsolationError(msg)
        loaded: list[AtomicRecord] = []
        for item in json.loads(artifact.records_ledger_json or "[]"):
            row = await self._db.scalar(
                select(MIAtomicRecordRow).where(
                    MIAtomicRecordRow.session_id == session_id,
                    MIAtomicRecordRow.snapshot_id == artifact.snapshot_id,
                    MIAtomicRecordRow.record_id == item["record_id"],
                    MIAtomicRecordRow.content_fingerprint == item["content_fingerprint"],
                )
            )
            if row is None:
                msg = "ledger fingerprint has no canonical record row"
                raise MeetingIntelligenceRepositoryError(msg)
            loaded.append(AtomicRecord.model_validate_json(row.record_json))
        return loaded

    async def finalize_attempt(
        self,
        *,
        job_id: UUID,
        attempt_id: UUID,
        session_id: UUID,
        generation: int,
        processing_status: ProcessingJobStatus,
        layered: LayeredMeetingResult | None,
        limitations: list[CoverageLimitation],
        snapshot_id: str,
        snapshot_content_hash: str,
        run_fingerprint: str,
        publish_current: bool,
        mark_stale_previous: bool,
        records: list[AtomicRecord] | None = None,
        relations: list[RecordRelation] | None = None,
    ) -> UUID | None:
        del mark_stale_previous
        await self._lock_fence(session_id)
        job = await self._db.scalar(
            select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id).with_for_update()
        )
        attempt = await self._db.scalar(
            select(MIJobAttempt).where(MIJobAttempt.id == attempt_id).with_for_update()
        )
        if attempt is None:
            msg = "attempt not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, attempt.session_id, context="finalize_attempt")
        if job is None or attempt.job_id != job_id or job.session_id != session_id:
            msg = "finalize job does not own the attempt"
            raise AttemptFenceError(msg)
        if attempt.generation != generation or job.active_generation != generation:
            msg = "attempt generation mismatch on finalize"
            raise StaleGenerationError(msg)
        if (
            attempt.snapshot_id != snapshot_id
            or attempt.source_content_hash != snapshot_content_hash
            or attempt.run_fingerprint != run_fingerprint
            or job.snapshot_id != snapshot_id
            or job.source_content_hash != snapshot_content_hash
            or job.run_fingerprint != run_fingerprint
        ):
            msg = "finalize source does not match the attempt binding"
            raise AttemptFenceError(msg)
        manifest = await self._assert_snapshot_owner(
            session_id=session_id,
            snapshot_id=snapshot_id,
            snapshot_content_hash=snapshot_content_hash,
        )
        ledger_records = list(records or [])
        ledger_relations = list(relations or [])
        ledger = [
            {
                "record_id": record.record_id,
                "content_fingerprint": record_content_fingerprint(record),
            }
            for record in ledger_records
        ]
        ledger.sort(key=lambda item: item["record_id"])
        relation_payload = [item.model_dump(mode="json") for item in ledger_relations]
        stored_status = _status_value(processing_status)
        content_fingerprint = _sha256(
            {
                "status": stored_status,
                "layered": layered.model_dump(mode="json") if layered else None,
                "limitations": [item.model_dump(mode="json") for item in limitations],
                "ledger": ledger,
                "relations": relation_payload,
            }
        )
        existing = await self._db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.attempt_id == attempt.id)
        )
        if attempt.status != MIAttemptStatus.RUNNING.value:
            if existing is not None and existing.content_fingerprint == content_fingerprint:
                return existing.id if existing.is_current else None
            msg = "attempt is not running"
            raise AttemptFenceError(msg)
        if existing is not None:
            if existing.content_fingerprint != content_fingerprint:
                msg = "attempt already has a different artifact"
                raise ArtifactConflictError(msg)
            return existing.id if existing.is_current else None
        stored_checkpoint = await self._load_bound_checkpoint(attempt)
        await self._assert_publication(
            session_id=session_id,
            manifest=manifest,
            processing_status=processing_status,
            layered=layered,
            records=ledger_records,
            relations=ledger_relations,
            checkpoint=stored_checkpoint,
        )
        await self._assert_ledger_and_relations(
            session_id=session_id,
            snapshot_id=snapshot_id,
            snapshot_content_hash=snapshot_content_hash,
            records=ledger_records,
            relations=ledger_relations,
        )
        fence = await self._lock_fence(session_id)
        epoch_ok = (
            fence.source_epoch == attempt.source_epoch
            and fence.current_snapshot_content_hash == attempt.source_content_hash
        )
        can_publish = (
            publish_current and epoch_ok and processing_status == ProcessingJobStatus.SUCCEEDED
        )
        artifact_id = uuid4()
        artifact = MIArtifactVersion(
            id=artifact_id,
            job_id=job.id,
            attempt_id=attempt.id,
            session_id=session_id,
            generation=generation,
            snapshot_id=snapshot_id,
            snapshot_content_hash=snapshot_content_hash,
            run_fingerprint=run_fingerprint,
            processing_status=stored_status,
            is_current=False,
            is_stale=not can_publish,
            content_fingerprint=content_fingerprint,
            layered_result_json=layered.model_dump_json() if layered else None,
            limitations_json=_canonical([item.model_dump(mode="json") for item in limitations]),
            records_ledger_json=_canonical(ledger),
            relations_json=_canonical(relation_payload),
        )
        self._db.add(artifact)
        await self._db.flush()
        for relation in ledger_relations:
            self._db.add(
                MIRecordRelationRow(
                    artifact_id=artifact.id,
                    session_id=session_id,
                    snapshot_id=snapshot_id,
                    relation_fingerprint=relation_content_fingerprint(relation),
                    relation_json=relation.model_dump_json(),
                )
            )
        attempt_status = {
            ProcessingJobStatus.SUCCEEDED: MIAttemptStatus.SUCCEEDED,
            ProcessingJobStatus.PARTIAL: MIAttemptStatus.PARTIAL,
            ProcessingJobStatus.FAILED: MIAttemptStatus.FAILED,
            ProcessingJobStatus.CANCELLED: MIAttemptStatus.CANCELLED,
        }.get(processing_status, MIAttemptStatus.PARTIAL)
        attempt.status = attempt_status.value
        attempt.finished_at = _utcnow()
        published: UUID | None = None
        if can_publish:
            await self._db.execute(
                update(MIArtifactVersion)
                .where(
                    MIArtifactVersion.session_id == session_id,
                    MIArtifactVersion.is_current.is_(True),
                )
                .values(is_current=False, is_stale=True)
            )
            await self._db.flush()
            artifact.is_current = True
            artifact.is_stale = False
            await self._db.execute(
                update(MIIntelligenceJob)
                .where(
                    MIIntelligenceJob.session_id == session_id,
                    MIIntelligenceJob.id != job.id,
                    MIIntelligenceJob.current_artifact_id.is_not(None),
                )
                .values(
                    stale_artifact_id=func.coalesce(
                        MIIntelligenceJob.stale_artifact_id,
                        MIIntelligenceJob.current_artifact_id,
                    ),
                    current_artifact_id=None,
                )
            )
            if job.current_artifact_id is not None:
                job.stale_artifact_id = job.current_artifact_id
            job.current_artifact_id = artifact.id
            job.status = MIJobStatus.SUCCEEDED.value
            published = artifact.id
        elif not epoch_ok:
            artifact.is_stale = True
            artifact.is_current = False
            job.status = MIJobStatus.PARTIAL.value
        else:
            job.status = {
                ProcessingJobStatus.SUCCEEDED: MIJobStatus.SUCCEEDED,
                ProcessingJobStatus.PARTIAL: MIJobStatus.PARTIAL,
                ProcessingJobStatus.FAILED: MIJobStatus.FAILED,
                ProcessingJobStatus.CANCELLED: MIJobStatus.CANCELLED,
            }.get(processing_status, MIJobStatus.PARTIAL).value
        await self._db.flush()
        return published

    async def load_current_artifact(
        self, *, job_id: UUID, session_id: UUID
    ) -> tuple[LayeredMeetingResult | None, ProcessingJobStatus, list[CoverageLimitation]] | None:
        published = await self.load_current_publication(job_id=job_id, session_id=session_id)
        if published is None:
            return None
        return published.layered, published.processing_status, published.limitations

    async def load_current_publication(
        self, *, job_id: UUID, session_id: UUID
    ) -> PublishedArtifact | None:
        job = await self._db.scalar(select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id))
        if job is None or job.current_artifact_id is None:
            return None
        self._assert_session(session_id, job.session_id, context="load_current_artifact")
        artifact = await self._db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.id == job.current_artifact_id)
        )
        if artifact is None or artifact.session_id != session_id or not artifact.is_current:
            return None
        return self._published_view(artifact)

    async def load_artifact(self, artifact_id: UUID, *, session_id: UUID) -> PublishedArtifact:
        artifact = await self._db.scalar(
            select(MIArtifactVersion).where(MIArtifactVersion.id == artifact_id)
        )
        if artifact is None:
            msg = "artifact not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, artifact.session_id, context="load_artifact")
        return self._published_view(artifact)

    async def _load_bound_checkpoint(self, attempt: MIJobAttempt) -> EngineCheckpoint | None:
        row = await self._db.scalar(
            select(MIAttemptCheckpoint).where(
                MIAttemptCheckpoint.attempt_id == attempt.id,
                MIAttemptCheckpoint.session_id == attempt.session_id,
                MIAttemptCheckpoint.job_id == attempt.job_id,
                MIAttemptCheckpoint.generation == attempt.generation,
            )
        )
        if row is None:
            return None
        job = await self._db.scalar(
            select(MIIntelligenceJob).where(MIIntelligenceJob.id == attempt.job_id)
        )
        if job is None:
            return None
        checkpoint = deserialize_checkpoint(row.body_json)
        await self._assert_checkpoint_bindings(checkpoint, job=job, attempt=attempt)
        return checkpoint

    async def _assert_publication(
        self,
        *,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        processing_status: ProcessingJobStatus,
        layered: LayeredMeetingResult | None,
        records: list[AtomicRecord],
        relations: list[RecordRelation],
        checkpoint: EngineCheckpoint | None,
    ) -> None:
        if processing_status == ProcessingJobStatus.SUCCEEDED:
            await self._assert_success_publication(
                session_id=session_id,
                manifest=manifest,
                layered=layered,
                records=records,
                relations=relations,
                checkpoint=checkpoint,
            )
            return
        await self._assert_partial_publication(
            session_id=session_id,
            manifest=manifest,
            processing_status=processing_status,
            layered=layered,
            records=records,
            relations=relations,
            checkpoint=checkpoint,
        )

    async def _assert_success_publication(
        self,
        *,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        layered: LayeredMeetingResult | None,
        records: list[AtomicRecord],
        relations: list[RecordRelation],
        checkpoint: EngineCheckpoint | None,
    ) -> None:
        if not checkpoint_accepts_success(checkpoint) or checkpoint is None:
            msg = "succeeded publication requires a host-accepted complete checkpoint"
            raise PublicationIntegrityError(msg)
        accepted = checkpoint.layered_partial
        if (
            layered is None
            or accepted is None
            or layered.snapshot_id != manifest.snapshot_id
            or layered.model_dump(mode="json") != accepted.model_dump(mode="json")
            or not layered_claims_match(layered, accepted)
        ):
            msg = "layered result does not match the accepted checkpoint"
            raise PublicationIntegrityError(msg)
        if not records_match(records, checkpoint.atomic_records) or not records_match(
            records, layered.atomic_records
        ):
            msg = "ledger records do not match the accepted checkpoint and layered result"
            raise PublicationIntegrityError(msg)
        if not relations_match(relations, checkpoint.relations) or not relations_match(
            relations, layered.relations
        ):
            msg = "relations do not match the accepted checkpoint and layered result"
            raise PublicationIntegrityError(msg)
        if not is_success_coverage(layered):
            msg = "succeeded coverage is not an admitted complete check"
            raise PublicationIntegrityError(msg)
        self._raise_if_invalid(validate_result_against_manifest(manifest, layered))
        self._raise_if_invalid(validate_record_ledger(manifest, records, relations))
        await self._resolve_layered_claims(session_id, manifest, layered)

    async def _assert_partial_publication(
        self,
        *,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        processing_status: ProcessingJobStatus,
        layered: LayeredMeetingResult | None,
        records: list[AtomicRecord],
        relations: list[RecordRelation],
        checkpoint: EngineCheckpoint | None,
    ) -> None:
        if (
            checkpoint is not None
            and checkpoint.atomic_records
            and records
            and not records_match(records, checkpoint.atomic_records)
        ):
            msg = "partial ledger does not match the accepted checkpoint records"
            raise PublicationIntegrityError(msg)
        if (
            checkpoint is not None
            and checkpoint.relations
            and relations
            and not relations_match(relations, checkpoint.relations)
        ):
            msg = "partial relations do not match the accepted checkpoint"
            raise PublicationIntegrityError(msg)
        if layered is None:
            return
        if layered.snapshot_id != manifest.snapshot_id:
            msg = "layered snapshot is not the authoritative manifest"
            raise PublicationIntegrityError(msg)
        if partial_coverage_claims_success(layered, processing_status):
            msg = "partial artifact claims completed processing or semantic checks"
            raise PublicationIntegrityError(msg)
        if not records_match(records, layered.atomic_records):
            msg = "layered atomic records do not match the artifact ledger"
            raise PublicationIntegrityError(msg)
        if not relations_match(relations, layered.relations):
            msg = "layered relations do not match the artifact ledger"
            raise PublicationIntegrityError(msg)
        self._raise_if_invalid(validate_result_against_manifest(manifest, layered))
        self._raise_if_invalid(
            validate_record_ledger(manifest, layered.atomic_records, layered.relations)
        )
        await self._resolve_layered_claims(session_id, manifest, layered)

    @staticmethod
    def _raise_if_invalid(report: object) -> None:
        ok = getattr(report, "ok", False)
        issues = getattr(report, "issues", [])
        if ok:
            return
        message = issues[0].message if issues else "layered result failed manifest validation"
        raise PublicationIntegrityError(message)

    async def _resolve_layered_claims(
        self,
        session_id: UUID,
        manifest: SourceSnapshotManifest,
        layered: LayeredMeetingResult,
    ) -> None:
        segments = {segment.id: segment for segment in manifest.segments}
        for evidence in _layered_evidence(layered):
            await self._resolve_one_evidence(
                session_id=session_id, evidence=evidence, segments=segments
            )

    async def list_unresolved_work(
        self,
        *,
        session_id: UUID | None = None,
        limit: int = _UNRESOLVED_PAGE_DEFAULT,
        cursor: str | None = None,
    ) -> UnresolvedWorkPage:
        """Bounded unresolved jobs for a #73 consumer. Cancelled work is excluded.

        Order is ``(created_at, id)``. Fair scheduling is not decided here.
        """
        if limit < 1:
            bounded = _UNRESOLVED_PAGE_DEFAULT
        elif limit > _UNRESOLVED_PAGE_MAX:
            bounded = _UNRESOLVED_PAGE_MAX
        else:
            bounded = limit
        statuses = [
            MIJobStatus.PENDING.value,
            MIJobStatus.RUNNING.value,
            MIJobStatus.PARTIAL.value,
            MIJobStatus.FAILED.value,
        ]
        live = aliased(MIJobAttempt)
        stmt = (
            select(MIIntelligenceJob, live)
            .outerjoin(
                live,
                and_(
                    live.job_id == MIIntelligenceJob.id,
                    live.status == MIAttemptStatus.RUNNING.value,
                    live.generation == MIIntelligenceJob.active_generation,
                ),
            )
            .where(MIIntelligenceJob.status.in_(statuses))
            .order_by(MIIntelligenceJob.created_at, MIIntelligenceJob.id)
            .limit(bounded + 1)
        )
        if session_id is not None:
            stmt = stmt.where(MIIntelligenceJob.session_id == session_id)
        if cursor is not None:
            stamp, job_id = _parse_unresolved_cursor(cursor)
            stmt = stmt.where(
                tuple_(MIIntelligenceJob.created_at, MIIntelligenceJob.id) > tuple_(stamp, job_id)
            )
        rows = (await self._db.execute(stmt)).all()
        extra = len(rows) > bounded
        visible = rows[:bounded]
        items = tuple(
            UnresolvedWork(
                job=self._job_view(row),
                live_attempt_id=None if attempt is None else attempt.id,
                live_generation=None if attempt is None else attempt.generation,
            )
            for row, attempt in visible
        )
        next_cursor = None
        if extra and visible:
            last = visible[-1][0]
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        return UnresolvedWorkPage(items=items, next_cursor=next_cursor)

    async def select_visible_result(
        self, *, job_id: UUID, session_id: UUID
    ) -> PublishedArtifact | None:
        """Current publication when it matches source authority, otherwise the stale artifact."""
        job = await self._db.scalar(select(MIIntelligenceJob).where(MIIntelligenceJob.id == job_id))
        if job is None:
            msg = "intelligence job not found"
            raise MeetingIntelligenceRepositoryError(msg)
        self._assert_session(session_id, job.session_id, context="select_visible_result")
        fence_hash = await self.current_source_hash(session_id)
        if job.current_artifact_id is not None:
            current = await self.load_artifact(job.current_artifact_id, session_id=session_id)
            if (
                current.is_current
                and not current.is_stale
                and current.snapshot_content_hash == fence_hash
            ):
                return current
        if job.stale_artifact_id is not None:
            return await self.load_artifact(job.stale_artifact_id, session_id=session_id)
        return None

    async def delete_session_intelligence(self, session_id: UUID) -> None:
        artifact_ids = select(MIArtifactVersion.id).where(
            MIArtifactVersion.session_id == session_id
        )
        await self._db.execute(
            MIRecordRelationRow.__table__.delete().where(
                MIRecordRelationRow.session_id == session_id
            )
        )
        await self._db.execute(
            MIValidatedExtraction.__table__.delete().where(
                MIValidatedExtraction.session_id == session_id
            )
        )
        await self._db.execute(
            MIAtomicRecordRow.__table__.delete().where(MIAtomicRecordRow.session_id == session_id)
        )
        await self._db.execute(
            MIAttemptCheckpoint.__table__.delete().where(
                MIAttemptCheckpoint.session_id == session_id
            )
        )
        await self._db.execute(
            MIArtifactVersion.__table__.delete().where(MIArtifactVersion.id.in_(artifact_ids))
        )
        await self._db.execute(
            MIJobAttempt.__table__.delete().where(MIJobAttempt.session_id == session_id)
        )
        await self._db.execute(
            MIJobRequestAlias.__table__.delete().where(MIJobRequestAlias.session_id == session_id)
        )
        await self._db.execute(
            MIIntelligenceJob.__table__.delete().where(MIIntelligenceJob.session_id == session_id)
        )
        await self._db.execute(
            MISourceSnapshot.__table__.delete().where(MISourceSnapshot.session_id == session_id)
        )
        await self._db.execute(
            MISessionSourceFence.__table__.delete().where(
                MISessionSourceFence.session_id == session_id
            )
        )

    @staticmethod
    def _job_view(row: MIIntelligenceJob) -> PersistedJob:
        return PersistedJob(
            id=row.id,
            session_id=row.session_id,
            client_request_id=row.client_request_id,
            run_fingerprint=row.run_fingerprint,
            source_content_hash=row.source_content_hash,
            snapshot_id=row.snapshot_id,
            status=MIJobStatus(row.status),
            active_generation=row.active_generation,
            current_artifact_id=row.current_artifact_id,
            stale_artifact_id=row.stale_artifact_id,
            source_epoch=row.source_epoch,
            prompt_cache_key=row.prompt_cache_key,
            schema_identity=row.schema_identity,
            algorithm_identity=row.algorithm_identity,
            limits_fingerprint=row.limits_fingerprint,
            provider_adapter_id=row.provider_adapter_id,
            requested_model=row.requested_model,
            requested_reasoning_effort=row.requested_reasoning_effort,
        )

    @staticmethod
    def _attempt_view(row: MIJobAttempt) -> PersistedAttempt:
        return PersistedAttempt(
            id=row.id,
            job_id=row.job_id,
            session_id=row.session_id,
            generation=row.generation,
            status=MIAttemptStatus(row.status),
            source_epoch=row.source_epoch,
            snapshot_id=row.snapshot_id,
            source_content_hash=row.source_content_hash,
            run_fingerprint=row.run_fingerprint,
        )

    @staticmethod
    def _published_view(artifact: MIArtifactVersion) -> PublishedArtifact:
        limitations_raw = json.loads(artifact.limitations_json or "[]")
        return PublishedArtifact(
            id=artifact.id,
            attempt_id=artifact.attempt_id,
            job_id=artifact.job_id,
            session_id=artifact.session_id,
            is_current=artifact.is_current,
            is_stale=artifact.is_stale,
            snapshot_id=artifact.snapshot_id,
            snapshot_content_hash=artifact.snapshot_content_hash,
            processing_status=ProcessingJobStatus(artifact.processing_status),
            layered=(
                LayeredMeetingResult.model_validate_json(artifact.layered_result_json)
                if artifact.layered_result_json
                else None
            ),
            limitations=[CoverageLimitation.model_validate(item) for item in limitations_raw],
            records_ledger=list(json.loads(artifact.records_ledger_json or "[]")),
            content_fingerprint=artifact.content_fingerprint,
        )
