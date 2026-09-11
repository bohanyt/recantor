from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from recantor.settings import get_settings

logger = logging.getLogger(__name__)
_CHANNEL_PREFIX = "recantor:transcript:"
_REDIS_OPERATION_TIMEOUT_SECONDS = 1.0


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


async def publish_transcript_available(*, session_id: UUID, sequence: int) -> bool:
    """Best-effort publish after canonical commit; never a durability boundary."""
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
