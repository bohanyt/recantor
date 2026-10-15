"""Periodic canonical scan for summary work. Redis wakes are not the source of truth."""

from __future__ import annotations

import asyncio
import logging

from redis.exceptions import RedisError

from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling import jobs
from recantor.meeting_essence.scheduling.tasks import ensure_summary_wake_capacity
from recantor.settings import get_settings

logger = logging.getLogger(__name__)


async def run_reconciler() -> None:
    settings = get_settings()
    interval = float(settings.meeting_essence_reconcile_interval_seconds)
    while True:
        try:
            async with get_sessionmaker()() as db:
                result = await jobs.reconcile_summary_work(db)
                await db.commit()
            if result.wake_target:
                try:
                    ensure_summary_wake_capacity(result.wake_target)
                except (OSError, RedisError):
                    logger.info(
                        "summary reconcile found work but the broker wake was not published"
                    )
            if result.scanned or result.created:
                logger.info(
                    "summary reconciliation scanned=%s created=%s coalesced=%s wake_target=%s",
                    result.scanned,
                    result.created,
                    result.coalesced,
                    result.wake_target,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("summary reconciliation pass failed")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(run_reconciler())


if __name__ == "__main__":
    main()
