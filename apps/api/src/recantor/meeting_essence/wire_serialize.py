from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def serialized_json_text(payload: Any) -> str:
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def serialized_utf8_byte_length(payload: Any) -> int:
    return len(serialized_json_text(payload).encode("utf-8"))


def schema_json_utf8_bytes(model_type: type[BaseModel]) -> int:
    return len(json.dumps(model_type.model_json_schema(), sort_keys=True).encode("utf-8"))
