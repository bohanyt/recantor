from __future__ import annotations

from dataclasses import dataclass

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_adapters.operation_versions import (
    OPERATION_VERSION_V1,
    assert_supported_operation_version,
)
from recantor.meeting_essence.stage_contracts import StageRequest, StageResponse


@dataclass
class StructuredMeetingEssenceProvider:
    """Versioned stage adapter over a concrete inner provider (#74)."""

    inner: MeetingEssenceProvider
    adapter_id: str
    operation_version: str = OPERATION_VERSION_V1

    @property
    def spawns_process_tree(self) -> bool:
        return bool(getattr(self._inner, "spawns_process_tree", False))

    @property
    def _inner(self) -> MeetingEssenceProvider:
        return self.inner

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
        assert_supported_operation_version(
            prompt_bundle=prompt_bundle,
            operation=operation,
            requested_version=self.operation_version,
        )
        return await self.inner.run_stage(
            operation=operation,
            request=request,
            prompt_bundle=prompt_bundle,
            job=job,
        )
