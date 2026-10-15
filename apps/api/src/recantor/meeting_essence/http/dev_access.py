from __future__ import annotations

from fastapi import HTTPException, status

from recantor.settings import Settings


def require_trusted_dev_api(settings: Settings) -> None:
    if not settings.meeting_essence_dev_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="meeting essence development API is disabled",
        )
