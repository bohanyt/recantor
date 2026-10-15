"""Drain configured summary jobs without Redis/Celery (development fixture path).

Uses the same provider factory contract as the API process and Celery worker.
Run from ``apps/api`` with the same ``DATABASE_URL`` and
``RECANTOR_MEETING_ESSENCE_*`` environment as the API.
"""

from __future__ import annotations

import asyncio
import os
import sys

from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling.jobs import (
    SummaryExecutionStatus,
    execute_next_summary_job,
    outstanding_summary_dispatch_count,
)

_TERMINAL_FAILURE = {
    SummaryExecutionStatus.FAILED,
    SummaryExecutionStatus.CANCELLED,
    SummaryExecutionStatus.UNAVAILABLE,
    SummaryExecutionStatus.QUARANTINED,
}

_INCOMPLETE = {
    SummaryExecutionStatus.BUSY,
    SummaryExecutionStatus.PARTIAL,
    SummaryExecutionStatus.PARKED,
    SummaryExecutionStatus.RETRY_SCHEDULED,
}


async def _drain(
    *, max_rounds: int = 200, idle_not_claimed_rounds: int = 30
) -> tuple[int, str, bool, bool]:
    """Return (success_count, last_status, exhausted_max_rounds, outstanding_work)."""
    success_count = 0
    idle_rounds = 0
    last_status = SummaryExecutionStatus.NOT_CLAIMED.value
    exhausted = False
    outstanding_work = False
    for round_index in range(max_rounds):
        async with get_sessionmaker()() as db:
            outcome = await execute_next_summary_job(db, entrypoint="offline-fixture-worker")
            await db.commit()
        last_status = outcome.status.value
        if outcome.status == SummaryExecutionStatus.NOT_CLAIMED:
            idle_rounds += 1
            if idle_rounds >= idle_not_claimed_rounds:
                break
            await asyncio.sleep(0.05)
            continue
        idle_rounds = 0
        if outcome.status == SummaryExecutionStatus.SUCCEEDED:
            success_count += 1
        print(
            f"round status={outcome.status.value} "
            f"dispatch={outcome.dispatch_id} session={outcome.session_id}",
            flush=True,
        )
        if outcome.status in _TERMINAL_FAILURE:
            break
        if round_index == max_rounds - 1:
            exhausted = True
        await asyncio.sleep(0.05)
    async with get_sessionmaker()() as db:
        outstanding_work = await outstanding_summary_dispatch_count(db) > 0
    return success_count, last_status, exhausted, outstanding_work


def main() -> int:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    kind = os.environ.get("RECANTOR_MEETING_ESSENCE_PROVIDER", "").strip().casefold()
    if kind != "deterministic":
        print(
            "RECANTOR_MEETING_ESSENCE_PROVIDER=deterministic is required for the offline worker",
            file=sys.stderr,
        )
        return 2
    success_count, last_status, exhausted, outstanding_work = asyncio.run(_drain())
    print(
        f"succeeded_jobs={success_count} last_status={last_status} "
        f"exhausted={exhausted} outstanding_work={outstanding_work}"
    )
    if last_status in {item.value for item in _TERMINAL_FAILURE}:
        print(f"worker_stop status={last_status}", file=sys.stderr)
        return 1
    if exhausted or outstanding_work:
        print(
            "worker_stop reason=incomplete_or_outstanding_dispatches",
            file=sys.stderr,
        )
        return 1
    if last_status in {item.value for item in _INCOMPLETE}:
        print(f"worker_stop status={last_status}", file=sys.stderr)
        return 1
    if success_count == 0 and last_status != SummaryExecutionStatus.NOT_CLAIMED.value:
        print(f"worker_stop status={last_status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
