from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

UploadResultState = Literal[
    "uploading",
    "preparing",
    "transcribing",
    "complete",
    "no_speech",
    "failed",
]
UploadExportFormat = Literal["txt", "json", "vtt", "srt"]


class UploadResultStatusResponse(BaseModel):
    session_id: UUID
    state: UploadResultState
    transcript_segment_count: int = Field(ge=0)
    exports_available: bool
    completed_at: datetime | None
    expires_at: datetime
    failure_message: str | None = None
