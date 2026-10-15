"""Permit wrapper. The row lock is never held across the inner stage call.

A process-spawning adapter sets ``spawns_process_tree`` and must register the
child before that child is allowed to run. Registration failure returns false
and the caller must not resume it. ``aclose`` returns within a bound. A stage,
waiter, or closer is unproven from the moment its task is created until that
task exits. Repeated cancellation of ``aclose`` does not enter the cleanup task,
and cleanup never releases the permit while one of those tasks is still running.
The reaper releases only after every tracked task has actually finished.
"""

from __future__ import annotations

import asyncio
import contextlib

from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_adapters.spawn_gate import (
    PermitPreResumeGate,
    reset_held_permit,
    reset_pre_resume_gate,
    set_held_permit,
    set_pre_resume_gate,
)
from recantor.meeting_essence.scheduling.host_permit import (
    HostInvocationBusy,
    HostInvocationPermit,
    PermitHandle,
)
from recantor.meeting_essence.scheduling.process_liveness import (
    ProcessIdentity,
    terminate_process_tree,
)
from recantor.meeting_essence.stage_contracts import StageRequest, StageResponse

_REAP_SECONDS = 0.4
_CLOSE_BOUND_SECONDS = 0.4


def _absorb_cancellation() -> None:
    """Drop cancellation on the cleanup task so it can still quarantine or release."""
    current = asyncio.current_task()
    if current is None:
        return
    while current.cancelling():
        current.uncancel()


