from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from recantor.meeting_essence.scheduling import jobs
from recantor.meeting_essence.scheduling.models import MISummaryDispatch
from recantor.meeting_essence.scheduling.triggers import (
    note_session_finalized,
    note_transcript_publication,
)


@dataclass(frozen=True)
class DispatchStatusView:
    dispatch_id: UUID
    session_id: UUID
    status: str
    job_id: UUID | None
    attempt_count: int
    last_error_category: str | None
    last_error_code: str | None


class MeetingEssenceSchedulerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def submit(
        self, *, session_id: UUID, client_request_id: UUID
    ) -> jobs.SummarySubmitResult:
        result = await jobs.submit_summary_work(
            self._db, session_id=session_id, client_request_id=client_request_id
        )
        await self._db.commit()
        return result

    async def status(self, *, dispatch_id: UUID, session_id: UUID) -> DispatchStatusView | None:
        row = await self._db.get(MISummaryDispatch, dispatch_id)
        if row is None or row.session_id != session_id:
            return None
        return DispatchStatusView(
            dispatch_id=row.id,
            session_id=row.session_id,
            status=row.status,
            job_id=row.job_id,
            attempt_count=row.attempt_count,
            last_error_category=row.last_error_category,
            last_error_code=row.last_error_code,
        )

    async def cancel(self, *, dispatch_id: UUID, session_id: UUID) -> None:
        stop = await jobs.cancel_summary_dispatch(
            self._db, dispatch_id=dispatch_id, session_id=session_id
        )
        await self._db.commit()
        if stop is not None:
            await stop

    async def retry(self, *, dispatch_id: UUID, session_id: UUID) -> None:
        await jobs.retry_summary_dispatch(self._db, dispatch_id=dispatch_id, session_id=session_id)
        await self._db.commit()

    async def reconcile(self) -> jobs.SummaryReconcileResult:
        result = await jobs.reconcile_summary_work(self._db)
        await self._db.commit()
        return result

    async def execute_next(
        self,
        *,
        entrypoint: str = "internal-service",
        provider: object | None = None,
    ) -> jobs.SummaryExecutionResult:
        result = await jobs.execute_next_summary_job(
            self._db,
            entrypoint=entrypoint,
            provider=provider,  # type: ignore[arg-type]
        )
        await self._db.commit()
        return result

    async def note_transcript_publication(
        self, *, session_id: UUID, publication_sequence: int
    ) -> None:
        await note_transcript_publication(
            self._db, session_id=session_id, publication_sequence=publication_sequence
        )
        await self._db.commit()

    async def note_session_finalized(
        self,
        *,
        session_id: UUID,
        audio_final_sequence: int,
        transcript_sequence: int,
        transcript_hash: str,
    ) -> None:
        await note_session_finalized(
            self._db,
            session_id=session_id,
            audio_final_sequence=audio_final_sequence,
            transcript_sequence=transcript_sequence,
            transcript_hash=transcript_hash,
        )
        await self._db.commit()
