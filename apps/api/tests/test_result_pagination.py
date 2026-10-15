from __future__ import annotations

from uuid import UUID

import pytest

from recantor.meeting_essence.http.pagination import (
    CursorBindingError,
    InvalidResultCursor,
    ResultCursor,
    decode_cursor,
    paginate_layer,
)

SESSION = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ARTIFACT = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def test_malformed_negative_and_noncanonical_cursors_are_rejected() -> None:
    with pytest.raises(InvalidResultCursor):
        decode_cursor("not-a-valid-cursor")
    negative = ResultCursor(session_id=SESSION, artifact_id=ARTIFACT, layer="records", offset=-1)
    with pytest.raises(InvalidResultCursor):
        decode_cursor(negative.encode())
    oversized = ResultCursor(
        session_id=SESSION, artifact_id=ARTIFACT, layer="records", offset=1_000_001
    )
    with pytest.raises(InvalidResultCursor):
        decode_cursor(oversized.encode())
    canonical = ResultCursor(session_id=SESSION, artifact_id=ARTIFACT, layer="records", offset=2)
    encoded = canonical.encode()
    assert decode_cursor(encoded) == canonical
    with pytest.raises(InvalidResultCursor):
        decode_cursor(encoded[:-2] + "AA")
    page, cursor = paginate_layer(
        session_id=SESSION,
        artifact_id=ARTIFACT,
        layer="records",
        items=["a", "b", "c"],
        cursor=None,
        limit=1,
    )
    assert page == ["a"]
    assert cursor is not None
    with pytest.raises(CursorBindingError):
        paginate_layer(
            session_id=SESSION,
            artifact_id=ARTIFACT,
            layer="topics",
            items=["a", "b", "c"],
            cursor=decode_cursor(cursor),
            limit=1,
        )


def test_pages_cover_every_record_once_including_unicode() -> None:
    items = [f"butir {index} 𐍈" if index == 64 else f"butir {index}" for index in range(70)]
    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = paginate_layer(
            session_id=SESSION,
            artifact_id=ARTIFACT,
            layer="records",
            items=items,
            cursor=decode_cursor(cursor),
            limit=64,
        )
        seen.extend(page)
        if cursor is None:
            break
    assert seen == items
    assert "𐍈" in seen[64]
