from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_DEFAULT_PAGE = 64
_MAX_PAGE = 64
_MAX_CURSOR_CHARS = 512
_CURSOR_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
_MAX_OFFSET = 1_000_000


class InvalidResultCursor(ValueError):
    """The cursor is malformed, non-canonical, negative, oversized, or past the layer."""


class CursorBindingError(ValueError):
    """A well-formed cursor does not belong to this session, artifact, and layer."""


@dataclass(frozen=True)
class ResultCursor:
    session_id: UUID
    artifact_id: UUID
    layer: str
    offset: int

    def encode(self) -> str:
        payload = {
            "session_id": str(self.session_id),
            "artifact_id": str(self.artifact_id),
            "layer": self.layer,
            "offset": self.offset,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(value: str | None) -> ResultCursor | None:
    """Return the first-page sentinel, or a canonical cursor.

    An empty value starts at offset 0. Any other value must be the exact
    encoding ``ResultCursor.encode`` would emit. Silent fallback to the first
    page is not a valid cursor.
    """
    if value is None or value == "":
        return None
    if len(value) > _MAX_CURSOR_CHARS:
        raise InvalidResultCursor("result cursor is too large")
    if any(char not in _CURSOR_ALPHABET for char in value):
        raise InvalidResultCursor("result cursor is malformed")
    padded = value + ("=" * (-len(value) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidResultCursor("result cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "artifact_id",
        "layer",
        "offset",
        "session_id",
    }:
        raise InvalidResultCursor("result cursor is not canonical")
    try:
        session_id = UUID(str(payload["session_id"]))
        artifact_id = UUID(str(payload["artifact_id"]))
        layer = payload["layer"]
        offset = payload["offset"]
        if not isinstance(layer, str) or not layer:
            raise TypeError
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError
        cursor = ResultCursor(
            session_id=session_id,
            artifact_id=artifact_id,
            layer=layer,
            offset=offset,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidResultCursor("result cursor is not canonical") from exc
    if cursor.offset < 0 or cursor.offset > _MAX_OFFSET:
        raise InvalidResultCursor("result cursor offset is outside the allowed range")
    if str(payload["session_id"]) != str(cursor.session_id):
        raise InvalidResultCursor("result cursor session id is not canonical")
    if str(payload["artifact_id"]) != str(cursor.artifact_id):
        raise InvalidResultCursor("result cursor artifact id is not canonical")
    if cursor.encode() != value:
        raise InvalidResultCursor("result cursor is not canonical")
    return cursor


def paginate_layer(
    *,
    session_id: UUID,
    artifact_id: UUID,
    layer: str,
    items: list[Any],
    cursor: ResultCursor | None,
    limit: int,
) -> tuple[list[Any], str | None]:
    if limit < 1 or limit > _MAX_PAGE:
        raise InvalidResultCursor("result page limit is outside 1..64")
    if cursor is not None:
        if (
            cursor.session_id != session_id
            or cursor.artifact_id != artifact_id
            or cursor.layer != layer
        ):
            raise CursorBindingError("result cursor does not match this artifact session binding")
        start = cursor.offset
    else:
        start = 0
    if start > len(items):
        raise InvalidResultCursor("result cursor is past the end of this layer")
    page = items[start : start + limit]
    next_offset = start + len(page)
    if next_offset >= len(items):
        return page, None
    return page, ResultCursor(
        session_id=session_id,
        artifact_id=artifact_id,
        layer=layer,
        offset=next_offset,
    ).encode()


DEFAULT_PAGE_SIZE = _DEFAULT_PAGE
MAX_PAGE_SIZE = _MAX_PAGE
