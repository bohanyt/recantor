from __future__ import annotations

import asyncio
import logging
import os

from celery import Celery
from redis import Redis
from redis.exceptions import RedisError

from recantor.db import get_sessionmaker
from recantor.meeting_essence.provider_adapters.factory import configure_summary_provider_factory
from recantor.meeting_essence.scheduling import jobs
from recantor.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
configure_summary_provider_factory()

celery_app = Celery("recantor-summary", broker=settings.redis_url)
celery_app.conf.update(
    task_default_queue=settings.meeting_essence_queue_name,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    beat_schedule={
        "summary-reconcile": {
            "task": "recantor.reconcile_summary_work",
            "schedule": float(settings.meeting_essence_reconcile_interval_seconds),
        }
    },
)

_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_pid: int | None = None


def _run_async(coro):
    global _worker_loop, _worker_pid
    pid = os.getpid()
    if _worker_loop is None or _worker_loop.is_closed() or _worker_pid != pid:
        _worker_loop = asyncio.new_event_loop()
        _worker_pid = pid
    return _worker_loop.run_until_complete(coro)


async def _execute_wake() -> jobs.SummaryExecutionResult:
    async with get_sessionmaker()() as db:
        result = await jobs.execute_next_summary_job(db, entrypoint="celery-summary-worker")
        await db.commit()
        return result


@celery_app.task(name="recantor.process_summary_wake")
def process_summary_wake() -> None:
    result = _run_async(_execute_wake())
    logger.info(
        "summary scheduler wake finished status=%s dispatch=%s session=%s",
        result.status.value,
        result.dispatch_id,
        result.session_id,
    )
    if result.status in {
        jobs.SummaryExecutionStatus.PARTIAL,
        jobs.SummaryExecutionStatus.RETRY_SCHEDULED,
    }:
        try:
            ensure_summary_wake_capacity(1)
        except (OSError, RedisError):
            logger.info("summary wake was not republished")


@celery_app.task(name="recantor.reconcile_summary_work")
def reconcile_summary_work() -> None:
    result = _run_async(_reconcile())
    if result.wake_target:
        try:
            ensure_summary_wake_capacity(result.wake_target)
        except (OSError, RedisError):
            logger.info("summary reconcile wake was not published")


async def _reconcile() -> jobs.SummaryReconcileResult:
    async with get_sessionmaker()() as db:
        result = await jobs.reconcile_summary_work(db)
        await db.commit()
        return result


def ensure_summary_wake_capacity(target: int) -> int:
    if target < 0:
        raise ValueError("summary wake target must be non-negative")
    if target == 0:
        return 0
    current_settings = get_settings()
    queue_name = current_settings.meeting_essence_queue_name
    timeout = float(current_settings.meeting_essence_broker_socket_timeout_seconds)
    redis_client = Redis.from_url(
        current_settings.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    lock = redis_client.lock(
        f"recantor:summary:wake-coalesce:{queue_name}",
        timeout=5,
        blocking_timeout=1,
    )
    published = 0
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=True))
        if not acquired:
            return 0
        queued = int(redis_client.llen(queue_name))
        missing = max(0, target - queued)
        for _ in range(missing):
            celery_app.send_task("recantor.process_summary_wake", queue=queue_name)
            published += 1
        return published
    finally:
        if acquired:
            lock.release()
        redis_client.close()
