from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import meeting_essence_offline_worker as worker
import pytest

from recantor.meeting_essence.scheduling.jobs import SummaryExecutionResult, SummaryExecutionStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "open_count", "successes"),
    [
        ([SummaryExecutionStatus.PARTIAL, SummaryExecutionStatus.SUCCEEDED], 0, 1),
        ([SummaryExecutionStatus.PARTIAL], 1, 0),
        ([SummaryExecutionStatus.RETRY_SCHEDULED], 1, 0),
    ],
)
async def test_drain_outstanding_follows_queue_not_historical_partial(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[SummaryExecutionStatus],
    open_count: int,
    successes: int,
) -> None:
    queue = [SummaryExecutionResult(status=status) for status in statuses]

    async def _commit():
        pass

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace(commit=_commit)

    monkeypatch.setattr(worker, "get_sessionmaker", lambda: _session)

    async def _fake_execute(db, **kwargs):  # type: ignore[no-untyped-def]
        del db, kwargs
        if queue:
            return queue.pop(0)
        return SummaryExecutionResult(status=SummaryExecutionStatus.NOT_CLAIMED)

    async def _no_open(db) -> int:  # type: ignore[no-untyped-def]
        del db
        return open_count

    monkeypatch.setattr("meeting_essence_offline_worker.execute_next_summary_job", _fake_execute)
    monkeypatch.setattr(
        "meeting_essence_offline_worker.outstanding_summary_dispatch_count",
        _no_open,
    )
    success_count, last_status, exhausted, outstanding_work = await worker._drain(
        max_rounds=10,
        idle_not_claimed_rounds=1,
    )
    assert success_count == successes
    assert last_status == SummaryExecutionStatus.NOT_CLAIMED.value
    assert exhausted is False
    assert outstanding_work is (open_count > 0)


@pytest.mark.parametrize("outstanding", [False, True])
def test_worker_exit_reflects_outstanding_work(monkeypatch, outstanding: bool) -> None:
    monkeypatch.setenv("RECANTOR_MEETING_ESSENCE_PROVIDER", "deterministic")

    async def _drained():
        return 1, SummaryExecutionStatus.NOT_CLAIMED.value, False, outstanding

    monkeypatch.setattr(worker, "_drain", _drained)
    assert worker.main() == (1 if outstanding else 0)
