from __future__ import annotations

import asyncio
import threading
import time

import pytest
from synthetic_owned_child import SyntheticOwnedChildProcessAdapter

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import load_prompt_bundle
from recantor.meeting_essence.provider_adapters.codex_stage import CodexStructuredStageProvider
from recantor.meeting_essence.scheduling.host_authority import reset_for_tests
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_intelligence import (
    CodexSubscriptionMeetingIntelligenceProvider,
    MeetingIntelligenceError,
    MeetingIntelligenceErrorCategory,
    ProcessResult,
    ProcessSpec,
)
from recantor.settings import get_settings


class _HoldAdapter:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.started = threading.Event()

    def run(self, spec: ProcessSpec) -> ProcessResult:
        self.started.set()
        while not self.release.is_set():
            if spec.cancel_event is not None and spec.cancel_event.is_set():
                break
            time.sleep(0.01)
        return ProcessResult(returncode=0, stdout=b"Logged in using ChatGPT\n", stderr=b"")


class _SlowThreadProvider:
    spawns_process_tree = False

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()

    async def run_stage(self, *, operation, request, prompt_bundle, job):
        del operation, request, prompt_bundle, job
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._work)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
                task = asyncio.current_task()
                if task is not None:
                    while task.cancelling():
                        task.uncancel()
        self.finished.wait(timeout=2)
        if cancelled:
            raise asyncio.CancelledError
        return None

    def _work(self) -> None:
        self.started.set()
        time.sleep(0.6)
        self.finished.set()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_helper_instances_share_host_permit(
    mi_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path / "host-authority"))
    get_settings.cache_clear()
    reset_for_tests()
    release = threading.Event()
    first = CodexSubscriptionMeetingIntelligenceProvider(
        process_adapter=_HoldAdapter(release),
        bind_host_permit=True,
    )
    second = CodexSubscriptionMeetingIntelligenceProvider(
        process_adapter=_HoldAdapter(threading.Event()),
        bind_host_permit=True,
    )
    task = asyncio.create_task(first.check_chatgpt_auth())
    assert await asyncio.to_thread(first.process_adapter.started.wait, 2)
    permit = HostInvocationPermit()
    assert await permit.try_acquire(entrypoint="configured-worker") is None
    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await second.check_chatgpt_auth()
    assert exc_info.value.category == MeetingIntelligenceErrorCategory.BUSY
    release.set()
    await task
    handle = await permit.try_acquire(entrypoint="configured-worker")
    assert handle is not None
    assert await permit.release(handle) is True


@pytest.mark.asyncio
async def test_cancelled_thread_keeps_permit_until_work_settles(
    mi_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    del mi_db
    monkeypatch.setenv("RECANTOR_HOST_AUTHORITY_DIR", str(tmp_path / "host-authority"))
    get_settings.cache_clear()
    reset_for_tests()
    permit = HostInvocationPermit()
    handle = await permit.try_acquire(entrypoint="worker-cancel-witness")
    assert handle is not None
    inner = _SlowThreadProvider()
    bound = PermitBoundMeetingEssenceProvider(
        inner=inner,
        permit=permit,
        entrypoint="worker-cancel-witness",
        shared_handle=handle,
    )
    from uuid import uuid4

    bundle = load_prompt_bundle()
    job = MeetingIntelligenceJobMetadata(
        job_id=uuid4(),
        session_id=uuid4(),
        snapshot_id="snap_test_permit_cancel",
        prompt_bundle_id=bundle.package_id,
        schema_identity=bundle.schema_identity,
        algorithm_identity=bundle.algorithm_identity,
        input_fingerprint="ab" * 32,
    )

    async def _call():
        return await bound.run_stage(
            operation=StageOperation.EXTRACT,
            request=object(),  # type: ignore[arg-type]
            job=job,
            prompt_bundle=bundle,
        )

    task = asyncio.create_task(_call())
    assert await asyncio.to_thread(inner.started.wait, 2)
    task.cancel()
    assert await permit.try_acquire(entrypoint="second-worker") is None
    with pytest.raises(asyncio.CancelledError):
        await task
    await bound.aclose()
    assert await asyncio.to_thread(inner.finished.wait, 2)
    await asyncio.sleep(0.2)
    recovered = await permit.try_acquire(entrypoint="after-settle")
    assert recovered is not None
    assert await permit.release(recovered) is True
    await HostInvocationPermit().recover_proven_dead()


@pytest.mark.asyncio
async def test_codex_structured_stage_keeps_host_cap_while_sync_work_alive(
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
    handle = await permit.try_acquire(entrypoint="codex-stage-host-cap")
    assert handle is not None
    slow = SyntheticOwnedChildProcessAdapter(child_sleep_seconds=0.5)
    inner = CodexStructuredStageProvider(
        process_adapter=slow,
        codex_binary="codex-test",
        spawns_process_tree=True,
    )
    bound = PermitBoundMeetingEssenceProvider(
        inner=inner,
        permit=permit,
        entrypoint="codex-stage-host-cap",
        shared_handle=handle,
    )
    from uuid import uuid4

    bundle = load_prompt_bundle()
    job = MeetingIntelligenceJobMetadata(
        job_id=uuid4(),
        session_id=uuid4(),
        snapshot_id="snap_codex_stage_cap",
        prompt_bundle_id=bundle.package_id,
        schema_identity=bundle.schema_identity,
        algorithm_identity=bundle.algorithm_identity,
        input_fingerprint="cd" * 32,
    )
    task = asyncio.create_task(
        bound.run_stage(
            operation=StageOperation.EXTRACT,
            request=type(
                "Req",
                (),
                {"model_dump": lambda self, **_: {}},
            )(),
            job=job,
            prompt_bundle=bundle,
        )
    )
    assert await asyncio.to_thread(slow.exec_started.wait, 5)
    assert await permit.try_acquire(entrypoint="second-codex-stage") is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await permit.try_acquire(entrypoint="second-codex-stage-2") is None
    await bound.aclose()
    recovered = await permit.try_acquire(entrypoint="after-codex-stage-settle")
    assert recovered is not None
    assert await permit.release(recovered) is True
    await HostInvocationPermit().recover_proven_dead()
