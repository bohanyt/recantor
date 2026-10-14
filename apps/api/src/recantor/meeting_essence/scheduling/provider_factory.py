"""Recantor-owned provider binding for summary jobs.

#74 supplies a concrete adapter by calling ``set_summary_provider_factory``.
Until that adapter is configured, scheduling reports unavailable and does not
invent transcript, ledger, or summary data.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_errors import ProviderErrorCategory, StageInvocationError
from recantor.meeting_essence.stage_contracts import StageRequest, StageResponse


@dataclass
class UnavailableMeetingEssenceProvider:
    """Explicit disabled adapter. It never fabricates a meeting result."""

    adapter_id: str = "unconfigured"
    spawns_process_tree: bool = False
    evaluation_records: list[object] | None = None
    relation_annotations: list[object] = field(default_factory=list)

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: PromptBundle,
        job: MeetingIntelligenceJobMetadata,
    ) -> StageResponse:
        del operation, request, prompt_bundle, job
        raise StageInvocationError(
            ProviderErrorCategory.CONFIG,
            "summary provider is not configured",
        )


class SummaryProviderFactory:
    adapter_id: str = "unconfigured"

    def build(self) -> MeetingEssenceProvider:
        return UnavailableMeetingEssenceProvider()


_factory: SummaryProviderFactory = SummaryProviderFactory()


_env_spec: str | None = None
_env_factory: SummaryProviderFactory | None = None


def get_summary_provider_factory() -> SummaryProviderFactory:
    """Return the in-process factory, or the factory named by the worker environment.

    ``RECANTOR_SUMMARY_PROVIDER_FACTORY`` is ``module:callable`` for a separate
    Celery process. Unset means the explicit unconfigured adapter. This is not
    a second launcher.
    """
    global _env_factory, _env_spec
    spec = os.environ.get("RECANTOR_SUMMARY_PROVIDER_FACTORY")
    if not spec:
        return _factory
    if spec != _env_spec or _env_factory is None:
        module_name, separator, attr = spec.partition(":")
        if not separator:
            return _factory
        module = importlib.import_module(module_name)
        loaded = getattr(module, attr)
        _env_factory = loaded() if callable(loaded) else loaded
        _env_spec = spec
    return _env_factory


def set_summary_provider_factory(factory: SummaryProviderFactory | None) -> None:
    global _factory
    _factory = factory or SummaryProviderFactory()
