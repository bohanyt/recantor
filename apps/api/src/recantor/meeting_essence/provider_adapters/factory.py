from __future__ import annotations

import json
import os
from pathlib import Path

from recantor.meeting_essence.contracts import MeetingIntelligenceExampleBundle
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.provider_adapters.codex_stage import CodexStructuredStageProvider
from recantor.meeting_essence.provider_adapters.operation_versions import (
    OPERATION_VERSION_V1,
    SUPPORTED_OPERATION_VERSIONS,
)
from recantor.meeting_essence.provider_adapters.structured_provider import (
    StructuredMeetingEssenceProvider,
)
from recantor.meeting_essence.scheduling.provider_factory import (
    SummaryProviderFactory,
    set_summary_provider_factory,
)
from recantor.settings import get_settings


def requested_operation_version() -> str:
    version = os.environ.get("RECANTOR_MEETING_ESSENCE_OPERATION_VERSION", OPERATION_VERSION_V1)
    version = version.strip() or OPERATION_VERSION_V1
    if version not in SUPPORTED_OPERATION_VERSIONS:
        msg = f"unknown meeting essence operation version {version}"
        raise ValueError(msg)
    return version


def load_deterministic_fixture_provider(path: str | Path) -> StructuredMeetingEssenceProvider:
    bundle = MeetingIntelligenceExampleBundle.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
    inner = DeterministicMeetingEssenceProvider(gold=bundle)
    return StructuredMeetingEssenceProvider(
        inner=inner,
        adapter_id="deterministic-v1",
        operation_version=requested_operation_version(),
    )


class _ConfiguredFactory(SummaryProviderFactory):
    def __init__(self, *, adapter_id: str, inner: StructuredMeetingEssenceProvider) -> None:
        self.adapter_id = adapter_id
        self._inner = inner

    def build(self) -> StructuredMeetingEssenceProvider:
        return self._inner


def build_summary_provider_from_settings() -> SummaryProviderFactory | None:
    kind = os.environ.get("RECANTOR_MEETING_ESSENCE_PROVIDER", "").strip().casefold()
    if not kind or kind == "unconfigured":
        return None
    if kind == "deterministic":
        fixture = os.environ.get("RECANTOR_MEETING_ESSENCE_FIXTURE", "").strip()
        if not fixture:
            return None
        provider = load_deterministic_fixture_provider(fixture)
        return _ConfiguredFactory(adapter_id=provider.adapter_id, inner=provider)
    if kind in {"codex-structured-v1", "codex_structured_v1"}:
        if os.environ.get("RECANTOR_CODEX_BRIDGE_ENABLED", "").strip() not in {"1", "true", "yes"}:
            return None
        settings = get_settings()
        version = requested_operation_version()
        inner = StructuredMeetingEssenceProvider(
            inner=CodexStructuredStageProvider(
                codex_binary=settings.meeting_essence_codex_binary,
                model=settings.meeting_essence_requested_model or None,
                reasoning_effort=settings.meeting_essence_requested_reasoning_effort or None,
            ),
            adapter_id="codex-structured-v1",
            operation_version=version,
        )
        return _ConfiguredFactory(adapter_id="codex-structured-v1", inner=inner)
    return None


def configure_summary_provider_factory() -> None:
    factory = build_summary_provider_from_settings()
    set_summary_provider_factory(factory)


def export_fixture_identity(path: str | Path) -> dict[str, str]:
    bundle = MeetingIntelligenceExampleBundle.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
    return {
        "snapshot_id": bundle.manifest.snapshot_id,
        "snapshot_content_hash": bundle.manifest.snapshot_content_hash,
        "contract_version": bundle.result.contract_version,
    }


def write_public_fixture_summary(path: str | Path) -> str:
    data = export_fixture_identity(path)
    return json.dumps(data, sort_keys=True)
