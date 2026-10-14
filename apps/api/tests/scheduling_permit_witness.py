"""Subprocess helper for the cross-process host permit test (#73)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--token-out", required=True)
    parser.add_argument("--hold-seconds", type=float, default=8.0)
    return parser.parse_args()


async def _hold(database_url: str, hold_seconds: float, token_out: str) -> None:
    import os

    os.environ["DATABASE_URL"] = database_url
    from recantor.db import get_engine, get_sessionmaker
    from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
    from recantor.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="witness-subprocess")
    with open(token_out, "w", encoding="utf-8") as fh:
        fh.write(handle.token)
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline:
        await permit.renew(handle, lease_seconds=5)
        await asyncio.sleep(0.05)


def main() -> None:
    args = _parse()
    asyncio.run(_hold(args.database_url, args.hold_seconds, args.token_out))


if __name__ == "__main__":
    main()
