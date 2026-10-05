from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from celery import Celery
from redis import Redis

from recantor.settings import get_settings
from recantor.stt import GroqSTTProvider
from recantor.stt_jobs import STTWorkloadClass, execute_next_reserved_stt_job

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


def _execute_generic_wake(workload_class: STTWorkloadClass) -> None:
    provider = GroqSTTProvider.from_settings()
    result = _run_async(
        execute_next_reserved_stt_job(provider=provider, workload_class=workload_class)
    )
    logger.info(
        "STT %s scheduler wake finished with status=%s state=%s attempts=%s",
        workload_class.value,
        result.status.value,
        None if result.state is None else result.state.value,
        result.attempt_count,
    )


@celery_app.task(name="recantor.process_stt_wake")
def process_stt_wake() -> None:
    # Historical task name is the live-class wake. Old broker messages remain safe.
    _execute_generic_wake(STTWorkloadClass.LIVE)


@celery_app.task(name="recantor.process_upload_stt_wake")
def process_upload_stt_wake() -> None:
    _execute_generic_wake(STTWorkloadClass.UPLOAD)


@celery_app.task(name="recantor.process_stt_utterance")
def process_stt_utterance(utterance_id: str) -> None:
    try:
        UUID(utterance_id)
    except ValueError:
        logger.warning("Discarding malformed legacy STT wake payload")
        return
    logger.info("Treating legacy per-work STT message as a live scheduler wake")
    _execute_generic_wake(STTWorkloadClass.LIVE)


def _queue_and_task(workload_class: STTWorkloadClass) -> tuple[str, str]:
    current_settings = get_settings()
    if workload_class == STTWorkloadClass.UPLOAD:
        return current_settings.stt_upload_queue_name, "recantor.process_upload_stt_wake"
    return current_settings.stt_queue_name, "recantor.process_stt_wake"


def ensure_stt_wake_capacity(
    target: int,
    workload_class: STTWorkloadClass = STTWorkloadClass.LIVE,
) -> int:
    if target < 0:
        raise ValueError("STT wake target must be non-negative")
    if target == 0:
        return 0
    current_settings = get_settings()
    queue_name, task_name = _queue_and_task(workload_class)
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
            celery_app.send_task(task_name, queue=queue_name)
            published += 1
        return published
    finally:
        if acquired:
            lock.release()
        redis_client.close()


def ensure_live_stt_wake_capacity(target: int) -> int:
    return ensure_stt_wake_capacity(target, STTWorkloadClass.LIVE)


def ensure_upload_stt_wake_capacity(target: int) -> int:
    return ensure_stt_wake_capacity(target, STTWorkloadClass.UPLOAD)


def enqueue_stt_utterance(utterance_id: UUID) -> None:
    del utterance_id
    ensure_live_stt_wake_capacity(1)
