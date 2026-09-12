import re
from functools import lru_cache
from pathlib import PurePosixPath

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    recantor_env: str = "development"
    recantor_app_name: str = "Recantor API"
    database_url: str = "postgresql+psycopg://recantor:recantor@localhost:5432/recantor"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:5173"
    audio_storage_path: str = "./data/audio"
    recording_heartbeat_timeout_seconds: int = 20
    recording_max_chunk_bytes: int = 16 * 1024 * 1024
    upload_max_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=1)
    upload_max_duration_seconds: int = Field(default=12 * 60 * 60, ge=1)
    upload_capability_ttl_seconds: int = Field(default=24 * 60 * 60, ge=60)
    upload_tus_storage_prefix: str = "uploads/tus"
    tus_public_endpoint: str = "http://localhost:1080/files/"
    realtime_max_pcm_packet_bytes: int = 64 * 1024
    realtime_vad_pre_roll_ms: int = 200
    realtime_vad_min_voiced_ms: int = 160
    realtime_vad_silence_ms: int = 600
    realtime_vad_hard_max_ms: int = 8_000
    realtime_vad_absolute_threshold_dbfs: float = -50.0
    realtime_vad_noise_margin_db: float = 12.0
    groq_api_key: str | None = None
    groq_stt_endpoint: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    groq_stt_model: str = "whisper-large-v3-turbo"
    groq_stt_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # Live STT keeps the historical queue/settings. Upload STT has independent broker/worker
    # capacity while both classes share one PostgreSQL STTJob model and retry vocabulary.
    stt_queue_name: str = "stt-live"
    stt_upload_queue_name: str = "stt-upload"
    stt_claim_lease_seconds: float = Field(default=60.0, ge=1, le=3600)
    stt_max_attempts: int = Field(default=5, ge=1, le=20)
    stt_retry_base_seconds: float = Field(default=2.0, ge=0.1, le=300)
    stt_retry_max_seconds: float = Field(default=60.0, ge=0.1, le=3600)
    stt_configuration_retry_seconds: float = Field(default=300.0, ge=1, le=86400)
    stt_reconcile_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    stt_reconcile_batch_size: int = Field(default=100, ge=1, le=256)
    stt_upload_reconcile_batch_size: int = Field(default=32, ge=1, le=256)
    stt_reconcile_per_session_limit: int = Field(default=2, ge=1, le=32)
    stt_upload_reconcile_per_session_limit: int = Field(default=2, ge=1, le=32)
    stt_dispatch_reenqueue_seconds: float = Field(default=15.0, ge=1, le=3600)

    media_upload_queue_name: str = "media-upload"
    media_claim_lease_seconds: float = Field(default=1200.0, ge=30, le=7200)
    media_max_attempts: int = Field(default=3, ge=1, le=10)
    media_retry_base_seconds: float = Field(default=5.0, ge=0.1, le=300)
    media_reconcile_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    media_probe_timeout_seconds: float = Field(default=20.0, ge=1, le=120)
    media_normalize_timeout_seconds: float = Field(default=900.0, ge=10, le=3600)
    media_subprocess_output_limit_bytes: int = Field(default=256 * 1024, ge=4096, le=4 * 1024 * 1024)
    log_level: str = "INFO"

    @field_validator("stt_queue_name", "stt_upload_queue_name", "media_upload_queue_name")
    @classmethod
    def validate_queue_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("queue name must not be blank")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", normalized) is None:
            raise ValueError("queue name must use only A-Z, a-z, 0-9, dot, dash, underscore")
        return normalized

    @field_validator("upload_tus_storage_prefix")
    @classmethod
    def validate_upload_tus_storage_prefix(cls, value: str) -> str:
        normalized = value.strip().strip("/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError("upload tus storage prefix must be relative and stay inside audio root")
        return normalized

    @field_validator("tus_public_endpoint")
    @classmethod
    def validate_tus_public_endpoint(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("tus public endpoint must be an http(s) URL")
        return normalized.rstrip("/") + "/"

    @model_validator(mode="after")
    def validate_retry_windows(self) -> "Settings":
        if self.stt_retry_max_seconds < self.stt_retry_base_seconds:
            raise ValueError("STT retry maximum delay must be >= retry base delay")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
