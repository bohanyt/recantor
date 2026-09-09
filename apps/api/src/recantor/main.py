from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from recantor.routes.health import router as health_router
from recantor.routes.v1 import router as v1_router
from recantor.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Recantor API",
        version="0.1.0",
        description="Self-hosted recording and meeting-intelligence API.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


app = create_app()
