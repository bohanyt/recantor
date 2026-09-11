from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recantor.db import get_db_session, get_sessionmaker
from recantor.models import RecordingSession
from recantor.schemas import TranscriptPageResponse, TranscriptSegmentResponse
from recantor.transcript import TranscriptConflict, TranscriptNotFound, read_transcript_segments
from recantor.transcript_realtime import (
    create_transcript_redis,
    decode_transcript_notice,
    transcript_channel,
)

router = APIRouter(prefix="/sessions", tags=["transcript"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
_PUBSUB_TIMEOUT_SECONDS = 1.0
_KEEPALIVE_SECONDS = 10.0


async def _safe_send_json(websocket: WebSocket, payload: dict[str, object]) -> bool:
    try:
        await websocket.send_json(payload)
    except (RuntimeError, WebSocketDisconnect):
        return False
    return True


async def _safe_close(websocket: WebSocket, code: int) -> None:
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)


@router.get(
    "/{session_id}/transcript",
    response_model=TranscriptPageResponse,
    operation_id="getSessionTranscript",
)
async def get_session_transcript(
    session_id: UUID,
    db: DbSession,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TranscriptPageResponse:
    try:
        segments, has_more = await read_transcript_segments(
            db,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except TranscriptNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TranscriptConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    next_after_sequence = segments[-1].sequence if segments else after_sequence
    return TranscriptPageResponse(
        session_id=session_id,
        after_sequence=after_sequence,
        next_after_sequence=next_after_sequence,
        has_more=has_more,
        segments=[TranscriptSegmentResponse.model_validate(segment) for segment in segments],
    )


@router.websocket("/{session_id}/transcript/live", name="sessionTranscriptLive")
async def session_transcript_live(websocket: WebSocket, session_id: UUID) -> None:
    """Deliver ephemeral wakeups; clients rebuild transcript state through HTTP reads."""
    await websocket.accept()

    async with get_sessionmaker()() as db:
        session_exists = await db.scalar(
            select(RecordingSession.id).where(RecordingSession.id == session_id)
        )
    if session_exists is None:
        await _safe_send_json(
            websocket,
            {"type": "error", "code": "session_not_found", "detail": "recording session not found"},
        )
        await _safe_close(websocket, 1008)
        return

    redis = create_transcript_redis()
    pubsub = redis.pubsub()
    channel = transcript_channel(session_id)
    subscribed = False
    loop = asyncio.get_running_loop()
    next_keepalive = loop.time() + _KEEPALIVE_SECONDS

    try:
        try:
            await asyncio.wait_for(pubsub.subscribe(channel), timeout=_PUBSUB_TIMEOUT_SECONDS)
            subscribed = True
        except (RedisError, OSError, TimeoutError):
            await _safe_send_json(
                websocket,
                {
                    "type": "delivery_degraded",
                    "detail": "Live transcript updates are temporarily unavailable.",
                },
            )
            await _safe_close(websocket, 1013)
            return

        if not await _safe_send_json(websocket, {"type": "ready"}):
            return

        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=_PUBSUB_TIMEOUT_SECONDS,
                    ),
                    timeout=_PUBSUB_TIMEOUT_SECONDS + 0.25,
                )
            except (RedisError, OSError, TimeoutError):
                await _safe_send_json(
                    websocket,
                    {
                        "type": "delivery_degraded",
                        "detail": "Live transcript updates are temporarily unavailable.",
                    },
                )
                await _safe_close(websocket, 1013)
                return

            if message is not None and message.get("type") == "message":
                notice = decode_transcript_notice(message.get("data"), session_id=session_id)
                if notice is not None and not await _safe_send_json(websocket, notice):
                    return

            if loop.time() >= next_keepalive:
                if not await _safe_send_json(websocket, {"type": "keepalive"}):
                    return
                next_keepalive = loop.time() + _KEEPALIVE_SECONDS
    except WebSocketDisconnect:
        return
    finally:
        if subscribed:
            with suppress(RedisError, OSError, RuntimeError, TimeoutError):
                await asyncio.wait_for(
                    pubsub.unsubscribe(channel),
                    timeout=_PUBSUB_TIMEOUT_SECONDS,
                )
        with suppress(RedisError, OSError, RuntimeError, TimeoutError):
            await pubsub.aclose()
        with suppress(RedisError, OSError, RuntimeError, TimeoutError):
            await redis.aclose()
