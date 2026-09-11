from __future__ import annotations

import asyncio
import logging

from recantor.settings import get_settings
from recantor.stt_jobs import reconcile_stt_jobs
from recantor.stt_tasks import enqueue_stt_utterance

logger = logging.getLogger(__name__)


async def run_reconciler() -> None:
    settings = get_settings()
    interval = max(0.1, float(settings.stt_reconcile_interval_seconds))
    while True:
        try:
            result = await reconcile_stt_jobs(enqueue=enqueue_stt_utterance)
            if result.created or result.converged or result.dispatched or result.enqueue_failures:
                logger.info(
                    "STT reconciliation created=%s converged=%s dispatched=%s enqueue_failures=%s",
                    result.created,
                    result.converged,
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
