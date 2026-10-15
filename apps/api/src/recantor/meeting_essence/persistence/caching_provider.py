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


class EnteredProviderCalls:
    """Count a stage only after this coroutine has started.

    Permit admission and authority checks run before this wrapper. A cancel
    during that work never reaches ``run_stage``, so it is not a provider call.
    """

    def __init__(self, inner: MeetingEssenceProvider) -> None:
        self._inner = inner
        self.provider_calls = 0
        self.extract_provider_calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: PromptBundle,
        job: MeetingIntelligenceJobMetadata,
    ) -> StageResponse:
        self.provider_calls += 1
        if operation == StageOperation.EXTRACT:
            self.extract_provider_calls += 1
        return await self._inner.run_stage(
            operation=operation,
            request=request,
            job=job,
            prompt_bundle=prompt_bundle,
        )


def entered_call_counts(provider: object) -> tuple[int, int]:
    """Calls that entered the admitted provider, not earlier wrapper work."""
    counter = _find_entry_counter(provider)
    if counter is not None:
        return counter.provider_calls, counter.extract_provider_calls
    return (
        int(getattr(provider, "provider_calls", 0) or 0),
        int(getattr(provider, "extract_provider_calls", 0) or 0),
    )


def _find_entry_counter(provider: object) -> EnteredProviderCalls | None:
    current: object | None = provider
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, EnteredProviderCalls):
            return current
        nxt = getattr(current, "_inner", None)
        if nxt is None:
            nxt = getattr(current, "inner", None)
        current = nxt
    return None


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
        # An entry counter inside the permit increments only after admission.
        # Counting here would include a call cancelled during authority work.
        if _find_entry_counter(self.inner) is None:
            if operation == StageOperation.EXTRACT:
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
