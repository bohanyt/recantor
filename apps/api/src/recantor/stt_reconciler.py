from __future__ import annotations

import asyncio
import logging

from recantor.settings import get_settings
from recantor.stt_jobs import STTWorkloadClass, reconcile_stt_jobs
from recantor.stt_tasks import ensure_live_stt_wake_capacity, ensure_upload_stt_wake_capacity

logger = logging.getLogger(__name__)


async def _reconcile_class(workload_class: STTWorkloadClass):
    ensure = (
        ensure_upload_stt_wake_capacity
        if workload_class == STTWorkloadClass.UPLOAD
        else ensure_live_stt_wake_capacity
    )
    result = await reconcile_stt_jobs(
        workload_class=workload_class,
        ensure_wake_capacity=ensure,
    )
    if (
        result.created
        or result.converged
        or result.reserved
        or result.dispatched
        or result.enqueue_failures
    ):
        logger.info(
            "STT %s reconciliation created=%s converged=%s reserved=%s "
            "wake_target=%s dispatched=%s enqueue_failures=%s",
            workload_class.value,
            result.created,
            result.converged,
            result.reserved,
            result.wake_target,
            result.dispatched,
            result.enqueue_failures,
        )


async def run_reconciler() -> None:
    settings = get_settings()
    interval = float(settings.stt_reconcile_interval_seconds)
    while True:
        try:
            # Live is deliberately reconciled first; queues/workers are independent afterward.
            await _reconcile_class(STTWorkloadClass.LIVE)
            await _reconcile_class(STTWorkloadClass.UPLOAD)
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
