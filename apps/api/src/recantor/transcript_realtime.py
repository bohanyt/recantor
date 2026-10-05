from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any
from uuid import UUID

from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from recantor.settings import get_settings

logger = logging.getLogger(__name__)
_CHANNEL_PREFIX = "recantor:transcript:"
_REDIS_OPERATION_TIMEOUT_SECONDS = 1.0
_DEFAULT_NOTIFIER_CAPACITY = 64

RealtimePublisher = Callable[[UUID, int], bool]


def transcript_channel(session_id: UUID) -> str:
    return f"{_CHANNEL_PREFIX}{session_id}"


def transcript_notice(session_id: UUID, sequence: int) -> dict[str, object]:
    return {
        "type": "transcript_available",
        "session_id": str(session_id),
        "sequence": sequence,
    }


def decode_transcript_notice(payload: Any, *, session_id: UUID) -> dict[str, object] | None:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "transcript_available":
        return None
    if parsed.get("session_id") != str(session_id):
        return None
    sequence = parsed.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        return None
    return transcript_notice(session_id, sequence)


def create_transcript_redis() -> Redis:
    return Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )


def create_transcript_sync_redis() -> SyncRedis:
    return SyncRedis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )


def _publish_transcript_available_sync(session_id: UUID, sequence: int) -> bool:
    client = create_transcript_sync_redis()
    try:
        payload = json.dumps(transcript_notice(session_id, sequence), separators=(",", ":"))
        client.publish(transcript_channel(session_id), payload)
        return True
    except (RedisError, OSError, TimeoutError, ValueError):
        logger.warning(
            "transcript realtime notification unavailable for session %s sequence %s",
            session_id,
            sequence,
            exc_info=True,
        )
        return False
    finally:
        with suppress(RedisError, OSError, TimeoutError):
            client.close()


class TranscriptRealtimeNotifier:
    """One bounded process-local publisher lane for ephemeral transcript wake-ups."""

    def __init__(
        self,
        *,
        max_pending: int = _DEFAULT_NOTIFIER_CAPACITY,
        publisher: RealtimePublisher | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("realtime notifier capacity must be positive")
        self._queue: Queue[tuple[UUID, int]] = Queue(maxsize=max_pending)
        self._publisher = publisher or _publish_transcript_available_sync
        self._start_lock = Lock()
        self._thread: Thread | None = None

    def enqueue(self, *, session_id: UUID, sequence: int) -> bool:
        self._ensure_started()
        try:
            self._queue.put_nowait((session_id, sequence))
        except Full:
            logger.warning(
                "dropping transcript realtime wake-up because notifier queue is full: "
                "%s sequence %s",
                session_id,
                sequence,
            )
            return False
        return True

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name="recantor-transcript-realtime-publisher",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            session_id, sequence = self._queue.get()
            try:
                self._publisher(session_id, sequence)
            except Exception:
                logger.exception(
                    "unexpected transcript realtime publisher failure for session %s sequence %s",
                    session_id,
                    sequence,
                )
            finally:
                self._queue.task_done()


_default_notifier = TranscriptRealtimeNotifier()


def enqueue_transcript_available(*, session_id: UUID, sequence: int) -> bool:
    """Queue a best-effort wake-up without waiting for Redis or growing work without bound."""
    return _default_notifier.enqueue(session_id=session_id, sequence=sequence)


async def publish_transcript_available(*, session_id: UUID, sequence: int) -> bool:
    """Direct async publisher retained for focused transport tests and diagnostics."""
    client = create_transcript_redis()
    try:
        payload = json.dumps(transcript_notice(session_id, sequence), separators=(",", ":"))
        await asyncio.wait_for(
            client.publish(transcript_channel(session_id), payload),
            timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        )
        return True
    except (RedisError, OSError, TimeoutError, ValueError):
        logger.warning(
            "transcript realtime notification unavailable for session %s sequence %s",
            session_id,
            sequence,
            exc_info=True,
        )
        return False
    finally:
        with suppress(RedisError, OSError, TimeoutError):
            await client.aclose()
