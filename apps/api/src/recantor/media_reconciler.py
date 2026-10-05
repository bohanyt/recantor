from __future__ import annotations

import asyncio
import logging

from recantor.media_processing import reconcile_upload_processing
from recantor.media_tasks import ensure_media_wake_capacity
from recantor.settings import get_settings

logger = logging.getLogger(__name__)


async def run_reconciler() -> None:
    settings = get_settings()
    interval = float(settings.media_reconcile_interval_seconds)
    while True:
        try:
            result = await reconcile_upload_processing()
            dispatched = 0
            enqueue_failed = False
            try:
                dispatched = ensure_media_wake_capacity(result.wake_target)
            except Exception:
                enqueue_failed = True
                logger.exception("media upload wake publication failed")
            if (
                result.created
                or result.stt_succeeded
                or result.stt_failed
                or result.wake_target
                or dispatched
                or enqueue_failed
            ):
                logger.info(
                    "media reconciliation created=%s stt_succeeded=%s stt_failed=%s "
                    "wake_target=%s dispatched=%s enqueue_failed=%s",
                    result.created,
                    result.stt_succeeded,
                    result.stt_failed,
                    result.wake_target,
                    dispatched,
                    enqueue_failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media upload reconciliation pass failed")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(run_reconciler())


if __name__ == "__main__":
    main()
