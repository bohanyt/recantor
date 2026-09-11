from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from recantor.models import STTJobState


class STTJobFailureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    utterance_id: UUID
    state: STTJobState
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime | None
    last_delivery_attempt_at: datetime | None
    last_error_category: str | None
    last_error_code: str | None
    last_error_message: str | None
    updated_at: datetime


class STTSchedulingDiagnosticsResponse(BaseModel):
    session_id: UUID
    total: int = Field(ge=0)
    counts: dict[str, int]
    recent_failures: list[STTJobFailureResponse]
