"""Real child/PG authority proof, independent of Redis and live providers."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from test_summary_broker_lifecycle import prepare_shared_broker_permit

from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit

_CHILD = """
import asyncio, json, sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# Import exactly the worker's entry module before inspecting its authority.
from recantor.meeting_essence.scheduling import tasks
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
async def main():
    permit = HostInvocationPermit()
    reason = await permit.blocking_reason()
    handle = await permit.try_acquire(entrypoint='child-authority-regression')
    released = await permit.release(handle) if handle is not None else None
    print(json.dumps(dict(reason=reason, acquired=handle is not None, released=released)))
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_child_uses_the_prepared_shared_authority(mi_db) -> None:
    del mi_db
    await prepare_shared_broker_permit()
    env = os.environ.copy()

    def child() -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return json.loads(result.stdout)

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="parent-authority-regression")
    try:
        assert child() == {"reason": "lease-active", "acquired": False, "released": None}
    finally:
        assert await permit.release(handle) is True
    assert child() == {"reason": None, "acquired": True, "released": True}
    assert await permit.blocks_new_invocation() is False
