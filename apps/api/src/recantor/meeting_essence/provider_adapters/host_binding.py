"""Bind a direct helper call to the same host permit the worker uses.

A second provider instance does not get its own cap. When this task already
holds the permit, the call reuses that handle and does not acquire another.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from recantor.meeting_essence.provider_adapters.spawn_gate import (
    PermitPreResumeGate,
    held_permit_handle,
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
from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity


class _DirectPermitTarget:
    def __init__(self, permit: HostInvocationPermit, handle: PermitHandle) -> None:
        self._permit = permit
        self._handle = handle
        self._cancel = False

    def cancel_requested(self) -> bool:
        return self._cancel

    async def prepare_owned_spawn(
        self,
        identities: list[ProcessIdentity],
        admission_closed: object | None = None,
    ) -> bool:
        closed = getattr(admission_closed, "is_set", None)
        if self._cancel or (closed is not None and closed()):
            return False
        if not identities:
            return not self._cancel
        registered = await self._permit.register_owned_runtime(self._handle, identities)
        if self._cancel or (closed is not None and closed()):
            return False
        return registered


@asynccontextmanager
async def shared_host_invocation(entrypoint: str) -> AsyncIterator[PermitHandle]:
    existing = held_permit_handle()
    if isinstance(existing, PermitHandle):
        yield existing
        return
    permit = HostInvocationPermit()
    if not await permit.authority_reachable():
        raise HostInvocationBusy("host invocation authority is unreachable")
    handle = await permit.try_acquire(entrypoint=entrypoint)
    if handle is None:
        raise HostInvocationBusy("host model invocation permit is busy or unreachable")
    target = _DirectPermitTarget(permit, handle)
    gate = PermitPreResumeGate(target, asyncio.get_running_loop())
    gate_token = set_pre_resume_gate(gate)
    held_token = set_held_permit(handle)
    try:
        yield handle
    finally:
        gate.close()
        reset_pre_resume_gate(gate_token)
        reset_held_permit(held_token)
        spawned = gate.admitted_count > 0
        await permit.mark_admitted(handle, spawns_process_tree=spawned)
        await permit.release(handle)
