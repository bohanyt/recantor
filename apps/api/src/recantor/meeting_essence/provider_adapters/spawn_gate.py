from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import Protocol, TypeVar

from recantor.meeting_essence.scheduling.process_liveness import identity_for_pid

_ADMIT_TIMEOUT_SECONDS = 5.0


class PreResumeGate(Protocol):
    def admit_pid(self, pid: int) -> bool: ...

    def close(self) -> None: ...


_current_gate: ContextVar[PreResumeGate | None] = ContextVar(
    "meeting_essence_pre_resume_gate", default=None
)
_held_permit: ContextVar[object | None] = ContextVar("meeting_essence_held_permit", default=None)


def current_pre_resume_gate() -> PreResumeGate | None:
    return _current_gate.get()


def set_pre_resume_gate(gate: PreResumeGate | None) -> object:
    return _current_gate.set(gate)


def reset_pre_resume_gate(token: object) -> None:
    _current_gate.reset(token)


def held_permit_handle() -> object | None:
    return _held_permit.get()


def set_held_permit(handle: object | None) -> object:
    return _held_permit.set(handle)


def reset_held_permit(token: object) -> None:
    _held_permit.reset(token)


class PermitPreResumeGate:
    """Register a pid before it may run. A timeout cancels the registration task.

    A late completion after denial sees ``admission_closed`` and must not treat
    that pid as admitted.
    """

    def __init__(
        self,
        wrapper: object,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout_seconds: float = _ADMIT_TIMEOUT_SECONDS,
    ) -> None:
        self._wrapper = wrapper
        self._loop = loop
        self._timeout = timeout_seconds
        self._closed = threading.Event()
        self._future: asyncio.Future[bool] | None = None
        self.admitted_count = 0

    @property
    def admission_closed(self) -> threading.Event:
        return self._closed

    def close(self) -> None:
        self._closed.set()
        future = self._future
        if future is not None and not future.done():
            future.cancel()

    def admit_pid(self, pid: int) -> bool:
        if self._closed.is_set():
            return False
        identity = identity_for_pid(pid)
        future = asyncio.run_coroutine_threadsafe(
            self._wrapper.prepare_owned_spawn(  # type: ignore[attr-defined]
                [identity],
                admission_closed=self._closed,
            ),
            self._loop,
        )
        self._future = future
        try:
            admitted = bool(future.result(timeout=self._timeout))
        except Exception:
            self._closed.set()
            future.cancel()
            return False
        if not admitted or self._closed.is_set():
            self._closed.set()
            if not future.done():
                future.cancel()
            return False
        self.admitted_count += 1
        return True


def pre_resume_hook_from_gate() -> PreResumeGate | None:
    return current_pre_resume_gate()


_T = TypeVar("_T")


class _UnsetType:
    pass


_UNSET = _UnsetType()


def run_sync_with_spawn_context(  # noqa: UP047
    func: Callable[..., _T],
    /,
    *args: object,
    gate_snapshot: PreResumeGate | None | _UnsetType = _UNSET,
    held_snapshot: object | None | _UnsetType = _UNSET,
    **kwargs: object,
) -> _T:
    """Re-bind gate/permit ContextVars for executor or ``asyncio.to_thread`` workers."""
    if gate_snapshot is _UNSET:
        gate_snapshot = current_pre_resume_gate()
    if held_snapshot is _UNSET:
        held_snapshot = held_permit_handle()
    gate_token = set_pre_resume_gate(gate_snapshot)
    held_token = set_held_permit(held_snapshot)
    try:
        return func(*args, **kwargs)
    finally:
        reset_held_permit(held_token)
        reset_pre_resume_gate(gate_token)
