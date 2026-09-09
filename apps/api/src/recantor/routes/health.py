from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from recantor.db import check_database
from recantor.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["operations"])


async def require_database() -> None:
    try:
        await check_database()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc


@router.get("/healthz", response_model=HealthResponse, operation_id="healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz", response_model=ReadinessResponse, operation_id="readyz")
async def readyz(_: Annotated[None, Depends(require_database)]) -> ReadinessResponse:
    return ReadinessResponse()
