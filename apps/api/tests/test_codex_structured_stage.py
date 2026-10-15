from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from synthetic_owned_child import SyntheticOwnedChildProcessAdapter

from recantor.codex_process import ProcessResult, ProcessSpec
from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.provider_adapters.codex_stage import CodexStructuredStageProvider
from recantor.meeting_essence.provider_adapters.spawn_gate import (
    PermitPreResumeGate,
    current_pre_resume_gate,
    held_permit_handle,
    reset_held_permit,
    set_held_permit,
    set_pre_resume_gate,
)
from recantor.meeting_essence.provider_errors import StageInvocationError
from recantor.meeting_essence.scheduling.host_authority import reset_for_tests
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit, PermitHandle
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import current_process_identity
from recantor.meeting_essence.stage_contracts import ExtractStageResponse
from recantor.meeting_intelligence import MeetingIntelligenceError, MeetingIntelligenceErrorCategory
from recantor.settings import get_settings


class _GateRecordingProvider(CodexStructuredStageProvider):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("spawns_process_tree", False)
        super().__init__(*args, **kwargs)
        self.gate_inside_worker: list[bool | None] = []
        self.held_inside_worker: list[bool | None] = []
        self.thread_started = threading.Event()
        self.thread_finished = threading.Event()
        self.hook_calls: list[int] = []

    def _prompt_for(self, operation, prompt_bundle):  # type: ignore[no-untyped-def]
        del operation, prompt_bundle
        return "stage-instruction"

    def _exec_json_sync(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        self.gate_inside_worker.append(current_pre_resume_gate() is not None)
        self.held_inside_worker.append(held_permit_handle() is not None)
        self.thread_started.set()
        time.sleep(0.9)
        self.thread_finished.set()
        return ExtractStageResponse()


class _AuthProbeAdapter:
    def __init__(self) -> None:
        self.hook_calls: list[bool] = []

    def run(self, spec: ProcessSpec) -> ProcessResult:
        if len(spec.argv) >= 2 and spec.argv[1] == "login":
            if spec.pre_resume_hook is not None:
                import os

                self.hook_calls.append(spec.pre_resume_hook(os.getpid()))
            return ProcessResult(returncode=0, stdout=b"Logged in using ChatGPT\n", stderr=b"")
        if spec.pre_resume_hook is not None:
            import os

            self.hook_calls.append(spec.pre_resume_hook(os.getpid()))
        return ProcessResult(returncode=0, stdout=b"Logged in using ChatGPT\n", stderr=b"")


def _job() -> MeetingIntelligenceJobMetadata:
    bundle = load_prompt_bundle()
    return MeetingIntelligenceJobMetadata(
        job_id=uuid4(),
        session_id=uuid4(),
        snapshot_id="snap_codex_stage_test",
        prompt_bundle_id=bundle.package_id,
        schema_identity=bundle.schema_identity,
        algorithm_identity=bundle.algorithm_identity,
        input_fingerprint="cd" * 32,
    )


def _stage_request() -> SimpleNamespace:
    return SimpleNamespace(model_dump=lambda **_: {})


async def _release_bound_permit_test_cleanup(bound: PermitBoundMeetingEssenceProvider) -> None:
    with contextlib.suppress(Exception):
        released = await bound.aclose()
        if not released:
            await bound.cancel_running()
            with contextlib.suppress(Exception):
                await bound.aclose()


@pytest.mark.asyncio
async def test_run_stage_executor_future_does_not_use_create_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    task = asyncio.create_task(
        provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    )
    assert await asyncio.to_thread(provider.thread_started.wait, 2)
    assert provider._inflight is not None
    assert not provider._inflight.done()
    await task
    assert provider.thread_finished.is_set()
    assert provider._inflight is None


@pytest.mark.asyncio
async def test_run_stage_cancel_keeps_inflight_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    task = asyncio.create_task(
        provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    )
    assert await asyncio.to_thread(provider.thread_started.wait, 2)
    assert not provider.thread_finished.is_set()
    assert provider._inflight is not None and not provider._inflight.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider._inflight is not None
    assert not provider.thread_finished.is_set()
    await provider.aclose()
    assert provider.thread_finished.is_set()
    assert provider._inflight is None


@pytest.mark.asyncio
async def test_run_stage_worker_failure_clears_inflight_after_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())

    def _fail(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        provider.thread_started.set()
        raise MeetingIntelligenceError(
            MeetingIntelligenceErrorCategory.INVALID_OUTPUT,
            "bad-json",
        )

    monkeypatch.setattr(provider, "_exec_json_sync", _fail)
    with pytest.raises(StageInvocationError):
        await provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    assert provider._inflight is None


@pytest.mark.asyncio
async def test_pre_resume_gate_visible_inside_executor_with_permit_bound_wrapper(
    mi_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path / "host-authority"))
    get_settings.cache_clear()
    reset_for_tests()
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    permit = HostInvocationPermit()
    adapter = SyntheticOwnedChildProcessAdapter(child_sleep_seconds=0.15)
    inner = CodexStructuredStageProvider(
        process_adapter=adapter,
        codex_binary="codex-test",
        spawns_process_tree=True,
    )
    bound = PermitBoundMeetingEssenceProvider(
        inner=inner,
        permit=permit,
        entrypoint="codex-stage-gate-witness",
    )
    try:
        await bound.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
        assert adapter.gate_inside_worker
        assert all(adapter.gate_inside_worker)
        assert adapter.exec_started.is_set()
    finally:
        await _release_bound_permit_test_cleanup(bound)


