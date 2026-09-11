from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from celery import Celery

from recantor.settings import get_settings
from recantor.stt import GroqSTTProvider
from recantor.stt_jobs import execute_stt_job

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


@celery_app.task(name="recantor.process_stt_utterance")
def process_stt_utterance(utterance_id: str) -> None:
    work_id = UUID(utterance_id)
    provider = GroqSTTProvider.from_settings()
    result = _run_async(execute_stt_job(utterance_id=work_id, provider=provider))
    logger.info(
        "STT job %s finished worker attempt with status=%s state=%s attempts=%s",
        work_id,
        result.status.value,
        None if result.state is None else result.state.value,
        result.attempt_count,
    )


def enqueue_stt_utterance(utterance_id: UUID) -> None:
    celery_app.send_task(
        "recantor.process_stt_utterance",
        args=[str(utterance_id)],
        queue=get_settings().stt_queue_name,
    )
