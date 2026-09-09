from fastapi import APIRouter

from recantor.schemas import ApiMetaResponse

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


@router.get("/meta", response_model=ApiMetaResponse, operation_id="getApiMeta")
async def get_api_meta() -> ApiMetaResponse:
    return ApiMetaResponse()