@pytest.mark.asyncio
async def test_held_permit_snapshot_visible_inside_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held permit snapshots from the async caller must reach the executor thread."""
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    handle = PermitHandle(
        token="held-permit-executor-test",
        identity=current_process_identity(),
        entrypoint="codex-stage-gate-witness",
        acquired_at=datetime.now(tz=UTC),
        generation=1,
    )
    held_token = set_held_permit(handle)
    try:
        await provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
        assert provider.held_inside_worker == [True]
    finally:
        reset_held_permit(held_token)
        await provider.aclose()


@pytest.mark.asyncio
async def test_double_cancel_still_waits_for_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    task = asyncio.create_task(
        provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    )
    assert await asyncio.to_thread(provider.thread_started.wait, 2)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()
    assert provider.thread_finished.is_set()


@pytest.mark.asyncio
async def test_permit_pre_resume_gate_admits_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Target:
        async def prepare_owned_spawn(self, identities, admission_closed=None):  # type: ignore[no-untyped-def]
            del admission_closed
            assert identities
            return True

    target = Target()
    gate = PermitPreResumeGate(target, asyncio.get_running_loop(), timeout_seconds=2.0)
    token = set_pre_resume_gate(gate)
    try:
        assert await asyncio.to_thread(gate.admit_pid, 1234)
    finally:
        from recantor.meeting_essence.provider_adapters.spawn_gate import reset_pre_resume_gate

        reset_pre_resume_gate(token)


@pytest.mark.asyncio
async def test_aclose_cancel_keeps_inflight_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    provider = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    task = asyncio.create_task(
        provider.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    )
    assert await asyncio.to_thread(provider.thread_started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider._inflight is not None
    assert not provider.thread_finished.is_set()
    closer = asyncio.create_task(provider.aclose())
    await asyncio.sleep(0.05)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert provider._inflight is not None
    assert not provider.thread_finished.is_set()
    settle = asyncio.create_task(provider.aclose())
    await settle
    assert provider.thread_finished.is_set()
    assert provider._inflight is None


@pytest.mark.asyncio
async def test_concrete_adapter_auth_and_exec_invoke_pre_resume_hook(
    mi_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path / "host-authority"))
    get_settings.cache_clear()
    reset_for_tests()
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    adapter = SyntheticOwnedChildProcessAdapter(child_sleep_seconds=0.12)
    inner = CodexStructuredStageProvider(
        process_adapter=adapter,
        codex_binary="codex-test",
        spawns_process_tree=True,
    )
    permit = HostInvocationPermit()
    bound = PermitBoundMeetingEssenceProvider(
        inner=inner,
        permit=permit,
        entrypoint="codex-hook-witness",
    )
    try:
        await bound.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
        assert len(adapter.hook_calls) >= 2
        assert all(adapter.hook_calls)
        assert adapter.exec_started.is_set()

        spawn_calls = 0
        deny_adapter = SyntheticOwnedChildProcessAdapter(child_sleep_seconds=0.12)

        async def _limited_spawn(identities, admission_closed=None):  # type: ignore[no-untyped-def]
            nonlocal spawn_calls
            del identities, admission_closed
            spawn_calls += 1
            return spawn_calls == 1

        inner.process_adapter = deny_adapter
        monkeypatch.setattr(bound, "prepare_owned_spawn", _limited_spawn)
        with pytest.raises(StageInvocationError):
            await bound.run_stage(
                operation=StageOperation.EXTRACT,
                request=_stage_request(),  # type: ignore[arg-type]
                prompt_bundle=load_prompt_bundle(),
                job=_job(),
            )
        assert spawn_calls >= 2
        assert not deny_adapter.exec_started.is_set()
        assert deny_adapter.hook_calls[-1] is False
    finally:
        await _release_bound_permit_test_cleanup(bound)


@pytest.mark.asyncio
async def test_bound_cleanup_does_not_release_unrelated_active_permit(
    mi_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path / "host-authority"))
    get_settings.cache_clear()
    reset_for_tests()
    monkeypatch.setattr(
        "recantor.meeting_intelligence._strict_filesystem_containment_supported",
        lambda: True,
    )
    permit = HostInvocationPermit()
    slow = _GateRecordingProvider(process_adapter=_AuthProbeAdapter())
    bound_slow = PermitBoundMeetingEssenceProvider(
        inner=slow,
        permit=permit,
        entrypoint="slow-permit-witness",
    )
    bound_unrelated = PermitBoundMeetingEssenceProvider(
        inner=_GateRecordingProvider(process_adapter=_AuthProbeAdapter()),
        permit=permit,
        entrypoint="unrelated-bound",
    )
    slow_task = asyncio.create_task(
        bound_slow.run_stage(
            operation=StageOperation.EXTRACT,
            request=_stage_request(),  # type: ignore[arg-type]
            prompt_bundle=load_prompt_bundle(),
            job=_job(),
        )
    )
    try:
        assert await asyncio.to_thread(slow.thread_started.wait, 2)
        await _release_bound_permit_test_cleanup(bound_unrelated)
        assert await permit.try_acquire(entrypoint="second-acquire") is None
        assert await permit.read_active_witness() == 1
    finally:
        await slow_task
        await _release_bound_permit_test_cleanup(bound_slow)
    assert await permit.read_active_witness() == 0
    handle = await permit.try_acquire(entrypoint="after-cleanup")
    assert handle is not None
    await permit.release(handle)
