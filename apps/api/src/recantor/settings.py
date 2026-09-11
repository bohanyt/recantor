from functools import lru_cache

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
    groq_stt_timeout_seconds: float = 30.0
    stt_queue_name: str = "stt-live"
    stt_claim_lease_seconds: float = 60.0
    stt_max_attempts: int = 5
    stt_retry_base_seconds: float = 2.0
    stt_retry_max_seconds: float = 60.0
    stt_configuration_retry_seconds: float = 300.0
    stt_reconcile_interval_seconds: float = 1.0
    stt_reconcile_batch_size: int = 100
    stt_dispatch_reenqueue_seconds: float = 15.0
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
