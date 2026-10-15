from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from meeting_intelligence_fixtures import build_s01_s06_bundle

from recantor.meeting_essence.contracts import (
    ActionFollowUpItem,
    RecordContentStatus,
    StageOperation,
)
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.provider_adapters.legacy_v1 import (
    LEGACY_WIRE_LIMIT,
    layered_to_legacy_v1,
)
from recantor.meeting_essence.provider_adapters.operation_versions import (
    OPERATION_VERSION_V1,
    assert_supported_operation_version,
)
from recantor.meeting_essence.provider_adapters.spawn_gate import PermitPreResumeGate
from recantor.meeting_essence.provider_errors import StageInvocationError
from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity


def test_legacy_v1_keeps_action_status_and_does_not_drop_overflow() -> None:
    bundle = build_s01_s06_bundle()
    action = bundle.result.actions[0]
    extras = []
    evidence = action.evidence
    for index in range(LEGACY_WIRE_LIMIT + 3):
        extras.append(
            ActionFollowUpItem(
                task=f"tugas {index}",
                content_status=RecordContentStatus.ACCEPTANCE,
                condition_text="kalau tes lulus",
                assignee="Dini" if index == 0 else None,
                deadline_raw="besok pukul 12.00" if index == 0 else None,
                evidence=evidence,
            )
        )
    layered = bundle.result.model_copy(update={"actions": extras})
    projection = layered_to_legacy_v1(
        layered,
        requested_model="requested-only",
        requested_reasoning_effort="high",
    )
    assert projection.result.requested_model == "requested-only"
    assert len(projection.result.action_items) == LEGACY_WIRE_LIMIT + 3
    assert projection.detail_limited is True
    assert ("action_items", 3) in projection.overflow_by_array
    assert projection.result.key_points
    sample = projection.result.action_items[0]
    assert "status:acceptance" in sample
    assert "kondisi: kalau tes lulus" in sample
    assert "PIC: Dini" in sample
    assert "tenggat: besok pukul 12.00" in sample


def test_unknown_operation_version_is_explicit() -> None:
    bundle = load_prompt_bundle()
    with pytest.raises(StageInvocationError):
        assert_supported_operation_version(
            prompt_bundle=bundle,
            operation=StageOperation.EXTRACT,
            requested_version="meeting-essence.operation/v9",
        )
    selected = assert_supported_operation_version(
        prompt_bundle=bundle,
        operation=StageOperation.CHECK,
        requested_version=OPERATION_VERSION_V1,
    )
    assert selected == OPERATION_VERSION_V1
    broken = bundle.__class__(
        package_id=bundle.package_id,
        schema_identity="unknown/schema",
        algorithm_identity=bundle.algorithm_identity,
        prompts=bundle.prompts,
    )
    with pytest.raises(StageInvocationError):
        assert_supported_operation_version(
            prompt_bundle=broken,
            operation=StageOperation.EXTRACT,
            requested_version=OPERATION_VERSION_V1,
        )


def test_registration_timeout_does_not_admit_late() -> None:
    class SlowTarget:
        def __init__(self) -> None:
            self.registered: list[ProcessIdentity] = []

        async def prepare_owned_spawn(self, identities, admission_closed=None):
            await asyncio.sleep(0.4)
            closed = getattr(admission_closed, "is_set", None)
            if closed is not None and closed():
                return False
            self.registered.extend(identities)
            return True

    async def _run() -> SlowTarget:
        target = SlowTarget()
        gate = PermitPreResumeGate(target, asyncio.get_running_loop(), timeout_seconds=0.05)
        admitted = await asyncio.to_thread(gate.admit_pid, 4242)
        assert admitted is False
        await asyncio.sleep(0.5)
        assert target.registered == []
        return target

    asyncio.run(_run())


def test_worker_factory_uses_the_api_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "meeting_intelligence"
        / "s01_s06_example.json"
    )
    monkeypatch.setenv("RECANTOR_MEETING_ESSENCE_PROVIDER", "deterministic")
    monkeypatch.setenv("RECANTOR_MEETING_ESSENCE_FIXTURE", str(fixture))
    monkeypatch.delenv("RECANTOR_SUMMARY_PROVIDER_FACTORY", raising=False)
    from recantor.meeting_essence.scheduling import provider_factory as registry

    registry._auto_signature = None
    factory = registry.get_summary_provider_factory()
    assert factory.adapter_id == "deterministic-v1"
    monkeypatch.delenv("RECANTOR_MEETING_ESSENCE_PROVIDER")
    monkeypatch.delenv("RECANTOR_MEETING_ESSENCE_FIXTURE")
    registry._auto_signature = ("deterministic", str(fixture))
    restored = registry.get_summary_provider_factory()
    assert restored.adapter_id == "unconfigured"
