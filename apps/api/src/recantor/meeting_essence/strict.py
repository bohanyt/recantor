from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Strict contract models; preserve exact source UTF-8 (no global str strip)."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )
