from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from celery import Celery
from redis import Redis

from recantor.settings import get_settings
from recantor.stt import GroqSTTProvider
from recantor.stt_jobs import execute_next_reserved_stt_job

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("recantor-stt", broker=settings.redis_url)
celery_app.conf.update(
    task_default_queue=settings.stt_queue_name,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
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


def _execute_generic_wake() -> None:
    provider = GroqSTTProvider.from_settings()
    result = _run_async(execute_next_reserved_stt_job(provider=provider))
    logger.info(
        "STT scheduler wake finished with status=%s state=%s attempts=%s",
        result.status.value,
        None if result.state is None else result.state.value,
        result.attempt_count,
    )


@celery_app.task(name="recantor.process_stt_wake")
def process_stt_wake() -> None:
    _execute_generic_wake()


@celery_app.task(name="recantor.process_stt_utterance")
def process_stt_utterance(utterance_id: str) -> None:
    # Compatibility fence for any per-work messages published by an older process during
    # a rolling development restart. The old payload is deliberately *not* authoritative:
    # PostgreSQL selects the currently reserved/fair job exactly as a generic wake does.
    try:
        UUID(utterance_id)
    except ValueError:
        logger.warning("Discarding malformed legacy STT wake payload")
        return
    logger.info("Treating legacy per-work STT message as a generic scheduler wake")
    _execute_generic_wake()


def ensure_stt_wake_capacity(target: int) -> int:
    """Coalesce broker wakes to current PostgreSQL reservation demand.

    Redis is allowed to forget everything. Each reconciliation pass calls this helper with
    the current durable reservation count, so a flush/lost publish is replenished. Conversely,
    a stopped consumer cannot accumulate one per-work FIFO prefix per cooldown window: queued
    messages are generic and this helper only tops the queue up to the current target.
    """

    if target < 0:
        raise ValueError("STT wake target must be non-negative")
    if target == 0:
        return 0

    current_settings = get_settings()
    queue_name = current_settings.stt_queue_name
    redis_client = Redis.from_url(current_settings.redis_url)
    lock = redis_client.lock(
        f"recantor:stt:wake-coalesce:{queue_name}",
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
            celery_app.send_task(
                "recantor.process_stt_wake",
                queue=queue_name,
            )
            published += 1
        return published
    finally:
        if acquired:
            lock.release()
        redis_client.close()


def enqueue_stt_utterance(utterance_id: UUID) -> None:
    # Kept as a compatibility/operator helper. It emits a generic wake, never a durable
    # per-work service-order message. Normal reconciliation uses ensure_stt_wake_capacity().
    del utterance_id
    ensure_stt_wake_capacity(1)
