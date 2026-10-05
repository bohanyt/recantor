from __future__ import annotations

import asyncio
import logging
import os

from redis import Redis

from recantor.media_processing import process_next_upload
from recantor.settings import get_settings
from recantor.stt_tasks import celery_app

logger = logging.getLogger(__name__)

_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_pid: int | None = None


def _run_async(coro):
    global _worker_loop, _worker_pid
    pid = os.getpid()
    if _worker_loop is None or _worker_loop.is_closed() or _worker_pid != pid:
        _worker_loop = asyncio.new_event_loop()
        _worker_pid = pid
    return _worker_loop.run_until_complete(coro)


@celery_app.task(name="recantor.process_media_upload_wake")
def process_media_upload_wake() -> None:
    processed = _run_async(process_next_upload())
    logger.info("media upload scheduler wake processed=%s", processed)


def ensure_media_wake_capacity(target: int) -> int:
    if target < 0:
        raise ValueError("media wake target must be non-negative")
    if target == 0:
        return 0

    settings = get_settings()
    queue_name = settings.media_queue_name
    redis_client = Redis.from_url(settings.redis_url)
    lock = redis_client.lock(
        f"recantor:media:wake-coalesce:{queue_name}",
        timeout=5,
        blocking_timeout=1,
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=True))
        if not acquired:
            return 0
        queued = int(redis_client.llen(queue_name))
        # Alpha intentionally reserves exactly one media-processing slot. Redis carries only
        # a generic wake; backlog cardinality remains PostgreSQL state, not broker state.
        missing = max(0, min(1, target) - queued)
        for _ in range(missing):
            celery_app.send_task(
                "recantor.process_media_upload_wake",
                queue=queue_name,
            )
        return missing
    finally:
        if acquired:
            lock.release()
        redis_client.close()
