from __future__ import annotations

from typing import Protocol

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.stage_contracts import StageRequest, StageResponse


class MeetingEssenceProvider(Protocol):
    """Provider-injected bounded stage boundary (#71+). Codex is one implementation."""

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: PromptBundle,
        job: MeetingIntelligenceJobMetadata,
    ) -> StageResponse: ...
