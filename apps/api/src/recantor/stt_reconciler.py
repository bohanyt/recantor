from __future__ import annotations

import asyncio
import logging

from recantor.settings import get_settings
from recantor.stt_jobs import reconcile_stt_jobs
from recantor.stt_tasks import ensure_stt_wake_capacity

logger = logging.getLogger(__name__)


async def run_reconciler() -> None:
    settings = get_settings()
    interval = float(settings.stt_reconcile_interval_seconds)
    while True:
        try:
            result = await reconcile_stt_jobs(ensure_wake_capacity=ensure_stt_wake_capacity)
            if (
                result.created
                or result.converged
                or result.reserved
                or result.dispatched
                or result.enqueue_failures
            ):
                logger.info(
                    "STT reconciliation created=%s converged=%s reserved=%s "
                    "wake_target=%s dispatched=%s enqueue_failures=%s",
                    result.created,
                    result.converged,
                    result.reserved,
                    result.wake_target,
                    result.dispatched,
                    result.enqueue_failures,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("STT reconciliation pass failed")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(run_reconciler())


if __name__ == "__main__":
    main()
