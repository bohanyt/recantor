from fastapi import APIRouter

from recantor.routes.realtime_audio import router as realtime_audio_router
from recantor.routes.recording import router as recording_router
from recantor.routes.transcript import router as transcript_router
from recantor.routes.utterance import router as utterance_router
from recantor.schemas import ApiMetaResponse

router = APIRouter(prefix="/api/v1", tags=["api-v1"])
router.include_router(recording_router)
router.include_router(transcript_router)
router.include_router(utterance_router)
router.include_router(realtime_audio_router)


@router.get("/meta", response_model=ApiMetaResponse, operation_id="getApiMeta")
async def get_api_meta() -> ApiMetaResponse:
    return ApiMetaResponse()