class PermitBoundMeetingEssenceProvider:
    """One shared permit for the whole invocation, not one lock per stage."""

    def __init__(
        self,
        *,
        inner: MeetingEssenceProvider,
        permit: HostInvocationPermit,
        entrypoint: str,
        shared_handle: PermitHandle | None = None,
    ) -> None:
        self._inner = inner
        self._permit = permit
        self._entrypoint = entrypoint
        self._handle = shared_handle
        self._owned = shared_handle is not None
        self._cancel = asyncio.Event()
        self._stage: asyncio.Task[object] | None = None
        self._waiter: asyncio.Task[object] | None = None
        self._task_unproven = False
        self._closer_unproven = False
        self._closer_task: asyncio.Task[object] | None = None
        self._reaper: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[bool] | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def spawns_process_tree(self) -> bool:
        return bool(getattr(self._inner, "spawns_process_tree", False))

    async def register_owned_processes(self, identities: list[ProcessIdentity]) -> bool:
        """Register before the child runs. False means do not resume or exec."""
        if self._handle is None:
            self._handle = await self._permit.try_acquire(entrypoint=self._entrypoint)
            self._owned = self._handle is not None
        if self._handle is None:
            return False
        return await self._permit.register_owned_runtime(self._handle, identities)

    def request_cancel(self) -> None:
        self._cancel.set()

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    async def cancel_running(self) -> None:
        self.request_cancel()
        stage = self._stage
        if stage is not None and not stage.done():
            stage.cancel()
        if self._handle is not None:
            await self._permit.terminate_owned_tree(self._handle)
        await self._reap(stage)

    async def prepare_owned_spawn(
        self,
        identities: list[ProcessIdentity],
        admission_closed: object | None = None,
    ) -> bool:
        """Register before executable work. Denial after a timeout does not admit late."""
        closed = getattr(admission_closed, "is_set", None)

        def _closed() -> bool:
            return bool(closed is not None and closed())

        if self._cancel.is_set() or _closed():
            await self._stop_identities(identities)
            return False
        if not identities:
            return not self._cancel.is_set() and not _closed()
        registered = await self.register_owned_processes(identities)
        if not registered or self._cancel.is_set() or _closed():
            await self._stop_identities(identities)
            return False
        return True

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        job: MeetingIntelligenceJobMetadata,
        prompt_bundle: PromptBundle,
    ) -> StageResponse:
        if not await self._permit.authority_reachable():
            raise HostInvocationBusy("host invocation authority is unreachable")
        if self._handle is None:
            self._handle = await self._permit.try_acquire(entrypoint=self._entrypoint)
            self._owned = self._handle is not None
            if self._handle is None:
                raise HostInvocationBusy("host model invocation permit is busy or unreachable")
        else:
            await self._permit.renew(self._handle)
        prepare = getattr(self._inner, "prepare_owned_processes", None)
        if prepare is not None:
            identities = await prepare()
            if identities and not await self.prepare_owned_spawn(list(identities)):
                raise HostInvocationBusy("owned process registration failed")
        await self._permit.mark_admitted(self._handle, spawns_process_tree=self.spawns_process_tree)
        if self._cancel.is_set():
            raise HostInvocationBusy("summary invocation was cancelled")
        gate = (
            PermitPreResumeGate(self, asyncio.get_running_loop())
            if self.spawns_process_tree
            else None
        )
        gate_token = set_pre_resume_gate(gate)
        held_token = set_held_permit(self._handle)
        stage = asyncio.create_task(
            self._inner.run_stage(
                operation=operation,
                request=request,
                job=job,
                prompt_bundle=prompt_bundle,
            )
        )
        self._stage = stage
        self._task_unproven = True
        stage.add_done_callback(self._on_stage_exit)
        waiter = asyncio.create_task(self._cancel.wait())
        self._waiter = waiter
        try:
            done, _pending = await asyncio.wait(
                {stage, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if stage in done and not stage.cancelled():
                self._task_unproven = False
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
                return stage.result()
            if gate is not None:
                gate.close()
            stage.cancel()
            if self._handle is not None:
                await self._permit.terminate_owned_tree(self._handle)
            await self._reap(stage)
            raise HostInvocationBusy("summary invocation was cancelled")
        finally:
            if gate is not None:
                gate.close()
            reset_pre_resume_gate(gate_token)
            reset_held_permit(held_token)
            if not waiter.done():
                waiter.cancel()
            await self._reap(waiter)
            if stage.done() and not stage.cancelled():
                self._task_unproven = False
                self._stage = None
                self._waiter = None
            elif not stage.done():
                stage.cancel()

    async def aclose(self) -> bool:
        """Bounded return. A second cancel cannot reach into permit cleanup."""
        cleanup = self._ensure_cleanup()
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            _absorb_cancellation()
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                raise
            raise

    def _ensure_cleanup(self) -> asyncio.Task[bool]:
        cleanup = self._cleanup
        if cleanup is None or cleanup.done():
            cleanup = asyncio.create_task(self._bounded_close())
            self._cleanup = cleanup
        return cleanup

    def _on_stage_exit(self, task: asyncio.Task[object]) -> None:
        if task is self._stage and task.done():
            self._task_unproven = False

    def _on_closer_exit(self, task: asyncio.Task[object]) -> None:
        if task is self._closer_task and task.done():
            self._closer_unproven = False

    def _still_open(self) -> bool:
        return any(
            task is not None and not task.done()
            for task in (self._stage, self._waiter, self._closer_task)
        )

    async def _bounded_close(self) -> bool:
        closer_error: Exception | None = None
        try:
            closer = getattr(self._inner, "aclose", None)
            if closer is not None:
                closer_task = self._closer_task
                if closer_task is None:
                    closer_task = asyncio.create_task(closer())
                    self._closer_task = closer_task
                    self._closer_unproven = True
                    closer_task.add_done_callback(self._on_closer_exit)
                try:
                    await asyncio.wait({closer_task}, timeout=_CLOSE_BOUND_SECONDS)
                except asyncio.CancelledError:
                    _absorb_cancellation()
                if closer_task.done():
                    self._closer_unproven = False
                    closer_error = _closer_exception(closer_task)
                else:
                    closer_task.cancel()
                    try:
                        await asyncio.wait({closer_task}, timeout=0.05)
                    except asyncio.CancelledError:
                        _absorb_cancellation()
                    self._closer_unproven = not closer_task.done()
                    if closer_task.done():
                        closer_error = _closer_exception(closer_task)
        finally:
            _absorb_cancellation()
            released = await self._finish_permit()
        if closer_error is not None:
            raise closer_error
        return released

    async def _finish_permit(self) -> bool:
        await self._reap(self._waiter)
        await self._reap(self._stage)
        handle = self._handle
        if handle is None:
            return not self._still_open()
        if self._still_open() or self._task_unproven or self._closer_unproven:
            closer_open = self._closer_unproven or (
                self._closer_task is not None and not self._closer_task.done()
            )
            reason = "closer-not-proven-dead" if closer_open else "inner-task-not-proven-dead"
            self._arm_release_when_done(handle)
            await self._permit.quarantine_unproven(handle, reason)
            return False
        released = await self._permit.release(handle)
        if released and self._handle is handle:
            self._handle = None
        return released

    def _arm_release_when_done(self, handle: PermitHandle) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        stage = self._stage
        closer = self._closer_task
        waiter = self._waiter

        async def _release_after_task() -> None:
            for task in (stage, closer, waiter):
                if task is None:
                    continue
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if any(task is not None and not task.done() for task in (stage, closer, waiter)):
                return
            if self._handle is handle and await self._permit.release(handle):
                self._handle = None

        self._reaper = asyncio.create_task(_release_after_task())

    async def _reap(self, task: asyncio.Task[object] | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_REAP_SECONDS)
            except TimeoutError:
                if task is self._stage:
                    self._task_unproven = True
                return
            except asyncio.CancelledError:
                if not task.done() and task is self._stage:
                    self._task_unproven = True
                return
        if task is self._stage:
            self._task_unproven = not task.done()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result() if task.done() and not task.cancelled() else None

    async def _stop_identities(self, identities: list[ProcessIdentity]) -> None:
        for identity in identities:
            await asyncio.to_thread(terminate_process_tree, identity)


def _closer_exception(task: asyncio.Task[object]) -> Exception | None:
    if task.cancelled():
        return None
    error = task.exception()
    if isinstance(error, Exception):
        return error
    return None
