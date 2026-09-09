from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "recantor-api"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"


class ApiMetaResponse(BaseModel):
    name: str = "Recantor API"
    api_version: Literal["v1"] = "v1"
