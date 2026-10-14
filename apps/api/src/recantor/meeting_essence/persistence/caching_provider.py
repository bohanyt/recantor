from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.persistence.fingerprints import extraction_reuse_key
from recantor.meeting_essence.persistence.repository import MeetingIntelligenceRepository
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.stage_contracts import (
    ExtractStageRequest,
    ExtractStageResponse,
    StageRequest,
    StageResponse,
)


@dataclass
class CachedMeetingEssenceProvider:
    """Read validated extraction reuse. Do not store raw provider output."""

    inner: MeetingEssenceProvider
    repository: MeetingIntelligenceRepository
    session_id: str
    run_fingerprint: str
    source_content_hash: str
    provider_adapter_id: str
    prompt_cache_key: str
    limits_fingerprint: str
    cache_hits: int = field(default=0, init=False)
    provider_calls: int = field(default=0, init=False)
    extract_provider_calls: int = field(default=0, init=False)

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: PromptBundle,
        job: MeetingIntelligenceJobMetadata,
    ) -> StageResponse:
        if operation == StageOperation.EXTRACT:
            if not isinstance(request, ExtractStageRequest):
                msg = "extract cache requires ExtractStageRequest"
                raise TypeError(msg)
            key = extraction_reuse_key(
                request,
                provider_adapter_id=self.provider_adapter_id,
                prompt_cache_key=self.prompt_cache_key,
                limits_fingerprint=self.limits_fingerprint,
            )
            cached = await self.repository.get_validated_extraction(
                session_id=self._session_uuid(),
                extraction_key=key,
            )
            if cached is not None:
                self.cache_hits += 1
                return ExtractStageResponse.model_validate_json(cached)
            self.extract_provider_calls += 1
        self.provider_calls += 1
        return await self.inner.run_stage(
            operation=operation,
            request=request,
            job=job,
            prompt_bundle=prompt_bundle,
        )

    def _session_uuid(self) -> UUID:
        return UUID(self.session_id)
