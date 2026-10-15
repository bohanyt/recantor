"""Real row-lock contention during bounded permit cleanup; no live provider."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError

from recantor.db import get_engine, get_sessionmaker
from recantor.meeting_essence.contracts import StageOperation
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import (
    HostInvocationBusy,
    HostInvocationPermit,
    open_permit_handles,
)
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider


async def _hold_row(db) -> None:
    row = await db.scalar(
        select(MIHostInvocationLease).where(MIHostInvocationLease.id == 1).with_for_update()
    )
    assert row is not None


@pytest.mark.asyncio
async def test_held_row_serializes_concurrent_cancel_and_close(mi_db) -> None:
    del mi_db
    started = asyncio.Event()
    contended = asyncio.Event()
    cleanup_queries = 0
    closes = 0

    class Inner:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs
            started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            nonlocal closes
            closes += 1

    def observe(_conn, _cursor, statement, _params, _context, _many) -> None:
        nonlocal cleanup_queries
        if "mi_host_invocation_lease" in statement and "FOR UPDATE" in statement:
            cleanup_queries += 1
            if cleanup_queries >= 2:
                contended.set()

    permit = HostInvocationPermit()
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(), permit=permit, entrypoint="release-race"
    )
    running = asyncio.create_task(
        wrapper.run_stage(
            operation=StageOperation.EXTRACT, request=None, job=None, prompt_bundle=None
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    tasks = []
    try:
        # Open the concurrent connections before the 200 ms row-lock window;
        # Windows connection startup itself can take longer than that window.
        async with contextlib.AsyncExitStack() as stack:
            for _ in range(5):
                await stack.enter_async_context(get_engine().connect())
        async with get_sessionmaker()() as db, db.begin():
            await _hold_row(db)
            event.listen(get_engine().sync_engine, "before_cursor_execute", observe)
            try:
                tasks = [
                    asyncio.create_task(wrapper.cancel_running()),
                    asyncio.create_task(wrapper.aclose()),
                ]
                await asyncio.wait_for(contended.wait(), 1)
                # Both cleanup paths have reached a genuinely locked row.
                await asyncio.sleep(0.03)
                assert all(not task.done() for task in tasks)
                assert host_authority.try_hold() is None
                assert await permit.read_active_witness() == 1
            finally:
                event.remove(get_engine().sync_engine, "before_cursor_execute", observe)
        with pytest.raises(HostInvocationBusy, match="cancelled"):
            await asyncio.wait_for(running, 2)
        assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [None, True]
        assert await wrapper.aclose() is True
        assert closes == 1
        assert await permit.read_active_witness() == 0
        follow = await permit.acquire(entrypoint="after-release-race")
        assert await permit.release(follow) is True
    finally:
        await asyncio.gather(running, *tasks, return_exceptions=True)
        await wrapper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True], ids=["close", "reaper"])
async def test_cleanup_lock_timeout_keeps_handle_for_bounded_retry(mi_db, deferred) -> None:
    del mi_db
    stop = asyncio.Event()
    closes = 0

    class Inner:
        async def aclose(self) -> None:
            nonlocal closes
            closes += 1
            if deferred:
                while not stop.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop.wait()

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="release-timeout")
    await permit.mark_admitted(handle, spawns_process_tree=False)
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Inner(), permit=permit, entrypoint="release-timeout", shared_handle=handle
    )
    try:
        if deferred:
            assert await wrapper.aclose() is False
        async with get_sessionmaker()() as db, db.begin():
            await _hold_row(db)
            if deferred:
                stop.set()
                closing = wrapper._reaper
                assert closing is not None
            else:
                closing = asyncio.create_task(wrapper.aclose())
            with pytest.raises(DBAPIError) as failure:
                await asyncio.wait_for(asyncio.shield(closing), 2)
            assert failure.value.orig.sqlstate == "55P03"
            assert wrapper._handle is handle
            assert (permit, handle) in open_permit_handles()
            assert host_authority.try_hold() is None
            assert await permit.read_active_witness() == 1
        assert await HostInvocationPermit().try_acquire(entrypoint="before-release-retry") is None
        assert await wrapper.aclose() is True
        assert closes == 1
        assert wrapper._handle is None
        assert (permit, handle) not in open_permit_handles()
        assert await permit.read_active_witness() == 0
        follow = await permit.acquire(entrypoint="after-release-retry")
        assert await permit.release(follow) is True
    finally:
        stop.set()
        if wrapper._reaper is not None:
            await asyncio.gather(wrapper._reaper, return_exceptions=True)
        await wrapper.aclose()
