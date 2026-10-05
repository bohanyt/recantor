from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from recantor.models import SessionKind, SessionState

CAPABILITY_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43,128}$"


class CreateUploadSessionRequest(BaseModel):
    client_request_id: UUID
    capability_token: str = Field(
        min_length=43,
        max_length=128,
        pattern=CAPABILITY_TOKEN_PATTERN,
        repr=False,
    )
    original_filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", min_length=1, max_length=128)
    byte_length: int = Field(gt=0)
    duration_ms: int | None = Field(default=None, gt=0)


class UploadSessionResponse(BaseModel):
    session_id: UUID
    client_request_id: UUID
    kind: SessionKind
    state: SessionState
    original_filename: str
    content_type: str
    declared_byte_length: int
    declared_duration_ms: int | None
    received_bytes: int
    expires_at: datetime
    completed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    upload_endpoint: str


class TusUploadInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = Field(default=None, alias="ID")
    size: int = Field(alias="Size", ge=0)
    size_is_deferred: bool = Field(default=False, alias="SizeIsDeferred")
    offset: int = Field(default=0, alias="Offset", ge=0)
    metadata: dict[str, str] = Field(default_factory=dict, alias="MetaData")
    storage: dict[str, Any] | None = Field(default=None, alias="Storage")


class TusHttpRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    method: str = Field(default="", alias="Method")
    uri: str = Field(default="", alias="URI")
    header: dict[str, list[str]] = Field(default_factory=dict, alias="Header")


class TusEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    upload: TusUploadInfo = Field(alias="Upload")
    http_request: TusHttpRequest = Field(default_factory=TusHttpRequest, alias="HTTPRequest")


class TusHookRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    type: str = Field(alias="Type")
    event: TusEvent = Field(alias="Event")


class TusHookHttpResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status_code: int = Field(default=0, alias="StatusCode")
    body: str = Field(default="", alias="Body")
    header: dict[str, str] = Field(default_factory=dict, alias="Header")


class TusHookResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    http_response: TusHookHttpResponse | None = Field(default=None, alias="HTTPResponse")
    reject_upload: bool = Field(default=False, alias="RejectUpload")
    stop_upload: bool = Field(default=False, alias="StopUpload")
