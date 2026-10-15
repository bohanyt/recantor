"""The broker setup must not clear a live or unknown shared permit."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_summary_broker_lifecycle import prepare_shared_broker_permit

from recantor.db import get_sessionmaker
from recantor.meeting_essence.contracts import StageOperation
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    identity_for_pid,
    process_is_alive,
    process_liveness,
)


async def _lease_view() -> tuple[object, ...]:
    async with get_sessionmaker()() as db:
        row = await db.get(MIHostInvocationLease, 1)
    path = host_authority.sidecar_path()
    sidecar = path.read_text(encoding="utf-8") if path.is_file() else ""
    if row is None:
        return (None, sidecar)
    return (
        row.holder_token,
        row.holder_pid,
        row.holder_state,
        row.active_invocations,
        row.owned_runtime_json,
        row.quarantine_reason,
        sidecar,
    )


def _stop_pid(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return
    import signal

    with contextlib.suppress(OSError):
        import os

        os.kill(pid, signal.SIGKILL)


def _wait_dead(identity: object) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process_liveness(identity) == Liveness.DEAD:  # type: ignore[arg-type]
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {getattr(identity, 'pid', None)} did not exit")


@pytest.mark.asyncio
async def test_finished_permit_is_free_for_the_shared_broker_setup(mi_db) -> None:
    """A completed in-process call releases through the permit, then setup proceeds."""
    del mi_db

    class Inner:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> str:
            del kwargs
            return "ran"

    permit = HostInvocationPermit()
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(), permit=permit, entrypoint="round10-finished"
    )
    ran = await wrapper.run_stage(
        operation=StageOperation.EXTRACT,
        request=None,  # type: ignore[arg-type]
        job=None,  # type: ignore[arg-type]
        prompt_bundle=None,  # type: ignore[arg-type]
    )
    assert ran == "ran"
    assert await wrapper.aclose() is True
    assert await permit.read_active_witness() == 0
    await prepare_shared_broker_permit()
    assert await permit.read_active_witness() == 0


@pytest.mark.asyncio
async def test_broker_setup_leaves_unknown_owned_and_an_active_stage(mi_db) -> None:
    del mi_db
    active = 0
    stop = asyncio.Event()
    began = asyncio.Event()

    async def _stage() -> None:
        nonlocal active
        active += 1
        began.set()
        try:
            while not stop.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.01)
        finally:
            active -= 1

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round10-unknown-owned")
    await permit.mark_admitted(handle, spawns_process_tree=False)
    task = asyncio.create_task(_stage())
    await began.wait()
    unknown = {
        "pid": 424242,
        "created_ms": 1,
        "start_ticks": 1,
        "namespace": "linux:other-boot:pid:[9]",
        "owned_pgid": None,
        "owned_sid": None,
        "tree_root": None,
    }
    async with get_sessionmaker()() as db, db.begin():
        row = await db.get(MIHostInvocationLease, 1)
        assert row is not None and row.holder_token == handle.token
        row.owned_runtime_json = json.dumps([unknown])
    before = await _lease_view()
    try:
        with pytest.raises(AssertionError, match="busy"):
            await prepare_shared_broker_permit()
        assert await _lease_view() == before
        assert active == 1
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 1)
        async with get_sessionmaker()() as db, db.begin():
            row = await db.get(MIHostInvocationLease, 1)
            if row is not None and row.holder_token == handle.token:
                row.owned_runtime_json = "[]"
        await permit.release(handle)


@pytest.mark.asyncio
async def test_broker_setup_leaves_an_active_closer(mi_db) -> None:
    del mi_db
    active = 0
    stop = asyncio.Event()
    began = asyncio.Event()

    class Inner:
        async def aclose(self) -> None:
            nonlocal active
            active += 1
            began.set()
            try:
                while not stop.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            finally:
                active -= 1

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round10-active-closer")
    await permit.mark_admitted(handle, spawns_process_tree=False)
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(),
        permit=permit,
        entrypoint="round10-active-closer",
        shared_handle=handle,
    )
    closing = asyncio.create_task(wrapper.aclose())
    await began.wait()
    assert await closing is False
    assert active == 1
    before = await _lease_view()
    try:
        with pytest.raises(AssertionError, match="busy"):
            await prepare_shared_broker_permit()
        assert await _lease_view() == before
        assert active == 1
        assert await permit.read_active_witness() == 1
    finally:
        stop.set()
        if wrapper._closer_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(wrapper._closer_task, 1)
        if wrapper._reaper is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(wrapper._reaper, 1)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(closing, 1)
        if await permit.read_active_witness() == 1:
            await permit.release(handle)


@pytest.mark.asyncio
async def test_broker_setup_leaves_a_live_nested_child(mi_db, tmp_path: Path) -> None:
    del mi_db
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    permit = HostInvocationPermit()
    handle = None
    child_pid = 0
    child_identity = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.is_file() and time.monotonic() < deadline:
            if parent.poll() is not None:
                raise AssertionError("nested parent exited before it recorded the child")
            time.sleep(0.05)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        child_identity = identity_for_pid(child_pid)
        parent_identity = identity_for_pid(parent.pid)
        handle = await permit.acquire(entrypoint="round10-nested-child")
        assert await permit.register_owned_runtime(handle, [parent_identity, child_identity])
        await permit.mark_admitted(handle, spawns_process_tree=True)
        _stop_pid(parent.pid)
        parent.wait(timeout=5)
        assert process_is_alive(child_identity)
        before = await _lease_view()
        with pytest.raises(AssertionError, match="busy"):
            await prepare_shared_broker_permit()
        assert await _lease_view() == before
        assert process_is_alive(child_identity)
        assert await permit.read_active_witness() == 1
    finally:
        if parent.poll() is None:
            _stop_pid(parent.pid)
            parent.wait(timeout=5)
        if child_pid:
            _stop_pid(child_pid)
        if child_identity is not None:
            _wait_dead(child_identity)
        if handle is not None:
            await permit.release(handle)
